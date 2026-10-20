"""Generic relevance for free-text items (no per-item rules) + the hybrid "needs_choice" decision.

How a typed item like "יין לקידוש קדם" is read:
  * core noun   = the first Hebrew word ("יין"). A product must contain it (prefix-tolerant) to count as a
                  name match; otherwise the code never auto-picks it.
  * other words = soft: purpose words ("לקידוש", "להקצפה" — matched without the ל/ה prefix) and brand-like
                  words ("קדם") raise the rank but are never required.
  * noise       = a lone x / X / × / * (left over from "x2" / "×2" quantities) is dropped; if a search still
                  comes back empty, it is retried without any non-Hebrew tokens.
Category sanity: for a food query, products the site marks as non-food (Rami Levy `is_food: 0`) or whose
category / name is cosmetics, hair, cleaning, disposables are dropped — only when at least one food product
remains, so a non-food query ("שקיות אשפה") is never emptied. Shufersal's `food` flag is not used: it is
False for wine (seen live 2026-10-20), so Shufersal relies on `secondLevelCategory` + the name.

When the code is not confident (nothing matched, or two different products score too close), the picker
returns needs_choice with the top candidates; Hermes chooses using the full list title and Ben's
preferences (hermes/SOUL.md "בחירת מוצר"), and re-runs the cart with `choices`.
"""
from __future__ import annotations

import re
from typing import List, Optional

from . import normalize as N

HEB = re.compile(r"[א-ת]")
NOISE_TOKENS = {"x", "×", "*", "-", "+", "·"}
QTY_TOKEN = re.compile(r"^(?:[x×*]\d+|\d+[x×*])$", re.I)
PREFIXES = "והלבמשכ"

# category / name words that mean "not food" (cosmetics, hair, cleaning, disposables, baby care)
NONFOOD_CATEGORY = ("טואלטיקה", "טיפוח", "פארם", "קוסמטיק", "היגיינ", "ניקיון", "ניקוי", "כביסה",
                    "חד פעמי", "חד-פעמי", "אחסון", "כלי בית", "חיתול")
NONFOOD_NAME = ("שמפו", "מרכך", "קרם לחות", "קרם ידיים", "קרם ידים", "קרם גוף", "קרם פנים", "לשיער", "תלתלים",
                "דאודורנט", "סבון", "שעווה", "משחת נעליים", "משחת שיניים", "מברשת שיניים", "שקיות",
                "צלחת", "צלחות", "כוסות", "גביעי", "סכו\"ם", "מגבונים", "נוזל כלים", "אקונומיקה", "טישו",
                "נייר טואלט", "מגבות נייר", "חיתולים", "תחליב", "ג'ל רחצה", "בושם", "לק ", "אבקת כביסה")
NONFOOD_ITEM = NONFOOD_NAME + ("נייר", "ניקוי", "ניקיון", "כביסה", "כלים", "מטהר", "סקוטש", "ספוג", "פח",
                               "סוללות", "נורה", "רדיד", "אלומיניום", "ניילון", "מפית", "מפיות")

RAMI_DEPARTMENTS = {   # observed live (department_id -> label) for the candidate list only; not used to filter
    "49": "פירות וירקות", "50": "חלב, ביצים וקירור", "57": "משקאות ויין", "58": "חד פעמי ואחסון",
    "59": "ניקיון", "60": "טואלטיקה וטיפוח"}


def clean_text(text: str) -> str:
    """Drop stray noise tokens: 'שמנת מתוקה x' -> 'שמנת מתוקה'. 'x2'/'2x' are quantities (freetext parses
    them first); any left here are dropped too. Legit Latin words (e.g. 'L', 'M', 'zero') stay."""
    toks = (text or "").split()
    keep = [t for t in toks if t.lower() not in NOISE_TOKENS and not QTY_TOKEN.match(t)]
    return " ".join(keep) if keep else (text or "").strip()


def hebrew_only(text: str) -> str:
    """Retry form for an empty search: keep Hebrew-bearing tokens and numbers with units/percent."""
    keep = [t for t in (text or "").split() if HEB.search(t) or re.match(r"^\d+(?:[.,]\d+)?%?$", t)]
    return " ".join(keep)


def tokens(text: str) -> List[str]:
    return [t for t in N.norm_text(clean_text(text)).split() if t]


def core_word(text: str) -> Optional[str]:
    for t in tokens(text):
        if HEB.search(t) and len(t) >= 2:
            return t
    return None


def is_purpose(tok: str) -> bool:
    return tok.startswith("ל") and len(tok) >= 4 and HEB.search(tok) is not None


def tok_hit(tok: str, name_norm: str) -> bool:
    if tok in name_norm:
        return True
    if len(tok) >= 4 and tok[0] in PREFIXES and tok[1:] in name_norm:   # לקידוש ~ קידוש
        return True
    return False


def rel_score(name: str, text: str) -> (float, bool, int):
    """(fraction of typed tokens found, core found, hits). Without the core noun the score is capped < 0.5,
    so it can never be auto-picked as a name match."""
    n = N.norm_text(name)
    toks = tokens(text)
    if not toks:
        return 0.0, False, 0
    core = core_word(text)
    hits = sum(1 for t in toks if tok_hit(t, n))
    frac = hits / len(toks)
    core_ok = core is None or tok_hit(core, n)
    if not core_ok:
        frac = min(frac, 0.45)
    return frac, core_ok, hits


def item_is_nonfood(text: str) -> bool:
    n = N.norm_text(text)
    return any(w in n for w in NONFOOD_ITEM)


def product_category(p) -> str:
    raw = p.raw or {}
    if p.site == "shufersal":
        return str(raw.get("secondLevelCategory") or "")
    dep = raw.get("department_id")
    return RAMI_DEPARTMENTS.get(str(dep), f"מחלקה {dep}" if dep not in (None, "") else "")


def product_is_nonfood(p) -> bool:
    raw = p.raw or {}
    if p.site == "rami_levy" and raw.get("is_food") in (0, "0", False):
        return True
    cat = N.norm_text(product_category(p))
    if any(w in cat for w in NONFOOD_CATEGORY):
        return True
    n = N.norm_text(p.name) + " "
    return any(w in n for w in NONFOOD_NAME)


def relaxed_terms(text: str) -> List[str]:
    """Extra searches (max 2) when nothing matched a multi-word free-text item:
    'יין לקידוש קדם' -> ['יין לקידוש', 'יין קדם']; 'שמנת מתוקה' -> ['שמנת']."""
    toks = clean_text(text).split()
    if len(toks) < 2:
        return []
    out = [" ".join(toks[:-1])]
    if len(toks) >= 3:
        out.append(f"{toks[0]} {toks[-1]}")
    return [t for t in out if t and t != text]
