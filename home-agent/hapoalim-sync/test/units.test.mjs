// Unit tests: run lock / lockout, redaction, the broker client (one request per run), egress allowlist,
// the encrypted profile vault. All offline.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, writeFileSync, mkdirSync, statSync } from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { describe, test } from "node:test";

import { hostAllowed, parseAllowlist } from "../lib/egress.mjs";
import { brokerRequest, createOtpCodeRetriever } from "../lib/otp-client.mjs";
import { restoreProfile, saveProfile } from "../lib/profile-vault.mjs";
import { makeRedactor } from "../lib/redact.mjs";
import { acquireRun, jerusalemDay, markFinished, markStarted, readState } from "../lib/run-lock.mjs";

const tmp = (p = "hp-") => mkdtempSync(path.join(os.tmpdir(), p));

describe("run lock", () => {
  test("one run per Jerusalem day", async () => {
    const dir = tmp();
    const now = new Date("2026-10-21T06:00:00Z");
    const release = acquireRun(dir, now);
    await markStarted(dir, now);
    release();
    assert.throws(() => acquireRun(dir, new Date("2026-10-21T20:00:00Z")), { name: "DAILY_LIMIT" });
    // 21:30Z on the 21st is already the 22nd in Jerusalem (UTC+3)
    assert.equal(jerusalemDay(new Date("2026-10-21T21:30:00Z")), "2026-10-22");
    acquireRun(dir, new Date("2026-10-21T21:30:00Z"))();
  });

  test("two concurrent runs are refused", () => {
    const dir = tmp();
    const release = acquireRun(dir);
    assert.throws(() => acquireRun(dir), { name: "RUN_IN_PROGRESS" });
    release();
  });

  for (const reason of ["INVALID_PASSWORD", "CHANGE_PASSWORD", "OTP_REJECTED", "OTP_NO_RESULT", "OTP_FORM_CHANGED"]) {
    test(`${reason} locks out until a manual reset`, async () => {
      const dir = tmp();
      await markFinished(dir, reason, new Date("2026-10-01T08:00:00Z"));
      assert.equal(readState(dir).lockout.reason, reason);
      assert.throws(() => acquireRun(dir, new Date("2026-11-30T08:00:00Z")), { name: "LOCKED_OUT" });
    });
  }

  for (const reason of ["OTP_TIMEOUT", "OTP_AMBIGUOUS", "TIMEOUT", "GENERAL_ERROR"]) {
    test(`${reason} does not lock out (next day allowed)`, async () => {
      const dir = tmp();
      await markStarted(dir, new Date("2026-10-01T08:00:00Z"));
      await markFinished(dir, reason, new Date("2026-10-01T08:01:00Z"));
      assert.equal(readState(dir).lockout, undefined);
      acquireRun(dir, new Date("2026-10-02T08:00:00Z"))();
    });
  }

  test("state file is 0600", async () => {
    const dir = tmp();
    await markStarted(dir);
    assert.equal(statSync(path.join(dir, "run-state.json")).mode & 0o777, 0o600);
  });
});

describe("redaction", () => {
  test("secrets, digit runs and URL paths never survive", () => {
    const r = makeRedactor(["FAKEUSER1", "fake-Pa55word!"]);
    const out = r("login FAKEUSER1/fake-Pa55word! failed at https://login.bankhapoalim.co.il/x?acct=12-600-987654 code 246810");
    assert.ok(!out.includes("FAKEUSER1") && !out.includes("fake-Pa55word!"));
    assert.ok(!/\d{4,}/.test(out));
    assert.ok(out.includes("login.bankhapoalim.co.il") && !out.includes("acct"));
  });
});

function fakeBroker(handler) {
  const dir = tmp("hpb-");
  const sock = path.join(dir, "broker.sock");
  const seen = [];
  const server = net.createServer((c) => {
    let buf = "";
    c.on("data", (d) => {
      buf += d;
      if (!buf.includes("\n")) return;
      const msg = JSON.parse(buf.split("\n")[0]);
      seen.push(msg);
      c.end(`${JSON.stringify(handler(msg))}\n`);
    });
  });
  return new Promise((resolve) => server.listen(sock, () => resolve({ sock, seen, close: () => server.close() })));
}

