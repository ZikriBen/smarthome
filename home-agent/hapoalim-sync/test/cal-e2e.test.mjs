// E2E against the FAKE Cal: browser side via interception (offline Chromium), API side via an injected fetch.
// Synthetic data only. Asserts: one login, OTP only when prompted, auth-failure stop, egress guards, walk metadata.
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { after, describe, test } from "node:test";
import path from "node:path";
import { PassThrough } from "node:stream";
import puppeteer from "puppeteer";
import { CompanyTypes, createScraper } from "israeli-bank-scrapers";

import { runCalAuto } from "../cal-auto.mjs";
import { fakeCal } from "../lib/cal/fake-cal.mjs";
import { CAL_ALLOWED } from "../lib/cal/egress.mjs";
import { addMonths, monthIL } from "../lib/cal/months.mjs";
import { createLineReader, makeEmitter } from "../lib/protocol.mjs";
import { mountType } from "../lib/summary.mjs";

const CHROME = [process.env.CHROMIUM_PATH, "/usr/bin/google-chrome", "/usr/bin/chromium"].find((p) => p && existsSync(p));
const TMPFS = [process.env.POC_TEST_TMPFS, "/dev/shm", "/tmp"].find((d) => d && existsSync(d) && mountType(d) === "tmpfs");
const OFFLINE = ["--proxy-server=http://127.0.0.1:9", "--host-resolver-rules=MAP * ~NOTFOUND"];
const made = [];
after(() => made.forEach((d) => rmSync(d, { recursive: true, force: true })));
process.env.CAL_WALK_DELAY_MS = "0";

async function drive({ scenario = {}, actions, otp = "246810", optsExtra = {}, otpReply }) {
  const cal = fakeCal(scenario);
  const input = new PassThrough();
  const lines = createLineReader(input);
  const events = [];
  const emit = makeEmitter((s) => {
    const ev = JSON.parse(s);
    events.push(ev);
    if (ev.event === "otp_needed") setTimeout(() => input.write(JSON.stringify(otpReply || { otp }) + "\n"), 300);
  });
  const profileDir = mkdtempSync(path.join(TMPFS, "calauto-"));
  made.push(profileDir);
  const run = runCalAuto({
    lines, emit, puppeteer, CompanyTypes, createScraper: (o) => createScraper(o),
    executablePath: CHROME, profileDir, responder: cal.responder, fetchImpl: cal.fetch,
    extraLaunchArgs: OFFLINE, allowedHosts: CAL_ALLOWED, requireTmpfs: true,
  }, { otpWaitMs: 5_000, navTimeoutMs: 6_000, otpResultTimeoutMs: 4_000, otpEntry: true, extraHosts: "", defaultMonths: 3,
    ...optsExtra });
  input.write(JSON.stringify({ username: "FAKEUSER", password: "fakepw-xyz", actions }) + "\n");
  const out = await run;
  lines.close();
  return { ...out, events, cal };
}

const payloadOf = (files) => JSON.parse(files.find((f) => f.kind === "archive_rows").body);

