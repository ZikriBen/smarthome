"""Google Tasks access for the grocery list (one task list, default "קניות").

Uses its own token (GOOGLE_TASKS_TOKEN, scope tasks only), so Calendar/Gmail never depend on it.
Every failure is raised as TasksError; a missing/revoked grant is TasksAuthError ("needs re-consent").
"""

import os

from .config import GOOGLE_TASKS_SCOPES, GOOGLE_TASKS_TOKEN, GROCERY_TASKLIST

NEEDS_CONSENT = "Google Tasks needs re-consent (one-time authorization for the tasks scope)"
PAGE = 100  # Tasks API maximum page size


class TasksError(Exception):
    """Any Tasks failure. str() is short and safe to show the user."""


class TasksAuthError(TasksError):
    pass


class TasksConflict(TasksError):
    """The task changed (e.g. edited on the phone) since it was read."""


class TasksNotFound(TasksError):
    pass


def tasks_credentials():
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    if not GOOGLE_TASKS_TOKEN.is_file():
        raise TasksAuthError(NEEDS_CONSENT)
    try:
        credentials = Credentials.from_authorized_user_file(str(GOOGLE_TASKS_TOKEN),
                                                            GOOGLE_TASKS_SCOPES)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            _save_token(credentials)
    except RefreshError as exc:  # invalid_grant / invalid_scope / revoked
        raise TasksAuthError(f"{NEEDS_CONSENT}: {str(exc)[:120]}") from exc
    except ValueError as exc:    # malformed token file
        raise TasksAuthError(f"{NEEDS_CONSENT}: {exc}") from exc
    if not credentials.valid:
        raise TasksAuthError(NEEDS_CONSENT)
    return credentials


def _save_token(credentials):
    """Same 0600 write as google_workspace.save_google_token, kept local (no Calendar imports)."""
    descriptor = os.open(GOOGLE_TASKS_TOKEN, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
        os.fchmod(descriptor, 0o600)
        token_file.write(credentials.to_json())


def build_service():
    from googleapiclient.discovery import build
    return build("tasks", "v1", credentials=tasks_credentials(), cache_discovery=False)


def _translate(exc):
    """googleapiclient HttpError / transport errors -> TasksError subclasses."""
    if isinstance(exc, TasksError):
        return exc
    status = getattr(getattr(exc, "resp", None), "status", None)
    text = str(exc)[:160]
    if type(exc).__name__ == "RefreshError":
        return TasksAuthError(f"{NEEDS_CONSENT}: {text}")
    if status == 403 and ("accessNotConfigured" in text or "has not been used" in text
                          or "disabled" in text.lower()):
        return TasksError("Google Tasks API is not enabled in the Google Cloud project")
    if status in (401, 403) and ("insufficient" in text.lower() or "scope" in text.lower()
                                 or status == 401):
        return TasksAuthError(f"{NEEDS_CONSENT} (HTTP {status})")
    if status == 404:
        return TasksNotFound("task or list not found")
    if status in (409, 412):
        return TasksConflict("changed meanwhile")
    if status is not None:
        return TasksError(f"Google Tasks HTTP {status}")
    if isinstance(exc, (OSError, TimeoutError)) or "ServerNotFound" in type(exc).__name__:
        return TasksError(f"Google Tasks unreachable: {type(exc).__name__}")
    return TasksError(f"Google Tasks error: {type(exc).__name__}: {text[:80]}")


def _slim(task):
    return {"id": task["id"], "title": (task.get("title") or "").strip(),
            "notes": task.get("notes") or "", "etag": task.get("etag"),
            "updated": task.get("updated"), "position": task.get("position") or "",
            "status": task.get("status", "needsAction")}


class TasksStore:
    """Thin, paginated wrapper. `service` is a googleapiclient Tasks resource (or a fake)."""

    def __init__(self, service=None, list_title=GROCERY_TASKLIST, service_factory=build_service):
        self._service = service
        self._factory = service_factory
        self.list_title = list_title
        self._list_id = None

    @property
    def service(self):
        if self._service is None:
            self._service = self._call(self._factory)
        return self._service

    @staticmethod
    def _call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - every failure becomes a TasksError
            raise _translate(exc) from exc

    def _exec(self, request):
        return self._call(request.execute)

    # --- list ---------------------------------------------------------------------------------
    def list_id(self):
        if self._list_id:
            return self._list_id
        token = None
        while True:
            page = self._exec(self.service.tasklists().list(maxResults=PAGE, pageToken=token))
            for tl in page.get("items", []):
                if (tl.get("title") or "").strip() == self.list_title:
                    self._list_id = tl["id"]
                    return self._list_id
            token = page.get("nextPageToken")
            if not token:
                break
        created = self._exec(self.service.tasklists().insert(body={"title": self.list_title}))
        self._list_id = created["id"]
        return self._list_id

    def _with_list(self, fn):
        """Run fn(list_id); if the list vanished (deleted on the phone), find/create it once more."""
        try:
            return fn(self.list_id())
        except TasksNotFound:
            self._list_id = None
            return fn(self.list_id())

    # --- tasks --------------------------------------------------------------------------------
    def list_open(self):
        """Open (unticked) tasks, in the list's display order. Ticked/hidden/deleted are excluded."""
        def run(lid):
            out, token = [], None
            while True:
                page = self._exec(self.service.tasks().list(
                    tasklist=lid, maxResults=PAGE, pageToken=token, showCompleted=False,
                    showHidden=False, showDeleted=False))
                out += [_slim(t) for t in page.get("items", [])
                        if t.get("status", "needsAction") == "needsAction" and not t.get("deleted")
                        and (t.get("title") or "").strip()]
                token = page.get("nextPageToken")
                if not token:
                    return sorted(out, key=lambda t: t["position"])
        return self._with_list(run)

    def get(self, task_id):
        return _slim(self._exec(self.service.tasks().get(tasklist=self.list_id(), task=task_id)))

    def _check(self, task_id, expected):
        """Optimistic concurrency: compare etag (or `updated`) with what we last read."""
        if not expected:
            return None
        current = self.get(task_id)
        if expected.get("etag") and current["etag"] != expected["etag"]:
            raise TasksConflict("changed meanwhile")
        if not expected.get("etag") and expected.get("updated") and \
                current["updated"] != expected["updated"]:
            raise TasksConflict("changed meanwhile")
        return current

    def add(self, title, notes=""):
        body = {"title": title[:1024]}
        if notes:
            body["notes"] = notes[:8192]
        return _slim(self._with_list(lambda lid: self._exec(
            self.service.tasks().insert(tasklist=lid, body=body))))

    def update_title(self, task_id, title, expected=None):
        self._check(task_id, expected)
        return _slim(self._exec(self.service.tasks().patch(
            tasklist=self.list_id(), task=task_id, body={"title": title[:1024]})))

    def complete(self, task_id, expected=None):
        self._check(task_id, expected)
        return _slim(self._exec(self.service.tasks().patch(
            tasklist=self.list_id(), task=task_id, body={"status": "completed"})))

    def reopen(self, task_id):
        return _slim(self._exec(self.service.tasks().patch(
            tasklist=self.list_id(), task=task_id, body={"status": "needsAction", "completed": None})))

    def remove(self, task_id, expected=None):
        self._check(task_id, expected)
        self._exec(self.service.tasks().delete(tasklist=self.list_id(), task=task_id))
