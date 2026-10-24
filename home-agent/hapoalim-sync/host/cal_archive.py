#!/usr/bin/env python3
"""File-name-indexed Cal (visaCal) credit-card archive (no SQLite). Byte-identical copy in
command-center/command_center/cal_archive.py (a test enforces it); the Command Center reads it via a :ro mount.

Layout (Ben-owned, dir 0700, files 0600):
  ~/smarthome/data/cal/archive/cal-card-<last4>-YYYY-MM.csv    one file per card and BILLING month
  ~/smarthome/data/cal/archive/cal-card-<last4>-pending.csv    pending (not yet billed) snapshot, replaced each sync
  ~/smarthome/data/cal/archive/_coverage.json
  ~/smarthome/data/cal/archive/.lock

Date semantics (one row = one charge line on one statement):
  billing_month  the statement month the row was returned for (= the month the walk asked Cal for). The FILE month.
  purchase_date  the original purchase day (installment n of 3 still shows the day of the purchase).
  charge_date    the debit day of that statement line (debCrdDate); empty for pending rows.
  amount         the charged amount in `currency` (negative = charge, positive = refund/credit, library sign).
  original_amount / original_currency: the purchase amount (whole deal for installments; foreign currency).

Dedupe key c1: c1:<last4>:sha256(last4|billing_month|identifier|purchase_date|amount|original_amount|currency|
  description|inst_n/inst_t)[:32]:<occurrence>. `occurrence` numbers exact repeats in input order, so two genuinely
  identical lines both survive and a re-pull maps onto the same keys. Rows with a non-c1 key are re-keyed on read.
  Two cards with the same last-4 share files (documented collision; Cal shows only last-4 to the library too).

Coverage: per (card, billing month) {complete, final, capped, rows, last_sync, run_id}. complete = the month came
  back from Cal (statusCode 1) with rows < cap. final = fetched after month end + repull_days (the statement
  can still change before that). A month is COVERED only if it is complete+final for EVERY card Cal last listed
  (cards_seen). Open months (current / next statement) are never covered and always re-fetched.
Merge: a month that is not yet final is replaced by the new snapshot; a final month is a union by key (a later
  re-pull can add, never silently drop). Coverage is saved only after every month file was written.
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
from typing import Any, Optional
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Jerusalem")
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
YM = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
CARD_RE = re.compile(r"^\d{4}$")
MONTH_NAME_RE = re.compile(r"^cal-card-(\d{4})-(\d{4}-\d{2})\.csv$")
PENDING_NAME_RE = re.compile(r"^cal-card-(\d{4})-pending\.csv$")
KEY_PREFIX = "c1:"
DEFAULT_REPULL_DAYS = 10
COVERAGE_NAME = "_coverage.json"
LOCK_NAME = ".lock"
CSV_FIELDS = ["billing_month", "purchase_date", "charge_date", "description", "amount", "currency",
              "original_amount", "original_currency", "installment_number", "installment_total", "type", "status",
              "identifier", "category", "memo", "card", "dedupe_key"]


# ---------------------------------------------------------------------------------------------- dates
def today_il(now: Optional[float] = None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now, TZ).strftime("%Y-%m-%d")


def month_il(now: Optional[float] = None) -> str:
    return today_il(now)[:7]


def add_months(ym: str, n: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7]) - 1 + n
    return f"{y + m // 12:04d}-{m % 12 + 1:02d}"


def month_end(ym: str) -> str:
    nxt = add_months(ym, 1)
    return (date(int(nxt[:4]), int(nxt[5:7]), 1) - timedelta(days=1)).isoformat()


def months_between(a: str, b: str) -> list[str]:
    """Inclusive, oldest first."""
    out, ym = [], a
    while ym <= b and len(out) < 240:
        out.append(ym)
        ym = add_months(ym, 1)
    return out


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


def is_final(ym: str, now: Optional[float], repull_days: int) -> bool:
    end = date.fromisoformat(month_end(ym))
    return date.fromisoformat(today_il(now)) > end + timedelta(days=repull_days)


# ---------------------------------------------------------------------------------------------- rows + keys
def card_suffix(card: Any) -> Optional[str]:
    digits = re.sub(r"\D", "", str(card or ""))
    return digits[-4:] if len(digits) >= 4 else None


def _amount_s(v: Any) -> str:
    if v is None or v == "":
        return ""
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return ""


def _int_s(v: Any) -> str:
    try:
        return str(int(v)) if v not in (None, "") else ""
    except (TypeError, ValueError):
        return ""


def normalize_row(raw: dict) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    card = card_suffix(raw.get("card"))
    if not card:
        return None
    status = str(raw.get("status") or "completed").lower()
    if status not in ("completed", "pending"):
        status = "completed"
    bm = str(raw.get("billing_month") or "")
    if status == "completed" and not YM.fullmatch(bm):
        return None
    amount = _amount_s(raw.get("amount"))
    if amount == "":
        return None
    return {
        "billing_month": bm if status == "completed" else "",
        "purchase_date": day_from_any(raw.get("purchase_date")) or "",
        "charge_date": (day_from_any(raw.get("charge_date")) or "") if status == "completed" else "",
        "description": str(raw.get("description") or "").strip(),
        "amount": amount,
        "currency": str(raw.get("currency") or "ILS").strip() or "ILS",
        "original_amount": _amount_s(raw.get("original_amount")),
        "original_currency": str(raw.get("original_currency") or "").strip(),
        "installment_number": _int_s(raw.get("installment_number")),
        "installment_total": _int_s(raw.get("installment_total")),
        "type": str(raw.get("type") or "").strip(),
        "status": status,
        "identifier": str(raw.get("identifier") or "").strip(),
        "category": str(raw.get("category") or "").strip(),
        "memo": str(raw.get("memo") or "").strip(),
        "card": card,
        "dedupe_key": str(raw.get("dedupe_key") or ""),
    }


def group_fp(r: dict) -> str:
    if r["status"] == "pending":
        parts = [r["card"], "pending", r["purchase_date"], r["amount"], r["currency"], r["description"]]
    else:
        parts = [r["card"], r["billing_month"], r["identifier"], r["purchase_date"], r["amount"],
                 r["original_amount"], r["currency"], r["description"],
                 f"{r['installment_number']}/{r['installment_total']}"]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def key_rows(rows: list[dict]) -> list[dict]:
    """Assign c1 keys (occurrence in input order). Returns new dicts."""
    seen: dict[str, int] = {}
    out = []
    for r in rows:
        fp = group_fp(r)
        seen[fp] = seen.get(fp, 0) + 1
        out.append(dict(r, dedupe_key=f"{KEY_PREFIX}{r['card']}:{fp}:{seen[fp]}"))
    return out


def rows_from_payload(payload: dict) -> list[dict]:
    rows = []
    for raw in (payload or {}).get("rows") or []:
        r = normalize_row(raw)
        if r:
            rows.append(r)
    return rows


# ---------------------------------------------------------------------------------------------- files
def month_path(archive_dir: Path, card: str, ym: str) -> Path:
    return Path(archive_dir) / f"cal-card-{card}-{ym}.csv"


def pending_path(archive_dir: Path, card: str) -> Path:
    return Path(archive_dir) / f"cal-card-{card}-pending.csv"


def read_rows(path: Path) -> list[dict]:
    """Rows of one archive file; rows without a valid c1 key are re-keyed (tolerant reader)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    good, legacy = [], []
    for raw in csv.DictReader(io.StringIO(text)):
        r = normalize_row(raw)
        if not r:
            continue
        if r["dedupe_key"].startswith(f"{KEY_PREFIX}{r['card']}:"):
            good.append(r)
        else:
            legacy.append(r)
    if legacy:
        used = {r["dedupe_key"] for r in good}
        counts: dict[str, int] = {}
        for r in legacy:
            fp = group_fp(r)
            n = counts.get(fp, 0) + 1
            while f"{KEY_PREFIX}{r['card']}:{fp}:{n}" in used:
                n += 1
            counts[fp] = n
            r["dedupe_key"] = f"{KEY_PREFIX}{r['card']}:{fp}:{n}"
            used.add(r["dedupe_key"])
            good.append(r)
    return good


