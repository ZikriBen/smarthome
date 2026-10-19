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
           "size_max": "size_max", "terms": "terms", "unit": "unit", "qty_unit": "qty_unit",
           "kashrut": "kashrut"}


class LearnError(ValueError):
    pass


# Every spelling Hermes may send → the canonical change key. The tool description, the error text and
# this table must agree (see VALID_KEYS_HELP); field names from defaults.yaml are accepted as-is.
KEY_ALIASES = {
    "require_any": "require", "requires": "require", "must": "require", "must_have": "require",
    "exclude_words": "exclude", "excludes": "exclude", "without": "exclude",
    "brands": "brand", "prefer_brand": "brand", "prefer_brands": "brand",
    "prefer_words": "prefer", "prefers": "prefer",
    "pack": "pack_preference", "packs": "pack_preference", "pack_size": "pack_preference",
    "pinned": "pin", "product_id": "pin",
    "kosher": "kashrut", "hechsher": "kashrut", "כשרות": "kashrut",
    "notes": "note", "text": "note",
}
VALID_KEYS = ("require", "exclude", "brand", "prefer", "pack_preference", "qty", "size_min", "size_max",
              "terms", "pin", "unpin", "unbrand", "unexclude", "kashrut", "note", "delivery")
VALID_KEYS_HELP = (
    "valid keys: require (=require_any), exclude (=exclude_words), brand (=brands), prefer (=prefer_words), "
    "pack_preference, qty, size_min, size_max, terms, pin {site:id}, unpin, unbrand, unexclude, "
    "kashrut ('mehadrin'|'badatz'|'none'), note (free text, saved but not enforced), "
    "delivery {site:{fee,min_order,free_above}}. "
    'Example: item="ממרח שוקולד", change={"require": ["פרווה"], "exclude": ["חלבי"]}; '
    'global: item="כללי", change={"kashrut": "mehadrin"}. Or a short Hebrew phrase: "פרווה" / "רק X" / "בלי X" / "מהדרין".')

# item values that mean "all items" (global preference)
GLOBAL_ITEMS = {"כללי", "כללית", "הכל", "הכול", "כל המוצרים", "כל הפריטים", "תמיד", "global", "all", "*",
                "general", "default", "ברירת מחדל", "ברירות מחדל", "העדפה כללית", "העדפות כלליות"}
GLOBAL_KEYS = {"kashrut", "note"}
SPLIT_WORDS = re.compile(r"^\s*(?:בנפרד|לחוד|נפרד|תפריד|תפצל|separate(?:ly)?|split)\s*$", re.I)


def canon_change(change: Dict) -> Dict:
    """Rename alias keys (require_any → require …). Raises LearnError with the full key list when unknown."""
    out: Dict = {}
    for k, v in change.items():
        key = KEY_ALIASES.get(str(k).strip(), str(k).strip())
        if key == "pin" and not isinstance(v, dict):
            raise LearnError("pin must be {site: product_id}, e.g. {\"rami_levy\": \"419939\"}. " + VALID_KEYS_HELP)
        if key in out and isinstance(out[key], list):
            out[key] = out[key] + _as_list(v)
        else:
            out[key] = v
    from .kashrut import phrase_level
    # Hermes' natural first guess for "תמיד מהדרין/בד״ץ" is {"require_any": ["מהדרין", "בד\"צ"]}: hechsher
    # words never appear reliably in product names, so when every require/prefer value is a kashrut word
    # treat it as the kashrut rule.
    for k in ("require", "prefer"):
        vals = _as_list(out.get(k))
        lvls = [phrase_level(str(v)) for v in vals]
        if vals and all(lvls):
            out.pop(k)
            out.setdefault("kashrut", "mehadrin" if "mehadrin" in lvls else lvls[0])
    unknown = set(out) - set(VALID_KEYS) - {"scope"}
    if unknown:
        raise LearnError(f"unknown change key(s) {sorted(unknown)} — " + VALID_KEYS_HELP)
    if "kashrut" in out:
        from .kashrut import norm_level
        try:
            out["kashrut"] = norm_level(out["kashrut"]) or ""
        except ValueError as e:
            raise LearnError(f"{e}. " + VALID_KEYS_HELP)
    return out


