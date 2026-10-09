def register(mcp, client):
    @mcp.tool()
    def system_health() -> dict:
        """Read the home server's uptime, load average, and available memory."""
        return client.request("GET", "/health")
