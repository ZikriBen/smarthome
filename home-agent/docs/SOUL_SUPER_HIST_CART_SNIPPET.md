# SOUL snippet: הסופר החברתי cart (NOT applied — paste into hermes/SOUL.md at deploy time, after the
# הסופר החברתי login bullet from SOUL_SUPER_HIST_SNIPPET.md)

- הסופר החברתי cart: Ben's cart on super-hist.co.il is ACCOUNT-level — the same cart he
  sees on the site and in the app. You can read it and propose changes; you can never
  check out, pay, order, use coupons or empty the cart (the server blocks those URLs).
  Ben pays himself.
  - "מה יש לי בעגלה בסופר?" → `super_hist_cart_view` and summarize lines + total.
  - To add an item: `super_hist_search(term)` once, pick the product (price = the price
    actually paid; say when it's on sale), then `super_hist_cart_add(product_id, quantity)`.
    add means "at least N in the cart": if it's already there with fewer, it becomes a
    quantity update; if there's already enough you get `already_in_cart` (say so, nothing
    to approve). To change an existing line to an exact number use
    `super_hist_cart_set_qty`; to remove a line use `super_hist_cart_remove` — only when
    Ben explicitly asked to remove that item.
  - Every write returns `pending_approval`: show `text` (item, price, quantity before→after,
    expected new total) and append the exact final marker `[[CC_APPROVAL:<approval_id>]]`.
    The approval (button, or Ben replying "מאשר"/"כן"/"לא") performs the write; never claim
    an item was added/changed before approval, and never re-propose because he typed "מאשר".
    A failed approval that says "ייתכן שהעגלה השתנתה" → call `super_hist_cart_view` once and
    report what is really in the cart.
  - Searches/reads are slow (≥10 s per request on the site): one search per item, no loops,
    tell Ben when several are needed.
  - Logged out: the cart tools (view / search / add / set_qty / remove) log in BY
    THEMSELVES — one SMS, the code comes through the relay, up to 3 minutes — and
    then continue; don't ask Ben first and don't call `super_hist_login_start`
    for it. If the call is slow, that is the login. If Ben pastes 6 digits while
    it waits, call `super_hist_login_code(code)`. `login_failed` → relay its one
    line and stop (no retry, no second SMS; the next request tries once again).
    `login_required` (the session dropped in the middle of a call) → nothing was
    changed; relay `text` and stop.
  - `site_blocked` / `site_changed` / `blocked_by_policy` → relay `text` and stop; do not
    retry. `weighted_unsupported` (by-kg produce) → tell Ben to add it himself on the site.
    `search_first` → search, then propose again. `out_of_stock` → say so, offer an alternative.

## Verified live (approved run, 2026-10-20 23:22–23:27 IDT, from the NUC; 15 requests, 1 SMS)

- Cart page: lines `div.cart_product[data-product_id][data-cart-id]`. The price shown on a line is the UNIT
  price: the sum of unit price × qty equals the cart total. The markup uses self-closing `<img ... />` and
  repeats the price for mobile.
- `checkout/cart/edit` with `data[0][key]=<cart_id>&data[0][quantity]=N` works:
  `{success: true, cart_id, total, ...}`. Its `total` lagged the real cart, so the re-read decides.
- add returns `{success: <html>, cart_id, current_product_quantity:int, total, ...}`. remove returns
  `{total, countProducts (units), ...}`.
- Mini-cart: `<input type="hidden" class="hidden_total" value="N פריט\ים - ₪X">`. N counts UNITS, not lines.
- Search: `weight` is a boolean (false on all 10 packaged products seen); `bgfnribbon_text` and `special` are
  `false` when absent; `results` is a string; only the top 5 are returned.
- A guest (logged-out) cart page redirects 302 to /login/.

## Open items

- A by-weight product was not seen live (`weight: true`?), so weighted items stay refused for writes.
- The empty-cart page was not seen: a guest gets a redirect, and Ben's cart was not emptied for it. The
  fixture is synthetic; the mini-cart canary covers it.
- The 403/429/challenge breaker (24 h) is shared by every Super-Hist path and, since feat/grocery-weekly-deals,
  persisted in grocery_site_state["super_hist"] (survives a restart).
- Logout is local only; the site session stays until it expires.

## Automatic login (2026-10-21, Ben)

- cart_view / search / propose_* → `_logged_in()`: logged out → `_auto_login()` (shared with the weekly
  gate): ONE `start_login` (one SMS; waits ≤45 s for the relayed code), then waits for the session up to
  LOGIN_BUDGET = 180 s polling local state + the relay inbox (no site requests), then the tool runs once
  more. A pending code is waited for, never re-sent. Failure/timeout → `login_failed` (one line), nothing read
  or proposed. No retries.
- Not covered on purpose: an APPROVED write (single `apply_write`, or the background batch) never logs in —
  it runs inside the approval decision / a background thread, so a dropped session fails it with
  "nothing changed" and the next proposal logs in.
- Timeouts: MCP client 260 s for the cart tools; Hermes `mcp_servers.command_center.timeout` 180 → 270 s
  (hermes/config.yaml) — part of the same deploy.
