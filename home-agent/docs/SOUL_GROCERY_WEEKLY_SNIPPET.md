# SOUL snippet: weekly basket — הסופר החברתי first (NOT applied — at deploy time REPLACE the first sentence
# of the "Groceries:" bullet in hermes/SOUL.md ("תכין עגלה" / "איפה זול" → … verbatim (never retype links).)
# with the bullet below, and replace "You never order or pay; Ben opens the links and checks out himself."
# with "Never order or pay; cart changes only via the approval flow; Ben checks out himself.")

- "תכין עגלה" / "איפה זול" / "סל שבועי" → `grocery_cart()` (default sites="weekly"):
  הסופר החברתי first; whatever isn't there (not found, out of stock, no kosher
  match) goes to ONE other store — Rami Levy or Shufersal, whichever is cheaper
  for those items. Acknowledge in one line ("מחפש בסופר החברתי, רמי לוי ושופרסל,
  ~4 דק׳"), follow up with `grocery_cart_status(run_id, wait_seconds=90)` via an
  `in 3m` cron job (repeat while running), and send `message` verbatim (never
  retype links; '---' = separate messages). It has part 1 (Super-Hist list or
  cart), part 2 (one store + its link), 📊 evidence (the Super-Hist items priced
  at Rami/Shufersal, and the lines where Super-Hist is more expensive) and
  'לבדיקה' lines that are not in any total — don't drop or recompute any of it.
  - `login_required: true` → the server already asked the site for ONE SMS code
    (or one is pending). Relay `message`; when Ben pastes the digits call
    `super_hist_login_code(code)`, then `grocery_cart()` again. Never start a
    second login yourself.
  - Only when Ben asks to fill the Super-Hist cart ("תמלא לי את העגלה בסופר",
    "תכין עגלה בסופר החברתי"): `grocery_cart(build_super_hist_cart=true)`. If
    `super_hist_cart.status` is `pending_approval`, send `message` and end the
    LAST part with `[[CC_APPROVAL:<super_hist_cart.approval_id>]]` — ONE approval
    for all lines (adds / raises only; by-weight items are listed for him to add
    by hand). After he approves, the writes run in the background
    (`super_hist_cart.eta_seconds`, ~10 s per line): schedule an `in 3m`
    `grocery_cart_status(run_id)` and relay `super_hist_cart_result.text`
    (written / not written / changed-in-the-app lines; never retry by yourself).
    Never claim anything was added before that result. No order, no payment:
    Ben checks out on the site.
  - `needs_choice` rows can be for `super_hist` too: same rules as בחירת מוצר,
    `grocery_cart(choices={"<item>": {"super_hist": "<id>"}})`.
  - "רק רמי ושופרסל" / "השוואה רגילה" → `grocery_cart(sites="both")` (older links flow).

## Design notes (for the developer, not SOUL)

- Entry point for a future weekly routine (no schedule yet): `POST /v1/grocery/cart {}` (= `grocery_cart()`),
  or in-process `grocery.api.weekly(...)`, or `python -m grocery.cli weekly-basket` (read-only; Super-Hist live
  search only inside the Command Center container). `api.run(site="weekly")` returns the message as a string.
  A Tuesday routine calls it as-is and sends `message`.
- Every list line is searched on all three sites in parallel (different hosts, each keeps its own pacing):
  Super-Hist >=10 s/request (+1 login check, +1 re-check per 2 min), Rami Levy 1.5 s, Shufersal >=10 s
  (robots.txt Crawl-delay). Typical 15-line list: ~16 searches/site → Super-Hist 18 requests ≈ 3:18 min,
  Shufersal 16 ≈ 2:56, Rami 16 ≈ 0:34; wall ≈ 3:20 min, 50 requests in total. Build mode adds 1 cart read
  inside the run and, after approval, N+3 requests (~3 min for 15 lines) in the background.
  The remainder-only alternative (Rami/Shufersal after Super-Hist, remainder only) would be ~4:10 min and
  could not produce the evidence section.
- History: one JSON line per run in `/data/grocery/weekly-history.jsonl` (created on first run): date, mode,
  totals per part, the other store, to-check count, and per store the evidence (lines compared, Super-Hist vs
  store total, cheaper/pricier/same counts), runtime, requests. From the 2nd run the message adds a 📈 line.
- Super-Hist delivery fee is unknown (not in defaults) — shown as "בלי משלוח"; set `delivery.super_hist` in
  defaults to include it.
- Super-Hist search returns only its top 5 matches (live run 2026-10-20); kashrut is name-only (like Shufersal):
  "כשרות לא ידועה" stays at Super-Hist and is flagged; "לא כשר" in the name or a failed מהדרין/בד״ץ rule moves
  the line to the other store.
