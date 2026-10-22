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
        # fix3: the reference is not unique; two identical rows in one pull are two transactions
        self.assertEqual(r1["new"], 4)
        r2 = A.merge_sync(self.arch, rows, range_from="2026-10-01", range_to="2026-10-02", run_id="r2")
        self.assertEqual(r2["new"], 0)
        path = A.month_path(self.arch, "5678", "2026-10")
        self.assertTrue(path.is_file())
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        got = A.read_month_rows(path)
        self.assertEqual(len(got), 4)

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
        self.assertTrue(rows[0]["dedupe_key"].startswith("k2:"))

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


    # ---------------- fix3: key v2 + truthful coverage ----------------
    def _live_150(self):
        """150 rows 2026-04-15..2026-10-20 with only 42 distinct references (like the live pull)."""
        from datetime import date as D, timedelta
        rows = []
        d0 = D(2026, 4, 15)
        for i in range(150):
            d = (d0 + timedelta(days=(i * 189) // 150)).isoformat()
            rows.append(row("12-600-987654", d, -(i % 7) - 1, f"desc{i % 5}", identifier=str(9000 + i % 42)))
        # two genuine identical repeats
        rows[10] = dict(rows[9])
        return rows

    def test_ref_not_unique_keeps_all_150(self):
        rows = self._live_150()
        self.assertEqual(len({r["identifier"] for r in rows}), 42)
        r1 = A.merge_sync(self.arch, rows, range_from="2025-10-23", range_to="2026-10-22", run_id="r1")
        self.assertEqual(r1["new"], 150)
        total = sum(len(A.read_month_rows(p)) for p in self.arch.glob("hapoalim-*.csv"))
        self.assertEqual(total, 150)
        r2 = A.merge_sync(self.arch, rows, range_from="2025-10-23", range_to="2026-10-22", run_id="r2")
        self.assertEqual((r2["new"], r2["updated"]), (0, 150))

    def test_identical_repeats_max_semantics_across_pulls(self):
        same = row("1234", "2026-10-03", -5, "COFFEE", identifier="77")
        A.merge_sync(self.arch, [same, same], range_from="2026-10-03", range_to="2026-10-03", run_id="a")
        A.merge_sync(self.arch, [same, same, same], range_from="2026-10-03", range_to="2026-10-03", run_id="b")
        A.merge_sync(self.arch, [same], range_from="2026-10-03", range_to="2026-10-03", run_id="c")
        got = A.read_month(self.arch, "2026-10")["rows"]
        self.assertEqual(len(got), 3)

    def test_rekey_legacy_archive_file(self):
        path = A.month_path(self.arch, "5678", "2026-10")
        legacy = (
            "date,description,amount,account_balance_now,reference,account,processed_date,memo,currency,status,type,dedupe_key\n"
            "2026-10-01,A,-10.0,,111,12345678,2026-10-01,,ILS,completed,,id:5678:111\n"
            "2026-10-02,DUP,-5.0,,,12345678,2026-10-02,,ILS,completed,,h:abc\n"
            "2026-10-04,PEND,-3.0,,,12345678,2026-10-04,,ILS,superseded,,h:def\n"
        )
        path.write_text(legacy, encoding="utf-8")
        got = A.read_month_rows(path)
        self.assertEqual(len(got), 3)
        self.assertTrue(all(r["dedupe_key"].startswith("k2:5678:") for r in got))
        # a re-pull with the same row + a second (genuine) one with the same reference on the same day
        pull = [row("12345678", "2026-10-01", -10, "A", identifier="111"),
                row("12345678", "2026-10-01", -10, "A", identifier="111"),
                row("12345678", "2026-10-02", -5, "DUP")]
        res = A.merge_sync(self.arch, pull, range_from="2026-10-01", range_to="2026-10-02", run_id="r")
        self.assertEqual((res["new"], res["updated"]), (1, 2))
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("id:5678", text)
        self.assertEqual(len(A.read_month_rows(path)), 4)
        self.assertEqual(len(A.read_month(self.arch, "2026-10")["rows"]), 3)  # superseded hidden

    def test_truncated_payload_without_metadata_gets_partial_coverage(self):
        rows = self._live_150()
        start, warns = A.coverage_start([A.normalize_row(r) for r in rows], "2025-10-23", "2026-10-22")
        self.assertEqual(start, "2026-04-16")
        self.assertTrue(any("cap" in w for w in warns))
        A.merge_sync(self.arch, rows, range_from="2025-10-23", range_to="2026-10-22", run_id="r",
                     coverage_from={"7654": start}, now=1792627200)  # 2026-10-21
        months = A.load_coverage(self.arch)["accounts"]["7654"]["months"]
        self.assertNotIn("2025-11", months)
        self.assertNotIn("2026-03", months)
        self.assertFalse(months["2026-04"]["complete"])
        self.assertTrue(months["2026-05"]["complete"])
        plan = A.plan_gaps(self.arch, "2025-11-01", "2025-11-30", now=1792627200)
        self.assertFalse(plan["covered"])
        rm = A.read_month(self.arch, "2025-11")
        self.assertFalse(rm["complete"])
        self.assertIn("אין לומר", rm["text"])

    def test_coverage_start_from_retrieval_metadata(self):
        rows = [A.normalize_row(row("1", "2026-09-01", -1, "a"))]
        self.assertEqual(A.coverage_start(rows, "2026-08-25", "2026-10-22", retrieval={"complete": True})[0], "2026-08-25")
        st, w = A.coverage_start(rows, "2026-08-25", "2026-10-22",
                                 retrieval={"complete": False, "completeFrom": "2026-09-10", "stopReason": "no_progress"})
        self.assertEqual(st, "2026-09-10")
        self.assertTrue(w)
        self.assertIsNone(A.coverage_start(rows, "2026-08-25", "2026-10-22", retrieval={"complete": False})[0])
        # first row much later than from → start at the first row
        late = [A.normalize_row(row("1", "2026-06-01", -1, "a"))]
        self.assertEqual(A.coverage_start(late, "2025-10-23", "2026-10-22", retrieval={"complete": True})[0], "2026-06-01")
        # quiet range with zero rows and a complete walk is trusted
        self.assertEqual(A.coverage_start([], "2026-09-01", "2026-10-22", retrieval={"complete": True})[0], "2026-09-01")

    def test_coverage_from_none_records_no_coverage(self):
        A.merge_sync(self.arch, [row("1234", "2026-10-02", -1, "a")], range_from="2026-09-01",
                     range_to="2026-10-22", run_id="r", coverage_from={"1234": None})
        self.assertEqual(A.load_coverage(self.arch)["accounts"].get("1234", {}).get("months", {}), {})

    def test_import_truncated_csv_coverage_from_day_after_oldest(self):
        csv_path = self.root / "hapoalim-20261022-101900-checking_transactions.csv"
        lines = ["date,description,amount,account_balance_now,reference,account,processed_date,memo,currency,status,type"]
        for r in self._live_150():
            lines.append(f"{r['date']},{r['description']},{r['amount']},,{r['identifier']},******54,{r['date']},,ILS,completed,normal")
        csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        res = A.import_csv_file(self.arch, csv_path, account_suffix_override="7654")
        self.assertEqual(res["new"], 150)
        self.assertIn("cap", res["coverage_warning"])
        months = A.load_coverage(self.arch)["accounts"]["7654"]["months"]
        self.assertEqual(months["2026-04"]["synced_from"], "2026-04-16")

    # ---------------- fix3: repair ----------------
    def _legacy_fixture(self):
        """Old-format archive as the deployed 0005 code left it (refs collapsed per month), plus bogus
        coverage for the whole truncated 1y request, plus two identical capped out/ CSVs."""
        import csv as _csv, io as _io
        rows = self._live_150()
        by_month = {}
        for r in rows:
            by_month.setdefault(r["date"][:7], {})[r["identifier"]] = r
        for ym, refs in by_month.items():
            buf = _io.StringIO()
            w = _csv.DictWriter(buf, fieldnames=A.CSV_FIELDS, lineterminator="\n")
            w.writeheader()
            for ref, r in refs.items():
                w.writerow({"date": r["date"], "description": r["description"], "amount": float(r["amount"]),
                            "reference": ref, "account": "12-600-987654", "processed_date": r["date"],
                            "currency": "ILS", "status": "completed", "dedupe_key": f"id:7654:{ref}"})
            A.month_path(self.arch, "7654", ym).write_text(buf.getvalue(), encoding="utf-8")
        months = {}
        for ym in A.months_spanning("2025-10-23", "2026-10-22"):
            a, b = max("2025-10-23", A.month_start(ym)), min("2026-10-22", A.month_end(ym))
            months[ym] = {"ranges": [[a, b]], "synced_from": a, "synced_to": b,
                          "complete": a == A.month_start(ym) and b == A.month_end(ym), "rows": 0}
        A.save_coverage(self.arch, {"version": 1, "accounts": {"7654": {"months": months}}})
        out = self.root / "out"
        out.mkdir()
        lines = ["date,description,amount,account_balance_now,reference,account,processed_date,memo,currency,status,type"]
        for r in rows:
            lines.append(f"{r['date']},{r['description']},{r['amount']},,{r['identifier']},******54,{r['date']},,ILS,completed,normal")
        for name in ("hapoalim-20261022-101900-checking_transactions.csv", "hapoalim-20261022-171400-checking_transactions.csv"):
            (out / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
        legacy_count = sum(len(v) for v in by_month.values())
        return sorted(out.glob("*.csv")), legacy_count

    def _snapshot(self):
        return {p.name: p.read_bytes() for p in sorted(self.arch.iterdir()) if p.is_file()}

    def test_repair_dry_run_writes_nothing_and_reports_counts(self):
        csvs, legacy = self._legacy_fixture()
        self.assertLess(legacy, 150)
        snap = self._snapshot()
        res = A.repair_archive(self.arch, csvs, dry_run=True)
        self.assertEqual(self._snapshot(), snap)
        self.assertEqual((res["rows_before"], res["rows_after"]), (legacy, 150))
        self.assertEqual(res["csv"][0]["account"], "7654")      # inferred from the single archive account
        self.assertTrue(res["csv"][0]["capped"])
        self.assertEqual(res["csv"][1]["added"], 0)               # second CSV is byte-identical
        dec = [t for t in res["table"] if t["month"] == "2025-12"][0]
        self.assertEqual((dec["cov_before"], dec["cov_after"]), ("complete", "-"))
        self.assertIn("7654:2026-03", res["dropped_coverage"])
        self.assertFalse(any(p.parent.name == "archive-backups" for p in self.root.rglob("*.tar.gz")))

    def test_repair_real_backup_rekey_coverage_idempotent(self):
        csvs, legacy = self._legacy_fixture()
        before = self._snapshot()
        res = A.repair_archive(self.arch, csvs, dry_run=False, now=1792627200)
        tar = Path(res["backup"])
        self.assertTrue(tar.is_file())
        self.assertEqual(oct(tar.stat().st_mode & 0o777), "0o600")
        import tarfile
        with tarfile.open(tar) as t:
            names = {Path(n).name for n in t.getnames()}
        self.assertTrue(set(before) <= names | {".lock"})
        total = 0
        for pth in self.arch.glob("hapoalim-*.csv"):
            text = pth.read_text(encoding="utf-8")
            self.assertNotIn(",id:", text)
            total += len(A.read_month_rows(pth))
        self.assertEqual(total, 150)
        cov = A.load_coverage(self.arch)["accounts"]["7654"]
        self.assertNotIn("2025-12", cov["months"])
        self.assertEqual(cov["months"]["2026-04"]["synced_from"], "2026-04-16")
        self.assertTrue(cov["months"]["2026-05"]["complete"])
        self.assertFalse(A.plan_gaps(self.arch, "2025-12-01", "2025-12-31", now=1792627200)["covered"])
        again = A.repair_archive(self.arch, csvs, dry_run=True)
        self.assertEqual((again["rows_before"], again["rows_after"]), (150, 150))
        self.assertEqual([c["added"] for c in again["csv"]], [0, 0])

    def test_repair_cli_dry_run(self):
        import subprocess
        csvs, _ = self._legacy_fixture()
        cli = HERE / "host" / "hapoalim-archive-repair"
        r = subprocess.run([sys.executable, str(cli), "--dry-run", "--archive", str(self.arch),
                            "--out", str(self.root / "out")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DRY RUN", r.stdout)
        self.assertIn("-> 150", r.stdout)
        self.assertNotIn("desc1", r.stdout)  # never prints descriptions
        r2 = subprocess.run([sys.executable, str(cli), "--dry-run", "--archive", str(self.arch),
                             "--account-suffix", "1111", *map(str, csvs)], capture_output=True, text=True)
        self.assertEqual(r2.returncode, 2)

if __name__ == "__main__":
    unittest.main()
