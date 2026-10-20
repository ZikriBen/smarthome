"""MCP tools for the Histadrut "הסופר החברתי" login. The ID number never passes through Hermes."""


def register(mcp, client):
    @mcp.tool()
    def super_hist_login_start() -> dict:
        """Log in to הסופר החברתי (super-hist.co.il): asks the site to SMS a 6-digit code to Ben's phone, then waits up to ~45s for the code to arrive by itself (phone SMS forwarder -> Telegram relay) and submits it. Use only when Ben asks to connect, or a הסופר החברתי task needs a login and super_hist_status says logged_out — tell Ben first ('מבקש קוד מהסופר החברתי…'). Result logged_in (auto=true): relay `text`. code_sent: the code did not arrive by itself — ask Ben to paste the 6 digits (no time pressure; ~10 min), or call super_hist_login_code() with no code to check the relay again once. Never call start again by yourself (no auto-resend); on cooldown/rate_limited just relay `text`."""
        return client.request("POST", "/super-hist/login/start", {}, timeout=170)

    @mcp.tool()
    def super_hist_login_code(code: str = "") -> dict:
        """Submit the 6-digit SMS code Ben pasted for הסופר החברתי — pass exactly the digits he sent; never guess or reuse a code. With code empty, waits up to 30s for the auto-relayed code instead (no_relayed_code -> ask Ben to paste). Relay `text`. wrong_code: ask him to re-check the SMS (attempts_left). no_pending_login/too_many_attempts: offer to start again; don't restart by yourself."""
        return client.request("POST", "/super-hist/login/code", {"code": code}, timeout=120)

    @mcp.tool()
    def super_hist_status() -> dict:
        """Read-only: is the saved הסופר החברתי session logged in (logged_in / code_sent / logged_out)? Check this before starting a login."""
        return client.request("GET", "/super-hist/status", timeout=60)

    @mcp.tool()
    def super_hist_logout() -> dict:
        """Forget the saved הסופר החברתי session on the server (only when Ben asks)."""
        return client.request("POST", "/super-hist/logout", {})
