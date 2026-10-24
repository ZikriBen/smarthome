import asyncio
import json
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.stream import watch

MOVIE = {
    "message_id": 555, "quality": "720p", "title": "הסנדק", "english_title": "The Godfather: Part/II",
    "year": 1974, "imdb_rating": 9.0, "genres_json": json.dumps(["דרמה", "פשע"]),
    "description": "Plot <with> & specials", "mime_type": "video/mp4",
}


@pytest.mark.parametrize("raw,expected", [
    ("הסנדק", "הסנדק"), ("a/b\\c", "a b c"), ("..", "Untitled"), ("", "Untitled"), (None, "Untitled"),
    ('x:*?"<>|y', "x y"), ("  trailing. ", "trailing"), ("tab\tnew\nline", "tab new line"), ("\u202eevil", "evil"),
])
def test_sanitize(raw, expected):
    assert watch.sanitize_component(raw) == expected


def test_sanitize_length_in_bytes():
    out = watch.sanitize_component("א" * 500)
    assert len(out.encode()) <= watch.MAX_NAME_BYTES


def test_entry_paths_cannot_escape(tmp_path):
    e = watch.entry_paths(tmp_path, {"english_title": "../../etc", "year": 2000}, {"quality": "../720p"})
    assert e.folder.parent == tmp_path and "/" not in e.strm.name


def test_write_entry_atomic_idempotent(tmp_path):
    root = tmp_path / "streams"
    entry, changed = watch.write_entry(root, MOVIE, MOVIE, "http://<LAN_IP>:8789/s/555/abc", b"JPEG")
    assert changed and entry.folder.name == "The Godfather Part II (1974)"
    assert entry.strm.name == "The Godfather Part II (1974) - 720p.strm"
    assert entry.strm.read_text() == "http://<LAN_IP>:8789/s/555/abc\n"
    assert entry.poster.read_bytes() == b"JPEG"
    assert json.loads((entry.folder / watch.MARKER).read_text()) == {"variants": {"555": entry.strm.name}}
    assert not list(root.glob(".tmp-*"))
    nfo = ET.fromstring(entry.nfo.read_bytes())
    assert nfo.findtext("title") == "הסנדק" and nfo.findtext("originaltitle") == MOVIE["english_title"]
    assert nfo.findtext("year") == "1974" and nfo.findtext("rating") == "9.0"
    assert [g.text for g in nfo.findall("genre")] == ["דרמה", "פשע"]
    assert nfo.findtext("plot") == "Plot <with> & specials" and nfo.findtext("lockdata") == "true"
    _, changed2 = watch.write_entry(root, MOVIE, MOVIE, "http://<LAN_IP>:8789/s/555/abc", b"JPEG")
    assert changed2 is False
    _, changed3 = watch.write_entry(root, MOVIE, MOVIE, "http://<LAN_IP>:8789/s/555/new", None)
    assert changed3 and entry.strm.read_text().endswith("/new\n")
    other = {**MOVIE, "message_id": 556, "quality": "1080p"}
    e2, _ = watch.write_entry(root, MOVIE, other, "http://x/s/556/y", None)
    assert e2.folder == entry.folder and e2.strm.exists() and entry.strm.exists()
    assert set(json.loads((entry.folder / watch.MARKER).read_text())["variants"]) == {"555", "556"}
    assert not [p for p in entry.folder.iterdir() if p.name.endswith(".tmp")]


def test_jellyfin_path(tmp_path):
    p = tmp_path / "A (2000)" / "A (2000) - 720p.strm"
    assert watch.jellyfin_path(p, tmp_path, "/media/telegram-streams") == "/media/telegram-streams/A (2000)/A (2000) - 720p.strm"


