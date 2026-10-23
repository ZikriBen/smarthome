// Cal (visaCal) session / login layer — same contract as ../session.mjs (Hapoalim), separate code path.
// ONE login attempt per run (never retried), credentials from stdin, OTP only if Cal actually shows a code form
// (the library has no OTP hook for visaCal, so a watcher here detects the form in the connect.* login iframe,
// asks the runner for ONE code, types it once and clicks once). Browser egress: *.cal-online.co.il only.
// Node-side API egress (the library uses global fetch): api.cal-online.co.il only (./egress.mjs guardFetch).
import { makePreparePage } from "../egress.mjs";
import { makeRedactor } from "../redact.mjs";
import { mountType } from "../summary.mjs";
import { CAL_ALLOWED, CAL_API_HOSTS, CAL_DENIED, installFetchGuard } from "./egress.mjs";

export const CAL_STOP_TODAY = new Set(["INVALID_PASSWORD", "CHANGE_PASSWORD", "ACCOUNT_BLOCKED", "LOGIN_UNKNOWN",
  "OTP_TIMEOUT", "OTP_REJECTED", "OTP_NO_RESULT", "OTP_FORM_CHANGED", "OTP_REQUIRED", "OTP_AMBIGUOUS",
  "CAPTCHA_OR_BLOCK", "CAL_BLOCKED"]);

export const INVALID_PASSWORD_TEXT = "שם המשתמש או הסיסמה שהוזנו שגויים";
const CONNECT_HOST = /(^|\.)connect\.cal-online\.co\.il$/i;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function connectFrames(page) {
  try {
    return page.frames().filter((f) => {
      try { return CONNECT_HOST.test(new URL(f.url()).hostname); } catch { return false; }
    });
  } catch {
    return [];
  }
}

async function anyFrame(page, fn, arg) {
  for (const frame of [page.mainFrame(), ...connectFrames(page)]) {
    try {
      const v = await frame.evaluate(fn, arg);
      if (v) return v;
    } catch { /* detached / navigating */ }
  }
  return null;
}

const CAPTCHA_FN = () => {
  const html = document.documentElement ? document.documentElement.outerHTML.slice(0, 400000) : "";
  return /captcha|g-recaptcha|hcaptcha|px-captcha|אני לא רובוט/i.test(html);
};

const INVALID_FN = (text) => [...document.querySelectorAll("div.general-error > div, .general-error, .error-message")]
  .some((el) => (el.innerText || el.textContent || "").trim().includes(text));

const CHANGE_PW_FN = () => Boolean(document.querySelector("change-password, .change-password-title"))
  || /\/change-password/.test(location.pathname);

// Detect a one-time-code form inside the login iframe (only meaningful after the credentials were submitted):
// a single code input, or 4-8 single-character boxes, with the username/password form gone.
const OTP_FORM_FN = () => {
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const user = document.querySelector('[formcontrolname="userName"]');
  if (user && visible(user)) return null;
  const inputs = [...document.querySelectorAll("input")].filter(visible)
    .filter((i) => !["hidden", "password", "checkbox", "radio", "submit", "button"].includes((i.type || "").toLowerCase()));
  if (!inputs.length) return null;
  const hint = (i) => [i.name, i.id, i.getAttribute("formcontrolname"), i.getAttribute("autocomplete"),
    i.getAttribute("aria-label"), i.placeholder, i.className].join(" ");
  const named = inputs.filter((i) => /otp|code|sms|one-time-code|קוד/i.test(hint(i)));
  const text = (document.body?.innerText || "").slice(0, 5000);
  const words = /קוד אימות|קוד חד[- ]?פעמי|הזינו את הקוד|הזן את הקוד|נשלח אליך|נשלח לטלפון|SMS/i.test(text);
  if (!named.length && !words) return null;
  const boxes = inputs.filter((i) => Number(i.maxLength) === 1);
  if (boxes.length >= 4 && boxes.length <= 8) return { kind: "boxes", count: boxes.length };
  const one = named.length ? named : inputs;
  if (one.length === 1) return { kind: "single", count: 1 };
  return { kind: "unknown", count: inputs.length };
};

const OTP_FILL_FN = ({ code, kind }) => {
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const inputs = [...document.querySelectorAll("input")].filter(visible)
    .filter((i) => !["hidden", "password", "checkbox", "radio", "submit", "button"].includes((i.type || "").toLowerCase()));
  const set = (el, v) => {
    const proto = Object.getPrototypeOf(el);
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc?.set) desc.set.call(el, v); else el.value = v;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  };
  if (kind === "boxes") {
    const boxes = inputs.filter((i) => Number(i.maxLength) === 1);
    if (boxes.length !== code.length) return "OTP_FORM_CHANGED";
    boxes.forEach((b, i) => set(b, code[i]));
  } else {
    const hint = (i) => [i.name, i.id, i.getAttribute("formcontrolname"), i.getAttribute("autocomplete"),
      i.getAttribute("aria-label"), i.placeholder, i.className].join(" ");
    const named = inputs.filter((i) => /otp|code|sms|one-time-code|קוד/i.test(hint(i)));
    const target = (named.length ? named : inputs);
    if (target.length !== 1) return "OTP_FORM_CHANGED";
    set(target[0], code);
  }
  const buttons = [...document.querySelectorAll("button, input[type=submit]")].filter(visible)
    .filter((b) => !b.disabled);
  const submit = buttons.find((b) => (b.type || "").toLowerCase() === "submit")
    || buttons.find((b) => /המשך|אישור|כניסה|שלח|אמת/.test(b.innerText || b.value || ""));
  if (!submit) return "OTP_FORM_CHANGED";
  submit.click();   // exactly one click
  return "SUBMITTED";
};

