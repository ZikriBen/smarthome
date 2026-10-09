import urllib.parse


def register(mcp, client):
    @mcp.tool()
    def jellyfin_search(query: str) -> dict:
        """Search the existing Jellyfin library. Returns matching title, media type, year, series and watched state; this is read-only."""
        return client.request("GET", "/jellyfin/search?" + urllib.parse.urlencode({"query": query}))

    @mcp.tool()
    def jellyfin_series_episodes(query: str, season: int) -> dict:
        """List the numbered episodes available for one Jellyfin series season. Searches normally first, then falls back to the complete series list if no result is found; this is read-only."""
        return client.request("POST", "/jellyfin/series-episodes", {"query": query, "season": season})