class FakeJellyfin:
    """Fake Jellyfin 12.1. ``mode`` = what makes the new item visible:
    'poll'  : the n-th GET /Items (targeted Media/Updated works),
    'library': only POST /Items/{lib}/Refresh (the NUC field result),
    'full'  : only POST /Library/Refresh (library created while empty),
    'never' : nothing.
    Rejects X-Emby-Token and any key in the query string with 401."""

    def __init__(self, appear_after=2, item_path="/media/telegram-streams/F/F - 720p.strm", mode="poll",
                 lib_id="lib42", lib_path="/media/telegram-streams", items_status=200, media_updated_status=204,
                 on_request=None):
        self.requests = []
        self.appear_after = appear_after
        self.item_path = item_path
        self.mode = mode
        self.visible = False
        self.items_status = items_status
        self.media_updated_status = media_updated_status
        self.folders = [{"Name": "Movies", "Locations": ["/media/movies"], "ItemId": "movies1"}]
        if lib_id:
            self.folders.append({"Name": "Telegram Streams", "Locations": [lib_path + "/"], "ItemId": lib_id})
        self.on_request = on_request
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _rec(self, body=None):
                outer.requests.append((self.command, self.path, self.headers.get("Authorization"), body,
                                       self.headers.get("X-Emby-Token")))
                if outer.on_request:
                    outer.on_request(self.command, self.path)

            def _send(self, code, obj=None):
                data = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code)
                if data:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self):
                auth = self.headers.get("Authorization") or ""
                return (auth.startswith("MediaBrowser Token=") and not self.headers.get("X-Emby-Token")
                        and "api_key" not in self.path.lower())

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self._rec(json.loads(self.rfile.read(n)) if n else None)
                if not self._authorized():
                    return self._send(401)
                if self.path.startswith("/Library/Media/Updated"):
                    return self._send(outer.media_updated_status)
                if self.path.startswith("/Library/Refresh") and outer.mode == "full":
                    outer.visible = True
                if self.path.startswith(f"/Items/{lib_id}/Refresh") and outer.mode == "library":
                    outer.visible = True
                self._send(204)

            def do_GET(self):
                self._rec()
                if not self._authorized():
                    return self._send(401)
                if self.path.startswith("/Library/VirtualFolders"):
                    return self._send(200, outer.folders)
                if self.path.startswith("/Users") and not self.path.startswith("/Users/"):
                    return self._send(200, [{"Id": "u-plain", "Policy": {}}, {"Id": "u-admin", "Policy": {"IsAdministrator": True}}])
                if self.path.startswith("/Items") and "Limit=0" in self.path:
                    return self._send(200, {"Items": [], "TotalRecordCount": 1 if outer.visible else 0})
                if self.path.startswith("/Items") and outer.items_status != 200:
                    return self._send(outer.items_status, {"detail": "no user"})
                gets = sum(1 for r in outer.requests if r[0] == "GET" and "/Items" in r[1] and "Limit=0" not in r[1])
                if outer.mode == "poll" and gets >= outer.appear_after:
                    outer.visible = True
                items = [{"Id": "other", "Path": "/media/movies/F/F.mkv"}]
                if outer.visible:
                    items.append({"Id": "abc123", "Path": outer.item_path})
                self._send(200, {"Items": items, "TotalRecordCount": len(items)})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()

    def paths(self, method=None):
        return [r[1] for r in self.requests if method is None or r[0] == method]


def _run(jf_srv, t=None, **kw):
    t = t if t is not None else [0.0]

    async def sleep(s):
        t[0] += s

    jf = watch.JellyfinClient(jf_srv.url, "fake-test-key")
    kw.setdefault("library_id", None)
    return asyncio.run(watch.refresh_and_find(
        jf, strm_path="/media/telegram-streams/F/F - 720p.strm", folder_path="/media/telegram-streams/F",
        search_term="F", public_url="http://jellyfin.example:8096", sleep=sleep, clock=lambda: t[0], **kw))


