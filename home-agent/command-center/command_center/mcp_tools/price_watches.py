def register(mcp, client):
    @mcp.tool()
    def price_watch_status() -> dict:
        """List price watches and recent price alerts. This is read-only."""
        return {"watches": client.request("GET", "/price-watches"),
                "alerts": client.request("GET", "/price-alerts")}

    @mcp.tool()
    def price_quote(url: str) -> dict:
        """Read a machine-readable price from a public product page; private URLs are rejected."""
        return client.request("POST", "/price-watches/quote", {"url": url})

    @mcp.tool()
    def propose_price_watch(url: str, target: float, currency: str = "USD",
                            every_minutes: int = 360) -> dict:
        """Create, but do not execute, a price-watch proposal requiring human approval."""
        return client.request("POST", "/proposals/price-watch", {
            "url": url, "target": target, "currency": currency,
            "every_minutes": every_minutes})
