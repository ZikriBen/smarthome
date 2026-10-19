"""The working grocery list. Google Tasks ("קניות") is the source of truth; current_list.json is a cache.

- Every read pulls the open (unticked) tasks first, so edits made on the phone win and ticked
  items never reach a price run.
- Writes go to Tasks first. If Tasks is unreachable or not authorized, the change is applied to the
  cache, queued in `pending`, replayed on the next successful sync, and every reply says so.
- One task title is human text ("חלב", "מלפפון ×2", "עגבניות 0.5 ק\"ג"); it is parsed with the same
  free-text parser as chat input. Defaults/preferences never live in Tasks.
"""

import datetime as dt
import json
import os

from . import tasks_store as ts

_cfg = {"defaults": None, "state": None, "backend": "tasks", "factory": None}
_store = None


def configure(defaults_path, state_file, backend="tasks", store_factory=None):
    global _store
    _cfg.update(defaults=str(defaults_path), state=str(state_file), backend=backend,
                factory=store_factory or ts.TasksStore)
    _store = None


def _defaults():
    from grocery.defaults import load_defaults
    return load_defaults(_cfg["defaults"])


def _parse(text):
    from grocery.freetext import parse_text
    return parse_text(text)


def _now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _read():
    try:
        with open(_cfg["state"], encoding="utf-8") as f:
            st = json.load(f)
    except FileNotFoundError:
        st = {}
    st.setdefault("items", [])
    st.setdefault("tasks", {})
    st.setdefault("pending", [])
    st.setdefault("last_cleared", [])
    st.setdefault("synced_at", None)
    for e in st["items"]:   # caches written by the pre-Tasks version have no task ids
        e.setdefault("task_ids", [])
    return st


def _write(st):
    st["updated_at"] = _now()
    tmp = _cfg["state"] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _cfg["state"])


def _get_store():
    global _store
    if _store is None:
        _store = _cfg["factory"]()
    return _store


def _drop_store():
    global _store
    _store = None


def qty_txt(e):
    if e.get("qty") is None:
        return ""
    q = f"{e['qty']:g}"
    return f" ×{q} ק\"ג" if e.get("qty_unit") == "kg" else f" ×{q}"


def title_for(text, qty=None, unit=None):
    """Task title that round-trips through the free-text parser."""
    if qty is None:
        return text
    return f"{text} {qty:g} ק\"ג" if unit == "kg" else f"{text} ×{qty:g}"


def _entry(pl, d, task_id=None):
    it = d.resolve_parsed(pl)
    return {"text": pl.text, "item": it.name, "qty": pl.qty, "qty_unit": pl.qty_unit,
            "known": it.source == "defaults", "task_ids": [task_id] if task_id else []}


def _meta(task):
    return {"title": task["title"], "etag": task.get("etag"), "updated": task.get("updated")}


def _find(st, name):
    return next((e for e in st["items"] if e["item"] == name), None)


def _apply(store, op):
    kind, tid = op["op"], op.get("task_id")
    if kind == "add":
        return store.add(op["title"])
    if kind == "set_title":
        return store.update_title(tid, op["title"])
    if kind == "remove":
        return store.remove(tid)
    if kind == "complete":
        return store.complete(tid)
    if kind == "reopen":
        return store.reopen(tid)
    raise ValueError(f"unknown pending op {kind}")


def _replay(store, st):
    done = 0
    while st["pending"]:
        try:
            _apply(store, st["pending"][0])
        except ts.TasksNotFound:
            pass  # already gone on the phone: nothing left to do
        st["pending"].pop(0)
        done += 1
        _write(st)   # a failure later must not replay what already went through
    return done


def _pull(store, st):
    replayed = _replay(store, st)
    d = _defaults()
    items, metas = [], {}
    for task in store.list_open():
        metas[task["id"]] = _meta(task)
        for pl in _parse(task["title"]):
            e = _entry(pl, d, task["id"])
            ex = _find({"items": items}, e["item"])
            if ex is None:
                items.append(e)
                continue
            ex["task_ids"] = list(dict.fromkeys(ex["task_ids"] + e["task_ids"]))
            if e["qty"] is not None and (ex["qty"] is None or e["qty"] > ex["qty"]):
                ex["qty"], ex["qty_unit"] = e["qty"], e["qty_unit"]   # merge = max
    st.update(items=items, tasks=metas, source="tasks", synced_at=_now())
    _write(st)
    return replayed