def rows_to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    return buf.getvalue()


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600, uid: Optional[int] = None,
                      gid: Optional[int] = None):
    path = Path(path)
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
        self._fh = open(self.path, "a+")
        os.chmod(self.path, 0o600)
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()


def ensure_archive_dir(archive_dir: Path, *, uid: Optional[int] = None, gid: Optional[int] = None) -> Path:
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(archive_dir, 0o700)
    if uid is not None and os.geteuid() == 0:
        try:
            os.chown(archive_dir, uid, gid if gid is not None else uid)
        except OSError:
            pass
    return archive_dir


def load_coverage(archive_dir: Path) -> dict:
    try:
        data = json.loads((Path(archive_dir) / COVERAGE_NAME).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
    except (OSError, ValueError):
        data = {}
    data.setdefault("version", 1)
    data.setdefault("site", "cal")
    data.setdefault("cards", {})
    data.setdefault("cards_seen", [])
    return data


def save_coverage(archive_dir: Path, coverage: dict, *, uid=None, gid=None):
    atomic_write_text(Path(archive_dir) / COVERAGE_NAME,
                      json.dumps(coverage, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      mode=0o600, uid=uid, gid=gid)


def _month_meta(coverage: dict, card: str, ym: str) -> dict:
    return ((coverage.get("cards") or {}).get(card) or {}).get("months", {}).get(ym) or {}


def month_covered(coverage: dict, ym: str, cards: Optional[list[str]] = None) -> bool:
    cards = cards if cards is not None else list(coverage.get("cards_seen") or [])
    if not cards:
        return False
    return all(_month_meta(coverage, c, ym).get("complete") and _month_meta(coverage, c, ym).get("final")
               for c in cards)


# ---------------------------------------------------------------------------------------------- plan + merge
def plan_gaps(archive_dir: Path, from_month: str, to_month: str, *, now: Optional[float] = None) -> dict:
    """Which billing months still need Cal. fetch_from..fetch_to is the contiguous span of needed months
    (the walk runs newest -> oldest inside it)."""
    if not (YM.fullmatch(from_month) and YM.fullmatch(to_month)) or from_month > to_month:
        raise ValueError("months must be YYYY-MM, from <= to")
    coverage = load_coverage(archive_dir)
    cards = list(coverage.get("cards_seen") or [])
    needed = [m for m in months_between(from_month, to_month) if not month_covered(coverage, m, cards)]
    plan = {"from_month": from_month, "to_month": to_month, "cards": cards, "needed": needed,
            "covered": not needed}
    if needed:
        plan.update(fetch_from=needed[0], fetch_to=needed[-1])
    return plan


def merge_sync(archive_dir: Path, payload: dict, *, run_id: str = "", now: Optional[float] = None,
               repull_days: int = DEFAULT_REPULL_DAYS, uid: Optional[int] = None, gid: Optional[int] = None) -> dict:
    archive_dir = ensure_archive_dir(archive_dir, uid=uid, gid=gid)
    now = time.time() if now is None else now
    rows = rows_from_payload(payload)
    retrieval = payload.get("retrieval") if isinstance(payload.get("retrieval"), dict) else {}
    cards_listed = [c for c in (card_suffix(x) for x in payload.get("cards") or []) if c]
    by_cm: dict[tuple[str, str], list[dict]] = {}
    pending: dict[str, list[dict]] = {}
    for r in rows:
        if r["status"] == "pending":
            pending.setdefault(r["card"], []).append(r)
        else:
            by_cm.setdefault((r["card"], r["billing_month"]), []).append(r)
    out = {"new": 0, "removed": 0, "months": [], "complete_months": [], "pending_rows": 0, "warnings": [],
           "dropped": 0}
    placed: set[tuple[str, str]] = set()
    with ArchiveLock(archive_dir):
        coverage = load_coverage(archive_dir)
        writes: list[tuple[Path, str]] = []
        updates: list[tuple[str, str, dict]] = []
        pending_updates: list[tuple[str, dict]] = []
        for card_raw, ret in retrieval.items():
            card = card_suffix(card_raw)
            if not card or not isinstance(ret, dict):
                continue
            if not ret.get("known"):
                out["warnings"].append(f"…{card}: no retrieval metadata (unpatched image?); nothing merged")
                continue
            for entry in ret.get("months") or []:
                ym = str((entry or {}).get("month") or "")
                if not YM.fullmatch(ym):
                    continue
                placed.add((card, ym))
                capped = bool(entry.get("capped"))
                new_rows = key_rows(by_cm.get((card, ym), []))
                old_meta = _month_meta(coverage, card, ym)
                path = month_path(archive_dir, card, ym)
                old_rows = read_rows(path) if path.exists() else []
                was_final = bool(old_meta.get("final") and old_meta.get("complete"))
                if was_final:
                    keys = {r["dedupe_key"] for r in old_rows}
                    merged = old_rows + [r for r in new_rows if r["dedupe_key"] not in keys]
                else:
                    merged = new_rows
                old_keys = {r["dedupe_key"] for r in old_rows}
                new_keys = {r["dedupe_key"] for r in merged}
                out["new"] += len(new_keys - old_keys)
                out["removed"] += len(old_keys - new_keys)
                merged.sort(key=lambda r: (r["charge_date"], r["purchase_date"], r["dedupe_key"]))
                writes.append((path, rows_to_csv(merged)))
                final = is_final(ym, now, repull_days)
                complete = was_final or not capped
                updates.append((card, ym, {"complete": complete, "final": final and complete, "capped": capped,
                                           "rows": len(merged), "last_sync": now, "run_id": run_id}))
                out["months"].append(ym)
                if complete and final:
                    out["complete_months"].append(f"{card}:{ym}")
                if capped:
                    out["warnings"].append(f"…{card} {ym}: rows reached the cap; not marked complete")
            if ret.get("stopReason"):
                out["warnings"].append(f"…{card}: walk stopped at {ret.get('stopMonth')} ({ret.get('stopReason')})")
            if ret.get("pending") == "ok":
                prow = key_rows(pending.get(card, []))
                writes.append((pending_path(archive_dir, card), rows_to_csv(prow)))
                pending_updates.append((card, {"rows": len(prow), "last_sync": now, "run_id": run_id}))
                out["pending_rows"] += len(prow)
        out["dropped"] = sum(len(v) for k, v in by_cm.items() if k not in placed)
        if out["dropped"]:
            out["warnings"].append(f"{out['dropped']} rows outside the returned months were not merged")
        for path, text in writes:
            atomic_write_text(path, text, mode=0o600, uid=uid, gid=gid)
        for card, ym, meta in updates:
            coverage["cards"].setdefault(card, {}).setdefault("months", {})[ym] = meta
        for card, meta in pending_updates:
            coverage["cards"].setdefault(card, {})["pending"] = meta
        if cards_listed:
            coverage["cards_seen"] = sorted(set(cards_listed))
            coverage["cards_seen_at"] = now
        coverage["last_sync"] = now
        save_coverage(archive_dir, coverage, uid=uid, gid=gid)
    out["months"] = sorted(set(out["months"]))
    return out


# ---------------------------------------------------------------------------------------------- readers
def _cards_for(coverage: dict, card: Optional[str]) -> list[str]:
    if card:
        return [card]
    return list(coverage.get("cards_seen") or sorted(coverage.get("cards") or {}))


def month_state(coverage: dict, ym: str, cards: list[str], now: Optional[float] = None) -> dict:
    cur = month_il(now)
    missing = [c for c in cards if not _month_meta(coverage, c, ym).get("complete")]
    not_final = [c for c in cards if c not in missing and not _month_meta(coverage, c, ym).get("final")]
    return {"month": ym, "open": ym >= cur, "missing_cards": missing, "not_final_cards": not_final,
            "covered": bool(cards) and not missing and not not_final}


def list_archive(archive_dir: Path) -> dict:
    archive_dir = Path(archive_dir)
    coverage = load_coverage(archive_dir)
    files = []
    for p in sorted(archive_dir.glob("cal-card-*.csv")):
        m = MONTH_NAME_RE.match(p.name)
        pm = PENDING_NAME_RE.match(p.name)
        if m:
            meta = _month_meta(coverage, m.group(1), m.group(2))
            files.append({"name": p.name, "card": m.group(1), "month": m.group(2), "rows": meta.get("rows"),
                          "complete": bool(meta.get("complete")), "final": bool(meta.get("final")),
                          "last_sync": meta.get("last_sync")})
        elif pm:
            meta = ((coverage.get("cards") or {}).get(pm.group(1)) or {}).get("pending") or {}
            files.append({"name": p.name, "card": pm.group(1), "month": "pending", "rows": meta.get("rows"),
                          "last_sync": meta.get("last_sync")})
    return {"status": "ok", "files": files, "cards": coverage.get("cards_seen") or []}


def _check_card(card: Optional[str]) -> Optional[str]:
    if card in (None, ""):
        return None
    c = card_suffix(card)
    if not c or not CARD_RE.fullmatch(c):
        raise ValueError("card must be the last 4 digits")
    return c


def read_month(archive_dir: Path, yyyy_mm: str, card: Optional[str] = None, *, include_pending: bool = False,
               now: Optional[float] = None) -> dict:
    if not YM.fullmatch(str(yyyy_mm or "")):
        raise ValueError("month must be YYYY-MM")
    card = _check_card(card)
    archive_dir = Path(archive_dir)
    coverage = load_coverage(archive_dir)
    cards = _cards_for(coverage, card)
    rows, names = [], []
    for c in cards:
        p = month_path(archive_dir, c, yyyy_mm)
        if p.is_file():
            names.append(p.name)
            rows.extend(read_rows(p))
    if include_pending:
        for c in cards:
            p = pending_path(archive_dir, c)
            if p.is_file():
                names.append(p.name)
                rows.extend(read_rows(p))
    rows.sort(key=lambda r: (r["charge_date"] or "9999", r["purchase_date"]))
    st = month_state(coverage, yyyy_mm, cards, now)
    who = f" (כרטיס …{card})" if card else ""
    text = f"{len([r for r in rows if r['status'] == 'completed'])} חיובים בחשבון כאל לחודש החיוב {yyyy_mm}{who}."
    if not cards:
        text += " עדיין אין סנכרון של כאל — אין לומר 'אין חיובים'."
    elif st["missing_cards"]:
        text += (" שים לב: החודש לא סונכרן במלואו (" + ", ".join(f"…{c}" for c in st["missing_cards"]) +
                 ") — ייתכן שחסרים חיובים; אין לומר 'אין חיובים' לחודש הזה.")
    elif st["open"] or st["not_final_cards"]:
        text += " החודש עדיין פתוח — ייתכנו עוד חיובים עד סגירת החשבון."
    return {"status": "ok", "month": yyyy_mm, "card": card, "cards": cards, "files": names, "rows": rows,
            "row_count": len(rows), "complete": st["covered"], "open": st["open"],
            "missing_cards": st["missing_cards"], "text": text,
            "date_semantics": "month = billing (statement) month; purchase_date = original purchase day; "
                              "charge_date = debit day"}


def read_range(archive_dir: Path, from_date: str, to_date: str, card: Optional[str] = None, *,
               by: str = "charge", now: Optional[float] = None) -> dict:
    if not (ISO.fullmatch(str(from_date or "")) and ISO.fullmatch(str(to_date or ""))) or from_date > to_date:
        raise ValueError("dates must be YYYY-MM-DD, from <= to")
    if by not in ("charge", "purchase"):
        raise ValueError("by must be charge or purchase")
    card = _check_card(card)
    archive_dir = Path(archive_dir)
    coverage = load_coverage(archive_dir)
    cards = _cards_for(coverage, card)
    first, last = from_date[:7], to_date[:7]
    if by == "purchase":
        # A purchase is billed in the same or a later statement (regular: usually the next one).
        last = min(add_months(last, 2), add_months(month_il(now), 1))
        last = max(last, first)
    field = "charge_date" if by == "charge" else "purchase_date"
    rows, uncovered = [], []
    for ym in months_between(first, last):
        st = month_state(coverage, ym, cards, now)
        if not st["covered"]:
            uncovered.append(ym)
        for c in cards:
            p = month_path(archive_dir, c, ym)
            if p.is_file():
                rows.extend(r for r in read_rows(p) if from_date <= (r[field] or "") <= to_date)
    rows.sort(key=lambda r: (r[field], r["dedupe_key"]))
    label = "תאריך חיוב" if by == "charge" else "תאריך רכישה"
    text = f"{len(rows)} חיובים בכאל לפי {label} {from_date}..{to_date}" + (f" (כרטיס …{card})" if card else "") + "."
    if not cards:
        text += " עדיין אין סנכרון של כאל — אין לומר 'אין חיובים'."
    elif uncovered:
        text += (" שים לב: חודשי חיוב שלא נסגרו/לא סונכרנו במלואם: " + ", ".join(uncovered) +
                 " — ייתכן שחסרים חיובים.")
    if by == "purchase":
        text += " תשלומים: כל תשלום מופיע בחודש החיוב שלו עם תאריך הרכישה המקורי."
    return {"status": "ok", "from": from_date, "to": to_date, "by": by, "card": card, "cards": cards,
            "months_scanned": months_between(first, last), "rows": rows, "row_count": len(rows),
            "complete": bool(cards) and not uncovered, "uncovered_months": uncovered, "text": text}


def coverage_report(archive_dir: Path, now: Optional[float] = None) -> dict:
    coverage = load_coverage(archive_dir)
    cards = list(coverage.get("cards_seen") or [])
    per = {}
    for c in sorted(set(cards) | set(coverage.get("cards") or {})):
        months = (coverage["cards"].get(c) or {}).get("months") or {}
        per[c] = {"complete_final": sorted(m for m, v in months.items() if v.get("complete") and v.get("final")),
                  "open_or_partial": sorted(m for m, v in months.items() if not (v.get("complete") and v.get("final"))),
                  "pending_rows": ((coverage["cards"].get(c) or {}).get("pending") or {}).get("rows")}
    return {"status": "ok", "cards_seen": cards, "cards": per, "last_sync": coverage.get("last_sync"),
            "current_month": month_il(now)}
