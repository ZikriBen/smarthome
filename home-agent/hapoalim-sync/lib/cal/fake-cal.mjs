// A FAKE Cal (visaCal), tests only. Browser side: served from puppeteer request interception (the browser also gets
// a dead proxy + NOTFOUND resolver). API side: a fetch() replacement for api.cal-online.co.il (the library calls the
// API from Node by default, see ./egress.mjs); with CAL_API_TRANSPORT=page the same handler answers in-page calls. Everything is synthetic; no network. Markup mirrors the selectors visa-cal.js
// uses (#ccLoginDesktopBtn, iframe on connect.*, #regular-login, <regular-login>, formcontrolname inputs,
// button[type=submit], div.general-error > div). The OTP form is a GUESS (Cal's real one is unverified).
import { INVALID_PASSWORD_TEXT } from "./session.mjs";

export const WWW = "https://www.cal-online.co.il";
export const CONNECT = "https://connect.cal-online.co.il";
export const DIGITAL = "https://digital-web.cal-online.co.il";
export const API = "https://api.cal-online.co.il";
export const SSO_URL = `${CONNECT}/col-rest/calconnect/authentication/SSO`;

const html = (body, script = "") => `<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8"><title>fake cal</title></head>
<body>${body}<script>${script}</script></body></html>`;

function wwwPage(captcha) {
  return html(`${captcha ? '<div class="g-recaptcha"></div>' : ""}<button id="ccLoginDesktopBtn">כניסה לאזור האישי</button><div id="slot"></div>`, `
document.getElementById("ccLoginDesktopBtn").addEventListener("click", () => {
  const f = document.createElement("iframe");
  f.src = "${CONNECT}/login";
  f.style.width = "600px"; f.style.height = "500px";
  document.getElementById("slot").appendChild(f);
});
window.addEventListener("message", (e) => {
  if (e.origin === "${CONNECT}" && e.data && e.data.go) location.href = e.data.go;
});`);
}

function connectPage(s) {
  return html(`${s.captchaScript ? '<script src="https://www.cal-online.co.il/recaptcha-loader.js"></script>' : ""}<div id="tabs"><span id="otp-login-tab">כניסה עם קוד</span> <span id="regular-login">כניסה עם סיסמה</span></div><div id="app"></div>`, `
const post = (u, b, h) => fetch(u, { method: "POST", headers: { "content-type": "application/json", ...(h || {}) }, body: JSON.stringify(b) }).then((r) => r.json());
const done = async () => {
  await post("${SSO_URL}", {}, { authorization: "CALAuthScheme fake-token" });
  parent.postMessage({ go: "${DIGITAL}/dashboard" }, "${WWW}");
};
document.getElementById("regular-login").addEventListener("click", () => {
  document.getElementById("app").innerHTML = '<regular-login><form onsubmit="return false"><input formcontrolname="userName"><input formcontrolname="password" type="password"><button type="submit">כניסה</button><div id="err"></div></form></regular-login>';
  document.querySelector('button[type="submit"]').addEventListener("click", async () => {
    const j = await post("${CONNECT}/__fake/login", { u: document.querySelector('[formcontrolname="userName"]').value, p: document.querySelector('[formcontrolname="password"]').value });
    if (j.next === "stall") return;   // the (fake) site just sits there behind its challenge
    if (j.next === "bad") { document.getElementById("err").innerHTML = '<div class="general-error"><div>${INVALID_PASSWORD_TEXT}</div></div>'; return; }
    if (j.next === "change") { document.getElementById("app").innerHTML = '<change-password><div class="change-password-title">החלפת סיסמה</div></change-password>'; return; }
    if (j.next === "otp") {
      const n = j.inputs;
      document.getElementById("app").innerHTML = '<form class="otp" onsubmit="return false"><p>הזינו את הקוד שנשלח אליך ב-SMS</p>' +
        Array.from({ length: n }, () => '<input type="tel" name="otpCode" maxlength="' + (n === 1 ? 8 : 1) + '">').join("") +
        '<button type="submit">המשך</button><div class="general-error-slot"></div></form>';
      document.querySelector(".otp button").addEventListener("click", async () => {
        const code = [...document.querySelectorAll(".otp input")].map((i) => i.value).join("");
        const r = await post("${CONNECT}/__fake/otp", { code });
        if (r.ok) { if (!r.hang) await done(); return; }
        document.querySelector(".general-error-slot").innerHTML = '<div class="general-error"><div>הקוד שהוזן שגוי</div></div>';
      });
      return;
    }
    await done();
  });
});`);
}

