"""Defaults layer (shared by all sites): short names -> precise intent.

defaults.yaml          curated base file (comments kept, edited by humans).
defaults.user.yaml     overrides written by upsert_default() (later: MCP tool remember_grocery_default).
User entries override base entries field by field.
"""
from __future__ import annotations

import copy
import os
import re
from typing import Dict, List, Optional

from . import minyaml
from .listfile import ALIAS_KEYS, ListError, apply_intent, load_structured, parse_intent
from . import delivery as D
from .model import Item
from .normalize import norm_text

PRODUCE_DEFAULTS = {"units": 5, "kg": 0.5, "unit_weight_kg": 0.15}   # kg = non-produce weighed goods only


def _key(s: str) -> str:
    s = norm_text(s)
    s = re.sub(r"[\"'.!?]", "", s)
    return s.strip()


def _variants(k: str) -> List[str]:
    """Light Hebrew normalization for lookup: drop leading ה, simple plural/singular endings."""
    out = [k]
    if k.startswith("ה") and len(k) > 3:
        out.append(k[1:])
    for base in list(out):
        for suf, rep in (("ים", ""), ("ות", "ה"), ("ות", ""), ("ה", "ות"), ("", "ים")):
            if suf and base.endswith(suf) and len(base) - len(suf) >= 2:
                out.append(base[: -len(suf)] + rep)
            elif not suf:
                out.append(base + rep)
    return list(dict.fromkeys(out))


class Defaults:
    def __init__(self, aliases: Dict[str, dict], produce: dict = None, user_path: Optional[str] = None,
                 delivery: dict = None, global_prefs: dict = None, notes: list = None):
        self.global_prefs = dict(global_prefs or {})   # {"kashrut": "mehadrin", …} applies to every item
        self.notes = list(notes or [])                 # free-text prefs saved as notes (not enforced)
        self.aliases = aliases            # canonical name -> raw entry dict (validated)
        self.produce = dict(PRODUCE_DEFAULTS, **(produce or {}))
        self.delivery = D.load(delivery)  # {site: SiteDelivery}
        self.user_path = user_path
        self._index: Dict[str, str] = {}
        for name, entry in aliases.items():
            self._index[_key(name)] = name
            for a in entry.get("aliases") or []:
                self._index.setdefault(_key(a), name)

    # ---- lookup ----
    def lookup(self, text: str) -> Optional[str]:
        k = _key(text)
        if k in self._index:
            return self._index[k]
        for v in _variants(k):
            if v in self._index:
                return self._index[v]
        return None

    def _apply_global(self, item: Item) -> Item:
        if item.kashrut is None and self.global_prefs.get("kashrut"):
            item.kashrut = self.global_prefs["kashrut"]
        return item

    def split_known(self, text: str) -> List[str]:
        """'ממרח שוקולד וממרח חלווה' → ['ממרח שוקולד', 'ממרח חלווה'] when every part is a known item.
        Unknown combos ('שמן וחומץ' if not both known) are left alone."""
        s = (text or "").strip()
        if self.lookup(s) or " ו" not in s:
            return [s]
        parts = re.split(r"\s+ו(?=\S)", s)
        if len(parts) > 1 and all(self.lookup(p) for p in parts):
            return [p.strip() for p in parts]
        return [s]

    def resolve(self, text: str) -> Item:
        name = self.lookup(text)
        if name is None:
            item = Item(name=text.strip(), terms=[text.strip()], source="unknown", raw_text=text)
            return self._apply_global(item)
        entry = {k: v for k, v in self.aliases[name].items() if k not in ("aliases", "_learned")}
        item = Item(name=name, source="defaults", raw_text=text)
        apply_intent(item, parse_intent(copy.deepcopy(entry), f"defaults[{name}]"))
        if not item.terms:
            item.terms = [name]
        if item.produce and item.unit_weight_kg is None:
            item.unit_weight_kg = self.produce["unit_weight_kg"]
        return self._apply_global(item)

    def resolve_parsed(self, parsed) -> Item:
        """parsed: freetext.ParsedLine"""
        item = self.resolve(parsed.text)
        item.raw_text = parsed.raw
        if parsed.qty is not None:
            item.qty, item.qty_unit = parsed.qty, parsed.qty_unit
        if item.source == "unknown":
            item.source = "text"
        return item


def _validate(data, where) -> (Dict[str, dict], dict):
    if data is None:
        return {}, {}
    if not isinstance(data, dict):
        raise ListError(f"{where}: expected a mapping")
    aliases = data.get("aliases") or {}
    if not isinstance(aliases, dict):
        raise ListError(f"{where}.aliases: expected a mapping name -> entry")
    out = {}
    for name, entry in aliases.items():
        entry = entry or {}
        parse_intent(entry, f"{where}.aliases[{name}]", allowed=ALIAS_KEYS)  # validation only
        out[str(name).strip()] = entry
    return out, data.get("produce_defaults") or {}, data.get("delivery") or {}


def _global(data) -> (dict, list):
    if not isinstance(data, dict):
        return {}, []
    g = dict(data.get("global") or {})
    if "kashrut" in g:
        from .kashrut import norm_level
        try:
            g["kashrut"] = norm_level(g["kashrut"]) or ""
        except ValueError as e:
            raise ListError(f"global.kashrut: {e}")
    return g, list(data.get("notes") or [])


def load_defaults(path: str, user_path: Optional[str] = None) -> Defaults:
    raw_base = load_structured(path)
    base, produce, delivery = _validate(raw_base, os.path.basename(path))
    gprefs, notes = _global(raw_base)
    if user_path is None:
        root, ext = os.path.splitext(path)
        user_path = root + ".user" + ext
    if os.path.exists(user_path):
        loaded = load_structured(user_path) or {}
        user, uprod, udel = _validate(loaded, os.path.basename(user_path))
        ug, un = _global(loaded)
        gprefs.update(ug)
        notes += un
        produce = dict(produce, **uprod)
        # deep-merge delivery per site so a user fee override keeps the base min_order
        for site, cfg in (udel or {}).items():
            if isinstance(cfg, dict):
                delivery[site] = dict(delivery.get(site) or {}, **cfg)
        for name, entry in user.items():
            merged = dict(base.get(name, {}))
            merged.update(entry)
            base[name] = merged
    return Defaults(base, produce, user_path, delivery, gprefs, notes)


def upsert_default(alias: str, user_path: str, **fields) -> dict:
    """Create/update one alias in the user overrides file. Returns the stored entry.
    Example: upsert_default("מרק עוף", "defaults.user.yaml", brands=["אסם"], qty=2)."""
    alias = alias.strip()
    if not alias:
        raise ListError("alias is required")
    fields = {k: v for k, v in fields.items() if v is not None}
    parse_intent(fields, f"upsert[{alias}]", allowed=ALIAS_KEYS)
    data = {"version": 1, "aliases": {}}
    if os.path.exists(user_path):
        loaded = load_structured(user_path) or {}
        if isinstance(loaded, dict):
            data.update(loaded)
            data["aliases"] = dict(loaded.get("aliases") or {})
    entry = dict(data["aliases"].get(alias) or {})
    entry.update(fields)
    data["aliases"][alias] = entry
    tmp = user_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if user_path.endswith(".json"):
            import json
            json.dump(data, f, ensure_ascii=False, indent=2)
        else:
            f.write("# Written by upsert_default() — user overrides on top of defaults.yaml\n")
            f.write(minyaml.dumps(data) + "\n")
    os.replace(tmp, user_path)
    return entry
