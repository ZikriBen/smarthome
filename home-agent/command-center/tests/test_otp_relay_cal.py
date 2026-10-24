"""otp-relay `#OTP cal` rules (synthetic SMS texts; no real codes)."""
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
PLUGIN_DIR = HERE.parent / "hermes" / "plugins" / "otp-relay"
os.environ.setdefault("COMMAND_CENTER_TOKEN", "test")
sys.path.insert(0, str(HERE))
from command_center import otp_inbox as inbox  # noqa: E402

BOT = "7000000001"


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "otp_relay_plugin_cal", PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["otp_relay_plugin_cal"] = module
    spec.loader.exec_module(module)
    return module


PLUGIN = load_plugin()


def event(text):
    source = types.SimpleNamespace(platform=types.SimpleNamespace(value="telegram"), chat_id=BOT, user_id=BOT,
                                   is_bot=True, chat_type="dm", message_id=None)
    return types.SimpleNamespace(text=text, source=source, message_id="77")


class CalRelayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "otp" / "codes.sqlite"
        self.saved = {k: os.environ.get(k) for k in ("OTP_FORWARD_BOT_ID", "OTP_FORWARD_CHAT_ID", "OTP_INBOX_DB",
                                                     "OTP_CAL_REQUIRE_RE", "OTP_CAL_CODE_DIGITS")}
        os.environ.update(OTP_FORWARD_BOT_ID=BOT, OTP_INBOX_DB=str(self.db))
        for k in ("OTP_FORWARD_CHAT_ID", "OTP_CAL_REQUIRE_RE", "OTP_CAL_CODE_DIGITS"):
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def relay(self, body, site="cal"):
        out = PLUGIN.on_pre_gateway_dispatch(event=event(f"#OTP {site} {body}"))
        self.assertEqual(out, PLUGIN.SKIP)
        return inbox.take(self.db, site) if self.db.exists() else None

    def test_site_registered_everywhere(self):
        self.assertIn("cal", inbox.SITES)
        self.assertIn("cal", PLUGIN.SITE_CODE_RE)
        self.assertEqual(inbox.SITE_TTL["cal"], 180)

    def test_login_sms_stored(self):
        self.assertEqual(self.relay("קוד הכניסה שלך לאתר כאל הוא 482913"), "482913")

    def test_webotp_line(self):
        self.assertEqual(self.relay("קוד אימות: 482913\n@digital-web.cal-online.co.il #482913"), "482913")

    def test_purchase_and_approval_codes_never_stored(self):
        for body in ("קוד לאישור עסקה באתר בסך 120 ₪: 482913",
                     "קוד אימות לרכישה אונליין 482913",
                     "Your Cal 3D Secure purchase code 482913",
                     "קוד להוספת הכרטיס לארנק דיגיטלי Apple Pay: 482913",
                     "כאל: קוד לאישור הגדלת מסגרת 482913"):
            self.assertIsNone(self.relay(body), body)

    def test_cross_site_messages_dropped(self):
        self.assertIsNone(self.relay("בנק הפועלים: קוד כניסה 482913"))
        self.assertIsNone(self.relay("super-hist קוד כניסה 482913"))

    def test_needs_login_wording_and_one_code(self):
        self.assertIsNone(self.relay("482913"))
        self.assertIsNone(self.relay("קוד כניסה לכאל 482913 או 112233"))
        self.assertIsNone(self.relay("קוד כניסה לכאל 4829"))          # default 6 digits

    def test_code_digits_override_and_bad_config(self):
        os.environ["OTP_CAL_CODE_DIGITS"] = "4-6"
        self.assertEqual(self.relay("קוד כניסה לכאל 4829"), "4829")
        os.environ["OTP_CAL_CODE_DIGITS"] = "9"
        self.assertIsNone(self.relay("קוד כניסה לכאל 482913"))

    def test_cal_code_never_lands_in_hapoalim(self):
        self.assertEqual(self.relay("קוד הכניסה שלך לאתר כאל הוא 482913"), "482913")
        self.assertIsNone(inbox.take(self.db, "hapoalim"))


if __name__ == "__main__":
    unittest.main()
