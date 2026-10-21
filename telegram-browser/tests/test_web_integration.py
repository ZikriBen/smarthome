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
    monkeypatch.delenv("JELLYFIN_API_KEY", raising=False)
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


def test_watch_uses_env_key_by_reference(main_client, configured, monkeypatch):
    from tests.test_watch import FakeJellyfin

    srv = FakeJellyfin(appear_after=1, item_path="/media/telegram-streams/Film (2001)/Film (2001) - 720p.strm")
    monkeypatch.setenv("JELLYFIN_API_KEY", "env-dummy-key")
    monkeypatch.setattr(web, "JELLYFIN_URL", srv.url)
    try:
        data = main_client.post("/api/watch/555").json()
    finally:
        srv.close()
    assert data["status"] == "ready"
    assert srv.requests[0][2] == 'MediaBrowser Token="env-dummy-key"'


def test_watch_pending_has_message_and_status_endpoint(main_client, configured, monkeypatch):
    from tests.test_watch import FakeJellyfin

    srv = FakeJellyfin(mode="never", item_path="/media/telegram-streams/Film (2001)/Film (2001) - 720p.strm")
    monkeypatch.setenv("JELLYFIN_API_KEY", "env-dummy-key")
    monkeypatch.setattr(web, "JELLYFIN_URL", srv.url)
    monkeypatch.setattr(web, "JELLYFIN_POLL_TIMEOUT_S", 0.5)
    monkeypatch.setattr(web, "JELLYFIN_FALLBACK_AFTER_S", 0.1)
    monkeypatch.setattr(web, "JELLYFIN_LIBRARY", web.stream_watch.LibraryState(None, "/media/telegram-streams"))
    try:
        assert main_client.get("/api/watch/555").json()["status"] == "not_prepared"
        data = main_client.post("/api/watch/555").json()
        assert data["status"] == "pending" and data["message"] and "steps" in data
        assert main_client.get("/api/watch/555").json()["status"] == "pending"
        srv.visible = True
        st = main_client.get("/api/watch/555").json()
        assert st["status"] == "ready" and st["url"].endswith("/web/#/details?id=abc123")
        assert main_client.get("/api/watch/999").status_code == 404
    finally:
        srv.close()
    for r in srv.requests:
        assert r[2] == 'MediaBrowser Token="env-dummy-key"' and "env-dummy-key" not in r[1]
    sig = signing.sign(KEY, 555)
    assert sig not in json.dumps(data) and sig not in json.dumps(st)


def test_watch_schedules_subtitles_for_mkv(main_client, configured, monkeypatch):
    calls = []

    class Jobs:
        def schedule(self, vid, row, folder, strm_name, matroska):
            calls.append((vid, folder.name, strm_name, matroska))
            return {"status": "queued"}

        def public(self, vid):
            return {"status": "queued"}

        def boost(self, vid, seconds=15):
            calls.append(("boost", vid))

        async def wait_header(self, vid, timeout):
            calls.append(("wait_header", vid, timeout))
            return {"status": "queued"}

    monkeypatch.setattr(web, "subtitle_jobs", Jobs())
    data = main_client.post("/api/watch/555").json()
    assert data["status"] == "refresh_skipped" and data["subtitles"] == {"status": "queued"}
    # scheduled, boosted (popup open) and the header awaited BEFORE the Jellyfin refresh
    assert calls == [(555, "Film (2001)", "Film (2001) - 720p.strm", False), ("boost", 555),
                     ("wait_header", 555, web.TG_SUBS_HEADER_WAIT_S)]  # x.mp4 -> not Matroska
    main_client.get("/api/watch/555")
    assert calls[-1] == ("boost", 555)  # polling keeps the boost alive
    n = len(calls)
    monkeypatch.setattr(web, "TG_SUBS_ENABLED", False)
    main_client.post("/api/watch/555")
    assert len(calls) == n


def test_watch_double_tap_joins_inflight_job_and_reports_phase(configured, monkeypatch):
    import asyncio

    import httpx

    from tests.test_watch import FakeJellyfin

    srv = FakeJellyfin(mode="never", item_path="/media/telegram-streams/Film (2001)/Film (2001) - 720p.strm")
    monkeypatch.setenv("JELLYFIN_API_KEY", "env-dummy-key")
    monkeypatch.setattr(web, "JELLYFIN_URL", srv.url)
    monkeypatch.setattr(web, "JELLYFIN_POLL_TIMEOUT_S", 1.5)
    monkeypatch.setattr(web, "JELLYFIN_FALLBACK_AFTER_S", 5)
    monkeypatch.setattr(web, "JELLYFIN_LIBRARY", web.stream_watch.LibraryState("lib42", "/media/telegram-streams"))

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://t") as c:
            first = asyncio.ensure_future(c.post("/api/watch/555"))
            st = None
            for _ in range(50):
                await asyncio.sleep(0.05)
                st = (await c.get("/api/watch/555")).json()
                if st.get("in_progress"):
                    break
            second = await c.post("/api/watch/555")  # second tap joins the running job
            return (await first).json(), second.json(), st

    try:
        a, b, st = asyncio.run(go())
    finally:
        srv.close()
    assert st["in_progress"] and st["status"] == "pending" and st["phase"] in ("media_updated", "polling")
    assert "lib42" in st["library_url"]
    assert b["joined"] is True and a["joined"] is False
    assert a["status"] == b["status"] == "pending"
    assert a["library_url"].endswith("topParentId=lib42&collectionType=movies")
    reports = [r for r in srv.requests if r[1].startswith("/Library/Media/Updated")]
    assert len(reports) == 1  # one refresh cycle, not two
    assert not web._watch_inflight
