# super-hist-deals fixtures — ALL SYNTHETIC

- `search/<term>.json`: KEYS/TYPES as the real `/search-results/` JSON (live run 2026-10-20). Every value
  (ids, names, prices, ribbon texts) is fake. The ribbon TEXT formats ("2 ב-9", "1+1", "השני ב-50%",
  "₪36.90 עד 31.10") are assumptions: no real ribbon text has been recorded yet.
- `cart.html`: the real cart line/totals markup with fake products.
- `promo.html`: ASSUMED markup for the promotions category page (/משלמים-פחות-מקבלים-יותר/), never seen live.
- `scenario.json`: the list/regulars/staples/kashrut used by `run_fixtures` and the tests.
No cookies, ID numbers, phones, addresses or names of real people.
