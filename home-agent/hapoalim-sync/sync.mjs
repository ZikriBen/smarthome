// Bank Hapoalim monthly report — one-shot, locked-down, OTP via Hermes otp-relay.
// Credentials and the age identity come from a secret directory (files), never from env.
// The browser profile is encrypted at rest (age) and decrypted only into this container's tmpfs.
// At most one run a day; a lockout after authentication failure; no retries; no screenshots; no codes/secrets in logs.
import { existsSync } from "node:fs";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import puppeteer from "puppeteer";
import { CompanyTypes, createScraper } from "israeli-bank-scrapers";

import { makePreparePage, parseAllowlist } from "./lib/egress.mjs";
import { createOtpCodeRetriever, notifyOtpRequired } from "./lib/otp-client.mjs";
import { restoreProfile, saveProfile, wipeProfile } from "./lib/profile-vault.mjs";
import { makeRedactor } from "./lib/redact.mjs";
import { LOCKOUT_ERRORS, acquireRun, markFinished, markStarted } from "./lib/run-lock.mjs";

const reportsDirectory = process.env.REPORTS_DIRECTORY ?? "/reports";
const stateDirectory = process.env.STATE_DIRECTORY ?? "/state";
const secretDirectory = process.env.SECRET_DIRECTORY ?? "/run/secrets/hapoalim";
const profileDir = process.env.PROFILE_DIR ?? "/tmp/hapoalim-profile";
const encryptedProfile = path.join(stateDirectory, "profile.tar.age");
const ageIdentity = path.join(secretDirectory, "age.key");
const brokerSocket = process.env.OTP_BROKER_SOCKET ?? "/otp-broker/broker.sock";
const chromium = process.env.CHROMIUM_PATH ?? "/usr/bin/chromium";
const loginTimeout = Number(process.env.LOGIN_TIMEOUT_MS) || 180_000;
const otpWait = Number(process.env.OTP_WAIT_SECONDS) || 150;
// Optional egress proxy (host:port) on the internal docker network; the proxy allowlists the bank's domains.
const egressProxy = String(process.env.HAPOALIM_PROXY || "").trim();

// The library's debug output prints account numbers and URLs: refuse to run with it enabled.
if (process.env.DEBUG) {
  console.error(JSON.stringify({ status: "failed", errorType: "DEBUG_FORBIDDEN" }));
  process.exit(2);
}
if (egressProxy && !/^[a-z0-9.-]+:[0-9]{2,5}$/i.test(egressProxy)) {
  console.error(JSON.stringify({ status: "failed", errorType: "BAD_PROXY" }));
  process.exit(2);
}

function previousMonth() {
  const now = new Date();
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1));
}

function parseReportMonth(value) {
  if (!value) return previousMonth();
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(value)) throw new Error("REPORT_MONTH must use YYYY-MM format");
  const [year, month] = value.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, 1));
}

function addMonth(value) {
  return new Date(Date.UTC(value.getUTCFullYear(), value.getUTCMonth() + 1, 1));
}

