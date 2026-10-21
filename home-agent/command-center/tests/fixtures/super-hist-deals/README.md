# super-hist-deals fixtures — ALL SYNTHETIC

- `promo-p1.html`, `promo-p2.html`, `cost_of_living-p1.html`: ASSUMED markup for the category pages
  (/משלמים-פחות-מקבלים-יותר/, /יוקר-המחייה/) — never seen live. Card classes come from the theme CSS in the real
  home page; pagination is assumed OpenCart-style `?page=N` links. Every product, price and ribbon text is fake;
  the ribbon TEXT formats ("2 ב-9", "1+1", "השני ב-50%", "₪36.90 עד 31.10") are assumptions too.
- `cart.html`: the real cart line/totals markup (live 2026-10-20) with fake products.
- `scenario.json`: the list/regulars/staples/categories used by `run_fixtures` and the tests.
No cookies, ID numbers, phones, addresses or names of real people.