function dashboardPage(cards) {
  const init = JSON.stringify({ result: { cards } });
  const auth = JSON.stringify({ auth: { calConnectToken: "fake-token" } });
  return html("<h1>דשבורד</h1>", `sessionStorage.setItem("init", ${JSON.stringify(init)}); sessionStorage.setItem("auth-module", ${JSON.stringify(auth)});`);
}

const ymd = (y, m, d) => `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}T00:00:00`;

/** Synthetic rows for one card's billing month: purchases dated in the previous month, debited on the 10th. */
export function defaultMonthRows(card, ym, { rows } = {}) {
  const y = +ym.slice(0, 4);
  const m = +ym.slice(5, 7);
  const py = m === 1 ? y - 1 : y;
  const pm = m === 1 ? 12 : m - 1;
  const deb = ymd(y, m, 10);
  const base = (n, extra) => ({ trnIntId: `${card.cardUniqueId}-${ym}-${n}`, trnPurchaseDate: ymd(py, pm, 3 + n),
    trnAmt: 10 + n, amtBeforeConvAndIndex: 10 + n, trnCurrencySymbol: "ILS", debCrdDate: deb, debCrdCurrencySymbol: "ILS",
    merchantName: `סוחר דמה ${n}`, transTypeCommentDetails: [], branchCodeDesc: "קטגוריה דמה", trnTypeCode: "5",
    numOfPayments: 0, curPaymentNum: 0, ...extra });
  if (Number.isFinite(rows)) return Array.from({ length: rows }, (_, i) => base(i + 1));
  const out = [base(1), base(2)];
  // Two IDENTICAL rows (same identifier too): both must survive the archive.
  const dup = base(3, { trnIntId: `${card.cardUniqueId}-${ym}-dup` });
  out.push(dup, { ...dup });
  if (card.last4Digits === "1111") {
    // Installment 1..3 of one purchase made 2026-01-15 (the n-th payment lands n-1 months later).
    const k = (y * 12 + m) - (2026 * 12 + 2) + 1;
    if (k >= 1 && k <= 3) {
      out.push(base(9, { trnIntId: `${card.cardUniqueId}-inst-${k}`, trnPurchaseDate: ymd(2026, 1, 15), trnTypeCode: "8",
        numOfPayments: 3, curPaymentNum: k, trnAmt: 300, amtBeforeConvAndIndex: 100, merchantName: "תשלומים דמה" }));
    }
  }
  return out;
}

/**
 * scenario: { cards, otp:bool, otpInputs:1|6, expectedCode, otpHangs, wrongPassword, changePassword, captcha,
 *             monthRows(card, ym) override, failMonth:'YYYY-MM' (statusCode 99 from there), rowsPerMonth (cap tests),
 *             blockedMonth:'YYYY-MM' (429), htmlMonth:'YYYY-MM' (HTML "Request Rejected"), pendingFail, framesFail,
 *             pageCorsFail (in-page API calls get no CORS headers -> the page sees no response) }
 * Returns { responder(request), fetch(url, init), log }.
 */
