"""MCP tools for the credit-card on-demand sync (multi-account; provider cal = visaCal). Credentials/codes never
pass through Hermes; card charges DO (same decision as the bank, 2026-10-22) via the archive read tools.

Every tool takes an optional `account` (stable short id such as "cal-main" or "cal-2"; see card_status ->
accounts). Empty = the default account cal-main, so calls without it behave exactly as before."""

ACCOUNT_DOC = (" Optional account = card-account id (e.g. cal-main, cal-2; list: card_status -> accounts); "
               "empty = cal-main.")


def register(mcp, client):
    @mcp.tool()
    def card_status(account: str = "") -> dict:
        """Read-only: state of the credit-card sync (last run status, billing months, card count (last-4
        only), whether an SMS was needed, stopped_today, can_run_today, runs_today/max_runs/limit_reached/
        resets_at, pending run). Without account: the top level is the default account (cal-main) and
        `accounts` holds the same status for EVERY card account (id, label, provider). With account: only that
        one. If can_run_today is false, do not offer a card sync today for that account. Relay error_text.
        provider_stopped_today / provider_stops = a bot-detection/429/captcha block on ONE account stopped ALL
        accounts of that provider until midnight (reason, triggered_by account, at_il)."""
        if account:
            return client.request("POST", "/cal/status", {"account": account})
        return client.request("GET", "/cal/status")

    @mcp.tool()
    def card_sync(from_month: str = "", to_month: str = "", account: str = "") -> dict:
        """Propose (do not execute) ONE credit-card sync of ONE card account (account empty = cal-main).
        Optional from_month/to_month as YYYY-MM BILLING (statement) months; default = the last 3 statements +
        the upcoming one; max ≈18 months back. Only when Ben asks, every time: never on your own, never in a
        loop, never to retry a failure, never several accounts in a row without Ben asking for each.
        Result DAILY_LIMIT: today's login limit for that account (or all accounts) is used up — do NOT offer or
        retry today; answer from the archive or suggest tomorrow. Result PROVIDER_STOPPED_TODAY: Cal blocked
        the fetch today on account `triggered_by`, so EVERY Cal account is stopped until midnight — do not offer
        or retry any Cal account today. Result COVERED: archive already has those
        months — no approval/SMS; use card_read_month / card_read_range. Result pending_approval: show `text`
        and append [[CC_APPROVAL:<approval_id>]]. After approval the NUC logs in once; an SMS code is relayed
        automatically only if the card company asks — tell Ben NOT to log in meanwhile. Then card_status."""
        return client.request("POST", "/proposals/card-sync",
                              {"from_month": from_month or None, "to_month": to_month or None,
                               "account": account or None}, timeout=60)

    @mcp.tool()
    def card_read_month(yyyy_mm: str, card: str = "", include_pending: bool = False, account: str = "") -> dict:
        """Read one BILLING month (YYYY-MM = the statement month) of one card account. Optional card = last-4
        digits; card alone finds its account (status AMBIGUOUS_CARD if two accounts have it: pass account).
        Neither = cal-main. Rows: purchase_date (original purchase day), charge_date (debit day), amount
        (negative = charge), installment_number/total, description. Private financial data. If complete is
        false, never say "no charges" for that month; offer card_sync. include_pending adds not-yet-billed
        charges. For a total over ALL cards, call once per account from card_status -> accounts."""
        return client.request("POST", "/cal/read-month",
                              {"month": yyyy_mm, "card": card or None, "include_pending": include_pending,
                               "account": account or None}, timeout=60)

    @mcp.tool()
    def card_read_range(from_date: str, to_date: str, card: str = "", by: str = "charge", account: str = "") -> dict:
        """Read archive rows for [from_date, to_date] (YYYY-MM-DD) of one card account. by="charge" (default)
        filters on the debit day; by="purchase" on the original purchase day (scans up to 2 later statements).
        Optional card (last-4; alone it finds its account, AMBIGUOUS_CARD if in two). Neither = cal-main.
        No login. `uncovered_months` are statements the archive lacks: never report "no charges"."""
        return client.request("POST", "/cal/read-range",
                              {"from": from_date, "to": to_date, "card": card or None, "by": by,
                               "account": account or None}, timeout=60)

    @mcp.tool()
    def card_list_archive(account: str = "") -> dict:
        """List the card archive files (card last-4, billing month, rows, complete/final). No login. Without
        account: top level = cal-main, `accounts` = every account. With account: only that one."""
        if account:
            return client.request("POST", "/cal/list-archive", {"account": account})
        return client.request("GET", "/cal/list-archive")

    @mcp.tool()
    def card_coverage(account: str = "") -> dict:
        """Which billing months are complete+final per card (and which are still open/partial). Without
        account: top level = cal-main, `accounts` = every account grouped. With account: only that one."""
        if account:
            return client.request("POST", "/cal/coverage", {"account": account})
        return client.request("GET", "/cal/coverage")
