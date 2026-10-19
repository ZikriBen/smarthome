"""Minimal YAML-subset parser, used only when PyYAML is not installed.

Supported subset (enough for list.yaml):
  * block mappings  ``key: value``  and nested blocks by indentation (spaces only)
  * block lists     ``- value``  /  ``- key: value`` (list of mappings)
  * flow lists      ``[a, b, "c d"]`` (no nesting)
  * scalars: int, float, true/false, null/~, 'single' / "double" quoted, bare strings
  * comments: full-line ``# ...`` and trailing `` # ...`` (outside quotes)
Not supported: anchors, multi-line strings, flow mappings, tabs.
"""
from __future__ import annotations

import re

_KEY_RE = re.compile(r"""^([^\s'"\[\]{}#\-][^:]*?|"[^"]*"|'[^']*'):(?:\s+(.*))?$""")


class YamlSubsetError(ValueError):
    pass


def _strip_comment(line: str) -> str:
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            # quote only opens at token start
            if i == 0 or line[i - 1] in " [,:-":
                quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def parse_scalar(s: str):
    s = s.strip()
    if s == "" or s in ("~", "null", "Null", "NULL"):
        return None
    if s == "{}":
        return {}
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        parts, cur, quote = [], [], None
        for ch in inner:
            if quote:
                cur.append(ch)
                if ch == quote:
                    quote = None
            elif ch in "\"'" and not "".join(cur).strip():   # quote only at token start
                quote = ch
                cur.append(ch)
            elif ch == ",":
                parts.append("".join(cur))
                cur = []
            else:
                cur.append(ch)
        parts.append("".join(cur))
        return [parse_scalar(p) for p in parts]
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    if s in ("true", "True", "yes"):
        return True
    if s in ("false", "False", "no"):
        return False
    if re.fullmatch(r"[-+]?\d+", s):
        return int(s)
    if re.fullmatch(r"[-+]?\d*\.\d+", s):
        return float(s)
    return s


def _lines(text: str):
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise YamlSubsetError(f"line {n}: tabs are not allowed for indentation")
        line = _strip_comment(raw)
        if not line.strip() or line.strip() == "---":
            continue
        indent = len(line) - len(line.lstrip(" "))
        out.append((indent, line.strip(), n))
    return out


def loads(text: str):
    lines = _lines(text)
    if not lines:
        return None
    value, i = _block(lines, 0, lines[0][0])
    if i != len(lines):
        raise YamlSubsetError(f"line {lines[i][2]}: unexpected indentation")
    return value


def _is_list_item(content: str) -> bool:
    return content == "-" or content.startswith("- ")


def _block(lines, i, indent):
    if _is_list_item(lines[i][1]):
        return _list(lines, i, indent)
    return _mapping(lines, i, indent)


def _value_after_key(lines, i, indent, rest):
    """rest = text after 'key:'; i points to next line. Returns (value, i)."""
    if rest is not None and rest.strip() != "":
        return parse_scalar(rest), i
    if i < len(lines):
        nindent, ncontent, _ = lines[i]
        if nindent > indent or (nindent == indent and _is_list_item(ncontent)):
            return _block(lines, i, nindent)
    return None, i


def _mapping(lines, i, indent, first=None):
    result = {}
    if first is not None:
        key, rest = first
        val, i = _value_after_key(lines, i, indent, rest)
        result[key] = val
    while i < len(lines):
        ind, content, n = lines[i]
        if ind < indent:
            break
        if ind > indent:
            raise YamlSubsetError(f"line {n}: unexpected indentation")
        if _is_list_item(content):
            break
        m = _KEY_RE.match(content)
        if not m:
            raise YamlSubsetError(f"line {n}: expected 'key: value', got {content!r}")
        key = parse_scalar(m.group(1))
        val, i = _value_after_key(lines, i + 1, indent, m.group(2))
        result[key] = val
    return result, i


def _list(lines, i, indent):
    result = []
    while i < len(lines):
        ind, content, n = lines[i]
        if ind != indent or not _is_list_item(content):
            if ind > indent:
                raise YamlSubsetError(f"line {n}: unexpected indentation")
            break
        rest = content[1:].strip()
        if rest == "":
            if i + 1 < len(lines) and lines[i + 1][0] > indent:
                val, i = _block(lines, i + 1, lines[i + 1][0])
            else:
                val, i = None, i + 1
            result.append(val)
            continue
        m = _KEY_RE.match(rest)
        if m and not rest.startswith("["):
            child_indent = indent + (len(content) - len(rest))
            val, i = _mapping(lines, i + 1, child_indent,
                              first=(parse_scalar(m.group(1)), m.group(2)))
            result.append(val)
        else:
            result.append(parse_scalar(rest))
            i += 1
    return result, i


_PLAIN_RE = re.compile(r"^[^\s\-\[\]{}#&*!|>'\"%@`,:?][^\[\]{}#,:\"']*$")


# strings full PyYAML would load as non-strings (dates, yes/no, ~) -> always quote
_YAML_TYPED = re.compile(r"^(\d{4}-\d\d?-\d\d?([Tt ].*)?|y|n|yes|no|on|off|~|[-+]?\.(inf|nan))$", re.I)


def _dump_scalar(v) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = v.isoformat() if hasattr(v, "isoformat") else str(v)   # dates from PyYAML -> plain ISO string
    if _PLAIN_RE.match(s) and parse_scalar(s) == s and s == s.strip() and not _YAML_TYPED.match(s):
        return s
    if "'" not in s:
        return "'" + s + "'"
    return '"' + s.replace('"', "״") + '"'


def dumps(obj, indent: int = 0) -> str:
    """Dump dict/list/scalars in the subset accepted by loads()."""
    pad = " " * indent
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = _dump_scalar(k)
            if isinstance(v, dict) and v:
                lines.append(f"{pad}{key}:")
                lines.append(dumps(v, indent + 2))
            elif isinstance(v, list) and v and any(isinstance(x, (dict, list)) for x in v):
                lines.append(f"{pad}{key}:")
                lines.append(dumps(v, indent + 2))
            elif isinstance(v, list):
                lines.append(f"{pad}{key}: [" + ", ".join(_dump_scalar(x) for x in v) + "]")
            elif isinstance(v, dict):
                lines.append(f"{pad}{key}: null")
            else:
                lines.append(f"{pad}{key}: {_dump_scalar(v)}")
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, dict) and v:
                sub = dumps(v, indent + 2).split("\n")
                lines.append(f"{pad}- " + sub[0].lstrip())
                lines.extend(sub[1:])
            else:
                lines.append(f"{pad}- {_dump_scalar(v)}")
    else:
        lines.append(pad + _dump_scalar(obj))
    return "\n".join(l for l in lines if l != "")
