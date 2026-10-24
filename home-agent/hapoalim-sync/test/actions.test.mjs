import assert from "node:assert/strict";
import { describe, test } from "node:test";
import { prepare, list, _resetForTests, register } from "../lib/actions/registry.mjs";
import checking from "../lib/actions/checking_transactions.mjs";
import { prepareActions } from "../auto.mjs";

describe("action registry", () => {
  test("production list is just checking_transactions", async () => {
    // Re-import the wired index to confirm what's registered in the app.
    const mod = await import("../lib/actions/index.mjs");
    assert.deepEqual(mod.list(), ["checking_transactions"]);
  });
  test("unknown action", () => {
    _resetForTests();
    register(checking);
    const v = prepare({ name: "balances" });
    assert.equal(v.ok, false);
    assert.match(v.error, /unknown action/);
  });
  test("checking_transactions validates dates", () => {
    _resetForTests();
    register(checking);
    const bad = prepare({ name: "checking_transactions", from: "nope", to: "2026-10-01" });
    assert.equal(bad.ok, false);
    const ok = prepare({ name: "checking_transactions", from: "2026-09-01", to: "2026-10-01" });
    assert.equal(ok.ok, true);
    assert.equal(ok.params.from, "2026-09-01");
  });
  test("prepareActions rejects unknown and accepts default", () => {
    _resetForTests();
    register(checking);
    const bad = prepareActions([{ name: "nope" }], { defaultDays: 45, maskAccounts: true });
    assert.equal(bad.ok, false);
    const def = prepareActions(null, { defaultDays: 45, maskAccounts: true });
    assert.equal(def.ok, true);
    assert.equal(def.prepared[0].name, "checking_transactions");
  });
});
