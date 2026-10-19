"""List / update / delete grocery preferences (defaults.yaml built-ins + defaults.user.yaml learned).

A "preference" here is one durable rule on an item (or a site-level delivery override). Hermes shows a
numbered list; Ben picks a number to edit or delete. Deletes of built-in rules are stored as user
overrides that clear the field (not a rewrite of defaults.yaml).
"""
from __future__ import annotations

import copy
import datetime as dt
import os
from typing import Dict, List, Optional, Tuple

from . import minyaml
from .attrs import ATTR_PHRASES
from .defaults import load_defaults
from .learn import LearnError, _summary, learn_preference
from .listfile import load_structured

# Fields we surface as human preferences (skip empty / noise).
RULE_FIELDS = (
    ("require_any", "חייב להכיל"),
    ("exclude_words", "בלי"),
    ("brands", "מותג מועדף"),
    ("prefer_words", "עדיף"),
    ("pack_preference", "מארז"),
    ("pinned", "נעוץ"),
    ("qty", "כמות"),
    ("size_min", "גודל מינ׳"),
    ("size_max", "גודל מקס׳"),
    ("terms", "חיפוש"),
    ("kashrut", "כשרות"),
)
NOTE_MARK = "📝 (לא מיושם אוטומטית)"


def _kashrut_he(v) -> str:
    from .kashrut import LEVEL_HE
    return LEVEL_HE.get(v, "ללא דרישה") if v else "ללא דרישה"


def _user_path(defaults_path: str, user_path: Optional[str] = None) -> str:
    if user_path:
        return user_path
    root, ext = os.path.splitext(defaults_path)
    return root + ".user" + ext


def _fmt_val(v) -> str:
    if isinstance(v, list):
        return ", ".join(str(x).rstrip("0").rstrip(".") if isinstance(x, float) else str(x) for x in v)
    if isinstance(v, dict):
        return ", ".join(f"{a}={b}" for a, b in v.items())
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _load_user(user_path: str) -> dict:
    data = {"version": 1, "aliases": {}}
    if os.path.exists(user_path):
        loaded = load_structured(user_path) or {}
        if isinstance(loaded, dict):
            data.update(loaded)
            data["aliases"] = dict(loaded.get("aliases") or {})
    return data


def _save_user(user_path: str, data: dict) -> None:
    tmp = user_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# Written by upsert_default()/learn_preference()/prefs — user overrides on top of defaults.yaml\n")
        f.write(minyaml.dumps(data) + "\n")
    os.replace(tmp, user_path)


