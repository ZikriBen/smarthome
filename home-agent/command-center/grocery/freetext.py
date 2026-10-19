"""Free-text list parser: "חלב, 2 מלפפון, חצי קילו עגבניות\\nשתי חבילות פיתות" -> lines with qty."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

NUM_WORDS = {
    "אחד": 1, "אחת": 1, "שניים": 2, "שתיים": 2, "שני": 2, "שתי": 2, "זוג": 2,
    "שלוש": 3, "שלושה": 3, "ארבע": 4, "ארבעה": 4, "חמש": 5, "חמישה": 5,
    "שש": 6, "שישה": 6, "שבע": 7, "שבעה": 7, "שמונה": 8, "תשע": 9, "תשעה": 9,
    "עשר": 10, "עשרה": 10, "תריסר": 12,
}
COUNT_WORDS = r"(?:חבילות|חבילה|בקבוקים|בקבוק|יחידות|יחידה|יח'|יח|פחיות|פחית|מארזים|מארז|שקיות|שקית|קופסאות|קופסה|קרטונים|קרטון|ראשים|ראש)"
KG_WORDS = r'(?:קילו|ק"ג|קג|קילוגרם|kg)'
GRAM_WORDS = r"(?:גרם|גר|ג'|g)"
NUM = r"(\d+(?:[.,]\d+)?)"


@dataclass
class ParsedLine:
    raw: str
    text: str                 # the product words, quantity removed
    qty: Optional[float]      # None = not specified
    qty_unit: Optional[str]   # None | "unit" | "kg"


def split_items(text: str) -> List[str]:
    parts = re.split(r"[,\n;،]+|\s\+\s", text or "")
    return [p.strip(" -•*\t") for p in parts if p and p.strip(" -•*\t")]


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def _strip(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" -:,")


def parse_line(raw: str) -> ParsedLine:
    s = " " + re.sub(r"\s+", " ", raw.replace("״", '"').replace("׳", "'")).strip() + " "
    # --- weights ---
    pats = [
        (r"\sקילו\s?וחצי\s", lambda m: 1.5),
        (r"\s" + NUM + r"\s?" + KG_WORDS + r"\s?וחצי\s", lambda m: _num(m.group(1)) + 0.5),
        (r"\sחצי\s?" + KG_WORDS + r"\s", lambda m: 0.5),
        (r"\sרבע\s?" + KG_WORDS + r"\s", lambda m: 0.25),
        (r"\s" + NUM + r"\s?" + KG_WORDS + r"\s", lambda m: _num(m.group(1))),
        (r"\s" + NUM + r"\s?" + GRAM_WORDS + r"\s", lambda m: _num(m.group(1)) / 1000.0),
        (r"\s(" + "|".join(NUM_WORDS) + r")\s" + KG_WORDS + r"\s", lambda m: float(NUM_WORDS[m.group(1)])),
        (r"\s" + KG_WORDS + r"\s", lambda m: 1.0),
    ]
    for p, fn in pats:
        m = re.search(p, s)
        if m:
            return ParsedLine(raw, _strip(s[: m.start()] + " " + s[m.end():]), round(fn(m), 3), "kg")
    # --- counts ---
    cpats = [
        (r"\s" + NUM + r"\s?" + COUNT_WORDS + r"?\s", lambda m: _num(m.group(1))),           # "2 מלפפון", "2 חבילות"
        (r"\s[x×*]\s?" + NUM + r"\s", lambda m: _num(m.group(1))),                            # "x2"
        (r"\s" + NUM + r"\s?[x×*]\s", lambda m: _num(m.group(1))),                            # "2x"
        (r"\s(" + "|".join(NUM_WORDS) + r")\s" + COUNT_WORDS + r"?\s?", lambda m: float(NUM_WORDS[m.group(1)])),
        (r"\s(?:חצי)\s" + COUNT_WORDS + r"\s", lambda m: 1.0),
    ]
    for p, fn in cpats:
        m = re.search(p, s)
        if m:
            rest = _strip(s[: m.start()] + " " + s[m.end():])
            if rest:
                return ParsedLine(raw, rest, fn(m), "unit")
    # bare count word without a number: "חבילה פיתות" -> 1
    m = re.search(r"\s" + COUNT_WORDS + r"\s", s)
    if m and _strip(s[: m.start()] + " " + s[m.end():]):
        return ParsedLine(raw, _strip(s[: m.start()] + " " + s[m.end():]), 1.0, "unit")
    return ParsedLine(raw, _strip(s), None, None)


def parse_text(text: str) -> List[ParsedLine]:
    return [parse_line(p) for p in split_items(text)]