def is_global(item: str, change=None) -> bool:
    if isinstance(change, dict) and str(change.get("scope", "")).lower() in ("all", "global", "כללי"):
        return True
    return (item or "").strip().lower() in GLOBAL_ITEMS


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [x for x in v if x not in (None, "")]
    return [v]


_NUM = r"(\d+(?:\.\d+)?)"


def _short(words: str, n: int = 3) -> bool:
    """A keyword rule must be short ('רק פרווה', 'בלי נצמד'); longer sentences become notes."""
    return 0 < len(words.split()) <= n


def parse_change(text: str, site: Optional[str] = None) -> Dict:
    """Common phrases only (Hebrew/English). Raises LearnError if nothing was understood.
    'לא X, קח Y' / 'not X, take Y' · 'בלי X' / 'without X' · 'רק X' / 'only X' · 'עדיף Y' / 'prefer Y'
    'מארז 30, אחרת 18' / 'pack 30 then 18' · 'כמות 2' / 'qty 2' · 'נעץ 12345' / 'pin 12345' (needs site)
    Leading 'תזכור ש' / 'remember that' / 'next time' / 'בפעם הבאה' is ignored."""
    s = " " + re.sub(r"\s+", " ", text.strip()) + " "
    s = re.sub(r"^\s*(תזכור ש|תזכרי ש|זכור ש|remember that|remember|next time|בפעם הבאה)\s*", " ", s, flags=re.I)
    from .kashrut import phrase_level
    lv = phrase_level(s)
    if lv is not None:
        rest = re.sub(r'מהדרין|mehadrin|בד["״\'׳]?[צץ]|badat[sz]|העדה(?:\s*החרדית)?|כשרות|כשר(?:ים|ות)?|מהודרת|גבוהה|'
                      r'חרדי(?:ת)?|לא\s*משנה|בלי|ללא|אין|ה?כל(?:\s*ה?מוצרים)?|תמיד|רק|בכללי|כללי|עדיף|'
                      r'(?:תוסיף\s*)?ל?(?:ה)?העדפות|תוסיף|any|only|always|[/\\,.:;\-–]|\sאו\s|\sו', " ", s, flags=re.I)
        if not re.sub(r"\s+", "", rest):
            return {"kashrut": lv or "none"}
    # Attribute shortcuts ("פרווה", "ללא סוכר", …) before the part loop.
    try:
        from .attrs import phrase_to_change
        bare = re.sub(r"\s+", " ", s).strip()
        # allow "רק פרווה" / "רק X" already handled below; plain attr word:
        if bare:
            try:
                ch_attr = phrase_to_change(bare)
                return ch_attr
            except KeyError:
                pass
            m = re.match(r"^(?:רק|only|עדיף|prefer)\s+(.+)$", bare, re.I)
            if m:
                try:
                    return phrase_to_change(m.group(1).strip())
                except KeyError:
                    pass
    except ImportError:
        pass
        # Whole-string delivery/min phrases first (must not go through the "." split — 29.90 has a decimal).
    ch: Dict = {}
    m = re.search(r"(?:דמי\s*משלוח|משלוח|delivery(?:\s*fee)?)\s+"
                  r"(רמי(?:\s*לוי)?|rami(?:[_\s-]?levy)?|שופרסל|shufersal)\s+" + _NUM, s, re.I)
    if m:
        site_raw = m.group(1).lower()
        site = "rami_levy" if site_raw.startswith(("רמי", "rami")) else "shufersal"
        ch.setdefault("delivery", {})[site] = {"fee": float(m.group(2))}
        s = s[:m.start()] + " " + s[m.end():]
    m = re.search(r"(?:מינימום(?:\s*הזמנה)?|min(?:imum)?(?:\s*order)?)\s+"
                  r"(רמי(?:\s*לוי)?|rami(?:[_\s-]?levy)?|שופרסל|shufersal)\s+" + _NUM, s, re.I)
    if m:
        site_raw = m.group(1).lower()
        site = "rami_levy" if site_raw.startswith(("רמי", "rami")) else "shufersal"
        ch.setdefault("delivery", {})[site] = {**ch.get("delivery", {}).get(site, {}), "min_order": float(m.group(2))}
        s = s[:m.start()] + " " + s[m.end():]
    if ch and not re.sub(r"\s+", "", s):
        return ch
    for part in re.split(r"[,;]| אלא | but |\.", s):
        p = part.strip()
        if not p:
            continue
        m = re.match(r"^(?:לא|בלי|without|not|no)\s+(.+)$", p, re.I)
        if m and _short(m.group(1)):
            ch.setdefault("exclude", []).append(m.group(1).strip()); continue
        m = re.match(r"^(?:רק|only)\s+(.+)$", p, re.I)
        if m and _short(m.group(1)):
            ch.setdefault("require", []).append(m.group(1).strip()); continue
        m = re.match(r"^(?:קח|תקח|take|עדיף|prefer|מותג)\s+(.+)$", p, re.I)
        if m and _short(m.group(1)):
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
    if user_path is None:
        user_path = os.path.splitext(defaults_path)[0] + ".user.yaml"
    if isinstance(change, str):
        text = change.strip()
        reason = reason or text
        if SPLIT_WORDS.match(text):
            return (f"לא נשמרה העדפה: \"{text}\" זה פיצול פריט ברשימה, לא העדפה. "
                    f"להפריד: grocery_remove לפריט המשולב ואז grocery_add_item עם הפריטים מופרדים בפסיק "
                    f"(\"{item}\" יתווסף כפריט נפרד).")
        try:
            change = parse_change(text, site)
        except LearnError:
            return save_note(item, text, reason=reason, user_path=user_path, today=today)
    if not isinstance(change, dict) or not change:
        raise LearnError("change must be a non-empty dict or phrase — " + VALID_KEYS_HELP)
    change = canon_change(change)
    # Delivery is site-level (not per item). "תזכור שדמי משלוח רמי 29.90" → change={"delivery":{…}}.
    if "delivery" in change and set(change) - {"scope"} == {"delivery"}:
        return _learn_delivery(change["delivery"], reason=reason, user_path=user_path, today=today)
    if is_global(item, change):
        return _learn_global(change, reason=reason, user_path=user_path, today=today)
    change.pop("scope", None)
    if set(change) == {"note"}:
        return save_note(item, str(change["note"]), reason=reason, user_path=user_path, today=today)
    note = change.pop("note", None)
    if "delivery" in change:
        raise LearnError("delivery is site-level — send it alone: change={\"delivery\": {\"rami_levy\": {\"fee\": 29.9}}}")
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
    if note:
        save_note(canonical, str(note), reason=reason, user_path=user_path, today=today)
    return f"נלמד: {canonical} — {_summary(change)}" + (f" (סיבה: {reason})" if reason else "")