def collect(defaults_path: str, user_path: Optional[str] = None) -> List[dict]:
    """Ordered preference rows: learned first (user file), then notable built-ins not already covered."""
    up = _user_path(defaults_path, user_path)
    d = load_defaults(defaults_path, up)
    user = _load_user(up)
    rows: List[dict] = []
    seen = set()

    # Site-level delivery overrides
    for site, cfg in (user.get("delivery") or {}).items():
        if not isinstance(cfg, dict):
            continue
        bits = []
        if "fee" in cfg:
            bits.append(f"דמי משלוח ₪{float(cfg['fee']):g}")
        if "min_order" in cfg:
            bits.append(f"מינימום ₪{float(cfg['min_order']):g}")
        if "free_above" in cfg and cfg["free_above"] not in (None, "", "null"):
            bits.append(f"חינם מעל ₪{float(cfg['free_above']):g}")
        if bits:
            rows.append({"kind": "delivery", "item": "משלוח", "site": site, "field": "delivery",
                         "label": f"משלוח / {site}: " + ", ".join(bits), "source": "learned",
                         "value": cfg})
            seen.add(("delivery", site))

    # Global (all items)
    g = user.get("global") or {}
    if isinstance(g, dict) and g.get("kashrut"):
        rows.append({"kind": "global", "item": "כללי", "site": "", "field": "kashrut",
                     "label": f"כללי (כל המוצרים): כשרות {_kashrut_he(g['kashrut'])}", "source": "learned",
                     "value": g["kashrut"]})

    # Per-item learned rules (anything in user aliases except empty/_learned-only)
    for name, entry in (user.get("aliases") or {}).items():
        if not isinstance(entry, dict):
            continue
        for field, he in RULE_FIELDS:
            if field not in entry or entry[field] in (None, [], {}) or (entry[field] == "" and field != "kashrut"):
                continue
            val = _kashrut_he(entry[field]) if field == "kashrut" else _fmt_val(entry[field])
            label = f"{name}: {he} {val}"
            rows.append({"kind": "item", "item": name, "site": "", "field": field,
                         "label": label, "source": "learned", "value": entry[field]})
            seen.add(("item", name, field))

    # Free-text notes (saved, not enforced)
    for i, n in enumerate(user.get("notes") or []):
        if isinstance(n, dict) and n.get("text"):
            rows.append({"kind": "note", "item": n.get("item") or "כללי", "site": "", "field": "note",
                         "label": f"{n.get('item') or 'כללי'}: \"{n['text']}\" {NOTE_MARK}", "source": "learned",
                         "value": n["text"], "note_index": i})

    # Notable built-ins (require/exclude/brand/pack) not overridden
    for name, entry in d.aliases.items():
        for field, he in RULE_FIELDS:
            if field not in ("require_any", "exclude_words", "brands", "pack_preference", "prefer_words"):
                continue
            if ("item", name, field) in seen:
                continue
            val = entry.get(field)
            if val in (None, "", [], {}):
                continue
            # skip huge exclude lists that are just noise for the "my prefs" view? keep them — Ben asked to see prefs
            rows.append({"kind": "item", "item": name, "site": "", "field": field,
                         "label": f"{name}: {he} {_fmt_val(val)}", "source": "builtin", "value": val})
    return rows


def list_text(defaults_path: str, user_path: Optional[str] = None, full: bool = False) -> str:
    """Learned prefs are numbered by default. full=True also numbers built-ins (so Ben can override them)."""
    rows = collect(defaults_path, user_path)
    learned = [r for r in rows if r["source"] == "learned"]
    builtin = [r for r in rows if r["source"] == "builtin"]
    if not learned and not builtin:
        return "אין העדפות שמורות עדיין. אפשר להגיד למשל: תזכור שממרח שוקולד פרווה"
    out = []
    if learned:
        out.append(f"ההעדפות שלך ({len(learned)}):")
        for i, r in enumerate(learned, 1):
            out.append(f"{i}. {r['label']}")
    else:
        out.append("ההעדפות שלך: (אין עדיין — אפשר להגיד תזכור ש…)")
    if full:
        out.append(f"ברירת מחדל ({len(builtin)}):")
        base = len(learned)
        for i, r in enumerate(builtin, 1):
            out.append(f"{base + i}. {r['label']}")
    elif builtin:
        items = list(dict.fromkeys(r["item"] for r in builtin))
        sample = ", ".join(items[:8]) + ("…" if len(items) > 8 else "")
        out.append(f"ברירת מחדל: {len(builtin)} כללים על {sample} — אמור \"העדפות מלאות\" לרשימה ממוספרת")
    out.append('למחוק: "תמחק העדפה N" · לערוך: "תשנה העדפה N ל…" · להוסיף: "תזכור ש…"')
    return "\n".join(out)


def visible_rows(defaults_path, user_path=None, full: bool = False):
    rows = collect(defaults_path, user_path)
    learned = [r for r in rows if r["source"] == "learned"]
    if full:
        return learned + [r for r in rows if r["source"] == "builtin"]
    return learned


def _row(defaults_path, user_path, index: int, full: bool = False):
    rows = visible_rows(defaults_path, user_path, full=full)
    if index < 1 or index > len(rows):
        raise LearnError(
            f"אין העדפה מספר {index} (יש {len(rows)} ברשימה"
            + ("" if full else " הנלמדת; אמור העדפות מלאות לכלול ברירות מחדל") + ")")
    return rows[index - 1], rows


