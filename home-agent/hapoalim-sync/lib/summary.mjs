// Redacted summary: counts and date ranges only. No balances, amounts, descriptions or full account numbers.
import { readFileSync } from "node:fs";

export function maskAccount(accountNumber) {
  const digits = String(accountNumber ?? "").replace(/[^0-9]/g, "");
  return digits.length <= 2 ? "**" : `${"*".repeat(Math.min(digits.length - 2, 8))}${digits.slice(-2)}`;
}

function day(value) {
  const d = new Date(value);
  if (Number.isNaN(d.valueOf())) return null;
  // the bank's dates are Israeli calendar days (the library stores them as Israel midnight in UTC)
  return d.toLocaleDateString("en-CA", { timeZone: "Asia/Jerusalem" });
}

export function summarize(accounts = []) {
  const perAccount = [];
  const days = [];
  for (const account of accounts) {
    const txns = account?.txns ?? [];
    perAccount.push({ account: maskAccount(account?.accountNumber), transactions: txns.length });
    for (const t of txns) {
      const d = day(t?.date);
      if (d) days.push(d);
    }
  }
  days.sort();
  const months = [...new Set(days.map((d) => d.slice(0, 7)))];
  return {
    accountCount: accounts.length,
    accounts: perAccount,
    transactionCount: perAccount.reduce((n, a) => n + a.transactions, 0),
    firstDate: days[0] ?? null,
    lastDate: days.at(-1) ?? null,
    monthsCovered: months,
  };
}

function cell(value) {
  return `"${String(value ?? "").replaceAll('"', '""')}"`;
}

export function toCsv(accounts = []) {
  const header = ["account", "date", "processed_date", "status", "description", "memo", "amount", "currency"];
  const rows = [];
  for (const account of accounts) {
    for (const t of account?.txns ?? []) {
      rows.push([maskAccount(account.accountNumber), day(t.date), day(t.processedDate), t.status, t.description,
        t.memo, t.chargedAmount, t.originalCurrency]);
    }
  }
  rows.sort((a, b) => String(a[1]).localeCompare(String(b[1])));
  return `${[header, ...rows].map((r) => r.map(cell).join(",")).join("\n")}\n`;
}

/**
 * Full transactions CSV for the automation (Ben decided on 2026-10-22: Hermes may read the full details).
 * Columns: date, description, amount, account_balance_now, reference, account, processed_date, memo, currency,
 * status, type. The scraper (6.9.0) returns no running balance per transaction, only the account's current
 * balance at sync time: it is repeated per row in `account_balance_now` (empty when the bank gave none).
 * `maskAccounts` (default true) keeps only the last 2 digits of the account number.
 */
export function toTransactionsCsv(accounts = [], { maskAccounts = true } = {}) {
  const header = ["date", "description", "amount", "account_balance_now", "reference", "account", "processed_date",
    "memo", "currency", "status", "type"];
  const rows = [];
  for (const account of accounts) {
    const acct = maskAccounts ? maskAccount(account?.accountNumber) : String(account?.accountNumber ?? "");
    const balance = Number.isFinite(account?.balance) ? account.balance : "";
    for (const t of account?.txns ?? []) {
      rows.push([day(t.date), t.description, t.chargedAmount ?? t.originalAmount, balance, t.identifier ?? "", acct,
        day(t.processedDate), t.memo, t.originalCurrency, t.status, t.type]);
    }
  }
  rows.sort((a, b) => String(a[0]).localeCompare(String(b[0])));
  return `${[header, ...rows].map((r) => r.map(cell).join(",")).join("\n")}\n`;
}

/** fstype of the mount that holds `dir` (longest mount-point prefix in /proc/self/mountinfo). */
export function mountType(dir, mountinfo = "/proc/self/mountinfo") {
  let best = { point: "", type: null };
  for (const line of readFileSync(mountinfo, "utf8").split("\n")) {
    const parts = line.split(" ");
    const sep = parts.indexOf("-");
    if (sep < 0) continue;
    const point = parts[4].replace(/\\040/g, " ");
    const inside = dir === point || dir.startsWith(point.endsWith("/") ? point : `${point}/`);
    if (inside && point.length >= best.point.length) best = { point, type: parts[sep + 1] };
  }
  return best.type;
}
