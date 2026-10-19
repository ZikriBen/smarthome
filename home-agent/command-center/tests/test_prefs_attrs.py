"""Attribute preferences (פרווה/…) + preference list/update/delete."""
import tempfile
import unittest
from pathlib import Path

from grocery.attrs import extract_attrs, phrase_to_change
from grocery import learn, prefs, api
from grocery.defaults import load_defaults
from grocery.picker import Picker
from grocery.sites.rami_levy import RamiLevyAdapter
from grocery.sites.shufersal import ShufersalAdapter
from grocery.http import FixtureSearcher

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures" / "grocery-live"
BASE = HERE.parent / "grocery-defaults.yaml"


class AttrTests(unittest.TestCase):
    def test_extract_and_phrase(self):
        self.assertEqual(extract_attrs("השחר ממרח שוקולד חלבי 500 גר"), {"חלבי"})
        self.assertEqual(extract_attrs("השחר ממרח שוקולד פרווה 400ג"), {"פרווה"})
        self.assertEqual(phrase_to_change("פרווה"), {"require": ["פרווה"], "exclude": ["חלבי"]})
        self.assertEqual(learn.parse_change("פרווה"), phrase_to_change("פרווה"))

    def test_parve_chocolate_spread_switches_pick(self):
        td = Path(tempfile.mkdtemp())
        img = td / "defaults.yaml"
        img.write_text(BASE.read_text(encoding="utf-8"), encoding="utf-8")
        ad = RamiLevyAdapter()
        ad.searcher = FixtureSearcher(str(FIX / "rami_levy"), lambda m: None, results_key="data")
        d0 = load_defaults(str(img))
        before = Picker(ad, produce_defaults=d0.produce).pick(d0.resolve("ממרח שוקולד"))
        self.assertIn("חלבי", before.chosen.p.name)

        learn.learn_preference("ממרח שוקולד", "פרווה", "אני מעדיף פרווה", defaults_path=str(img))
        d = load_defaults(str(img), str(td / "defaults.user.yaml"))
        after = Picker(ad, produce_defaults=d.produce).pick(d.resolve("ממרח שוקולד"))
        self.assertIn("פרווה", after.chosen.p.name)
        self.assertNotIn("חלבי", after.chosen.p.name)

        # Shufersal fixtures lack פרווה in names → fallback + flag
        sad = ShufersalAdapter()
        sad.searcher = FixtureSearcher(str(FIX / "shufersal"), lambda m: None, results_key="results")
        sh = Picker(sad, produce_defaults=d.produce).pick(d.resolve("ממרח שוקולד"))
        self.assertTrue(any("פרווה" in f for f in sh.flags))
        self.assertIsNotNone(sh.chosen)

    def test_prefs_list_delete_undo(self):
        td = Path(tempfile.mkdtemp())
        img = td / "defaults.yaml"
        img.write_text(BASE.read_text(encoding="utf-8"), encoding="utf-8")
        learn.learn_preference("ממרח שוקולד", "פרווה", "x", defaults_path=str(img))
        text = prefs.list_text(str(img))
        self.assertIn("חייב להכיל פרווה", text)
        self.assertIn("בלי חלבי", text)
        self.assertIn("נמחקה", prefs.delete(2, str(img)))
        self.assertIn("שוחזרה", prefs.delete(0, str(img), undo=True))
