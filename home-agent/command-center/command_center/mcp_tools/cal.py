"""MCP tools for the Cal (visaCal) credit-card on-demand sync. Credentials/codes never pass through Hermes;
card charges DO (same decision as the bank, 2026-10-22) via the archive read tools."""


def register(mcp, client):
    @mcp.tool()
    def card_status() -> dict:
        """Read-only: state of the Cal credit-card sync (last run status, billing months, card count (last-4
        only), whether an SMS was needed, stopped_today, can_run_today, runs_today/max_runs/limit_reached/
        resets_at, pending run). If can_run_today is false, do not offer a card sync today. Relay error_text."""
        return client.request("GET", "/cal/status")

    @mcp.tool()
    def card_sync(from_month: str = "", to_month: str = "") -> dict:
        """Propose (do not execute) ONE Cal credit-card sync. Optional from_month/to_month as YYYY-MM BILLING
        (statement) months; default = the last 3 statements + the upcoming one; max ≈18 months back.
        Only when Ben asks, every time: never on your own, never in a loop, never to retry a failure.
        Result DAILY_LIMIT: today's Cal login limit is used up — do NOT offer or retry today; answer from the
        archive or suggest tomorrow. Result COVERED: archive already has those months — no approval/SMS; use
        card_read_month / card_read_range. Result pending_approval: show `text` and append
        [[CC_APPROVAL:<approval_id>]]. After approval the NUC logs in to Cal once; an SMS code is relayed
        automatically only if Cal asks — tell Ben NOT to log in to Cal meanwhile. Then card_status."""
        return client.request("POST", "/proposals/card-sync",
                              {"from_month": from_month or None, "to_month": to_month or None}, timeout=60)

    @mcp.tool()
    def card_read_month(yyyy_mm: str, card: str = "", include_pending: bool = False) -> dict:
        """Read one Cal BILLING month (YYYY-MM = the statement month). Optional card = last-4 digits.
        Rows: purchase_date (original purchase day), charge_date (debit day), amount (negative = charge),
        installment_number/total, description. Private financial data. If complete is false, never say
        "no charges" for that month; offer card_sync. include_pending adds not-yet-billed charges."""
        return client.request("POST", "/cal/read-month",
                              {"month": yyyy_mm, "card": card or None, "include_pending": include_pending},
                              timeout=60)

    @mcp.tool()
    def card_read_range(from_date: str, to_date: str, card: str = "", by: str = "charge") -> dict:
        """Read Cal archive rows for [from_date, to_date] (YYYY-MM-DD). by="charge" (default) filters on the
        debit day; by="purchase" on the original purchase day (scans up to 2 later statements). Optional card
        (last-4). No login. `uncovered_months` are statements the archive lacks: never report "no charges"."""
        return client.request("POST", "/cal/read-range",
                              {"from": from_date, "to": to_date, "card": card or None, "by": by}, timeout=60)

    @mcp.tool()
    def card_list_archive() -> dict:
        """List the Cal archive files (card last-4, billing month, rows, complete/final). No login."""
        return client.request("GET", "/cal/list-archive")

    @mcp.tool()
    def card_coverage() -> dict:
        """Which Cal billing months are complete+final per card (and which are still open/partial)."""
        return client.request("GET", "/cal/coverage")
