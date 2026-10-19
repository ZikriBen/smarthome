"""learn_preference(): turn one correction into a durable default (defaults.user.yaml), with history.

Structured change (preferred — Hermes interprets the user, we just apply):
  {"exclude": ["נצמד"], "require": ["רדיד"], "brand": ["יטבתה"], "unbrand": ["אסם"],
   "pin": {"rami_levy": "419939"}, "unpin": "rami_levy", "pack_preference": [30, 18, 12],
   "qty": 2, "size_min": 0.9, "size_max": 2, "prefer": ["3%"], "terms": ["..."]}
List fields are MERGED into what's already known (exclude/require/brand/prefer); scalars replace.
A tiny phrase parser covers the most common corrections (see parse_change); anything else -> pass a dict.
"""
from __future__ import annotations

import datetime as dt
import os
import re
from typing import Dict, Optional, Union

from . import minyaml
from .defaults import load_defaults
from .listfile import ALIAS_KEYS, ListError, load_structured, parse_intent

MERGE_LISTS = {"exclude": "exclude_words", "require": "require_any", "brand": "brands",
               "prefer": "prefer_words", "alias": "aliases"}
REPLACE = {"pack_preference": "pack_preference", "qty": "qty", "size_min": "size_min",
           "size_max": "size_max", "terms": "terms", "unit": "unit", "qty_unit": "qty_unit"}


class LearnError(ValueError):
    pass


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [x for x in v if x not in (None, "")]
    return [v]


_NUM = r"(\d+(?:\.\d+)?)"


def parse_change(text: str, site: Optional[str] = None) -> Dict:
    """Common phrases only (Hebrew/English). Raises LearnError if nothing was understood.
    'לא X, קח Y' / 'not X, take Y' · 'בלי X' / 'without X' · 'רק X' / 'only X' · 'עדיף Y' / 'prefer Y'
    'מארז 30, אחרת 18' / 'pack 30 then 18' · 'כמות 2' / 'qty 2' · 'נעץ 12345' / 'pin 12345' (needs site)
    Leading 'תזכור ש' / 'remember that' / 'next time' / 'בפעם הבאה' is ignored."""
    s = " " + re.sub(r"\s+", " ", text.strip()) + " "
    s = re.sub(r"^\s*(תזכור ש|תזכרי ש|זכור ש|remember that|remember|next time|בפעם הבאה)\s*", " ", s, flags=re.I)
    ch: Dict = {}
    for part in re.split(r"[,;]| אלא | but |\.", s):
        p = part.strip()
        if not p:
            continue
        m = re.match(r"^(?:לא|בלי|without|not|no)\s+(.+)$", p, re.I)
        if m:
            ch.setdefault("exclude", []).append(m.group(1).strip()); continue
        m = re.match(r"^(?:רק|only)\s+(.+)$", p, re.I)
        if m:
            ch.setdefault("require", []).append(m.group(1).strip()); continue
        m = re.match(r"^(?:קח|תקח|take|עדיף|prefer|מותג)\s+(.+)$", p, re.I)
        if m:
            ch.setdefault("brand", []).append(m.group(1).strip()); continue
        m = re.match(r"^(?:נעץ|pin)\s+(\S+)$", p, re.I)
        if m:
            if not site:
                raise LearnError("pin needs a site (rami_levy / shufersal)")
            ch["pin"] = {site: m.group(1)}; continue
        m = re.match(r"^(?:כמות|qty|quantity)\s+" + _NUM + r"$", p, re.I)
        if m:
            ch["qty"] = float(m.group(1)); continue
        nums = re.findall(r"\d+(?:\.\d+)?", p)
        if re.search(r"(מארז|חבילה|pack|אחרת|then|-pack)", p, re.I) and nums:
            ch.setdefault("pack_preference", []).extend(float(n) for n in nums); continue
        if re.match(r"^(?:אחרת|else|then)\s+" + _NUM + r"$", p, re.I):
            ch.setdefault("pack_preference", []).append(float(nums[0])); continue
        raise LearnError(f"didn't understand {p!r} — pass a structured change dict instead")
    if not ch:
        raise LearnError("empty change")
    return ch


