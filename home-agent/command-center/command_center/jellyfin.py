"""Read-only Jellyfin library capabilities."""

import json
import urllib.parse
import urllib.request

from .config import JELLYFIN_API_KEY, JELLYFIN_URL


def get(path, params):
    request = urllib.request.Request(
        JELLYFIN_URL + path + "?" + urllib.parse.urlencode(params),
        headers={"Authorization": f'MediaBrowser Token="{JELLYFIN_API_KEY}"'})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def search(query):
    query = str(query).strip()
    if not query:
        raise ValueError("Jellyfin search query is empty")
    if not JELLYFIN_URL or not JELLYFIN_API_KEY:
        raise ValueError("Jellyfin library search is not configured")
    data = get("/Items", {"SearchTerm": query, "Recursive": "true", "Limit": 20,
        "IncludeItemTypes": "Movie,Series,Episode", "Fields": "ProductionYear,UserData,SeriesName"})
    items = data.get("Items", [])
    if not isinstance(items, list):
        raise ValueError("Jellyfin returned an invalid search response")
    return {"query": query, "total": int(data.get("TotalRecordCount", len(items))),
            "results": [{"title": item.get("Name"), "type": item.get("Type"),
                         "year": item.get("ProductionYear"), "series": item.get("SeriesName"),
                         "watched": bool(item.get("UserData", {}).get("Played", False))}
                        for item in items]}


def normalized_title(value):
    return "".join(character for character in str(value).casefold() if character.isalnum())


def series_episodes(query, season):
    query = str(query).strip()
    if not query:
        raise ValueError("series query is empty")
    if isinstance(season, bool) or not isinstance(season, int) or season < 0:
        raise ValueError("season must be a non-negative number")
    if not JELLYFIN_URL or not JELLYFIN_API_KEY:
        raise ValueError("Jellyfin library search is not configured")
    fields = "ProductionYear,SortName"
    result = get("/Items", {"SearchTerm": query, "Recursive": "true", "Limit": 20,
                             "IncludeItemTypes": "Series", "Fields": fields})
    candidates = result.get("Items", [])
    fallback_used = False
    if not candidates:
        result = get("/Items", {"Recursive": "true", "Limit": 10000,
                                 "IncludeItemTypes": "Series", "Fields": fields})
        needle = normalized_title(query)
        candidates = [item for item in result.get("Items", [])
                      if needle in normalized_title(item.get("Name", ""))
                      or needle in normalized_title(item.get("SortName", ""))]
        fallback_used = True
    if not candidates:
        raise ValueError("series not found in Jellyfin")
    needle = normalized_title(query)
    series = next((item for item in candidates
                   if normalized_title(item.get("Name", "")) == needle), candidates[0])
    episodes = get(f"/Shows/{urllib.parse.quote(series['Id'], safe='')}/Episodes", {
        "Season": season, "Fields": "UserData,Overview"})
    items = episodes.get("Items", [])
    if not isinstance(items, list):
        raise ValueError("Jellyfin returned an invalid episode response")
    return {"series": series.get("Name"), "year": series.get("ProductionYear"), "season": season,
            "total": int(episodes.get("TotalRecordCount", len(items))),
            "fallback_used": fallback_used,
            "episodes": [{"number": item.get("IndexNumber"), "title": item.get("Name"),
                          "watched": item.get("UserData", {}).get("Played")
                          if isinstance(item.get("UserData"), dict) else None} for item in items]}


def register_routes(router):
    router.get("/v1/jellyfin/search", lambda _p, q: search(q.get("query", [""])[0]))
    router.post("/v1/jellyfin/series-episodes",
                lambda p, _q: series_episodes(p["query"], p["season"]))
