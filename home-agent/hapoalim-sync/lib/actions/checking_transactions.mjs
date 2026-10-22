// Action: checking_transactions — pull עו״ש transactions for [from, to] and emit a CSV.
// Uses the authenticated scraper's fetchData() (HapoalimScraper) after setting options.startDate;
// the library clamps start to today-1y+1d and always fetches through today — we then filter rows by `to`.
// See lib/dates.mjs for the cap and README for how to add more actions.
import { resolveDateRange, toUtcDate, todayIL } from "../dates.mjs";
import { summarize, toTransactionsCsv } from "../summary.mjs";

export const name = "checking_transactions";

export function validate(params = {}, now = new Date()) {
  const range = resolveDateRange({
    from: params.from, to: params.to,
    defaultDays: Number.isFinite(params.defaultDays) ? params.defaultDays : 45,
  }, now);
  if (!range.ok) return range;
  return {
    ok: true,
    params: {
      from: range.from,
      to: range.to,
      maskAccounts: params.maskAccounts !== false && params.maskAccounts !== "0",
    },
    warnings: range.warnings,
  };
}

function dayIL(value) {
  const d = new Date(value);
  if (Number.isNaN(d.valueOf())) return null;
  return d.toLocaleDateString("en-CA", { timeZone: "Asia/Jerusalem" });
}

export async function run(session, params) {
  const { scraper } = session;
  // Point the library's fetch at our (already clamped) from; end is always "today" inside fetchData.
  scraper.options.startDate = toUtcDate(params.from);
  let result;
  try {
    result = await scraper.fetchData();
  } catch (error) {
    return { ok: false, status: "TRANSACTIONS_FAILED",
      detail: session.redact?.(error?.message || error) || "fetch failed" };
  }
  if (!result?.success) {
    return { ok: false, status: "TRANSACTIONS_FAILED",
      detail: session.redact?.(result?.errorMessage || result?.errorType || "fetch failed") || "fetch failed" };
  }
  session.progress.accounts = true;
  session.progress.transactions = true;

  // Filter to the requested window (library endDate is always today).
  const accounts = (result.accounts || []).map((account) => ({
    ...account,
    txns: (account.txns || []).filter((t) => {
      const d = dayIL(t?.date);
      return d && d >= params.from && d <= params.to;
    }),
  }));
  const summary = summarize(accounts);
  const csv = toTransactionsCsv(accounts, { maskAccounts: params.maskAccounts });
  const warnings = [];
  if (params.to < todayIL() && summary.lastDate && summary.lastDate > params.to) {
    // should not happen after filter; keep as a belt-and-braces note
    warnings.push("filtered result still had rows after `to`");
  }
  return {
    ok: true,
    status: "DONE",
    warnings,
    summary: { ...summary, from: params.from, to: params.to },
    files: [{ kind: "csv", nameHint: "checking_transactions", body: csv }],
  };
}

export default { name, validate, run };
