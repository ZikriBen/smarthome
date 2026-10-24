"""Season finder (command_center.season + season_sources): episode-list source order (TMDb primary when a key is
set, by Jellyfin provider id; Jellyfin library; TVmaze strictly by ids; name search -> candidates only), statuses,
queueing (idempotent, cross-page, packs refused without allow_pack, skip already-have), failures and secret
hygiene. Offline: recorded TMDb/TVmaze JSON (auth stripped), synthetic Jellyfin JSON, and the real telegram-browser
ranking (app/season_parse.py) over the synthetic SearchGram pages. Credentials are canaries, never real."""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

os.environ.setdefault("COMMAND_CENTER_TOKEN", "test")
HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parents[1]
FIX = HERE / "tests" / "fixtures" / "season"
TGB_FIX = REPO / "telegram-browser" / "tests" / "fixtures" / "season" / "searchgram_pages.json"
sys.path.insert(0, str(HERE))
if "command_center.database" not in sys.modules:
    fake_db = types.ModuleType("command_center.database")
    fake_db.connection = sqlite3.connect(":memory:", check_same_thread=False)
    fake_db.approval_lock = threading.Lock()
    sys.modules["command_center.database"] = fake_db
_db = sys.modules["command_center.database"]
if not hasattr(_db, "approval_lock"):
    _db.approval_lock = threading.Lock()
_db.connection.execute("CREATE TABLE IF NOT EXISTS searchgram_sessions (id TEXT PRIMARY KEY, query TEXT NOT NULL, "
                       "page_json TEXT NOT NULL, created INTEGER NOT NULL)")
_db.connection.execute("CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, action TEXT NOT NULL, "
                       "payload TEXT NOT NULL, status TEXT NOT NULL, created INTEGER NOT NULL)")
from command_center import jellyfin, searchgram, season, season_sources as src  # noqa: E402
import importlib.util  # noqa: E402

# Real approvals.create bound to this test's database, whatever other test modules stubbed in sys.modules.
_ap_src = (HERE / "command_center" / "approvals.py").read_text().replace(
    "from .database import approval_lock, connection", "approval_lock, connection = _db.approval_lock, _db.connection")
REAL_APPROVALS = types.ModuleType("approvals_for_season_tests")
REAL_APPROVALS._db = _db
exec(compile(_ap_src, "approvals.py", "exec"), REAL_APPROVALS.__dict__)

_spec = importlib.util.spec_from_file_location(  # telegram-browser's pure ranking module (no Telegram imports)
    "tgb_season_parse", REPO / "telegram-browser" / "app" / "season_parse.py")
season_parse = importlib.util.module_from_spec(_spec)
sys.modules["tgb_season_parse"] = season_parse
_spec.loader.exec_module(season_parse)

CANARY_V4 = "eyJcanaryV4TokenNotReal." + "A" * 40
CANARY_V3 = "c0ffee" * 5 + "ab"          # 32 chars, v3-shaped, fake


def fx(name):
    return json.loads((FIX / name).read_text())


TMDB = {"/tv/71446": "tmdb_tv_71446_he.json", "/tv/71446/season/1": "tmdb_tv_71446_season_1.json",
        "/tv/100088": "tmdb_tv_100088_he.json", "/tv/100088/season/2": "tmdb_tv_100088_season_2.json"}
TVMAZE = {("/lookup/shows", "imdb=tt3581920"): "tvmaze_lookup_imdb_tt3581920.json",
          ("/lookup/shows", "thetvdb=327417"): "tvmaze_lookup_thetvdb_327417.json",
          ("/shows/46562/seasons", ""): "tvmaze_shows_46562_seasons.json",
          ("/shows/27436/seasons", ""): "tvmaze_shows_27436_seasons.json",
          ("/seasons/148200/episodes", ""): "tvmaze_seasons_148200_episodes.json",
          ("/seasons/184287/episodes", ""): "tvmaze_seasons_184287_episodes.json",
          ("/seasons/66312/episodes", ""): None,
          ("/search/shows", "q=money+heist"): "tvmaze_search_money_heist.json",
          ("/search/shows", "q=the+office"): "tvmaze_search_the_office.json",
          ("/search/shows", "q=%d7%91%d7%99%d7%aa+%d7%94%d7%a0%d7%99%d7%99%d7%a8"):
              "tvmaze_search_hebrew_money_heist.json"}


