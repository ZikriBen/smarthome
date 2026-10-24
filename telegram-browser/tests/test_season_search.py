"""Season scan (app/season_search.py), POST /api/search/season and the earlier-page download fallback.
Offline: a fake SearchGram service replays synthetic pages; no Telegram."""

import asyncio
import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.web as web
from app import season_search as ss

FIX = json.loads((Path(__file__).resolve().parent / "fixtures" / "season" / "searchgram_pages.json").read_text())
EPS10 = [{"n": n, "runtime": 45} for n in range(1, 11)]


class FakeSearchGram:
    def __init__(self, scenarios=("full_3_pages", "short_variant", "english_variant"), fail=None):
        self.pages = {FIX[s]["query"]: copy.deepcopy(FIX[s]["pages"]) for s in scenarios}
        self.fail = dict(fail or {})        # query -> exception to raise on search
        self.nav_fail = set()               # (query, page) -> raise TimeoutError on navigate
        self.calls = []
        self.media_errors = []
        self.media_calls = []

    async def search(self, query):
        self.calls.append(("search", query))
        if query in self.fail:
            raise self.fail[query]
        pages = self.pages.get(query)
        if not pages:
            return {"query": query, "message_id": 9999, "page": 1, "total_pages": 1, "items": [], "navigation": []}
        return copy.deepcopy(pages[0])

    async def navigate(self, *, message_id, callback_data, query):
        target = int(callback_data.rsplit("#", 1)[1])
        self.calls.append(("navigate", query, target))
        if (query, target) in self.nav_fail:
            raise TimeoutError("SearchGram page navigation timed out")
        return copy.deepcopy(self.pages[query][target - 1])

    async def request_media(self, *, message_id, callback_data):
        self.media_calls.append((message_id, callback_data))
        if self.media_errors:
            raise self.media_errors.pop(0)
        return type("Media", (), {"chat_id": 1, "message_id": 2, "filename": "f.mkv", "file_size": 3})()


async def nosleep(_):
    return None


def run(coro):
    return asyncio.run(coro)


def test_query_variants_hebrew_first():
    assert ss.build_queries(["Money Heist", "בית הנייר", "money  heist"], 1) == [
        "בית הנייר עונה 1", "בית הנייר ע1", "Money Heist S01"]
    assert ss.build_queries(["", "   "], 2) == []


def test_scan_pages_and_keeps_every_page_addressable():
    fake = FakeSearchGram()
    out = run(ss.scan(fake, ["בית הנייר עונה 1"], sleep=nosleep))
    assert out["pages_fetched"] == 3 and not out["truncated"]
    assert ("navigate", "בית הנייר עונה 1", 2) in fake.calls and ("navigate", "בית הנייר עונה 1", 3) in fake.calls
    first = [i for i in out["items"] if i["page"] == 1][0]
    third = [i for i in out["items"] if i["page"] == 3][0]
    # page 1 items carry the real 'previous' callback seen on page 2; later pages the button that opened them
    assert first["page_callback"] == "search#בית הנייר עונה 1#1" and first["message_id"] == 7001
    assert third["page_callback"] == "search#בית הנייר עונה 1#3"


def test_page_caps_truncate():
    out = run(ss.scan(FakeSearchGram(), ["בית הנייר עונה 1"], sleep=nosleep, max_pages_per_query=2))
    assert out["pages_fetched"] == 2 and out["truncated"]
    out = run(ss.scan(FakeSearchGram(), ["בית הנייר עונה 1", "בית הנייר ע1"], sleep=nosleep, max_pages_total=3))
    assert out["queries_attempted"] == ["בית הנייר עונה 1"] and out["truncated"]


def test_budget_truncates():
    ticks = iter([0, 0, 500, 500, 500, 500, 500])
    out = run(ss.scan(FakeSearchGram(), ["בית הנייר עונה 1"], sleep=nosleep, clock=lambda: next(ticks),
                      budget_seconds=10))
    assert out["truncated"] and out["pages_fetched"] == 1


def test_find_season_stops_when_complete_and_ranks():
    fake = FakeSearchGram()
    out = run(ss.find_season(fake, titles=["בית הנייר", "Money Heist"], season=1, episodes=EPS10, sleep=nosleep))
    assert out["queries_ok"] == ["בית הנייר עונה 1", "בית הנייר ע1"]       # E10 found by the 2nd; English not needed
    assert ("search", "Money Heist S01") not in fake.calls
    assert out["missing"] == [] and len(out["picks"]) == 10
    assert out["pack"] and out["pack"]["id"] not in {p["id"] for p in out["picks"]}


