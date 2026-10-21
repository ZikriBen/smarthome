import asyncio
import json

import pytest

from app.stream import cleanup, subs, watch
from app.stream.service import StreamError
from tests.fakes import CONTENT, make_service
from tests.mkvgen import make_mkv
from tests.test_watch import MOVIE


def _reader(data, cs=4096, log=None):
    async def fetch(i):
        if log is not None:
            log.append(i)
        return data[i * cs:(i + 1) * cs]

    return subs.ChunkReader(fetch, len(data), cs)


@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("cs", [64, 4096, 512 * 1024])
def test_extract_text_tracks(unknown, cs):
    data = make_mkv(unknown_cluster=unknown)
    tracks = asyncio.run(subs.extract(_reader(data, cs)))
    by = {(t.lang, t.forced): t for t in tracks}
    assert set(by) == {("heb", False), ("heb", True), ("eng", False)}  # PGS excluded
    assert [t.lang for t in tracks][0] == "heb" and not tracks[0].forced  # Hebrew full track first
    heb = subs.to_srt(by[("heb", False)].cues).decode()
    assert heb.startswith("1\n00:00:00,100 --> 00:00:01,600\nשורה 0\n")
    assert "6\n00:00:10,100 --> 00:00:11,600\nשורה 5\n" in heb
    eng = subs.to_srt(by[("eng", False)].cues).decode()
    assert "00:00:00,200 --> 00:00:02,200\nHello 0" in eng  # header stripping; no duration -> next cue
    assert not by[("heb", True)].cues


def test_no_text_tracks_stops_after_header():
    data = make_mkv(with_subs=False, clusters=200, video_block=8000)
    log = []
    assert asyncio.run(subs.extract(_reader(data, 4096, log))) == []
    assert len(log) == 1 and len(data) > 100 * 4096


