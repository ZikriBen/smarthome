import assert from "node:assert/strict";
import { describe, test } from "node:test";
import { addDays, earliestFrom, parseISODate, resolveDateRange, todayIL } from "../lib/dates.mjs";

const NOW = new Date("2026-10-22T12:00:00+03:00");

describe("parseISODate", () => {
  test("accepts real days", () => assert.equal(parseISODate("2026-10-22"), "2026-10-22"));
  test("rejects garbage", () => {
    for (const v of ["", "2026-13-01", "2026-02-30", "22-10-2026", "2026/10/22", null, 1]) {
      assert.equal(parseISODate(v), null);
    }
  });
});

describe("resolveDateRange", () => {
  test("defaults to today-45d .. today", () => {
    const r = resolveDateRange({}, NOW);
    assert.equal(r.ok, true);
    assert.equal(r.to, "2026-10-22");
    assert.equal(r.from, addDays("2026-10-22", -45));
    assert.equal(r.warnings.length, 0);
  });
  test("rejects from > to", () => {
    const r = resolveDateRange({ from: "2026-10-20", to: "2026-10-01" }, NOW);
    assert.equal(r.ok, false);
  });
  test("rejects future from", () => {
    const r = resolveDateRange({ from: "2026-10-23", to: "2026-10-23" }, NOW);
    assert.equal(r.ok, false);
  });
  test("clamps future to", () => {
    const r = resolveDateRange({ from: "2026-10-01", to: "2026-12-01" }, NOW);
    assert.equal(r.ok, true);
    assert.equal(r.to, "2026-10-22");
    assert.match(r.warnings[0], /clamped/);
  });
  test("clamps from older than ~1 year", () => {
    const r = resolveDateRange({ from: "2024-01-01", to: "2026-10-22" }, NOW);
    assert.equal(r.ok, true);
    assert.equal(r.from, earliestFrom(NOW));
    assert.match(r.warnings.join(" "), /1 year/);
  });
  test("earliestFrom matches library (today - 1y + 1d)", () => {
    assert.equal(earliestFrom(NOW), "2025-10-23");
    assert.equal(todayIL(NOW), "2026-10-22");
  });
});