function csvCell(value) {
  return `"${String(value ?? "").replaceAll('"', '""')}"`;
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

async function readSecret(name) {
  const file = path.join(secretDirectory, name);
  const text = (await readFile(file, "utf8")).trim();
  if (!text) throw new Error(`secret ${name} is empty`);
  return text;
}

if (process.env.CHECK_ONLY === "true") {
  console.log(JSON.stringify({
    ready: true,
    company: CompanyTypes.hapoalim,
    chromium,
    patched: true,
  }));
  process.exit(0);
}

const reportMonth = parseReportMonth(process.env.REPORT_MONTH);
const reportEnd = addMonth(reportMonth);
const monthName = reportMonth.toISOString().slice(0, 7);
const runId = String(process.env.RUN_ID || "").trim();
const approvedAt = Number(process.env.APPROVED_AT) || 0;

let userCode;
let password;
let releaseLock = () => {};
let redact = makeRedactor([]);
let otpAsked = false;
let otpUsed = false;
let profileRestored = false;
const blockedHosts = new Set();

await mkdir(reportsDirectory, { recursive: true, mode: 0o700 });
await mkdir(stateDirectory, { recursive: true, mode: 0o700 });

try {
  releaseLock = acquireRun(stateDirectory);
  await markStarted(stateDirectory);

  userCode = await readSecret("user_code");
  password = await readSecret("password");
  redact = makeRedactor([userCode, password]);

  if (!existsSync(ageIdentity)) throw new Error("age identity missing under SECRET_DIRECTORY");
  if (!runId || !/^[0-9a-f]{16}$/.test(runId)) throw new Error("RUN_ID (16 hex) is required for an OTP-capable run");
  if (!(approvedAt > 0)) throw new Error("APPROVED_AT (unix seconds) is required");

  const restore = await restoreProfile({ encryptedPath: encryptedProfile, identityPath: ageIdentity, profileDir });
  profileRestored = restore.restored;

  const allowed = parseAllowlist(process.env.EGRESS_EXTRA_HOSTS);
  const preparePage = makePreparePage({ allowed, blocked: blockedHosts });
  const launchArgs = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"];
  if (egressProxy) launchArgs.push(`--proxy-server=http://${egressProxy}`, "--proxy-bypass-list=<-loopback>");
  // "Login started" = now, just before the browser opens: a code that arrived earlier (e.g. Ben's own login
  // a minute ago) is never used. The broker also clamps this to the approval time.
  const otpSince = Math.max(approvedAt, Date.now() / 1000 - 5);
  const otpCodeRetriever = createOtpCodeRetriever({
    socketPath: brokerSocket,
    runId,
    since: otpSince,
    waitSeconds: otpWait,
    onAsked: () => {
      otpAsked = true;
      otpUsed = true;
    },
  });

  const browser = await puppeteer.launch({
    headless: true,
    executablePath: chromium,
    userDataDir: profileDir,
    args: launchArgs,
    timeout: 90_000,
  });

  let result;
  try {
    const scraper = createScraper({
      companyId: CompanyTypes.hapoalim,
      startDate: reportMonth,
      combineInstallments: false,
      browser,
      skipCloseBrowser: true,
      preparePage,
      loginTimeout,
      onOtpRequired: () => notifyOtpRequired(brokerSocket, runId),
      timeout: 90_000,
      defaultTimeout: 90_000,
      navigationRetryCount: 1,
      // never: storeFailureScreenShotPath (a screenshot carries balances and names), verbose
    });
    result = await scraper.scrape({ userCode, password, otpCodeRetriever });
  } finally {
    try {
      await browser.close();
    } catch {
      // ignore
    }
  }

  if (!result?.success) {
    const error = new Error(redact(result?.errorMessage || result?.errorType || "scrape failed"));
    error.name = result?.errorType || (result?.errorMessage?.startsWith("OTP_")
      ? String(result.errorMessage).split(":")[0]
      : "SCRAPE_FAILED");
    if (typeof result?.errorMessage === "string" && result.errorMessage.startsWith("OTP_")) {
      error.name = result.errorMessage.split(":")[0];
    }
    throw error;
  }

  // Save the profile only after a successful login (and scrape): a half-written trust after a failed OTP
  // is worse than keeping the previous one.
  try {
    await saveProfile({ encryptedPath: encryptedProfile, identityPath: ageIdentity, profileDir });
  } catch (error) {
    // a save failure must not hide a good report; the next run may need a fresh SMS
  }

  const rows = [];
  const accounts = result.accounts ?? [];
  for (const account of accounts) {
    for (const transaction of account.txns ?? []) {
      const transactionDate = new Date(transaction.date);
      if (Number.isNaN(transactionDate.valueOf()) || transactionDate < reportMonth || transactionDate >= reportEnd) continue;
      rows.push([
        account.accountNumber, transaction.date, transaction.processedDate, transaction.status,
        transaction.description, transaction.memo, transaction.chargedAmount, transaction.originalCurrency,
        transaction.identifier,
      ]);
    }
  }
  rows.sort((left, right) => String(left[1]).localeCompare(String(right[1])));
  const header = ["account", "date", "processed_date", "status", "description", "memo", "amount", "currency", "identifier"];
  const csv = [header, ...rows].map((row) => row.map(csvCell).join(",")).join("\n") + "\n";
  const reportFile = `hapoalim-${monthName}.csv`;
  await atomicWrite(path.join(reportsDirectory, reportFile), csv);

  await markFinished(stateDirectory, null);
  const status = {
    status: "succeeded",
    completedAt: new Date().toISOString(),
    reportMonth: monthName,
    reportFile,
    transactionCount: rows.length,
    accounts: accounts.map((account) => redactAccount(account.accountNumber)),
    otpRequested: otpAsked,
    otpUsed,
    profile: profileRestored ? "restored" : "new",
    blockedHosts: [...blockedHosts],
    runId,
  };
  await writeStatus(status);
  console.log(JSON.stringify(status));
} catch (error) {
  const errorType = error?.name || error?.otpErrorCode || "Error";
  try {
    await markFinished(stateDirectory, errorType);
  } catch {
    // ignore
  }
  // Persist a lockout for authentication failures even when markFinished already did.
  const status = {
    status: "failed",
    completedAt: new Date().toISOString(),
    reportMonth: monthName,
    errorType,
    error: redact(error?.message || error),
    otpRequested: otpAsked,
    otpUsed,
    profile: profileRestored ? "restored" : "new",
    blockedHosts: [...blockedHosts],
    runId: runId || undefined,
  };
  await writeStatus(status);
  console.error(JSON.stringify(status));
  process.exitCode = 1;
} finally {
  userCode = password = null;
  try {
    await wipeProfile(profileDir);
  } catch {
    // ignore
  }
  try {
    releaseLock();
  } catch {
    // ignore
  }
}
