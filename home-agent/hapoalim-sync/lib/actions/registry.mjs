// Layer B — action registry. Each action: { name, paramsSchema?, validate(params), run(session, params) }.
// Production register() only lists the live actions. Stubs for future work live next to them but are NOT
// registered (see _example_balances.mjs and README "How to add a new action").

const ACTIONS = new Map();

export function register(action) {
  if (!action || typeof action.name !== "string" || typeof action.run !== "function") {
    throw new Error("action needs a name and a run(session, params)");
  }
  if (ACTIONS.has(action.name)) throw new Error(`action already registered: ${action.name}`);
  ACTIONS.set(action.name, action);
}

export function get(name) {
  return ACTIONS.get(name) || null;
}

export function list() {
  return [...ACTIONS.keys()];
}

/**
 * Validate and normalise one action request {name, ...params}.
 * Returns { ok:true, name, params, warnings } or { ok:false, error }.
 */
export function prepare(request) {
  if (!request || typeof request !== "object" || Array.isArray(request)) {
    return { ok: false, error: "action must be an object" };
  }
  const name = String(request.name || "").trim();
  if (!name) return { ok: false, error: "action.name is required" };
  const action = ACTIONS.get(name);
  if (!action) return { ok: false, error: `unknown action: ${name}` };
  const params = { ...request };
  delete params.name;
  if (typeof action.validate === "function") {
    const v = action.validate(params);
    if (!v || v.ok !== true) return { ok: false, error: v?.error || `invalid params for ${name}` };
    return { ok: true, name, params: v.params, warnings: v.warnings || [] };
  }
  return { ok: true, name, params, warnings: [] };
}

/**
 * Run a list of prepared actions against one authenticated session, in order.
 * Each result: { name, ok, status, warnings, files?, summary?, detail? }.
 * Stops on the first action that throws unexpectedly; a soft {ok:false} continues.
 */
export async function runAll(session, prepared) {
  const results = [];
  for (const item of prepared) {
    const action = ACTIONS.get(item.name);
    try {
      const out = await action.run(session, item.params);
      results.push({
        name: item.name,
        ok: out?.ok !== false,
        status: out?.status || (out?.ok === false ? "ACTION_FAILED" : "DONE"),
        warnings: [...(item.warnings || []), ...(out?.warnings || [])],
        files: out?.files || null,
        summary: out?.summary || null,
        detail: out?.detail || null,
      });
    } catch (error) {
      results.push({
        name: item.name, ok: false, status: "ACTION_ERROR",
        warnings: item.warnings || [], files: null, summary: null,
        detail: session.redact?.(error?.message || error) || "action error",
      });
      break;
    }
  }
  return results;
}

export function _resetForTests() {
  ACTIONS.clear();
}