def _warning(exc, st):
    when = st.get("synced_at")
    when = dt.datetime.fromisoformat(when).strftime("%d.%m %H:%M") if when else "אף פעם"
    if isinstance(exc, ts.TasksAuthError):
        why = "צריך לאשר מחדש גישה (re-consent)"
    else:
        why = f"לא זמין: {exc}"
    return f"⚠️ Google Tasks {why} — עובד על הרשימה השמורה (סונכרנה לאחרונה: {when})"


def sync():
    """-> (state, warning or None, replayed count). Never raises for Tasks problems."""
    st = _read()
    if _cfg["backend"] != "tasks":
        return st, None, 0
    try:
        replayed = _pull(_get_store(), st)
        return st, None, replayed
    except ts.TasksError as exc:
        if isinstance(exc, ts.TasksAuthError):
            _drop_store()   # pick up a new token after re-consent
        print(f"[grocery] tasks sync failed: {type(exc).__name__}: {exc}", flush=True)
        st = _read()
        st["source"] = "cache"
        return st, _warning(exc, st), 0


class _Writer:
    """Online-first write; after the first Tasks failure, queue the rest for replay."""

    def __init__(self, st, warn):
        self.st, self.warn = st, warn
        self.online = warn is None and _cfg["backend"] == "tasks"

    def do(self, fn, op):
        if self.online:
            try:
                return fn(_get_store())
            except (ts.TasksConflict, ts.TasksNotFound):
                raise   # task-level outcomes: the caller decides
            except ts.TasksError as exc:
                print(f"[grocery] tasks write failed: {type(exc).__name__}: {exc}", flush=True)
                if isinstance(exc, ts.TasksAuthError):
                    _drop_store()
                self.online, self.warn = False, _warning(exc, self.st)
        if _cfg["backend"] == "tasks":
            self.st["pending"].append(op)
        return None


def _reply(text, warn):
    return text + (f"\n{warn}" if warn else "")


def add_item(text):
    st, warn, _ = sync()
    d = _defaults()
    w = _Writer(st, warn)
    out = []
    for pl in _parse(text):
        it = d.resolve_parsed(pl)
        ex = _find(st, it.name)
        if ex is None:
            e = _entry(pl, d)
            title = title_for(pl.text, pl.qty, pl.qty_unit)
            task = w.do(lambda s: s.add(title), {"op": "add", "title": title})
            if task:
                e["task_ids"] = [task["id"]]
                st["tasks"][task["id"]] = _meta(task)
            else:
                e["pending_title"] = title
            st["items"].append(e)
            out.append(f"{e['item']}{qty_txt(e)}" + ("" if e["known"] else " (חדש — חיפוש חופשי)"))
            continue
        same_unit = (ex["qty_unit"] or "unit") == (pl.qty_unit or "unit")
        if pl.qty is None or (ex["qty"] is not None and same_unit and pl.qty <= ex["qty"]):
            # merge = max: re-adding never lowers a quantity (to lower: grocery_remove, then add again)
            out.append(f"{ex['item']}{qty_txt(ex)} (כבר ברשימה)")
            continue
        new_title = title_for(ex["text"], pl.qty, pl.qty_unit)
        tid = ex["task_ids"][0] if ex["task_ids"] else None
        if tid is None:   # still a queued add: just rewrite the queued title
            for op in st["pending"]:
                if op["op"] == "add" and op["title"] == ex.get("pending_title"):
                    op["title"] = new_title
            ex["pending_title"] = new_title
        else:
            try:
                task = w.do(lambda s: s.update_title(tid, new_title, st["tasks"].get(tid)),
                            {"op": "set_title", "task_id": tid, "title": new_title})
            except ts.TasksConflict:
                out.append(f"{ex['item']} (השתנה בטלפון — לא דרסתי, בדוק ב-Tasks)")
                continue
            except ts.TasksNotFound:   # deleted on the phone meanwhile: add it again
                task = w.do(lambda s: s.add(new_title), {"op": "add", "title": new_title})
                ex["task_ids"] = [task["id"]] if task else []
                tid = task["id"] if task else None
            if task and tid:
                st["tasks"][tid] = _meta(task)
        ex.update(qty=pl.qty, qty_unit=pl.qty_unit)
        out.append(f"{ex['item']}{qty_txt(ex)} (עודכן)")
    if not out:
        return _reply("לא זוהו פריטים", warn)
    _write(st)
    return _reply("נוסף: " + " · ".join(out) + f" (ברשימה: {len(st['items'])})", w.warn)


