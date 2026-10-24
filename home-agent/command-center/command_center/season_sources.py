"""Episode-list sources for the season finder (media_find_season).

Source order (Ben, 2026-10-24):
1. TMDb is PRIMARY whenever a TMDb credential is configured. The show is resolved by its Tmdb provider id from
   Jellyfin (the library item's ProviderIds, or /Items/RemoteSearch/Series), never by name. For a library series
   TMDb's season list still wins (it catches episodes Jellyfin has not refreshed); discrepancies become notes.
2. No credential, or TMDb failed: a library series uses Jellyfin's own episode metadata. When that season holds
   no virtual ("missing") items it only knows the files on disk, so TVmaze (by ids) cross-checks the count.
   A series outside the library uses TVmaze resolved strictly by ids from Jellyfin's RemoteSearch:
   /lookup/shows?imdb=tt... first, then ?thetvdb=.
3. A name search (TVmaze /search/shows) is only a last resort and always returns candidates, never an auto-pick.
"Already have" always comes from the Jellyfin library.

Credentials: TMDB_READ_TOKEN (v4, sent as a Bearer header; preferred) or TMDB_API_KEY (v3 key as a query
parameter, or a v4 token, detected), each also as a *_FILE path. Values are never logged, cached or returned;
cache keys and error texts carry only the API path.
"""

import datetime
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import jellyfin
from .database import approval_lock, connection

TMDB_BASE = "https://api.themoviedb.org/3"
TVMAZE_BASE = "https://api.tvmaze.com"
USER_AGENT = "smarthome-command-center/season-finder"
CACHE_TTL = 24 * 3600
CACHE_TTL_AIRING = 6 * 3600
TVMAZE_MIN_INTERVAL = 0.6          # TVmaze allows ~20 calls / 10 s; stay well below

connection.execute("CREATE TABLE IF NOT EXISTS season_source_cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                   "expires INTEGER NOT NULL)")
connection.commit()

_tvmaze_lock = threading.Lock()
_tvmaze_last = [0.0]


class SourceError(Exception):
    """A source failed. ``str()`` is a short code safe for logs and tool output (never a URL or credential)."""


# --------------------------------------------------------------------------- helpers

def today_il():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Asia/Jerusalem")).date()
    except Exception:  # image without tzdata
        return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)).date()


def title_key(value):
    return jellyfin.normalized_title(str(value or "").replace("\u05f3", "'").replace("\u2019", "'"))


def has_hebrew(text):
    return any("\u0590" <= c <= "\u05ff" for c in str(text or ""))


def has_latin(text):
    return any(("a" <= c <= "z") or ("A" <= c <= "Z") for c in str(text or ""))


def _year(value):
    try:
        return int(str(value)[:4])
    except (TypeError, ValueError):
        return None


def _http_json(url, headers=None, timeout=15):
    """(status, parsed JSON or None). Never raises for HTTP status codes."""
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT,
                                                   **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8", "replace") or "null")
        except ValueError:
            body = None
        return exc.code, body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SourceError("network") from exc


def cache_get(key):
    row = connection.execute("SELECT value,expires FROM season_source_cache WHERE key=?", (key,)).fetchone()
    if row and row[1] > time.time():
        return json.loads(row[0])
    return None


def cache_put(key, value, ttl=CACHE_TTL):
    with approval_lock:          # one shared connection: serialize writes with the delivery threads
        connection.execute("INSERT OR REPLACE INTO season_source_cache VALUES (?,?,?)",
                           (key, json.dumps(value), int(time.time() + ttl)))
        connection.commit()


# --------------------------------------------------------------------------- TMDb

