// In-browser egress allowlist + progress tracking. Only HOSTNAMES are recorded (never paths or queries).
// Default: *.bankhapoalim.co.il only. Extra hosts (if the login needs a CDN) via POC_EXTRA_HOSTS (not secret).
export const DEFAULT_ALLOWED = [/^(?:[a-z0-9-]+\.)*bankhapoalim\.co\.il$/i];
// Bank-owned but not needed for login/transactions: bcodes.* is a tag/iframe host listed in the bank's public-site
// CSP (script-src/frame-src, next to *.bcodes.co.il, bcodes.fingrow.co.il, bcodes.bankhapoalim.biz). In the live PoC
// (2026-10-22) its request FAILED and login + OTP + transactions still worked, so it is blocked explicitly: every
// run then behaves like the proven one, and one less analytics/fingerprint call leaves the NUC.
export const DEFAULT_DENIED = [/^bcodes\./i];
const CAPTCHA_HOST = /captcha|recaptcha|hcaptcha|arkoselabs|funcaptcha|perimeterx|px-cdn|px-cloud|datadome|incapsula|imperva/i;

export function hostAllowed(host, allowed = DEFAULT_ALLOWED, denied = DEFAULT_DENIED) {
  if (denied.some((rule) => rule.test(host))) return false;
  return allowed.some((rule) => rule.test(host));
}

export function parseAllowlist(text) {
  const extra = String(text || "").split(/[\s,]+/).filter(Boolean)
    .filter((h) => /^[a-z0-9.-]+$/i.test(h))
    .map((h) => new RegExp(`^(?:[a-z0-9-]+\\.)*${h.replace(/\./g, "\\.")}$`, "i"));
  return [...DEFAULT_ALLOWED, ...extra];
}

/** progress: { loginPage, credentials, loggedIn, accounts, transactions, captcha } booleans. */
export function makePreparePage({ allowed = DEFAULT_ALLOWED, denied = DEFAULT_DENIED, blocked = new Set(), failed = new Set(), progress = {}, responder, onPage } = {}) {
  return async function preparePage(page) {
    onPage?.(page);
    await page.setRequestInterception(true);
    page.on("requestfailed", (request) => {
      try {
        const url = new URL(request.url());
        const why = request.failure()?.errorText || "";
        if (/BLOCKED_BY_CLIENT/.test(why) || !/^https?:$/.test(url.protocol)) return;
        if (failed.size < 50) failed.add(url.hostname);
      } catch { /* ignore */ }
    });
    page.on("request", (request) => {
      if (request.isInterceptResolutionHandled?.()) return;
      let url;
      try {
        url = new URL(request.url());
      } catch {
        return request.abort("blockedbyclient");
      }
      if (url.protocol === "data:" || url.protocol === "blob:" || url.protocol === "about:") return request.continue();
      const host = url.hostname;
      if (!hostAllowed(host, allowed, denied)) {
        if (CAPTCHA_HOST.test(host) || CAPTCHA_HOST.test(url.pathname)) progress.captcha = true;
        if (blocked.size < 50) blocked.add(host);
        return request.abort("blockedbyclient");
      }
      const p = url.pathname;
      if (/\/cgi-bin\/poalwwwc/i.test(p) || /getLogonPage/i.test(url.search)) progress.loginPage = true;
      if (/\/AUTHENTICATE\//i.test(p)) progress.credentials = true;
      if (/\/ng-portals\/(rb|biz)\/he\/homepage/i.test(p)) progress.loggedIn = true;
      if (/\/ServerServices\/general\/accounts/i.test(p)) progress.accounts = true;
      if (/\/current-account\/transactions/i.test(p)) progress.transactions = true;
      if (CAPTCHA_HOST.test(p)) progress.captcha = true;
      if (responder) return responder(request);
      return request.continue();
    });
  };
}
