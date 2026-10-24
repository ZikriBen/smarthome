"""Season parser + ranking (app/season_parse.py). Offline: synthetic titles and the recorded corpus."""

import json
from pathlib import Path

import pytest

from app import season_parse as sp

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures" / "season"
SHOWS = ["בית הנייר", "Money Heist"]


def marker(title):
    return sp.find_marker(sp.norm(title))


@pytest.mark.parametrize("title,season,episodes,kind", [
    ("Money.Heist.S01E02.1080p", 1, [2], "episode"),
    ("money heist s1e2", 1, [2], "episode"),
    ("MONEY HEIST S01 E02", 1, [2], "episode"),
    ("Money Heist 1x07", 1, [7], "episode"),
    ("Money Heist Season 1 Episode 4", 1, [4], "episode"),
    ("בית הנייר עונה 1 פרק 2", 1, [2], "episode"),
    ("בית הנייר ע1 פ20", 1, [20], "episode"),
    ("לולו_סרטים_בית_הנייר_ע1פ12_480p", 1, [12], "episode"),
    ("בית הנייר - עונה 1 - פרק 6", 1, [6], "episode"),
    ("בית הנייר ע1 פ1-2", 1, [1, 2], "multi"),
    ("בית הנייר עונה 1 פרקים 3 עד 4", 1, [3, 4], "multi"),
    ("Money Heist S01E01-E03 720p", 1, [1, 2, 3], "multi"),
    ("money heist s01e05e06", 1, [5, 6], "multi"),
    ("Money Heist S01E01-10", 1, None, "pack"),
    ("Money Heist S01 COMPLETE 1080p", 1, None, "pack"),
    ("בית הנייר עונה 1 מלאה", 1, None, "pack"),
    ("Money Heist S01E05-720p", 1, [5], "episode"),           # '-720p' is not a range
    ("Money Heist 1920x1080 S01E02", 1, [2], "episode"),        # a resolution is not 'NxM'
])
def test_markers(title, season, episodes, kind):
    c = sp.parse_item({"title": title, "size": "500 MB"}, season=season, shows=SHOWS, order=0)
    assert c is not None, title
    assert c.kind == kind
    if kind != "pack":
        assert c.episodes == episodes
    else:
        assert c.episodes is None or len(c.episodes) >= 4


@pytest.mark.parametrize("title", [
    "בית הנייר קוריאה עונה 1 פרק 2",       # a different show (remake) sharing the prefix
    "Money Heist Korea S01E02",
    "בית הנייר עונה 2 פרק 1",              # other season
    "משהו אחר עונה 1 פרק 1",
    "בית הניירות עונה 1 פרק 1",             # not a word boundary
])
def test_rejects(title):
    assert sp.parse_item({"title": title, "size": "500 MB"}, season=1, shows=SHOWS, order=0) is None


@pytest.mark.parametrize("title", ["Money Heist 2017 S01E03", "Money Heist (2017) - S01E03", "money.heist.720p.s01e03"])
def test_year_and_tags_between_title_and_marker(title):
    assert sp.parse_item({"title": title, "size": "1 GB"}, season=1, shows=SHOWS, order=0).episodes == [3]


def test_case_and_geresh_insensitive_titles():
    assert sp.parse_item({"title": "480p בוג׳ק הורסמן ע1 פ10 נריה סרטים", "size": "200 MB"}, season=1,
                         shows=["בוג'ק הורסמן"], order=0).group == "נריה סרטים"
    assert sp.parse_item({"title": "spongebob-squarepants.S4E12_480P", "size": "120 MB"}, season=4,
                         shows=["SpongeBob SquarePants"], order=0).res == 480
    assert sp.title_key("Money  Heist!") == sp.title_key("money heist")


def test_size_resolution_hints():
    assert sp.parse_size_mb("1.5 GB") == 1536
    assert sp.parse_size_mb("665,71 MB") == pytest.approx(665.71)
    assert sp.parse_size_mb("900 KB") == pytest.approx(900 / 1024)
    assert sp.parse_size_mb(None) is None and sp.parse_size_mb("?") is None
    assert sp.parse_resolution(sp.norm("Show.S01E01.2160p")) == 2160
    assert sp.parse_resolution(sp.norm("Show 4K S01E01")) == 2160
    assert sp.parse_resolution(sp.norm("Show S01E01")) is None
    c = sp.parse_item({"title": "בית הנייר ע1פ29 ת מ 480p", "size": "236 MB"}, season=1, shows=SHOWS, order=0)
    assert c.heb is True
    assert sp.parse_item({"title": "בית הנייר ע1 פ2 מדובב", "size": "236 MB"}, season=1, shows=SHOWS, order=0).dubbed


