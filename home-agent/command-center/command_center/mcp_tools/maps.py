def register(mcp, client):
    @mcp.tool()
    def map_search(query: str) -> list[dict]:
        """Find a place using OpenStreetMap. This is read-only."""
        return client.request("POST", "/maps/search", {"query": query})