def _assert_auth_only_header(srv):
    assert srv.requests, "no requests"
    for method, path, auth, _, emby in srv.requests:
        assert auth == 'MediaBrowser Token="fake-test-key"', (method, path)
        assert emby is None and "fake-test-key" not in path and "api_key" not in path.lower()


def test_refresh_targeted_and_poll_finds_item():
    srv = FakeJellyfin(appear_after=3)
    try:
        out = _run(srv)
    finally:
        srv.close()
    assert out["status"] == "ready" and out["item_id"] == "abc123"
    assert out["url"] == "http://jellyfin.example:8096/web/#/details?id=abc123"
    method, path, auth, body, _ = srv.requests[0]
    assert (method, path) == ("POST", "/Library/Media/Updated")
    assert body == {"Updates": [{"Path": "/media/telegram-streams/F", "UpdateType": "Created"}]}
    assert not any("/Refresh" in p for p in srv.paths())  # found before the fallback: no scan
    _assert_auth_only_header(srv)


def test_field_case_media_updated_ignored_library_refresh_surfaces_item():
    """NUC 2026-10-21: Media/Updated never scanned; /Items/{LIB}/Refresh did in ~5 s."""
    srv = FakeJellyfin(mode="library")
    try:
        out = _run(srv, library_id="lib42")
    finally:
        srv.close()
    assert out["status"] == "ready" and out["steps"][:2] == ["media_updated", "library_refresh"]
    assert 6 <= out["elapsed_s"] <= 9  # fallback at ~6 s, then the next poll finds it
    assert [p for p in srv.paths("POST") if "/Refresh" in p][0].startswith(
        "/Items/lib42/Refresh?Recursive=true&MetadataRefreshMode=Default&ImageRefreshMode=Default")
    assert "/Library/Refresh" not in srv.paths()  # library not empty after the refresh: no full scan
    assert not any(p.startswith("/Library/VirtualFolders") for p in srv.paths())  # env id: no discovery
    _assert_auth_only_header(srv)


def test_library_id_discovered_by_path_and_cached():
    srv = FakeJellyfin(mode="library", lib_id="d1sc0")
    lib = watch.LibraryState(None, "/media/telegram-streams")
    try:
        out1 = _run(srv, library=lib)
        out2 = _run(srv, library=lib)
    finally:
        srv.close()
    assert out1["status"] == "ready" and out2["status"] == "ready"
    assert lib.discovered_id == "d1sc0"
    assert sum(p.startswith("/Library/VirtualFolders") for p in srv.paths()) == 1
    assert any(p.startswith("/Items/d1sc0/Refresh") for p in srv.paths("POST"))
    assert any("ParentId=d1sc0" in p for p in srv.paths("GET"))


def test_empty_library_gets_one_full_refresh_with_cooldown():
    srv = FakeJellyfin(mode="full")
    lib = watch.LibraryState("lib42", "/media/telegram-streams", full_refresh_cooldown=900)
    try:
        out1 = _run(srv, library=lib)
        srv.visible = False  # a second new title, still within the cooldown
        out2 = _run(srv, library=lib, timeout=20)
    finally:
        srv.close()
    assert out1["status"] == "ready" and "full_refresh:empty_library" in out1["steps"]
    assert out2["status"] == "pending" and "full_refresh:cooldown" in out2["steps"]
    assert srv.paths("POST").count("/Library/Refresh") == 1


def test_no_library_found_last_resort_full_refresh():
    srv = FakeJellyfin(mode="full", lib_id=None)
    try:
        out = _run(srv)
    finally:
        srv.close()
    assert out["status"] == "ready"
    assert "library_id:unknown" in out["steps"] and "full_refresh:no_library_id" in out["steps"]


