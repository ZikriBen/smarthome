"""Hybrid picker (generic scoring + needs_choice for Hermes) on REAL responses captured live on the NUC
2026-10-20 (fixtures/real/20261020-nuc). No per-item rules: the same code handles every free-text item."""
import copy
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

from grocery import api
from grocery import relevance as R
from grocery.cli import make_adapter
from grocery.defaults import load_defaults
from grocery.freetext import parse_line
from grocery.picker import Picker

REAL = os.path.join(os.path.dirname(__file__), "fixtures", "grocery-real-20261020")
BASELINE = json.load(open(os.path.join(REAL, "picks-83f042f.json"), encoding="utf-8"))
HAIR = "קרם לחות לעיצוב תלתלים נטורל"
KEDEM = "מיץ ענבים קדם קונקורד"


def defaults():
    return load_defaults(os.path.join(REAL, "defaults.yaml"), os.path.join(REAL, "defaults.user.yaml"))


def picker(site, fx=None):
    ad, _ = make_adapter(site, SimpleNamespace(delay=0, size=10, crawl_delay=None, deep_size=None), None,
                         fx or os.path.join(REAL, site), lambda m: None)
    return Picker(ad, produce_defaults=defaults().produce)


def list_items(d):
    out = []
    for e in BASELINE["state"]["items"]:
        it = d.resolve(e["item"] if e.get("known") else e["text"])
        if e.get("qty") is not None:
            it.qty, it.qty_unit = e["qty"], e.get("qty_unit")
        out.append(it)
    return out


def with_kedem(tmp):
    """Rami Levy fixtures + Ben's expected product (seen by Ben on the site at ₪26.60; the search API did not
    return it in 13 live queries on 2026-10-20, so it is injected into the 'יין קדם' results here)."""
    dst = os.path.join(tmp, "rami_levy")
    shutil.copytree(os.path.join(REAL, "rami_levy"), dst)
    idx = json.load(open(os.path.join(dst, "index.json"), encoding="utf-8"))
    juice = json.load(open(os.path.join(dst, idx["מיץ ענבים"]), encoding="utf-8"))["data"][0]
    k = copy.deepcopy(juice)
    k.update({"id": 9900001, "name": KEDEM, "price": {"price": 26.6}, "barcode": "9900001",
              "gs": dict(k.get("gs") or {}, BrandName="קדם")})
    f = os.path.join(dst, idx["יין קדם"])
    data = json.load(open(f, encoding="utf-8"))
    data["data"].insert(3, k)
    json.dump(data, open(f, "w", encoding="utf-8"), ensure_ascii=False)
    return dst


class NoiseTokens(unittest.TestCase):
    def test_clean_text(self):
        self.assertEqual(R.clean_text("שמנת מתוקה x"), "שמנת מתוקה")
        self.assertEqual(R.clean_text("x שמנת מתוקה"), "שמנת מתוקה")
        self.assertEqual(R.clean_text("שמנת מתוקה × X2"), "שמנת מתוקה")
        for legit in ("ביצים L", "ביצים M", "קוקה קולה zero", "חלב 3%", "שוקולד 70%", "XL ביצים"):
            self.assertEqual(R.clean_text(legit), legit)

    def test_parse_line_quantity_then_noise(self):
        for raw in ("שמנת מתוקה x ×2", "שמנת מתוקה x2", "2x שמנת מתוקה", "שמנת מתוקה X2"):
            pl = parse_line(raw)
            self.assertEqual((pl.text, pl.qty), ("שמנת מתוקה", 2.0), raw)

    def test_old_list_entry_resolves_clean(self):
        it = defaults().resolve("שמנת מתוקה x")
        self.assertEqual((it.name, it.terms), ("שמנת מתוקה", ["שמנת מתוקה"]))

    def test_hebrew_only_retry_form(self):
        self.assertEqual(R.hebrew_only("שמנת מתוקה xyz"), "שמנת מתוקה")


class Relevance(unittest.TestCase):
    def test_core_noun_required(self):
        self.assertEqual(R.core_word("יין לקידוש קדם"), "יין")
        sc, core, _ = R.rel_score(HAIR, "יין לקידוש קדם")
        self.assertFalse(core)
        self.assertLess(sc, 0.5)
        sc, core, hits = R.rel_score("יין מתוק לקידוש סגל", "יין לקידוש קדם")   # purpose word w/o ל
        self.assertTrue(core)
        self.assertEqual(hits, 2)

    def test_nonfood(self):
        d = defaults()
        pk = picker("rami_levy")
        raw = pk.adapter.searcher.search("יין לקידוש קדם")[0]
        self.assertTrue(R.product_is_nonfood(pk.adapter.normalize(raw)))
        self.assertFalse(R.item_is_nonfood("יין לקידוש קדם"))
        self.assertTrue(R.item_is_nonfood("שקיות אשפה"))
        self.assertEqual(R.relaxed_terms("יין לקידוש קדם"), ["יין לקידוש", "יין קדם"])


