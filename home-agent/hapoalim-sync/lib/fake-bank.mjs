// A FAKE Bank Hapoalim (copied from the PoC, lives in lib/ so `auto.mjs --fake-bank` works in the image) served entirely from puppeteer request interception (tests only).
// Nothing here talks to the network: every request is answered locally, and the browser is launched with a dead
// proxy + a NOTFOUND resolver as a second guard. Markup mirrors the selectors we patch for (UNVERIFIED vs live:
// form.auth-otp-login / .btn-red_1 / .errors-rb .error-message come from upstream PR #1084).
export const BASE = "https://login.bankhapoalim.co.il";
export const HOMEPAGE = `${BASE}/ng-portals/rb/he/homepage`;
export const INVALID_PASSWORD_URL = `${BASE}/AUTHENTICATE/LOGON?flow=AUTHENTICATE&state=LOGON&errorcode=1.6&callme=false`;

export const SAFE_LAUNCH_ARGS = [
  "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
  "--proxy-server=http://127.0.0.1:9", "--host-resolver-rules=MAP * ~NOTFOUND",
];

const page = (body, script = "") => `<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8"><title>fake</title></head>
<body>${body}<script>${script}</script></body></html>`;

function loginPage(captcha = false) {
  // bcodes.* is requested like on the live login (it must be blocked by lib/egress.mjs before it reaches the responder)
  return page(`<script src="https://bcodes.bankhapoalim.co.il/fake-tag.js"></script>${captcha ? '<iframe src="https://www.google.com/recaptcha/api2/anchor?k=x"></iframe><div class="g-recaptcha"></div>' : ""}<div id="app"><form id="login" onsubmit="return false">
  <input id="userCode" autocomplete="off"><input id="password" type="password">
  <button type="button" class="login-btn">כניסה</button></form></div>`, `
const post = (u, b) => fetch(u, { method: "POST", body: JSON.stringify(b) }).then((r) => r.json());
document.querySelector(".login-btn").addEventListener("click", async () => {
  const j = await post("/__fake/login", { u: userCode.value, p: password.value });
  if (j.next !== "otp") { location.href = j.next; return; }
  const n = j.inputs;
  document.getElementById("app").innerHTML = '<form class="auth-otp-login" onsubmit="return false"><p>הזן את הקוד שנשלח</p>' +
    Array.from({ length: n }, () => '<input type="text" maxlength="' + (n === 1 ? 8 : 1) + '">').join("") +
    '<button type="button" class="btn-red_1">המשך</button><div class="errors-rb"></div></form>';
  document.querySelector(".btn-red_1").addEventListener("click", async () => {
    const code = [...document.querySelectorAll(".auth-otp-login input")].map((i) => i.value).join("");
    const r = await post("/__fake/otp", { code });
    if (r.ok) { if (r.next) location.href = r.next; return; }
    document.querySelector(".errors-rb").innerHTML = '<span class="error-message">הקוד שגוי</span>';
  });
});`);
}

/**
 * scenario: { otp: bool, otpInputs: 1|6, expectedCode, otpHangs: bool, wrongPasswordFor: string, wrongPasswordAny, captcha,
 *             txns: raw bank rows (then served per window, newest first, capped), cap: rows per request (150), ignoreEnd }
 * Returns { responder(request), log } — log records what the "bank" received (for assertions).
 */
export function fakeBank(scenario = {}) {
  const s = { otp: true, otpInputs: 1, expectedCode: "246810", otpHangs: false, ...scenario };
  const log = { logins: [], otpSubmissions: [], hosts: new Set(), apiCalls: [] };
  const json = (request, body, status = 200) =>
    request.respond({ status, contentType: "application/json", body: JSON.stringify(body) });
  const html = (request, body) => request.respond({ status: 200, contentType: "text/html; charset=utf-8", body });

  function responder(request) {
    const url = new URL(request.url());
    log.hosts.add(url.hostname);
    const p = url.pathname;
    if (p === "/cgi-bin/poalwwwc") return html(request, loginPage(s.captcha));
    if (p === "/__fake/login") {
      const { u, p: pw } = JSON.parse(request.postData() || "{}");
      log.logins.push({ u, pw });
      if (s.captcha) return json(request, { next: "/cgi-bin/poalwwwc?reqName=getLogonPage" });
      if (s.wrongPasswordAny || (s.wrongPasswordFor && pw === s.wrongPasswordFor)) return json(request, { next: INVALID_PASSWORD_URL });
      return json(request, s.otp ? { next: "otp", inputs: s.otpInputs } : { next: HOMEPAGE });
    }
    if (p === "/__fake/otp") {
      const { code } = JSON.parse(request.postData() || "{}");
      log.otpSubmissions.push(code);
      if (s.otpHangs) return json(request, { ok: true }); // form stays: no answer from the bank
      return code === s.expectedCode ? json(request, { ok: true, next: HOMEPAGE }) : json(request, { ok: false });
    }
    if (p === "/AUTHENTICATE/LOGON") return html(request, page("<p>סיסמה שגויה</p>"));
    if (p === "/ng-portals/rb/he/homepage") {
      return html(request, page("<h1>דף הבית</h1>", 'window.bnhpApp = { restContext: "/ssb" };'));
    }
    if (p === "/ServerServices/general/accounts") {
      log.apiCalls.push(p);
      return json(request, [{ bankNumber: "12", branchNumber: "600", accountNumber: "987654", accountClosingReasonCode: 0 }]);
    }
    if (p === "/ssb/current-account/composite/balanceAndCreditLimit") {
      log.apiCalls.push(p);
      return json(request, { currentBalance: 1234.5 });
    }
    if (p === "/ssb/current-account/transactions") {
      log.apiCalls.push(p);
      const startD = url.searchParams.get("retrievalStartDate") || "";
      const endD = url.searchParams.get("retrievalEndDate") || "";
      log.txnWindows = log.txnWindows || [];
      log.txnWindows.push({ start: startD, end: endD });
      if (Array.isArray(s.txns)) {
        // Like the live bank: inclusive window, newest first, at most `cap` rows per request.
        const cap = Number(s.cap) > 0 ? Number(s.cap) : 150;
        const inWin = s.txns.filter((t) => (s.ignoreEnd || String(t.eventDate) <= endD) && String(t.eventDate) >= startD);
        inWin.sort((a, b) => String(b.eventDate).localeCompare(String(a.eventDate)));
        return json(request, { transactions: inWin.slice(0, cap) });
      }
      const today = new Date();
      const ymd = `${today.getFullYear()}${String(today.getMonth() + 1).padStart(2, "0")}01`;
      return json(request, { transactions: [
        { eventDate: ymd, valueDate: ymd, eventAmount: 50, eventActivityTypeCode: 2, activityDescription: "סופר", serialNumber: 1, referenceNumber: 111 },
        { eventDate: ymd, valueDate: ymd, eventAmount: 900, eventActivityTypeCode: 1, activityDescription: "משכורת", serialNumber: 2, referenceNumber: 112 },
      ] });
    }
    if (url.hostname.startsWith("bcodes.")) log.bcodesReached = true; // must never happen
    if (p === "/favicon.ico") return request.respond({ status: 204, body: "" });
    return request.respond({ status: 404, contentType: "text/plain", body: "not in the fake bank" });
  }
  return { responder, log };
}