def _load_user(user_path: str) -> Dict:
    data = {"version": 1, "aliases": {}}
    if user_path and os.path.exists(user_path):
        loaded = load_structured(user_path) or {}
        if isinstance(loaded, dict):
            data.update(loaded)
            data["aliases"] = dict(loaded.get("aliases") or {})
    return data


def _write_user(user_path: str, data: Dict) -> None:
    tmp = user_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# Written by upsert_default()/learn_preference() — user overrides on top of defaults.yaml\n")
        f.write(minyaml.dumps(data) + "\n")
    os.replace(tmp, user_path)


def _learn_global(change: Dict, reason: str = "", user_path: str = None, today: Optional[str] = None) -> str:
    """Preferences for every item (defaults.user.yaml → global:). Supported: kashrut, note."""
    change = {k: v for k, v in change.items() if k != "scope"}
    bad = set(change) - GLOBAL_KEYS
    if bad:
        raise LearnError(f"global preferences support only {sorted(GLOBAL_KEYS)} (got {sorted(bad)}); "
                         "per-item rules need a specific item. " + VALID_KEYS_HELP)
    if set(change) == {"note"}:
        return save_note("כללי", str(change["note"]), reason=reason, user_path=user_path, today=today)
    data = _load_user(user_path)
    g = dict(data.get("global") or {})
    from .kashrut import LEVEL_HE
    lv = change.get("kashrut")
    if lv:
        g["kashrut"] = lv
        msg = f"נלמד (כללי, לכל המוצרים): כשרות {LEVEL_HE[lv]}"
    else:
        g.pop("kashrut", None)
        msg = "נלמד (כללי): בלי דרישת כשרות"
    data["global"] = g
    hist = list(data.get("_learned_global") or [])
    hist.append({"date": today or dt.date.today().isoformat(), "reason": reason or "", "change": _summary(change)})
    data["_learned_global"] = hist[-20:]
    _write_user(user_path, data)
    if change.get("note"):
        save_note("כללי", str(change["note"]), reason=reason, user_path=user_path, today=today)
    if lv:
        msg += (" — עדיפות למוצרים עם הכשר מתאים; אם אין, נבחר הטוב ביותר ומסומן בלבדיקה"
                " (ירקות/פירות לא נבדקים)")
    return msg + (f" (סיבה: {reason})" if reason else "")


