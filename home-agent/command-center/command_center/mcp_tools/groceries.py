"""MCP tools for the household grocery list, the weekly basket (Super-Hist first) and cart links. Never orders or pays; a Super-Hist cart change happens only through one Telegram approval."""

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
    def grocery_cart(sites: Literal["weekly", "both", "rami_levy", "shufersal"] = "weekly",
                     choices: dict | None = None, build_super_hist_cart: bool = False) -> dict:
        """"תכין עגלה"/"איפה זול": weekly = הסופר החברתי first, the rest at ONE cheaper store (Rami/Shufersal), read-only (~4 min). build_super_hist_cart=true only when Ben asks to fill the Super-Hist cart: ONE batched approval, never an order. Returns {run_id, eta_seconds}: ack in one line, schedule `in 3m` grocery_cart_status(run_id, wait_seconds=90). run_id null: send `message`. choices={"<item>": {"super_hist": "<id>"}} = your pick for needs_choice, this run only."""
        payload = {"sites": sites}
        if choices:
            payload["choices"] = choices
        if build_super_hist_cart:
            payload["build_super_hist_cart"] = True
        return client.request("POST", "/grocery/cart", payload)

    @mcp.tool()
    def grocery_cart_status(run_id: str, wait_seconds: int = 0) -> dict:
        """Check a grocery_cart job (running/done/failed). needs_choice non-empty: choose per SOUL and rerun with choices; send only the final message. Send `message` VERBATIM (never rebuild URLs; '---' = separate parts). super_hist_cart.status pending_approval: end the last part with [[CC_APPROVAL:<approval_id>]]; after approval, check again for super_hist_cart_result. login_required: relay; no retry by yourself."""
        wait_seconds = max(0, min(int(wait_seconds), 90))
        return client.request("POST", "/grocery/cart-status",
                              {"run_id": run_id, "wait_seconds": wait_seconds},
                              timeout=wait_seconds + 30)
