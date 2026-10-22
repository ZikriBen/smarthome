// Layer A — generic Bank Hapoalim session / login.
// Credentials from stdin (via protocol), OTP from the relayed-file wait (otpCodeRetriever callback), ONE login
// attempt, then an authenticated scraper (page + cookies + API context) handed to the actions. The browser
// profile is a fresh tmpfs, wiped by the caller when the session closes. Never retries the login.
import { makePreparePage } from "./egress.mjs";
import { makeRedactor } from "./redact.mjs";
import { mountType } from "./summary.mjs";

export const STOP_TODAY = new Set(["INVALID_PASSWORD", "CHANGE_PASSWORD", "ACCOUNT_BLOCKED", "LOGIN_UNKNOWN",
  "OTP_TIMEOUT", "OTP_REJECTED", "OTP_NO_RESULT", "OTP_FORM_CHANGED", "OTP_REQUIRED", "OTP_AMBIGUOUS",
  "CAPTCHA_OR_BLOCK"]);

async function pageShowsCaptcha(page) {
  try {
    return await page.evaluate(() => {
      const html = document.documentElement ? document.documentElement.outerHTML.slice(0, 400000) : "";
      return /captcha|g-recaptcha|hcaptcha|px-captcha|אני לא רובוט/i.test(html);
    });
  } catch {
    return false;
  }
}

function classifyLogin({ progress, otpAsked, loginResult, thrown, captchaSeen }) {
  if (captchaSeen || progress.captcha) {
    return { status: "CAPTCHA_OR_BLOCK", stage: progress.submitted ? "credentials" : "login-page" };
  }
  const message = String(loginResult?.errorMessage || thrown?.message || "");
  const otp = /^(OTP_[A-Z_]+):/.exec(message)?.[1];
  if (otp) return { status: otp, stage: otp === "OTP_REJECTED" ? "otp-rejected" : "otp-requested" };
  if (!progress.loginPage || !progress.submitted) return { status: "LOGIN_PAGE_FAILED", stage: "login-page" };
  const type = String(loginResult?.errorType || "");
  if (["INVALID_PASSWORD", "CHANGE_PASSWORD", "ACCOUNT_BLOCKED"].includes(type)) {
    return { status: type, stage: "credentials" };
  }
  if (otpAsked) return { status: "OTP_NO_RESULT", stage: "otp-requested" };
  return { status: "LOGIN_UNKNOWN", stage: "credentials" };
}

/**
 * Open a hardened Chromium, log in to Bank Hapoalim once, return an authenticated session.
 *
 * deps: { puppeteer, createScraper, CompanyTypes, executablePath, profileDir, allowedHosts,
 *         extraLaunchArgs, responder (fake bank), requireTmpfs, loginTimeoutMs,
 *         otpWaitMs, emit (otp_needed), waitForOtp (() => Promise<code|error>) }
 * credentials: { userCode, password }
 *
 * Returns either:
 *   { ok:true, session:{ scraper, page, browser, progress, redact, close }, otpAsked, otpDelivered }
 *   { ok:false, status, stage, detail, otpAsked, otpDelivered, progress, blockedHosts, failedHosts }
 */
