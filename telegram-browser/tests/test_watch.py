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
    def __init__(self, appear_after=2, item_path="/media/telegram-streams/F/F - 720p.strm"):
        self.requests = []
        self.appear_after = appear_after
        self.item_path = item_path
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _rec(self, body=None):
                outer.requests.append((self.command, self.path, self.headers.get("Authorization"), body))

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self._rec(json.loads(self.rfile.read(n)) if n else None)
                self.send_response(204)
                self.end_headers()

            def do_GET(self):
                self._rec()
                gets = sum(1 for r in outer.requests if r[0] == "GET")
                items = []
                if gets >= outer.appear_after:
                    items = [{"Id": "other", "Path": "/media/movies/F/F.mkv"},
                             {"Id": "abc123", "Path": outer.item_path, "MediaSources": []}]
                data = json.dumps({"Items": items}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def _run(jf_srv, **kw):
    t = [0.0]

    async def sleep(s):
        t[0] += s

    jf = watch.JellyfinClient(jf_srv.url, "fake-test-key")
    return asyncio.run(watch.refresh_and_find(
        jf, strm_path="/media/telegram-streams/F/F - 720p.strm", folder_path="/media/telegram-streams/F",
        search_term="F", public_url="http://jellyfin.example:8096", sleep=sleep, clock=lambda: t[0], **kw))


def test_refresh_targeted_and_poll_finds_item():
    srv = FakeJellyfin(appear_after=3)
    try:
        out = _run(srv, library_id=None)
    finally:
        srv.close()
    assert out == {"status": "ready", "item_id": "abc123", "url": "http://jellyfin.example:8096/web/#/details?id=abc123"}
    method, path, auth, body = srv.requests[0]
    assert (method, path) == ("POST", "/Library/Media/Updated")
    assert body == {"Updates": [{"Path": "/media/telegram-streams/F", "UpdateType": "Created"}]}
    assert auth == 'MediaBrowser Token="fake-test-key"'
    gets = [r for r in srv.requests if r[0] == "GET"]
    assert len(gets) == 3 and all(r[1].startswith("/Items?") and "Recursive=true" in r[1] for r in gets)
    assert not any("/Refresh" in r[1] for r in srv.requests)  # never a full scan


def test_poll_timeout_pending_with_one_library_fallback():
    srv = FakeJellyfin(appear_after=10**6)
    try:
        out = _run(srv, library_id="lib42", timeout=45, poll_interval=2)
    finally:
        srv.close()
    assert out["status"] == "pending"
    refreshes = [r for r in srv.requests if "/Refresh" in r[1]]
    assert len(refreshes) == 1 and refreshes[0][1].startswith("/Items/lib42/Refresh?Recursive=true")
    gets = [r for r in srv.requests if r[0] == "GET"]
    assert 20 <= len(gets) <= 25 and all("ParentId=lib42" in r[1] for r in gets)


def test_refresh_unreachable():
    jf = watch.JellyfinClient("http://127.0.0.1:9", "k", timeout=1)
    out = asyncio.run(watch.refresh_and_find(jf, strm_path="/x", folder_path="/", search_term="x",
                                             library_id=None, public_url="http://j"))
    assert out["status"] == "refresh_failed"


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
