# super-hist fixtures

- `login_forms.html` — trimmed real login page forms (no personal data).
- `cart_*.html`, `cart_info_*.html`, `search_*.json`, `cart_*_ok.json` — **SYNTHETIC**. Written by hand on
  2026-10-20 to match the structure documented in the approved feasibility run (the raw pages from that run
  were deleted, not recorded). Products, prices, product ids and cart ids are fake; there are no cookies, ID
  numbers, names, phones or addresses. Re-recording real, anonymized fixtures needs an approved live run
  from the NUC (see docs/SOUL_SUPER_HIST_CART_SNIPPET.md, "Open items").
- `test_super_hist_cart.FixturePrivacy` fails if any fixture contains an ID-like 9-digit run, an Israeli
  mobile number, an e-mail address, or a session/cookie string.
