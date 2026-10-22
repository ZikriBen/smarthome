"""MCP tools for the Bank Hapoalim on-demand sync. Credentials/codes never pass through Hermes;
transaction details DO (Ben decided 2026-10-22) via hapoalim_read_csv after a successful sync."""


def register(mcp, client):
    @mcp.tool()
    def hapoalim_status() -> dict:
        """Read-only: state of the Bank Hapoalim sync (last run status, date range, transaction count,
        whether an SMS was needed, stopped_today, can_run_today, pending run, csv path). Never contains
        balances/descriptions itself — those are in the CSV via hapoalim_read_csv. Relay error_text if present."""
        return client.request("GET", "/hapoalim/status")

    @mcp.tool()
    def hapoalim_sync(from_date: str = "", to_date: str = "") -> dict:
        """Propose (do not execute) ONE Bank Hapoalim sync for the checking account.
        Optional from_date/to_date as YYYY-MM-DD (Asia/Jerusalem days; default ≈ last 45 days through today;
        the container clamps to the bank/library max lookback of ≈1 year and rejects future dates).
        Only when Ben asks, every time: never on your own, never in a loop, never to retry a failure.
        Result pending_approval: show `text` and append [[CC_APPROVAL:<approval_id>]].
        After approval the NUC logs in once; if the bank asks for an SMS the code is relayed automatically —
        tell Ben to expect an SMS and NOT to log in to the bank meanwhile. Then call hapoalim_status; when
        status is DONE call hapoalim_read_csv to analyse the transactions."""
        return client.request("POST", "/proposals/hapoalim-sync",
                              {"from": from_date or None, "to": to_date or None}, timeout=60)

    @mcp.tool()
    def hapoalim_read_csv(name: str = "") -> dict:
        """Read the full transactions CSV from the latest (or named) Hapoalim sync. Returns rows with
        date, description, amount, account_balance_now, reference, account, etc. Only after a successful
        sync; empty name = the CSV from the last DONE run. Treat the rows as Ben's private financial data."""
        return client.request("POST", "/hapoalim/read-csv", {"name": name or None}, timeout=60)