def save_note(item: str, text: str, reason: str = "", user_path: str = None, today: Optional[str] = None) -> str:
    """Free-text preference we couldn't turn into a rule: keep it (shown in the list), don't enforce it."""
    data = _load_user(user_path)
    notes = [n for n in (data.get("notes") or []) if isinstance(n, dict)]
    scope = "כללי" if is_global(item) else (item or "כללי").strip()
    if not any(n.get("item") == scope and n.get("text") == text for n in notes):
        notes.append({"item": scope, "text": text, "date": today or dt.date.today().isoformat(),
                      "reason": reason or ""})
    data["notes"] = notes[-50:]
    _write_user(user_path, data)
    return (f"נשמר כהערה (לא מיושם אוטומטית): {scope} — {text}. "
            "כדי שזה ייאכף בבחירת מוצרים אפשר לנסח למשל: \"רק X\" / \"בלי X\" / \"מותג X\" / "
            "\"פרווה\" / \"מהדרין\" / \"מארז 12\"")


def _learn_delivery(change, reason: str = "", user_path: str = None, today: Optional[str] = None) -> str:
    """Write top-level delivery: overrides into defaults.user.yaml. change = {site: {fee?, min_order?, …}}."""
    if not isinstance(change, dict) or not change:
        raise LearnError("delivery change must be {site: {fee|min_order|free_above: number}}")
    data = {"version": 1, "aliases": {}}
    if os.path.exists(user_path):
        loaded = load_structured(user_path) or {}
        if isinstance(loaded, dict):
            data.update(loaded)
            data["aliases"] = dict(loaded.get("aliases") or {})
    cur = dict(data.get("delivery") or {})
    for site, cfg in change.items():
        if not isinstance(cfg, dict):
            raise LearnError(f"delivery[{site}] must be a mapping")
        site = str(site)
        if site not in ("rami_levy", "shufersal"):
            raise LearnError(f"unknown delivery site {site}")
        entry = dict(cur.get(site) or {})
        for k in ("fee", "min_order", "free_above", "note"):
            if k in cfg:
                entry[k] = cfg[k]
        cur[site] = entry
    data["delivery"] = cur
    hist = list(data.get("_learned_delivery") or [])
    hist.append({"date": today or dt.date.today().isoformat(), "reason": reason or "",
                 "change": _summary({"delivery": change})})
    data["_learned_delivery"] = hist[-20:]
    tmp = user_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# Written by upsert_default()/learn_preference() — user overrides on top of defaults.yaml\n")
        f.write(minyaml.dumps(data) + "\n")
    os.replace(tmp, user_path)
    return f"נלמד: דמי משלוח — {_summary({'delivery': change})}" + (f" (סיבה: {reason})" if reason else "")