const OTP_ERROR_FN = () => {
  const text = [...document.querySelectorAll(".general-error, .error, .error-message, [role=alert]")]
    .map((e) => e.innerText || "").join(" ");
  return /קוד|שגוי|לא תקין|incorrect|invalid/i.test(text);
};

// Order: an explicit answer from Cal (OTP outcome, wrong password, change password) beats the captcha heuristic,
// because the live site may embed a captcha script on every page.
function classify({ progress, thrown, loginResult, captchaSeen, invalidSeen, changeSeen, otp }) {
  if (otp.status) return { status: otp.status, stage: otp.status === "OTP_REJECTED" ? "otp-rejected" : "otp-requested" };
  const type = String(loginResult?.errorType || "");
  if (invalidSeen || type === "INVALID_PASSWORD") return { status: "INVALID_PASSWORD", stage: "credentials" };
  if (changeSeen || type === "CHANGE_PASSWORD") return { status: "CHANGE_PASSWORD", stage: "credentials" };
  if (captchaSeen || progress.captcha) {
    return { status: "CAPTCHA_OR_BLOCK", stage: progress.submitted ? "credentials" : "login-page" };
  }
  if (!progress.loginPage || !progress.submitted) return { status: "LOGIN_PAGE_FAILED", stage: "login-page" };
  if (type === "ACCOUNT_BLOCKED") return { status: "ACCOUNT_BLOCKED", stage: "credentials" };
  void thrown;
  return { status: "LOGIN_UNKNOWN", stage: "credentials" };
}

/**
 * deps: { puppeteer, createScraper, CompanyTypes, executablePath, profileDir, allowedHosts, apiHosts,
 *         extraLaunchArgs, responder (fake Cal browser side), fetchImpl (fake Cal API side; default real fetch),
 *         requireTmpfs, navTimeoutMs, otpResultTimeoutMs, otpEntry (default true), emit, waitForOtp }
 * credentials: { username, password }
 * Returns { ok:true, session:{scraper,page,browser,progress,redact,failed,api,close}, otpAsked, otpDelivered, ... }
 *      or { ok:false, status, stage, detail, otpAsked, otpDelivered, progress, blockedHosts, failedHosts }
 */
