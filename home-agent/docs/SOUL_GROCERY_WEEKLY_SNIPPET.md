# SOUL snippet: weekly basket — הסופר החברתי first (NOT applied — at deploy time REPLACE the first sentence
# of the "Groceries:" bullet in hermes/SOUL.md ("תכין עגלה" / "איפה זול" → … verbatim (never retype links).)
# with the bullet below, and replace "You never order or pay; Ben opens the links and checks out himself."
# with "Never order or pay; cart changes only via the approval flow; Ben checks out himself.")

- "תכין עגלה" / "איפה זול" / "סל שבועי" → `grocery_cart()` (default sites="weekly"):
  הסופר החברתי first; whatever isn't there (not found, out of stock) goes to
  ONE other store — Rami Levy or Shufersal, whichever is cheaper
  for those items. Acknowledge in one line ("מחפש בסופר החברתי, רמי לוי ושופרסל,
  ~4 דק׳"), follow up with `grocery_cart_status(run_id, wait_seconds=90)` via an
  `in 3m` cron job (repeat while running), and send `message` verbatim (never
  retype links; '---' = separate messages). It has part 1 (Super-Hist list or
  cart), part 2 (one store + its link), 📊 evidence (the Super-Hist items priced
  at Rami/Shufersal, and the lines where Super-Hist is more expensive) and
  'לבדיקה' lines that are not in any total — don't drop or recompute any of it.
  - Super-Hist logged out: the run logs in BY ITSELF (one SMS, the code comes
    through the #OTP relay; up to 3 min, so the run can take that much longer —
    keep following up while `running`). Don't call `super_hist_login_start`
    yourself for a basket and don't ask Ben first. If he pastes 6 digits while
    the run waits, call `super_hist_login_code(code)` — the run picks it up.
    `super_hist_login: "failed"` → the message already has ONE line saying the
    login failed and that the whole list went to Rami Levy / Shufersal; relay
    it as is, no retry (the next "תכין עגלה" tries once again).
  - Only when Ben asks to fill the Super-Hist cart ("תמלא לי את העגלה בסופר",
    "תכין עגלה בסופר החברתי"): `grocery_cart(build_super_hist_cart=true)`. If
    `super_hist_cart.status` is `pending_approval`, send `message` and end the
    LAST part with `[[CC_APPROVAL:<super_hist_cart.approval_id>]]` — ONE approval
    for all lines (adds / raises only; by-weight items are listed for him to add
    by hand). After he approves, the prompt shows "⏳ מאושר, מבצע ברקע…" and the
    writes run in the background (~10 s per line). The real result (written /
    not written / changed-in-the-app lines) is posted under the prompt by the
    Telegram adapter itself — don't schedule a follow-up for it and don't
    repeat it. If Ben asks before it arrives, `grocery_cart_status(run_id)` →
    `super_hist_cart_result.text`. Never retry by yourself, never claim anything
    was added before that result. No order, no payment: Ben checks out on the site.
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
- Login (Ben, 21.10): every weekly run (MCP tool, API, CLI in the container, a future routine — one rule, no
  origin flag) calls `super_hist.grocery_gate()` first. Logged in → go (1 read). Logged out → ONE automatic
  login per run: the existing `start_login` (one SMS, waits ≤45 s for the relayed code and submits it), then
  waits until the session is up, bounded by LOGIN_BUDGET = 180 s from the gate start, polling every 5 s the
  local login state (also set when Ben pastes the code → `super_hist_login_code`) and the relay inbox — no
  site requests while waiting. A code already pending from an earlier start is waited for, never re-sent.
  No retries, no second SMS. Failure (not configured / cooldown / rate limited / site changed / rejected /
  wrong codes) or timeout → `login_failed`: Super-Hist is not searched at all, and the run CONTINUES with
  Rami Levy + Shufersal for the whole list (part 2 = the cheaper one; the evidence section is empty) — Ben
  still gets a usable basket from the same request instead of nothing; it costs the normal ~32 Rami/Shufersal
  requests. The message says it in one line ("🔐 ההתחברות האוטומטית לסופר החברתי נכשלה (…) — לא חיפשתי בסופר;
  כל הרשימה … עברה לרמי לוי"). A session that expires MID-run is not re-logged (one attempt per run):
  Super-Hist stops at once, the rest goes to the other store, the next run logs in again. Success adds one
  line "התחברתי מחדש אוטומטית". The job ETA does not include the (≤3 min) login wait.
- Kashrut is OFF (Ben, 21.10): `grocery/kashrut.py` runs only with `GROCERY_KASHRUT=1` (env, off by default).
  Off = no hechsher filter/re-rank on ANY site (Super-Hist, Rami Levy, Shufersal), no "לא נמצא מהדרין" /
  "כשרות לא ידועה" flags or lines, "לא כשר" in a Super-Hist name doesn't move the line. Learned kashrut prefs
  are kept (listed as "כבוי כרגע") for when it's back on. What still filters: ordinary word rules
  (require/exclude/brand/pin) — e.g. a learned `require_any: ["מהדרין"]` word rule is a name filter, not
  the kashrut module, and still applies; the Super-Hist adapter still records hechsher words from names (data
  only). Turning it on restores the 3a9aa34 behaviour (tests cover both).
- Background cart result → Telegram: no Command Center → Hermes push endpoint exists, and none was added.
  `super_hist_cart_batch` is registered as a background approval action (`approvals.register_action(...,
  background=True)`): approve → status `running`; the worker calls `approvals.finish_background(id, ok, text)`
  → `executed`/`failed` + `result_text` (stored in approval_decisions.result; a restart marks a stale
  `running` as failed with a "restarted mid-run" text). Hermes' ApprovalController (cc_approvals.py) shows
  "⏳ מאושר, מבצע ברקע…", then follows the approval by polling the existing payload-free
  `GET /v1/approvals/<id>` with the existing COMMAND_CENTER_CALLBACK_TOKEN every 15 s (max 30 min, in a task
  tracked by the adapter; resumed after a Hermes restart), re-edits the prompt and posts `result_text` under
  it through the adapter's existing control-message path (`_TelegramApprovalUI.send_control`; never reaches
  the LLM). No new token, secret or endpoint.
- Super-Hist search returns only its top 5 matches (live run 2026-10-20).
