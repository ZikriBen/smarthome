"""MCP tools for the household grocery list and cart links. Never logs in to, writes to, or pays at a supermarket."""

from typing import Literal


def register(mcp, client):
    @mcp.tool()
    def grocery_add_item(text: str) -> str:
        """Add items to the shopping list (Google Tasks 'קניות') from free Hebrew text (comma/newline separated, optional quantities like '2 מלפפון'). Pass the user's words as-is; never ask brand/size questions; defaults are applied server-side. Re-adding keeps the larger quantity; to lower a quantity call grocery_remove and add again."""
        return client.request("POST", "/grocery/add", {"text": text})["text"]

    @mcp.tool()
    def grocery_list() -> str:
        """Show the open shopping list (always re-read from Google Tasks first; items ticked on the phone are gone)."""
        return client.request("POST", "/grocery/list", {})["text"]

    @mcp.tool()
    def grocery_remove(item: str) -> str:
        """Remove one item by name, alias, unique partial name, or 1-based index."""
        return client.request("POST", "/grocery/remove", {"item": item})["text"]

    @mcp.tool()
    def grocery_clear(undo: bool = False) -> str:
        """Empty the list by ticking every item in Google Tasks (e.g. after Ben says he ordered). Confirm with the user first. undo=true restores the last clear."""
        return client.request("POST", "/grocery/clear", {"undo": undo})["text"]

    @mcp.tool()
    def grocery_learn_preference(item: str, change: dict | str, reason: str = "",
                                 site: Literal["", "rami_levy", "shufersal"] = "") -> str:
        """Save a preference. item = product, or "כללי" for ALL items. change = Ben's short Hebrew phrase ("פרווה", "רק X", "בלי X", "מותג X", "מהדרין", "דמי משלוח רמי 29.90") or a dict with keys: require, exclude, brand, prefer, pack_preference, qty, size_min, size_max, pin, kashrut (mehadrin|badatz|none), note, delivery (require_any/exclude_words/brands also accepted). Global: item="כללי", change={"kashrut":"mehadrin"}. Unparsed text is saved as a note (not enforced); reply with the result text."""
        return client.request("POST", "/grocery/learn", {
            "item": item, "change": change, "reason": reason, "site": site or None})["text"]

    @mcp.tool()
    def grocery_preferences(action: Literal["list", "full", "update", "delete"] = "list",
                            index: int = 0, change: dict | str = "", undo: bool = False) -> str:
        """List/edit/delete preferences. "מה ההעדפות" → list; "העדפות מלאות"/"תאחד הכל" → full (+built-ins). "תמחק העדפה N" → delete index=N (undo=true restores); "תשנה העדפה N ל…" → update index=N change=... Add new ones with grocery_learn_preference."""
        payload = {"action": action, "undo": undo, "full": action == "full"}
        if index:
            payload["index"] = index
        if change not in ("", None):
            payload["change"] = change
        return client.request("POST", "/grocery/preferences", payload)["text"]

    @mcp.tool()
    def grocery_cart(sites: Literal["both", "rami_levy", "shufersal"] = "both") -> dict:
        """"תכין עגלה" / "איפה זול": pull the list from Google Tasks, search Rami Levy and Shufersal (read-only, ~3 min) and build cart LINKS + a price comparison. Returns {run_id, status, eta_seconds}. Acknowledge in one short line, then schedule an `in 2m` follow-up that calls grocery_cart_status(run_id, wait_seconds=90), repeating `in 2m` while status is running. If run_id is null, send `message`. You never order or pay; Ben opens the links and checks out himself."""
        return client.request("POST", "/grocery/cart", {"sites": sites})

    @mcp.tool()
    def grocery_cart_status(run_id: str, wait_seconds: int = 0) -> dict:
        """Check a grocery_cart job. status running/done/failed. When done, send `message` VERBATIM (never retype, shorten or rebuild the URLs; `links` holds the same URLs). If `message` contains a line '---', send each part as its own message. On failure report `error`; do not retry automatically."""
        wait_seconds = max(0, min(int(wait_seconds), 90))
        return client.request("POST", "/grocery/cart-status",
                              {"run_id": run_id, "wait_seconds": wait_seconds},
                              timeout=wait_seconds + 30)
