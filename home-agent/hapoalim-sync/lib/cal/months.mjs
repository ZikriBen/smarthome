// Billing-month arithmetic for Cal. A Cal "month" is the STATEMENT (billing) month the API is asked for
// ({month, year} in getCardTransactionsDetails); it holds purchases made earlier (e.g. the previous month, and
// every installment of an older purchase). All months are Israeli calendar months, "YYYY-MM".
export const YM_RE = /^\d{4}-(0[1-9]|1[0-2])$/;

export function monthIL(now = new Date()) {
  return now.toLocaleDateString("en-CA", { timeZone: "Asia/Jerusalem" }).slice(0, 7);
}

export function addMonths(ym, n) {
  const y = Number(ym.slice(0, 4));
  const m = Number(ym.slice(5, 7)) - 1 + n;
  const yy = y + Math.floor(m / 12);
  const mm = ((m % 12) + 12) % 12 + 1;
  return `${yy}-${String(mm).padStart(2, "0")}`;
}

/** Newest first, inclusive. */
export function monthsDesc(from, to) {
  const out = [];
  for (let ym = to; ym >= from; ym = addMonths(ym, -1)) {
    out.push(ym);
    if (out.length > 120) break;
  }
  return out;
}

/**
 * Resolve a requested [from_month, to_month] into a clamped, newest-first list.
 * Limits mirror the library defaults: 18 months back, 1 month ahead (the next statement already has rows).
 */
export function resolveMonthRange({ from_month, to_month, defaultMonths = 3, lookbackMonths = 18, futureMonths = 1 } = {},
  now = new Date()) {
  const warnings = [];
  const cur = monthIL(now);
  const max = addMonths(cur, futureMonths);
  const min = addMonths(cur, -lookbackMonths);
  for (const [k, v] of [["from_month", from_month], ["to_month", to_month]]) {
    if (v != null && v !== "" && !YM_RE.test(String(v))) return { ok: false, error: `${k} must be YYYY-MM` };
  }
  let to = to_month || max;
  let from = from_month || addMonths(cur, -(Math.max(1, defaultMonths) - 1));
  if (to > max) { warnings.push(`to_month clamped to ${max}`); to = max; }
  if (from < min) { warnings.push(`from_month clamped to ${min} (Cal keeps ~${lookbackMonths} months)`); from = min; }
  if (from > to) return { ok: false, error: "from_month is after to_month" };
  return { ok: true, from, to, months: monthsDesc(from, to), current: cur, warnings };
}
