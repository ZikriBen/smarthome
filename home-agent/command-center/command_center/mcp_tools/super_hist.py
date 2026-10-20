"""MCP tools for the Histadrut "הסופר החברתי" login and cart. The ID number never passes through Hermes; cart writes are approval-only and checkout/orders are unreachable."""


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

    # ------------------------------------------------------------------ cart (account-level, shared with the site/app)

    @mcp.tool()
    def super_hist_cart_view() -> dict:
        """Read-only: what is in Ben's הסופר החברתי cart (lines, quantities, unit prices, total). The cart is shared with the site and app. login_required: tell Ben the session expired and ask whether to send an SMS code; never log in by yourself. site_blocked/site_changed: relay `text`, do not retry."""
        return client.request("GET", "/super-hist/cart", timeout=120)

    @mcp.tool()
    def super_hist_search(term: str) -> dict:
        """Read-only product search on הסופר החברתי (one request; the site returns only the top few matches). Each product has product_id, name, price (the price actually paid: the sale price when on sale), shelf_price, on_sale, in_cart, out_of_stock, minimum, weighted. Use product_id from here for super_hist_cart_add. Searches are slow (>=10s apart): search once per item, never loop."""
        return client.request("POST", "/super-hist/search", {"term": term}, timeout=120)

    @mcp.tool()
    def super_hist_cart_add(product_id: str, quantity: int = 1) -> dict:
        """Propose (do not execute) making sure Ben's הסופר החברתי cart has AT LEAST `quantity` of a product found with super_hist_search: not in the cart -> add it; fewer -> raise to `quantity`; already enough -> already_in_cart, nothing proposed. Result pending_approval: show `text` (item, price, qty before→after, expected new total) and append the exact final marker [[CC_APPROVAL:<approval_id>]]; the Telegram approval performs the write. Never claim it was added before approval. Weighted (by-kg) items and out-of-stock items are refused."""
        return client.request("POST", "/proposals/super-hist-cart/add",
                              {"product_id": product_id, "quantity": quantity}, timeout=120)

    @mcp.tool()
    def super_hist_cart_set_qty(product_id: str, quantity: int) -> dict:
        """Propose (do not execute) setting the quantity of a product ALREADY in Ben's הסופר החברתי cart to exactly `quantity` (1-20; to remove use super_hist_cart_remove). Same approval rules as super_hist_cart_add: show `text`, append [[CC_APPROVAL:<approval_id>]]."""
        return client.request("POST", "/proposals/super-hist-cart/set-qty",
                              {"product_id": product_id, "quantity": quantity}, timeout=120)

    @mcp.tool()
    def super_hist_cart_remove(product_id: str) -> dict:
        """Propose (do not execute) removing one product line from Ben's הסופר החברתי cart — only when Ben explicitly asked to remove that item. Show `text`, append [[CC_APPROVAL:<approval_id>]]. Never remove items on your own initiative and never empty the cart."""
        return client.request("POST", "/proposals/super-hist-cart/remove", {"product_id": product_id}, timeout=120)
