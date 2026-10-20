"""הסופר החברתי login flow (manual code paste), offline: scripted site responses + a local HTTP server."""
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
os.environ.setdefault("COMMAND_CENTER_TOKEN", "test")
sys.path.insert(0, str(HERE))
from command_center import super_hist as sh  # noqa: E402
from command_center.router import Router  # noqa: E402

FORMS = (Path(__file__).parent / "fixtures" / "super-hist" / "login_forms.html").read_text()
VAT = "123456782"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        sh.DATA_DIR, sh.VAT_ID_FILE, sh.COOKIES_FILE = root, root / "vat_id", root / "cookies.txt"
        sh.VAT_ID_FILE.write_text(VAT + "\n")
        sh.INBOX_DB = root / "no-inbox" / "codes.sqlite"  # relay off unless a test turns it on
        sh._jar = None
        sh._state.update(phase="idle", sent_at=0.0, attempts=0, logged_in_at=0.0)
        self.clock = [1_000_000.0]
        self.slept = []
        sh._now = lambda: self.clock[0]
        sh._sleep = lambda s: (self.slept.append(s), self.clock.__setitem__(0, self.clock[0] + s))
        sh._last_request[0] = 0.0

    def tearDown(self):
        sh._jar = None
        self.tmp.cleanup()


