// Bank Hapoalim automatic sync — one login, then one or more registered actions.
//
// Layer A (lib/session.mjs): credentials from stdin, OTP from the relayed-file wait, one login attempt,
//                             stop-for-today / lock owned by the host runner, profile wipe here.
// Layer B (lib/actions/*):   registry of post-login actions; first live action = checking_transactions.
//
// Started only by the root host runner (host/hapoalim-runner.py) as a hardened one-shot container.
// Protocol (lib/protocol.mjs):
//   runner -> container, line 1 : {userCode, password, actions?: [{name, ...params}]}
//   container -> runner          : {event:"otp_needed"}
//   runner -> container, line 2 : {otp} | {otp:null, error}
//   container -> runner          : {event:"result", report, files:[{kind,nameHint,body}]}
import { readdir, rm } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";

import { list as listActions, prepare, runAll } from "./lib/actions/index.mjs";
import { parseAllowlist } from "./lib/egress.mjs";
import { createLineReader, makeEmitter, parseJsonLine } from "./lib/protocol.mjs";
import { openSession, STOP_TODAY } from "./lib/session.mjs";

const CREDENTIALS_TIMEOUT_MS = 30_000;
const OFFLINE_ARGS = ["--proxy-server=http://127.0.0.1:9", "--host-resolver-rules=MAP * ~NOTFOUND"];

export function envOptions(env = process.env) {
  const int = (v, d, lo, hi) => (/^\d{1,6}$/.test(String(v ?? "")) && +v >= lo && +v <= hi ? +v : d);
  return {
    otpWaitMs: int(env.HAPOALIM_OTP_WAIT_MS, 150_000, 1_000, 300_000),
    loginTimeoutMs: int(env.HAPOALIM_LOGIN_TIMEOUT_MS, 180_000, 5_000, 300_000),
    extraHosts: env.HAPOALIM_EXTRA_HOSTS || "",
    maskAccounts: env.HAPOALIM_MASK_ACCOUNTS !== "0",
    defaultDays: int(env.HAPOALIM_DAYS, 45, 1, 365),
  };
}

/** Default when the runner sends no actions: one checking_transactions with env defaults. */
export function defaultActions(opts) {
  return [{ name: "checking_transactions", defaultDays: opts.defaultDays, maskAccounts: opts.maskAccounts }];
}

export function prepareActions(raw, opts) {
  const list = Array.isArray(raw) && raw.length ? raw : defaultActions(opts);
  const prepared = [];
  const warnings = [];
  for (const item of list) {
    // Propagate runner-level defaults into each action when the field is absent.
    const enriched = {
      maskAccounts: opts.maskAccounts,
      defaultDays: opts.defaultDays,
      ...(item && typeof item === "object" ? item : {}),
    };
    const v = prepare(enriched);
    if (!v.ok) return { ok: false, error: v.error };
    prepared.push(v);
    warnings.push(...v.warnings);
  }
  return { ok: true, prepared, warnings };
}

async function wipeDir(dir) {
  try {
    for (const entry of await readdir(dir)) await rm(path.join(dir, entry), { recursive: true, force: true });
  } catch { /* ignore */ }
}

/**
 * Full run: read credentials+actions from the line reader, open a session, run actions, emit result.
 * Returns the report object (also emitted). Used by tests with injected deps.
 */
