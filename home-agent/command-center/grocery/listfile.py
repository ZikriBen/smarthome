"""Item-field parsing shared by list.yaml and defaults.yaml, and the list loader.
See README "List format" / "Defaults format"."""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

from . import minyaml
from .model import CAT_KEYS, UNITS, Item

# Fields allowed on a list item AND on a defaults alias entry.
INTENT_KEYS = {
    "qty", "qty_unit", "terms", "fallback_terms", "extra_terms", "keywords", "exclude_words", "require_any", "prefer_words",
    "brands", "product_id", "pinned", "filters", "unit", "size_min", "size_max", "produce",
    "unit_weight_kg", "note", "pack_preference",
} | set(CAT_KEYS)
ITEM_KEYS = INTENT_KEYS | {"name"}
ALIAS_KEYS = INTENT_KEYS | {"aliases", "_learned"}
TOP_KEYS = {"store", "items", "version", "site"}
LIST_FIELDS = ("terms", "fallback_terms", "extra_terms", "keywords", "exclude_words", "require_any", "prefer_words", "brands")


class ListError(ValueError):
    pass


def str_list(v, where) -> List[str]:
    if v is None:
        return []
    if isinstance(v, (str, int, float)):
        v = [v]
    if not isinstance(v, list):
        raise ListError(f"{where}: expected a list")
    return [str(x).strip() for x in v if x is not None and str(x).strip()]


def _num(v, where, positive=True) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ListError(f"{where}: expected a number, got {v!r}")
    if positive and f <= 0:
        raise ListError(f"{where}: must be > 0")
    return f


def parse_intent(raw: dict, where: str, allowed=INTENT_KEYS) -> Dict:
    """Validate a mapping of intent fields -> normalized dict (only keys that were present)."""
    if not isinstance(raw, dict):
        raise ListError(f"{where}: expected a mapping")
    unknown = set(raw) - allowed
    if unknown:
        raise ListError(f"{where}: unknown key(s) {sorted(unknown)}")
    out: Dict = {}
    for k in LIST_FIELDS:
        if k in raw:
            out[k] = str_list(raw[k], f"{where}.{k}")
    if "qty" in raw:
        out["qty"] = _num(raw["qty"], f"{where}.qty")
    if "qty_unit" in raw and raw["qty_unit"] is not None:
        qu = str(raw["qty_unit"]).lower()
        if qu not in ("unit", "kg"):
            raise ListError(f"{where}.qty_unit: must be 'unit' or 'kg'")
        out["qty_unit"] = qu
    if "unit" in raw and raw["unit"] is not None:
        u = str(raw["unit"]).lower()
        if u not in UNITS:
            raise ListError(f"{where}.unit: must be one of {UNITS}")
        out["unit"] = u
    for k in ("size_min", "size_max", "unit_weight_kg"):
        if k in raw:
            out[k] = _num(raw[k], f"{where}.{k}")
    if "pack_preference" in raw and raw["pack_preference"] is not None:
        pp = raw["pack_preference"]
        if isinstance(pp, (int, float, str)):
            pp = [pp]
        if not isinstance(pp, list) or not pp:
            raise ListError(f"{where}.pack_preference: expected a non-empty list of numbers, e.g. [30, 18, 12]")
        out["pack_preference"] = [_num(x, f"{where}.pack_preference") for x in pp]
    if "produce" in raw:
        out["produce"] = bool(raw["produce"])
    if "note" in raw:
        out["note"] = raw["note"]
    if raw.get("product_id") not in (None, ""):
        out["product_id"] = str(raw["product_id"]).strip()
    if "pinned" in raw and raw["pinned"]:
        if not isinstance(raw["pinned"], dict):
            raise ListError(f"{where}.pinned: expected {{site: product_id}}")
        out["pinned"] = {str(k): str(v) for k, v in raw["pinned"].items() if v not in (None, "")}
    filters: Dict[str, Dict[str, List[str]]] = {}
    if "filters" in raw and raw["filters"]:
        if not isinstance(raw["filters"], dict):
            raise ListError(f"{where}.filters: expected {{site: {{departments: [...]}}}}")
        for site, f in raw["filters"].items():
            if not isinstance(f, dict):
                raise ListError(f"{where}.filters.{site}: expected a mapping")
            bad = set(f) - set(CAT_KEYS)
            if bad:
                raise ListError(f"{where}.filters.{site}: unknown key(s) {sorted(bad)}")
            filters[str(site)] = {k: str_list(f.get(k), f"{where}.filters.{site}.{k}") for k in f}
    legacy = {k: str_list(raw[k], f"{where}.{k}") for k in CAT_KEYS if k in raw}
    if legacy:  # flat keys = Rami Levy ids (back-compat with the first list.yaml format)
        filters.setdefault("rami_levy", {}).update(legacy)
    if filters:
        out["filters"] = filters
    return out


def apply_intent(item: Item, fields: Dict) -> Item:
    for k, v in fields.items():
        if k == "filters":
            for site, f in v.items():
                item.filters.setdefault(site, {}).update(f)
        elif k == "pinned":
            item.pinned.update(v)
        else:
            setattr(item, k, v)
    return item


def load_structured(path: str):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        return json.loads(text)
    if ext in (".yaml", ".yml"):
        return load_yaml_text(text)
    raise ListError(f"unsupported extension {ext!r} (use .yaml/.yml/.json)")


def load_yaml_text(text: str):
    try:
        import yaml  # type: ignore
    except ImportError:
        return minyaml.loads(text)
    return yaml.safe_load(text)


class ListFile:
    def __init__(self, items: List[Item], store: Optional[str] = None, site: Optional[str] = None):
        self.items, self.store, self.site = items, store, site


def parse_list(data, defaults=None) -> ListFile:
    """data: {store?, site?, items: [str | {name, ...}]}. Strings and names go through defaults."""
    if isinstance(data, list):
        data = {"items": data}
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ListError("list file must contain an 'items:' list")
    unknown = set(data) - TOP_KEYS
    if unknown:
        raise ListError(f"unknown top-level key(s) {sorted(unknown)}")
    items = []
    for i, raw in enumerate(data["items"]):
        where = f"items[{i}]"
        if isinstance(raw, (str, int)):
            raw = {"name": str(raw)}
        if not isinstance(raw, dict):
            raise ListError(f"{where}: expected a mapping or a string")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ListError(f"{where}: 'name' is required")
        fields = parse_intent({k: v for k, v in raw.items() if k != "name"}, f"{where} ({name})")
        item = defaults.resolve(name) if defaults is not None else Item(name=name, terms=[name])
        if defaults is None:
            item.source = "list"
        apply_intent(item, fields)
        if not item.terms:
            item.terms = [name]
        items.append(item)
    if not items:
        raise ListError("'items' is empty")
    store = data.get("store")
    return ListFile(items, str(store) if store not in (None, "") else None, data.get("site"))


def load_list(path: str, defaults=None) -> ListFile:
    return parse_list(load_structured(path), defaults)
