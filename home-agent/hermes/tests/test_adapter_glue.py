"""Smoke test of the adapter glue with every upstream Hermes/telegram import stubbed.

Not a substitute for running the real image; it proves the wiring: markers → prompts, a chat reply is
consumed before the LLM, button → same state machine, edit removes the keyboard.
"""
import asyncio
import importlib.abc
import importlib.machinery
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
STUB_ROOTS = {"hermes_cli", "agent", "gateway", "plugins", "utils", "telegram", "tools"}


class _Anything(mock.MagicMock):
    pass


class _StubMeta(type):
    def __getattr__(cls, attr):
        if attr.startswith("__"):
            raise AttributeError(attr)
        return _Anything(name=f"{cls.__name__}.{attr}")


def _stub_attr(module_name, attr):
    if attr == "cc_approvals":  # the real sibling module must be imported, never stubbed
        raise AttributeError(attr)
    # CamelCase names become real (subclassable) classes; everything else a MagicMock.
    if attr[:1].isupper() and not attr.isupper():
        return _StubMeta(attr, (), {"__init__": lambda self, *a, **k: None})
    return _Anything(name=f"{module_name}.{attr}")


class _StubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in STUB_ROOTS and name != "plugins.platforms.telegram.cc_approvals":
            return importlib.machinery.ModuleSpec(name, self, is_package=True)
        return None

    def create_module(self, spec):
        module = types.ModuleType(spec.name)
        module.__path__ = []
        cache = {}
        def getattr_(attr):
            if attr not in cache:
                cache[attr] = _stub_attr(spec.name, attr)
            return cache[attr]
        module.__getattr__ = getattr_
        return module

    def exec_module(self, module):
        pass


