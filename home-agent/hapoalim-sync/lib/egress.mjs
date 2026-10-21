// In-browser egress allowlist (defense in depth behind the network-level proxy allowlist).
// Requests to any other host are aborted; only blocked HOSTNAMES are recorded (never paths or queries).
// UNVERIFIED: the full set of hosts the bank's login/app needs. Start strict; widen only from blockedHosts.
export const DEFAULT_ALLOWED = [/^(?:[a-z0-9-]+\.)*bankhapoalim\.co\.il$/i];

export function hostAllowed(host, allowed = DEFAULT_ALLOWED) {
  return allowed.some((rule) => rule.test(host));
}

export function parseAllowlist(text) {
  const extra = String(text || "").split(/[\s,]+/).filter(Boolean)
    .filter((h) => /^[a-z0-9.-]+$/i.test(h))
    .map((h) => new RegExp(`^(?:[a-z0-9-]+\\.)*${h.replace(/\./g, "\\.")}$`, "i"));
  return [...DEFAULT_ALLOWED, ...extra];
}

/** Returns an async preparePage(page) that installs the filter; `responder(request)` is a test hook. */
export function makePreparePage({ allowed = DEFAULT_ALLOWED, blocked = new Set(), responder } = {}) {
  return async function preparePage(page) {
    await page.setRequestInterception(true);
    page.on("request", (request) => {
      if (request.isInterceptResolutionHandled?.()) return;
      let host = "";
      try {
        const url = new URL(request.url());
        if (url.protocol === "data:" || url.protocol === "blob:" || url.protocol === "about:") {
          return request.continue();
        }
        host = url.hostname;
      } catch {
        return request.abort("blockedbyclient");
      }
      if (!hostAllowed(host, allowed)) {
        if (blocked.size < 50) blocked.add(host);
        return request.abort("blockedbyclient");
      }
      if (responder) return responder(request);
      return request.continue();
    });
  };
}