def tlou_pages(season_no=2):
    q = f"The Last of Us S{season_no:02d}"
    items = [{"title": f"The.Last.of.Us.S0{season_no}E0{e}.720p.WEB-DL", "size": f"{900 + e} MB",
              "callback_data": f"dl_tlou{e}", "message_id": 8001, "page": 1 + (e > 4),
              "page_callback": f"search#{q}#{1 + (e > 4)}", "query": q} for e in range(1, 8)]
    return items


def mh_items():
    data = json.loads(TGB_FIX.read_text())
    out = []
    for name in ("full_3_pages", "short_variant"):
        sc = data[name]
        for p in sc["pages"]:
            for it in p["items"]:
                out.append({**it, "message_id": p["message_id"], "page": p["page"], "query": sc["query"],
                            "page_callback": f"search#{sc['query']}#{p['page']}"})
    return out


class World:
    """Fake TMDb / TVmaze / Jellyfin / telegram-browser. Records every outbound request."""

    def __init__(self):
        self.urls, self.headers, self.downloads, self.season_requests = [], [], [], []
        self.library, self.episodes = {"Items": []}, {"Items": []}
        self.remote = {}
        self.tmdb_status = None
        self.jellyfin_down = False
        self.remote_down = False
        self.browser_items = mh_items()
        self.browser_error = None

    # season_sources._http_json
    def http(self, url, headers=None, timeout=15):
        self.urls.append(url)
        self.headers.append(dict(headers or {}))
        parsed = urllib.parse.urlsplit(url)
        if parsed.netloc == "api.themoviedb.org":
            path = parsed.path[len("/3"):]
            if self.tmdb_status:
                return self.tmdb_status, {"status_message": "Invalid API key"}
            name = TMDB.get(path)
            return (200, fx(name)) if name else (404, fx("tmdb_404.json"))
        if parsed.netloc == "api.tvmaze.com":
            key = (parsed.path, parsed.query.lower() if parsed.path == "/search/shows" else parsed.query)
            if key not in TVMAZE:
                return 404, {"name": "Not Found", "status": 404}
            name = TVMAZE[key]
            return (200, fx(name) if name else [])
        raise AssertionError("unexpected host " + parsed.netloc)

    # jellyfin.get / jellyfin.post_json
    def jf_get(self, path, params):
        if self.jellyfin_down:
            raise urllib.error.URLError("down")
        if path == "/Items":
            term = season_sources_key(params["SearchTerm"])
            return {"Items": [i for i in self.library["Items"] if term in season_sources_key(i["Name"])
                              or term in season_sources_key(i.get("OriginalTitle"))]}
        if path.startswith("/Shows/"):
            return self.episodes
        raise AssertionError(path)

    def jf_post(self, path, payload, timeout=20):
        if self.jellyfin_down or self.remote_down:
            raise urllib.error.URLError("down")
        info = payload["SearchInfo"]
        if info.get("ProviderIds"):
            return self.remote.get(("tmdb", info["ProviderIds"]["Tmdb"]), [])
        return self.remote.get(("name", info["Name"].casefold()), [])

    # searchgram.internal_json
    def internal(self, method, base, path, payload=None, timeout=90):
        if path == "/api/search/season":
            self.season_requests.append(payload)
            if self.browser_error:
                raise self.browser_error
            return season_parse.rank_season(self.browser_items, season=payload["season"], shows=payload["titles"],
                                            episodes=payload["episodes"], have=payload["have"])
        if path == "/api/search/download":
            self.downloads.append(payload)
            return {"status": "queued"}
        if path == "/status":
            return {"queue": 0}
        raise AssertionError(path)


def season_sources_key(value):
    return src.title_key(value)


class SeasonTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.saved = (src._http_json, jellyfin.get, jellyfin.post_json, searchgram.internal_json,
                      src.TVMAZE_MIN_INTERVAL, src.today_il)
        src._http_json, jellyfin.get, jellyfin.post_json = self.w.http, self.w.jf_get, self.w.jf_post
        searchgram.internal_json = self.w.internal
        self.saved_approvals = searchgram.approvals
        searchgram.approvals = REAL_APPROVALS
        src.TVMAZE_MIN_INTERVAL = 0
        import datetime
        src.today_il = lambda: datetime.date(2026, 10, 24)
        for var in ("TMDB_READ_TOKEN", "TMDB_API_KEY", "TMDB_READ_TOKEN_FILE", "TMDB_API_KEY_FILE"):
            os.environ.pop(var, None)
        conn = _db.connection
        for table in ("season_source_cache", "season_sessions", "approvals"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()

    def tearDown(self):
        searchgram.approvals = self.saved_approvals
        (src._http_json, jellyfin.get, jellyfin.post_json, searchgram.internal_json,
         src.TVMAZE_MIN_INTERVAL, src.today_il) = self.saved
        for var in ("TMDB_READ_TOKEN", "TMDB_API_KEY", "TMDB_READ_TOKEN_FILE", "TMDB_API_KEY_FILE"):
            os.environ.pop(var, None)

    def money_heist_library(self, episodes="jellyfin_episodes_files_only.json"):
        self.w.library = fx("jellyfin_library_series.json")
        self.w.episodes = fx(episodes)

    def tlou_remote(self):
        self.w.remote[("name", "the last of us")] = fx("jellyfin_remote_tlou_by_name.json")
        self.w.remote[("tmdb", "100088")] = fx("jellyfin_remote_tlou_by_tmdb.json")
        self.w.browser_items = tlou_pages()

    def wait_deliveries(self, n):
        for _ in range(100):
            if len(self.w.downloads) >= n:
                return
            time.sleep(0.02)
        self.fail(f"expected {n} deliveries, got {len(self.w.downloads)}")

    # ---------------------------------------------------------------- sources
    def test_tmdb_primary_for_library_series_by_provider_id(self):
        os.environ["TMDB_READ_TOKEN"] = CANARY_V4
        self.money_heist_library()
        out = season.find_season("בית הנייר", 1)
        self.assertEqual(out["episode_source"], "tmdb")
        self.assertEqual(out["episode_count"], 15)
        self.assertEqual(out["have"], [1, 2])
        self.assertIn("jellyfin_has_2_tmdb_says_15", out["notes"])
        self.assertEqual(out["status"], "PARTIAL")
        self.assertEqual(out["missing"], [11, 12, 13, 14, 15])
        self.assertEqual([p["ep"] for p in out["picks"]], list(range(3, 11)))
        self.assertIsNotNone(out["pack"])
        tmdb_urls = [u for u in self.w.urls if "themoviedb" in u]
        self.assertTrue(all("/search/" not in u for u in tmdb_urls))           # by id, never by name
        self.assertTrue(any("/tv/71446/season/1" in u for u in tmdb_urls))
        self.assertTrue(all("api_key" not in u for u in tmdb_urls))
        self.assertTrue(any(h.get("Authorization", "").startswith("Bearer ") for h in self.w.headers))
        self.assertFalse(any("tvmaze" in u for u in self.w.urls))
        # Hebrew titles first, then the English/original names for the SearchGram variants
        titles = self.w.season_requests[0]["titles"]
        self.assertEqual(titles[0], "בית הנייר")
        self.assertIn("Money Heist", titles)

    def test_v3_key_and_file_variants_never_leak(self):
        os.environ["TMDB_API_KEY"] = CANARY_V3
        self.money_heist_library()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            out = season.find_season("בית הנייר", 1)
        self.assertEqual(out["episode_source"], "tmdb")
        self.assertTrue(any("api_key=" in u for u in self.w.urls if "themoviedb" in u))
        self.assertFalse(any("Authorization" in h for h in self.w.headers))
        dump = "\n".join(map(str, _db.connection.execute("SELECT * FROM season_source_cache"))) + \
               "\n".join(map(str, _db.connection.execute("SELECT * FROM season_sessions")))
        for blob in (json.dumps(out, ensure_ascii=False), dump, stdout.getvalue(), stderr.getvalue()):
            self.assertNotIn(CANARY_V3, blob)
        os.environ.pop("TMDB_API_KEY")
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write(CANARY_V4 + "\n")
        os.environ["TMDB_READ_TOKEN_FILE"] = handle.name
        try:
            self.assertEqual(src.tmdb_auth(), ("bearer", CANARY_V4))
        finally:
            os.unlink(handle.name)
        os.environ["TMDB_READ_TOKEN_FILE"] = "/nonexistent/x"
        self.assertIsNone(src.tmdb_auth())
        os.environ.pop("TMDB_READ_TOKEN_FILE")
        os.environ["TMDB_API_KEY"] = CANARY_V4            # a v4 token in TMDB_API_KEY is sent as Bearer
        self.assertEqual(src.tmdb_auth()[0], "bearer")

    def test_tmdb_errors_never_echo_credentials(self):
        os.environ["TMDB_READ_TOKEN"] = CANARY_V4
        self.w.tmdb_status = 401
        with self.assertRaises(src.SourceError) as ctx:
            src.tmdb_get("/tv/71446")
        self.assertEqual(str(ctx.exception), "tmdb_http_401")
        self.assertNotIn(CANARY_V4, repr(ctx.exception.__dict__))

    def test_no_key_library_with_virtual_episodes_uses_jellyfin_only(self):
        self.money_heist_library("jellyfin_episodes_with_virtual.json")
        out = season.find_season("בית הנייר", 1)
        self.assertEqual(out["episode_source"], "jellyfin_library")
        self.assertEqual(out["episode_count"], 10)
        self.assertEqual(out["status"], "COMPLETE")
        self.assertEqual([p["ep"] for p in out["picks"]], list(range(3, 11)))
        self.assertEqual(out["next_action"], "confirm_then_queue")
        self.assertFalse(any("tvmaze" in u or "themoviedb" in u for u in self.w.urls))

    def test_no_key_files_only_library_cross_checked_by_tvmaze_ids_imdb_then_tvdb(self):
        self.money_heist_library()
        out = season.find_season("בית הנייר", 1)
        self.assertEqual(out["episode_source"], "jellyfin_library+tvmaze")
        self.assertEqual(out["episode_count"], 15)
        self.assertIn("jellyfin_has_2_tvmaze_says_15", out["notes"])
        lookups = [u for u in self.w.urls if "/lookup/shows" in u]
        self.assertEqual(lookups, ["https://api.tvmaze.com/lookup/shows?imdb=tt6468322",
                                   "https://api.tvmaze.com/lookup/shows?thetvdb=327417"])
        self.assertFalse(any("/search/shows" in u for u in self.w.urls))

    def test_not_in_library_tmdb_primary_via_remote_search_id(self):
        os.environ["TMDB_READ_TOKEN"] = CANARY_V4
        self.tlou_remote()
        out = season.find_season("The Last of Us", 2)
        self.assertEqual((out["episode_source"], out["episode_count"], out["status"]), ("tmdb", 7, "COMPLETE"))
        self.assertEqual(out["show"]["tmdb_id"], 100088)
        self.assertFalse(out["show"]["in_library"])
        self.assertIn("האחרונים מבינינו", self.w.season_requests[0]["titles"])    # Hebrew name from TMDb first

    def test_not_in_library_no_key_tvmaze_by_imdb_from_remote_search(self):
        self.tlou_remote()
        out = season.find_season("The Last of Us", 2)
        self.assertEqual((out["episode_source"], out["episode_count"], out["status"]), ("tvmaze", 7, "COMPLETE"))
        self.assertEqual(out["show"]["tvmaze_id"], 46562)
        self.assertIn("https://api.tvmaze.com/lookup/shows?imdb=tt3581920", self.w.urls)
        self.assertFalse(any("/search/shows" in u for u in self.w.urls))

    def test_tmdb_failure_falls_back_to_tvmaze(self):
        os.environ["TMDB_READ_TOKEN"] = CANARY_V4
        self.w.tmdb_status = 503
        self.tlou_remote()
        out = season.find_season("The Last of Us", 2)
        self.assertEqual(out["episode_source"], "tvmaze")
        self.assertIn("tmdb_http_503", out["notes"])

    def test_unaired_announced_season(self):
        os.environ["TMDB_READ_TOKEN"] = CANARY_V4       # TMDb has no season 3 (404) -> TVmaze: episodeOrder null
        self.tlou_remote()
        out = season.find_season("The Last of Us", 3)
        self.assertEqual(out["status"], "NOT_AIRED")
        self.assertEqual(out["next_action"], "tell_not_aired")
        self.assertIsNone(out["episode_count"])
        self.assertIn("tmdb_season_not_found", out["notes"])
        self.assertEqual(self.w.season_requests, [])              # no SearchGram scan for an unaired season

    def test_partly_aired_season_lists_unaired(self):
        self.tlou_remote()
        import datetime
        src.today_il = lambda: datetime.date(2025, 4, 30)          # E1-E3 aired, E4-E7 announced
        out = season.find_season("The Last of Us", 2)
        self.assertEqual(out["unaired"], [4, 5, 6, 7])
        self.assertEqual([p["ep"] for p in out["picks"]], [1, 2, 3])
        self.assertEqual(out["status"], "COMPLETE")

    def test_ambiguous_show_returns_candidates_without_searching(self):
        self.w.remote[("name", "the office")] = fx("jellyfin_remote_the_office.json")
        out = season.find_season("The Office", 2)
        self.assertEqual(out["status"], "AMBIGUOUS_SHOW")
        self.assertEqual({c["tmdb_id"] for c in out["candidates"]}, {2316, 2996, 250000})
        self.assertEqual(self.w.season_requests, [])
        out = season.find_season("The Office", 2, year=2005)
        self.assertEqual(out["show"]["tmdb_id"], 2316)

    def test_no_ids_name_search_is_last_resort_and_never_auto_picks(self):
        self.w.jellyfin_down = True
        out = season.find_season("Money Heist", 1)
        self.assertEqual(out["status"], "AMBIGUOUS_SHOW")
        self.assertIn("identified_by_name_only", out["notes"])
        self.assertEqual(out["candidates"][0]["tvmaze_id"], 59307)      # TVmaze's first hit is the Korea remake
        self.assertEqual(out["candidates"][1]["name"], "La Casa de Papel")
        self.assertEqual(self.w.season_requests, [])

    def test_unknown_episode_list_still_returns_best_candidates(self):
        self.w.jellyfin_down = True                                  # Hebrew-only title: TVmaze search finds nothing
        out = season.find_season("בית הנייר", 1)
        self.assertEqual(out["status"], "UNKNOWN_EPISODE_LIST")
        self.assertEqual(out["next_action"], "ask_episode_count")
        self.assertIn(10, out["observed"])
        self.assertIsNone(self.w.season_requests[0]["episodes"])
        self.assertIn("show_not_identified", out["notes"])

    def test_already_have(self):
        self.money_heist_library("jellyfin_episodes_all_have_multi.json")   # E1 + a two-episode file E2-E3
        self.w.library["Items"][0]["ProviderIds"] = {}                  # no ids: no TVmaze cross-check possible
        out = season.find_season("בית הנייר", 1)
        self.assertEqual(out["have"], [1, 2, 3])
        self.assertEqual(out["status"], "ALREADY_HAVE")

    def test_search_failed(self):
        self.money_heist_library("jellyfin_episodes_with_virtual.json")
        self.w.browser_error = urllib.error.HTTPError("u", 502, "bad", {}, None)
        out = season.find_season("בית הנייר", 1)
        self.assertEqual((out["status"], out["next_action"]), ("SEARCH_FAILED", "tell_search_failed"))

    def test_session_reuse_and_validation(self):
        self.money_heist_library("jellyfin_episodes_with_virtual.json")
        first = season.find_season("בית הנייר", 1)
        again = season.find_season("בית הנייר", 1)
        self.assertTrue(again.get("reused"))
        self.assertEqual(first["search_id"], again["search_id"])
        self.assertEqual(len(self.w.season_requests), 1)
        self.assertNotEqual(season.find_season("בית הנייר", 1, refresh=True)["search_id"], first["search_id"])
        for bad in ({"season": "1"}, {"season": True}, {"season": 1, "tmdb_id": "x"}):
            with self.assertRaises(ValueError):
                season.find_season("x", **bad)
        with self.assertRaises(ValueError):
            season.find_season("   ", 1)

    # ---------------------------------------------------------------- queueing
    def found(self):
        self.money_heist_library("jellyfin_episodes_with_virtual.json")
        return season.find_season("בית הנייר", 1)

    def test_queue_all_idempotent_and_cross_page(self):
        out = self.found()
        res = season.queue_season_candidates(out["search_id"])
        n = len({p["id"] for p in out["picks"]})                 # E7-E8 share one two-episode file
        self.assertEqual(len(res["queued"]), n)
        self.wait_deliveries(n)
        pages = {d["page_callback"].rsplit("#", 1)[1] for d in self.w.downloads}
        self.assertTrue({"1", "2"} <= pages)                       # page-1 items queued after paging to page 2
        self.assertTrue(all(d["query"] and d["message_id"] for d in self.w.downloads))
        again = season.queue_season_candidates(out["search_id"])
        self.assertEqual(again["queued"], [])
        self.assertEqual(sorted(a["audit_id"] for a in again["already_queued"]), sorted(res["audit_ids"]))
        time.sleep(0.1)
        self.assertEqual(len(self.w.downloads), n)
        rows = _db.connection.execute("SELECT action,status FROM approvals").fetchall()
        self.assertEqual(len(rows), n)
        self.assertTrue(all(r[0] == "searchgram_download" for r in rows))
        status = searchgram.delivery_status(res["audit_ids"][0])  # existing follow-up path works unchanged
        self.assertIn(status["delivery"]["status"], ("processing", "executed"))

    def test_pack_refused_without_allow_pack(self):
        out = self.found()
        pack_id = out["pack"]["id"]
        self.assertNotIn(pack_id, {p["id"] for p in out["picks"]})
        with self.assertRaises(ValueError) as ctx:
            season.queue_season_candidates(out["search_id"], candidate_ids=[pack_id])
        self.assertIn("allow_pack", str(ctx.exception))
        with self.assertRaises(ValueError):
            season.queue_season_candidates(out["search_id"], candidate_ids=[pack_id], allow_pack="true")
        self.assertEqual(self.w.downloads, [])
        res = season.queue_season_candidates(out["search_id"], candidate_ids=[pack_id], allow_pack=True)
        self.assertEqual(len(res["queued"]), 1)

    def test_queue_subset_skip_have_and_errors(self):
        out = self.found()
        res = season.queue_season_candidates(out["search_id"], episodes=[3, 4])
        self.assertEqual([q["episodes"] for q in res["queued"]], [[3], [4]])
        with self.assertRaises(ValueError):
            season.queue_season_candidates(out["search_id"], episodes=[11])
        with self.assertRaises(ValueError):
            season.queue_season_candidates(out["search_id"], candidate_ids=["c999"])
        with self.assertRaises(ValueError):
            season.queue_season_candidates("nope")
        have_cand = next(c for c in season._load(out["search_id"])["candidates"].values()
                         if c.get("episodes") == [1] and c["kind"] == "episode")
        res = season.queue_season_candidates(out["search_id"], candidate_ids=[have_cand["id"]])
        self.assertEqual(res["skipped"][0]["reason"], "already_in_jellyfin")
        alts = season.season_candidates(out["search_id"], 3)
        self.assertEqual(alts["candidates"][0]["id"], alts["pick"])

    def test_session_expiry(self):
        out = self.found()
        _db.connection.execute("UPDATE season_sessions SET created=created-1000")
        with self.assertRaises(ValueError) as ctx:
            season.queue_season_candidates(out["search_id"])
        self.assertIn("expired", str(ctx.exception))

    def test_legacy_single_result_queue_unchanged(self):
        searchgram.save_session("legacy1", {"query": "q", "message_id": 5, "page": 1, "items": [
            {"title": "t", "size": "1 GB", "callback_data": "dl_a"}]})
        res = searchgram.queue_result("legacy1", 1)
        self.wait_deliveries(1)
        self.assertEqual(self.w.downloads[0], {"message_id": 5, "callback_data": "dl_a"})   # no page fields
        self.assertEqual(res["status"], "processing")

    # ---------------------------------------------------------------- registration
    def test_mcp_tools_and_routes(self):
        from command_center.mcp_tools import searchgram as mcp_searchgram
        from command_center.mcp_tools import season as mcp_season
        tools, calls = {}, []

        class FakeMCP:
            def tool(self):
                def deco(fn):
                    tools[fn.__name__] = fn
                    return fn
                return deco

        class FakeClient:
            def request(self, method, path, payload=None, timeout=30):
                calls.append((method, path, payload, timeout))
                return {}
        mcp_searchgram.register(FakeMCP(), FakeClient())        # season tools ride on the SearchGram module
        self.assertTrue({"media_find_season", "queue_season_candidates", "media_season_candidates",
                         "searchgram_search", "queue_searchgram_result"} <= set(tools))
        tools.clear()
        mcp_season.register(FakeMCP(), FakeClient())
        self.assertEqual(set(tools), {"media_find_season", "queue_season_candidates", "media_season_candidates"})
        tools["media_find_season"]("x", 1)
        tools["queue_season_candidates"]("sid")
        self.assertEqual(calls[0][1], "/media/find-season")
        self.assertGreater(calls[0][3], season.SEARCH_TIMEOUT)          # MCP waits longer than the scan
        self.assertEqual(calls[1][2]["allow_pack"], False)
        routes = {}

        class FakeRouter:
            def post(self, path, handler, status=200):
                routes[path] = status

            def get(self, path, handler, status=200):
                routes[path] = status
        season.register_routes(FakeRouter())
        self.assertEqual(routes, {"/v1/media/find-season": 200, "/v1/media/queue-season": 202,
                                  "/v1/media/season-candidates": 200})
        routes.clear()
        searchgram.register_routes(FakeRouter())                 # the API wires them through searchgram
        self.assertIn("/v1/media/find-season", routes)
        self.assertIn("/v1/searchgram/search", routes)


if __name__ == "__main__":
    unittest.main()