def list_items():
    st, warn, _ = sync()
    if not st["items"]:
        return _reply("הרשימה ריקה", warn)
    return _reply(f"{len(st['items'])} פריטים: " +
                  " · ".join(f"{e['item']}{qty_txt(e)}" for e in st["items"]), warn)


def _locate(st, item):
    items = st["items"]
    s = str(item).strip()
    if s.isdigit():
        i = int(s) - 1
        return i if 0 <= i < len(items) else None
    name = _defaults().lookup(s) or s
    idx = next((i for i, e in enumerate(items) if e["item"] == name or e["text"] == s), None)
    if idx is None:
        hits = [i for i, e in enumerate(items) if s in e["item"]]
        idx = hits[0] if len(hits) == 1 else None
    return idx


def remove_item(item):
    st, warn, _ = sync()
    idx = _locate(st, item)
    if idx is None:
        return _reply(f"לא נמצא ברשימה: {item}", warn)
    e = st["items"].pop(idx)
    w = _Writer(st, warn)
    if not e["task_ids"]:
        st["pending"] = [op for op in st["pending"]
                         if not (op["op"] == "add" and op["title"] == e.get("pending_title"))]
    for tid in e["task_ids"]:
        others = [o for o in st["items"] if tid in o["task_ids"]]
        try:
            if others:   # one phone task held several items ("חלב, ביצים"): keep the rest
                title = ", ".join(title_for(o["text"], o["qty"], o["qty_unit"]) for o in others)
                w.do(lambda s, t=tid, x=title: s.update_title(t, x),
                     {"op": "set_title", "task_id": tid, "title": title})
                continue
            w.do(lambda s, t=tid: s.remove(t), {"op": "remove", "task_id": tid})
        except ts.TasksNotFound:
            pass
        st["tasks"].pop(tid, None)
    _write(st)
    return _reply(f"הוסר: {e['item']} (ברשימה: {len(st['items'])})", w.warn)


def clear(undo=False):
    st, warn, _ = sync()
    w = _Writer(st, warn)
    if undo:
        ids = list(st["last_cleared"])
        if not ids:
            return _reply("אין ניקוי אחרון לשחזר", warn)
        for tid in ids:
            try:
                w.do(lambda s, t=tid: s.reopen(t), {"op": "reopen", "task_id": tid})
            except ts.TasksNotFound:
                pass
        st["last_cleared"] = []
        _write(st)
        if w.online:
            st, warn2, _ = sync()
            return _reply(f"שוחזרו {len(ids)} משימות (ברשימה: {len(st['items'])})", warn2)
        return _reply(f"השחזור של {len(ids)} משימות ממתין לסנכרון", w.warn)
    n = len(st["items"])
    ids = list(dict.fromkeys(t for e in st["items"] for t in e["task_ids"]))
    for tid in ids:
        try:
            w.do(lambda s, t=tid: s.complete(t), {"op": "complete", "task_id": tid})
        except ts.TasksNotFound:
            pass
    st["pending"] = [op for op in st["pending"] if op["op"] != "add"]
    st["items"], st["tasks"], st["last_cleared"] = [], {}, ids
    _write(st)
    return _reply(f"הרשימה נוקתה ({n} פריטים) — סומנו כבוצעו ב-Google Tasks, "
                  "אפשר לשחזר (grocery_clear undo=true)", w.warn)


def sync_tool():
    st, warn, replayed = sync()
    if warn:
        return f"{warn} ({len(st['items'])} פריטים, {len(st['pending'])} שינויים ממתינים)"
    if _cfg["backend"] != "tasks":
        return f"רשימה מקומית בלבד: {len(st['items'])} פריטים"
    extra = f", הועלו {replayed} שינויים ממתינים" if replayed else ""
    return f"סונכרן מ-Google Tasks: {len(st['items'])} פריטים פתוחים{extra}"
