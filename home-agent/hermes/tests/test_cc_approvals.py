"""Chat-reply parser, idempotent button/chat state machine, multi-pending and stale prompts."""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cc_approvals as ca  # noqa: E402


class FakeCC:
    """Command Center: first decision wins, later ones get the recorded outcome."""

    def __init__(self, clock):
        self.clock, self.rows, self.calls, self.down = clock, {}, [], False

    def add(self, ident, summary="פעולה", ttl=900):
        self.rows[ident] = {"status": "pending", "summary": summary, "expires_at": self.clock() + ttl, "decided": None}

    async def get(self, ident):
        row = self.rows.get(ident)
        return {"approval_id": ident, "status": "unknown"} if row is None else dict(row, approval_id=ident)

    async def decide(self, ident, decision, via, actor):
        if self.down:
            raise OSError("connection refused")
        self.calls.append((ident, decision, via, actor))
        row = self.rows.get(ident)
        if row is None:
            return {"approval_id": ident, "status": "unknown"}
        if row["status"] == "pending" and self.clock() >= row["expires_at"]:
            row["status"] = "expired"
        if row["status"] != "pending":
            return dict(row, approval_id=ident, already_decided=row["decided"] is not None)
        row["status"] = "executed" if decision == "approve" else "denied"
        row["decided"] = {"decision": decision, "via": via, "actor": actor, "decided_at": self.clock()}
        return dict(row, approval_id=ident, already_decided=False)


class FakeUI:
    def __init__(self):
        self.edits, self.sent = [], []

    async def edit_prompt(self, chat_id, message_id, text):
        self.edits.append((str(chat_id), str(message_id), text))

    async def send_control(self, chat_id, text, reply_to):
        self.sent.append((str(chat_id), text, reply_to))


def run(coro):
    return asyncio.run(coro)


class ParserTests(unittest.TestCase):
    def d(self, text):
        p = ca.parse_decision(text)
        return None if p is None else p.decision

    def test_hebrew_and_english_approvals(self):
        for text in ("מאשר", "כן", "כן!", "מאשרת", "אוקיי", "סבבה", "יאללה", "כן כן", "כן מאשר", "מאשר בבקשה",
                     "תאשר את זה", "approve", "Yes", "OK", "ok 👍", "✅", "👍", "go ahead", "מָאשֵׁר"):
            self.assertEqual(self.d(text), "approve", text)

    def test_denials_including_negated_approve(self):
        for text in ("לא", "לא לא", "לא מאשר", "בטל", "תבטל", "ביטול", "לא צריך", "עזוב", "no", "Cancel",
                     "deny", "don't", "❌", "👎", "אל תבצע"):
            self.assertEqual(self.d(text), "deny", text)

    def test_real_messages_are_not_decisions(self):
        for text in ("", "כן אבל תזיז לשעה 9", "לא, תוסיף גם את פלוני", "תבטל את הפגישה עם אלמוני מחר",
                     "מה זה אומר?", "תקבע לי פגישה", "כן לא", "אני רוצה להמשיך ולהוריד את הפרקים הבאים",
                     "yes and also email Alice"):
            self.assertIsNone(ca.parse_decision(text), text)

    def test_targets(self):
        self.assertTrue(ca.parse_decision("מאשר הכל").all)
        self.assertTrue(ca.parse_decision("בטל את כולם").all)
        self.assertTrue(ca.parse_decision("approve all").all)
        self.assertEqual(ca.parse_decision("מאשר 2").index, 2)
        self.assertEqual(ca.parse_decision("#3 כן").index, 3)
        self.assertEqual(ca.parse_decision("approve a3f9").short_id, "a3f9")
        self.assertEqual(ca.parse_decision("בטל קוד 0b1e").short_id, "0b1e")

    def test_markers_anywhere_and_all_stripped(self):
        content = "הכנתי 2 אירועים.\n\n[[CC_APPROVAL:aaaaaaaaaaaaaaaaaa]] [[CC_APPROVAL:bbbbbbbbbbbbbbbbbb]]\n" \
                  "[[CC_APPROVAL:aaaaaaaaaaaaaaaaaa]]"
        self.assertEqual(ca.marker_ids(content), ["a" * 18, "b" * 18])
        self.assertEqual(ca.strip_markers(content), "הכנתי 2 אירועים.")
        self.assertEqual(ca.marker_ids("no marker"), [])


