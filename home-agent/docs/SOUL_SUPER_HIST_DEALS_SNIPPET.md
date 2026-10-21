# SOUL snippet: הסופר החברתי deals (NOT applied — paste into hermes/SOUL.md at deploy time, after the
# הסופר החברתי cart bullet from SOUL_SUPER_HIST_CART_SNIPPET.md)

- הסופר החברתי deals (מבצעים): "יש מבצעים בסופר?" / "מה במבצע בסופר החברתי?" / the weekly deals
  routine → `super_hist_deals()`. A standalone, READ-ONLY command: it scans the site's promotion
  category pages ("משלמים פחות מקבלים יותר" and "מתמודדים עם יוקר המחיה", page 1 of each) and lists
  the best-value deals: sale price vs regular price, by % off, food first. It does not look at the
  grocery list or the cart and puts nothing in the cart. About a minute (3 requests, 10 s apart;
  + up to 3 minutes if it has to log in).
  - It returns `running` with `run_id` and `text`: tell Ben `text`, then
    `super_hist_deals_status(run_id)` until it is no longer `running`. Never start a second scan
    while one is running (`already_running` → use its run_id).
  - `ok` / `partial`: send `text` as-is (each deal: deal price vs regular price and % off, the unit
    price, "חדש" if it wasn't in the previous report, the product link). Terms like 1+1 and validity
    dates are often only in the sticker picture on the site — don't invent them; the text shows a
    sticker's words only when the site provides them. `partial` = the site stopped us midway; say so.
  - Ben wants one of the items? Offer `super_hist_search` + `super_hist_cart_add` (normal approval
    flow); never add deals to the cart on your own.
  - Session expired? The scan logs in ONCE by itself (Ben, 2026-10-21): the normal SMS login, the code
    arrives through the relay, bounded to 3 minutes, then it continues; `text` then says
    "התחברתי אוטומטית עם קוד SMS". Don't call `super_hist_login_start` yourself for a deals scan.
  - `login_failed` → `text` is one line (why it couldn't log in); relay it and stop. Don't start another
    login or scan by yourself (no resend, no retry).
  - `disabled` (both categories switched off) / `site_blocked` / `site_changed` → relay
    `text`, don't retry (24 h stop after 403/429/captcha).

## Also change in the login bullet (from SOUL_SUPER_HIST_SNIPPET.md, flagged by גבאי)

Replace "Don't order, pay, or change anything on the site" with:
"Never order or pay on the site; cart changes only through the approval flow
(`super_hist_cart_add` / `_set_qty` / `_remove` → Ben approves)."

## Switches (command-center env)

- Both categories are ON by default; `SUPER_HIST_DEALS_PROMO_PAGES=0` / `SUPER_HIST_DEALS_COST_OF_LIVING=0`
  turns one off. Page 1 only (no pagination on the live pages); a warning appears if the site adds it.
- `SUPER_HIST_DEALS_LIMIT_100=1` — UNVERIFIED: ask for `?limit=100` (OpenCart page size) so a longer
  category still fits one GET. Off by default; never tested live.
- `SUPER_HIST_DEALS_TOP` (default 15).
- `SUPER_HIST_DEALS_PERSONAL=1` — optional "על מה שאתה קונה" matching (reads the cart + cached list +
  regulars); off by default. `SUPER_HIST_DEALS_KASHRUT=1` — name-based 🔍 kashrut flag; off by default.

## Verified vs assumed (live check 2026-10-21 07:31)

- Verified live: both category pages are server-rendered with the same card markup
  (`div[data-product_id].product-item`, `.price ins/del`, `h3.caption-title a`, `.caption-weight`, an image
  sticker with an empty alt) — 31 and 25 cards; no `?page=` links; the page also embeds the products as JSON
  (`out_of_stock` is read from there). Automatic login via the API worked (48 s, one SMS).
- Not verified: whether a category ever paginates, `?limit=100`, sticker alt texts other than empty.
