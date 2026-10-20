# super-hist fixtures

- `login_forms.html`: the real login page forms, trimmed (no personal data).
- `cart_full.html`, `cart_info_*.html`, `search_*.json`, `cart_*_ok.json`: the STRUCTURE (markup, JSON keys and
  types) is copied from the real site in the approved live run on 2026-10-20, 23:22–23:27 IDT. Every VALUE is
  SYNTHETIC: fake product ids, cart ids, names, prices and URLs. There are no cookies, ID numbers, names, phones,
  addresses or analytics payloads.
- Not observed live, so still assumptions: a by-weight product (`weight: true`, fractional quantity),
  `cart_empty.html` (the site redirects a guest cart to /login/, and Ben's cart was not emptied for it), and
  `cart_info_empty.html`'s empty values.
- `test_super_hist_cart.FixturePrivacy` fails if any fixture contains a 9-digit run that looks like an ID, an
  Israeli mobile number, an e-mail address, or a session/cookie string.
