def register(mcp, client):
    @mcp.tool()
    def uptime_kuma_status() -> dict:
        """Read Uptime Kuma's monitored-service health and response-time summary. This is read-only."""
        return client.request("GET", "/uptime-kuma/monitors")
