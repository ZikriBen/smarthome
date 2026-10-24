def register(mcp, client):
    @mcp.tool()
    def media_find_season(title: str, season: int, title_en: str | None = None, tmdb_id: int | None = None,
                          tvmaze_id: int | None = None, year: int | None = None, skip_in_jellyfin: bool = True,
                          refresh: bool = False) -> dict:
        """Find a FULL SEASON in one call: identifies the show and its episode list, skips episodes already in
        Jellyfin, runs one season-level SearchGram scan and returns one ranked pick per needed episode (smallest
        sufficient file; each pick has a short 'why'), the missing episodes and whether a season pack exists.
        Follow the returned next_action/instruction. Pass title_en too when you know the English title. Read-only."""
        return client.request("POST", "/media/find-season", {
            "title": title, "season": season, "title_en": title_en, "tmdb_id": tmdb_id, "tvmaze_id": tvmaze_id,
            "year": year, "skip_in_jellyfin": skip_in_jellyfin, "refresh": refresh}, timeout=240)

    @mcp.tool()
    def queue_season_candidates(search_id: str, episodes: list[int] | None = None,
                                candidate_ids: list[str] | None = None, allow_pack: bool = False) -> dict:
        """Queue a confirmed season result from media_find_season in ONE call: no arguments besides search_id =
        all picks; or only some episodes; or explicit candidate ids. Idempotent (repeats return the same audit
        IDs). A season pack is refused unless the user explicitly chose the pack and allow_pack=true. Acknowledge
        once and schedule one consolidated `in 1m` follow-up for the returned audit_ids. Never retry automatically."""
        return client.request("POST", "/media/queue-season", {
            "search_id": search_id, "episodes": episodes, "candidate_ids": candidate_ids,
            "allow_pack": allow_pack})

    @mcp.tool()
    def media_season_candidates(search_id: str, episode: int) -> dict:
        """List the alternatives found for one episode of a media_find_season result (no new search). Use only
        when the user asks about a specific episode's options. Read-only."""
        return client.request("POST", "/media/season-candidates", {"search_id": search_id, "episode": episode})
