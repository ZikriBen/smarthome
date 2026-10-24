// Cal (visaCal) automatic sync — one login, then the registered Cal actions (card_transactions).
// Same image and line protocol as auto.mjs (Hapoalim); started only by host/cal-runner.py as a hardened one-shot
// container with `--entrypoint dumb-init IMAGE -- node /app/cal-auto.mjs`.
//   runner -> container, line 1 : {username, password, actions?: [{name:"card_transactions", from_month, to_month}]}
//   container -> runner          : {event:"otp_needed"}            (only if Cal shows a code form)
//   runner -> container, line 2 : {otp} | {otp:null, error}
//   container -> runner          : {event:"result", report, files:[{kind,nameHint,body}]}
import { readdir, rm } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";

import { list as listActions, prepare, runAll } from "./lib/cal/actions/index.mjs";
import { parseCalAllowlist } from "./lib/cal/egress.mjs";
import { CAL_STOP_TODAY, openCalSession } from "./lib/cal/session.mjs";
import { createLineReader, makeEmitter, parseJsonLine } from "./lib/protocol.mjs";

const CREDENTIALS_TIMEOUT_MS = 30_000;
const OFFLINE_ARGS = ["--proxy-server=http://127.0.0.1:9", "--host-resolver-rules=MAP * ~NOTFOUND"];

export function envOptions(env = process.env) {
  const int = (v, d, lo, hi) => (/^\d{1,6}$/.test(String(v ?? "")) && +v >= lo && +v <= hi ? +v : d);
  return {
    otpWaitMs: int(env.CAL_OTP_WAIT_MS, 150_000, 1_000, 300_000),
    navTimeoutMs: int(env.CAL_NAV_TIMEOUT_MS, 60_000, 2_000, 180_000),
    otpResultTimeoutMs: int(env.CAL_OTP_RESULT_TIMEOUT_MS, 45_000, 1_000, 120_000),
    otpEntry: env.CAL_OTP_ENTRY !== "0",
    extraHosts: env.CAL_EXTRA_HOSTS || "",
    defaultMonths: int(env.CAL_MONTHS, 3, 1, 19),
  };
}