export function fakeCal(scenario = {}) {
  const s = { otp: false, otpInputs: 6, expectedCode: "246810",
    cards: [{ cardUniqueId: "U-A", last4Digits: "1111" }, { cardUniqueId: "U-B", last4Digits: "2222" }], ...scenario };
  const log = { logins: [], otpSubmissions: [], hosts: new Set(), api: [], sso: 0 };

  function responder(request) {
    const url = new URL(request.url());
    log.hosts.add(url.hostname);
    const send = (body, type = "text/html; charset=utf-8", status = 200) => request.respond({ status, contentType: type, body });
    const json = (o) => send(JSON.stringify(o), "application/json");
    const origin = request.headers().origin || "*";
    const cors = s.pageCorsFail && url.hostname === "api.cal-online.co.il" ? {} : { "access-control-allow-origin": origin,
      "access-control-allow-credentials": "true", "access-control-allow-headers": "authorization,x-site-id,content-type,accept",
      "access-control-allow-methods": "POST" };
    if (request.method() === "OPTIONS") return request.respond({ status: 204, headers: cors, body: "" });
    if (url.hostname === "api.cal-online.co.il") {
      // In-page Cal API calls (CAL_API_TRANSPORT=page). Same synthetic handler as the Node-side fake fetch.
      const h = request.headers();
      const r = apiHandle(url, JSON.parse(request.postData() || "{}"), String(h.authorization || ""), "page");
      return request.respond({ status: r.status, headers: cors, contentType: r.type, body: r.body });
    }
    if (url.hostname === "www.cal-online.co.il" && url.pathname === "/") return send(wwwPage(s.captcha));
    if (url.hostname === "connect.cal-online.co.il") {
      if (url.pathname === "/login") return send(connectPage(s));
      if (url.pathname === "/__fake/login") {
        const { u, p } = JSON.parse(request.postData() || "{}");
        log.logins.push({ u, p });
        if (s.captcha) return json({ next: "stall" });
        if (s.wrongPassword) return json({ next: "bad" });
        if (s.changePassword) return json({ next: "change" });
        return json({ next: s.otp ? "otp" : "ok", inputs: s.otpInputs });
      }
      if (url.pathname === "/__fake/otp") {
        const { code } = JSON.parse(request.postData() || "{}");
        log.otpSubmissions.push(code);
        if (s.otpHangs) return json({ ok: true, hang: true });
        return json({ ok: code === s.expectedCode });
      }
      if (url.pathname === "/col-rest/calconnect/authentication/SSO") { log.sso += 1; return json({ ok: true }); }
    }
    if (url.hostname === "digital-web.cal-online.co.il" && url.pathname === "/dashboard") return send(dashboardPage(s.cards));
    if (url.pathname === "/favicon.ico") return request.respond({ status: 204, body: "" });
    return send("not in the fake cal", "text/plain", 404);
  }

  const respond = (obj, status = 200) => ({ status, type: "application/json", body: JSON.stringify(obj) });
  function apiHandle(url, body, auth, via) {
    log.api.push({ path: url.pathname, month: body.month, year: body.year, card: body.cardUniqueId || body.cardUniqueIDArray?.[0], authOk: auth === "CALAuthScheme fake-token", via });
    if (url.pathname === "/Frames/api/Frames/GetFrameStatus") {
      if (s.framesFail) return respond({ statusCode: 99 }, 500);
      return respond({ statusCode: 1, result: { bankIssuedCards: { cardLevelFrames: s.cards.map((c) => ({ cardUniqueId: c.cardUniqueId, nextTotalDebit: 1, nextDebitDate: "2026-11-10T00:00:00" })) } } });
    }
    if (url.pathname === "/Transactions/api/approvals/getClearanceRequests") {
      if (s.pendingFail) return respond({ statusCode: 50 });
      const card = s.cards.find((c) => c.cardUniqueId === body.cardUniqueIDArray?.[0]);
      const list = card?.last4Digits === "1111" ? [{ trnPurchaseDate: "2026-10-20T00:00:00", trnAmt: 7, trnCurrencySymbol: "ILS",
        merchantName: "ממתין דמה", transTypeCommentDetails: [], branchCodeDesc: "קטגוריה דמה", trnTypeCode: "5", numberOfPayments: 0 }] : [];
      return respond({ statusCode: 1, result: { cardsList: [{ cardUniqueID: body.cardUniqueIDArray?.[0], authDetalisList: list }] } });
    }
    if (url.pathname === "/Transactions/api/transactionsDetails/getCardTransactionsDetails") {
      const ym = `${body.year}-${String(body.month).padStart(2, "0")}`;
      if (s.blockedMonth && ym <= s.blockedMonth) return { status: 429, type: "text/plain", body: "blocked" };
      if (s.htmlMonth && ym <= s.htmlMonth) return { status: 200, type: "text/html", body: "<html><head><title>Request Rejected</title></head></html>" };
      if (s.failMonth && ym <= s.failMonth) return respond({ statusCode: 99, title: "synthetic failure" });
      const card = s.cards.find((c) => c.cardUniqueId === body.cardUniqueId);
      const rows = (s.monthRows || defaultMonthRows)(card, ym, { rows: s.rowsPerMonth });
      return respond({ statusCode: 1, result: { bankAccounts: [{ debitDates: [{ date: "x", transactions: rows }], immidiateDebits: { debitDays: [] } }] } });
    }
    return respond({ statusCode: 404 }, 404);
  }
  async function fakeFetch(input, init = {}) {
    const url = new URL(typeof input === "string" ? input : input.url);
    if (url.origin !== API) throw new Error("fake cal: unexpected host");
    const r = apiHandle(url, JSON.parse(init.body || "{}"), String(init.headers?.Authorization || ""), "node");
    return new Response(r.body, { status: r.status, headers: { "content-type": r.type } });
  }
  return { responder, fetch: fakeFetch, log };
}