def eps(n, runtime=45):
    return [{"n": i, "runtime": runtime} for i in range(1, n + 1)]


def rank(items, episodes, **kw):
    items = [{"callback_data": f"dl_{i}", **it} for i, it in enumerate(items)]
    return sp.rank_season(items, season=1, shows=SHOWS, episodes=episodes, **kw)


def pick_titles(result):
    by_id = {c["id"]: c for c in result["candidates"]}
    return {p["ep"]: by_id[p["id"]]["title"] for p in result["picks"]}


def test_smallest_sufficient_wins_and_floor():
    r = rank([{"title": "Money Heist S01E01 1080p", "size": "1.4 GB"},
              {"title": "Money Heist S01E01 720p", "size": "600 MB"},
              {"title": "Money Heist S01E01 480p", "size": "200 MB"},       # smaller but below 720p
              {"title": "Money Heist S01E01", "size": "40 MB"}], eps(1))    # < 1.5 MB/min: broken
    assert pick_titles(r) == {1: "Money Heist S01E01 720p"}
    assert "smallest sufficient" in r["picks"][0]["why"]
    tiers = {c["title"]: c["tier"] for c in r["candidates"]}
    assert tiers["Money Heist S01E01"] == "broken" and tiers["Money Heist S01E01 480p"] == "low"


def test_unknown_resolution_uses_size_floor():
    r = rank([{"title": "Money Heist S01E01", "size": "150 MB"},     # 3.3 MB/min < 4: low
              {"title": "Money Heist S01E01 x", "size": "300 MB"}], eps(1))
    assert pick_titles(r) == {1: "Money Heist S01E01 x"}


def test_remux_penalised_but_used_when_only_option():
    r = rank([{"title": "Money Heist S01E01 2160p REMUX", "size": "30 GB"},
              {"title": "Money Heist S01E01 1080p", "size": "2 GB"},
              {"title": "Money Heist S01E02 2160p REMUX", "size": "30 GB"}], eps(2))
    assert pick_titles(r) == {1: "Money Heist S01E01 1080p", 2: "Money Heist S01E02 2160p REMUX"}
    assert "remux" in r["picks"][1]["why"]


def test_low_quality_only_is_picked_and_explained():
    r = rank([{"title": "Money Heist S01E01 480p", "size": "200 MB"}], eps(1))
    assert r["picks"][0]["why"].startswith("best available but below 720p")


def test_tie_breakers_resolution_hebrew_group_order():
    # within 10%: higher resolution wins
    r = rank([{"title": "Money Heist S01E01 720p", "size": "1.00 GB"},
              {"title": "Money Heist S01E01 1080p", "size": "1.05 GB"}], eps(1))
    assert pick_titles(r)[1].endswith("1080p")
    # same res, within 10%: Hebrew subtitle marker wins
    r = rank([{"title": "Money Heist S01E01 720p", "size": "500 MB"},
              {"title": "Money Heist S01E01 720p HebSub", "size": "520 MB"}], eps(1))
    assert pick_titles(r)[1].endswith("HebSub")
    # outside the tie band: smaller wins regardless
    r = rank([{"title": "Money Heist S01E01 720p", "size": "500 MB"},
              {"title": "Money Heist S01E01 720p HebSub", "size": "700 MB"}], eps(1))
    assert not pick_titles(r)[1].endswith("HebSub")
    # identical: earlier result (recency proxy)
    r = rank([{"title": "Money Heist S01E01 720p a", "size": "500 MB"},
              {"title": "Money Heist S01E01 720p b", "size": "500 MB"}], eps(1))
    assert pick_titles(r)[1].endswith(" a")


def test_release_group_consistency():
    items = [{"title": f"Money Heist S01E0{e} 720p-GRP", "size": "520 MB"} for e in (1, 2, 3)]
    items += [{"title": "Money Heist S01E04 720p-OTHER", "size": "500 MB"},
              {"title": "Money Heist S01E04 720p-GRP", "size": "530 MB"}]
    r = rank(items, eps(4))
    assert r["majority_group"] == "grp"
    assert pick_titles(r)[4].endswith("-GRP")
    assert "same release group" in r["picks"][3]["why"]


