"""Kashrut (מהדרין/בד״ץ) + global prefs + note fallback + 'בנפרד' — strings taken from the 19-Oct Hermes log."""
import os
import shutil
import tempfile
import unittest

from grocery import kashrut as K
from grocery import prefs
from grocery.defaults import load_defaults
from grocery.freetext import parse_text
from grocery.http import FixtureSearcher
from grocery.learn import LearnError, learn_preference
from grocery.picker import Picker
from grocery.sites.rami_levy import RamiLevyAdapter
from grocery.sites.shufersal import ShufersalAdapter

HERE = os.path.dirname(__file__)
BASE = os.path.join(HERE, "..", "grocery-defaults.yaml")
LIVE = os.path.join(HERE, "fixtures", "grocery-live")


def _tmp_defaults():
    td = tempfile.mkdtemp()
    p = os.path.join(td, "defaults.yaml")
    shutil.copy(BASE, p)
    return p


def _picker(site, d):
    if site == "rami_levy":
        ad = RamiLevyAdapter()
        ad.searcher = FixtureSearcher(os.path.join(LIVE, "rami_levy"), lambda m: None, results_key="data")
    else:
        ad = ShufersalAdapter()
        ad.searcher = FixtureSearcher(os.path.join(LIVE, "shufersal"), lambda m: None, results_key="results")
    return Picker(ad, produce_defaults=d.produce)


class KashrutTable(unittest.TestCase):
    def test_rami_ids(self):
        self.assertEqual(K.rami_levels([1]), {"badatz", "mehadrin"})          # בד"ץ העדה החרדית
        self.assertEqual(K.rami_levels([6]), {"badatz", "mehadrin"})          # בד"צ בית יוסף
        self.assertEqual(K.rami_levels([5]), {"mehadrin"})                    # שארית ישראל
        self.assertEqual(K.rami_levels([49]), {"mehadrin"})                   # ועדת מהדרין תנובה
        self.assertEqual(K.rami_levels([16, 21]), set())                      # רבנות ראשית / מקומית
        self.assertEqual(K.rami_levels([53]), set())                          # שמיטה לחומרה בד"צ
        self.assertIsNone(K.rami_hechsherim([]))
        self.assertEqual(K.rami_hechsherim([1]), ['בד"ץ העדה החרדית'])

    def test_phrases(self):
        self.assertEqual(K.phrase_level("תמיד מהדרין/בדצ"), "mehadrin")
        self.assertEqual(K.phrase_level('רק בד"ץ'), "badatz")
        self.assertEqual(K.phrase_level("לא משנה כשרות"), "")
        self.assertIsNone(K.phrase_level("פרווה"))


