"""Kashrut (hechsher) preferences: "תמיד מהדרין/בד״ץ".

Data sources
  Rami Levy  search JSON has `kashruts: [id, …]`. The id → name table is the site's own public
             static data (POST https://www-api.rami-levy.co.il/api/v2/site/static/menu → "kashruts",
             fetched 2026-10-19, saved in rami_kashruts.json; same list the site's "מהדרין" filter uses).
             `[]` = no data (unknown), not "not kosher".
  Shufersal  search JSON has no kashrut field (checked 2026-10-19: no kosher/kashrut keys, only
             healthAttributes). We read hechsher words from the product name ("… בד\"ץ"); otherwise unknown.

Levels (what a rule accepts)
  "mehadrin"  any mehadrin-level hechsher, including every בד״ץ:
              - any hechsher whose name contains בד״ץ/בד״צ, "מהדרין" or "העדה החרדית"
                (e.g. בד"ץ העדה החרדית, בד"צ בית יוסף, בד"צ מחפוד, בד"ץ חתם סופר, בד"צ רובין,
                 ועדת מהדרין תנובה, רבנות רחובות-מהדרין, בד"צ אגודת ישראל, בד"ץ איגוד החרדים …)
              - plus the hechsherim Rami Levy marks as leading (is_leading=1) that are mehadrin without
                saying so in the name: שארית ישראל, הרב לנדא, אפרתי, הרב וועסטהיים
              NOT accepted: plain רבנות (ראשית / מקומית / any city), foreign generic (OU, OK, KF alone, Star-K…),
              and "שמיטה לחומרה בד"צ" (a shmita marker, not a hechsher by itself).
  "badatz"    only hechsherim whose name says בד״ץ/בד״צ (or העדה החרדית).
"מהדרין/בד״ץ" (Ben's wording) → "mehadrin" (the superset).

Rule strength: soft-hard. When matching products exist they win (if the top product merely has no data,
only a near-equal match — ≥90% of its score — replaces it); otherwise the best product is kept and
flagged under לבדיקה ("לא נמצא מהדרין" when there was kashrut data, "כשרות לא ידועה" when there was none).
Produce (fruit/veg) is exempt: no hechsher on the product, never dropped or flagged.
Switch: everything above runs only with GROCERY_KASHRUT=1 (see enabled()); off by default.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Set

ENV_FLAG = "GROCERY_KASHRUT"   # OFF by default (Ben 21.10): no kashrut filtering or markers anywhere


def enabled() -> bool:
    """Kashrut rules are applied only with GROCERY_KASHRUT=1. Off: no filter, no 'לא נמצא מהדרין' /
    'כשרות לא ידועה' flags, on every site. Learned kashrut preferences are kept (inert) for when it's back on."""
    return os.environ.get(ENV_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


LEVELS = ("mehadrin", "badatz")
KASHRUT_UNKNOWN = "כשרות לא ידועה"
CLOSE = 0.90
LEVEL_HE = {"mehadrin": "מהדרין / בד״ץ", "badatz": "בד״ץ בלבד"}
_BADATZ = re.compile(r'בד["״\'׳]?[צץ]|העדה החרדית')
_MEHADRIN = re.compile(r'בד["״\'׳]?[צץ]|מהדרין|העדה החרדית')
_NOT_HECHSHER = {53}                     # "שמיטה לחומרה בד"צ"
_EXTRA_MEHADRIN = {76}                   # "חיים יצחק לנדא" (= הרב לנדא, not flagged is_leading)
_FILE = os.path.join(os.path.dirname(__file__), "rami_kashruts.json")

# Words in Ben's phrasing → level
_PHRASE_MEHADRIN = re.compile(r'מהדרין|mehadrin|כשרות\s*(?:מהודרת|גבוהה)|חרדי', re.I)
_PHRASE_BADATZ = re.compile(r'בד["״\'׳]?[צץ]|badat[sz]|העדה', re.I)
_PHRASE_NONE = re.compile(r'(?:לא\s*משנה|בלי|ללא|אין)\s*(?:ה)?כשרות|any\s*kashrut', re.I)


def norm_level(v) -> Optional[str]:
    """'mehadrin' | 'badatz' | None. Accepts Hebrew words too."""
    if v in (None, "", False):
        return None
    s = str(v).strip().lower()
    if s in LEVELS:
        return s
    if s in ("none", "any", "off", "ללא", "לא משנה"):
        return None
    if _PHRASE_MEHADRIN.search(s):
        return "mehadrin"
    if _PHRASE_BADATZ.search(s):
        return "badatz"
    raise ValueError(f"unknown kashrut level {v!r} (use 'mehadrin' or 'badatz')")


def phrase_level(text: str):
    """Kashrut level named in free text, '' for an explicit 'no kashrut rule', or None if not a kashrut phrase.
    'תמיד מהדרין/בדצ' → mehadrin; 'רק בד"ץ' → badatz; 'לא משנה כשרות' → ''."""
    if not text:
        return None
    if _PHRASE_NONE.search(text):
        return ""
    if _PHRASE_MEHADRIN.search(text):
        return "mehadrin"
    if _PHRASE_BADATZ.search(text):
        return "badatz"
    return None


@lru_cache(maxsize=1)
def rami_table() -> Dict[int, dict]:
    try:
        with open(_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {int(x["id"]): x for x in data.get("kashruts") or []}


def name_level(name: str, leading: bool = False, kid: Optional[int] = None) -> Set[str]:
    """Levels a hechsher name satisfies."""
    if kid in _NOT_HECHSHER:
        return set()
    out = set()
    if _BADATZ.search(name or ""):
        out |= {"badatz", "mehadrin"}
    if _MEHADRIN.search(name or "") or leading or kid in _EXTRA_MEHADRIN:
        out.add("mehadrin")
    return out


def rami_hechsherim(ids: Iterable) -> Optional[List[str]]:
    """Rami `kashruts` ids → names. None when there is no data."""
    ids = [int(i) for i in (ids or []) if str(i).strip().lstrip("-").isdigit()]
    if not ids:
        return None
    t = rami_table()
    return [t[i]["name"] if i in t else f"#{i}" for i in ids]


def rami_levels(ids: Iterable) -> Set[str]:
    t = rami_table()
    out: Set[str] = set()
    for i in ids or []:
        try:
            i = int(i)
        except (TypeError, ValueError):
            continue
        x = t.get(i)
        if x:
            out |= name_level(x["name"], bool(x.get("is_leading")), i)
        elif i in _EXTRA_MEHADRIN:
            out.add("mehadrin")
    return out


def name_hechsherim(*texts: str) -> Optional[List[str]]:
    """Hechsher words found in a product name (Shufersal). None when nothing is said."""
    hay = " ".join(t or "" for t in texts)
    found = []
    if _BADATZ.search(hay):
        found.append('בד"ץ (לפי שם המוצר)')
    elif re.search(r"מהדרין", hay):
        found.append("מהדרין (לפי שם המוצר)")
    return found or None


def product_status(p, level: str) -> str:
    """'ok' | 'no' | 'unknown' for one Product under a level."""
    levels = getattr(p, "kashrut_levels", None)
    if getattr(p, "kashrut", None) is None:
        # still trust the name (e.g. Rami item with empty kashruts but "בד"ץ" in the name)
        return "ok" if level in name_level(p.name or "") else "unknown"
    return "ok" if level in (levels or set()) or level in name_level(p.name or "") else "no"


def apply(ranked: list, level: str):
    """Soft-hard filter on ranked Candidates. Returns (ranked', flag or None, filtered: bool)."""
    if not level or not ranked:
        return ranked, None, False
    status = [(c, product_status(c.p, level)) for c in ranked]
    ok = [c for c, s in status if s == "ok"]
    top, top_status = status[0]
    if ok and ok[0].name_score >= min(0.5, top.name_score):
        # Top is known NOT mehadrin → any matching product wins. Top just has no data (common at Shufersal,
        # where we only see the name) → switch only when the match is about as good (≥90% of the score),
        # so "no kashrut info" never turns 3% milk into lactose-free 2%.
        if top_status == "no" or ok[0] is top or ok[0].score >= CLOSE * top.score:
            return ok + [c for c, s in status if s != "ok"], None, True
        return ranked, KASHRUT_UNKNOWN, False
    if top_status == "unknown":
        return ranked, KASHRUT_UNKNOWN, False
    return ranked, f"לא נמצא {LEVEL_HE[level].split(' /')[0]}" if level == "mehadrin" else "לא נמצא בד״ץ", False
