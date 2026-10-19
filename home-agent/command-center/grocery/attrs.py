"""Product attributes (parve/dairy/…): name keywords + optional site fields.

Rami Levy returns `kashruts: [int]` but the IDs are hechsher-codes, not dairy/parve, so we do NOT
map them. Shufersal search JSON has no kashrut field. Attribute rules therefore use:
  - substrings in product name/brand (פרווה, חלבי, בשרי, אורגני, …)
  - and, when present, Product.attrs populated by the adapters from those same tokens.

`require_any` / `exclude_words` already enforce hard filters; this module only helps Hermes translate
Ben's everyday words ("פרווה", "בלי סוכר") into those fields, and gives the picker a stable tag set.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Set

# Everyday Hebrew/English → require / exclude lists applied by learn_preference.
# "פרווה" also excludes חלבי so a dairy product whose name omits "חלבי" but isn't tagged פרווה loses.
ATTR_PHRASES: Dict[str, Dict[str, List[str]]] = {
    "פרווה": {"require": ["פרווה"], "exclude": ["חלבי"]},
    "pareve": {"require": ["פרווה"], "exclude": ["חלבי"]},
    "parve": {"require": ["פרווה"], "exclude": ["חלבי"]},
    "חלבי": {"require": ["חלבי"], "exclude": ["פרווה"]},
    "dairy": {"require": ["חלבי"], "exclude": ["פרווה"]},
    "בשרי": {"require": ["בשרי"]},
    "meat": {"require": ["בשרי"]},
    "אורגני": {"require": ["אורגני"]},
    "organic": {"require": ["אורגני"]},
    "ללא סוכר": {"require": ["ללא סוכר"]},
    "בלי סוכר": {"require": ["ללא סוכר"]},
    "sugar free": {"require": ["ללא סוכר"]},
    "sugar-free": {"require": ["ללא סוכר"]},
    "ללא גלוטן": {"require": ["ללא גלוטן"]},
    "בלי גלוטן": {"require": ["ללא גלוטן"]},
    "gluten free": {"require": ["ללא גלוטן"]},
}

# Tokens we stamp onto Product.attrs when present in the name (checked by word_filter alongside text).
ATTR_TOKENS = ("פרווה", "חלבי", "בשרי", "אורגני", "ללא סוכר", "ללא גלוטן", "טבעוני", "דיאט")


def extract_attrs(*texts: str) -> Set[str]:
    from .normalize import norm_text
    hay = " ".join(norm_text(t or "") for t in texts)
    return {t for t in ATTR_TOKENS if norm_text(t) in hay}


def phrase_to_change(phrase: str) -> Dict[str, List[str]]:
    """Map a short attribute phrase to a learn change dict. Raises KeyError if unknown."""
    from .normalize import norm_text
    k = norm_text(phrase).strip()
    for key, ch in ATTR_PHRASES.items():
        if norm_text(key) == k:
            return {a: list(b) for a, b in ch.items()}
    raise KeyError(phrase)
