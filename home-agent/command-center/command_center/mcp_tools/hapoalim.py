"""MCP tools for the Bank Hapoalim monthly report. Hermes never sees credentials, codes, balances or the CSV."""


def register(mcp, client):
    @mcp.tool()
    def hapoalim_status() -> dict:
        """Read-only: state of the Bank Hapoalim monthly report (last run status/error type, report month,
        transaction count, whether an SMS was needed, locked_out, can_run_today, a pending run). Never contains
        balances, account numbers, transactions or codes. If `error_text` is present, relay it to Ben."""
        return client.request("GET", "/hapoalim/status")

    @mcp.tool()
    def hapoalim_run_report(month: str = "") -> dict:
        """Propose (do not execute) ONE run of the Bank Hapoalim monthly report for `month` (YYYY-MM; empty =
        last month). Only when Ben asks for it, every time: never on your own, never in a loop, never to retry a
        failure. Result pending_approval: show `text` and append the exact final marker
        [[CC_APPROVAL:<approval_id>]]; Ben's approval queues the run on the NUC. If the bank asks for an SMS the
        code is relayed automatically once — tell Ben to expect an SMS from the bank and NOT to log in to the bank
        meanwhile. locked_out/daily_limit/already_pending: relay `text`, do not propose again. The CSV stays on
        the NUC; afterwards check hapoalim_status once."""
        return client.request("POST", "/proposals/hapoalim-run", {"month": month})