def test_multi_episode_only_when_no_single_and_maps_all_covered():
    r = rank([{"title": "Money Heist S01E01E02 720p", "size": "1 GB"},
              {"title": "Money Heist S01E01 720p", "size": "520 MB"},
              {"title": "Money Heist S01E03-E04 720p", "size": "1 GB"}], eps(4))
    titles = pick_titles(r)
    assert titles[1] == "Money Heist S01E01 720p"
    assert titles[2] == "Money Heist S01E01E02 720p"            # covers the missing E2
    assert titles[3] == titles[4] == "Money Heist S01E03-E04 720p"
    assert "multi-episode" in r["picks"][2]["why"]


def test_pack_never_picked_partial_reports_missing_and_pack():
    r = rank([{"title": "Money Heist S01E01 720p", "size": "500 MB"},
              {"title": "Money Heist S01 COMPLETE 1080p", "size": "20 GB"}], eps(3))
    assert [p["ep"] for p in r["picks"]] == [1]
    assert r["missing"] == [2, 3]
    assert r["pack"]["covers"] == "season" and r["pack"]["id"] not in {p["id"] for p in r["picks"]}


def test_outside_list_and_have_and_unknown_list():
    r = rank([{"title": "Money Heist S01E02 720p", "size": "500 MB"},
              {"title": "Money Heist S01E20 720p", "size": "500 MB"}], eps(3), have=[1])
    assert r["outside_list"] == [20] and r["needed"] == [2, 3] and r["missing"] == [3]
    r = rank([{"title": "Money Heist S01E01 720p", "size": "500 MB"},
              {"title": "Money Heist S01E04 720p", "size": "500 MB"}], None)
    assert r["observed"] == [1, 4] and r["gaps"] == [2, 3] and len(r["picks"]) == 2


def test_runtime_scales_floor():
    # 30-min episode: 130 MB = 4.3 MB/min (sufficient); for a 60-min episode it is 2.2 (low)
    short = rank([{"title": "Money Heist S01E01", "size": "130 MB"}], eps(1, runtime=30))
    long = rank([{"title": "Money Heist S01E01", "size": "130 MB"}], eps(1, runtime=60))
    assert short["candidates"][0]["tier"] == "sufficient" and long["candidates"][0]["tier"] == "low"


def test_config_from_env():
    cfg = sp.RankConfig.from_env({"SEASON_MIN_RES": "1080", "SEASON_FLOOR_MB_PER_MIN": "6", "SEASON_SIZE_TIE_PCT": "x"})
    assert cfg.min_res == 1080 and cfg.floor_mb_per_min == 6.0 and cfg.size_tie_pct == 10.0


def test_synthetic_three_page_fixture():
    scen = json.loads((FIX / "searchgram_pages.json").read_text())["full_3_pages"]
    items = [dict(it, page=p["page"]) for p in scen["pages"] for it in p["items"]]
    r = sp.rank_season(items, season=1, shows=SHOWS, episodes=eps(10))
    t = pick_titles(r)
    assert t[1] == "בית הנייר עונה 1 פרק 1 720p WEB-DL זירה מדיה"
    assert t[3].endswith("זירה מדיה")                       # 530 MB group-consistent over 515 MB (tie band)
    assert t[4].endswith("זירה מדיה")                       # the 35 MB sample is broken
    assert "REMUX" in t[5]
    assert "מתורגם" in t[6]
    assert t[7] == t[8] and "פ7-8" in t[7]
    assert t[9] == "בית הנייר עונה 1 פרק 9 זירה מדיה"
    assert r["missing"] == [10] and r["outside_list"] == [20] and r["pack"]["size"] == "14.20 GB"
    assert all(c["season"] == 1 for c in r["candidates"])


def test_recorded_corpus_parses_and_skips_broken():
    rows = [json.loads(line) for line in (ROOT / "corpus-480p.jsonl").read_text().splitlines()]
    items = [{"title": r["result_title"], "size": r["size"], "callback_data": r["callback_data"],
              "message_id": r["search_message_id"], "page": r["page"]} for r in rows]
    for item in items:                                    # never raises on 1000 real titles
        sp.find_marker(sp.norm(item["title"]))
    r = sp.rank_season(items, season=2, shows=["אליפים"], episodes=[{"n": n, "runtime": 22} for n in range(1, 52)])
    by_id = {c["id"]: c for c in r["candidates"]}
    assert len(r["picks"]) > 10
    assert all(by_id[p["id"]]["tier"] != "broken" for p in r["picks"])
    assert any(c["size"] == "22 MB" and c["tier"] == "broken" for c in r["candidates"])   # the 21.99 MB E43
    riley = sp.rank_season(items, season=1, shows=["העולם של ריילי"], episodes=None)
    assert riley["observed"] and all(by["page"] for by in riley["candidates"])