export async function runAuto(deps, opts = envOptions()) {
  const t0 = Date.now();
  const finish = (fields) => ({
    status: "FAILED", stage: "preflight", stopForToday: false,
    otpRequested: false, otpDelivered: false,
    blockedHosts: [], failedHosts: [], actions: [],
    seconds: Math.round((Date.now() - t0) / 1000),
    fakeBank: Boolean(deps.responder),
    knownActions: listActions(),
    ...fields,
  });

  if ((deps.env || process.env).DEBUG) {
    return { report: finish({ status: "DEBUG_FORBIDDEN" }), files: [] };
  }

  const first = parseJsonLine(await deps.lines.next(CREDENTIALS_TIMEOUT_MS));
  if (!first) return { report: finish({ status: "NO_CREDENTIALS" }), files: [] };

  const plan = prepareActions(first.actions, opts);
  if (!plan.ok) {
    return { report: finish({ status: "BAD_ACTIONS", detail: plan.error }), files: [] };
  }

  await wipeDir(deps.profileDir);

  let otpReplyQueue = null;
  const waitForOtp = async () => {
    if (typeof deps.waitForOtp === "function") return deps.waitForOtp();
    const reply = parseJsonLine(await deps.lines.next(opts.otpWaitMs));
    return reply || { otp: null, error: "OTP_TIMEOUT" };
  };

  const opened = await openSession({
    puppeteer: deps.puppeteer,
    createScraper: deps.createScraper,
    CompanyTypes: deps.CompanyTypes,
    executablePath: deps.executablePath,
    profileDir: deps.profileDir,
    allowedHosts: deps.allowedHosts,
    extraLaunchArgs: deps.extraLaunchArgs,
    responder: deps.responder,
    requireTmpfs: deps.requireTmpfs,
    loginTimeoutMs: opts.loginTimeoutMs,
    emit: deps.emit,
    waitForOtp,
  }, { userCode: first.userCode, password: first.password });

  // Drop credentials from memory ASAP.
  first.userCode = first.password = undefined;

  if (!opened.ok) {
    await wipeDir(deps.profileDir);
    const report = finish({
      status: opened.status, stage: opened.stage,
      stopForToday: STOP_TODAY.has(opened.status),
      otpRequested: opened.otpAsked, otpDelivered: opened.otpDelivered,
      detail: opened.detail,
      blockedHosts: opened.blockedHosts, failedHosts: opened.failedHosts,
      warnings: plan.warnings,
    });
    if (["preflight", "launch"].includes(report.stage)) report.stopForToday = false;
    if (report.stage === "login-page" && report.status === "LOGIN_PAGE_FAILED") report.stopForToday = false;
    report.profileWiped = (await readdir(deps.profileDir).catch(() => [])).length === 0;
    return { report, files: [] };
  }

  let actionResults = [];
  let files = [];
  try {
    actionResults = await runAll(opened.session, plan.prepared);
    for (const r of actionResults) {
      for (const f of r.files || []) files.push({ kind: f.kind, nameHint: f.nameHint, body: f.body });
    }
  } finally {
    await opened.session.close();
    await wipeDir(deps.profileDir);
  }

  const allOk = actionResults.length > 0 && actionResults.every((r) => r.ok);
  const failed = actionResults.find((r) => !r.ok);
  // Aggregates from the first successful checking_transactions (for the runner's status line).
  const tx = actionResults.find((r) => r.name === "checking_transactions" && r.ok)?.summary || null;
  const report = finish({
    status: allOk ? "DONE" : (failed?.status || "ACTION_FAILED"),
    stage: allOk ? "done" : "actions",
    stopForToday: !allOk && STOP_TODAY.has(failed?.status),
    otpRequested: opened.otpAsked, otpDelivered: opened.otpDelivered,
    blockedHosts: opened.blockedHosts, failedHosts: [...opened.failedHosts, ...opened.session.failed],
    actions: actionResults.map(({ name, ok, status, warnings, summary, detail }) => (
      { name, ok, status, warnings, summary, detail })),
    warnings: plan.warnings,
    accountCount: tx?.accountCount, transactionCount: tx?.transactionCount,
    firstDate: tx?.firstDate, lastDate: tx?.lastDate, monthsCovered: tx?.monthsCovered,
    from: tx?.from, to: tx?.to, retrieval: tx?.retrieval,
    profileWiped: (await readdir(deps.profileDir).catch(() => [])).length === 0,
  });
  // Never put file bodies on the report (they travel in `files`).
  return { report, files };
}

function silenceConsole() {
  for (const k of ["log", "info", "warn", "error", "debug", "trace", "dir"]) console[k] = () => {};
}

async function main() {
  silenceConsole();
  const argv = process.argv.slice(2);
  let fake = null;
  for (const arg of argv) {
    const m = /^--fake-bank(?:=([a-z-]+))?$/.exec(arg);
    if (m) fake = m[1] || "ok";
    else {
      process.stdout.write(`${JSON.stringify({ event: "result", report: { status: "BAD_ARGS", stage: "preflight" }, files: [] })}\n`);
      process.exit(2);
    }
  }
  const emit = makeEmitter((s) => process.stdout.write(s));
  const lines = createLineReader(process.stdin);
  const [{ default: puppeteer }, { CompanyTypes, createScraper }] = await Promise.all([
    import("puppeteer"), import("israeli-bank-scrapers"),
  ]);
  let responder;
  let extraLaunchArgs = [];
  if (fake) {
    const { fakeBank } = await import("./lib/fake-bank.mjs");
    const scenarios = {
      ok: { otp: true }, "wrong-otp": { otp: true, expectedCode: "000000" }, "no-otp": { otp: false },
      "wrong-password": { wrongPasswordAny: true }, captcha: { captcha: true }, "otp-hangs": { otp: true, otpHangs: true },
    };
    responder = fakeBank(scenarios[fake] || scenarios.ok).responder;
    extraLaunchArgs = OFFLINE_ARGS;
  }
  const opts = envOptions();
  const { report, files } = await runAuto({
    lines, emit, puppeteer, createScraper, CompanyTypes,
    executablePath: process.env.CHROMIUM_PATH || "/usr/bin/chromium",
    profileDir: process.env.HAPOALIM_PROFILE_DIR || "/profile",
    allowedHosts: parseAllowlist(opts.extraHosts),
    requireTmpfs: process.env.HAPOALIM_REQUIRE_TMPFS !== "0",
    responder, extraLaunchArgs,
  }, opts);
  // Strip file bodies from the report path; send them alongside.
  emit({ event: "result", report, files: files.map((f) => ({ kind: f.kind, nameHint: f.nameHint, body: f.body })) });
  lines.close();
  process.exitCode = report.status === "DONE" ? 0 : (report.stage === "preflight" ? 2 : 1);
  setTimeout(() => process.exit(process.exitCode), 200).unref();
}

process.on("unhandledRejection", () => {
  process.stdout.write(`${JSON.stringify({ event: "result", report: { status: "INTERNAL", stage: "unknown" }, files: [] })}\n`);
  process.exit(1);
});

if (import.meta.url === pathToFileURL(process.argv[1] || "").href) await main();