class LearnFromLog(unittest.TestCase):
    def setUp(self):
        self.D = _tmp_defaults()

    def test_badatz_mehadrin_free_text_is_global(self):
        out = learn_preference("כללי", "תמיד מהדרין/בדצ", "בכללי תוסיף לההעדפות, תמיד מהדרין/בדצ",
                               defaults_path=self.D)
        self.assertTrue(out.startswith("נלמד (כללי"), out)
        self.assertEqual(load_defaults(self.D).global_prefs["kashrut"], "mehadrin")

    def test_require_any_from_log_is_accepted(self):
        out = learn_preference("כללי", {"require_any": ["מהדרין", 'בד"צ', "בדצ"]}, defaults_path=self.D)
        self.assertIn("מהדרין", out)
        self.assertEqual(load_defaults(self.D).global_prefs["kashrut"], "mehadrin")
        out = learn_preference("ממרח שוקולד", {"require_any": ["פרווה"], "exclude_words": ["חלבי"],
                                               "brands": ["השחר"]}, defaults_path=self.D)
        self.assertTrue(out.startswith("נלמד"))
        it = load_defaults(self.D).resolve("ממרח שוקולד")
        self.assertIn("פרווה", it.require_any)
        self.assertIn("השחר", it.brands)

    def test_bare_mehadrin_and_long_sentence(self):
        self.assertIn("מהדרין", learn_preference("כללי", "מהדרין", defaults_path=self.D))
        out = learn_preference("כללי", "בכללי תוסיף לההעדפות, תמיד מהדרין/בדצ", defaults_path=self.D)
        self.assertTrue(out.startswith("נלמד (כללי"), out)

    def test_unknown_key_lists_valid_keys_and_example(self):
        with self.assertRaises(LearnError) as e:
            learn_preference("ממרח שוקולד", {"colour": "red"}, defaults_path=self.D)
        msg = str(e.exception)
        for k in ("require", "exclude", "brand", "kashrut", "note", "Example"):
            self.assertIn(k, msg)

    def test_bnifrad_is_not_a_400(self):
        out = learn_preference("ממרח חלווה", "בנפרד", "ממרח חלווה בנפרד", defaults_path=self.D)
        self.assertIn("פיצול", out)
        self.assertFalse(os.path.exists(os.path.splitext(self.D)[0] + ".user.yaml"))

    def test_unparsed_text_becomes_note(self):
        out = learn_preference("ביצים", "שיהיו מתרנגולות חופש אם אפשר", defaults_path=self.D)
        self.assertTrue(out.startswith("נשמר כהערה (לא מיושם אוטומטית)"), out)
        text = prefs.list_text(self.D)
        self.assertIn("לא מיושם אוטומטית", text)
        self.assertIn("נמחקה", prefs.delete(1, self.D))
        self.assertNotIn("מתרנגולות", prefs.list_text(self.D))

    def test_prefs_list_shows_global_and_edit(self):
        learn_preference("כללי", "תמיד מהדרין/בדצ", defaults_path=self.D)
        self.assertIn("1. כללי (כל המוצרים): כשרות מהדרין / בד״ץ", prefs.list_text(self.D))
        prefs.update(1, 'רק בד"ץ', self.D)
        self.assertEqual(load_defaults(self.D).global_prefs["kashrut"], "badatz")
        prefs.delete(1, self.D)
        self.assertNotIn("kashrut", load_defaults(self.D).global_prefs)
        prefs.delete(0, self.D, undo=True)
        self.assertEqual(load_defaults(self.D).global_prefs["kashrut"], "badatz")


class Split(unittest.TestCase):
    def test_known_conjunction_splits(self):
        d = load_defaults(BASE, "/nonexistent.yaml")
        got = [pl.text for pl in parse_text("ממרח שוקולד וממרח חלווה, 2 מלפפון", split=d.split_known)]
        self.assertEqual(got, ["ממרח שוקולד", "ממרח חלווה", "מלפפון"])
        self.assertEqual([pl.text for pl in parse_text("שמן וחומץ בלסמי", split=d.split_known)], ["שמן וחומץ בלסמי"])


class PickWithKashrut(unittest.TestCase):
    def setUp(self):
        self.D = _tmp_defaults()
        learn_preference("כללי", "תמיד מהדרין/בדצ", defaults_path=self.D)
        self.d = load_defaults(self.D)

    def test_rami_prefers_mehadrin_and_never_drops(self):
        pick = _picker("rami_levy", self.d).pick(self.d.resolve("חלב"))
        self.assertIsNotNone(pick.chosen)
        self.assertEqual(K.product_status(pick.chosen.p, "mehadrin"), "ok")

    def test_produce_exempt(self):
        it = self.d.resolve("מלפפון")
        self.assertTrue(it.produce)
        pick = _picker("rami_levy", self.d).pick(it)
        self.assertIsNotNone(pick.chosen)
        self.assertFalse(any("כשרות" in f or "מהדרין" in f for f in pick.flags))

    def test_shufersal_unknown_is_flagged_not_dropped(self):
        pick = _picker("shufersal", self.d).pick(self.d.resolve("חלב"))
        self.assertIsNotNone(pick.chosen)
        st = K.product_status(pick.chosen.p, "mehadrin")
        if st != "ok":
            self.assertIn("כשרות לא ידועה", pick.flags)


class ReportLine(unittest.TestCase):
    def test_unknown_kashrut_summarised_once(self):
        from grocery import report
        D = _tmp_defaults()
        learn_preference("כללי", "תמיד מהדרין/בדצ", defaults_path=D)
        d = load_defaults(D)
        names = ["חלב", "ביצים", "מלפפון"]
        res = {s: [_picker(s, d).pick(d.resolve(n)) for n in names] for s in ("rami_levy", "shufersal")}
        msg = report.cart_message(res, {"rami_levy": "רמי לוי", "shufersal": "שופרסל"},
                                  {"rami_levy": ["https://x"], "shufersal": ["https://y"]})
        self.assertEqual(msg.count("כשרות לא ידועה"), 1)
        self.assertNotIn("מלפפון", msg.split("כשרות לא ידועה")[1].split("\n")[0])
