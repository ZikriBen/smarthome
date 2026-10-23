// Cal (visaCal) egress policy. Two layers, hostnames only are ever recorded (never paths, queries or bodies):
//  1. Browser (puppeteer interception, reused from ../egress.mjs): *.cal-online.co.il only (+ CAL_EXTRA_HOSTS).
//  2. Node side: the visaCal scraper calls api.cal-online.co.il with Node's global fetch (helpers/fetch.js fetchPost),
//     NOT from inside the page, so the browser allowlist does not see those calls. guardFetch() wraps
//     globalThis.fetch for the session lifetime: only CAL_API_HOSTS over https, everything else throws before any
//     socket is opened. A 429 or an HTML answer is turned into the library's "Automation detected" error so the walk
//     records `blocked`. With CAL_API_TRANSPORT=page the patched library sends these calls from inside the page
//     instead (layer 1 then applies; api.cal-online.co.il is inside *.cal-online.co.il).
export const CAL_ALLOWED = [/^(?:[a-z0-9-]+\.)*cal-online\.co\.il$/i];
export const CAL_DENIED = [];
export const CAL_API_HOSTS = [/^api\.cal-online\.co\.il$/i];

export function parseCalAllowlist(text) {
  const extra = String(text || "").split(/[\s,]+/).filter(Boolean)
    .filter((h) => /^[a-z0-9.-]+$/i.test(h))
    .map((h) => new RegExp(`^(?:[a-z0-9-]+\\.)*${h.replace(/\./g, "\\.")}$`, "i"));
  return [...CAL_ALLOWED, ...extra];
}

/**
 * record: { apiHosts:Set, blockedApiHosts:Set, statuses:{[code]:count}, calls:number }
 * Returns a fetch-compatible function. Never logs URLs beyond the hostname, never reads bodies.
 */
export function guardFetch(realFetch, { allowed = CAL_API_HOSTS, record = {} } = {}) {
  record.apiHosts ??= new Set();
  record.blockedApiHosts ??= new Set();
  record.statuses ??= {};
  record.calls ??= 0;
  return async function guardedFetch(input, init) {
    let url;
    try {
      url = new URL(typeof input === "string" ? input : input?.url);
    } catch {
      throw new Error("EGRESS_BLOCKED: bad url");
    }
    if (url.protocol !== "https:" || !allowed.some((rule) => rule.test(url.hostname))) {
      if (record.blockedApiHosts.size < 50) record.blockedApiHosts.add(url.hostname);
      throw new Error(`EGRESS_BLOCKED: ${url.hostname}`);
    }
    record.apiHosts.add(url.hostname);
    record.calls += 1;
    const res = await realFetch(input, init);
    const code = Number(res?.status) || 0;
    record.statuses[code] = (record.statuses[code] || 0) + 1;
    if (code === 429) throw new Error(`Automation detected and blocked by server. Status: ${code}`);
    // Akamai answers blocked API calls with an HTML "Request Rejected" page (see upstream PR #1193). The library's
    // fetchPost would die in result.json() with a parse error quoting the body; classify it as blocked instead
    // (status only, the body is never read or logged).
    const type = String(res?.headers?.get?.("content-type") || "");
    if (type && /text\/html/i.test(type)) {
      record.statuses.html = (record.statuses.html || 0) + 1;
      throw new Error(`Automation detected and blocked by server (non-JSON). Status: ${code}`);
    }
    return res;
  };
}

/** Install the guard on globalThis.fetch; returns restore(). */
export function installFetchGuard(realFetch, options) {
  const original = globalThis.fetch;
  globalThis.fetch = guardFetch(realFetch || original, options);
  return () => { globalThis.fetch = original; };
}