def _summary(change: Dict) -> str:
    bits = []
    for k, v in change.items():
        if isinstance(v, list):
            v = ", ".join(str(x).rstrip("0").rstrip(".") if isinstance(x, float) else str(x) for x in v)
        elif isinstance(v, dict):
            v = ", ".join(f"{a}={b}" for a, b in v.items())
        bits.append(f"{k}: {v}")
    return "; ".join(bits)


def learn_preference(item: str, change: Union[Dict, str], reason: str = "", defaults_path: str = None,
                     user_path: str = None, site: Optional[str] = None, today: Optional[str] = None) -> str:
    """Apply one correction to defaults.user.yaml. Returns a one-line confirmation (Hebrew)."""
    if not item or not item.strip():
        raise LearnError("item is required")
    if isinstance(change, str):
        reason = reason or change
        change = parse_change(change, site)
    if not isinstance(change, dict) or not change:
        raise LearnError("change must be a non-empty dict or phrase")
    unknown = set(change) - set(MERGE_LISTS) - set(REPLACE) - {"pin", "unpin", "unbrand", "unexclude"}
    if unknown:
        raise LearnError(f"unknown change key(s) {sorted(unknown)}")
    if user_path is None:
        user_path = os.path.splitext(defaults_path)[0] + ".user.yaml"
    defaults = load_defaults(defaults_path, user_path) if defaults_path else None
    canonical = (defaults.lookup(item) if defaults else None) or item.strip()
    base = dict(defaults.aliases.get(canonical, {})) if defaults else {}

    data = {"version": 1, "aliases": {}}
    if os.path.exists(user_path):
        loaded = load_structured(user_path) or {}
        if isinstance(loaded, dict):
            data.update(loaded)
            data["aliases"] = dict(loaded.get("aliases") or {})
    entry = dict(data["aliases"].get(canonical) or {})

    for key, field in MERGE_LISTS.items():
        if key in change:
            cur = list(entry.get(field) or base.get(field) or [])
            for v in _as_list(change[key]):
                if str(v) not in [str(x) for x in cur]:
                    cur.append(v)
            entry[field] = cur
    for key, field in (("unbrand", "brands"), ("unexclude", "exclude_words")):
        if key in change:
            drop = {str(x) for x in _as_list(change[key])}
            entry[field] = [x for x in (entry.get(field) or base.get(field) or []) if str(x) not in drop]
    for key, field in REPLACE.items():
        if key in change:
            v = change[key]
            entry[field] = _as_list(v) if field in ("pack_preference", "terms") else v
    if "pin" in change:
        if not isinstance(change["pin"], dict):
            raise LearnError("pin must be {site: product_id}")
        pins = dict(entry.get("pinned") or base.get("pinned") or {})
        pins.update({str(k): str(v) for k, v in change["pin"].items()})
        entry["pinned"] = pins
    if "unpin" in change:
        pins = dict(entry.get("pinned") or base.get("pinned") or {})
        for s in _as_list(change["unpin"]):
            pins.pop(str(s), None)
        entry["pinned"] = pins
    parse_intent({k: v for k, v in entry.items() if k != "_learned"}, f"learn[{canonical}]", allowed=ALIAS_KEYS)

    hist = list(entry.get("_learned") or [])
    hist.append({"date": today or dt.date.today().isoformat(), "reason": reason or "", "change": _summary(change)})
    entry["_learned"] = hist[-20:]
    data["aliases"][canonical] = entry
    tmp = user_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# Written by upsert_default()/learn_preference() — user overrides on top of defaults.yaml\n")
        f.write(minyaml.dumps(data) + "\n")
    os.replace(tmp, user_path)
    return f"נלמד: {canonical} — {_summary(change)}" + (f" (סיבה: {reason})" if reason else "")
