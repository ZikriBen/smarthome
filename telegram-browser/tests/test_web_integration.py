"""Main app (8788) vs stream listener (8789) separation, and /api/watch."""

import asyncio
import json
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

import app.web as web
from app.stream import signing
from app.stream.server import EmbeddedServer, build_stream_app
from tests.fakes import CONTENT, KEY, make_service


@pytest.fixture()
def main_client():
    return TestClient(web.app)  # no lifespan: no Telegram client is created


def test_main_app_never_serves_streams(main_client):
    sig = signing.sign(KEY, 10)
    for method in ("GET", "HEAD", "POST"):
        r = main_client.request(method, f"/s/10/{sig}")
        assert r.status_code == 404
    paths = {getattr(r, "path", "") for r in web.app.routes}
    assert "/s/{variant_id}/{sig}" not in paths


def test_stream_app_has_only_stream_routes():
    app = build_stream_app(make_service(), lambda: KEY)
    paths = sorted(getattr(r, "path", "") for r in app.routes)
    assert paths == ["/health", "/s/{variant_id}/{sig}"]


def test_watch_not_configured_503(main_client, monkeypatch):
    monkeypatch.setattr(web, "TG_STREAM_HMAC_KEY_FILE", "")
    assert main_client.post("/api/watch/1").status_code == 503


@pytest.fixture()
def configured(tmp_path, monkeypatch):
    keyf = tmp_path / "hmac.key"
    keyf.write_bytes(KEY)
    monkeypatch.setattr(web, "TG_STREAM_HMAC_KEY_FILE", str(keyf))
    monkeypatch.setattr(web, "TG_STREAM_PUBLIC_BASE", "http://<LAN_IP>:8789")
    monkeypatch.setattr(web, "TG_STREAMS_DIR", tmp_path / "streams")
    monkeypatch.setattr(web, "JELLYFIN_API_KEY_FILE", "")
    monkeypatch.setattr(web, "stream_service", None)
    rows = {
        555: {"message_id": 555, "details_message_id": 1, "quality": "720p", "filename": "x.mp4",
              "mime_type": "video/mp4", "size": 1, "title": "סרט", "english_title": "Film", "year": 2001,
              "imdb_rating": 7.5, "genres_json": "[]", "description": "d", "poster_message_id": None},
        556: {"message_id": 556, "mime_type": "application/zip", "details_message_id": 1, "title": "z", "year": 1},
    }
    monkeypatch.setattr(web.database, "get_variant_details", lambda vid: rows.get(vid))
    return tmp_path


def test_watch_writes_files_and_skips_refresh_without_key(main_client, configured):
    r = main_client.post("/api/watch/555")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "refresh_skipped" and data["folder"] == "Film (2001)" and data["written"] is True
    strm = configured / "streams" / "Film (2001)" / "Film (2001) - 720p.strm"
    assert strm.read_text() == f"http://<LAN_IP>:8789/s/555/{signing.sign(KEY, 555)}\n"
    assert main_client.post("/api/watch/555").json()["written"] is False
    assert main_client.post("/api/watch/556").status_code == 404  # non-media
    assert main_client.post("/api/watch/999").status_code == 404  # unknown


def test_watch_with_fake_jellyfin(main_client, configured, monkeypatch):
    from tests.test_watch import FakeJellyfin

    srv = FakeJellyfin(appear_after=1, item_path="/media/telegram-streams/Film (2001)/Film (2001) - 720p.strm")
    keyf = configured / "jf.key"
    keyf.write_text("fake-jf-key")
    monkeypatch.setattr(web, "JELLYFIN_API_KEY_FILE", str(keyf))
    monkeypatch.setattr(web, "JELLYFIN_URL", srv.url)
    monkeypatch.setattr(web, "JELLYFIN_PUBLIC_URL", "http://<LAN_IP>:8096")
    try:
        data = main_client.post("/api/watch/555").json()
    finally:
        srv.close()
    assert data["status"] == "ready" and data["url"] == "http://<LAN_IP>:8096/web/#/details?id=abc123"
    assert srv.requests[0][3] == {"Updates": [{"Path": "/media/telegram-streams/Film (2001)", "UpdateType": "Created"}]}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_embedded_server_real_socket_and_redacted_log(capfd):
    """Second listener in the same loop; real HTTP; signature never in access log."""
    port = _free_port()
    svc = make_service()

    async def scenario():
        server = EmbeddedServer(build_stream_app(svc, lambda: KEY), "127.0.0.1", port)
        server.start()
        for _ in range(100):
            if server.server.started:
                break
            await asyncio.sleep(0.05)
        sig = signing.sign(KEY, 10)
        async with httpx.AsyncClient() as c:
            r = await c.get(f"http://127.0.0.1:{port}/s/10/{sig}", headers={"Range": "bytes=0-99"})
            h = await c.get(f"http://127.0.0.1:{port}/health")
        await server.stop()
        return r, h, sig

    r, h, sig = asyncio.run(scenario())
    assert r.status_code == 206 and r.content == CONTENT[:100]
    assert h.json()["active_streams"] == 0
    out = capfd.readouterr()
    text = out.out + out.err
    assert sig not in text and "/s/10/<sig>" in text


def test_embedded_server_mid_stream_abort_closes_connection():
    from tests.fakes import FakeSource
    from app.stream.ranges import CHUNK_SIZE

    port = _free_port()
    src = FakeSource()
    src.fail_at_offset = 2 * CHUNK_SIZE
    svc = make_service(source=src)

    async def scenario():
        server = EmbeddedServer(build_stream_app(svc, lambda: KEY), "127.0.0.1", port)
        server.start()
        for _ in range(100):
            if server.server.started:
                break
            await asyncio.sleep(0.05)
        try:
            async with httpx.AsyncClient() as c:
                with pytest.raises(httpx.HTTPError):
                    r = await c.get(f"http://127.0.0.1:{port}/s/10/{signing.sign(KEY, 10)}")
                    r.raise_for_status()
                    assert len(r.content) == len(CONTENT)  # unreachable on abort
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert svc.active == {}