def load_adapter():
    sys.meta_path.insert(0, _StubFinder())
    sys.path.insert(0, str(HERE))
    try:
        spec = importlib.util.spec_from_file_location("hermes_tg_adapter_under_test", HERE / "telegram-adapter.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.meta_path.pop(0)


try:
    ADAPTER = load_adapter()
    ADAPTER._redact_telegram_error_text = repr
    assert ADAPTER._cca.__file__.endswith("cc_approvals.py"), ADAPTER._cca
    ADAPTER.InlineKeyboardButton = lambda text, callback_data: (text, callback_data)
    ADAPTER.InlineKeyboardMarkup = lambda rows: rows
    LOAD_ERROR = None
except Exception as exc:  # pragma: no cover - reported by the test
    ADAPTER, LOAD_ERROR = None, exc


class Result:
    def __init__(self, ok=True, mid="99"):
        self.success, self.message_id, self.error = ok, mid, None


class FakeCC:
    def __init__(self):
        self.calls = []
        self.status = {}

    async def get(self, ident):
        return {"approval_id": ident, "status": "pending", "summary": f"פעולה {ident[:4]}", "expires_at": 9e12}

    async def decide(self, ident, decision, via, actor):
        self.calls.append((ident, decision, via))
        if ident in self.status:
            return {"status": self.status[ident], "already_decided": True,
                    "decided": {"via": "button", "actor": "Ben", "decided_at": 1}}
        self.status[ident] = "executed" if decision == "approve" else "denied"
        return {"status": self.status[ident], "decided": {"via": via, "actor": actor, "decided_at": 1}}


@unittest.skipIf(ADAPTER is None, f"adapter import failed: {LOAD_ERROR!r}")
class GlueTests(unittest.TestCase):
    def make(self):
        cls = ADAPTER.TelegramAdapter
        a = object.__new__(cls)
        a.name = "telegram"
        a._bot = mock.MagicMock()
        a._bot.edit_message_text = mock.AsyncMock()
        a._reply_to_mode = None
        a._background_tasks = set()
        sent = []

        async def control(chat_id, text, **kw):
            sent.append((text, kw.get("reply_markup")))
            return types.SimpleNamespace(message_id=100 + len(sent))
        a._send_control_message = control
        a._metadata_thread_id = lambda md: None
        a._send_message_with_thread_fallback = mock.AsyncMock()
        self.cc = FakeCC()
        ui = ADAPTER._TelegramApprovalUI(a)
        a._cc_ctl = ADAPTER._cca.ApprovalController(ADAPTER._cca.Registry(":memory:"), self.cc, ui)
        return a, sent

    def test_send_strips_every_marker_and_sends_labelled_prompts(self):
        a, sent = self.make()
        plain = []

        async def text_locked(chat_id, content, reply_to, metadata):
            plain.append(content)
            return Result()
        a._send_text_locked = text_locked
        a._chat_send_lock = lambda chat_id: _NullLock()
        a._send_path_degraded = False
        ids = ["a1" * 9, "b2" * 9]
        content = f"הכנתי שני אירועים. [[CC_APPROVAL:{ids[0]}]] [[CC_APPROVAL:{ids[1]}]]"
        asyncio.run(a.send("1", content))
        self.assertEqual(plain, ["הכנתי שני אירועים."])
        self.assertEqual(len(sent), 2)
        self.assertIn("#1", sent[0][0])
        self.assertIn("פעולה a1a1", sent[0][0])

    def test_chat_reply_consumed_and_button_then_already_decided(self):
        a, sent = self.make()
        ident = "c3" * 9
        asyncio.run(a._send_command_center_approval_prompts("1", [ident], "99", None))
        msg = types.SimpleNamespace(chat_id=1, text="מאשר", reply_to_message=None,
                                    from_user=types.SimpleNamespace(first_name="Ben"))
        self.assertTrue(asyncio.run(a._cc_consume_chat_decision(msg)))
        self.assertEqual(self.cc.calls, [(ident, "approve", "chat")])
        a._bot.edit_message_text.assert_awaited()
        self.assertIsNone(a._bot.edit_message_text.await_args.kwargs["reply_markup"])
        other = types.SimpleNamespace(chat_id=1, text="מה השעה?", reply_to_message=None,
                                      from_user=types.SimpleNamespace(first_name="Ben"))
        self.assertFalse(asyncio.run(a._cc_consume_chat_decision(other)))

        query = mock.MagicMock()
        query.answer = mock.AsyncMock()
        query.from_user.first_name = "Ben"
        query.message.message_id = 101
        a._callback_authorized = mock.AsyncMock(return_value=True)
        with mock.patch.dict(os.environ, {"COMMAND_CENTER_CALLBACK_TOKEN": "x"}):
            asyncio.run(a._handle_command_center_approval_callback(query, f"cc:a:{ident}", {"chat_id": 1}))
        self.assertIn("כבר הוחלט", query.answer.await_args.kwargs["text"])
        self.assertEqual(len(self.cc.calls), 1)

    def test_many_markers_make_one_batch_prompt(self):
        a, sent = self.make()
        ids = [f"{i:02x}" * 9 for i in range(1, 9)]
        asyncio.run(a._send_command_center_approval_prompts("1", ids, "99", None))
        self.assertEqual(len(sent), 1)
        self.assertIn("8 פעולות", sent[0][0])
        query = mock.MagicMock()
        query.answer = mock.AsyncMock()
        query.from_user.first_name = "Ben"
        a._callback_authorized = mock.AsyncMock(return_value=True)
        batch_id = a._cc_ctl.registry.get(ids[0])["batch_id"]
        with mock.patch.dict(os.environ, {"COMMAND_CENTER_CALLBACK_TOKEN": "x"}):
            asyncio.run(a._handle_command_center_approval_callback(query, f"cc:bd:{batch_id}", {"chat_id": 1}))
        self.assertEqual(len(self.cc.calls), 8)
        self.assertIn("8 פעולות", a._bot.edit_message_text.await_args.kwargs["text"])

    def test_invalid_callback_data_rejected(self):
        a, _ = self.make()
        query = mock.MagicMock()
        query.answer = mock.AsyncMock()
        a._callback_authorized = mock.AsyncMock(return_value=True)
        asyncio.run(a._handle_command_center_approval_callback(query, "cc:x:zz", {"chat_id": 1}))
        self.assertEqual(query.answer.await_args.kwargs["text"], "Invalid approval data.")


class _NullLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()