def delete(index: int, defaults_path: str, user_path: Optional[str] = None,
           undo: bool = False, full: bool = False) -> str:
    up = _user_path(defaults_path, user_path)
    data = _load_user(up)
    if undo:
        snap = data.pop("_prefs_undo", None)
        if not snap:
            return "אין מחיקה אחרונה לשחזר"
        data["aliases"] = snap.get("aliases", data.get("aliases", {}))
        for k in ("global", "notes"):
            if snap.get(k):
                data[k] = snap[k]
            else:
                data.pop(k, None)
        if "delivery" in snap:
            data["delivery"] = snap["delivery"]
        elif "delivery" in data:
            del data["delivery"]
        _save_user(up, data)
        return "שוחזרה ההעדפה שנמחקה"
    row, _ = _row(defaults_path, up, index, full=full)
    data["_prefs_undo"] = {
        "aliases": copy.deepcopy(data.get("aliases") or {}),
        "delivery": copy.deepcopy(data.get("delivery") or {}),
        "global": copy.deepcopy(data.get("global") or {}),
        "notes": copy.deepcopy(data.get("notes") or []),
    }
    if row["kind"] == "global":
        g = dict(data.get("global") or {})
        g.pop(row["field"], None)
        data["global"] = g
    elif row["kind"] == "note":
        notes = list(data.get("notes") or [])
        if 0 <= row["note_index"] < len(notes):
            notes.pop(row["note_index"])
        data["notes"] = notes
    elif row["kind"] == "delivery":
        deliv = dict(data.get("delivery") or {})
        deliv.pop(row["site"], None)
        data["delivery"] = deliv
    else:
        aliases = dict(data.get("aliases") or {})
        entry = dict(aliases.get(row["item"]) or {})
        if row["source"] == "learned":
            entry.pop(row["field"], None)
            useful = {k: v for k, v in entry.items() if k != "_learned" and v not in (None, "", [], {})}
            if useful:
                aliases[row["item"]] = entry
            elif entry.get("_learned"):
                aliases[row["item"]] = {"_learned": entry["_learned"]}
            else:
                aliases.pop(row["item"], None)
        else:
            empty = {} if row["field"] == "pinned" else []
            entry[row["field"]] = empty
            hist = list(entry.get("_learned") or [])
            hist.append({"date": dt.date.today().isoformat(), "reason": "מחיקת העדפת ברירת מחדל",
                         "change": f"clear {row['field']}"})
            entry["_learned"] = hist[-20:]
            aliases[row["item"]] = entry
        data["aliases"] = aliases
    _save_user(up, data)
    return f"נמחקה: {row['label']} (אפשר \"תחזיר העדפה\" לביטול)"


def update(index: int, change, defaults_path: str, user_path: Optional[str] = None,
           reason: str = "", full: bool = False) -> str:
    """Replace one preference row. change = dict or attribute phrase ("פרווה")."""
    up = _user_path(defaults_path, user_path)
    row, _ = _row(defaults_path, up, index, full=full)
    if row["kind"] == "note":
        # replace the note with whatever the new wording turns into (a rule, or a new note)
        data = _load_user(up)
        notes = list(data.get("notes") or [])
        if 0 <= row["note_index"] < len(notes):
            notes.pop(row["note_index"])
        data["notes"] = notes
        _save_user(up, data)
        return learn_preference(row["item"], change, reason=reason or f"עריכת הערה: {row['value']}",
                                defaults_path=defaults_path, user_path=up)
    if isinstance(change, str):
        change = change.strip()
        try:
            from .attrs import phrase_to_change
            change = phrase_to_change(change)
        except KeyError:
            from .learn import parse_change
            try:
                change = parse_change(change)
            except LearnError:
                return learn_preference(row["item"], change, reason=reason or "עריכת העדפה",
                                        defaults_path=defaults_path, user_path=up)
    if row["kind"] == "global":
        return learn_preference("כללי", change, reason=reason or f"עריכת העדפה: {row['label']}",
                                defaults_path=defaults_path, user_path=up)
    if row["kind"] == "delivery":
        if "delivery" not in change:
            change = {"delivery": {row["site"]: change}}
        return learn_preference("משלוח", change, reason=reason or "עריכת העדפה",
                                defaults_path=defaults_path, user_path=up)
    return learn_preference(row["item"], change, reason=reason or f"עריכת העדפה: {row['label']}",
                            defaults_path=defaults_path, user_path=up)