describe("broker client (otpCodeRetriever)", () => {
  test("returns the code; a second call in the same run is refused without contacting the broker", async () => {
    const b = await fakeBroker(() => ({ ok: true, code: "246810" }));
    const asked = [];
    const get = createOtpCodeRetriever({ socketPath: b.sock, runId: "0123456789abcdef", since: 100, waitSeconds: 1, onAsked: () => asked.push(1) });
    assert.equal(await get({ attempt: 1 }), "246810");
    await assert.rejects(get({ attempt: 2 }), (e) => e.otpErrorCode === "OTP_ALREADY_USED");
    assert.equal(b.seen.length, 1);
    assert.deepEqual(b.seen[0], { op: "take_code", run_id: "0123456789abcdef", since: 100, wait: 1 });
    assert.equal(asked.length, 1);
    b.close();
  });

  for (const [error, code] of [["OTP_TIMEOUT", "OTP_TIMEOUT"], ["OTP_AMBIGUOUS", "OTP_AMBIGUOUS"],
    ["NOT_ARMED", "OTP_NOT_ARMED"], ["ALREADY_USED", "OTP_ALREADY_USED"], ["weird", "OTP_BROKER_UNAVAILABLE"]]) {
    test(`broker error ${error} -> ${code}`, async () => {
      const b = await fakeBroker(() => ({ ok: false, error }));
      const get = createOtpCodeRetriever({ socketPath: b.sock, runId: "0123456789abcdef", since: 1, waitSeconds: 1 });
      await assert.rejects(get(), (e) => e.otpErrorCode === code);
      b.close();
    });
  }

  test("no socket -> OTP_BROKER_UNAVAILABLE", async () => {
    await assert.rejects(brokerRequest("/nonexistent/broker.sock", { op: "x" }, 1000),
      (e) => e.otpErrorCode === "OTP_BROKER_UNAVAILABLE");
  });
});

describe("egress allowlist", () => {
  test("only the bank's domains by default", () => {
    assert.ok(hostAllowed("login.bankhapoalim.co.il"));
    assert.ok(hostAllowed("bankhapoalim.co.il"));
    for (const h of ["bankhapoalim.co.il.evil.com", "evilbankhapoalim.co.il", "example.com", "bankhapoalim.com"]) {
      assert.ok(!hostAllowed(h), h);
    }
  });
  test("extra hosts are exact-suffix, junk ignored", () => {
    const rules = parseAllowlist("cdn.example.net, bad^host");
    assert.ok(hostAllowed("a.cdn.example.net", rules));
    assert.ok(!hostAllowed("cdn.example.net.evil", rules));
    assert.equal(rules.length, 2);
  });
});

const hasAge = spawnSync("age", ["--version"]).status === 0;
describe("encrypted profile vault", { skip: !hasAge && "age not installed" }, () => {
  test("round trip; ciphertext has no plaintext; caches and locks are not saved", async () => {
    const dir = tmp("hpv-");
    const key = path.join(dir, "age.key");
    spawnSync("age-keygen", ["-o", key], { stdio: "ignore" });
    const profile = path.join(dir, "profile");
    mkdirSync(path.join(profile, "Default", "Cache"), { recursive: true });
    writeFileSync(path.join(profile, "Default", "Local Storage-device-id"), "DEVICE-TRUST-MARKER-xyz");
    writeFileSync(path.join(profile, "Default", "Cache", "big"), "CACHE");
    writeFileSync(path.join(profile, "SingletonLock"), "lock");
    const enc = path.join(dir, "state", "profile.tar.age");
    mkdirSync(path.dirname(enc));
    await saveProfile({ encryptedPath: enc, identityPath: key, profileDir: profile });
    const blob = readFileSync(enc);
    assert.ok(blob.toString("latin1").startsWith("age-encryption.org/v1"));
    assert.ok(!blob.includes("DEVICE-TRUST-MARKER"));
    const restored = path.join(dir, "tmpfs-profile");
    assert.deepEqual(await restoreProfile({ encryptedPath: enc, identityPath: key, profileDir: restored }), { restored: true });
    assert.equal(readFileSync(path.join(restored, "Default", "Local Storage-device-id"), "utf8"), "DEVICE-TRUST-MARKER-xyz");
    assert.ok(!existsSync(path.join(restored, "Default", "Cache", "big")));
    assert.ok(!existsSync(path.join(restored, "SingletonLock")));
  });

  test("no saved profile yet -> a fresh one (first run = new device)", async () => {
    const dir = tmp("hpv-");
    const r = await restoreProfile({ encryptedPath: path.join(dir, "none.age"), identityPath: "/x", profileDir: path.join(dir, "p") });
    assert.deepEqual(r, { restored: false });
  });

  test("wrong key -> restore fails (no silent new device)", async () => {
    const dir = tmp("hpv-");
    const k1 = path.join(dir, "k1"); const k2 = path.join(dir, "k2");
    spawnSync("age-keygen", ["-o", k1], { stdio: "ignore" });
    spawnSync("age-keygen", ["-o", k2], { stdio: "ignore" });
    const p = path.join(dir, "p"); mkdirSync(p); writeFileSync(path.join(p, "f"), "x");
    const enc = path.join(dir, "e.age");
    await saveProfile({ encryptedPath: enc, identityPath: k1, profileDir: p });
    await assert.rejects(restoreProfile({ encryptedPath: enc, identityPath: k2, profileDir: path.join(dir, "q") }));
  });
});
