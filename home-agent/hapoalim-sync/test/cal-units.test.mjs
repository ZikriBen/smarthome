import assert from "node:assert/strict";
import { describe, test } from "node:test";
import { addMonths, monthsDesc, resolveMonthRange } from "../lib/cal/months.mjs";
import { CAL_ALLOWED, guardFetch, parseCalAllowlist } from "../lib/cal/egress.mjs";
import { hostAllowed } from "../lib/egress.mjs";
import { toArchiveRow, validate, walkOptions } from "../lib/cal/actions/card_transactions.mjs";
import * as calActions from "../lib/cal/actions/index.mjs";
import * as hapActions from "../lib/actions/index.mjs";

const NOW = new Date("2026-10-22T12:00:00+03:00");

describe("cal months", () => {
  test("arithmetic", () => {
    assert.equal(addMonths("2026-01", -1), "2025-12");
    assert.equal(addMonths("2026-12", 1), "2027-01");
    assert.deepEqual(monthsDesc("2025-11", "2026-02"), ["2026-02", "2026-01", "2025-12", "2025-11"]);
  });
  test("defaults: 3 months back to next statement", () => {
    const r = resolveMonthRange({}, NOW);
    assert.deepEqual(r.months, ["2026-11", "2026-10", "2026-09", "2026-08"]);
  });
  test("clamps and validates", () => {
    const r = resolveMonthRange({ from_month: "2020-01", to_month: "2030-01" }, NOW);
    assert.equal(r.from, "2025-04");
    assert.equal(r.to, "2026-11");
    assert.equal(r.warnings.length, 2);
    assert.equal(resolveMonthRange({ from_month: "2026-13" }, NOW).ok, false);
    assert.equal(resolveMonthRange({ from_month: "2026-09", to_month: "2026-08" }, NOW).ok, false);
  });
  test("action validate", () => {
    const v = validate({ from_month: "2026-09", to_month: "2026-10" }, NOW);
    assert.deepEqual(v.params.months, ["2026-10", "2026-09"]);
  });
});

describe("cal egress", () => {
  test("browser allowlist is cal-online only", () => {
    assert.ok(hostAllowed("www.cal-online.co.il", CAL_ALLOWED, []));
    assert.ok(hostAllowed("connect.cal-online.co.il", CAL_ALLOWED, []));
    assert.ok(!hostAllowed("cal-online.co.il.evil.com", CAL_ALLOWED, []));
    assert.ok(!hostAllowed("login.bankhapoalim.co.il", CAL_ALLOWED, []));
    assert.ok(!hostAllowed("www.google-analytics.com", CAL_ALLOWED, []));
    assert.ok(hostAllowed("cdn.example.net", parseCalAllowlist("example.net"), []));
  });
  test("node fetch guard: api host only, https only, 429 -> Automation detected", async () => {
    const calls = [];
    const real = async (u) => { calls.push(u); return new Response("{}", { status: String(u).includes("busy") ? 429 : 200 }); };
    const record = {};
    const f = guardFetch(real, { record });
    await f("https://api.cal-online.co.il/x");
    await assert.rejects(f("https://evil.example/x"), /EGRESS_BLOCKED/);
    await assert.rejects(f("http://api.cal-online.co.il/x"), /EGRESS_BLOCKED/);
    await assert.rejects(f("https://www.cal-online.co.il/x"), /EGRESS_BLOCKED/);
    await assert.rejects(f("https://api.cal-online.co.il/busy"), /Automation detected/);
    assert.equal(calls.length, 2);
    assert.deepEqual([...record.blockedApiHosts].sort(), ["api.cal-online.co.il", "evil.example", "www.cal-online.co.il"]);
  });
});

describe("cal transport", () => {
  test("node guard: HTML answer (Akamai 'Request Rejected') -> Automation detected, counted, body unread", async () => {
    const record = {};
    let read = false;
    const real = async () => ({ status: 200, headers: { get: () => "text/html; charset=utf-8" }, text: async () => { read = true; return "x"; } });
    const f = guardFetch(real, { record });
    await assert.rejects(f("https://api.cal-online.co.il/x"), /Automation detected/);
    assert.equal(record.statuses.html, 1);
    assert.equal(read, false);
    const ok = guardFetch(async () => ({ status: 200, headers: { get: () => "application/json" } }), { record: {} });
    assert.equal((await ok("https://api.cal-online.co.il/x")).status, 200);
  });
  test("walkOptions transport: page by default, node only when exactly 'node'", () => {
    assert.equal(walkOptions({}).transport, "page");
    assert.equal(walkOptions({ CAL_API_TRANSPORT: "node" }).transport, "node");
    assert.equal(walkOptions({ CAL_API_TRANSPORT: "NODE; rm -rf" }).transport, "page");
  });
});

describe("cal rows + registry", () => {
  test("archive row date semantics", () => {
    const r = toArchiveRow("1111", { status: "completed", calBillingMonth: "2026-03", calPurchaseDate: "2026-01-15T00:00:00.000Z",
      date: "2026-02-15T00:00:00.000Z", processedDate: "2026-03-10T00:00:00.000Z", chargedAmount: -1, originalAmount: -3,
      originalCurrency: "ILS", chargedCurrency: "ILS", installments: { number: 2, total: 3 }, identifier: "x" });
    assert.equal(r.purchase_date, "2026-01-15");
    assert.equal(r.charge_date, "2026-03-10");
    assert.equal(r.billing_month, "2026-03");
    assert.equal(r.installment_number, 2);
    const p = toArchiveRow("1111", { status: "pending", date: "2026-10-20T00:00:00.000Z", processedDate: "2026-10-20T00:00:00.000Z" });
    assert.equal(p.charge_date, null);
    assert.equal(p.billing_month, null);
  });
  test("registries are separate", () => {
    assert.deepEqual(calActions.list(), ["card_transactions"]);
    assert.deepEqual(hapActions.list(), ["checking_transactions"]);
    assert.equal(calActions.prepare({ name: "checking_transactions" }).ok, false);
    assert.equal(hapActions.prepare({ name: "card_transactions" }).ok, false);
  });
});
