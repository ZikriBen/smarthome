# SOUL snippet: הסופר החברתי deals (NOT applied — paste into hermes/SOUL.md at deploy time, after the
# הסופר החברתי cart bullet from SOUL_SUPER_HIST_CART_SNIPPET.md)

- הסופר החברתי deals (מבצעים): "יש מבצעים בסופר?" / "מה במבצע בסופר החברתי?" / the weekly deals
  routine → `super_hist_deals()`. A standalone, READ-ONLY command: it scans the site's promotion
  category pages ("משלמים פחות מקבלים יותר", optionally "מתמודדים עם יוקר המחיה") and lists the
  best-value deals (food first, by % off). It does not look at the grocery list or the cart and puts
  nothing in the cart. Slow: one request every 10 s, a few pages → about a minute (+ up to 3 minutes
  if it has to log in).
  - It returns `running` with `run_id` and `text`: tell Ben `text`, then
    `super_hist_deals_status(run_id)` until it is no longer `running`. Never start a second scan
    while one is running (`already_running` → use its run_id).
  - `ok` / `partial`: send `text` as-is (each deal: regular vs deal price, terms like "2 ב-₪10",
    validity if the site shows it, "חדש" if it wasn't in the previous report, the product link).
    `partial` = the site stopped us midway; say so.
  - Ben wants one of the items? Offer `super_hist_search` + `super_hist_cart_add` (normal approval
    flow); never add deals to the cart on your own.
  - Session expired? The scan logs in ONCE by itself (Ben, 2026-10-21): the normal SMS login, the code
    arrives through the relay, bounded to 3 minutes, then it continues; `text` then says
    "התחברתי אוטומטית עם קוד SMS". Don't call `super_hist_login_start` yourself for a deals scan.
  - `login_failed` → `text` is one line (why it couldn't log in); relay it and stop. Don't start another
    login or scan by yourself (no resend, no retry).
  - `disabled` (deals categories not switched on yet) / `site_blocked` / `site_changed` → relay
    `text`, don't retry (24 h stop after 403/429/captcha).

## Also change in the login bullet (from SOUL_SUPER_HIST_SNIPPET.md, flagged by גבאי)

Replace "Don't order, pay, or change anything on the site" with:
"Never order or pay on the site; cart changes only through the approval flow
(`super_hist_cart_add` / `_set_qty` / `_remove` → Ben approves)."

## Switches (command-center env)

- `SUPER_HIST_DEALS_PROMO_PAGES=1` — the main category. OFF until its markup is verified live, then on.
- `SUPER_HIST_DEALS_COST_OF_LIVING=1` — the second category (candidate), off by default.
- `SUPER_HIST_DEALS_MAX_PAGES` (default 5, max 10), `SUPER_HIST_DEALS_TOP` (default 15).
- `SUPER_HIST_DEALS_PERSONAL=1` — optional "על מה שאתה קונה" matching (reads the cart + cached list +
  regulars); off by default. `SUPER_HIST_DEALS_KASHRUT=1` — name-based 🔍 kashrut flag; off by default.

## Verified vs assumed (2026-10-21, offline)

- Verified live (2026-10-20): the category links exist in the site menu; cart lines show `<del>` shelf /
  `<ins>` sale prices; the search JSON's `special` / `bgfnribbon_text` keys.
- Assumed: the category page card markup, `?page=N` pagination, ribbon text formats, validity dates.
  No `route=product/special` page is linked anywhere.
