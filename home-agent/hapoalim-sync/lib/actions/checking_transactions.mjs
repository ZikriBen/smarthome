// Action: checking_transactions — pull עו״ש transactions for [from, to] and emit a CSV.
// Uses the authenticated scraper's fetchData() (HapoalimScraper) after setting options.startDate;
// the library clamps start to today-1y+1d and always fetches through today — we then filter rows by `to`.
// See lib/dates.mjs for the cap and README for how to add more actions.
import { resolveDateRange, toUtcDate, todayIL } from "../dates.mjs";
import { maskAccount, summarize, toTransactionsCsv } from "../summary.mjs";

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

// Backward date-window walk inside the patched scraper (see patches/README.md). Env overrides for tests/ops.
export function walkOptions(env = process.env) {
  const num = (name, lo, hi) => {
    const v = Number(env[name]);
    return Number.isFinite(v) && v >= lo && v <= hi ? v : undefined;
  };
  const o = { cap: num("HAPOALIM_PAGE_CAP", 2, 1000), maxWindows: num("HAPOALIM_WALK_MAX_WINDOWS", 1, 60),
    delayMs: num("HAPOALIM_WALK_DELAY_MS", 0, 10_000) };
  return Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined));
}

export async function run(session, params) {
  const { scraper } = session;
  // Point the library's fetch at our (already clamped) from; end is always "today" inside fetchData.
  scraper.options.startDate = toUtcDate(params.from);
  scraper.options.hapoalimWalk = walkOptions();
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
  // Retrieval metadata from the patched scraper (counts/dates only). Missing = unpatched library.
  const retrieval = {};
  const retrievalSummary = [];
  const warnings = [];
  for (const account of result.accounts || []) {
    const acct = String(account?.accountNumber ?? "");
    const r = account?.retrieval;
    if (!acct) continue;
    if (!r || typeof r !== "object") {
      retrieval[acct] = { known: false };
      continue;
    }
    const keep = { known: true, cap: r.cap, windows: r.windows, rows: r.rows, maxWindowRows: r.maxWindowRows,
      complete: !!r.complete, completeFrom: r.completeFrom ?? null, oldestDate: r.oldestDate ?? null,
      stopReason: r.stopReason ?? null, partialDays: r.partialDays || [], warnings: r.warnings || [] };
    retrieval[acct] = keep;
    retrievalSummary.push({ account: maskAccount(acct), windows: keep.windows, rows: keep.rows,
      complete: keep.complete, completeFrom: keep.completeFrom, stopReason: keep.stopReason });
    for (const w of keep.warnings) warnings.push(`${maskAccount(acct)}: ${w}`);
    if (!keep.complete) warnings.push(`${maskAccount(acct)}: retrieval incomplete; archive coverage starts ${keep.completeFrom ?? "nowhere"}`);
  }
  summary.retrieval = retrievalSummary;
  const csv = toTransactionsCsv(accounts, { maskAccounts: params.maskAccounts });
  // Unmasked rows for the host archive merge (kind=archive_rows; never written to out/).
  const archiveRows = [];
  for (const account of accounts) {
    for (const t of account?.txns ?? []) {
      archiveRows.push({
        account: String(account?.accountNumber ?? ""),
        date: dayIL(t?.date),
        processed_date: dayIL(t?.processedDate),
        amount: t?.chargedAmount ?? t?.originalAmount,
        currency: t?.originalCurrency ?? t?.chargedCurrency ?? "ILS",
        description: t?.description ?? "",
        memo: t?.memo ?? "",
        status: t?.status ?? "completed",
        type: t?.type ?? "",
        identifier: t?.identifier ?? "",
        balance: Number.isFinite(account?.balance) ? account.balance : null,
      });
    }
  }
  if (params.to < todayIL() && summary.lastDate && summary.lastDate > params.to) {
    warnings.push("filtered result still had rows after `to`");
  }
  return {
    ok: true,
    status: "DONE",
    warnings,
    summary: { ...summary, from: params.from, to: params.to },
    files: [
      { kind: "csv", nameHint: "checking_transactions", body: csv },
      { kind: "archive_rows", nameHint: "archive", body: JSON.stringify({
        from: params.from, to: params.to,
        accounts: accounts.map((a) => String(a?.accountNumber ?? "")).filter(Boolean),
        retrieval,
        rows: archiveRows,
      }) },
    ],
  };
}

export default { name, validate, run };