class ScriptedSite(Base):
    """Replace _send with a scripted site; check request shapes and never-leak rules."""

    def setUp(self):
        super().setUp()
        self.calls, self.replies = [], []
        self.real_send = sh._send
        sh._send = self.fake_send

    def tearDown(self):
        sh._send = self.real_send
        super().tearDown()

    def fake_send(self, method, url, form=None, ajax=False, follow=True):
        self.calls.append((method, url, dict(form or {}), ajax, follow))
        return self.replies.pop(0)

    def script(self, *replies):
        self.replies.extend(replies)

    def assert_no_secret(self, result, *secrets):
        raw = json.dumps(result, ensure_ascii=False)
        for secret in secrets:
            self.assertNotIn(secret, raw)

    def test_start_sends_id_once_and_hides_it(self):
        self.script((200, {}, FORMS), (200, {}, json.dumps({"success": True, "message": "קוד נשלח לטלפון 050***1234"})))
        out = sh.start_login({}, {})
        self.assertEqual(out["status"], "code_sent")
        self.assertEqual([c[:2] for c in self.calls], [("GET", sh.LOGIN_PAGE), ("POST", sh.SMS_URL)])
        self.assertEqual(self.calls[1][2], {"form_type": "sms", "vat_id": VAT})
        self.assertTrue(self.calls[1][3])
        self.assert_no_secret(out, VAT)
        self.assertEqual(sh._state["phase"], "code_sent")

    def test_missing_vat_id_contacts_nothing(self):
        sh.VAT_ID_FILE.unlink()
        self.assertEqual(sh.start_login({}, {})["status"], "not_configured")
        sh.VAT_ID_FILE.write_text("abc")
        self.assertEqual(sh.start_login({}, {})["status"], "not_configured")
        self.assertEqual(self.calls, [])

    def test_changed_login_page_fails_closed(self):
        self.script((200, {}, "<html>Just a moment...</html>"))
        out = sh.start_login({}, {})
        self.assertEqual(out["status"], "site_changed")
        self.assertEqual(len(self.calls), 1)  # the ID was never posted

    def test_cooldown_blocks_resend(self):
        self.script((200, {}, FORMS), (200, {}, '{"success": true, "message": "ok"}'))
        sh.start_login({}, {})
        self.clock[0] += 20
        out = sh.start_login({}, {})
        self.assertEqual(out["status"], "cooldown")
        self.assertEqual(out["retry_after_seconds"], 40)
        self.assertEqual(len(self.calls), 2)

    def test_rate_limited(self):
        self.script((200, {}, FORMS), (429, {"x-ratelimit-remaining": "0"},
                                       '{"error": "rate_limited", "retry_after_seconds": 900}'))
        out = sh.start_login({}, {})
        self.assertEqual(out["status"], "rate_limited")
        self.assertEqual(out["retry_after_seconds"], 900)
        self.assertEqual(sh._state["phase"], "idle")

    def test_site_error_text_is_cleaned(self):
        self.script((200, {}, FORMS), (200, {}, json.dumps({"error": "<b>ת.ז. 123456782 לא נמצאה</b>"})))
        out = sh.start_login({}, {})
        self.assertEqual(out["status"], "rejected")
        self.assertNotIn("<b>", out["text"])
        self.assert_no_secret(out, VAT)

    def test_code_happy_path(self):
        self.script((200, {}, FORMS), (200, {}, '{"success": true, "message": "ok"}'))
        sh.start_login({}, {})
        self.clock[0] += 150  # Ben took 2.5 minutes to paste: nothing expires
        self.script((200, {}, json.dumps({"redirect": "https://super-hist.co.il/account/"})), (200, {}, "<html>account</html>"))
        out = sh.submit_code({"code": " 482-913 "}, {})
        self.assertEqual(out["status"], "logged_in")
        self.assertEqual(self.calls[2][1:3], (sh.VALIDATE_URL, {"form_type": "sms", "code": "482913"}))
        self.assertEqual(self.calls[3][:2], ("GET", sh.ACCOUNT_URL))
        self.assertFalse(self.calls[3][4])  # status probe does not follow the login redirect
        self.assert_no_secret(out, "482913", VAT)

    def test_bad_code_format_contacts_nothing(self):
        for bad in ("", "12345", "1234567", "abcdef", None):
            self.assertEqual(sh.submit_code({"code": bad}, {})["status"], "invalid_code")
        self.assertEqual(self.calls, [])

    def test_code_without_pending_login(self):
        self.assertEqual(sh.submit_code({"code": "123456"}, {})["status"], "no_pending_login")
        self.assertEqual(self.calls, [])

    def test_code_window_expires(self):
        self.script((200, {}, FORMS), (200, {}, '{"success": true}'))
        sh.start_login({}, {})
        self.clock[0] += sh.CODE_WINDOW + 1
        self.assertEqual(sh.submit_code({"code": "123456"}, {})["status"], "no_pending_login")
        self.assertEqual(len(self.calls), 2)

    def test_wrong_code_then_lockout(self):
        self.script((200, {}, FORMS), (200, {}, '{"success": true}'))
        sh.start_login({}, {})
        self.script((200, {}, '{"error": "קוד שגוי"}'))
        out = sh.submit_code({"code": "111111"}, {})
        self.assertEqual((out["status"], out["attempts_left"], out["text"]), ("wrong_code", 2, "קוד שגוי"))
        self.script((200, {}, '{"error": "קוד שגוי"}'), (200, {}, '{"error": "קוד שגוי"}'))
        sh.submit_code({"code": "222222"}, {})
        out = sh.submit_code({"code": "333333"}, {})
        self.assertEqual(out["status"], "too_many_attempts")
        self.assertEqual(sh.submit_code({"code": "444444"}, {})["status"], "no_pending_login")

    def test_redirect_but_not_logged_in_is_not_success(self):
        self.script((200, {}, FORMS), (200, {}, '{"success": true}'))
        sh.start_login({}, {})
        self.script((200, {}, '{"redirect": "https://super-hist.co.il/login/"}'), (302, {"Location": "/login/"}, ""))
        self.assertEqual(sh.submit_code({"code": "123456"}, {})["status"], "wrong_code")

    def test_status_and_already_logged_in(self):
        self.script((302, {}, ""))
        self.assertEqual(sh.status({}, {})["status"], "logged_out")
        self.script((200, {}, "account"))
        self.assertEqual(sh.status({}, {})["status"], "logged_in")
        self.script((200, {}, "account"))
        self.assertEqual(sh.start_login({}, {})["status"], "already_logged_in")
        self.assertEqual(sum(1 for c in self.calls if c[1] == sh.SMS_URL), 0)

    def test_site_down(self):
        def boom(*_a, **_k):
            raise sh.SiteError("האתר לא זמין כרגע (URLError)")
        sh._send = boom
        self.assertEqual(sh.start_login({}, {})["status"], "site_error")
        self.assertEqual(sh.status({}, {})["status"], "site_error")

    def test_routes_registered(self):
        router = Router()
        sh.register_routes(router)
        self.script((302, {}, ""))
        status, body = router.dispatch("GET", "/v1/super-hist/status")
        self.assertEqual((status, body["status"]), (200, "logged_out"))
        status, body = router.dispatch("POST", "/v1/super-hist/login/code", {"code": "x"})
        self.assertEqual(body["status"], "invalid_code")
        self.assertEqual(router.dispatch("POST", "/v1/super-hist/logout", {})[1]["status"], "logged_out")