describe("e2e fake Cal", { skip: !CHROME || !TMPFS ? "no chrome/tmpfs" : false }, () => {
  test("no OTP prompt: one login, multi-card walk, installments, identical rows, pending", async () => {
    const { report, files, events, cal } = await drive({ actions: [{ name: "card_transactions", from_month: "2026-02", to_month: "2026-04" }] });
    assert.equal(report.status, "DONE", JSON.stringify(report));
    assert.equal(report.otpRequested, false);
    assert.equal(events.filter((e) => e.event === "otp_needed").length, 0);
    assert.equal(cal.log.logins.length, 1);
    assert.equal(report.cardCount, 2);
    // default transport = page: every API call left from inside the browser page, none from Node
    assert.deepEqual(report.apiHosts, []);
    assert.deepEqual(report.blockedApiHosts, []);
    assert.ok(cal.log.api.every((c) => c.via === "page"), "default transport is in-page");
    assert.ok(cal.log.api.every((c) => c.authOk), "API calls carry the session token");
    // months requested newest first, per card sequentially
    const months = cal.log.api.filter((c) => c.path.endsWith("getCardTransactionsDetails")).map((c) => `${c.card}:${c.year}-${c.month}`);
    assert.deepEqual(months, ["U-A:2026-4", "U-A:2026-3", "U-A:2026-2", "U-B:2026-4", "U-B:2026-3", "U-B:2026-2"]);
    const p = payloadOf(files);
    assert.deepEqual(p.cards.sort(), ["1111", "2222"]);
    assert.equal(p.retrieval["1111"].complete, true);
    assert.deepEqual(p.retrieval["1111"].months.map((m) => m.month), ["2026-04", "2026-03", "2026-02"]);
    const inst = p.rows.filter((r) => r.installment_total === 3);
    assert.equal(inst.length, 3);
    assert.deepEqual(inst.map((r) => r.installment_number).sort(), [1, 2, 3]);
    assert.ok(inst.every((r) => r.purchase_date === "2026-01-15"), "purchase_date is the original purchase day");
    assert.deepEqual(inst.map((r) => r.billing_month).sort(), ["2026-02", "2026-03", "2026-04"]);
    const dups = p.rows.filter((r) => r.card === "2222" && r.billing_month === "2026-03" && /-dup$/.test(r.identifier));
    assert.equal(dups.length, 2, "identical rows both kept");
    const pending = p.rows.filter((r) => r.status === "pending");
    assert.equal(pending.length, 1);
    assert.equal(pending[0].billing_month, null);
    assert.equal(pending[0].charge_date, null);
    const one = p.rows.find((r) => r.card === "1111" && r.billing_month === "2026-03" && r.identifier.endsWith("-1"));
    assert.equal(one.charge_date, "2026-03-10");
    assert.equal(one.purchase_date, "2026-02-04");
    const all = JSON.stringify({ report, files });
    assert.ok(!all.includes("FAKEUSER") && !all.includes("fakepw-xyz"));
    assert.ok(!JSON.stringify(report).includes("סוחר"), "no descriptions on the report");
    assert.equal(report.profileWiped, true);
  });

  test("OTP prompted: one code relayed, one submit, success", async () => {
    const { report, events, cal } = await drive({ scenario: { otp: true },
      actions: [{ name: "card_transactions", from_month: addMonths(monthIL(), -1), to_month: monthIL() }] });
    assert.equal(report.status, "DONE", JSON.stringify(report));
    assert.equal(report.otpRequested, true);
    assert.equal(report.otpDelivered, true);
    assert.equal(events.filter((e) => e.event === "otp_needed").length, 1);
    assert.deepEqual(cal.log.otpSubmissions, ["246810"]);
    assert.equal(cal.log.logins.length, 1);
  });

  test("wrong OTP -> OTP_REJECTED, stop for today, one submission", async () => {
    const { report, cal } = await drive({ scenario: { otp: true, expectedCode: "000000" } });
    assert.equal(report.status, "OTP_REJECTED");
    assert.equal(report.stopForToday, true);
    assert.equal(cal.log.otpSubmissions.length, 1);
    assert.equal(cal.log.api.length, 0);
  });

  test("OTP not relayed -> OTP_TIMEOUT stop", async () => {
    const { report, cal } = await drive({ scenario: { otp: true }, otpReply: { otp: null, error: "OTP_TIMEOUT" } });
    assert.equal(report.status, "OTP_TIMEOUT");
    assert.equal(report.stopForToday, true);
    assert.equal(cal.log.otpSubmissions.length, 0);
  });

  test("CAL_OTP_ENTRY=0 -> OTP_REQUIRED without asking", async () => {
    const { report, events } = await drive({ scenario: { otp: true }, optsExtra: { otpEntry: false } });
    assert.equal(report.status, "OTP_REQUIRED");
    assert.equal(report.stopForToday, true);
    assert.equal(events.filter((e) => e.event === "otp_needed").length, 0);
  });

  test("wrong password -> INVALID_PASSWORD, stop for today, exactly one login, no API calls", async () => {
    const { report, cal } = await drive({ scenario: { wrongPassword: true } });
    assert.equal(report.status, "INVALID_PASSWORD", JSON.stringify(report));
    assert.equal(report.stopForToday, true);
    assert.equal(cal.log.logins.length, 1);
    assert.equal(cal.log.api.length, 0);
  });

  test("wrong password with a captcha script on the page is still INVALID_PASSWORD", async () => {
    const { report } = await drive({ scenario: { wrongPassword: true, captchaScript: true } });
    assert.equal(report.status, "INVALID_PASSWORD");
  });

  test("change password -> CHANGE_PASSWORD stop", async () => {
    const { report } = await drive({ scenario: { changePassword: true } });
    assert.equal(report.status, "CHANGE_PASSWORD");
    assert.equal(report.stopForToday, true);
  });

  test("captcha -> CAPTCHA_OR_BLOCK stop", async () => {
    const { report } = await drive({ scenario: { captcha: true } });
    assert.equal(report.status, "CAPTCHA_OR_BLOCK");
    assert.equal(report.stopForToday, true);
  });

  test("walk stops at the first failing month; newer months kept, not complete", async () => {
    const { report, files } = await drive({ scenario: { failMonth: "2026-07" },
      actions: [{ name: "card_transactions", from_month: "2026-05", to_month: "2026-09" }] });
    assert.equal(report.status, "DONE");
    const p = payloadOf(files);
    assert.deepEqual(p.retrieval["1111"].months.map((m) => m.month), ["2026-09", "2026-08"]);
    assert.equal(p.retrieval["1111"].complete, false);
    assert.equal(p.retrieval["1111"].stopMonth, "2026-07");
    assert.equal(p.retrieval["1111"].stopReason, "status");
    assert.ok(!p.rows.some((r) => r.billing_month && r.billing_month < "2026-08"));
  });

  test("cap reached -> month flagged capped", async () => {
    process.env.CAL_MONTH_CAP = "5";
    try {
      const { files, report } = await drive({ scenario: { rowsPerMonth: 5 },
        actions: [{ name: "card_transactions", from_month: "2026-09", to_month: "2026-09" }] });
      const p = payloadOf(files);
      assert.equal(p.retrieval["1111"].months[0].capped, true);
      assert.ok(report.actions[0].warnings.some((w) => /cap/.test(w)));
    } finally {
      delete process.env.CAL_MONTH_CAP;
    }
  });

  test("429 mid-walk -> blocked, stop for today, earlier months kept", async () => {
    const { report, files } = await drive({ scenario: { blockedMonth: "2026-08" },
      actions: [{ name: "card_transactions", from_month: "2026-07", to_month: "2026-09" }] });
    assert.equal(report.blocked, true);
    assert.equal(report.stopForToday, true);
    const p = payloadOf(files);
    assert.equal(p.retrieval["1111"].stopReason, "blocked");
    assert.deepEqual(p.retrieval["1111"].months.map((m) => m.month), ["2026-09"]);
  });

  const withTransport = async (t, fn) => {
    process.env.CAL_API_TRANSPORT = t;
    try { return await fn(); } finally { delete process.env.CAL_API_TRANSPORT; }
  };

  test("CAL_API_TRANSPORT=node: 6.9.0 path, all calls from Node through the guard", async () => {
    const { report, files, cal } = await withTransport("node", () =>
      drive({ actions: [{ name: "card_transactions", from_month: "2026-02", to_month: "2026-04" }] }));
    assert.equal(report.status, "DONE", JSON.stringify(report));
    assert.deepEqual(report.apiHosts, ["api.cal-online.co.il"]);
    assert.ok(cal.log.api.every((c) => c.via === "node"));
    const p = payloadOf(files);
    assert.equal(p.retrieval["1111"].transport, "node");
    assert.equal(p.retrieval["1111"].complete, true);
    assert.equal(p.rows.filter((r) => r.installment_total === 3).length, 3);
  });

  test("HTML 'Request Rejected' from the API (node transport) -> blocked, stop for today", async () => {
    const { report, files } = await withTransport("node", () => drive({ scenario: { htmlMonth: "2026-08" },
      actions: [{ name: "card_transactions", from_month: "2026-07", to_month: "2026-09" }] }));
    assert.equal(report.blocked, true);
    assert.equal(report.stopForToday, true);
    const p = payloadOf(files);
    assert.equal(p.retrieval["1111"].stopReason, "blocked");
    assert.equal(p.retrieval["1111"].transport, "node");
    assert.deepEqual(p.retrieval["1111"].months.map((m) => m.month), ["2026-09"]);
    assert.ok(!JSON.stringify(report).includes("Request Rejected"), "no response bytes in the report");
  });

  test("CAL_API_TRANSPORT=page (explicit): frames, pending and months all sent from inside the page", async () => {
    const { report, files, cal } = await withTransport("page", () =>
      drive({ actions: [{ name: "card_transactions", from_month: "2026-02", to_month: "2026-04" }] }));
    assert.equal(report.status, "DONE", JSON.stringify(report));
    assert.ok(cal.log.api.length >= 8);
    assert.ok(cal.log.api.every((c) => c.via === "page"), JSON.stringify(cal.log.api.map((c) => c.via)));
    assert.ok(cal.log.api.every((c) => c.authOk));
    assert.ok(cal.log.api.some((c) => c.path.endsWith("GetFrameStatus")));
    assert.deepEqual(report.blockedApiHosts, []);
    const p = payloadOf(files);
    assert.equal(p.retrieval["1111"].transport, "page");
    assert.equal(p.retrieval["1111"].complete, true);
    assert.equal(p.rows.filter((r) => r.installment_total === 3).length, 3);
    assert.equal(p.rows.filter((r) => r.status === "pending").length, 1);
  });

  test("CAL_API_TRANSPORT=page: 429 and HTML in the page -> blocked", async () => {
    for (const scenario of [{ blockedMonth: "2026-08" }, { htmlMonth: "2026-08" }]) {
      const { report, files } = await withTransport("page", () =>
        drive({ scenario, actions: [{ name: "card_transactions", from_month: "2026-07", to_month: "2026-09" }] }));
      assert.equal(report.blocked, true, JSON.stringify(scenario));
      assert.equal(report.stopForToday, true);
      const p = payloadOf(files);
      assert.equal(p.retrieval["1111"].stopReason, "blocked");
      assert.deepEqual(p.retrieval["1111"].months.map((m) => m.month), ["2026-09"]);
    }
  });

  test("CAL_API_TRANSPORT=page: no response in the page (CORS) -> one Node retry per call, through the Node guard", async () => {
    const { report, files, cal } = await withTransport("page", () =>
      drive({ scenario: { pageCorsFail: true }, actions: [{ name: "card_transactions", from_month: "2026-03", to_month: "2026-04" }] }));
    assert.equal(report.status, "DONE", JSON.stringify(report));
    const viaNode = cal.log.api.filter((c) => c.via === "node");
    assert.ok(viaNode.length >= 6, "frames + pending + 2 months per card retried from Node");
    assert.deepEqual(report.apiHosts, ["api.cal-online.co.il"]);
    assert.equal(payloadOf(files).retrieval["2222"].complete, true);
  });

  test("unknown action never logs in", async () => {
    const { report, cal } = await drive({ actions: [{ name: "checking_transactions" }] });
    assert.equal(report.status, "BAD_ACTIONS");
    assert.equal(cal.log.logins.length, 0);
  });
});
