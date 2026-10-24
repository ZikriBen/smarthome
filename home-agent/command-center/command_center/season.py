"""Full-season finder: media_find_season / queue_season_candidates / media_season_candidates.

One call identifies the show and its episode list (season_sources.py), runs ONE season-level SearchGram scan in
telegram-browser (POST /api/search/season: query variants, bounded paging, parse + rank) and returns a compact,
ranked pick per needed episode with an explicit ``next_action``. Queuing goes through the existing SearchGram
audit + serialized delivery path (searchgram.start_delivery), is idempotent per search and never queues a
season pack unless the caller passes allow_pack=true after the user chose the pack.
Idea credit: branch feature/searchgram-season-candidates (9cea2f9); fresh implementation.
"""

import json
import os
import threading
import time

from . import searchgram, season_sources
from .config import BROWSER_URL
from .database import approval_lock, connection

SESSION_TTL = 900
SEARCH_TIMEOUT = 200          # telegram-browser's own scan budget is 150 s

connection.execute("CREATE TABLE IF NOT EXISTS season_sessions (id TEXT PRIMARY KEY, key TEXT NOT NULL, "
                   "data TEXT NOT NULL, created INTEGER NOT NULL)")
connection.commit()
_queue_lock = threading.Lock()

INSTRUCTIONS = {
    "confirm_then_queue": "Show one short summary (episode count, total size, quality) and ask to confirm. On yes, "
                          "call queue_season_candidates(search_id) once.",
    "ask_about_missing": "Show the summary, say which episodes were not found, ask whether to queue the found "
                         "episodes. If a season pack exists, ask separately whether to use the pack instead; never "
                         "queue the pack unless the user chooses it (then allow_pack=true with its id).",
    "ask_episode_count": "The season's episode list is unknown. Show what was found (episode numbers, total size) "
                         "and ask the user to confirm before queueing.",
    "ask_which_show": "Several shows match. Ask the user which one (name + year), then call media_find_season again "
                      "with its tmdb_id (or tvmaze_id).",
    "tell_already_have": "Tell the user these episodes are already in the library. Nothing to queue.",
    "tell_not_aired": "Tell the user the season has not aired yet (episode count unknown). Nothing to queue.",
    "tell_not_found": "Tell the user nothing suitable was found for this season. Do not retry automatically.",
    "tell_search_failed": "Tell the user the search could not run right now. Do not retry automatically.",
}


def _clean_titles(*values):
    out = []
    for value in values:
        value = " ".join(str(value or "").split())
        if value and (season_sources.has_hebrew(value) or season_sources.has_latin(value)):
            if season_sources.title_key(value) not in {season_sources.title_key(t) for t in out}:
                out.append(value)
    return out


def _session_key(titles, season, tmdb_id, tvmaze_id, skip):
    return json.dumps([sorted(season_sources.title_key(t) for t in titles), season, tmdb_id, tvmaze_id, skip])


def _save(session_id, key, data):
    with approval_lock:          # one shared connection: serialize writes with the delivery threads
        connection.execute("INSERT OR REPLACE INTO season_sessions VALUES (?,?,?,?)",
                           (session_id, key, json.dumps(data), data.get("created", int(time.time()))))
        connection.commit()


