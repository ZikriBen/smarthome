// Cal action: card_transactions — walk billing months (newest first) for every card on the Cal login and emit
// (a) a CSV for out/ and (b) an archive_rows payload for the host archive (host/cal_archive.py).
// Date semantics (see README "Cal"): billing_month = the statement month the row was returned for (the month the
// walk asked for); purchase_date = the original purchase day (calPurchaseDate, NOT the library's installment-shifted
// `date`); charge_date = the debit day (debCrdDate / processedDate), null for pending rows.
import { resolveMonthRange } from "../months.mjs";

export const name = "card_transactions";

const int = (v, d, lo, hi) => (Number.isInteger(Number(v)) && +v >= lo && +v <= hi ? +v : d);

export function walkOptions(env = process.env) {
  return {
    cap: int(env.CAL_MONTH_CAP, 1000, 2, 100_000),
    delayMs: int(env.CAL_WALK_DELAY_MS, 300, 0, 10_000),
    // "page" (default: API calls from inside the logged-in page with one Node retry on no-response, port of
    // upstream PR #1193) | "node" (library 6.9.0 behaviour). Anything else -> "page".
    transport: env.CAL_API_TRANSPORT === "node" ? "node" : "page",
  };
}

export function validate(params = {}, now = new Date()) {
  const range = resolveMonthRange({ from_month: params.from_month, to_month: params.to_month,
    defaultMonths: int(params.defaultMonths, 3, 1, 19) }, now);
  if (!range.ok) return range;
  return { ok: true, params: { from_month: range.from, to_month: range.to, months: range.months,
    pending: params.pending !== false }, warnings: range.warnings };
}

export function dayIL(value) {
  if (value == null || value === "") return null;
  const d = new Date(value);
  if (Number.isNaN(d.valueOf())) return null;
  return d.toLocaleDateString("en-CA", { timeZone: "Asia/Jerusalem" });
}

const CSV_COLUMNS = ["card", "billing_month", "purchase_date", "charge_date", "description", "amount", "currency",
  "original_amount", "original_currency", "installment_number", "installment_total", "type", "status", "category", "memo"];

function cell(v) {
  return `"${String(v ?? "").replaceAll('"', '""')}"`;
}

export function toCardCsv(rows) {
  return [CSV_COLUMNS.join(","), ...rows.map((r) => CSV_COLUMNS.map((c) => cell(r[c])).join(","))].join("\n") + "\n";
}

export function toArchiveRow(last4, t) {
  const pending = String(t?.status || "") === "pending";
  return {
    card: last4,
    billing_month: pending ? null : (t?.calBillingMonth ?? null),
    purchase_date: dayIL(t?.calPurchaseDate ?? t?.date),
    charge_date: pending ? null : dayIL(t?.processedDate),
    description: t?.description ?? "",
    amount: t?.chargedAmount ?? t?.originalAmount ?? null,
    currency: t?.chargedCurrency ?? t?.originalCurrency ?? "ILS",
    original_amount: t?.originalAmount ?? null,
    original_currency: t?.originalCurrency ?? "",
    installment_number: t?.installments?.number ?? null,
    installment_total: t?.installments?.total ?? null,
    type: t?.type ?? "",
    status: t?.status ?? "completed",
    identifier: t?.identifier ?? "",
    category: t?.category ?? "",
    memo: t?.memo ?? "",
  };
}

export async function run(session, params) {
  const { scraper } = session;
  scraper.options.calWalk = { months: params.months, pending: params.pending, ...walkOptions() };
  scraper.options.outputData = { ...(scraper.options.outputData || {}), enableTransactionsFilterByDate: false };
  let result;
  try {
    result = await scraper.fetchData();
  } catch (error) {
    // Never echo the message: a JSON parse error can quote response bytes.
    const blocked = Number(session.api?.statuses?.[429] || 0) > 0 || Number(session.api?.statuses?.html || 0) > 0
      || /Automation detected/i.test(String(error?.message || ""));
    return { ok: false, status: blocked ? "CAL_BLOCKED" : "CARDS_FAILED", detail: String(error?.name || "Error") };
  }
  const accounts = Array.isArray(result?.accounts) ? result.accounts : [];
  if (!accounts.length) return { ok: false, status: "NO_CARDS", detail: "no cards in the Cal session" };
  session.progress.transactions = true;

  const rows = [];
  const retrieval = {};
  const perCard = [];
  const warnings = [];
  let blocked = false;
  for (const account of accounts) {
    const last4 = String(account?.accountNumber ?? "").replace(/\D/g, "").slice(-4);
    if (last4.length !== 4) { warnings.push("card without last-4 skipped"); continue; }
    const r = account?.retrieval;
    if (!r || typeof r !== "object") {
      retrieval[last4] = { known: false };
      warnings.push(`…${last4}: unpatched library (no retrieval metadata); nothing is marked complete`);
    } else {
      retrieval[last4] = { known: true, cap: r.cap, requested: r.requested, months: r.months || [], complete: !!r.complete,
        stopMonth: r.stopMonth ?? null, stopReason: r.stopReason ?? null, stopStatusCode: r.stopStatusCode ?? null,
        pending: r.pending, pendingRows: r.pendingRows ?? 0, transport: r.transport ?? "node", warnings: r.warnings || [] };
      if (r.stopReason === "blocked" || r.pending === "blocked") blocked = true;
      for (const w of r.warnings || []) warnings.push(`…${last4}: ${w}`);
      if (!r.complete) warnings.push(`…${last4}: walk stopped at ${r.stopMonth} (${r.stopReason}); older months not covered`);
    }
    const cardRows = (account?.txns || []).map((t) => toArchiveRow(last4, t));
    rows.push(...cardRows);
    perCard.push({ card: `…${last4}`, transactions: cardRows.filter((x) => x.status !== "pending").length,
      pending: cardRows.filter((x) => x.status === "pending").length,
      monthsReturned: (retrieval[last4].months || []).map((m) => m.month),
      complete: Boolean(retrieval[last4].complete), stopReason: retrieval[last4].stopReason ?? null });
  }
  const months = [...new Set(rows.map((x) => x.billing_month).filter(Boolean))].sort();
  const summary = { cardCount: perCard.length, cards: perCard,
    transactionCount: rows.filter((x) => x.status !== "pending").length,
    pendingCount: rows.filter((x) => x.status === "pending").length,
    from_month: params.from_month, to_month: params.to_month, monthsCovered: months };
  const payload = { from_month: params.from_month, to_month: params.to_month, months_requested: params.months,
    cards: Object.keys(retrieval), retrieval, rows };
  return {
    ok: true,
    status: blocked ? "CAL_BLOCKED_PARTIAL" : "DONE",
    warnings,
    summary,
    files: [
      { kind: "csv", nameHint: "card_transactions", body: toCardCsv(rows) },
      { kind: "archive_rows", nameHint: "archive", body: JSON.stringify(payload) },
    ],
  };
}

export default { name, validate, run };