class Harness:
    def __init__(self):
        self.now = 1_000_000.0
        self.cc = FakeCC(lambda: self.now)
        self.ui = FakeUI()
        self.reg = ca.Registry(":memory:")
        self.ctl = ca.ApprovalController(self.reg, self.cc, self.ui, clock=lambda: self.now)

    def prompt(self, ident, chat="1", msg="100", carrier="99", summary="אתחול jellyfin", label=None, batch=None):
        self.cc.add(ident, summary)
        info = run(self.ctl.summary_for(ident))
        self.ctl.register(chat, ident, msg, carrier, info, batch_id=batch, label=label)

    def text(self, text, chat="1", reply_to=None, actor="Ben"):
        return run(self.ctl.on_text(chat, text, reply_to, actor))


A, B, C = "a1" * 9, "b2" * 9, "c3" * 9


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def test_chat_reply_approves_the_single_fresh_prompt_and_removes_buttons(self):
        self.h.prompt(A)
        res = self.h.text("מאשר")
        self.assertTrue(res.handled)
        self.assertEqual(self.h.cc.calls, [(A, "approve", "chat", "Ben")])
        chat, msg, text = self.h.ui.edits[-1]
        self.assertEqual(msg, "100")
        self.assertIn("✅ אושר ובוצע", text)
        self.assertIn("Ben", text)
        self.assertIn("בצ׳אט", text)

    def test_button_after_chat_reply_says_already_decided_and_never_redecides(self):
        self.h.prompt(A)
        self.h.text("כן")
        toast = run(self.h.ctl.on_button(A, "approve", "Ben"))
        self.assertIn("כבר הוחלט", toast)
        self.assertEqual(len(self.h.cc.calls), 1)  # local claim short-circuits; CC is never asked twice
        self.assertIn("✅", self.h.ui.edits[-1][2])

    def test_chat_reply_after_button_reports_already_decided(self):
        self.h.prompt(A)
        self.assertEqual(run(self.h.ctl.on_button(A, "deny", "Ben")), "❌ נדחה")
        res = self.h.text("מאשר", reply_to="100")
        self.assertTrue(res.handled)
        self.assertIn("כבר לא ממתין", self.h.ui.sent[-1][1])
        self.assertEqual(self.h.cc.calls, [(A, "deny", "button", "Ben")])

    def test_button_after_restart_uses_cc_as_source_of_truth(self):
        self.h.prompt(A)
        self.h.text("מאשר")
        fresh = Harness()  # new process: empty registry, same Command Center
        fresh.cc = self.h.cc
        fresh.ctl = ca.ApprovalController(fresh.reg, fresh.cc, fresh.ui, clock=lambda: self.h.now)
        toast = run(fresh.ctl.on_button(A, "approve", "Ben", chat_id="1", message_id="100"))
        self.assertIn("כבר הוחלט", toast)
        self.assertEqual(fresh.ui.edits[-1][1], "100")  # legacy button still gets cleaned up
        self.assertIn("בצ׳אט", fresh.ui.edits[-1][2])

    def test_simultaneous_button_and_chat_execute_once(self):
        self.h.prompt(A)

        async def both():
            return await asyncio.gather(self.h.ctl.on_button(A, "approve", "Ben"),
                                        self.h.ctl.on_text("1", "מאשר", None, "Ben"))
        run(both())
        self.assertEqual(len(self.h.cc.calls), 1)

    def test_non_decision_text_goes_to_llm(self):
        self.h.prompt(A)
        res = self.h.text("כן אבל רק מחר")
        self.assertFalse(res.handled)
        self.assertEqual(self.h.cc.calls, [])

    def test_no_pending_means_passthrough(self):
        self.assertFalse(self.h.text("כן").handled)

    def test_stale_prompt_after_conversation_moved_on_requires_reply(self):
        self.h.prompt(A)
        self.assertFalse(self.h.text("מה מזג האוויר מחר?").handled)  # user talks to the LLM
        self.assertFalse(self.h.text("כן").handled)                  # "כן" now answers the LLM, not A
        self.assertEqual(self.h.cc.calls, [])
        self.assertTrue(self.h.text("מאשר", reply_to="100").handled)  # explicit reply still works
        self.assertEqual(self.h.cc.calls[-1][0], A)

    def test_reply_to_the_carrier_text_message_also_targets(self):
        self.h.prompt(A)
        self.h.text("משהו אחר לגמרי")
        self.assertTrue(self.h.text("לא", reply_to="99").handled)
        self.assertEqual(self.h.cc.calls, [(A, "deny", "chat", "Ben")])

    def test_multiple_fresh_prompts_ask_which(self):
        self.h.prompt(A, msg="100", label=1, summary="אירוע א")
        self.h.prompt(B, msg="101", label=2, summary="אירוע ב")
        res = self.h.text("מאשר")
        self.assertTrue(res.handled)
        self.assertEqual(self.h.cc.calls, [])
        self.assertIn("איזו", self.h.ui.sent[-1][1])
        self.assertIn("#2", self.h.ui.sent[-1][1])
        # the clarification is not a user turn → prompts stay fresh
        self.h.text("מאשר 2")
        self.assertEqual(self.h.cc.calls, [(B, "approve", "chat", "Ben")])
        self.h.text("בטל", reply_to="100")
        self.assertEqual(self.h.cc.calls[-1], (A, "deny", "chat", "Ben"))

    def test_all_and_short_id(self):
        self.h.prompt(A, msg="100", label=1)
        self.h.prompt(B, msg="101", label=2)
        self.h.prompt(C, msg="102", label=3)
        self.h.text(f"בטל {C[:4]}")
        self.assertEqual(self.h.cc.calls, [(C, "deny", "chat", "Ben")])
        self.h.text("מאשר הכל")
        self.assertEqual({c[0] for c in self.h.cc.calls[1:]}, {A, B})
        self.assertEqual(len(self.h.ui.edits), 3)

    def test_other_chat_is_isolated(self):
        self.h.prompt(A, chat="1")
        self.assertFalse(self.h.text("מאשר", chat="2").handled)
        self.assertEqual(self.h.cc.calls, [])

    def test_batch_prompt_one_decision_for_all(self):
        ids = [f"{i:02x}" * 9 for i in range(1, 8)]
        for ident in ids:
            self.h.prompt(ident, msg="200", batch="bt1")
        toast = run(self.h.ctl.on_batch_button("bt1", "deny", "Ben"))
        self.assertEqual(toast, "❌ נדחה")
        self.assertEqual(len(self.h.cc.calls), 7)
        self.assertEqual(len(self.h.ui.edits), 1)
        self.assertIn("7 פעולות", self.h.ui.edits[0][2])
        self.assertEqual(run(self.h.ctl.on_batch_button("bt1", "approve", "Ben")), "כבר הוחלט")
        self.assertEqual(len(self.h.cc.calls), 7)

    def test_batch_by_chat_reply(self):
        ids = [f"{i:02x}" * 9 for i in range(1, 8)]
        for ident in ids:
            self.h.prompt(ident, msg="200", batch="bt1")
        self.assertTrue(self.h.text("לא לא").handled)  # one fresh batch → no "which?" question
        self.assertEqual(len(self.h.cc.calls), 7)
        self.assertIn("7 פעולות", self.h.ui.edits[-1][2])

    def test_expiry_edits_buttons_away_and_late_reply_is_not_executed(self):
        self.h.prompt(A)
        self.h.now += 901
        self.assertEqual(run(self.h.ctl.expire_due()), [A])
        self.assertIn("⌛", self.h.ui.edits[-1][2])
        self.assertFalse(self.h.text("מאשר").handled)  # nothing pending anymore → LLM
        self.assertEqual(run(self.h.ctl.on_button(A, "approve", "Ben")), ca.STATUS_TEXT["expired"])
        self.assertEqual(self.h.cc.calls, [])

    def test_cc_unreachable_keeps_prompt_pending_for_retry(self):
        self.h.prompt(A)
        self.h.cc.down = True
        self.assertIn("נסה שוב", run(self.h.ctl.on_button(A, "approve", "Ben")))
        self.assertEqual(self.h.reg.get(A)["status"], "pending")
        self.h.cc.down = False
        self.assertEqual(run(self.h.ctl.on_button(A, "approve", "Ben")), "✅ אושר ובוצע")

    def test_cc_side_expiry_is_shown(self):
        self.h.prompt(A)
        self.h.cc.rows[A]["status"] = "expired"
        self.assertEqual(run(self.h.ctl.on_button(A, "approve", "Ben")), "⌛ פג תוקף — לא בוצע")

    def test_wanted_dedupes_prompts(self):
        self.h.prompt(A)
        self.assertEqual(self.h.ctl.wanted([A, B]), [B])
        self.assertEqual(self.h.ctl.plan([A] * 6), "batch")
        self.assertEqual(self.h.ctl.plan([A] * 5), "single")

    def test_prompt_texts(self):
        self.assertIn("#2", ca.prompt_text("אתחול x", A, 2))
        self.assertIn(A[:4], ca.prompt_text("אתחול x", A))
        text = ca.batch_prompt_text([f"אירוע {i}" for i in range(12)], "bt")
        self.assertIn("12", text)
        self.assertIn("ועוד 4", text)
        self.assertIn("09:", ca.decided_text("x", "executed", "chat", "Ben", 1760940000))  # 2025-10-20 09:00 IL


if __name__ == "__main__":
    unittest.main()
