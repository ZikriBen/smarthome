"""Read-only OpenStreetMap place search."""

import json
import urllib.parse
import urllib.request


def search(query):
    encoded = urllib.parse.quote(query)
    request = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/search?q={encoded}&format=jsonv2&limit=5",
        headers={"User-Agent": "CommandCenter/1.0"})
    return json.loads(urllib.request.urlopen(request, timeout=20).read())


def register_routes(router):
    router.post("/v1/maps/search", lambda p, _q: search(p["query"]))
