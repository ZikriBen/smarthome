// E2E against the fake bank: session login + actions. No network (dead proxy + NOTFOUND resolver).
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readdirSync, rmSync } from "node:fs";
import { after, describe, test } from "node:test";
import path from "node:path";
import puppeteer from "puppeteer";
import { CompanyTypes, createScraper } from "israeli-bank-scrapers";

import { runAuto } from "../auto.mjs";
import { createLineReader, makeEmitter } from "../lib/protocol.mjs";
import { fakeBank } from "../lib/fake-bank.mjs";
import { mountType } from "../lib/summary.mjs";
import { Readable, PassThrough } from "node:stream";

const CHROME = [process.env.CHROMIUM_PATH, "/usr/bin/google-chrome", "/usr/bin/chromium"].find((p) => p && existsSync(p));
const TMPFS = [process.env.POC_TEST_TMPFS, "/dev/shm", "/tmp"].find((d) => d && existsSync(d) && mountType(d) === "tmpfs");
const OFFLINE = ["--proxy-server=http://127.0.0.1:9", "--host-resolver-rules=MAP * ~NOTFOUND"];
const made = [];
after(() => made.forEach((d) => rmSync(d, { recursive: true, force: true })));

function profileDir() {
  const d = mkdtempSync(path.join(TMPFS, "hauto-"));
  made.push(d);
  return d;
}

async function drive({ actions, otp = "246810", otpDelayMs = 0, scenario = { otp: true }, firstLineExtra = {} }) {
  const bank = fakeBank(scenario);
  const input = new PassThrough();
  const lines = createLineReader(input);
  const events = [];
  const emit = makeEmitter((s) => {
    const ev = JSON.parse(s);
    events.push(ev);
    if (ev.event === "otp_needed") {
      setTimeout(() => input.write(JSON.stringify({ otp }) + "\n"), otpDelayMs);
    }
  });
  const start = Promise.resolve().then(() => runAuto({
    lines, emit, puppeteer, CompanyTypes,
    createScraper: (o) => createScraper(o),
    executablePath: CHROME, profileDir: profileDir(),
    responder: bank.responder, extraLaunchArgs: OFFLINE,
    allowedHosts: [/^(?:[a-z0-9-]+\.)*bankhapoalim\.co\.il$/i],
    requireTmpfs: true, loginTimeoutMs: 15_000,
  }, { otpWaitMs: 10_000, loginTimeoutMs: 15_000, maskAccounts: true, defaultDays: 45, extraHosts: "" }));

  input.write(JSON.stringify({ userCode: "FAKEU", password: "fakepw", actions, ...firstLineExtra }) + "\n");
  const out = await start;
  lines.close();
  return { ...out, events, bank };
}

describe("e2e fake bank", () => {
  test("one login, checking_transactions, delayed OTP, bcodes blocked", async () => {
    const { report, files, bank } = await drive({
      actions: [{ name: "checking_transactions", from: "2026-09-01", to: "2026-10-22" }],
      otpDelayMs: 800,
    });
    assert.equal(report.status, "DONE");
    assert.equal(report.otpRequested, true);
    assert.equal(report.otpDelivered, true);
    assert.ok(report.blockedHosts.includes("bcodes.bankhapoalim.co.il"));
    assert.equal(bank.log.bcodesReached, undefined);
    assert.equal(report.transactionCount, 2);
    // CSV for out/ + archive_rows payload for the host archive (never written to out/).
    assert.equal(files.length, 2);
    const csv = files.find((f) => f.kind === "csv");
    const arch = files.find((f) => f.kind === "archive_rows");
    assert.match(csv.body, /סופר/);
    assert.ok(!csv.body.includes("FAKEU"));
    const payload = JSON.parse(arch.body);
    assert.equal(payload.from, "2026-09-01");
    assert.equal(payload.rows.length, 2);
    assert.ok(payload.accounts.length >= 1);
    assert.ok(!arch.body.includes("FAKEU"));
  });

  test("unknown action never logs in", async () => {
    const { report, events } = await drive({ actions: [{ name: "no_such_action" }] });
    assert.equal(report.status, "BAD_ACTIONS");
    assert.equal(events.filter((e) => e.event === "otp_needed").length, 0);
  });

  test("multiple actions share one login (two checking_transactions ranges)", async () => {
    const { report, files, bank } = await drive({
      actions: [
        { name: "checking_transactions", from: "2026-10-01", to: "2026-10-22" },
        { name: "checking_transactions", from: "2026-09-01", to: "2026-09-30" },
      ],
    });
    assert.equal(report.status, "DONE");
    assert.equal(report.actions.length, 2);
    assert.ok(report.actions.every((a) => a.ok));
    assert.equal(files.filter((f) => f.kind === "csv").length, 2);
    assert.equal(files.filter((f) => f.kind === "archive_rows").length, 2);
    // One login = one password submission on the fake bank.
    assert.equal(bank.log.logins.length, 1);
    assert.equal(bank.log.otpSubmissions.length, 1);
  });

  test("wrong OTP -> stopForToday", async () => {
    const { report } = await drive({
      actions: [{ name: "checking_transactions" }],
      otp: "000000",
      scenario: { otp: true, expectedCode: "246810" },
    });
    assert.equal(report.status, "OTP_REJECTED");
    assert.equal(report.stopForToday, true);
  });
});