def test_first_variant_timeout_falls_through():
    fake = FakeSearchGram(fail={"בית הנייר עונה 1": TimeoutError("Timed out waiting for SearchGram results")})
    out = run(ss.find_season(fake, titles=["בית הנייר"], season=1, episodes=EPS10, sleep=nosleep))
    assert out["errors"] == [{"query": "בית הנייר עונה 1", "error": "timeout"}]
    assert out["queries_ok"] == ["בית הנייר ע1"] and [p["ep"] for p in out["picks"]] == [10]


def test_navigation_failure_keeps_partial_results():
    fake = FakeSearchGram()
    fake.nav_fail.add(("בית הנייר עונה 1", 2))
    out = run(ss.scan(fake, ["בית הנייר עונה 1"], sleep=nosleep))
    assert out["truncated"] and out["pages_fetched"] == 1 and out["errors"][0]["error"] == "timeout"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(ss, "PAGE_DELAY_SECONDS", 0)
    return TestClient(web.app)          # no lifespan: no Telegram client


def test_endpoint_happy_path(client, monkeypatch):
    monkeypatch.setattr(web, "searchgram", FakeSearchGram())
    r = client.post("/api/search/season", json={"titles": ["בית הנייר"], "season": 1, "episodes": EPS10, "have": [2]})
    assert r.status_code == 200
    body = r.json()
    assert 2 not in [p["ep"] for p in body["picks"]] and body["needed"] == [1, 3, 4, 5, 6, 7, 8, 9, 10]
    cand = {c["id"]: c for c in body["candidates"]}
    assert all(cand[p["id"]]["message_id"] and cand[p["id"]]["callback_data"] for p in body["picks"])
    assert body["config"]["min_res"] == 720


def test_endpoint_unknown_list(client, monkeypatch):
    monkeypatch.setattr(web, "searchgram", FakeSearchGram())
    body = client.post("/api/search/season", json={"titles": ["בית הנייר"], "season": 1, "episodes": None}).json()
    assert body["observed"][:3] == [1, 2, 3] and 20 in body["observed"]


def test_endpoint_failures(client, monkeypatch):
    monkeypatch.setattr(web, "searchgram", None)
    assert client.post("/api/search/season", json={"titles": ["x"], "season": 1}).status_code == 503
    monkeypatch.setattr(web, "searchgram", FakeSearchGram())
    for bad in ({"titles": [], "season": 1}, {"titles": ["x"], "season": "1"}, {"titles": ["x"], "season": True},
                {"titles": ["x"]}, {"titles": ["x"], "season": 1, "episodes": [{"n": "a"}]}):
        assert client.post("/api/search/season", json=bad).status_code == 400
    down = FakeSearchGram(fail={q: ConnectionError("down") for q in ("בית הנייר עונה 1", "בית הנייר ע1")})
    monkeypatch.setattr(web, "searchgram", down)
    r = client.post("/api/search/season", json={"titles": ["בית הנייר"], "season": 1, "episodes": EPS10})
    assert r.status_code == 502


@pytest.fixture()
def downloader(monkeypatch):
    sent = []

    async def fake_post(path, payload):
        sent.append((path, payload))
        return 202, {"status": "queued"}
    monkeypatch.setattr(web, "downloader_post", fake_post)
    return sent


def test_download_earlier_page_fallback(client, monkeypatch, downloader):
    fake = FakeSearchGram()
    fake.media_errors = [RuntimeError("SearchGram callback did not return a URL")]
    monkeypatch.setattr(web, "searchgram", fake)
    r = client.post("/api/search/download", json={"message_id": 7001, "callback_data": "dl_x",
                                                  "query": "בית הנייר עונה 1",
                                                  "page_callback": "search#בית הנייר עונה 1#1"})
    assert r.status_code == 200
    assert fake.media_calls == [(7001, "dl_x"), (7001, "dl_x")]
    assert ("navigate", "בית הנייר עונה 1", 1) in fake.calls and len(downloader) == 1


@pytest.mark.parametrize("error,payload,status", [
    (RuntimeError("SearchGram callback did not return a URL"), {}, 502),                 # no page info: no retry
    (RuntimeError("other failure"), {"query": "q", "page_callback": "search#q#1"}, 502),   # not a callback refusal
    (TimeoutError("Timed out waiting for SearchGram media delivery"),
     {"query": "q", "page_callback": "search#q#1"}, 504),                                  # may have been sent
])
def test_download_no_retry_cases(client, monkeypatch, downloader, error, payload, status):
    fake = FakeSearchGram()
    fake.media_errors = [error]
    monkeypatch.setattr(web, "searchgram", fake)
    r = client.post("/api/search/download", json={"message_id": 7001, "callback_data": "dl_x", **payload})
    assert r.status_code == status and len(fake.media_calls) == 1 and downloader == []
