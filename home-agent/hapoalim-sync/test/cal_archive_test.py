#!/usr/bin/env python3
"""cal_archive.py tests (synthetic rows only)."""
import csv
import io
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "host"))
import cal_archive as ca  # noqa: E402

TZ = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=TZ).timestamp()


def row(card, bm, n, **kw):
    r = {"card": card, "billing_month": bm, "purchase_date": f"{ca.add_months(bm, -1)}-0{n}", "charge_date": f"{bm}-10",
         "description": f"synthetic {n}", "amount": -n, "currency": "ILS", "status": "completed",
         "identifier": f"id{n}"}
    r.update(kw)
    return r


def payload(months_by_card, rows, cards=None, pending="ok", capped=()):
    retr = {c: {"known": True, "months": [{"month": m, "rows": 0, "capped": (c, m) in capped} for m in ms],
                "complete": True, "pending": pending} for c, ms in months_by_card.items()}
    return {"cards": cards or list(months_by_card), "retrieval": retr, "rows": rows}


class CalArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "archive"

    def tearDown(self):
        self.tmp.cleanup()

    def test_identical_rows_and_installments_survive(self):
        dup = row("1111", "2026-03", 3)
        rows = [row("1111", "2026-03", 1), dup, dict(dup),
                row("1111", "2026-03", 9, purchase_date="2026-01-15", installment_number=2, installment_total=3, identifier="i2"),
                row("1111", "2026-04", 9, purchase_date="2026-01-15", installment_number=3, installment_total=3, identifier="i3")]
        res = ca.merge_sync(self.dir, payload({"1111": ["2026-03", "2026-04"]}, rows), now=NOW)
        self.assertEqual(res["new"], 5)
        m = ca.read_rows(ca.month_path(self.dir, "1111", "2026-03"))
        self.assertEqual(len(m), 4)
        self.assertEqual(len({r["dedupe_key"] for r in m}), 4)
        self.assertTrue(all(r["dedupe_key"].startswith("c1:1111:") for r in m))
        # Re-pull of the same data maps onto the same keys (no growth).
        res = ca.merge_sync(self.dir, payload({"1111": ["2026-03", "2026-04"]}, rows), now=NOW)
        self.assertEqual((res["new"], res["removed"]), (0, 0))
        r = ca.read_month(self.dir, "2026-04", now=NOW)
        self.assertEqual(r["rows"][0]["purchase_date"], "2026-01-15")
        self.assertEqual(r["rows"][0]["installment_number"], "3")

    def test_modes(self):
        ca.merge_sync(self.dir, payload({"1111": ["2026-03"]}, [row("1111", "2026-03", 1)]), now=NOW)
        self.assertEqual(self.dir.stat().st_mode & 0o777, 0o700)
        for p in self.dir.iterdir():
            self.assertEqual(p.stat().st_mode & 0o777, 0o600, p.name)

    def test_coverage_final_complete_all_cards(self):
        ca.merge_sync(self.dir, payload({"1111": ["2026-03"], "2222": ["2026-03"]},
                                        [row("1111", "2026-03", 1)]), now=NOW)
        self.assertTrue(ca.plan_gaps(self.dir, "2026-03", "2026-03", now=NOW)["covered"])
        # one card missing a month -> not covered
        ca.merge_sync(self.dir, payload({"1111": ["2026-05"], "2222": []}, [], cards=["1111", "2222"]), now=NOW)
        plan = ca.plan_gaps(self.dir, "2026-03", "2026-05", now=NOW)
        self.assertFalse(plan["covered"])
        self.assertEqual(plan["needed"], ["2026-04", "2026-05"])
        self.assertEqual((plan["fetch_from"], plan["fetch_to"]), ("2026-04", "2026-05"))
        # a new card appears -> its months are needed again
        ca.merge_sync(self.dir, payload({"3333": []}, [], cards=["1111", "2222", "3333"]), now=NOW)
        self.assertFalse(ca.plan_gaps(self.dir, "2026-03", "2026-03", now=NOW)["covered"])

    def test_open_and_recent_months_not_final(self):
        ca.merge_sync(self.dir, payload({"1111": ["2026-09", "2026-10", "2026-11"]}, []), now=NOW, repull_days=10)
        cov = ca.load_coverage(self.dir)["cards"]["1111"]["months"]
        self.assertTrue(cov["2026-09"]["final"])          # ended 09-30, +10 days < 10-22
        self.assertFalse(cov["2026-10"]["final"])
        self.assertFalse(cov["2026-11"]["final"])
        ca.merge_sync(self.dir, payload({"1111": ["2026-09"]}, []), now=datetime(2026, 10, 5, tzinfo=TZ).timestamp(),
                      repull_days=10)
        self.assertFalse(ca.load_coverage(self.dir)["cards"]["1111"]["months"]["2026-09"]["final"])
        r = ca.read_month(self.dir, "2026-10", now=NOW)
        self.assertFalse(r["complete"])
        self.assertIn("פתוח", r["text"])

    def test_snapshot_replace_open_union_final(self):
        ca.merge_sync(self.dir, payload({"1111": ["2026-10"]}, [row("1111", "2026-10", 1), row("1111", "2026-10", 2)]), now=NOW)
        res = ca.merge_sync(self.dir, payload({"1111": ["2026-10"]}, [row("1111", "2026-10", 2)]), now=NOW)
        self.assertEqual(res["removed"], 1)                 # open month: snapshot
        ca.merge_sync(self.dir, payload({"1111": ["2026-03"]}, [row("1111", "2026-03", 1), row("1111", "2026-03", 2)]), now=NOW)
        res = ca.merge_sync(self.dir, payload({"1111": ["2026-03"]}, [row("1111", "2026-03", 2)]), now=NOW)
        self.assertEqual(res["removed"], 0)                 # final month: union, never silently drops
        self.assertEqual(len(ca.read_rows(ca.month_path(self.dir, "1111", "2026-03"))), 2)

    def test_capped_never_complete(self):
        ca.merge_sync(self.dir, payload({"1111": ["2026-03"]}, [row("1111", "2026-03", 1)], capped={("1111", "2026-03")}), now=NOW)
        self.assertFalse(ca.plan_gaps(self.dir, "2026-03", "2026-03", now=NOW)["covered"])
        self.assertIn("2026-03", ca.coverage_report(self.dir, now=NOW)["cards"]["1111"]["open_or_partial"])

    def test_rows_outside_returned_months_dropped(self):
        res = ca.merge_sync(self.dir, payload({"1111": ["2026-03"]}, [row("1111", "2026-03", 1), row("1111", "2026-02", 1)]), now=NOW)
        self.assertEqual(res["dropped"], 1)
        self.assertFalse(ca.month_path(self.dir, "1111", "2026-02").exists())

    def test_unknown_retrieval_merges_nothing(self):
        p = {"cards": ["1111"], "retrieval": {"1111": {"known": False}}, "rows": [row("1111", "2026-03", 1)]}
        res = ca.merge_sync(self.dir, p, now=NOW)
        self.assertEqual(res["months"], [])
        self.assertFalse(ca.plan_gaps(self.dir, "2026-03", "2026-03", now=NOW)["covered"])

    def test_pending_snapshot(self):
        pend = {"card": "1111", "status": "pending", "purchase_date": "2026-10-20", "amount": -7, "description": "p"}
        ca.merge_sync(self.dir, payload({"1111": []}, [pend]), now=NOW)
        self.assertEqual(len(ca.read_rows(ca.pending_path(self.dir, "1111"))), 1)
        ca.merge_sync(self.dir, payload({"1111": []}, []), now=NOW)
        self.assertEqual(len(ca.read_rows(ca.pending_path(self.dir, "1111"))), 0)
        ca.merge_sync(self.dir, payload({"1111": []}, [pend], pending="failed"), now=NOW)
        self.assertEqual(len(ca.read_rows(ca.pending_path(self.dir, "1111"))), 0)   # failed -> untouched

    def test_rekey_tolerant_reader(self):
        ca.ensure_archive_dir(self.dir)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=ca.CSV_FIELDS)
        w.writeheader()
        r = ca.normalize_row(row("1111", "2026-03", 1))
        for k in ("legacy-1", "legacy-1", ""):
            w.writerow(dict(r, dedupe_key=k))
        ca.month_path(self.dir, "1111", "2026-03").write_text(buf.getvalue())
        rows = ca.read_rows(ca.month_path(self.dir, "1111", "2026-03"))
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({x["dedupe_key"] for x in rows}), 3)
        self.assertTrue(all(x["dedupe_key"].startswith("c1:1111:") for x in rows))

    def test_read_range_by_charge_and_purchase(self):
        rows = [row("1111", "2026-03", 1, charge_date="2026-03-10", purchase_date="2026-02-01"),
                row("1111", "2026-04", 2, charge_date="2026-04-10", purchase_date="2026-02-20")]
        ca.merge_sync(self.dir, payload({"1111": ["2026-02", "2026-03", "2026-04"]}, rows), now=NOW)
        r = ca.read_range(self.dir, "2026-03-01", "2026-03-31", now=NOW)
        self.assertEqual(r["row_count"], 1)
        self.assertTrue(r["complete"])
        r = ca.read_range(self.dir, "2026-02-01", "2026-02-28", by="purchase", now=NOW)
        self.assertEqual(r["row_count"], 2)
        self.assertEqual(r["months_scanned"], ["2026-02", "2026-03", "2026-04"])
        r = ca.read_range(self.dir, "2026-05-01", "2026-05-31", now=NOW)
        self.assertFalse(r["complete"])
        self.assertIn("ייתכן שחסרים", r["text"])

    def test_no_sync_text(self):
        r = ca.read_month(self.dir, "2026-03", now=NOW)
        self.assertFalse(r["complete"])
        self.assertIn("אין לומר", r["text"])
        with self.assertRaises(ValueError):
            ca.read_month(self.dir, "2026-3")
        with self.assertRaises(ValueError):
            ca.read_month(self.dir, "2026-03", card="12")

    def test_cc_copy_identical(self):
        cc = HERE.parent / "command-center" / "command_center" / "cal_archive.py"
        self.assertEqual(cc.read_bytes(), (HERE / "host" / "cal_archive.py").read_bytes())


if __name__ == "__main__":
    unittest.main()
