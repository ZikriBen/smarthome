// Unit tests for the backward date-window walk in the patched israeli-bank-scrapers hapoalim.js.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { describe, test } from "node:test";

const require = createRequire(import.meta.url);
const { hapoalimWalkTransactions: walk } = require("israeli-bank-scrapers/lib/scrapers/hapoalim.js");

// A bank that answers an inclusive [start,end] window newest-first, capped.
function bank(rows, { cap = 150, ignoreEnd = false } = {}) {
  const calls = [];
  const fetchWindow = async (start, end) => {
    calls.push({ start, end });
    return rows
      .filter((t) => t.eventDate >= start && (ignoreEnd || t.eventDate <= end))
      .sort((a, b) => b.eventDate.localeCompare(a.eventDate))
      .slice(0, cap)
      .map((t) => ({ ...t }));
  };
  return { fetchWindow, calls };
}
const row = (eventDate, ref, amount, desc = "x", serial = 1) =>
  ({ eventDate, referenceNumber: ref, eventAmount: amount, activityDescription: desc, serialNumber: serial });
const opts = (o = {}) => ({ delayMs: 0, ...o });
const multiset = (rows) => {
  const m = new Map();
  for (const t of rows) {
    const k = JSON.stringify([t.eventDate, t.referenceNumber, t.eventAmount, t.activityDescription, t.serialNumber]);
    m.set(k, (m.get(k) || 0) + 1);
  }
  return [...m.entries()].sort();
};

describe("backward window walk", () => {
  test("under the cap: one window, complete", async () => {
    const b = bank([row("20261001", 1, 10), row("20261002", 2, 20)]);
    const r = await walk(b.fetchWindow, "20250101", "20261022", opts());
    assert.equal(b.calls.length, 1);
    assert.equal(r.transactions.length, 2);
    assert.equal(r.retrieval.complete, true);
    assert.equal(r.retrieval.completeFrom, "2025-01-01");
  });

  test("boundary day with identical repeats is not double-counted and keeps all repeats", async () => {
    // cap 5: window 1 = D3 x2, D2 x2, D1 x1 (of 4 identical). Window 2 ends ON D1 and gets all 4.
    const rows = [
      row("20261003", 7, 1), row("20261003", 7, 2),
      row("20261002", 7, 1), row("20261002", 8, 1),
      row("20261001", 9, 5), row("20261001", 9, 5), row("20261001", 9, 5), row("20261001", 9, 5),
      row("20260930", 7, 1),
    ];
    const b = bank(rows, { cap: 5 });
    const r = await walk(b.fetchWindow, "20260901", "20261022", opts({ cap: 5 }));
    assert.deepEqual(multiset(r.transactions), multiset(rows));
    assert.equal(r.retrieval.complete, true);
    assert.equal(b.calls[1].end, "20261001");
  });

  test("~1 year, >150 rows, duplicate refs: full retrieval", async () => {
    const rows = [];
    const d0 = new Date(Date.UTC(2025, 9, 23));
    for (let i = 0; i < 365; i++) {
      const d = new Date(d0.getTime() + i * 86400000).toISOString().slice(0, 10).replaceAll("-", "");
      const n = 1 + (i % 3);
      for (let j = 0; j < n; j++) rows.push(row(d, 1000 + ((i + j) % 42), 10 + j, `m${j}`, i * 10 + j));
      if (i % 9 === 0) rows.push(row(d, 5555, 99, "same", 0), row(d, 5555, 99, "same", 0)); // identical repeats
    }
    assert.ok(rows.length > 600);
    const b = bank(rows);
    const r = await walk(b.fetchWindow, "20251023", "20261022", opts());
    assert.deepEqual(multiset(r.transactions), multiset(rows));
    assert.equal(r.transactions.length, rows.length);
    assert.equal(r.retrieval.complete, true);
    assert.ok(b.calls.length >= Math.ceil(rows.length / 150));
    assert.ok(b.calls.every((c) => c.start === "20251023"));
  });

  test("single day with >= cap rows: warning, stop, coverage starts the next day", async () => {
    const rows = [row("20261010", 1, 1)];
    for (let i = 0; i < 6; i++) rows.push(row("20261005", 100 + i, i));
    rows.push(row("20261001", 3, 3));
    const b = bank(rows, { cap: 5 });
    const r = await walk(b.fetchWindow, "20260901", "20261022", opts({ cap: 5 }));
    assert.equal(r.retrieval.stopReason, "no_progress");
    assert.equal(r.retrieval.complete, false);
    assert.deepEqual(r.retrieval.partialDays, ["2026-10-05"]);
    assert.equal(r.retrieval.completeFrom, "2026-10-06");
    assert.match(r.retrieval.warnings.join(" "), /cannot be fully retrieved/);
    assert.equal(b.calls.length, 2);
  });

  test("bank ignores the end date: no-progress stop", async () => {
    const rows = [];
    for (let i = 1; i <= 9; i++) rows.push(row(`202610${String(i).padStart(2, "0")}`, i, i));
    const b = bank(rows, { cap: 5, ignoreEnd: true });
    const r = await walk(b.fetchWindow, "20260901", "20261022", opts({ cap: 5 }));
    assert.equal(r.retrieval.stopReason, "no_progress");
    assert.match(r.retrieval.warnings.join(" "), /ignored the window end/);
    assert.equal(r.retrieval.completeFrom, "2026-10-06");
    assert.equal(b.calls.length, 2);
  });

  test("max windows cap and pacing delay", async () => {
    const rows = [];
    for (let i = 1; i <= 28; i++) rows.push(row(`202610${String(i).padStart(2, "0")}`, i, i), row(`202610${String(i).padStart(2, "0")}`, i, i + 1));
    const b = bank(rows, { cap: 5 });
    const sleeps = [];
    const r = await walk(b.fetchWindow, "20261001", "20261028", { cap: 5, maxWindows: 3, delayMs: 7, sleep: async (ms) => sleeps.push(ms) });
    assert.equal(b.calls.length, 3);
    assert.deepEqual(sleeps, [7, 7]);
    assert.equal(r.retrieval.stopReason, "max_windows");
    assert.equal(r.retrieval.complete, false);
    assert.ok(r.retrieval.completeFrom > "2026-10-01");
  });
});
