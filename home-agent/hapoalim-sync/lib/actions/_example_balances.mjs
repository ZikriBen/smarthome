// STUB — NOT registered in production. Copy this shape when adding a new action (see README
// "How to add a new action"). Example: account balances / credit-card statements.
//
// To wire it up later:
//   1. implement validate + run against the authenticated session (session.scraper.page is the
//      logged-in puppeteer Page; the bank's REST calls go through fetchGetWithinPage on that page);
//   2. register it from lib/actions/index.mjs: register(balances);
//   3. add fake-bank fixtures + a unit/e2e test;
//   4. extend the Hermes tool params if the action needs new fields.
import { register } from "./registry.mjs";

export const name = "balances";

export function validate(_params = {}) {
  return { ok: true, params: {}, warnings: [] };
}

export async function run(_session, _params) {
  return {
    ok: false,
    status: "NOT_IMPLEMENTED",
    detail: "balances action is a stub; not registered in production",
  };
}

// Deliberately NOT called: register({ name, validate, run });
void register;
export default { name, validate, run };
