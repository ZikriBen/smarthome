#!/usr/bin/env python3
"""Archive unit tests: merge/dedupe, pending→superseded, month boundary, atomicity, gaps, import, names."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "host"))
import archive as A  # noqa: E402


def row(account, date, amount, description, **kw):
    r = {
        "account": account, "date": date, "processed_date": kw.get("processed_date", date),
        "amount": amount, "description": description,
        "status": kw.get("status", "completed"), "currency": "ILS",
    }
    if "identifier" in kw:
        r["identifier"] = kw["identifier"]
    if "reference" in kw:
        r["reference"] = kw["reference"]
    return r


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.arch = self.root / "archive"
        A.ensure_archive_dir(self.arch)

    def tearDown(self):
        self.tmp.cleanup()

    def test_account_suffix_and_filename(self):
        self.assertEqual(A.account_suffix("12345678"), "5678")
        self.assertEqual(A.account_suffix("**78"), "0078")
        self.assertEqual(A.account_suffix(""), "0000")
        p = A.month_path(self.arch, "5678", "2026-10")
        self.assertEqual(p.name, "hapoalim-5678-2026-10.csv")

    def test_dedupe_identifier_and_identical_duplicates(self):
        rows = [
            row("12345678", "2026-10-01", -10, "A", identifier="111"),
            row("12345678", "2026-10-01", -10, "A", identifier="111"),
            row("12345678", "2026-10-02", -5, "DUP"),
            row("12345678", "2026-10-02", -5, "DUP"),
        ]
        r1 = A.merge_sync(self.arch, rows, range_from="2026-10-01", range_to="2026-10-02", run_id="r1")
        self.assertEqual(r1["new"], 3)
        r2 = A.merge_sync(self.arch, rows, range_from="2026-10-01", range_to="2026-10-02", run_id="r2")
        self.assertEqual(r2["new"], 0)
        path = A.month_path(self.arch, "5678", "2026-10")
        self.assertTrue(path.is_file())
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        got = A.read_month_rows(path)
        self.assertEqual(len(got), 3)

    def test_pending_superseded(self):
        A.merge_sync(
            self.arch,
            [row("12", "2026-10-05", -20, "SHOP", identifier="P1", status="pending")],
            range_from="2026-10-01", range_to="2026-10-10", run_id="r1",
        )
        A.merge_sync(
            self.arch,
            [row("12", "2026-10-05", -20, "SHOP", identifier="P1", status="completed")],
            range_from="2026-10-01", range_to="2026-10-10", run_id="r2",
        )
        rows = A.read_month_rows(A.month_path(self.arch, "0012", "2026-10"))
        self.assertEqual(rows[0]["status"], "completed")
        A.merge_sync(
            self.arch,
            [row("12", "2026-10-06", -3, "X", identifier="KEEP", status="pending")],
            range_from="2026-10-01", range_to="2026-10-10", run_id="r3",
        )
        A.merge_sync(
            self.arch,
            [row("12", "2026-10-07", -1, "Y", identifier="NEW", status="completed")],
            range_from="2026-10-01", range_to="2026-10-10", run_id="r4",
        )
        rows = {r["reference"]: r["status"] for r in A.read_month_rows(A.month_path(self.arch, "0012", "2026-10"))}
        self.assertEqual(rows["KEEP"], "superseded")
        self.assertEqual(rows["NEW"], "completed")

    def test_month_boundary(self):
        rows = [
            row("9999", "2026-09-30", -1, "last", identifier="L"),
            row("9999", "2026-10-01", -2, "first", identifier="F"),
        ]
        A.merge_sync(self.arch, rows, range_from="2026-09-30", range_to="2026-10-01", run_id="r1")
        self.assertTrue(A.month_path(self.arch, "9999", "2026-09").is_file())
        self.assertTrue(A.month_path(self.arch, "9999", "2026-10").is_file())
        sep = A.read_month_rows(A.month_path(self.arch, "9999", "2026-09"))
        oct_ = A.read_month_rows(A.month_path(self.arch, "9999", "2026-10"))
        self.assertEqual(len(sep), 1)
        self.assertEqual(sep[0]["date"], "2026-09-30")
        self.assertEqual(len(oct_), 1)
        self.assertEqual(oct_[0]["date"], "2026-10-01")

    def test_atomic_rewrite_keeps_old_on_failure(self):
        A.merge_sync(
            self.arch, [row("55", "2026-10-01", -1, "ok", identifier="1")],
            range_from="2026-10-01", range_to="2026-10-01", run_id="r1",
        )
        path = A.month_path(self.arch, "0055", "2026-10")
        before = path.read_text(encoding="utf-8")
        real = A.atomic_write_text

        def boom(*a, **k):
            raise RuntimeError("disk full")

        with mock.patch.object(A, "atomic_write_text", side_effect=boom):
            with self.assertRaises(RuntimeError):
                A.merge_sync(
                    self.arch, [row("55", "2026-10-02", -2, "x", identifier="2")],
                    range_from="2026-10-01", range_to="2026-10-02", run_id="r2",
                )
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        cov = A.load_coverage(self.arch)
        # coverage still reflects first successful sync only
        meta = cov["accounts"]["0055"]["months"]["2026-10"]
        self.assertEqual(meta["synced_to"], "2026-10-01")

    def test_gaps_and_covered(self):
        import datetime as dt
        from zoneinfo import ZoneInfo
        now = dt.datetime(2026, 10, 10, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem")).timestamp()
        plan = A.plan_gaps(self.arch, "2026-09-01", "2026-09-30", repull_days=3, now=now)
        self.assertFalse(plan["covered"])
        A.merge_sync(
            self.arch, [row("12", "2026-09-15", -1, "z", identifier="1")],
            range_from="2026-09-01", range_to="2026-09-30", run_id="r1", now=now,
        )
        plan = A.plan_gaps(self.arch, "2026-09-01", "2026-09-30", repull_days=3, now=now)
        self.assertTrue(plan["covered"])
        plan = A.plan_gaps(self.arch, "2026-09-01", "2026-10-10", repull_days=3, now=now)
        self.assertFalse(plan["covered"])
        self.assertEqual(plan["fetch_from"], "2026-10-01")
        A.merge_sync(
            self.arch, [row("12", "2026-10-09", -1, "a", identifier="2")],
            range_from="2026-10-01", range_to="2026-10-10", run_id="r2", now=now,
        )
        plan = A.plan_gaps(self.arch, "2026-10-01", "2026-10-10", repull_days=3, now=now)
        self.assertFalse(plan["covered"])
        self.assertEqual(plan["fetch_from"], "2026-10-08")

    def test_import_idempotent_and_warning(self):
        csv_path = self.root / "hapoalim-20261022-100600-checking_transactions.csv"
        csv_path.write_text(
            "date,description,amount,account_balance_now,reference,account,processed_date,memo,currency,status,type\n"
            "2026-10-01,A,-10,,R1,1234,2026-10-01,,ILS,completed,normal\n"
            "2026-10-02,B,-20,,R2,1234,2026-10-02,,ILS,completed,normal\n",
            encoding="utf-8",
        )
        a = A.import_csv_file(self.arch, csv_path)
        b = A.import_csv_file(self.arch, csv_path)
        self.assertEqual(a["new"], 2)
        self.assertEqual(b["new"], 0)
        self.assertIn("coverage_warning", a)
        listing = A.list_archive(self.arch)
        self.assertEqual(len(listing["files"]), 1)
        self.assertEqual(listing["files"][0]["account"], "1234")

    def test_read_month_and_range(self):
        A.merge_sync(
            self.arch,
            [
                row("1111", "2026-09-15", -1, "a", identifier="1"),
                row("1111", "2026-10-05", -2, "b", identifier="2"),
                row("2222", "2026-10-06", -3, "c", identifier="3"),
            ],
            range_from="2026-09-01", range_to="2026-10-31", run_id="r1",
        )
        m = A.read_month(self.arch, "2026-10", account="1111")
        self.assertEqual(m["row_count"], 1)
        r = A.read_range(self.arch, "2026-09-01", "2026-10-31")
        self.assertEqual(r["row_count"], 3)
        r2 = A.read_range(self.arch, "2026-10-01", "2026-10-31", account="2222")
        self.assertEqual(r2["row_count"], 1)


    def test_quiet_month_covered_and_noncontiguous_not_complete(self):
        import datetime as dt
        from zoneinfo import ZoneInfo
        now = dt.datetime(2026, 10, 20, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem")).timestamp()
        # account seen, but no rows in August → August still covered (no file), complete
        A.merge_sync(self.arch, [row("12345678", "2026-09-10", -1, "s", identifier="9")],
                     range_from="2026-08-01", range_to="2026-09-30", run_id="r1", now=now,
                     accounts=["12345678"])
        self.assertFalse(A.month_path(self.arch, "5678", "2026-08").exists())
        cov = A.load_coverage(self.arch)["accounts"]["5678"]["months"]
        self.assertTrue(cov["2026-08"]["complete"])
        self.assertEqual(cov["2026-08"]["rows"], 0)
        self.assertTrue(A.plan_gaps(self.arch, "2026-08-01", "2026-09-30", repull_days=3, now=now)["covered"])
        # two disjoint syncs inside one month must NOT claim the hole between them
        A.merge_sync(self.arch, [], range_from="2026-07-01", range_to="2026-07-05", run_id="r2",
                     now=now, accounts=["12345678"])
        A.merge_sync(self.arch, [], range_from="2026-07-20", range_to="2026-07-31", run_id="r3",
                     now=now, accounts=["12345678"])
        jul = A.load_coverage(self.arch)["accounts"]["5678"]["months"]["2026-07"]
        self.assertFalse(jul["complete"])
        plan = A.plan_gaps(self.arch, "2026-07-01", "2026-07-31", repull_days=0, now=now)
        self.assertEqual(plan["gaps"], [{"from": "2026-07-06", "to": "2026-07-19"}])

    def test_open_current_month_not_complete(self):
        A.merge_sync(self.arch, [row("1", "2026-10-02", -1, "a", identifier="1")],
                     range_from="2026-10-01", range_to="2026-10-22", run_id="r1")
        meta = A.load_coverage(self.arch)["accounts"]["0001"]["months"]["2026-10"]
        self.assertFalse(meta["complete"])
        self.assertEqual(oct((self.arch / "_coverage.json").stat().st_mode & 0o777), "0o600")

    def test_payload_dict_form(self):
        rows = A.rows_from_payload({"accounts": ["1"], "rows": [row("1", "2026-10-02", -1, "a", identifier="x")]})
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["dedupe_key"].startswith("id:"))

    def test_masked_import_needs_suffix_and_dedupes_with_live(self):
        csv_path = self.root / "hapoalim-20261022-100600-checking_transactions.csv"
        csv_path.write_text(
            "date,description,amount,account_balance_now,reference,account,processed_date,memo,currency,status,type\n"
            "2026-10-01,A,-10,,R1,******78,2026-10-01,,ILS,completed,normal\n",
            encoding="utf-8")
        with self.assertRaises(ValueError):
            A.import_csv_file(self.arch, csv_path)
        with self.assertRaises(ValueError):
            A.import_csv_file(self.arch, csv_path, account_suffix_override="1234")  # visible 78 mismatch
        res = A.import_csv_file(self.arch, csv_path, account_suffix_override="5678")
        self.assertEqual(res["new"], 1)
        live = A.merge_sync(self.arch, [row("12345678", "2026-10-01", -10, "A", identifier="R1")],
                            range_from="2026-10-01", range_to="2026-10-01", run_id="live")
        self.assertEqual(live["new"], 0)
        self.assertEqual(len(A.read_month_rows(A.month_path(self.arch, "5678", "2026-10"))), 1)

    def test_dir_permissions(self):
        self.assertEqual(oct(self.arch.stat().st_mode & 0o777), "0o700")


if __name__ == "__main__":
    unittest.main()