def test_non_cluster_elements_skipped_without_fetching():
    data = make_mkv(attachment_bytes=3 * 1024 * 1024)
    log = []
    asyncio.run(subs.extract(_reader(data, 4096, log)))
    total = -(-len(data) // 4096)
    assert len(set(log)) < total - 700  # the 3 MB attachment (~768 chunks) never fetched


def test_bad_input():
    with pytest.raises(subs.MkvError):
        asyncio.run(subs.extract(_reader(b"\x00" * 100)))
    with pytest.raises(subs.MkvError):
        asyncio.run(subs.extract(_reader(make_mkv()[:100])))  # cut inside Tracks


def test_names_and_langs():
    t = subs.SubTrack(2, "S_TEXT/UTF8", "heb")
    assert subs.sidecar_name("A (2001) - 720p.strm", t) == "A (2001) - 720p.heb.srt"
    t.forced = True
    assert subs.sidecar_name("A (2001) - 720p.strm", t) == "A (2001) - 720p.heb.forced.srt"
    assert subs.norm_lang("he") == subs.norm_lang("iw") == subs.norm_lang("he-IL") == "heb"
    assert subs.norm_lang("") == "und" and subs.norm_lang("fre") == "fre" and subs.norm_lang("x1") == "und"


def test_to_srt_skips_empty_and_formats():
    out = subs.to_srt([(3_723_004_000_000, 1_000_000_000, b"b"), (0, None, b"  "), (1_000_000, None, "a\r\nb".encode())])
    assert out.decode() == "1\n00:00:00,001 --> 00:00:05,001\na\nb\n\n2\n01:02:03,004 --> 01:02:04,004\nb\n"


# ------------------------------------------------------------------ jobs
class FakeSvc:
    def __init__(self, data):
        self.data = data
        self.calls = 0

    async def location(self, vid):
        from app.stream.service import Location
        return Location(2, "loc", len(self.data), 0)

    async def get_chunk_background(self, vid, idx):
        self.calls += 1
        await asyncio.sleep(0)
        cs = 512 * 1024
        return self.data[idx * cs:(idx + 1) * cs]


def _entry(tmp_path):
    root = tmp_path / "streams"
    e, _ = watch.write_entry(root, MOVIE, MOVIE, "http://x/s/555/a", None)
    return root, e


def test_job_writes_sidecars_atomically_and_refreshes(tmp_path):
    root, e = _entry(tmp_path)
    done = []

    async def on_done(vid, folder, names):
        done.append((vid, names))

    async def scenario():
        jobs = subs.SubtitleJobs(FakeSvc(make_mkv()), marker_name=watch.MARKER, max_bytes=1 << 30,
                                 max_seconds=60, on_done=on_done, log=lambda m: None)
        st = jobs.schedule(555, {"size": 1000}, e.folder, e.strm.name, True)
        assert st["status"] == "queued"
        assert jobs.schedule(555, {"size": 1000}, e.folder, e.strm.name, True)["status"] in ("queued", "running")
        await asyncio.gather(*jobs.tasks.values())
        return jobs

    jobs = asyncio.run(scenario())
    st = jobs.public(555)
    assert st["status"] == "ready"
    names = st["files"]
    assert names == ["The Godfather Part II (1974) - 720p.heb.srt", "The Godfather Part II (1974) - 720p.eng.srt"]
    assert (e.folder / names[0]).read_text().startswith("1\n00:00:00,100 --> ")
    assert not list(e.folder.glob(".*.tmp"))
    marker = json.loads((e.folder / watch.MARKER).read_text())
    assert marker["subs"] == {"555": names} and marker["variants"] == {"555": e.strm.name}
    assert done == [(555, names)]
    # idempotent: already there -> ready without a second read
    jobs2 = subs.SubtitleJobs(FakeSvc(b""), marker_name=watch.MARKER, max_bytes=1 << 30, max_seconds=60)
    assert jobs2.existing(e.folder, 555) == names
    # re-pressing Watch now (write_entry again) keeps the subs bookkeeping
    watch.write_entry(root, MOVIE, MOVIE, "http://x/s/555/a2", None)
    assert json.loads((e.folder / watch.MARKER).read_text())["subs"] == {"555": names}


def test_job_states_unsupported_skipped_none_failed(tmp_path):
    _, e = _entry(tmp_path)

    class Boom(FakeSvc):
        async def get_chunk_background(self, vid, idx):
            raise StreamError(503, "Telegram rate limit", retry_after=30)

    async def scenario():
        mk = lambda svc: subs.SubtitleJobs(svc, marker_name=watch.MARKER, max_bytes=10_000_000, max_seconds=60,
                                           log=lambda m: None)
        j = mk(FakeSvc(make_mkv()))
        assert j.schedule(1, {"size": 5}, e.folder, e.strm.name, False)["status"] == "unsupported"
        assert j.schedule(2, {"size": 20_000_000}, e.folder, e.strm.name, True)["status"] == "skipped"
        j3 = mk(FakeSvc(make_mkv(with_subs=False)))
        j3.schedule(3, {"size": 5}, e.folder, e.strm.name, True)
        await asyncio.gather(*j3.tasks.values())
        assert j3.public(3)["status"] == "none"
        assert j3.schedule(3, {"size": 5}, e.folder, e.strm.name, True)["status"] == "none"  # cached, no re-read
        j4 = mk(Boom(make_mkv()))
        j4.schedule(4, {"size": 5}, e.folder, e.strm.name, True)
        await asyncio.gather(*j4.tasks.values())
        assert j4.public(4) == {"status": "failed", "detail": "Telegram rate limit"}
        assert j4.summary() == {"failed": 1}

    asyncio.run(scenario())


def test_jobs_run_one_at_a_time(tmp_path):
    _, e = _entry(tmp_path)
    running = []
    peak = [0]

    class Slow(FakeSvc):
        async def get_chunk_background(self, vid, idx):
            running.append(vid)
            peak[0] = max(peak[0], len(set(running)))
            await asyncio.sleep(0.001)
            running.remove(vid)
            return await super().get_chunk_background(vid, idx)

    async def scenario():
        j = subs.SubtitleJobs(Slow(make_mkv()), marker_name=watch.MARKER, max_bytes=1 << 30, max_seconds=60,
                              log=lambda m: None)
        for v in (7, 8, 9):
            j.schedule(v, {"size": 5}, e.folder, f"v{v}.strm", True)
        await asyncio.gather(*j.tasks.values())
        return j

    j = asyncio.run(scenario())
    assert peak[0] == 1 and all(j.public(v)["status"] == "ready" for v in (7, 8, 9))


# ------------------------------------------------------------------ priority lane
def test_background_reads_yield_to_foreground_and_skip_cache():
    gate = asyncio.Event
    svc = make_service()
    order = []
    real = svc.source.fetch

    async def fetch(dc, loc, offset, limit):
        order.append(("start", offset))
        if offset == 0:
            await svc._test_gate.wait()  # foreground fetch of chunk 0 is slow
        data = await real(dc, loc, offset, limit)
        order.append(("end", offset))
        return data

    svc.source.fetch = fetch

    async def scenario():
        svc._test_gate = gate()
        fg = asyncio.create_task(svc.get_chunk(10, 0))
        await asyncio.sleep(0.01)
        bg = asyncio.create_task(svc.get_chunk_background(10, 1))
        await asyncio.sleep(0.01)
        assert ("start", 512 * 1024) not in order  # waits while a foreground fetch is in flight
        svc._test_gate.set()
        a, b = await fg, await bg
        return a, b

    a, b = asyncio.run(scenario())
    assert a == CONTENT[:512 * 1024] and b == CONTENT[512 * 1024:1024 * 1024]
    assert order.index(("end", 0)) < order.index(("start", 512 * 1024))
    assert svc.cache.get((10, 1)) is None and svc.cache.get((10, 0)) is not None
    assert svc.stats["bg_chunks"] == 1 and svc.active == {}


def test_background_read_respects_floodwait_pause():
    svc = make_service()
    svc.paused_until = svc.clock() + 100
    with pytest.raises(StreamError):
        asyncio.run(svc.get_chunk_background(10, 1))


# ------------------------------------------------------------------ cleanup
def test_cleanup_removes_sidecars(tmp_path):
    root = tmp_path / "streams"
    e, _ = watch.write_entry(root, MOVIE, MOVIE, "http://x/s/555/a", None)
    e2, _ = watch.write_entry(root, MOVIE, {**MOVIE, "message_id": 556, "quality": "1080p"}, "http://x/s/556/b", None)
    s555 = e.folder / "The Godfather Part II (1974) - 720p.heb.srt"
    s556 = e.folder / "The Godfather Part II (1974) - 1080p.heb.srt"
    stray = e.folder / "The Godfather Part II (1974) - 720p.eng.srt"  # untracked (interrupted bookkeeping)
    for f in (s555, s556, stray):
        f.write_text("1\n")
    subs.record_sidecars(e.folder, watch.MARKER, 555, [s555.name])
    subs.record_sidecars(e.folder, watch.MARKER, 556, [s556.name])
    cleanup.main(["--variant", "555", "--apply", "--root", str(root), "--no-jellyfin"])
    assert not e.strm.exists() and not s555.exists() and not stray.exists()
    assert e2.strm.exists() and s556.exists()
    assert json.loads((e.folder / watch.MARKER).read_text())["subs"] == {"556": [s556.name]}


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_muxed_mkv_matches_ffmpeg_extraction(tmp_path):
    import subprocess

    (tmp_path / "heb.srt").write_text("1\n00:00:01,000 --> 00:00:02,500\nשלום עולם\n\n2\n00:00:03,000 --> 00:00:04,000\n"
                                      "שורה <i>שנייה</i>\nועוד\n", encoding="utf-8")
    mkv = tmp_path / "t.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-i", str(tmp_path / "heb.srt"), "-t", "5",
                    "-map", "0", "-map", "1", "-map", "2", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    "-c:s", "srt", "-metadata:s:s:0", "language=heb", str(mkv)], check=True)
    tracks = asyncio.run(subs.extract(_reader(mkv.read_bytes(), 4096)))
    assert [t.lang for t in tracks] == ["heb"]
    out = subs.to_srt(tracks[0].cues).decode()
    assert "00:00:01,000 --> 00:00:02,500\nשלום עולם" in out and "שורה <i>שנייה</i>\nועוד" in out