def test_never_appears_pending_with_message_and_bounded():
    srv = FakeJellyfin(mode="never")
    try:
        out = _run(srv, library_id="lib42", timeout=45, poll_interval=1.5)
    finally:
        srv.close()
    assert out["status"] == "pending" and out["message"] and out["elapsed_s"] <= 46
    refreshes = [p for p in srv.paths("POST") if "Refresh" in p]
    assert len([p for p in refreshes if p.startswith("/Items/lib42/Refresh")]) == 1
    assert refreshes.count("/Library/Refresh") == 1  # empty library -> once, not every poll


def test_request_time_counts_against_the_budget():
    """The field run took ~80 s for a 45 s budget: slow calls must not extend it."""
    t = [0.0]

    def slow(method, path):
        t[0] += 7.0  # every request 'takes' 7 s

    srv = FakeJellyfin(mode="never", on_request=slow)
    try:
        out = _run(srv, t=t, library_id="lib42", timeout=45)
    finally:
        srv.close()
    assert out["status"] == "pending" and t[0] <= 45 + 3 * 7  # at most the calls in flight at the deadline


def test_items_without_user_refused_falls_back_to_admin_user():
    srv = FakeJellyfin(appear_after=1, items_status=400)
    try:
        out = _run(srv)
    finally:
        srv.close()
    # /Items refused -> /Users (admin picked) -> /Users/u-admin/Items
    assert any(p.startswith("/Users/u-admin/Items?") for p in srv.paths("GET"))
    assert out["status"] == "ready"


def test_second_quality_matched_by_folder():
    srv = FakeJellyfin(appear_after=1, item_path="/media/telegram-streams/F/F - 1080p.strm")
    try:
        out = _run(srv)
    finally:
        srv.close()
    assert out["status"] == "ready" and out["item_id"] == "abc123"


def test_auth_failed_is_reported():
    srv = FakeJellyfin()
    try:
        jf = watch.JellyfinClient(srv.url, "fake-test-key")
        srv.media_updated_status = 401  # revoked/bad key
        out = asyncio.run(watch.refresh_and_find(jf, strm_path="/x", folder_path="/media/telegram-streams/F",
                                                 search_term="x", public_url="http://j"))
    finally:
        srv.close()
    assert out["status"] == "auth_failed" and "401" in out["message"]


def test_refresh_unreachable():
    jf = watch.JellyfinClient("http://127.0.0.1:9", "k", timeout=1)
    out = asyncio.run(watch.refresh_and_find(jf, strm_path="/x", folder_path="/", search_term="x",
                                             library_id=None, public_url="http://j"))
    assert out["status"] == "refresh_failed" and out["message"]


def test_refresh_item_and_full_refresh_endpoints():
    srv = FakeJellyfin()
    jf = watch.JellyfinClient(srv.url, "fake-test-key")
    try:
        asyncio.run(jf.refresh_item("it 1"))
        asyncio.run(jf.refresh_all())
    finally:
        srv.close()
    assert srv.paths("POST")[0].startswith("/Items/it%201/Refresh?Recursive=false&MetadataRefreshMode=Default")
    assert srv.paths("POST")[1] == "/Library/Refresh"
    _assert_auth_only_header(srv)


def test_read_secret_file(tmp_path):
    f = tmp_path / "k"
    f.write_text("  abc \n")
    assert watch.read_secret_file(str(f)) == "abc"
    assert watch.read_secret_file(str(tmp_path / "none")) is None
    assert watch.read_secret_file("") is None


def test_jellyfin_api_key_sources(tmp_path):
    f = tmp_path / "k"
    f.write_text("from-file\n")
    assert watch.jellyfin_api_key({"JELLYFIN_API_KEY_FILE": str(f), "JELLYFIN_API_KEY": "from-env"}) == "from-file"
    assert watch.jellyfin_api_key({"JELLYFIN_API_KEY_FILE": str(tmp_path / "missing"), "JELLYFIN_API_KEY": " from-env "}) == "from-env"
    assert watch.jellyfin_api_key({"JELLYFIN_API_KEY": ""}) is None
    assert watch.jellyfin_api_key({}) is None