def _read_secret(name):
    value = os.environ.get(name, "").strip()
    if value:
        return value
    path = os.environ.get(name + "_FILE", "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return ""
    return ""


def tmdb_auth():
    """('bearer', token) | ('v3', key) | None. Never log the returned value."""
    token = _read_secret("TMDB_READ_TOKEN")
    if token:
        return "bearer", token
    key = _read_secret("TMDB_API_KEY")
    if key:
        return ("bearer", key) if (key.startswith("eyJ") or len(key) > 40) else ("v3", key)
    return None


def tmdb_configured():
    return tmdb_auth() is not None


def tmdb_get(path, params=None, ttl=CACHE_TTL):
    params = dict(params or {})
    key = "tmdb:" + path + ("?" + urllib.parse.urlencode(sorted(params.items())) if params else "")
    cached = cache_get(key)
    if cached is not None:
        return cached
    auth = tmdb_auth()
    if auth is None:
        raise SourceError("tmdb_not_configured")
    headers = {}
    if auth[0] == "bearer":
        headers["Authorization"] = "Bearer " + auth[1]
    else:
        params["api_key"] = auth[1]
    url = TMDB_BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    status, data = _http_json(url, headers)
    del url, headers, params
    if status == 404:
        raise SourceError("tmdb_not_found")
    if status != 200 or not isinstance(data, dict):
        raise SourceError(f"tmdb_http_{status}")
    cache_put(key, data, ttl)
    return data


def tmdb_show(tmdb_id):
    data = tmdb_get(f"/tv/{int(tmdb_id)}", {"language": "he-IL", "append_to_response": "external_ids,translations"})
    english = next((t.get("data", {}).get("name") for t in data.get("translations", {}).get("translations", [])
                    if t.get("iso_639_1") == "en" and t.get("data", {}).get("name")), None)
    ext = data.get("external_ids") or {}
    return {"tmdb": int(data.get("id") or tmdb_id), "name": data.get("name"),
            "original_name": data.get("original_name"), "english_name": english,
            "year": _year(data.get("first_air_date")), "imdb": ext.get("imdb_id"),
            "tvdb": ext.get("tvdb_id"), "status": data.get("status"),
            "seasons": {int(s["season_number"]): s.get("episode_count") for s in data.get("seasons") or []
                        if s.get("season_number") is not None},
            "runtime": (data.get("episode_run_time") or [None])[0]}


def tmdb_season(tmdb_id, season):
    data = tmdb_get(f"/tv/{int(tmdb_id)}/season/{int(season)}", ttl=CACHE_TTL)
    episodes = [{"n": int(e["episode_number"]), "name": e.get("name"), "air_date": e.get("air_date"),
                 "runtime": e.get("runtime")} for e in data.get("episodes") or [] if e.get("episode_number")]
    if episodes and any(not e["air_date"] or e["air_date"] >= str(today_il() - datetime.timedelta(days=14))
                        for e in episodes):
        cache_put(f"tmdb:/tv/{int(tmdb_id)}/season/{int(season)}", data, CACHE_TTL_AIRING)
    return episodes


def tmdb_find(imdb=None, tvdb=None):
    for value, source in ((imdb, "imdb_id"), (tvdb, "tvdb_id")):
        if value:
            data = tmdb_get(f"/find/{urllib.parse.quote(str(value))}", {"external_source": source})
            results = data.get("tv_results") or []
            if results:
                return int(results[0]["id"])
    return None


# --------------------------------------------------------------------------- TVmaze

def tvmaze_get(path, params=None, ttl=CACHE_TTL):
    key = "tvmaze:" + path + ("?" + urllib.parse.urlencode(sorted((params or {}).items())) if params else "")
    cached = cache_get(key)
    if cached is not None:
        return cached
    url = TVMAZE_BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for attempt in range(2):
        with _tvmaze_lock:
            wait = _tvmaze_last[0] + TVMAZE_MIN_INTERVAL - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _tvmaze_last[0] = time.monotonic()
        status, data = _http_json(url)
        if status == 429 and attempt == 0:
            time.sleep(2)
            continue
        break
    if status == 404:
        cache_put(key, {"__not_found__": True}, 3600)
        return None
    if status != 200:
        raise SourceError(f"tvmaze_http_{status}")
    cache_put(key, data, ttl)
    return data


def _not_found(data):
    return data is None or (isinstance(data, dict) and data.get("__not_found__"))


def tvmaze_lookup(imdb=None, tvdb=None):
    """TVmaze show by external id only: imdb first, then thetvdb. None when neither resolves."""
    for param, value in (("imdb", imdb), ("thetvdb", tvdb)):
        if value:
            data = tvmaze_get("/lookup/shows", {param: value})
            if not _not_found(data):
                return data
    return None


def tvmaze_season(show_id, season):
    seasons = tvmaze_get(f"/shows/{int(show_id)}/seasons")
    if _not_found(seasons):
        raise SourceError("tvmaze_not_found")
    meta = next((s for s in seasons if s.get("number") == int(season)), None)
    if meta is None:
        raise SourceError("season_not_found")
    episodes = tvmaze_get(f"/seasons/{int(meta['id'])}/episodes")
    episodes = [] if _not_found(episodes) else episodes
    listed = [{"n": int(e["number"]), "name": e.get("name"), "air_date": e.get("airdate") or None,
               "runtime": e.get("runtime")} for e in episodes
              if e.get("type", "regular") == "regular" and e.get("number")]
    return meta.get("episodeOrder"), listed


def tvmaze_search(name):
    data = tvmaze_get("/search/shows", {"q": name})
    return [] if _not_found(data) else [
        {"tvmaze_id": item["show"]["id"], "name": item["show"].get("name"),
         "year": _year(item["show"].get("premiered")), "imdb": (item["show"].get("externals") or {}).get("imdb")}
        for item in data[:5]]


# --------------------------------------------------------------------------- Jellyfin

def _ids(provider_ids):
    lower = {str(k).lower(): v for k, v in (provider_ids or {}).items() if v}
    tmdb = lower.get("tmdb")
    return {"tmdb": int(tmdb) if str(tmdb or "").isdigit() else None, "imdb": lower.get("imdb"),
            "tvdb": int(lower["tvdb"]) if str(lower.get("tvdb") or "").isdigit() else None}


def library_series(titles, tmdb_id=None):
    keys = {title_key(t) for t in titles if title_key(t)}
    for title in titles:
        data = jellyfin.get("/Items", {"SearchTerm": title, "Recursive": "true", "Limit": 20,
                                       "IncludeItemTypes": "Series",
                                       "Fields": "ProviderIds,ProductionYear,OriginalTitle,SortName"})
        for item in data.get("Items") or []:
            ids = _ids(item.get("ProviderIds"))
            names = {title_key(item.get(k)) for k in ("Name", "OriginalTitle", "SortName")}
            if (tmdb_id and ids["tmdb"] == int(tmdb_id)) or (not tmdb_id and names & keys):
                return {"id": item["Id"], "name": item.get("Name"), "original_name": item.get("OriginalTitle"),
                        "year": item.get("ProductionYear"), **ids}
    return None


def library_episodes(series_id, season):
    data = jellyfin.get(f"/Shows/{urllib.parse.quote(str(series_id), safe='')}/Episodes",
                        {"Season": season, "Fields": "ProviderIds"})
    out = []
    for item in data.get("Items") or []:
        if item.get("IndexNumber") is None:
            continue
        ticks = item.get("RunTimeTicks")
        out.append({"n": int(item["IndexNumber"]), "end": item.get("IndexNumberEnd"),
                    "name": item.get("Name"), "runtime": round(ticks / 600_000_000) if ticks else None,
                    "virtual": item.get("LocationType") == "Virtual",
                    "air_date": (item.get("PremiereDate") or "")[:10] or None})
    return out


def remote_search(title, year=None, provider_ids=None):
    info = {"Name": title or "", "MetadataLanguage": "he" if has_hebrew(title) else "en"}
    if year:
        info["Year"] = int(year)
    if provider_ids:
        info["ProviderIds"] = provider_ids
    data = jellyfin.post_json("/Items/RemoteSearch/Series",
                              {"SearchInfo": info, "SearchProviderName": "TheMovieDb",
                               "IncludeDisabledProviders": True})
    return [{"name": r.get("Name"), "year": r.get("ProductionYear"), **_ids(r.get("ProviderIds"))}
            for r in (data or [])]


# --------------------------------------------------------------------------- resolution

def _aired(episode, today):
    return bool(episode.get("air_date")) and episode["air_date"] <= str(today)


def resolve(titles, season, *, tmdb_id=None, tvmaze_id=None, year=None):
    """Identify the show and its season episode list. See the module docstring for the order."""
    titles = [t for t in dict.fromkeys(str(t).strip() for t in titles if str(t or "").strip())]
    today = today_il()
    notes, have = [], []
    show = {"name": titles[0] if titles else None, "original_name": None, "english_name": None, "year": year,
            "tmdb": int(tmdb_id) if tmdb_id else None, "imdb": None, "tvdb": None,
            "tvmaze": int(tvmaze_id) if tvmaze_id else None, "in_library": False}
    lib_eps = None
    # 1. Library (also the only source of "already have").
    try:
        lib = library_series(titles, tmdb_id=tmdb_id)
    except Exception:
        lib = None
        notes.append("jellyfin_unavailable")
    if lib:
        show.update({k: lib[k] or show.get(k) for k in ("name", "original_name", "year", "tmdb", "imdb", "tvdb")})
        show["in_library"] = True
        try:
            lib_eps = library_episodes(lib["id"], season)
        except Exception:
            notes.append("jellyfin_episodes_unavailable")
        for e in lib_eps or []:
            if not e["virtual"]:
                have.extend(range(e["n"], int(e["end"] or e["n"]) + 1))
    # 2. Identify by provider ids through Jellyfin RemoteSearch (never by name alone).
    if not show["in_library"] and not show["tmdb"] and not show["tvmaze"]:
        keys = {title_key(t) for t in titles}
        candidates = []
        try:
            for title in titles:
                results = remote_search(title, year)
                exact = [r for r in results if title_key(r["name"]) in keys]
                if year:
                    exact = [r for r in exact if not r["year"] or abs(int(r["year"]) - int(year)) <= 1]
                if len(exact) == 1:
                    show.update({k: exact[0][k] for k in ("tmdb", "imdb", "tvdb")})
                    show.update({"name": exact[0]["name"], "year": exact[0]["year"] or year})
                    candidates = []
                    break
                candidates = candidates or (exact if len(exact) > 1 else results)[:5]
        except Exception:
            notes.append("jellyfin_remote_search_unavailable")
        if not show["tmdb"] and candidates:
            return {"status": "ambiguous", "show": show, "notes": notes, "have": have,
                    "candidates": [{"name": c["name"], "year": c["year"], "tmdb_id": c["tmdb"]} for c in candidates]}
        if not show["tmdb"]:
            # 3. Last resort: TVmaze name search -> candidates only.
            try:
                name = next((t for t in titles if has_latin(t)), titles[0] if titles else "")
                found = tvmaze_search(name) if name else []
            except SourceError as exc:
                found = []
                notes.append(str(exc))
            if found:
                return {"status": "ambiguous", "show": show, "notes": notes + ["identified_by_name_only"],
                        "have": have, "candidates": found}
            return {"status": "unknown", "source": None, "show": show, "episodes": None, "count_known": False,
                    "have": have, "unaired": [], "notes": notes + ["show_not_identified"]}

    episodes, source, count_known = None, None, False
    # 1. TMDb primary when configured.
    if tmdb_configured():
        try:
            if not show["tmdb"]:
                show["tmdb"] = tmdb_find(show["imdb"], show["tvdb"])
            if show["tmdb"]:
                meta = tmdb_show(show["tmdb"])
                show.update({"name": meta["name"] or show["name"], "original_name": meta["original_name"],
                             "english_name": meta["english_name"], "year": meta["year"] or show["year"],
                             "imdb": show["imdb"] or meta["imdb"], "tvdb": show["tvdb"] or meta["tvdb"]})
                episodes, source, count_known = tmdb_season(show["tmdb"], season), "tmdb", True
        except SourceError as exc:
            notes.append(str(exc).replace("tmdb_not_found", "tmdb_season_not_found"))
    # 2. Jellyfin library metadata.
    if episodes is None and lib_eps:
        episodes = [{"n": e["n"], "name": e["name"], "air_date": e["air_date"], "runtime": e["runtime"]}
                    for e in lib_eps]
        for e in lib_eps:   # a multi-episode file covers IndexNumber..IndexNumberEnd
            for n in range(e["n"] + 1, int(e["end"] or e["n"]) + 1):
                episodes.append({"n": n, "name": e["name"], "air_date": e["air_date"], "runtime": e["runtime"]})
        source, count_known = "jellyfin_library", True
        if not any(e["virtual"] for e in lib_eps):
            notes.append("jellyfin_lists_files_only")
    # 3. TVmaze by ids (fallback, or cross-check of a files-only library list).
    if episodes is None or (source == "jellyfin_library" and "jellyfin_lists_files_only" in notes):
        try:
            if not show["tvmaze"]:
                if show["tmdb"] and not (show["imdb"] or show["tvdb"]):
                    remote = remote_search(show["name"], provider_ids={"Tmdb": str(show["tmdb"])})
                    if remote:
                        show["imdb"] = show["imdb"] or remote[0]["imdb"]
                        show["tvdb"] = show["tvdb"] or remote[0]["tvdb"]
                found = tvmaze_lookup(show["imdb"], show["tvdb"])
                show["tvmaze"] = found["id"] if found else None
                if found and not show.get("english_name") and has_latin(found.get("name")):
                    show["english_name"] = found.get("name")
            if show["tvmaze"]:
                order, listed = tvmaze_season(show["tvmaze"], season)
                if episodes is None:
                    episodes, source, count_known = listed, "tvmaze", order is not None
                    if order is not None and len(listed) < order:
                        notes.append(f"tvmaze_lists_{len(listed)}_of_{order}")
                        known = {e["n"] for e in listed}
                        episodes += [{"n": n, "name": None, "air_date": None, "runtime": None}
                                     for n in range(1, order + 1) if n not in known]
                elif order and order > len({e["n"] for e in episodes}):
                    notes.append(f"jellyfin_has_{len({e['n'] for e in episodes})}_tvmaze_says_{order}")
                    known = {e["n"] for e in episodes}
                    episodes += [e for e in listed if e["n"] not in known]
                    episodes += [{"n": n, "name": None, "air_date": None, "runtime": None}
                                 for n in range(1, order + 1) if n not in known and n not in {x["n"] for x in listed}]
                    source = "jellyfin_library+tvmaze"
            elif episodes is None:
                notes.append("tvmaze_no_id_match")
        except SourceError as exc:
            notes.append(str(exc))
        except Exception:
            notes.append("id_lookup_failed")

    if episodes is None:
        return {"status": "unknown", "source": None, "show": show, "episodes": None, "count_known": False,
                "have": sorted(set(have)), "unaired": [], "notes": notes}
    episodes = sorted({e["n"]: e for e in episodes}.values(), key=lambda e: e["n"])
    if source == "tmdb" and lib_eps:
        lib_count = len({e["n"] for e in lib_eps})
        if lib_count != len(episodes):
            notes.append(f"jellyfin_has_{lib_count}_tmdb_says_{len(episodes)}")
    aired = [e for e in episodes if _aired(e, today) or (source.startswith("jellyfin") and not e["air_date"])]
    unaired = [e["n"] for e in episodes if e not in aired]
    return {"status": "ok" if count_known else "unknown", "source": source, "show": show,
            "episodes": [{"n": e["n"], "runtime": e.get("runtime")} for e in aired],
            "episode_count": len(episodes) if count_known else None, "count_known": count_known,
            "have": sorted(set(have)), "unaired": unaired, "notes": notes,
            "known_episodes": [e["n"] for e in episodes]}
