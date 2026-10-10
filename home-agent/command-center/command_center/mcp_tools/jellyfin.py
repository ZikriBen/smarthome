import urllib.parse


def register(mcp, client):
    @mcp.tool()
    def jellyfin_search(query: str) -> dict:
        """Search the existing Jellyfin library. Returns matching title, media type, year, series and watched state; this is read-only."""
        return client.request("GET", "/jellyfin/search?" + urllib.parse.urlencode({"query": query}))

    @mcp.tool()
    def jellyfin_series_episodes(query: str, season: int) -> dict:
        """List a Jellyfin season's numbered episodes. If found is false, retry with the title translated to English; a missing localized title is a normal result, not an error. This is read-only."""
        return client.request("POST", "/jellyfin/series-episodes", {"query": query, "season": season})
