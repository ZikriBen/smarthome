"""SearchGram delivery and Telegram Downloader status capabilities."""

import json
import os
import re
import threading
import time
import urllib.request

from . import approvals
from .config import BROWSER_URL, DOWNLOADER_URL
from .database import approval_lock, connection

_delivery_lock = threading.Lock()


def internal_json(method, base, path, payload=None):
    if not base:
        raise ValueError("internal media service is not configured")
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.load(response)


def result_page(session_id, page):
    items = page.get("items")
    if not isinstance(items, list):
        raise ValueError("SearchGram returned an invalid result page")
    return {"search_id": session_id, "query": page.get("query"), "page": page.get("page"),
            "total_pages": page.get("total_pages"), "total_results": page.get("total_results"),
            "results": [{"number": index + 1, "title": item.get("title"), "size": item.get("size")}
                        for index, item in enumerate(items)]}


def load_session(session_id):
    row = connection.execute(
        "SELECT page_json,created FROM searchgram_sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        raise ValueError("unknown SearchGram search; search again")
    if int(time.time()) - row[1] > 900:
        raise ValueError("SearchGram search expired; search again")
    return json.loads(row[0])


def save_session(session_id, page):
    connection.execute("INSERT OR REPLACE INTO searchgram_sessions VALUES (?,?,?,?)",
                       (session_id, page.get("query", ""), json.dumps(page), int(time.time())))
    connection.commit()


def search(query):
    query = str(query).strip()
    if not query:
        raise ValueError("search query is empty")
    page = internal_json("POST", BROWSER_URL, "/api/search", {"query": query})
    session_id = os.urandom(9).hex()
    save_session(session_id, page)
    return result_page(session_id, page)


def navigate(session_id, direction):
    page = load_session(session_id)
    current = page.get("page")
    if isinstance(current, bool) or not isinstance(current, int):
        raise ValueError("SearchGram returned an invalid current page")
    candidates = []
    for navigation in page.get("navigation", []):
        callback = navigation.get("callback_data", "")
        if not callback.startswith("search#"):
            continue
        try:
            target = int(callback.rsplit("#", 1)[1])
        except (IndexError, ValueError):
            continue
        if (direction == "next" and target > current) or (direction == "previous" and target < current):
            candidates.append((target, callback))
    if not candidates:
        raise ValueError(f"no {direction} SearchGram page is available")
    _, callback = min(candidates) if direction == "next" else max(candidates)
    result = internal_json("POST", BROWSER_URL, "/api/search/navigate", {
        "message_id": page["message_id"], "callback_data": callback, "query": page["query"]})
    save_session(session_id, result)
    return result_page(session_id, result)


def _finish_delivery(audit_id, status, error=None):
    with approval_lock:
        row = connection.execute("SELECT payload FROM approvals WHERE id=?", (audit_id,)).fetchone()
        detail = json.loads(row[0]) if row else {}
        if error is not None:
            detail["delivery_error"] = str(error)
            detail["delivery_http_status"] = getattr(error, "code", None)
        connection.execute("UPDATE approvals SET payload=?,status=? WHERE id=?",
                           (json.dumps(detail), status, audit_id))
        connection.commit()


def queue_result(session_id, result_number):
    page = load_session(session_id)
    if isinstance(result_number, bool) or not isinstance(result_number, int) or result_number < 1:
        raise ValueError("result number must be a positive integer")
    items = page.get("items", [])
    if result_number > len(items):
        raise ValueError("result number is not on the current SearchGram page")
    item = items[result_number - 1]
    audit_id = approvals.create("searchgram_download", {
        "query": page["query"], "title": item.get("title"), "size": item.get("size")},
        status="processing")

    def deliver():
        with _delivery_lock:
            try:
                internal_json("POST", BROWSER_URL, "/api/search/download", {
                    "message_id": page["message_id"], "callback_data": item["callback_data"]})
            except Exception as exc:
                _finish_delivery(audit_id, "failed", exc)
            else:
                _finish_delivery(audit_id, "executed")

    threading.Thread(target=deliver, daemon=True).start()
    return {"audit_id": audit_id,
            "queued_result": {"title": item.get("title"), "size": item.get("size")},
            "status": "processing", "follow_up_after_seconds": 30}


def delivery_status(audit_id, wait_seconds=0):
    if not isinstance(audit_id, str) or not re.fullmatch(r"[0-9a-f]{18}", audit_id):
        raise ValueError("a valid SearchGram delivery audit ID is required")
    if isinstance(wait_seconds, bool):
        raise ValueError("wait_seconds must be between 0 and 90")
    wait_seconds = max(0, min(int(wait_seconds), 90))
    deadline = time.monotonic() + wait_seconds
    while True:
        row = connection.execute(
            "SELECT payload,status,created FROM approvals WHERE id=? AND action='searchgram_download'",
            (audit_id,)).fetchone()
        if not row or row[1] != "processing" or time.monotonic() >= deadline:
            break
        time.sleep(2)
    if not row:
        raise ValueError("SearchGram delivery was not found")
    detail = json.loads(row[0])
    delivery = {"audit_id": audit_id, "title": detail.get("title"), "size": detail.get("size"),
                "status": row[1], "created": row[2], "error": detail.get("delivery_error"),
                "http_status": detail.get("delivery_http_status")}
    return {"delivery": delivery, "downloader": internal_json("GET", DOWNLOADER_URL, "/status")}


def download_status():
    deliveries = []
    for audit_id, payload, status, created in connection.execute(
            "SELECT id,payload,status,created FROM approvals WHERE action='searchgram_download' "
            "ORDER BY created DESC LIMIT 10"):
        detail = json.loads(payload)
        deliveries.append({"audit_id": audit_id, "title": detail.get("title"),
                           "size": detail.get("size"), "status": status, "created": created,
                           "error": detail.get("delivery_error"),
                           "http_status": detail.get("delivery_http_status")})
    return {"downloader": internal_json("GET", DOWNLOADER_URL, "/status"),
            "recent_searchgram_deliveries": deliveries}


def register_routes(router):
    router.get("/v1/media/download-status", lambda _p, _q: download_status())
    router.post("/v1/searchgram/search", lambda p, _q: search(p["query"]))
    router.post("/v1/searchgram/next-page", lambda p, _q: navigate(p["search_id"], "next"))
    router.post("/v1/searchgram/previous-page",
                lambda p, _q: navigate(p["search_id"], "previous"))
    router.post("/v1/searchgram/queue",
                lambda p, _q: queue_result(p["search_id"], p["result_number"]), 202)
    router.post("/v1/searchgram/delivery-status",
                lambda p, _q: delivery_status(p["audit_id"], p.get("wait_seconds", 0)))
