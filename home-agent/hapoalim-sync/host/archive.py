#!/usr/bin/env python3
"""File-name-indexed Hapoalim archive (no SQLite).

Layout (Ben-owned, dir 0700, files 0600):
  ~/smarthome/data/hapoalim/archive/hapoalim-<acct4>-YYYY-MM.csv
  ~/smarthome/data/hapoalim/archive/_coverage.json
  ~/smarthome/data/hapoalim/archive/.lock

acct4 = last 4 digits of the account number. If two distinct accounts share the
same last-4, their rows land in the same month file (documented collision risk;
suffix is stable and matches the masked CSV convention).

Dedupe key v2 (fix3): k2:{acct4}:{sha256(reference|date|amount|description)[:32]}:{occurrence}
  * the bank reference is NOT unique (one live pull: 150 rows, 42 distinct references), so it is only
    one part of the key; `occurrence` numbers exact repeats (same ref+date+amount+description) in
    input order, so genuine identical transactions on one day survive and a re-pull of the same day
    maps onto the same keys (multiset/max semantics across pulls).
  * processed_date and status are not in the key (pending → completed keeps its key).
  * legacy keys (id:{acct4}:{ref} / h:{sha}) are re-keyed on read (backward compatible). Rows the old
    key already collapsed cannot be recovered from the archive alone: see hapoalim-archive-repair.

Coverage (fix3): only the range that was really retrieved is recorded. merge_sync takes a per-account
coverage start (from the scraper's retrieval metadata, or the truncation heuristic in coverage_start);
months before it get no coverage, so plan_gaps never answers COVERED for data the bank cut off.

Pending→superseded: on merge of a range, pending rows in that date range that
did not return are marked status=superseded.

Coverage sidecar updated atomically ONLY after every touched month file is
rewritten. Gap plan always re-opens the last HAPOALIM_REPULL_DAYS (default 3).
"""
from __future__ import annotations

import csv
import fcntl
import hashlib
import io
import json
import os
import re
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Jerusalem")
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
YM = re.compile(r"^\d{4}-\d{2}$")
ARCHIVE_NAME_RE = re.compile(r"^hapoalim-(\d{4})-(\d{4}-\d{2})\.csv$")
DEFAULT_REPULL_DAYS = 3
KEY_PREFIX = "k2:"
DEFAULT_PAGE_CAP = 150          # rows the bank returns per request (israeli-bank-scrapers 6.9.0 + live)
DEFAULT_SUSPECT_GAP_DAYS = 45   # first row this much later than `from` → don't trust the early part
CSV_FIELDS = [
    "date", "description", "amount", "account_balance_now", "reference", "account",
    "processed_date", "memo", "currency", "status", "type", "dedupe_key",
]
STATUS_COMPLETED = "completed"
STATUS_PENDING = "pending"
STATUS_SUPERSEDED = "superseded"
COVERAGE_NAME = "_coverage.json"
LOCK_NAME = ".lock"


def today_il(now: Optional[float] = None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now, TZ).strftime("%Y-%m-%d")


def add_days(day: str, delta: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=delta)).isoformat()


def month_of(day: str) -> str:
    return day[:7]


def month_start(ym: str) -> str:
    return f"{ym}-01"


