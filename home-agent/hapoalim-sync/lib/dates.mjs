// Date validation and clamping for Bank Hapoalim actions (Asia/Jerusalem calendar days).
//
// Cap (from israeli-bank-scrapers 6.9.0, lib/scrapers/hapoalim.js fetchAccountData):
//   defaultStartMoment = moment().subtract(1, 'years').add(1, 'day')
//   startMoment = max(defaultStartMoment, requestedStart)
// so the library never asks the bank for more than ~364 days back. endDate is always "today" in the
// library's own fetchData; our checking_transactions action passes a custom `to` and filters, and
// also clamps `from` the same way. Empirically (PoC 2026-10-22) ~45 days worked; the 1-year cap is
// the library's documented/enforced limit, not a bank ToS we verified.

const ISO = /^\d{4}-\d{2}-\d{2}$/;

/** Today's calendar day in Asia/Jerusalem as YYYY-MM-DD. */
export function todayIL(now = new Date()) {
  return now.toLocaleDateString("en-CA", { timeZone: "Asia/Jerusalem" });
}

/** Parse YYYY-MM-DD; null when invalid or not a real calendar day. */
export function parseISODate(value) {
  if (typeof value !== "string" || !ISO.test(value)) return null;
  const [y, m, d] = value.split("-").map(Number);
  const utc = new Date(Date.UTC(y, m - 1, d));
  if (utc.getUTCFullYear() !== y || utc.getUTCMonth() !== m - 1 || utc.getUTCDate() !== d) return null;
  return value;
}

/** Add (or subtract) calendar days to a YYYY-MM-DD (UTC arithmetic on the date parts). */
export function addDays(iso, days) {
  const [y, m, d] = iso.split("-").map(Number);
  const utc = new Date(Date.UTC(y, m - 1, d + days));
  return utc.toISOString().slice(0, 10);
}

/** Earliest `from` the library will honour: todayIL - 1 year + 1 day. */
export function earliestFrom(now = new Date()) {
  const t = todayIL(now);
  const [y, m, d] = t.split("-").map(Number);
  return new Date(Date.UTC(y - 1, m - 1, d + 1)).toISOString().slice(0, 10);
}

/**
 * Validate and clamp a from/to pair.
 * Returns { ok:true, from, to, warnings[] } or { ok:false, error }.
 * Defaults: from = today-45d, to = today. Never future. from <= to. from clamped up to earliestFrom.
 */
export function resolveDateRange({ from, to, defaultDays = 45 } = {}, now = new Date()) {
  const today = todayIL(now);
  const warnings = [];
  let fromIso = from == null || from === "" ? addDays(today, -defaultDays) : parseISODate(from);
  let toIso = to == null || to === "" ? today : parseISODate(to);
  if (!fromIso) return { ok: false, error: "from must be YYYY-MM-DD" };
  if (!toIso) return { ok: false, error: "to must be YYYY-MM-DD" };
  if (fromIso > today) return { ok: false, error: "from is in the future" };
  if (toIso > today) {
    warnings.push(`to clamped from ${toIso} to ${today} (no future dates)`);
    toIso = today;
  }
  if (fromIso > toIso) return { ok: false, error: "from must be <= to" };
  const earliest = earliestFrom(now);
  if (fromIso < earliest) {
    warnings.push(`from clamped from ${fromIso} to ${earliest} (bank/library max lookback ≈ 1 year)`);
    fromIso = earliest;
  }
  if (fromIso > toIso) return { ok: false, error: "after clamping, from > to (range entirely outside the allowed window)" };
  return { ok: true, from: fromIso, to: toIso, warnings };
}

/** Convert YYYY-MM-DD to a Date at UTC midnight (what israeli-bank-scrapers' startDate option expects). */
export function toUtcDate(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}
