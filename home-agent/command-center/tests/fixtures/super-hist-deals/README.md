# super-hist-deals fixtures

- `promo-p1.html`, `cost_of_living-p1.html` — SYNTHETIC products/prices/ids in the REAL server-rendered card
  markup seen live on 2026-10-21 (`div[data-product_id].product-item`, `.price ins/del` twice, an image sticker,
  `h3.caption-title a`, `.caption-weight`, the embedded product JSON with `out_of_stock`). Extra cases: a card
  without `<del>` (no deal), a sticker with alt text (synthetic — live alts were empty), an out-of-stock card, a
  card in the cart (`data-cart-id` with a fake value), the same product in both categories, a JS template.
- `live-sample-promo-p1.html`, `live-sample-cost_of_living-p1.html` — LIVE SAMPLE 2026-10-21: the first 4 real
  cards of each page (public catalog: names, prices, links). Image URLs shortened, the cart-line id removed,
  the embedded JSON reduced to these cards' product_id / out_of_stock. No personal data.
- `cart.html` — the real cart line/totals markup (live 2026-10-20) with fake products (SYNTHETIC).
- `scenario.json` — the SYNTHETIC list/regulars/staples/categories used by `run_fixtures` and the tests.
No cookies, ID numbers, phones, addresses or names of real people.