class FakeSite(http.server.BaseHTTPRequestHandler):
    """Minimal local stand-in for super-hist.co.il (same routes, cookie session, JSON shapes)."""
    sessions = {}
    posts = []

    def log_message(self, *_):
        pass

    def _sid(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            if part.strip().startswith("PHPSESSID="):
                return part.strip().split("=", 1)[1]
        return None

    def _reply(self, code, body, ctype="text/html", cookie=None, location=None):
        raw = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if cookie:
            self.send_header("Set-Cookie", f"PHPSESSID={cookie}; Max-Age=12600; path=/; HttpOnly")
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        sid = self._sid()
        if self.path.startswith("/login/"):
            if not sid:
                sid = f"s{len(self.sessions) + 1}"
                self.sessions[sid] = {"sent": False, "auth": False}
                return self._reply(200, FORMS, cookie=sid)
            return self._reply(200, FORMS)
        if "route=account/account" in self.path:
            if sid and self.sessions.get(sid, {}).get("auth"):
                return self._reply(200, "<html>החשבון שלי</html>")
            return self._reply(302, "", location="/login/")
        return self._reply(404, "")

    def do_POST(self):
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        self.posts.append((self.path, {k: v[0] for k, v in form.items()}, self.headers.get("X-Requested-With")))
        session = self.sessions.get(self._sid())
        if session is None:
            return self._reply(200, '{"error": "session"}', "application/json")
        if self.path.endswith("route=account/login/sms"):
            session["sent"] = True
            return self._reply(200, json.dumps({"success": True, "message": "קוד נשלח"}), "application/json")
        if self.path.endswith("route=account/login/sms_validate"):
            if session["sent"] and form.get("code") == ["654321"]:
                session["auth"] = True
                return self._reply(200, '{"redirect": "/account/"}', "application/json")
            return self._reply(200, json.dumps({"error": "קוד שגוי"}), "application/json")
        return self._reply(404, "")


class RealHTTP(Base):
    """The real urllib path: cookie session kept across calls and persisted (0600) for a restart."""

    def setUp(self):
        super().setUp()
        FakeSite.sessions, FakeSite.posts = {}, []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeSite)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.server.server_port}"
        self.saved = (sh.LOGIN_PAGE, sh.SMS_URL, sh.VALIDATE_URL, sh.ACCOUNT_URL, sh.BASE)
        sh.BASE, sh.LOGIN_PAGE = base, base + "/login/"
        sh.SMS_URL = base + "/index.php?route=account/login/sms"
        sh.VALIDATE_URL = base + "/index.php?route=account/login/sms_validate"
        sh.ACCOUNT_URL = base + "/index.php?route=account/account"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        sh.LOGIN_PAGE, sh.SMS_URL, sh.VALIDATE_URL, sh.ACCOUNT_URL, sh.BASE = self.saved
        super().tearDown()

    def test_full_flow_with_wait_and_restart(self):
        self.assertEqual(sh.status({}, {})["status"], "logged_out")
        self.assertEqual(sh.start_login({}, {})["status"], "code_sent")
        self.clock[0] += 180  # a 3-minute wait for Ben's paste
        self.assertEqual(sh.submit_code({"code": "000000"}, {})["status"], "wrong_code")
        self.assertEqual(sh.submit_code({"code": "654321"}, {})["status"], "logged_in")
        posts = [(p.split("route=")[1], f, x) for p, f, x in FakeSite.posts]
        self.assertEqual(posts[0], ("account/login/sms", {"form_type": "sms", "vat_id": VAT}, "XMLHttpRequest"))
        self.assertEqual(posts[-1], ("account/login/sms_validate", {"form_type": "sms", "code": "654321"}, "XMLHttpRequest"))
        self.assertEqual(oct(sh.COOKIES_FILE.stat().st_mode & 0o777), "0o600")
        self.assertNotIn("654321", sh.COOKIES_FILE.read_text())
        self.assertNotIn(VAT, sh.COOKIES_FILE.read_text())
        sh._jar = None  # simulate a Command Center restart: the session is reloaded from disk
        sh._state.update(phase="idle")
        self.assertEqual(sh.status({}, {})["status"], "logged_in")
        self.assertEqual(sh.logout({}, {})["status"], "logged_out")
        self.assertFalse(sh.COOKIES_FILE.exists())
        self.assertEqual(sh.status({}, {})["status"], "logged_out")

    def test_requests_are_spaced(self):
        sh.status({}, {})
        sh.status({}, {})
        sh.status({}, {})
        self.assertEqual(len(self.slept), 2)
        self.assertTrue(all(abs(s - sh.MIN_INTERVAL) < 0.01 for s in self.slept))


if __name__ == "__main__":
    unittest.main()