export async function openSession(deps, credentials) {
  const redact = makeRedactor([]);
  const progress = {};
  const blocked = new Set();
  const failed = new Set();
  let browser = null;
  let page = null;
  let scraper = null;
  let otpAsked = false;
  let otpDelivered = false;
  let captchaSeen = false;

  if (deps.requireTmpfs !== false && mountType(deps.profileDir) !== "tmpfs") {
    return { ok: false, status: "PROFILE_NOT_TMPFS", stage: "preflight", otpAsked, otpDelivered,
      progress, blockedHosts: [], failedHosts: [] };
  }

  const userCode = String(credentials?.userCode || "").trim();
  const password = String(credentials?.password || "");
  if (!userCode || !password) {
    return { ok: false, status: "NO_CREDENTIALS", stage: "preflight", otpAsked, otpDelivered,
      progress, blockedHosts: [], failedHosts: [] };
  }
  redact.add(userCode);
  redact.add(password);

  try {
    browser = await deps.puppeteer.launch({
      headless: true,
      executablePath: deps.executablePath,
      userDataDir: deps.profileDir,
      // --no-sandbox: the container is the sandbox (same reasoning as the PoC README).
      args: ["--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
        "--disable-background-networking", "--disable-sync", "--metrics-recording-only",
        "--disable-component-update", "--disable-breakpad", ...(deps.extraLaunchArgs || [])],
      timeout: 60_000,
    });
  } catch (error) {
    return { ok: false, status: "LAUNCH_FAILED", stage: "launch", detail: redact(error?.message || error),
      otpAsked, otpDelivered, progress, blockedHosts: [...blocked], failedHosts: [...failed] };
  }

  const preparePage = makePreparePage({
    allowed: deps.allowedHosts, blocked, failed, progress, responder: deps.responder,
    onPage: (p) => {
      page = p;
      p.on("request", (request) => {
        const method = request.method();
        if (progress.loginPage && method !== "GET" && method !== "HEAD" && method !== "OPTIONS") {
          progress.submitted = true;
        }
      });
    },
  });

  const otpCodeRetriever = async () => {
    otpAsked = true;
    deps.emit?.({ event: "otp_needed" });
    const reply = await deps.waitForOtp();
    const code = typeof reply?.otp === "string" ? reply.otp.trim() : "";
    if (!/^[0-9]{4,8}$/.test(code)) {
      const err = reply?.error === "OTP_AMBIGUOUS" ? "OTP_AMBIGUOUS" : "OTP_TIMEOUT";
      throw Object.assign(new Error("no code"), { otpErrorCode: err });
    }
    redact.add(code);
    otpDelivered = true;
    return code;
  };

  // startDate is irrelevant for login; each action sets its own range before fetchData.
  scraper = deps.createScraper({
    companyId: deps.CompanyTypes.hapoalim,
    startDate: new Date(),
    combineInstallments: false,
    browser,
    skipCloseBrowser: true,
    preparePage,
    loginTimeout: deps.loginTimeoutMs || 180_000,
    otpResultTimeout: 45_000,
    timeout: 90_000,
    defaultTimeout: 90_000,
    navigationRetryCount: 0,
  });

  let loginResult = null;
  let thrown = null;
  try {
    await scraper.initialize();   // creates the page + runs preparePage (egress allowlist)
    page = scraper.page;
    loginResult = await scraper.login({ userCode, password, otpCodeRetriever });
  } catch (error) {
    thrown = error;
  }

  const success = loginResult?.success === true
    || String(loginResult?.errorType || loginResult || "").toUpperCase() === "SUCCESS";
  // Some versions return LoginResults.Success string; others {success:true}. Also accept a homepage URL seen.
  const loggedIn = success || progress.loggedIn;

  if (!loggedIn) {
    if (page) captchaSeen = await pageShowsCaptcha(page);
    const { status, stage } = classifyLogin({ progress, otpAsked, loginResult, thrown, captchaSeen });
    try { await browser.close(); } catch { /* ignore */ }
    return {
      ok: false, status, stage,
      detail: redact(loginResult?.errorMessage || loginResult?.errorType || thrown?.message || "login failed"),
      otpAsked, otpDelivered, progress,
      blockedHosts: [...blocked], failedHosts: [...failed],
    };
  }
  progress.loggedIn = true;

  const session = {
    scraper, page, browser, progress, redact, blocked, failed,
    async close() {
      try { await browser.close(); } catch { /* ignore */ }
      redact.forget();
    },
  };
  return { ok: true, session, otpAsked, otpDelivered, blockedHosts: [...blocked], failedHosts: [...failed] };
}
