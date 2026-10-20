"""Command Center approvals for the Telegram adapter: one state machine for buttons AND chat replies.

Pure logic (stdlib only) so it is unit-testable without python-telegram-bot or the Hermes gateway.
The Telegram adapter injects two small I/O objects:

* ``cc``  — ``await cc.get(id) -> dict`` and ``await cc.decide(id, "approve"|"deny", via, actor) -> dict``
  (the Command Center HTTP API; it is the single source of truth and claims atomically).
* ``ui``  — ``await ui.edit_prompt(chat_id, message_id, text)`` (edit + remove the keyboard) and
  ``await ui.send_control(chat_id, text, reply_to_message_id)`` (a control reply, never sent to the LLM).

Rules
-----
* First decision wins. A local atomic claim stops a button tap and a chat reply racing inside this
  process; the Command Center claim (``UPDATE … WHERE status='pending'``) is authoritative across
  restarts. A later tap/reply gets "already decided (how/who/when)" and the prompt is re-edited.
* Every decided/expired prompt is edited to show the outcome and its buttons are removed.
* A chat reply is consumed only when it is a *pure* decision ("מאשר", "כן", "לא", "בטל הכל", "#2",
  "approve a3f9" …) AND it unambiguously targets pending prompts in this chat:
    - it replies to a prompt (or to the message that carried it), or
    - it names a short id / number, or
    - the pending prompts are *fresh*: sent after the last user message that went to the LLM.
  With several fresh prompts it needs "הכל"/"all", a number or a reply; otherwise it asks which.
  Anything else goes to the LLM unchanged.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

DEFAULT_TTL_SECONDS = 900
BATCH_THRESHOLD = 5  # more proposals than this in one reply → one grouped prompt

MARKER_RE = re.compile(r"\[\[CC_APPROVAL:([0-9a-f]{12,64})\]\]")


# --------------------------------------------------------------------------------------------- markers

def marker_ids(content: Optional[str]) -> List[str]:
    """Every approval id in ``content`` (anywhere, de-duplicated, in order)."""
    seen: List[str] = []
    for ident in MARKER_RE.findall(content or ""):
        if ident not in seen:
            seen.append(ident)
    return seen


def strip_markers(content: str) -> str:
    """Remove every marker; collapse the whitespace they leave behind."""
    text = MARKER_RE.sub("", content or "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------------------------- parser

APPROVE_PHRASES = {
    "כן", "כן כן", "מאשר", "מאשרת", "מאשרים", "מאושר", "אשר", "תאשר", "תאשרי", "אישור", "אושר", "אשר הכל",
    "כן מאשר", "כן מאשרת", "אוקיי", "אוקי", "אוקיי מאשר", "סבבה", "בסדר", "יאללה", "קדימה", "תבצע", "בצע",
    "לך על זה", "סגור", "בטח", "ברור",
    "yes", "y", "yep", "yeah", "yup", "ok", "okay", "k", "sure", "approve", "approved", "confirm",
    "confirmed", "go", "go ahead", "do it", "yes please", "ok go",
}
DENY_PHRASES = {
    "לא", "לא לא", "לא מאשר", "לא מאשרת", "לא צריך", "לא תודה", "בטל", "תבטל", "תבטלי", "ביטול", "בטלי",
    "דחה", "תדחה", "דחייה", "דחיה", "נדחה", "אל תבצע", "אל תעשה", "לא לבצע", "עזוב", "עזבי", "עזוב את זה",
    "no", "n", "nope", "nah", "deny", "denied", "cancel", "reject", "stop", "dont", "do not", "no thanks",
}
ALL_WORDS = {"הכל", "הכול", "כולם", "כולן", "שניהם", "שתיהן", "שלושתם", "all", "both", "everything", "them"}
FILLER_WORDS = {"את", "זה", "זאת", "אותו", "אותה", "אותם", "בבקשה", "תודה", "please", "it", "this", "that",
                "the", "פשוט", "רק", "מספר", "number", "no.", "id", "קוד"}
EMOJI_MAP = {"✅": " approve ", "👍": " approve ", "👌": " approve ", "✔": " approve ", "☑": " approve ",
             "❌": " deny ", "👎": " deny ", "🚫": " deny ", "✖": " deny ", "⛔": " deny "}
MAX_WORDS = 7


@dataclass
class Decision:
    decision: str                      # "approve" | "deny"
    all: bool = False
    index: Optional[int] = None        # 1-based "#2"
    short_id: Optional[str] = None     # hex prefix (>= 4 chars)


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    for emo, word in EMOJI_MAP.items():
        text = text.replace(emo, word)
    # Drop Hebrew niqqud/cantillation and variation selectors.
    text = "".join(ch for ch in text if not unicodedata.combining(ch) and ch not in "\ufe0f\u200d\u200f\u200e")
    text = text.lower().replace("’", "'").replace("׳", "'").replace("״", '"')
    text = text.replace("don't", "dont").replace("do n't", "dont")
    text = re.sub(r"[!?.,;:\"'()\[\]{}…~\-–—_*]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _match_phrase(words: List[str]) -> Optional[tuple]:
    """Longest decision phrase at the start or end of ``words`` → (decision, remaining words)."""
    for size in range(min(4, len(words)), 0, -1):
        for start in (0, len(words) - size):
            phrase = " ".join(words[start:start + size])
            rest = words[:start] + words[start + size:]
            # Negated forms first: "לא מאשר" must never be read as approve.
            if phrase in DENY_PHRASES:
                return "deny", rest
            if phrase in APPROVE_PHRASES:
                return "approve", rest
    return None


def parse_decision(text: str) -> Optional[Decision]:
    """Parse a *pure* approve/deny reply, or ``None`` if the message says anything else."""
    norm = _normalize(text)
    if not norm:
        return None
    words = norm.split(" ")
    if len(words) > MAX_WORDS:
        return None
    # Repeated emoji/words ("✅✅", "כן כן כן") collapse.
    deduped: List[str] = []
    for w in words:
        if not deduped or deduped[-1] != w:
            deduped.append(w)
    matched = _match_phrase(deduped)
    if matched is None:
        return None
    decision, rest = matched
    result = Decision(decision)
    for word in rest:
        if word in FILLER_WORDS:
            continue
        if word in ALL_WORDS:
            result.all = True
            continue
        m = re.fullmatch(r"#?(\d{1,3})", word)
        if m and result.index is None:
            result.index = int(m.group(1))
            continue
        m = re.fullmatch(r"#?([0-9a-f]{4,18})", word)
        if m and result.short_id is None and re.search(r"[a-f]", m.group(1)):
            result.short_id = m.group(1)
            continue
        if word in APPROVE_PHRASES or word in DENY_PHRASES:
            other = "deny" if word in DENY_PHRASES else "approve"
            if other != decision:
                return None  # "כן לא" — contradictory
            continue
        return None  # any other content → this is a real message for the LLM
    return result


# --------------------------------------------------------------------------------------------- registry

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cc_prompts (
  approval_id TEXT PRIMARY KEY,
  chat_id TEXT NOT NULL,
  prompt_message_id TEXT,
  carrier_message_id TEXT,
  batch_id TEXT,
  label INTEGER,
  summary TEXT NOT NULL DEFAULT '',
  seq INTEGER NOT NULL,
  expires_at REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  outcome TEXT, decided_via TEXT, decided_by TEXT, decided_at REAL, detail TEXT
);
CREATE INDEX IF NOT EXISTS cc_prompts_chat ON cc_prompts(chat_id, status);
CREATE TABLE IF NOT EXISTS cc_chat_state (chat_id TEXT PRIMARY KEY, last_user_turn_seq INTEGER NOT NULL);
"""


