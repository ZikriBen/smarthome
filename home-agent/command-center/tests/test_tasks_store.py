"""tasks_store against a fake Tasks API + the per-profile OAuth split in google_workspace."""
import json
import os
import sys
import tempfile
import types
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

os.environ.setdefault("COMMAND_CENTER_TOKEN", "test")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
if "command_center.approvals" not in sys.modules:
    fake_approvals = types.ModuleType("command_center.approvals")
    fake_approvals.create = lambda *a: "a" * 18
    fake_approvals.register_action = lambda *a: None
    sys.modules["command_center.approvals"] = fake_approvals
from command_center import google_workspace as gw  # noqa: E402
from command_center import tasks_store as ts  # noqa: E402
from fake_tasks import FakeHttpError, FakeTasksAPI, RefreshError  # noqa: E402


class TasksStoreTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeTasksAPI(page_size=2)
        self.store = ts.TasksStore(service=self.api, list_title="קניות")

    def test_finds_existing_list_on_a_later_page(self):
        for t in ("עבודה", "בית", "רכב"):
            self.api.new_list(t)
        lid = self.api.new_list("קניות")
        self.assertEqual(self.store.list_id(), lid)
        self.assertEqual(self.api.inserted_lists, 0)

    def test_creates_list_once_when_missing(self):
        self.api.new_list("עבודה")
        lid = self.store.list_id()
        self.assertEqual(self.api.lists[lid], "קניות")
        self.store.list_id()
        self.assertEqual(self.api.inserted_lists, 1)

    def test_list_open_paginates_and_excludes_ticked_deleted_empty(self):
        self.api.new_list("קניות")
        for t in ("חלב", "ביצים", "פיתות", "סוכר", "קפה"):
            self.api.phone_add(t)
        self.api.phone_add("   ")
        self.api.phone_tick("ביצים")
        self.store.remove(self.api.by_title("קפה")["id"])
        titles = [t["title"] for t in self.store.list_open()]
        self.assertEqual(titles, ["חלב", "פיתות", "סוכר"])  # list order kept

    def test_etag_conflict_and_fresh_write(self):
        self.api.new_list("קניות")
        tid = self.api.phone_add("חלב")
        seen = self.store.list_open()[0]
        self.api.edit_on_get.add(tid)              # edited on the phone after we read it
        with self.assertRaises(ts.TasksConflict):
            self.store.complete(tid, {"etag": seen["etag"]})
        fresh = self.store.get(tid)
        self.assertEqual(self.store.complete(tid, {"etag": fresh["etag"]})["status"], "completed")
        self.assertEqual(self.store.reopen(tid)["status"], "needsAction")

    def test_list_deleted_on_phone_is_recreated(self):
        old = self.api.new_list("קניות")
        self.store.list_id()
        del self.api.lists[old], self.api.store[old]
        self.store.add("חלב")
        self.assertEqual(self.api.open_titles(), ["חלב"])

    def test_error_translation(self):
        cases = [
            (FakeHttpError(403, "Request had insufficient authentication scopes."), ts.TasksAuthError),
            (FakeHttpError(401, "Invalid Credentials"), ts.TasksAuthError),
            (RefreshError("invalid_grant"), ts.TasksAuthError),
            (FakeHttpError(403, "Tasks API has not been used in project 1 or it is disabled"), ts.TasksError),
            (FakeHttpError(503), ts.TasksError),
            (ConnectionError("down"), ts.TasksError),
        ]
        for exc, kind in cases:
            self.api.fail = exc
            with self.assertRaises(kind) as ctx:
                ts.TasksStore(service=self.api).list_open()
            if "disabled" in str(exc):
                self.assertNotIsInstance(ctx.exception, ts.TasksAuthError)
                self.assertIn("not enabled", str(ctx.exception))

    def test_lazy_service_factory_errors_become_tasks_errors(self):
        def boom():
            raise ts.TasksAuthError(ts.NEEDS_CONSENT)
        with self.assertRaises(ts.TasksAuthError):
            ts.TasksStore(service_factory=boom).list_open()


class TasksCredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.token = Path(self.tmp.name) / "google-tasks-token.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_token_says_reconsent(self):
        with mock.patch.object(ts, "GOOGLE_TASKS_TOKEN", self.token):
            with self.assertRaises(ts.TasksAuthError) as ctx:
                ts.tasks_credentials()
        self.assertIn("re-consent", str(ctx.exception))

    def test_refresh_failure_says_reconsent(self):
        from google.auth.exceptions import RefreshError as RealRefreshError
        self.token.write_text(json.dumps({
            "token": "x", "refresh_token": "r", "client_id": "c", "client_secret": "s",
            "token_uri": "https://oauth2.googleapis.com/token", "scopes": ts.GOOGLE_TASKS_SCOPES,
            "expiry": "2020-01-01T00:00:00Z"}))
        with mock.patch.object(ts, "GOOGLE_TASKS_TOKEN", self.token), \
                mock.patch("google.oauth2.credentials.Credentials.refresh",
                           side_effect=RealRefreshError("invalid_scope")):
            with self.assertRaises(ts.TasksAuthError):
                ts.tasks_credentials()


class OAuthProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.secret = root / "client-secret.json"
        self.secret.write_text(json.dumps({"installed": {
            "client_id": "cid.apps.googleusercontent.com", "client_secret": "sec",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"]}}))
        self.ws_token, self.tasks_token = root / "google-token.json", root / "google-tasks-token.json"
        self.patches = [
            mock.patch.object(gw, "GOOGLE_CLIENT_SECRET", self.secret),
            mock.patch.dict(gw.AUTH_PROFILES, {
                "workspace": (gw.GOOGLE_SCOPES, self.ws_token, "Google Calendar and Gmail are connected."),
                "tasks": (gw.GOOGLE_TASKS_SCOPES, self.tasks_token, "Google Tasks is connected.")})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    @staticmethod
    def _query(url):
        return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

    def test_workspace_consent_is_unchanged(self):
        q = self._query(gw.google_auth_start()["authorization_url"])
        self.assertEqual(sorted(q["scope"][0].split()), sorted(gw.GOOGLE_SCOPES))
        self.assertEqual(q["include_granted_scopes"], ["true"])
        self.assertNotIn("tasks", q["scope"][0])

    def test_tasks_consent_requests_only_tasks(self):
        out = gw.google_auth_start("tasks")
        q = self._query(out["authorization_url"])
        self.assertEqual(q["scope"], ["https://www.googleapis.com/auth/tasks"])
        self.assertEqual(q["include_granted_scopes"], ["false"])
        self.assertEqual(q["access_type"], ["offline"])
        self.assertEqual(out["profile"], "tasks")

    def test_bad_profile(self):
        with self.assertRaises(ValueError):
            gw.google_auth_start("drive")

    def _callback(self, state):
        handler = gw.GoogleOAuthCallback.__new__(gw.GoogleOAuthCallback)
        handler.path = "/?" + urllib.parse.urlencode({"code": "c", "state": state})
        replies = []
        handler._reply = lambda status, msg: replies.append((status, msg))
        fake_flow = mock.MagicMock()
        fake_flow.credentials.to_json.return_value = '{"refresh_token": "r"}'
        with mock.patch.object(gw, "google_flow", return_value=fake_flow) as flow:
            handler.do_GET()
        return replies[0], flow

    def test_tasks_callback_writes_only_tasks_token(self):
        self.ws_token.write_text("WORKSPACE")
        state = self._query(gw.google_auth_start("tasks")["authorization_url"])["state"][0]
        (status, msg), flow = self._callback(state)
        self.assertEqual(status, 200)
        self.assertIn("Google Tasks is connected", msg)
        flow.assert_called_once_with(gw.GOOGLE_TASKS_SCOPES)
        self.assertEqual(self.ws_token.read_text(), "WORKSPACE")
        self.assertEqual(oct(self.tasks_token.stat().st_mode & 0o777), "0o600")
        # the next consent defaults back to workspace
        state = self._query(gw.google_auth_start()["authorization_url"])["state"][0]
        (status, msg), flow = self._callback(state)
        flow.assert_called_once_with(gw.GOOGLE_SCOPES)
        self.assertIn("Calendar and Gmail", msg)

    def test_calendar_credentials_never_ask_for_tasks(self):
        self.assertNotIn("https://www.googleapis.com/auth/tasks", gw.GOOGLE_SCOPES)
        self.assertIn("tasks_authorized", gw.google_status())


if __name__ == "__main__":
    unittest.main()
