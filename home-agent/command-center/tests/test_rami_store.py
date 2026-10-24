"""Rami Levy store + search endpoint (live NUC capture 2026-10-20): fixtures from stores 9001 and 331;
the site's own search is POST /api/catalog. Offline on real responses."""
import json
import os
import unittest
from types import SimpleNamespace

from grocery.cli import make_adapter
from grocery.defaults import load_defaults
from grocery.picker import Picker
from grocery.sites import rami_levy as RL

FX = os.path.join(os.path.dirname(__file__), "fixtures")
OLD = os.path.join(FX, "grocery-real-20261020")          # defaults + list state
STATE = json.load(open(os.path.join(OLD, "picks-83f042f.json"), encoding="utf-8"))["state"]
HAIR = "קרם לחות לעיצוב תלתלים נטורל"
KEDEM_ID, KEDEM = "126860", "מיץ ענבים גאולה קדם רויאל יבוא 1.89 ליטר"


def d():
    return load_defaults(os.path.join(OLD, "defaults.yaml"), os.path.join(OLD, "defaults.user.yaml"))


def picker(store):
    ad, _ = make_adapter("rami_levy", SimpleNamespace(delay=0, size=10, crawl_delay=None, deep_size=None), None,
                         os.path.join(FX, f"grocery-real-20261020-catalog-{store}", "rami_levy"), lambda m: None)
    return Picker(ad, produce_defaults=d().produce)


def items():
    out = []
    dd = d()
    for e in STATE["items"]:
        it = dd.resolve(e["item"] if e.get("known") else e["text"])
        if e.get("qty") is not None:
            it.qty, it.qty_unit = e["qty"], e.get("qty_unit")
        out.append(it)
    return out


class StoreConfig(unittest.TestCase):
    def test_default_store_is_guest_store_and_env_overrides(self):
        old = os.environ.pop("GROCERY_STORE", None)
        try:
            self.assertEqual(RL.RamiLevyAdapter().store, "331")
            os.environ["GROCERY_STORE"] = "9001"
            self.assertEqual(RL.RamiLevyAdapter().store, "9001")
            self.assertEqual(RL.RamiLevyAdapter(store="412").store, "412")
        finally:
            os.environ.pop("GROCERY_STORE", None)
            if old is not None:
                os.environ["GROCERY_STORE"] = old

    def test_fixtures_replay_their_capture_store(self):
        self.assertEqual(picker("9001").adapter.store, "9001")
        self.assertEqual(picker("331").adapter.store, "331")

    def test_search_mode_default_catalog(self):
        old = os.environ.pop("GROCERY_RAMI_SEARCH", None)
        try:
            self.assertEqual(RL.search_mode(), "catalog")
            os.environ["GROCERY_RAMI_SEARCH"] = "search"
            self.assertEqual(RL.search_mode(), "search")
        finally:
            os.environ.pop("GROCERY_RAMI_SEARCH", None)
            if old is not None:
                os.environ["GROCERY_RAMI_SEARCH"] = old


class Store9001Catalog(unittest.TestCase):
    def test_kiddush_wine_needs_choice_with_kedem_juice(self):
        p = picker("9001").pick(d().resolve("יין לקידוש קדם"))
        self.assertTrue(p.needs_choice)
        ch = {c.p.id: c for c in p.choices}
        self.assertIn(KEDEM_ID, ch)
        self.assertEqual((ch[KEDEM_ID].p.name, ch[KEDEM_ID].p.price), (KEDEM, 26.6))
        names = [c.p.name for c in p.choices]
        for n in ("יין קונקורד קינג דיוויד יקבי כרמל", "יין ישן נושן יקבי כרמל", "יין מוסקט מתוק קינג דיוויד יקבי כרמל"):
            self.assertIn(n, names)                 # what Ben sees on the site
        self.assertNotIn(HAIR, names)
        self.assertEqual(p.terms_used, ["יין לקידוש קדם", "יין לקידוש", "יין קדם"])

    def test_kedem_not_stocked_in_331(self):
        p = picker("331").pick(d().resolve("יין לקידוש קדם"))
        self.assertNotIn(KEDEM_ID, [c.p.id for c in p.choices])

    def test_sweet_cream(self):
        p = picker("9001").pick(items()[0])
        self.assertIn("שמנת מתוקה", p.chosen.p.name)
        self.assertEqual(p.qty, 2.0)

    def test_list_matches_baseline_both_stores(self):
        for store in ("9001", "331"):
            base = json.load(open(os.path.join(FX, f"grocery-real-20261020-catalog-{store}", "picks.json"),
                                  encoding="utf-8"))["picks"]
            pk = picker(store)
            for it, b in zip(items(), base):
                p = pk.pick(it)
                self.assertEqual((p.chosen.p.id if p.chosen else None, p.qty, p.needs_choice),
                                 (b["id"], b["qty"], b["needs_choice"]), (store, b["item"]))
                if p.chosen:
                    self.assertNotEqual(p.chosen.p.name, HAIR)


if __name__ == "__main__":
    unittest.main()