class WineAndCream(unittest.TestCase):
    def test_rami_wine_never_hair_cream_needs_choice(self):
        p = picker("rami_levy").pick(defaults().resolve("יין לקידוש קדם"))
        self.assertTrue(p.chosen is None or HAIR not in p.chosen.p.name)
        self.assertTrue(p.needs_choice)
        names = [c.p.name for c in p.choices]
        self.assertNotIn(HAIR, names)
        self.assertTrue(all(not R.product_is_nonfood(c.p) for c in p.choices))
        self.assertIn('יין גליה מתוק יקבי ברקן 750 מ"ל', names)
        self.assertEqual(p.terms_used, ["יין לקידוש קדם", "יין לקידוש", "יין קדם"])   # max 2 extra searches

    def test_rami_wine_with_kedem_concord_listed(self):
        tmp = tempfile.mkdtemp()
        try:
            p = picker("rami_levy", with_kedem(tmp)).pick(defaults().resolve("יין לקידוש קדם"))
            ok = (p.chosen is not None and p.chosen.p.name == KEDEM) or \
                 (p.needs_choice and KEDEM in [c.p.name for c in p.choices])
            self.assertTrue(ok, (p.chosen and p.chosen.p.name, [c.p.name for c in p.choices]))
            k = next(c for c in p.choices if c.p.name == KEDEM)
            self.assertEqual(k.p.price, 26.6)
            # Hermes picks it -> one-run pin, labelled as a choice
            it = defaults().resolve("יין לקידוש קדם")
            it.pinned["rami_levy"] = k.p.id
            it.chosen_sites.add("rami_levy")
            q = picker("rami_levy", os.path.join(tmp, "rami_levy")).pick(it)
            self.assertEqual((q.chosen.p.name, q.chosen.p.price, q.needs_choice), (KEDEM, 26.6, False))
            self.assertIn("נבחר מתוך המועמדים", q.flags)
        finally:
            shutil.rmtree(tmp)

    def test_shufersal_wine_is_a_kiddush_wine(self):
        p = picker("shufersal").pick(defaults().resolve("יין לקידוש קדם"))
        self.assertEqual((p.chosen.p.name, p.chosen.p.price), ("יין מתוק לקידוש סגל", 17.9))
        self.assertNotIn("גביעי יין עגולים לקידוש", [c.p.name for c in p.alternatives + p.choices])

    def test_sweet_cream_both_sites(self):
        d = defaults()
        it = list_items(d)[0]
        self.assertEqual((it.name, it.qty), ("שמנת מתוקה", 2.0))
        r = picker("rami_levy").pick(it)
        self.assertEqual((r.chosen.p.name, r.chosen.p.price, r.qty), ('שמנת מתוקה להקצפה 32% 250 מ"ל', 8.1, 2.0))
        s = picker("shufersal").pick(list_items(d)[0])
        self.assertEqual((s.chosen.p.name, s.chosen.p.price, s.qty), ("שמנת מתוקה להקצפה 32%", 9.1, 2.0))
        self.assertFalse(r.needs_choice or s.needs_choice)

    def test_noise_retry_when_search_empty(self):
        from grocery.model import Item
        it = Item(name="שמנת מתוקה", terms=["שמנת מתוקה x"], source="unknown")   # raw term kept by a caller
        p = picker("rami_levy").pick(it)
        self.assertIn("שמנת מתוקה", p.terms_used)
        self.assertIn("שמנת מתוקה", p.chosen.p.name)


class RealListNoRegression(unittest.TestCase):
    def test_other_items_same_as_deployed(self):
        d = defaults()
        for site in ("rami_levy", "shufersal"):
            pk = picker(site)
            for it, old in zip(list_items(d), BASELINE["picks"][site]):
                if old["item"] in ("שמנת מתוקה x", "יין לקידוש קדם"):
                    continue
                p = pk.pick(it)
                self.assertEqual((p.chosen.p.name, p.qty), (old["name"], old["qty"]), (site, old["item"]))
                self.assertFalse(p.needs_choice, (site, old["item"]))


class CartApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["GROCERY_STATE_DIR"] = self.tmp
        st = {"items": [dict(e) for e in BASELINE["state"]["items"]]}
        json.dump(st, open(os.path.join(self.tmp, "current_list.json"), "w", encoding="utf-8"), ensure_ascii=False)
        self.dp = os.path.join(self.tmp, "defaults.yaml")
        shutil.copy(os.path.join(REAL, "defaults.yaml"), self.dp)
        shutil.copy(os.path.join(REAL, "defaults.user.yaml"), os.path.join(self.tmp, "defaults.user.yaml"))
        self.fx = os.path.join(self.tmp, "fx")
        os.makedirs(self.fx)
        with_kedem(self.fx)
        shutil.copytree(os.path.join(REAL, "shufersal"), os.path.join(self.fx, "shufersal"))

    def tearDown(self):
        os.environ.pop("GROCERY_STATE_DIR", None)
        shutil.rmtree(self.tmp)

    def test_needs_choice_then_choice_rerun(self):
        out = os.path.join(self.tmp, "out")
        r = api.cart("both", fixtures=self.fx, out_dir=out, defaults_path=self.dp, parallel=False)
        rows = [x for x in r["needs_choice"] if x["site"] == "rami_levy"]
        self.assertEqual([x["item"] for x in rows], ["יין לקידוש קדם"])
        cand = {c["name"]: c for c in rows[0]["candidates"]}
        self.assertIn(KEDEM, cand)
        self.assertEqual(set(cand[KEDEM]), {"id", "name", "price", "brand", "category", "kashrut"})
        self.assertNotIn(HAIR, cand)
        self.assertNotIn(HAIR, r["message"])
        self.assertIn("❓ לבחירה: יין לקידוש קדם", r["message"])
        r2 = api.cart("both", fixtures=self.fx, out_dir=out, defaults_path=self.dp, parallel=False,
                      choices={"יין לקידוש קדם": {"rami_levy": cand[KEDEM]["id"]}, "אננס": {"rami_levy": "1"}})
        self.assertEqual(r2["needs_choice"], [])
        self.assertEqual(r2["unmatched_choices"], ["אננס"])
        self.assertNotIn("❓", r2["message"])
        names = [ln.get("product_name") for ln in r2["lines"]["rami_levy"]]
        self.assertIn(KEDEM, names)
        # one-run only: nothing written to the preferences
        self.assertNotIn("קדם", open(os.path.join(self.tmp, "defaults.user.yaml"), encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main()