export function prepareActions(raw, opts) {
  const list = Array.isArray(raw) && raw.length ? raw : [{ name: "card_transactions" }];
  const prepared = [];
  const warnings = [];
  for (const item of list) {
    const v = prepare({ defaultMonths: opts.defaultMonths, ...(item && typeof item === "object" ? item : {}) });
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

export async function runCalAuto(deps, opts = envOptions()) {
  const t0 = Date.now();
  const finish = (fields) => ({
    site: "cal", status: "FAILED", stage: "preflight", stopForToday: false,
    otpRequested: false, otpDelivered: false, blockedHosts: [], failedHosts: [], apiHosts: [], blockedApiHosts: [],
    actions: [], seconds: Math.round((Date.now() - t0) / 1000), fakeCal: Boolean(deps.responder),
    knownActions: listActions(), ...fields,
  });
  if ((deps.env || process.env).DEBUG) return { report: finish({ status: "DEBUG_FORBIDDEN" }), files: [] };

  const first = parseJsonLine(await deps.lines.next(CREDENTIALS_TIMEOUT_MS));
  if (!first) return { report: finish({ status: "NO_CREDENTIALS" }), files: [] };
  const plan = prepareActions(first.actions, opts);
  if (!plan.ok) return { report: finish({ status: "BAD_ACTIONS", detail: plan.error }), files: [] };

  await wipeDir(deps.profileDir);
  const waitForOtp = async () => {
    if (typeof deps.waitForOtp === "function") return deps.waitForOtp();
    const reply = parseJsonLine(await deps.lines.next(opts.otpWaitMs));
    return reply || { otp: null, error: "OTP_TIMEOUT" };
  };

  const opened = await openCalSession({
    puppeteer: deps.puppeteer, createScraper: deps.createScraper, CompanyTypes: deps.CompanyTypes,
    executablePath: deps.executablePath, profileDir: deps.profileDir,
    allowedHosts: deps.allowedHosts, apiHosts: deps.apiHosts, extraLaunchArgs: deps.extraLaunchArgs,
    responder: deps.responder, fetchImpl: deps.fetchImpl, requireTmpfs: deps.requireTmpfs,
    navTimeoutMs: opts.navTimeoutMs, otpResultTimeoutMs: opts.otpResultTimeoutMs, otpEntry: opts.otpEntry,
    emit: deps.emit, waitForOtp,
  }, { username: first.username, password: first.password });
  first.username = first.password = undefined;

  if (!opened.ok) {
    await wipeDir(deps.profileDir);
    const report = finish({
      status: opened.status, stage: opened.stage, stopForToday: CAL_STOP_TODAY.has(opened.status),
      otpRequested: opened.otpAsked, otpDelivered: opened.otpDelivered, detail: opened.detail,
      blockedHosts: opened.blockedHosts, failedHosts: opened.failedHosts, warnings: plan.warnings,
    });
    if (["preflight", "launch"].includes(report.stage)) report.stopForToday = false;
    if (report.status === "LOGIN_PAGE_FAILED") report.stopForToday = false;
    report.profileWiped = (await readdir(deps.profileDir).catch(() => [])).length === 0;
    return { report, files: [] };
  }

  let results = [];
  const files = [];
  const api = opened.session.api;
  try {
    results = await runAll(opened.session, plan.prepared);
    for (const r of results) for (const f of r.files || []) files.push({ kind: f.kind, nameHint: f.nameHint, body: f.body });
  } finally {
    await opened.session.close();
    await wipeDir(deps.profileDir);
  }
  const allOk = results.length > 0 && results.every((r) => r.ok);
  const failed = results.find((r) => !r.ok);
  const blocked = results.some((r) => r.status === "CAL_BLOCKED" || r.status === "CAL_BLOCKED_PARTIAL");
  const tx = results.find((r) => r.name === "card_transactions" && r.ok)?.summary || null;
  const report = finish({
    status: allOk ? "DONE" : (failed?.status || "ACTION_FAILED"),
    stage: allOk ? "done" : "actions",
    // Blocked mid-walk still ingests what came back, but no more runs today.
    stopForToday: blocked || (!allOk && CAL_STOP_TODAY.has(failed?.status)),
    blocked,
    otpRequested: opened.otpAsked, otpDelivered: opened.otpDelivered,
    blockedHosts: opened.blockedHosts, failedHosts: [...opened.failedHosts, ...opened.session.failed],
    apiHosts: [...(api.apiHosts || [])], blockedApiHosts: [...(api.blockedApiHosts || [])],
    actions: results.map(({ name, ok, status, warnings, summary, detail }) => ({ name, ok, status, warnings, summary, detail })),
    warnings: plan.warnings,
    cardCount: tx?.cardCount, transactionCount: tx?.transactionCount, pendingCount: tx?.pendingCount,
    from_month: tx?.from_month, to_month: tx?.to_month, monthsCovered: tx?.monthsCovered, cards: tx?.cards,
    profileWiped: (await readdir(deps.profileDir).catch(() => [])).length === 0,
  });
  return { report, files };
}

function silenceConsole() {
  for (const k of ["log", "info", "warn", "error", "debug", "trace", "dir"]) console[k] = () => {};
}

export const FAKE_SCENARIOS = {
  ok: {}, otp: { otp: true }, "wrong-otp": { otp: true, expectedCode: "000000" }, "wrong-password": { wrongPassword: true },
  captcha: { captcha: true }, "month-fail": { failMonth: "2026-08" },
};

async function main() {
  silenceConsole();
  let fake = null;
  for (const arg of process.argv.slice(2)) {
    const m = /^--fake-cal(?:=([a-z-]+))?$/.exec(arg);
    if (m) fake = m[1] || "ok";
    else {
      process.stdout.write(`${JSON.stringify({ event: "result", report: { site: "cal", status: "BAD_ARGS", stage: "preflight" }, files: [] })}\n`);
      process.exit(2);
    }
  }
  const emit = makeEmitter((s) => process.stdout.write(s));
  const lines = createLineReader(process.stdin);
  const [{ default: puppeteer }, { CompanyTypes, createScraper }] = await Promise.all([
    import("puppeteer"), import("israeli-bank-scrapers"),
  ]);
  let responder;
  let fetchImpl;
  let extraLaunchArgs = [];
  if (fake) {
    const { fakeCal } = await import("./lib/cal/fake-cal.mjs");
    const f = fakeCal(FAKE_SCENARIOS[fake] || FAKE_SCENARIOS.ok);
    responder = f.responder;
    fetchImpl = f.fetch;
    extraLaunchArgs = OFFLINE_ARGS;
  }
  const opts = envOptions();
  const { report, files } = await runCalAuto({
    lines, emit, puppeteer, createScraper, CompanyTypes,
    executablePath: process.env.CHROMIUM_PATH || "/usr/bin/chromium",
    profileDir: process.env.CAL_PROFILE_DIR || process.env.HAPOALIM_PROFILE_DIR || "/profile",
    allowedHosts: parseCalAllowlist(opts.extraHosts),
    requireTmpfs: process.env.CAL_REQUIRE_TMPFS !== "0",
    responder, fetchImpl, extraLaunchArgs,
  }, opts);
  emit({ event: "result", report, files });
  lines.close();
  process.exitCode = report.status === "DONE" ? 0 : (report.stage === "preflight" ? 2 : 1);
  setTimeout(() => process.exit(process.exitCode), 200).unref();
}

process.on("unhandledRejection", () => {
  process.stdout.write(`${JSON.stringify({ event: "result", report: { site: "cal", status: "INTERNAL", stage: "unknown" }, files: [] })}\n`);
  process.exit(1);
});

if (import.meta.url === pathToFileURL(process.argv[1] || "").href) await main();