def month_end(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    if m == 12:
        nxt = date(y + 1, 1, 1)
    else:
        nxt = date(y, m + 1, 1)
    return (nxt - timedelta(days=1)).isoformat()


def months_spanning(from_date: str, to_date: str) -> list[str]:
    if from_date > to_date:
        return []
    out = []
    y, m = int(from_date[:4]), int(from_date[5:7])
    end = month_of(to_date)
    while True:
        ym = f"{y:04d}-{m:02d}"
        out.append(ym)
        if ym >= end:
            break
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def account_suffix(account: str) -> str:
    digits = re.sub(r"\D", "", str(account or ""))
    if not digits:
        return "0000"
    return digits[-4:].zfill(4)


def day_from_any(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    s = str(value).strip()
    if ISO.fullmatch(s):
        return s
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ).strftime("%Y-%m-%d")
    except ValueError:
        return None


def merge_intervals(intervals: list[tuple[str, str]]) -> list[tuple[str, str]]:
    items = sorted((a, b) for a, b in intervals if a and b and a <= b)
    if not items:
        return []
    out = [items[0]]
    for a, b in items[1:]:
        pa, pb = out[-1]
        if a <= add_days(pb, 1):
            out[-1] = (pa, max(pb, b))
        else:
            out.append((a, b))
    return out


def subtract_interval(have: tuple[str, str], cover: tuple[str, str]) -> list[tuple[str, str]]:
    a, b = have
    c, d = cover
    if d < a or c > b:
        return [(a, b)]
    out = []
    if c > a:
        out.append((a, add_days(c, -1)))
    if d < b:
        out.append((add_days(d, 1), b))
    return [(x, y) for x, y in out if x <= y]


def subtract_many(have: tuple[str, str], covered: list[tuple[str, str]]) -> list[tuple[str, str]]:
    gaps = [have]
    for c in merge_intervals(covered):
        nxt = []
        for g in gaps:
            nxt.extend(subtract_interval(g, c))
        gaps = nxt
    return gaps


def _amount_s(amount: Any) -> str:
    try:
        return f"{float(amount):.4f}"
    except (TypeError, ValueError):
        return str(amount) if amount is not None else ""


def _group_fp(account: str, reference: Any, date_s: str, amount: Any, description: str) -> str:
    # Keyed on the 4-digit suffix (the archive's account identity), so a masked-CSV import mapped to
    # the same suffix dedupes against live rows.
    ref = "" if reference is None else str(reference).strip()
    return "|".join([account_suffix(account), ref, str(date_s or ""), _amount_s(amount), str(description or "")])


def dedupe_key(account: str, *, identifier: Any = None, date_s: str = "", processed: str = "",
               amount: Any = None, description: str = "", occurrence: int = 0) -> str:
    """v2 key. `processed` is accepted for call compatibility but is not part of the key."""
    digest = hashlib.sha256(_group_fp(account, identifier, date_s, amount, description).encode()).hexdigest()[:32]
    return f"{KEY_PREFIX}{account_suffix(account)}:{digest}:{int(occurrence)}"


def _row_group(r: dict) -> str:
    ident = r.get("reference")
    if ident is None or ident == "":
        ident = r.get("identifier")
    return _group_fp(r.get("account") or "", ident, r.get("date") or "", r.get("amount"), r.get("description") or "")


def assign_occurrence(rows: list[dict]) -> list[dict]:
    """Number exact repeats (same acct4+ref+date+amount+description) 0..n-1 in input order."""
    counters: dict[str, int] = {}
    out: list[dict] = []
    for r in rows:
        rr = dict(r)
        g = _row_group(rr)
        rr["occurrence"] = counters.get(g, 0)
        counters[g] = rr["occurrence"] + 1
        out.append(rr)
    return out


def key_rows(rows: list[dict]) -> list[dict]:
    rows = assign_occurrence(rows)
    for r in rows:
        r["dedupe_key"] = dedupe_key(
            r["account"], identifier=r.get("reference"), date_s=r["date"],
            amount=r["amount"], description=r["description"], occurrence=int(r.get("occurrence") or 0),
        )
    return rows


def normalize_row(raw: dict) -> Optional[dict]:
    account = str(raw.get("account") or raw.get("account_id") or "").strip()
    date_s = day_from_any(raw.get("date"))
    if not account or not date_s:
        return None
    processed = day_from_any(raw.get("processed_date") or raw.get("processedDate")) or date_s
    amount = raw.get("amount")
    if amount is None:
        amount = raw.get("chargedAmount")
    try:
        amount_f = float(amount)
    except (TypeError, ValueError):
        return None
    ident = raw.get("identifier")
    if ident is None or ident == "":
        ident = raw.get("reference")
    status = str(raw.get("status") or STATUS_COMPLETED).strip().lower()
    if status not in (STATUS_COMPLETED, STATUS_PENDING, STATUS_SUPERSEDED):
        status = STATUS_COMPLETED
    bal = raw.get("account_balance_now")
    if bal is None:
        bal = raw.get("balance")
    try:
        bal_s = "" if bal is None or bal == "" else f"{float(bal)}"
    except (TypeError, ValueError):
        bal_s = ""
    return {
        "account": account,
        "acct4": account_suffix(account),
        "date": date_s,
        "processed_date": processed,
        "amount": amount_f,
        "currency": str(raw.get("currency") or raw.get("originalCurrency") or "ILS"),
        "description": str(raw.get("description") or ""),
        "memo": str(raw.get("memo") or ""),
        "status": status,
        "type": str(raw.get("type") or raw.get("txn_type") or ""),
        "reference": None if ident is None or str(ident).strip() == "" else str(ident).strip(),
        "account_balance_now": bal_s,
        "occurrence": int(raw.get("occurrence") or 0),
    }


def rows_from_csv_text(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    raw = [normalize_row(dict(r)) for r in reader]
    return key_rows([r for r in raw if r])


def rows_from_payload(payload) -> list[dict]:
    """payload: list of rows, or {"accounts": [...], "rows": [...]} (checking_transactions)."""
    if isinstance(payload, dict):
        payload = payload.get("rows") or []
    raw = [normalize_row(dict(r)) for r in payload if isinstance(r, dict)]
    return key_rows([r for r in raw if r])


def month_path(archive_dir: Path, acct4: str, ym: str) -> Path:
    return Path(archive_dir) / f"hapoalim-{acct4}-{ym}.csv"


def read_month_rows(path: Path) -> list[dict]:
    """Rows of one month file. Stored v2 keys are kept as-is (stable even for superseded rows);
    legacy keys (id:/h:/missing) are re-keyed with v2, numbering repeats after any v2 key already
    present in the file for the same group."""
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    out: list[dict] = []
    legacy: list[dict] = []
    used: set[str] = set()
    for raw in csv.DictReader(io.StringIO(text)):
        n = normalize_row(dict(raw))
        if not n:
            continue
        key = (raw.get("dedupe_key") or "").strip()
        if key.startswith(KEY_PREFIX):
            n["dedupe_key"] = key
            used.add(key)
            out.append(n)
        else:
            legacy.append(n)
    for r in legacy:
        occ = 0
        while True:
            key = dedupe_key(r["account"], identifier=r.get("reference"), date_s=r["date"],
                             amount=r["amount"], description=r["description"], occurrence=occ)
            if key not in used:
                break
            occ += 1
        r["occurrence"] = occ
        r["dedupe_key"] = key
        r["_rekeyed"] = True
        used.add(key)
        out.append(r)
    return out


def rows_to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    ordered = sorted(rows, key=lambda r: (r.get("date") or "", r.get("amount") or 0, r.get("dedupe_key") or ""))
    for r in ordered:
        w.writerow({
            "date": r.get("date"),
            "description": r.get("description"),
            "amount": r.get("amount"),
            "account_balance_now": r.get("account_balance_now") or "",
            "reference": r.get("reference") or "",
            "account": r.get("account"),
            "processed_date": r.get("processed_date"),
            "memo": r.get("memo") or "",
            "currency": r.get("currency") or "ILS",
            "status": r.get("status") or STATUS_COMPLETED,
            "type": r.get("type") or "",
            "dedupe_key": r.get("dedupe_key") or "",
        })
    return buf.getvalue()


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600, uid: int | None = None,
                      gid: int | None = None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        if uid is not None and os.geteuid() == 0:
            os.chown(tmp, uid, gid if gid is not None else uid)
        os.replace(tmp, path)
        # fsync directory for durability of the rename
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ArchiveLock:
    def __init__(self, archive_dir: Path):
        self.path = Path(archive_dir) / LOCK_NAME
        self._fh = None

    def __enter__(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


def ensure_archive_dir(archive_dir: Path, *, uid: int | None = None, gid: int | None = None) -> Path:
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if uid is not None and os.geteuid() == 0:
        try:
            os.chown(archive_dir, uid, gid if gid is not None else uid)
            os.chmod(archive_dir, 0o700)
        except OSError:
            pass
    return archive_dir


def load_coverage(archive_dir: Path) -> dict:
    path = Path(archive_dir) / COVERAGE_NAME
    if not path.is_file():
        return {"version": 1, "accounts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "accounts": {}}
    if not isinstance(data, dict):
        return {"version": 1, "accounts": {}}
    data.setdefault("version", 1)
    data.setdefault("accounts", {})
    return data


def save_coverage(archive_dir: Path, coverage: dict, *, uid: int | None = None, gid: int | None = None):
    atomic_write_text(
        Path(archive_dir) / COVERAGE_NAME,
        json.dumps(coverage, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        mode=0o600, uid=uid, gid=gid,
    )


def _account_cov(coverage: dict, acct4: str) -> dict:
    accounts = coverage.setdefault("accounts", {})
    node = accounts.setdefault(acct4, {"months": {}})
    node.setdefault("months", {})
    return node


def covered_intervals_for(coverage: dict, acct4: str) -> list[tuple[str, str]]:
    node = (coverage.get("accounts") or {}).get(acct4) or {}
    intervals = []
    for ym, meta in (node.get("months") or {}).items():
        if not isinstance(meta, dict):
            continue
        if meta.get("ranges"):
            for x in meta["ranges"]:
                if isinstance(x, (list, tuple)) and len(x) == 2 and all(ISO.fullmatch(str(v)) for v in x):
                    intervals.append((str(x[0]), str(x[1])))
            continue
        a = meta.get("synced_from")
        b = meta.get("synced_to")
        if a and b and ISO.fullmatch(str(a)) and ISO.fullmatch(str(b)):
            intervals.append((str(a), str(b)))
    return merge_intervals(intervals)


def list_acct4(coverage: dict, archive_dir: Path | None = None) -> list[str]:
    found = set((coverage.get("accounts") or {}).keys())
    if archive_dir:
        for p in Path(archive_dir).glob("hapoalim-????-????-??.csv"):
            m = ARCHIVE_NAME_RE.match(p.name)
            if m:
                found.add(m.group(1))
    return sorted(found)


def plan_gaps(
    archive_dir: Path,
    from_date: str,
    to_date: str,
    *,
    repull_days: int = DEFAULT_REPULL_DAYS,
    now: Optional[float] = None,
    coverage: Optional[dict] = None,
) -> dict:
    if not ISO.fullmatch(from_date) or not ISO.fullmatch(to_date) or from_date > to_date:
        raise ValueError("bad range")
    today = today_il(now)
    if to_date > today:
        to_date = today
    coverage = coverage if coverage is not None else load_coverage(archive_dir)
    accounts = list_acct4(coverage, archive_dir)
    if not accounts:
        return {
            "covered": False,
            "gaps": [{"from": from_date, "to": to_date}],
            "fetch_from": from_date,
            "fetch_to": to_date,
            "accounts": [],
            "repull_days": repull_days,
            "reason": "no_accounts",
        }
    all_gaps: list[tuple[str, str]] = []
    repull_from = add_days(today, -(max(repull_days, 0) - 1)) if repull_days > 0 else None
    for acct in accounts:
        covered = covered_intervals_for(coverage, acct)
        missing = subtract_many((from_date, to_date), covered)
        if repull_from and repull_from <= to_date:
            force_a = max(from_date, repull_from)
            force_b = to_date
            if force_a <= force_b:
                missing = merge_intervals(missing + [(force_a, force_b)])
        all_gaps.extend(missing)
    merged = merge_intervals(all_gaps)
    if not merged:
        return {
            "covered": True, "gaps": [], "fetch_from": None, "fetch_to": None,
            "accounts": accounts, "repull_days": repull_days,
        }
    return {
        "covered": False,
        "gaps": [{"from": a, "to": b} for a, b in merged],
        "fetch_from": merged[0][0],
        "fetch_to": merged[-1][1],
        "accounts": accounts,
        "repull_days": repull_days,
    }


def coverage_start(
    rows: list[dict],
    range_from: str,
    range_to: str,
    *,
    retrieval: Optional[dict] = None,
    cap: int = DEFAULT_PAGE_CAP,
    suspect_gap_days: int = DEFAULT_SUSPECT_GAP_DAYS,
) -> tuple[Optional[str], list[str]]:
    """First day of [range_from, range_to] that was REALLY retrieved for one account, or None.

    * retrieval (patched scraper metadata): complete → range_from; else its completeFrom (None if absent).
    * no metadata (old image / CSV import): >= cap rows means the bank cut the oldest rows, and the
      oldest returned day may be partial → start the day after the oldest row.
    * either way, if the first row is more than suspect_gap_days after range_from, start at that row
      (a checking account is rarely silent for that long; better one extra login than a false 'no
      transactions').
    """
    warnings: list[str] = []
    dates = sorted(r["date"] for r in rows if r.get("date") and range_from <= r["date"] <= range_to)
    start: Optional[str] = range_from
    if isinstance(retrieval, dict) and retrieval.get("known") is not False and (
            "complete" in retrieval or "completeFrom" in retrieval):
        if not retrieval.get("complete"):
            cf = retrieval.get("completeFrom")
            start = str(cf) if cf and ISO.fullmatch(str(cf)) else None
            warnings.append(f"bank retrieval incomplete ({retrieval.get('stopReason') or 'unknown'})")
    elif len(dates) >= cap:
        start = add_days(dates[0], 1)
        warnings.append(f"{len(dates)} rows >= bank cap {cap}: older rows were cut off")
    if start is not None and dates and dates[0] > add_days(range_from, suspect_gap_days) and start < dates[0]:
        start = dates[0]
        warnings.append(f"first row {dates[0]} is > {suspect_gap_days} days after {range_from}")
    if start is not None:
        start = max(start, range_from)
        if start > range_to:
            start = None
    return start, warnings


def merge_month_rows(
    existing: list[dict],
    incoming: list[dict],
    *,
    range_from: str,
    range_to: str,
) -> tuple[list[dict], dict]:
    """Merge incoming into existing for one month file. Returns (rows, stats)."""
    by_key: dict[str, dict] = {}
    for r in existing:
        by_key[r["dedupe_key"]] = dict(r)
    stats = {"new": 0, "updated": 0, "superseded": 0}
    seen: set[str] = set()
    for r in incoming:
        key = r["dedupe_key"]
        seen.add(key)
        if key in by_key:
            prev = by_key[key]
            merged = dict(prev)
            merged.update(r)
            # keep first_seen semantics lightly via unchanged key
            by_key[key] = merged
            stats["updated"] += 1
        else:
            by_key[key] = dict(r)
            stats["new"] += 1
    # pending in range that did not return → superseded
    for key, r in list(by_key.items()):
        if r.get("status") != STATUS_PENDING:
            continue
        d = r.get("date") or ""
        if d < range_from or d > range_to:
            continue
        if key not in seen:
            r = dict(r)
            r["status"] = STATUS_SUPERSEDED
            by_key[key] = r
            stats["superseded"] += 1
    return list(by_key.values()), stats


def merge_sync(
    archive_dir: Path,
    rows: list[dict],
    *,
    range_from: str,
    range_to: str,
    run_id: str = "",
    source: str = "sync",
    uid: int | None = None,
    gid: int | None = None,
    now: Optional[float] = None,
    update_coverage: bool = True,
    coverage_warning: Optional[str] = None,
    accounts: Optional[Iterable[str]] = None,
    coverage_from: Optional[dict] = None,
) -> dict:
    """Merge rows into month files + update coverage atomically after all month writes.

    On failure mid-way, already-written month files may be new content (each month write
    is itself atomic — old file remains if that month's rewrite fails). Coverage is NOT
    updated unless every planned month write succeeded.
    """
    now = time.time() if now is None else now
    archive_dir = ensure_archive_dir(archive_dir, uid=uid, gid=gid)
    normed = []
    for r in rows:
        n = normalize_row(r if isinstance(r, dict) else {})
        if n:
            normed.append(n)
    rows = key_rows(normed)

    # bucket by (acct4, YYYY-MM)
    buckets: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        if not r.get("date"):
            continue
        buckets.setdefault((r["acct4"], month_of(r["date"])), []).append(r)

    # Also touch months in range with no rows? No — coverage only for accounts that appear.
    totals = {"new": 0, "updated": 0, "superseded": 0, "months": []}
    with ArchiveLock(archive_dir):
        coverage = load_coverage(archive_dir)
        written_months: list[tuple[str, str, int, bool]] = []  # acct4, ym, rows, complete
        try:
            for (acct4, ym), incoming in sorted(buckets.items()):
                path = month_path(archive_dir, acct4, ym)
                existing = read_month_rows(path)
                # clip merge supersede window to this month ∩ requested range
                clip_from = max(range_from, month_start(ym))
                clip_to = min(range_to, month_end(ym))
                merged, stats = merge_month_rows(
                    existing, incoming, range_from=clip_from, range_to=clip_to,
                )
                atomic_write_text(path, rows_to_csv(merged), mode=0o600, uid=uid, gid=gid)
                totals["new"] += stats["new"]
                totals["updated"] += stats["updated"]
                totals["superseded"] += stats["superseded"]
                # month complete if sync covered through month_end
                complete = update_coverage and range_to >= month_end(ym) and range_from <= month_start(ym)
                # weaker: complete if synced_to reaches month_end (union below)
                written_months.append((acct4, ym, len(merged), complete))
                totals["months"].append(f"hapoalim-{acct4}-{ym}.csv")

            if update_coverage:
                # Coverage for EVERY month in [range_from, range_to] for every account seen in this
                # sync (rows or the scraper's account list) — a quiet month with 0 rows is still
                # covered (no file is written for it). A brand-new account gets only this range.
                rows_by_month = {(a, ym): n for a, ym, n, _ in written_months}
                acct_set = {a for a, _, _, _ in written_months}
                acct_set |= {account_suffix(x) for x in (accounts or []) if str(x or "").strip()}
                # coverage_from: {acct4: first really-retrieved day or None}; missing acct → range_from.
                totals["coverage_from"] = {}
                for acct4 in sorted(acct_set):
                    node = _account_cov(coverage, acct4)
                    acct_from = range_from
                    if coverage_from is not None and acct4 in coverage_from:
                        acct_from = coverage_from[acct4]
                    totals["coverage_from"][acct4] = acct_from
                    if acct_from is None or acct_from > range_to:
                        continue
                    acct_from = max(acct_from, range_from)
                    for ym in months_spanning(acct_from, range_to):
                        meta = node["months"].setdefault(ym, {})
                        clip = (max(acct_from, month_start(ym)), min(range_to, month_end(ym)))
                        ranges = [tuple(x) for x in (meta.get("ranges") or []) if isinstance(x, (list, tuple)) and len(x) == 2]
                        if not ranges and meta.get("synced_from") and meta.get("synced_to"):
                            ranges = [(str(meta["synced_from"]), str(meta["synced_to"]))]
                        ranges = merge_intervals(ranges + [clip])
                        meta["ranges"] = [list(x) for x in ranges]
                        meta["synced_from"] = ranges[0][0]
                        meta["synced_to"] = ranges[-1][1]
                        # complete = one contiguous synced range spanning the whole month
                        meta["complete"] = bool(len(ranges) == 1 and ranges[0][0] <= month_start(ym)
                                                and ranges[0][1] >= month_end(ym))
                        meta["last_sync"] = now
                        if (acct4, ym) in rows_by_month:
                            meta["rows"] = rows_by_month[(acct4, ym)]
                        else:
                            meta.setdefault("rows", 0)
                        meta["last_run_id"] = run_id
                        meta["source"] = source
                coverage["last_run_id"] = run_id
                coverage["last_sync"] = now
                if coverage_warning:
                    coverage["last_warning"] = coverage_warning
                save_coverage(archive_dir, coverage, uid=uid, gid=gid)
        except Exception:
            # coverage untouched; month files that succeeded already replaced atomically
            raise

    totals["accounts"] = sorted({a for a, _, _, _ in written_months})
    totals["coverage_updated"] = bool(update_coverage)
    if coverage_warning:
        totals["coverage_warning"] = coverage_warning
    return totals


def import_csv_file(
    archive_dir: Path,
    path: Path,
    *,
    uid: int | None = None,
    gid: int | None = None,
    run_id: str | None = None,
    account_suffix_override: str | None = None,
    cap: int = DEFAULT_PAGE_CAP,
) -> dict:
    """Import one out/ CSV. Its `account` column is masked by default (`******78`); masked rows need
    `account_suffix_override` (the real last 4 digits) or the import is refused — otherwise they would
    land in a wrong `0078` file and never dedupe against live rows."""
    text = Path(path).read_text(encoding="utf-8")
    raw = list(csv.DictReader(io.StringIO(text)))
    masked = any("*" in str(r.get("account") or "") for r in raw)
    if masked:
        if not account_suffix_override or not re.fullmatch(r"\d{4}", str(account_suffix_override)):
            raise ValueError("masked account column; pass the real last-4 digits (--account-suffix NNNN)")
        tail = str(account_suffix_override)
        for r in raw:
            acc = str(r.get("account") or "")
            if "*" in acc:
                vis = re.sub(r"\D", "", acc)
                if vis and not tail.endswith(vis):
                    raise ValueError("--account-suffix does not match the visible digits of the masked account")
                r["account"] = tail
    rows = [n for n in (normalize_row(r) for r in raw) if n]
    if not rows:
        return {"new": 0, "updated": 0, "superseded": 0, "months": [], "warning": "empty csv"}
    dates = sorted(r["date"] for r in rows)
    dmin, dmax = dates[0], dates[-1]
    warning = (
        f"CSV has no requested-range metadata; coverage set to min..max of row dates "
        f"({dmin}..{dmax})"
    )
    by_acct: dict[str, list[dict]] = {}
    for r in rows:
        by_acct.setdefault(r["acct4"], []).append(r)
    cov_from = {}
    for acct4, arows in by_acct.items():
        start, warns = coverage_start(arows, dmin, dmax, cap=cap)
        cov_from[acct4] = start
        if warns:
            warning += "; " + "; ".join(warns) + f" → coverage from {start or 'nowhere'}"
    rid = run_id or f"import-{hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()[:16]}"
    result = merge_sync(
        archive_dir, rows, range_from=dmin, range_to=dmax, run_id=rid,
        source="import", uid=uid, gid=gid, update_coverage=True, coverage_warning=warning,
        coverage_from=cov_from,
    )
    result["coverage_warning"] = warning
    result["row_dates"] = {"min": dmin, "max": dmax}
    result["path"] = str(path)
    return result


def _csv_rows_for_repair(path: Path, suffix: Optional[str], known_acct4: list[str]) -> tuple[list[dict], str]:
    """Rows of one out/ CSV keyed with v2 keys; masked accounts mapped to the real last-4 (explicit
    --account-suffix, or the single archive account whose suffix ends with the visible digits)."""
    raw = list(csv.DictReader(io.StringIO(Path(path).read_text(encoding="utf-8"))))
    used = None
    for r in raw:
        acc = str(r.get("account") or "")
        if "*" not in acc:
            continue
        vis = re.sub(r"\D", "", acc)
        tail = suffix
        if not tail:
            cands = [a for a in known_acct4 if a.endswith(vis)] if vis else []
            if len(cands) != 1:
                raise ValueError(f"{Path(path).name}: masked account; pass --account-suffix NNNN")
            tail = cands[0]
        if vis and not tail.endswith(vis):
            raise ValueError(f"{Path(path).name}: --account-suffix does not match the visible digits")
        r["account"] = tail
        used = tail
    rows = key_rows([n for n in (normalize_row(r) for r in raw) if n])
    return rows, used or ""


def repair_archive(
    archive_dir: Path,
    csv_paths: Iterable[Path],
    *,
    dry_run: bool = True,
    account_suffix_override: Optional[str] = None,
    backup_dir: Optional[Path] = None,
    cap: int = DEFAULT_PAGE_CAP,
    uid: int | None = None,
    gid: int | None = None,
    now: Optional[float] = None,
) -> dict:
    """Rebuild the archive with v2 keys from (a) the current month files, re-keyed, and (b) the out/
    CSVs still on disk (only these can restore rows the old key collapsed), then drop bogus coverage:
    per account nothing before the day AFTER the oldest row (that day may be partial in a capped
    response). Months left without coverage are re-fetched by the next hapoalim_sync (one login).
    dry_run → nothing written. Otherwise: tar.gz backup (0600) first, then atomic rewrites."""
    now = time.time() if now is None else now
    archive_dir = Path(archive_dir)
    if account_suffix_override and not re.fullmatch(r"\d{4}", str(account_suffix_override)):
        raise ValueError("--account-suffix must be 4 digits")
    with ArchiveLock(archive_dir) if not dry_run else _NoLock():
        coverage = load_coverage(archive_dir)
        known = list_acct4(coverage, archive_dir)
        files: dict[tuple[str, str], dict[str, dict]] = {}
        before: dict[tuple[str, str], dict] = {}
        for pth in sorted(archive_dir.glob("hapoalim-????-????-??.csv")):
            m = ARCHIVE_NAME_RE.match(pth.name)
            if not m:
                continue
            k = (m.group(1), m.group(2))
            rows = read_month_rows(pth)
            raw_lines = max(pth.read_text(encoding="utf-8").count("\n") - 1, 0)
            before[k] = {"rows": raw_lines,
                         "active": sum(1 for r in rows if r.get("status") != STATUS_SUPERSEDED),
                         "rekeyed": sum(1 for r in rows if r.get("_rekeyed"))}
            files[k] = {r["dedupe_key"]: r for r in rows}
        csv_report = []
        for cp in csv_paths:
            cp = Path(cp)
            rows, used = _csv_rows_for_repair(cp, account_suffix_override, known)
            added = 0
            for r in rows:
                bucket = files.setdefault((r["acct4"], month_of(r["date"])), {})
                if r["dedupe_key"] not in bucket:
                    bucket[r["dedupe_key"]] = r
                    added += 1
            dates = sorted(r["date"] for r in rows)
            csv_report.append({"file": cp.name, "rows": len(rows), "added": added, "account": used,
                               "min": dates[0] if dates else None, "max": dates[-1] if dates else None,
                               "capped": len(rows) >= cap})
        # coverage: clip every account at (oldest active row + 1 day)
        earliest: dict[str, str] = {}
        for (acct4, ym), bucket in files.items():
            for r in bucket.values():
                if r.get("status") == STATUS_SUPERSEDED:
                    continue
                if acct4 not in earliest or r["date"] < earliest[acct4]:
                    earliest[acct4] = r["date"]
        new_cov = json.loads(json.dumps(coverage))
        dropped: list[str] = []
        for acct4, node in (new_cov.get("accounts") or {}).items():
            floor = add_days(earliest[acct4], 1) if acct4 in earliest else None
            months = node.get("months") or {}
            for ym in sorted(list(months)):
                meta = months[ym] if isinstance(months[ym], dict) else {}
                ranges = [tuple(x) for x in (meta.get("ranges") or []) if isinstance(x, (list, tuple)) and len(x) == 2]
                if not ranges and meta.get("synced_from") and meta.get("synced_to"):
                    ranges = [(str(meta["synced_from"]), str(meta["synced_to"]))]
                if floor is None:
                    ranges = []
                else:
                    ranges = [(max(a, floor), b) for a, b in ranges if b >= floor]
                if not ranges:
                    del months[ym]
                    dropped.append(f"{acct4}:{ym}")
                    continue
                ranges = merge_intervals(ranges)
                meta["ranges"] = [list(x) for x in ranges]
                meta["synced_from"], meta["synced_to"] = ranges[0][0], ranges[-1][1]
                meta["complete"] = bool(len(ranges) == 1 and ranges[0][0] <= month_start(ym)
                                        and ranges[0][1] >= month_end(ym))
            for ym in list(months):
                months[ym]["rows"] = len(files.get((acct4, ym), {}))
            node["earliest_row"] = earliest.get(acct4)
        new_cov["repaired_at"] = now
        new_cov["last_warning"] = ("repair: coverage before the day after the oldest row dropped "
                                   "(capped bank responses); re-sync to backfill")
        table = []
        for k in sorted(set(before) | set(files)):
            acct4, ym = k
            ob = ((coverage.get("accounts") or {}).get(acct4) or {}).get("months", {}).get(ym) or {}
            nb = ((new_cov.get("accounts") or {}).get(acct4) or {}).get("months", {}).get(ym) or {}
            after_rows = list((files.get(k) or {}).values())
            table.append({"account": acct4, "month": ym,
                          "before": (before.get(k) or {}).get("active", 0),
                          "after": sum(1 for r in after_rows if r.get("status") != STATUS_SUPERSEDED),
                          "cov_before": "complete" if ob.get("complete") else ("partial" if ob else "-"),
                          "cov_after": "complete" if nb.get("complete") else ("partial" if nb else "-")})
        for item in dropped:
            acct4, ym = item.split(":")
            if not any(t["account"] == acct4 and t["month"] == ym for t in table):
                table.append({"account": acct4, "month": ym, "before": 0, "after": 0,
                              "cov_before": "complete" if (((coverage.get("accounts") or {}).get(acct4) or {})
                                                           .get("months", {}).get(ym) or {}).get("complete") else "partial",
                              "cov_after": "-"})
        table.sort(key=lambda t: (t["account"], t["month"]))
        result = {"status": "dry_run" if dry_run else "repaired", "table": table, "csv": csv_report,
                  "dropped_coverage": dropped, "earliest_row": earliest,
                  "rows_before": sum(t["before"] for t in table), "rows_after": sum(t["after"] for t in table)}
        if dry_run:
            return result
        # backup first
        backup_dir = Path(backup_dir) if backup_dir else archive_dir.parent / "archive-backups"
        backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(now, TZ).strftime("%Y%m%d-%H%M%S")
        tar_path = backup_dir / f"hapoalim-archive-{stamp}.tar.gz"
        import tarfile
        old_umask = os.umask(0o077)
        try:
            with tarfile.open(tar_path, "x:gz") as tar:
                tar.add(str(archive_dir), arcname=archive_dir.name,
                        filter=lambda ti: None if ti.name.endswith(LOCK_NAME) else ti)
        finally:
            os.umask(old_umask)
        os.chmod(tar_path, 0o600)
        if uid is not None and os.geteuid() == 0:
            os.chown(tar_path, uid, gid if gid is not None else uid)
            os.chown(backup_dir, uid, gid if gid is not None else uid)
        result["backup"] = str(tar_path)
        for (acct4, ym), bucket in sorted(files.items()):
            rows = list(bucket.values())
            for r in rows:
                r.pop("_rekeyed", None)
            atomic_write_text(month_path(archive_dir, acct4, ym), rows_to_csv(rows), mode=0o600, uid=uid, gid=gid)
        save_coverage(archive_dir, new_cov, uid=uid, gid=gid)
        return result


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def list_archive(archive_dir: Path) -> dict:
    archive_dir = Path(archive_dir)
    coverage = load_coverage(archive_dir)
    files = []
    for p in sorted(archive_dir.glob("hapoalim-????-????-??.csv")):
        m = ARCHIVE_NAME_RE.match(p.name)
        if not m:
            continue
        acct4, ym = m.group(1), m.group(2)
        meta = ((coverage.get("accounts") or {}).get(acct4) or {}).get("months", {}).get(ym) or {}
        rows = meta.get("rows")
        if rows is None:
            try:
                rows = max(p.read_text(encoding="utf-8").count("\n") - 1, 0)
            except OSError:
                rows = None
        files.append({
            "name": p.name,
            "account": acct4,
            "month": ym,
            "rows": rows,
            "complete": bool(meta.get("complete")),
            "synced_from": meta.get("synced_from"),
            "synced_to": meta.get("synced_to"),
            "last_sync": meta.get("last_sync"),
        })
    return {"status": "ok", "files": files, "coverage": coverage}


def read_month(archive_dir: Path, yyyy_mm: str, account: Optional[str] = None) -> dict:
    if not YM.fullmatch(yyyy_mm):
        raise ValueError("month must be YYYY-MM")
    archive_dir = Path(archive_dir)
    acct = account_suffix(account) if account else None
    paths = []
    if acct:
        paths = [month_path(archive_dir, acct, yyyy_mm)]
    else:
        paths = sorted(archive_dir.glob(f"hapoalim-????-{yyyy_mm}.csv"))
    rows = []
    names = []
    for p in paths:
        if not p.is_file():
            continue
        names.append(p.name)
        rows.extend(read_month_rows(p))
    rows = [r for r in rows if r.get("status") != STATUS_SUPERSEDED]
    for r in rows:
        r.pop("_rekeyed", None)
    rows.sort(key=lambda r: (r.get("date") or "", r.get("amount") or 0))
    uncovered = uncovered_gaps(archive_dir, month_start(yyyy_mm), month_end(yyyy_mm), account=acct)
    text = f"{len(rows)} תנועות ב-{yyyy_mm}" + (f" (חשבון …{acct})" if acct else "") + "."
    if uncovered:
        text += " שים לב: הארכיון לא מכסה את כל החודש (" + ", ".join(f"{g['from']}..{g['to']}" for g in uncovered) + \
            ") — ייתכן שחסרות תנועות; אין לומר 'אין תנועות' לתקופה הזו."
    return {
        "status": "ok", "month": yyyy_mm, "account": acct, "files": names,
        "rows": rows, "row_count": len(rows), "complete": not uncovered, "uncovered": uncovered,
        "text": text,
    }


def uncovered_gaps(archive_dir: Path, from_date: str, to_date: str, account: Optional[str] = None,
                   now: Optional[float] = None) -> list[dict]:
    """Days in [from, min(to, today)] not in coverage, for the account (or ANY known account)."""
    to_date = min(to_date, today_il(now))
    if from_date > to_date:
        return []
    coverage = load_coverage(archive_dir)
    accts = [account_suffix(account)] if account else list_acct4(coverage, archive_dir)
    if not accts:
        return [{"from": from_date, "to": to_date}]
    gaps: list[tuple[str, str]] = []
    for a in accts:
        gaps.extend(subtract_many((from_date, to_date), covered_intervals_for(coverage, a)))
    return [{"from": a, "to": b} for a, b in merge_intervals(gaps)]


def read_range(archive_dir: Path, from_date: str, to_date: str, account: Optional[str] = None) -> dict:
    if not ISO.fullmatch(from_date) or not ISO.fullmatch(to_date) or from_date > to_date:
        raise ValueError("bad range")
    months = months_spanning(from_date, to_date)
    all_rows = []
    files = []
    for ym in months:
        part = read_month(archive_dir, ym, account=account)
        files.extend(part.get("files") or [])
        for r in part.get("rows") or []:
            d = r.get("date") or ""
            if from_date <= d <= to_date:
                all_rows.append(r)
    all_rows.sort(key=lambda r: (r.get("date") or "", r.get("amount") or 0))
    uncovered = uncovered_gaps(archive_dir, from_date, to_date, account=account)
    text = f"{len(all_rows)} תנועות מ-{from_date} עד {to_date}."
    if uncovered:
        text += " שים לב: הארכיון לא מכסה " + ", ".join(f"{g['from']}..{g['to']}" for g in uncovered) + \
            " — ייתכן שחסרות תנועות; אפשר להשלים עם hapoalim_sync."
    return {
        "status": "ok", "from": from_date, "to": to_date,
        "account": account_suffix(account) if account else None,
        "files": files, "rows": all_rows, "row_count": len(all_rows),
        "complete": not uncovered, "uncovered": uncovered,
        "text": text,
    }


def coverage_report(archive_dir: Path) -> dict:
    listing = list_archive(archive_dir)
    cov = listing["coverage"]
    n = sum(int(f.get("rows") or 0) for f in listing["files"])
    return {
        "status": "ok",
        "files": listing["files"],
        "accounts": list_acct4(cov, archive_dir),
        "transaction_count": n,
        "last_sync": cov.get("last_sync"),
        "last_run_id": cov.get("last_run_id"),
        "last_warning": cov.get("last_warning"),
        "text": f"ארכיון: {len(listing['files'])} קבצי חודש, ~{n} תנועות.",
    }
