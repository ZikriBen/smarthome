import { mkdir, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { CompanyTypes, createScraper } from "israeli-bank-scrapers";

const reportsDirectory = process.env.REPORTS_DIRECTORY ?? "/reports";

function previousMonth() {
  const now = new Date();
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1));
}

function parseReportMonth(value) {
  if (!value) return previousMonth();
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(value)) {
    throw new Error("REPORT_MONTH must use YYYY-MM format");
  }
  const [year, month] = value.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, 1));
}

function addMonth(value) {
  return new Date(Date.UTC(value.getUTCFullYear(), value.getUTCMonth() + 1, 1));
}

function csvCell(value) {
  const text = value == null ? "" : String(value);
  return `"${text.replaceAll('"', '""')}"`;
}

function redactAccount(accountNumber) {
  const value = String(accountNumber ?? "");
  return value.length <= 4 ? "****" : `****${value.slice(-4)}`;
}

async function atomicWrite(filePath, contents, mode = 0o600) {
  const temporaryPath = `${filePath}.tmp-${process.pid}`;
  await writeFile(temporaryPath, contents, { encoding: "utf8", mode });
  await rename(temporaryPath, filePath);
}

async function writeStatus(status) {
  await mkdir(reportsDirectory, { recursive: true, mode: 0o700 });
  await atomicWrite(path.join(reportsDirectory, "last-run.json"), `${JSON.stringify(status, null, 2)}\n`);
}

if (process.env.CHECK_ONLY === "true") {
  console.log(JSON.stringify({
    ready: true,
    company: CompanyTypes.hapoalim,
    chromium: process.env.CHROMIUM_PATH ?? "/usr/bin/chromium",
  }));
  process.exit(0);
}

const reportMonth = parseReportMonth(process.env.REPORT_MONTH);
const reportEnd = addMonth(reportMonth);
const monthName = reportMonth.toISOString().slice(0, 7);
const userCode = process.env.HAPOALIM_USER_CODE;
const password = process.env.HAPOALIM_PASSWORD;

if (!userCode || !password) {
  throw new Error("HAPOALIM_USER_CODE and HAPOALIM_PASSWORD must be supplied for this run");
}

await mkdir(reportsDirectory, { recursive: true, mode: 0o700 });

try {
  const scraper = createScraper({
    companyId: CompanyTypes.hapoalim,
    startDate: reportMonth,
    combineInstallments: false,
    showBrowser: false,
    executablePath: process.env.CHROMIUM_PATH ?? "/usr/bin/chromium",
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
    timeout: 90_000,
    defaultTimeout: 90_000,
    navigationRetryCount: 1,
  });

  const result = await scraper.scrape({ userCode, password });
  if (!result.success) {
    const error = new Error(result.errorMessage || "Bank Hapoalim scrape failed");
    error.name = result.errorType || "SCRAPE_FAILED";
    throw error;
  }

  const rows = [];
  const accounts = result.accounts ?? [];
  for (const account of accounts) {
    for (const transaction of account.txns ?? []) {
      const transactionDate = new Date(transaction.date);
      if (Number.isNaN(transactionDate.valueOf()) || transactionDate < reportMonth || transactionDate >= reportEnd) {
        continue;
      }
      rows.push([
        account.accountNumber,
        transaction.date,
        transaction.processedDate,
        transaction.status,
        transaction.description,
        transaction.memo,
        transaction.chargedAmount,
        transaction.originalCurrency,
        transaction.identifier,
      ]);
    }
  }

  rows.sort((left, right) => String(left[1]).localeCompare(String(right[1])));
  const header = [
    "account",
    "date",
    "processed_date",
    "status",
    "description",
    "memo",
    "amount",
    "currency",
    "identifier",
  ];
  const csv = [header, ...rows].map((row) => row.map(csvCell).join(",")).join("\n") + "\n";
  const reportFile = `hapoalim-${monthName}.csv`;
  await atomicWrite(path.join(reportsDirectory, reportFile), csv);

  const status = {
    status: "succeeded",
    completedAt: new Date().toISOString(),
    reportMonth: monthName,
    reportFile,
    transactionCount: rows.length,
    accounts: accounts.map((account) => redactAccount(account.accountNumber)),
  };
  await writeStatus(status);
  console.log(JSON.stringify(status));
} catch (error) {
  const status = {
    status: "failed",
    completedAt: new Date().toISOString(),
    reportMonth: monthName,
    errorType: error?.name || "Error",
    error: String(error?.message || error).slice(0, 500),
  };
  await writeStatus(status);
  console.error(JSON.stringify(status));
  process.exitCode = 1;
}