def _load(session_id):
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("a season search_id is required")
    row = connection.execute("SELECT data,created FROM season_sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        raise ValueError("unknown season search; call media_find_season again")
    if int(time.time()) - row[1] > SESSION_TTL:
        raise ValueError("season search expired; call media_find_season again")
    return json.loads(row[0])


def _reuse(key):
    row = connection.execute("SELECT id,data,created FROM season_sessions WHERE key=? ORDER BY created DESC LIMIT 1",
                             (key,)).fetchone()
    if row and int(time.time()) - row[2] <= SESSION_TTL:
        return json.loads(row[1])
    return None


def _total(picks, cands):
    seen, total = set(), 0.0
    for p in picks:
        c = cands[p["id"]]
        if p["id"] not in seen and c.get("size_mb"):
            seen.add(p["id"])
            total += c["size_mb"]
    return f"{total / 1024:.1f} GB" if total >= 1024 else f"{total:.0f} MB"


def _validate_int(name, value, low, high, optional=True):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer {low}..{high}")
    return value


def find_season(title, season, title_en=None, tmdb_id=None, tvmaze_id=None, year=None, skip_in_jellyfin=True,
                refresh=False):
    season = _validate_int("season", season, 0, 99, optional=False)
    tmdb_id = _validate_int("tmdb_id", tmdb_id, 1, 10**9)
    tvmaze_id = _validate_int("tvmaze_id", tvmaze_id, 1, 10**9)
    year = _validate_int("year", year, 1900, 2100)
    user_titles = _clean_titles(title, title_en)
    if not user_titles:
        raise ValueError("title is empty")
    key = _session_key(user_titles, season, tmdb_id, tvmaze_id, bool(skip_in_jellyfin))
    if not refresh:
        cached = _reuse(key)
        if cached:
            return dict(cached["output"], reused=True)

    src = season_sources.resolve(user_titles, season, tmdb_id=tmdb_id, tvmaze_id=tvmaze_id, year=year)
    show = src["show"]
    out = {"season": season, "show": {"name": show.get("name"), "year": show.get("year"),
                                      "tmdb_id": show.get("tmdb"), "tvmaze_id": show.get("tvmaze"),
                                      "in_library": show.get("in_library", False)},
           "episode_source": src.get("source"), "episode_count": src.get("episode_count"),
           "have": src.get("have", []), "unaired": src.get("unaired", []), "notes": src.get("notes", [])}

    def finish(status, action, **extra):
        out.update(status=status, next_action=action, instruction=INSTRUCTIONS[action], **extra)
        return out

    if src["status"] == "ambiguous":
        return finish("AMBIGUOUS_SHOW", "ask_which_show", candidates=src["candidates"])

    episodes = src.get("episodes") if src.get("count_known") else None
    known = src.get("known_episodes")
    if src.get("source") and not src.get("episodes") and (src.get("count_known") or not known):
        return finish("NOT_AIRED", "tell_not_aired")
    have = set(src.get("have", [])) if skip_in_jellyfin else set()
    if episodes is not None and episodes and all(e["n"] in have for e in episodes):
        return finish("ALREADY_HAVE", "tell_already_have")

    search_titles = _clean_titles(*(t for t in user_titles if season_sources.has_hebrew(t)),
                                  show.get("name") if season_sources.has_hebrew(show.get("name")) else None,
                                  *(t for t in user_titles if not season_sources.has_hebrew(t)),
                                  show.get("english_name"), show.get("original_name"),
                                  show.get("name") if not season_sources.has_hebrew(show.get("name")) else None)
    try:
        result = searchgram.internal_json("POST", BROWSER_URL, "/api/search/season", {
            "titles": search_titles, "season": season, "episodes": episodes, "have": sorted(have)},
            timeout=SEARCH_TIMEOUT)
    except Exception as exc:
        out["notes"] = out["notes"] + [f"search_{type(exc).__name__}"]
        return finish("SEARCH_FAILED", "tell_search_failed")

    cands = {c["id"]: c for c in result.get("candidates") or []}
    picks = result.get("picks") or []
    pack = result.get("pack")
    out.update(picks=[{"ep": p["ep"], "id": p["id"], "size": cands[p["id"]].get("size"),
                       "res": cands[p["id"]].get("res"), "why": p["why"]} for p in picks],
               total_size=_total(picks, cands), missing=result.get("missing") or [],
               pack=({"id": pack["id"], "size": pack["size"], "covers": pack["covers"]} if pack else None),
               truncated=bool(result.get("truncated")))
    if result.get("outside_list"):
        out["notes"] = out["notes"] + [f"results_numbered_outside_list:{result['outside_list'][:10]}"]
    if episodes is None:
        out["observed"] = result.get("observed") or []
        out["known_episodes"] = known or []
        status, action = ("UNKNOWN_EPISODE_LIST", "ask_episode_count") if picks else ("NOT_FOUND", "tell_not_found")
    elif not picks and not pack:
        status, action = "NOT_FOUND", "tell_not_found"
    elif out["missing"]:
        status, action = "PARTIAL", "ask_about_missing"
    else:
        status, action = "COMPLETE", "confirm_then_queue"
    finish(status, action)
    session_id = os.urandom(9).hex()
    out["search_id"] = session_id
    _save(session_id, key, {"created": int(time.time()), "output": out, "candidates": cands,
                            "picks": {str(p["ep"]): p["id"] for p in picks},
                            "have": sorted(have), "queued": {}})
    return out


def queue_season_candidates(search_id, episodes=None, candidate_ids=None, allow_pack=False):
    if isinstance(allow_pack, str) or not isinstance(allow_pack, bool):
        raise ValueError("allow_pack must be true or false")
    with _queue_lock:
        data = _load(search_id)
        cands, picks, have = data["candidates"], data["picks"], set(data["have"])
        targets = []
        if candidate_ids:
            if not isinstance(candidate_ids, list) or not all(isinstance(c, str) for c in candidate_ids):
                raise ValueError("candidate_ids must be a list of ids such as 'c3'")
            unknown = [c for c in candidate_ids if c not in cands]
            if unknown:
                raise ValueError(f"unknown candidate ids for this search: {unknown}")
            packs = [c for c in candidate_ids if cands[c]["kind"] == "pack"]
            if packs and allow_pack is not True:
                raise ValueError(f"{packs} is a season pack. Queue a pack only after the user explicitly chose it, "
                                 "with allow_pack=true.")
            targets = list(dict.fromkeys(candidate_ids))
        elif episodes:
            if not isinstance(episodes, list) or not all(isinstance(e, int) and not isinstance(e, bool)
                                                         for e in episodes):
                raise ValueError("episodes must be a list of episode numbers")
            missing = [e for e in episodes if str(e) not in picks]
            if missing:
                raise ValueError(f"no pick for episodes {missing}; see the find result's missing list")
            targets = list(dict.fromkeys(picks[str(e)] for e in episodes))
        else:
            targets = list(dict.fromkeys(picks[k] for k in sorted(picks, key=int)))
        if not targets:
            raise ValueError("nothing to queue for this search")
        queued, already, skipped = [], [], []
        for cid in targets:
            cand = cands[cid]
            covers = cand.get("episodes") or []
            if covers and set(covers) <= have:
                skipped.append({"id": cid, "episodes": covers, "reason": "already_in_jellyfin"})
                continue
            if cid in data["queued"]:
                already.append({"id": cid, "episodes": covers, "audit_id": data["queued"][cid]})
                continue
            audit_id = searchgram.start_delivery(
                query=cand.get("query") or "", title=cand.get("title"), size=cand.get("size"),
                message_id=cand["message_id"], callback_data=cand["callback_data"],
                page_callback=cand.get("page_callback"), extra={"season_search_id": search_id, "episodes": covers})
            data["queued"][cid] = audit_id
            queued.append({"id": cid, "episodes": covers, "audit_id": audit_id})
        with approval_lock:
            connection.execute("UPDATE season_sessions SET data=? WHERE id=?", (json.dumps(data), search_id))
            connection.commit()
    return {"queued": queued, "already_queued": already, "skipped": skipped,
            "audit_ids": [q["audit_id"] for q in queued + already], "status": "processing" if queued else "no_change",
            "next_action": "ack_once_and_schedule_one_followup" if queued else "tell_user_nothing_new_was_queued",
            "follow_up_after_seconds": 30}


def season_candidates(search_id, episode):
    episode = _validate_int("episode", episode, 0, 999, optional=False)
    data = _load(search_id)
    pick = data["picks"].get(str(episode))
    rows = [c for c in data["candidates"].values() if episode in (c.get("episodes") or [])]
    rows.sort(key=lambda c: (c["id"] != pick, c.get("tier") or "z", c.get("size_mb") or 1e12))
    return {"search_id": search_id, "episode": episode, "pick": pick,
            "candidates": [{"id": c["id"], "title": c["title"], "size": c.get("size"), "res": c.get("res"),
                            "tier": c.get("tier"), "kind": c.get("kind"), "heb": c.get("heb")} for c in rows[:8]]}


def register_routes(router):
    router.post("/v1/media/find-season", lambda p, _q: find_season(
        p.get("title"), p.get("season"), p.get("title_en"), p.get("tmdb_id"), p.get("tvmaze_id"), p.get("year"),
        p.get("skip_in_jellyfin", True), p.get("refresh", False)))
    router.post("/v1/media/queue-season", lambda p, _q: queue_season_candidates(
        p.get("search_id"), p.get("episodes"), p.get("candidate_ids"), p.get("allow_pack", False)), 202)
    router.post("/v1/media/season-candidates", lambda p, _q: season_candidates(p.get("search_id"), p.get("episode")))
