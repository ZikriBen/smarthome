# SOUL snippet: הסופר החברתי deals (NOT applied — paste into hermes/SOUL.md at deploy time, after the
# הסופר החברתי cart bullet from SOUL_SUPER_HIST_CART_SNIPPET.md)

- הסופר החברתי deals (מבצעים): "יש מבצעים בסופר?" / "מה במבצע בסופר החברתי?" /
  the weekly deals routine → `super_hist_deals()`. It is READ-ONLY (nothing goes into the cart)
  and slow (the site allows one request every 10 s; a scan is ~15 requests, about 3 minutes).
  - It returns `running` with `run_id` and `text`: tell Ben `text` ("בודק מבצעים… ~N דק׳"), then
    `super_hist_deals_status(run_id)` until it is no longer `running`. Never start a second scan
    while one is running (`already_running` → use its run_id).
  - `ok` / `partial`: send `text` as-is. It lists first the deals on what Ben buys (his grocery list,
    his הסופר החברתי cart, items he buys regularly, his staples), then a few other big food deals,
    each with regular vs deal price, terms (e.g. "2 ב-₪10"), validity if the site shows it, and the
    product link. 🔍 = the product name doesn't show a mehadrin/בד״ץ hechsher (the site has no
    kashrut data) — Ben checks the product page. `partial` = the site stopped us midway; say so.
  - Want an item from the list? Offer `super_hist_cart_add` for it (normal approval flow); never add
    deals to the cart on your own.
  - `login_required` → "החיבור לסופר החברתי פג. לשלוח קוד SMS להתחברות?" and only after he says
    yes call `super_hist_login_start`, then run `super_hist_deals` again. Never log in by yourself
    (in a scheduled run: just send the message, don't start a login).
  - `site_blocked` / `site_changed` → relay `text`, don't retry (24 h stop after 403/429/captcha).

## Also change in the login bullet (from SOUL_SUPER_HIST_SNIPPET.md, flagged by גבאי)

Replace "Don't order, pay, or change anything on the site" with:
"Never order or pay on the site; cart changes only through the approval flow
(`super_hist_cart_add` / `_set_qty` / `_remove` → Ben approves)."

## What is verified vs assumed (2026-10-21, offline review)

- Verified live (2026-10-20 run): the search JSON keys `price` (shelf), `special` (sale, `false` when
  none), `bgfnribbon_text` (`false` when none), `out_of_stock`, `in_cart`, `href`; only the top 5 hits are
  returned per search; cart lines show `<del>` shelf / `<ins>` sale unit prices.
- Assumed: the ribbon TEXT formats ("2 ב-10", "1+1", "השני ב-50%", "₪X עד dd.mm"); that some products
  carry a ribbon at all; validity dates (not in the search JSON — shown only when the ribbon text has
  one); the promotions category page `/משלמים-פחות-מקבלים-יותר/` markup (scan source OFF by default:
  `SUPER_HIST_DEALS_PROMO_PAGES=1` turns it on after a live check).
- No `route=product/special` / specials page is linked anywhere on the site's pages we have.
