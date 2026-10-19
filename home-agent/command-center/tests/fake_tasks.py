"""In-memory stand-in for the googleapiclient Tasks v1 resource (pagination, etags, errors)."""
import itertools
from types import SimpleNamespace


class FakeHttpError(Exception):
    """Shaped like googleapiclient.errors.HttpError: .resp.status + message."""

    def __init__(self, status, message=""):
        super().__init__(message or f"HTTP {status}")
        self.resp = SimpleNamespace(status=status)


class RefreshError(Exception):
    """Same class name as google.auth.exceptions.RefreshError."""


class Req:
    def __init__(self, api, fn):
        self.api, self.fn = api, fn

    def execute(self):
        self.api.calls += 1
        if self.api.fail is not None:
            raise self.api.fail
        return self.fn()


class FakeTasksAPI:
    def __init__(self, page_size=2):
        self.page_size = page_size
        self.lists = {}     # list_id -> title
        self.store = {}     # list_id -> {task_id: task}
        self.fail = None
        self.calls = 0
        self.inserted_lists = 0
        self.edit_on_get = set()   # task ids "edited on the phone" right before our get()
        self._n = itertools.count(1)

    # --- helpers for tests --------------------------------------------------------------------
    def _tick(self):
        return next(self._n)

    def _stamp(self, task):
        n = self._tick()
        task["etag"], task["updated"] = f'"e{n}"', f"2026-10-19T09:{n % 60:02d}:00.000Z"

    def new_list(self, title):
        lid = f"L{self._tick()}"
        self.lists[lid], self.store[lid] = title, {}
        return lid

    def list_id(self, title="קניות"):
        return next(k for k, v in self.lists.items() if v == title)

    def phone_add(self, title, lid=None):
        lid = lid or self.list_id()
        tid = f"T{self._tick()}"
        task = {"id": tid, "title": title, "status": "needsAction",
                "position": f"{len(self.store[lid]):020d}"}
        self._stamp(task)
        self.store[lid][tid] = task
        return tid

    def by_title(self, title, lid=None):
        lid = lid or self.list_id()
        return next(t for t in self.store[lid].values() if t["title"] == title)

    def phone_tick(self, title):
        t = self.by_title(title)
        t["status"] = "completed"
        self._stamp(t)

    def open_titles(self, lid=None):
        lid = lid or self.list_id()
        return sorted(t["title"] for t in self.store[lid].values()
                      if t["status"] == "needsAction" and not t.get("deleted"))

    def _page(self, items, token):
        start = int(token or 0)
        chunk = items[start:start + self.page_size]
        out = {"items": chunk}
        if start + self.page_size < len(items):
            out["nextPageToken"] = str(start + self.page_size)
        return out

    def _list(self, lid):
        if lid not in self.store:
            raise FakeHttpError(404, "Not Found")
        return self.store[lid]

    def _task(self, lid, tid):
        tasks = self._list(lid)
        if tid not in tasks or tasks[tid].get("deleted"):
            raise FakeHttpError(404, "Not Found")
        return tasks[tid]

    # --- resource surface ---------------------------------------------------------------------
    def tasklists(self):
        api = self

        class TL:
            def list(self, maxResults=100, pageToken=None):
                items = [{"id": k, "title": v} for k, v in api.lists.items()]
                return Req(api, lambda: api._page(items, pageToken))

            def insert(self, body):
                def run():
                    api.inserted_lists += 1
                    lid = api.new_list(body["title"])
                    return {"id": lid, "title": body["title"]}
                return Req(api, run)
        return TL()

    def tasks(self):
        api = self

        class T:
            def list(self, tasklist, maxResults=100, pageToken=None, showCompleted=True,
                     showHidden=False, showDeleted=False):
                def run():
                    items = [dict(t) for t in api._list(tasklist).values()
                             if (showCompleted or t["status"] != "completed")
                             and (showDeleted or not t.get("deleted"))]
                    return api._page(items, pageToken)
                return Req(api, run)

            def get(self, tasklist, task):
                def run():
                    t = api._task(tasklist, task)
                    if task in api.edit_on_get:
                        api.edit_on_get.discard(task)
                        t["title"] += " (phone)"
                        api._stamp(t)
                    return dict(t)
                return Req(api, run)

            def insert(self, tasklist, body):
                def run():
                    api._list(tasklist)
                    return dict(api._task(tasklist, api.phone_add(body["title"], tasklist)))
                return Req(api, run)

            def patch(self, tasklist, task, body):
                def run():
                    t = api._task(tasklist, task)
                    t.update({k: v for k, v in body.items() if v is not None})
                    if body.get("completed", 0) is None:
                        t.pop("completed", None)
                    api._stamp(t)
                    return dict(t)
                return Req(api, run)

            def delete(self, tasklist, task):
                def run():
                    api._task(tasklist, task)["deleted"] = True
                return Req(api, run)
        return T()