class Registry:
    """Which prompt lives where. Thread-safe; ``path=':memory:'`` for tests."""

    def __init__(self, path: str = ":memory:", clock=time.time):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._lock = threading.Lock()
        self._clock = clock
        self._seq = 0

    def next_seq(self) -> int:
        with self._lock:
            self._seq = max(self._seq + 1, time.time_ns())
            return self._seq

    def has(self, approval_id: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM cc_prompts WHERE approval_id=?", (approval_id,)).fetchone() is not None

    def add(self, approval_id, chat_id, prompt_message_id, carrier_message_id, summary, expires_at,
            batch_id=None, label=None) -> None:
        seq = self.next_seq()
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO cc_prompts (approval_id,chat_id,prompt_message_id,carrier_message_id,batch_id,"
                "label,summary,seq,expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (approval_id, str(chat_id), _s(prompt_message_id), _s(carrier_message_id), batch_id, label,
                 summary or "", seq, float(expires_at)))

    def get(self, approval_id) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM cc_prompts WHERE approval_id=?", (approval_id,)).fetchone()
        return dict(row) if row else None

    def batch(self, batch_id) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM cc_prompts WHERE batch_id=? ORDER BY seq", (batch_id,)).fetchall()
        return [dict(r) for r in rows]

    def pending(self, chat_id) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM cc_prompts WHERE chat_id=? AND status='pending' ORDER BY seq", (str(chat_id),)).fetchall()
        return [dict(r) for r in rows]

    def by_message(self, chat_id, message_id) -> List[Dict[str, Any]]:
        mid = _s(message_id)
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM cc_prompts WHERE chat_id=? AND (prompt_message_id=? OR carrier_message_id=?) ORDER BY seq",
                (str(chat_id), mid, mid)).fetchall()
        return [dict(r) for r in rows]

    def due(self, now: float, chat_id=None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM cc_prompts WHERE status='pending' AND expires_at<=?"
        args: list = [now]
        if chat_id is not None:
            sql += " AND chat_id=?"
            args.append(str(chat_id))
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY seq", args).fetchall()
        return [dict(r) for r in rows]

    def claim(self, approval_id) -> bool:
        """pending → deciding, atomically. False if someone else already claimed/decided it."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE cc_prompts SET status='deciding' WHERE approval_id=? AND status='pending'", (approval_id,))
            return cur.rowcount == 1

    def release(self, approval_id) -> None:
        with self._lock:
            self._db.execute("UPDATE cc_prompts SET status='pending' WHERE approval_id=? AND status='deciding'",
                             (approval_id,))

    def finish(self, approval_id, status, outcome=None, via=None, by=None, at=None, detail=None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE cc_prompts SET status=?,outcome=?,decided_via=?,decided_by=?,decided_at=?,detail=? "
                "WHERE approval_id=?", (status, outcome, via, by, at, detail, approval_id))

    def expire(self, approval_id) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE cc_prompts SET status='expired',outcome='expired' WHERE approval_id=? AND status='pending'",
                (approval_id,))
            return cur.rowcount == 1

    def note_user_turn(self, chat_id) -> None:
        seq = self.next_seq()
        with self._lock:
            self._db.execute(
                "INSERT INTO cc_chat_state (chat_id,last_user_turn_seq) VALUES (?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET last_user_turn_seq=excluded.last_user_turn_seq", (str(chat_id), seq))

    def last_user_turn(self, chat_id) -> int:
        with self._lock:
            row = self._db.execute("SELECT last_user_turn_seq FROM cc_chat_state WHERE chat_id=?",
                                   (str(chat_id),)).fetchone()
        return int(row[0]) if row else 0


def _s(value) -> Optional[str]:
    return None if value is None else str(value)


# --------------------------------------------------------------------------------------------- text

STATUS_TEXT = {
    "executed": "✅ אושר ובוצע",
    "denied": "❌ נדחה",
    "expired": "⌛ פג תוקף — לא בוצע",
    "failed": "⚠️ אושר, אבל הביצוע נכשל",
    "unknown": "⚠️ הבקשה לא נמצאה",
}
VIA_TEXT = {"button": "בכפתור", "chat": "בצ׳אט"}


def _clock_text(ts: Optional[float]) -> str:
    if not ts:
        return ""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(float(ts), ZoneInfo("Asia/Jerusalem")).strftime("%H:%M")
    except Exception:  # no tzdata in the image → the IL offset beats UTC
        return time.strftime("%H:%M", time.gmtime(float(ts) + 3 * 3600))


def short_id(approval_id: str) -> str:
    return approval_id[:4]


def prompt_text(summary: str, approval_id: str, label: Optional[int] = None) -> str:
    head = f"⚠️ ממתין לאישור #{label}" if label else "⚠️ ממתין לאישור"
    body = f"{head} · {summary}" if summary else head
    return f"{body}\n(קוד {short_id(approval_id)} · לחץ, או ענה ״מאשר״ / ״לא״)"


def batch_prompt_text(rows_or_summaries: List[str], batch_id: str) -> str:
    lines = [f"⚠️ ממתינות לאישור {len(rows_or_summaries)} פעולות:"]
    shown = rows_or_summaries[:8]
    lines += [f"{i}. {s}" for i, s in enumerate(shown, 1)]
    if len(rows_or_summaries) > len(shown):
        lines.append(f"… ועוד {len(rows_or_summaries) - len(shown)}")
    lines.append("(כפתור אחד לכולן, או ענה ״מאשר הכל״ / ״בטל הכל״)")
    return "\n".join(lines)


def decided_text(summary: str, status: str, via: Optional[str], by: Optional[str], at: Optional[float],
                 detail: Optional[str] = None) -> str:
    head = STATUS_TEXT.get(status, "⚠️ כבר לא ממתין")
    first = f"{head} · {summary}" if summary else head
    who = " · ".join(x for x in (by or "", VIA_TEXT.get(via or "", via or ""), _clock_text(at)) if x)
    out = first + (f"\n{who}" if who else "")
    if detail and status == "failed":
        out += f"\n{detail[:300]}"
    return out


# --------------------------------------------------------------------------------------------- controller

@dataclass
class TextResult:
    handled: bool
    reply: Optional[str] = None
    decided: List[str] = field(default_factory=list)


def _cc_status(result: Dict[str, Any]) -> str:
    status = str(result.get("status") or "")
    return status if status in STATUS_TEXT else ("failed" if status == "failed" else "unknown")


class ApprovalController:
    def __init__(self, registry: Registry, cc, ui, clock=time.time, ttl: int = DEFAULT_TTL_SECONDS):
        self.registry, self.cc, self.ui, self.clock, self.ttl = registry, cc, ui, clock, ttl

    # ---- outbound
    async def summary_for(self, approval_id: str) -> Dict[str, Any]:
        try:
            info = await self.cc.get(approval_id)
            return info if isinstance(info, dict) else {}
        except Exception:
            return {}

    def wanted(self, ids: List[str]) -> List[str]:
        """Ids that still need a prompt (a re-sent/re-edited reply must not duplicate controls)."""
        return [i for i in ids if not self.registry.has(i)]

    def plan(self, ids: List[str]) -> str:
        return "batch" if len(ids) > BATCH_THRESHOLD else "single"

    def register(self, chat_id, approval_id, prompt_message_id, carrier_message_id, info: Dict[str, Any],
                 batch_id=None, label=None) -> None:
        expires = float(info.get("expires_at") or (self.clock() + self.ttl))
        self.registry.add(approval_id, chat_id, prompt_message_id, carrier_message_id,
                          str(info.get("summary") or ""), expires, batch_id=batch_id, label=label)

    # ---- decisions
    async def _decide_one(self, approval_id: str, decision: str, via: str, actor: str) -> Dict[str, Any]:
        """Claim locally, decide in Command Center, record and return the final local row."""
        row = self.registry.get(approval_id)
        if row is None:
            return {"approval_id": approval_id, "status": "unknown"}
        if not self.registry.claim(approval_id):
            return dict(self.registry.get(approval_id) or row, already=True)
        try:
            result = await self.cc.decide(approval_id, decision, via, actor)
        except Exception as exc:  # network: the decision did not happen; leave it pending for a retry
            self.registry.release(approval_id)
            return dict(row, status="pending", error=str(exc)[:200])
        status = _cc_status(result)
        decided = result.get("decided") or {}
        already = bool(result.get("already_decided"))
        self.registry.finish(
            approval_id, status if status != "unknown" else "unknown", outcome=status,
            via=decided.get("via") or via, by=decided.get("actor") or actor,
            at=decided.get("decided_at") or self.clock(), detail=result.get("error"))
        final = self.registry.get(approval_id) or row
        return dict(final, already=already)

    async def _render(self, row: Dict[str, Any]) -> None:
        if not row.get("prompt_message_id"):
            return
        if row.get("batch_id"):
            return await self._render_batch(row["batch_id"])
        await self.ui.edit_prompt(row["chat_id"], row["prompt_message_id"], decided_text(
            row.get("summary") or "", row.get("status") or "", row.get("decided_via"), row.get("decided_by"),
            row.get("decided_at"), row.get("detail")))

    async def _render_batch(self, batch_id: str) -> None:
        rows = self.registry.batch(batch_id)
        if not rows or any(r["status"] in ("pending", "deciding") for r in rows):
            return
        counts: Dict[str, int] = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        first = rows[0]
        parts = [f"{STATUS_TEXT.get(k, k)}: {v}" for k, v in counts.items()]
        last = max(rows, key=lambda r: r.get("decided_at") or 0)
        who = " · ".join(x for x in (last.get("decided_by") or "", VIA_TEXT.get(last.get("decided_via") or "", ""),
                                     _clock_text(last.get("decided_at"))) if x)
        await self.ui.edit_prompt(first["chat_id"], first["prompt_message_id"],
                                  f"{len(rows)} פעולות — " + " | ".join(parts) + (f"\n{who}" if who else ""))

    async def on_button(self, approval_id: str, decision: str, actor: str,
                        chat_id=None, message_id=None) -> str:
        """A tap on ``cc:a|d:<id>``. Returns the toast text."""
        await self.expire_due()
        row = self.registry.get(approval_id)
        if row is None:
            # Prompt sent before this registry existed (old buttons after a deploy): still decide via
            # Command Center and still edit the tapped message, so legacy buttons get cleaned up too.
            info = await self.summary_for(approval_id)
            self.registry.add(approval_id, str(chat_id or "?"), message_id, None,
                              str(info.get("summary") or ""), float(info.get("expires_at") or self.clock() + self.ttl))
            row = self.registry.get(approval_id)
        if row["status"] == "expired":
            await self._render(row)
            return STATUS_TEXT["expired"]
        final = await self._decide_one(approval_id, decision, "button", actor)
        if final.get("status") == "pending":
            return "לא הצלחתי להגיע ל-Command Center. נסה שוב."
        await self._render(final)
        label = STATUS_TEXT.get(final.get("status"), "⚠️ כבר לא ממתין")
        return f"כבר הוחלט: {label}" if final.get("already") else label

    async def on_batch_button(self, batch_id: str, decision: str, actor: str) -> str:
        await self.expire_due()
        rows = self.registry.batch(batch_id)
        if not rows:
            return STATUS_TEXT["unknown"]
        results = [await self._decide_one(r["approval_id"], decision, "button", actor)
                   for r in rows if r["status"] == "pending"]
        await self._render_batch(batch_id)
        if not results:
            return "כבר הוחלט"
        if any(r.get("status") == "pending" for r in results):
            return "חלק לא הגיעו ל-Command Center. נסה שוב."
        return STATUS_TEXT["executed" if decision == "approve" else "denied"]

    async def on_text(self, chat_id, text: str, reply_to_message_id, actor: str) -> TextResult:
        """A plain chat message. ``handled=False`` → forward it to the LLM unchanged."""
        chat_id = str(chat_id)
        parsed = parse_decision(text or "")
        if parsed is None:
            self.registry.note_user_turn(chat_id)
            return TextResult(False)
        await self.expire_due(chat_id)

        targets: List[Dict[str, Any]] = []
        if reply_to_message_id is not None:
            replied = self.registry.by_message(chat_id, reply_to_message_id)
            if replied:
                pending = [r for r in replied if r["status"] == "pending"]
                if not pending:
                    last = replied[-1]
                    await self._render(last)
                    note = decided_text(last.get("summary") or "", last["status"], last.get("decided_via"),
                                        last.get("decided_by"), last.get("decided_at"))
                    await self.ui.send_control(chat_id, f"כבר לא ממתין:\n{note}", reply_to_message_id)
                    return TextResult(True, reply=note)
                targets = pending if (parsed.all or len(pending) == 1 or pending[0].get("batch_id")) else \
                    self._pick(pending, parsed)
                if not targets:
                    return await self._ask_which(chat_id, pending, reply_to_message_id)
        if not targets:
            all_pending = self.registry.pending(chat_id)
            if not all_pending:
                self.registry.note_user_turn(chat_id)
                return TextResult(False)
            if parsed.short_id:
                targets = [r for r in all_pending if r["approval_id"].startswith(parsed.short_id)]
                if len(targets) != 1:
                    return await self._ask_which(chat_id, all_pending, None)
            else:
                fresh = [r for r in all_pending if r["seq"] > self.registry.last_user_turn(chat_id)]
                if not fresh:
                    # The conversation moved on since the prompt: a bare "כן" is for the LLM, not an old prompt.
                    self.registry.note_user_turn(chat_id)
                    return TextResult(False)
                batches = {r.get("batch_id") for r in fresh}
                if parsed.index is not None:
                    targets = self._pick(fresh, parsed)
                    if not targets:
                        return await self._ask_which(chat_id, fresh, None)
                elif len(fresh) == 1 or parsed.all or (len(batches) == 1 and None not in batches):
                    targets = fresh
                else:
                    return await self._ask_which(chat_id, fresh, None)

        decided, lines = [], []
        for row in targets:
            final = await self._decide_one(row["approval_id"], parsed.decision, "chat", actor)
            if final.get("status") == "pending":
                lines.append(f"לא הצלחתי להגיע ל-Command Center ({short_id(row['approval_id'])}). נסה שוב.")
                continue
            decided.append(row["approval_id"])
            if not final.get("batch_id"):
                await self._render(final)
            lines.append(("כבר הוחלט קודם: " if final.get("already") else "") + decided_text(
                final.get("summary") or "", final.get("status") or "", final.get("decided_via"),
                final.get("decided_by"), final.get("decided_at"), final.get("detail")).split("\n")[0])
        for b in {r.get("batch_id") for r in targets if r.get("batch_id")}:
            await self._render_batch(b)
        reply = "\n".join(lines[:10]) + (f"\n… ועוד {len(lines) - 10}" if len(lines) > 10 else "")
        anchor = targets[0].get("prompt_message_id")
        await self.ui.send_control(chat_id, reply, anchor)
        return TextResult(True, reply=reply, decided=decided)

    @staticmethod
    def _pick(rows: List[Dict[str, Any]], parsed: Decision) -> List[Dict[str, Any]]:
        if parsed.short_id:
            return [r for r in rows if r["approval_id"].startswith(parsed.short_id)][:1]
        if parsed.index is not None:
            labelled = [r for r in rows if r.get("label") == parsed.index]
            if labelled:
                return labelled[:1]
            if 1 <= parsed.index <= len(rows):
                return [rows[parsed.index - 1]]
        return []

    async def _ask_which(self, chat_id, rows, reply_to) -> TextResult:
        listing = "\n".join(
            f"{('#' + str(r['label'])) if r.get('label') else short_id(r['approval_id'])} · {r.get('summary') or ''}"
            for r in rows[:8])
        text = ("יש כמה בקשות שממתינות לאישור. איזו?\n" + listing +
                "\nענה בתגובה (reply) להודעה, או כתוב למשל ״מאשר 2״ / ״בטל הכל״.")
        await self.ui.send_control(str(chat_id), text, reply_to)
        return TextResult(True, reply=text)

    async def expire_due(self, chat_id=None) -> List[str]:
        """Edit the buttons away from every prompt whose approval window has passed."""
        expired = []
        for row in self.registry.due(self.clock(), chat_id):
            if self.registry.expire(row["approval_id"]):
                expired.append(row["approval_id"])
                row = self.registry.get(row["approval_id"]) or row
                if row.get("batch_id"):
                    await self._render_batch(row["batch_id"])
                else:
                    await self._render(row)
        return expired