export async function openCalSession(deps, credentials) {
  const redact = makeRedactor([]);
  const progress = {};
  const blocked = new Set();
  const failed = new Set();
  const api = {};
  let browser = null;
  let page = null;
  let restoreFetch = null;
  const otp = { asked: false, delivered: false, status: null, done: false };
  const fail = (fields) => ({ ok: false, otpAsked: otp.asked, otpDelivered: otp.delivered, progress,
    blockedHosts: [...blocked], failedHosts: [...failed], ...fields });

  if (deps.requireTmpfs !== false && mountType(deps.profileDir) !== "tmpfs") {
    return fail({ status: "PROFILE_NOT_TMPFS", stage: "preflight" });
  }
  const username = String(credentials?.username || "").trim();
  const password = String(credentials?.password || "");
  if (!username || !password) return fail({ status: "NO_CREDENTIALS", stage: "preflight" });
  redact.add(username);
  redact.add(password);

  try {
    browser = await deps.puppeteer.launch({
      headless: true,
      executablePath: deps.executablePath,
      userDataDir: deps.profileDir,
      args: ["--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
        "--disable-background-networking", "--disable-sync", "--metrics-recording-only",
        "--disable-component-update", "--disable-breakpad", ...(deps.extraLaunchArgs || [])],
      timeout: 60_000,
    });
  } catch (error) {
    return fail({ status: "LAUNCH_FAILED", stage: "launch", detail: redact(error?.message || error) });
  }

  const preparePage = makePreparePage({
    allowed: deps.allowedHosts || CAL_ALLOWED, denied: CAL_DENIED, blocked, failed, progress,
    responder: deps.responder,
    onPage: (p) => {
      page = p;
      p.on("request", (request) => {
        let url;
        try { url = new URL(request.url()); } catch { return; }
        if (/(^|\.)cal-online\.co\.il$/i.test(url.hostname) && request.isNavigationRequest?.()) progress.loginPage = true;
        const m = request.method();
        if (CONNECT_HOST.test(url.hostname) && m !== "GET" && m !== "HEAD" && m !== "OPTIONS") progress.submitted = true;
        if (/\/authentication\/SSO/i.test(url.pathname)) progress.credentials = true;
      });
      p.on("framenavigated", (frame) => {
        try {
          if (frame === p.mainFrame() && /\/dashboard/i.test(new URL(frame.url()).pathname)) progress.loggedIn = true;
        } catch { /* ignore */ }
      });
    },
  });

  // Node-side API guard for the whole session (restored in close()).
  restoreFetch = installFetchGuard(deps.fetchImpl, { allowed: deps.apiHosts || CAL_API_HOSTS, record: api });
  const navTimeoutMs = deps.navTimeoutMs || 60_000;
  const scraper = deps.createScraper({
    companyId: deps.CompanyTypes.visaCal,
    startDate: new Date(),
    combineInstallments: false,
    browser,
    skipCloseBrowser: true,
    preparePage,
    timeout: navTimeoutMs,
    defaultTimeout: navTimeoutMs,
    navigationRetryCount: 0,
    futureMonthsToScrape: 1,
    outputData: { enableTransactionsFilterByDate: false },
  });

  // OTP watcher: runs alongside the library's login; acts at most once.
  let stopWatch = false;
  const watcher = (async () => {
    while (!stopWatch && !progress.loggedIn) {
      await sleep(400);
      if (stopWatch || progress.loggedIn || !progress.submitted || !page) continue;
      let form = null;
      for (const frame of connectFrames(page)) {
        try { form = await frame.evaluate(OTP_FORM_FN); } catch { form = null; }
        if (form) {
          otp.frame = frame;
          break;
        }
      }
      if (!form) continue;
      otp.asked = true;
      if (deps.otpEntry === false) { otp.status = "OTP_REQUIRED"; return; }
      if (form.kind === "unknown") { otp.status = "OTP_FORM_CHANGED"; return; }
      deps.emit?.({ event: "otp_needed" });
      const reply = await deps.waitForOtp();
      const code = typeof reply?.otp === "string" ? reply.otp.trim() : "";
      if (!/^[0-9]{4,8}$/.test(code)) {
        otp.status = reply?.error === "OTP_AMBIGUOUS" ? "OTP_AMBIGUOUS" : "OTP_TIMEOUT";
        return;
      }
      redact.add(code);
      let filled;
      try { filled = await otp.frame.evaluate(OTP_FILL_FN, { code, kind: form.kind }); } catch { filled = "OTP_FORM_CHANGED"; }
      if (filled !== "SUBMITTED") { otp.status = "OTP_FORM_CHANGED"; return; }
      otp.delivered = true;
      const deadline = Date.now() + (deps.otpResultTimeoutMs || 45_000);
      while (Date.now() < deadline) {
        await sleep(400);
        if (progress.loggedIn) return;
        let bad = false;
        try { bad = await otp.frame.evaluate(OTP_ERROR_FN); } catch { /* frame navigated */ }
        if (bad) { otp.status = "OTP_REJECTED"; return; }
      }
      otp.status = "OTP_NO_RESULT";
      return;
    }
  })().finally(() => { otp.done = true; });

  let loginResult = null;
  let thrown = null;
  try {
    await scraper.initialize();
    page = scraper.page;
    loginResult = await scraper.login({ username, password });
  } catch (error) {
    thrown = error;
  }
  // If the code form was seen, the library's navigation wait may have timed out first: let the watcher finish.
  if (otp.asked && !otp.done) await watcher;
  stopWatch = true;
  await watcher;

  const success = loginResult?.success === true;
  const loggedIn = (success || progress.loggedIn) && !otp.status;
  if (!loggedIn) {
    let captchaSeen = false;
    let invalidSeen = false;
    let changeSeen = false;
    if (page) {
      captchaSeen = Boolean(await anyFrame(page, CAPTCHA_FN));
      invalidSeen = Boolean(await anyFrame(page, INVALID_FN, INVALID_PASSWORD_TEXT));
      changeSeen = Boolean(await anyFrame(page, CHANGE_PW_FN));
    }
    const { status, stage } = classify({ progress, thrown, loginResult, captchaSeen, invalidSeen, changeSeen, otp });
    try { await browser.close(); } catch { /* ignore */ }
    restoreFetch();
    return fail({ status, stage,
      detail: redact(loginResult?.errorType || (thrown ? (thrown.name || "Error") : "login failed")) });
  }
  progress.loggedIn = true;

  const session = {
    scraper, page, browser, progress, redact, blocked, failed, api,
    async close() {
      try { await browser.close(); } catch { /* ignore */ }
      restoreFetch();
      redact.forget();
    },
  };
  return { ok: true, session, otpAsked: otp.asked, otpDelivered: otp.delivered,
    blockedHosts: [...blocked], failedHosts: [...failed] };
}
