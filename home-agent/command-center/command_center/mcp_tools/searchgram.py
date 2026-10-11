def register(mcp, client):
    @mcp.tool()
    def media_download_status() -> dict:
        """Read Telegram Downloader queue counts and available media disk space. This is read-only."""
        return client.request("GET", "/media/download-status")

    @mcp.tool()
    def searchgram_search(query: str) -> dict:
        """Search SearchGram and return the first page of numbered media results. Use next or previous page tools to browse."""
        return client.request("POST", "/searchgram/search", {"query": query})

    @mcp.tool()
    def searchgram_next_page(search_id: str) -> dict:
        """Show the next page for a prior SearchGram search."""
        return client.request("POST", "/searchgram/next-page", {"search_id": search_id})

    @mcp.tool()
    def searchgram_previous_page(search_id: str) -> dict:
        """Show the previous page for a prior SearchGram search."""
        return client.request("POST", "/searchgram/previous-page", {"search_id": search_id})

    @mcp.tool()
    def queue_searchgram_result(search_id: str, result_number: int) -> dict:
        """Queue one confirmed SearchGram result asynchronously. Acknowledge it naturally and briefly, then schedule one consolidated `in 1m` follow-up for all audit IDs from the request. The follow-up must produce a short user-facing update without audit IDs, raw filenames, HTTP codes, or queue diagnostics. Never retry automatically."""
        return client.request("POST", "/searchgram/queue", {
            "search_id": search_id, "result_number": result_number})

    @mcp.tool()
    def searchgram_delivery_status(audit_id: str, wait_seconds: int = 0) -> dict:
        """Check one exact SearchGram delivery and downloader state. A scheduled follow-up may wait up to 90 seconds for a terminal result. HTTP 504 means unconfirmed delivery; report it and never retry automatically."""
        wait_seconds = max(0, min(int(wait_seconds), 90))
        return client.request("POST", "/searchgram/delivery-status",
                              {"audit_id": audit_id, "wait_seconds": wait_seconds},
                              timeout=wait_seconds + 30)
