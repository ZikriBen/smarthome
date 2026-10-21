// sync.mjs as a process, with FAKE secrets and a browser that cannot start (/bin/false), so it can never reach
// the bank. Checks: no secret in stdout/stderr/last-run.json, file modes, daily limit, lockout, DEBUG refused,
// a missing RUN_ID refused, and that the code never asks for screenshots.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, test } from "node:test";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SYNC = path.join(HERE, "..", "sync.mjs");
const SECRETS = { user_code: "FAKEUSER1", password: "fake-Pa55word!" };

function sandbox() {
  const root = mkdtempSync(path.join(os.tmpdir(), "hps-"));
  const secret = path.join(root, "secret");
  mkdirSync(secret, { mode: 0o700 });
  for (const [k, v] of Object.entries(SECRETS)) writeFileSync(path.join(secret, k), v, { mode: 0o600 });
  spawnSync("age-keygen", ["-o", path.join(secret, "age.key")], { stdio: "ignore" });
  return { root, secret, state: path.join(root, "state"), reports: path.join(root, "reports") };
}

function run(box, extraEnv = {}) {
  const env = {
    PATH: process.env.PATH, HOME: box.root,
    REPORTS_DIRECTORY: box.reports, STATE_DIRECTORY: box.state, SECRET_DIRECTORY: box.secret,
    PROFILE_DIR: path.join(box.root, "tmpfs-profile"), OTP_BROKER_SOCKET: path.join(box.root, "none.sock"),
    CHROMIUM_PATH: "/bin/false", RUN_ID: "0123456789abcdef", APPROVED_AT: String(Math.floor(Date.now() / 1000)),
    REPORT_MONTH: "2026-09", ...extraEnv,
  };
  const r = spawnSync(process.execPath, [SYNC], { env, encoding: "utf8", timeout: 60000 });
  return { ...r, all: `${r.stdout}\n${r.stderr}` };
}

const noSecrets = (text) => {
  for (const v of Object.values(SECRETS)) assert.ok(!text.includes(v), "secret leaked");
  assert.ok(!text.includes("AGE-SECRET-KEY"), "age key leaked");
};

describe("sync.mjs guards (no network: the browser cannot start)", () => {
  test("a failed run leaks no secret, writes 0600 status, counts as today's run", () => {
    const box = sandbox();
    const r = run(box);
    assert.equal(r.status, 1);
    noSecrets(r.all);
    const statusFile = path.join(box.reports, "last-run.json");
    const status = JSON.parse(readFileSync(statusFile, "utf8"));
    noSecrets(JSON.stringify(status));
    assert.equal(status.status, "failed");
    assert.equal(statSync(statusFile).mode & 0o777, 0o600);
    assert.equal(statSync(box.state).mode & 0o777, 0o700);
    const again = run(box);
    assert.equal(JSON.parse(readFileSync(statusFile, "utf8")).errorType, "DAILY_LIMIT");
    noSecrets(again.all);
  });

  test("a lockout blocks the run before secrets are even read", () => {
    const box = sandbox();
    mkdirSync(box.state, { recursive: true });
    writeFileSync(path.join(box.state, "run-state.json"), JSON.stringify({ lockout: { reason: "INVALID_PASSWORD" } }));
    run(box);
    assert.equal(JSON.parse(readFileSync(path.join(box.reports, "last-run.json"), "utf8")).errorType, "LOCKED_OUT");
  });

  test("DEBUG is refused (library debug prints account numbers)", () => {
    const box = sandbox();
    const r = run(box, { DEBUG: "israeli-bank-scrapers:*" });
    assert.equal(r.status, 2);
    assert.match(r.stderr, /DEBUG_FORBIDDEN/);
  });

  test("a run without an approved RUN_ID is refused", () => {
    const box = sandbox();
    run(box, { RUN_ID: "" });
    const status = JSON.parse(readFileSync(path.join(box.reports, "last-run.json"), "utf8"));
    assert.equal(status.status, "failed");
    assert.match(status.error, /RUN_ID/);
  });

  test("the source never enables screenshots or verbose output", () => {
    const src = readFileSync(SYNC, "utf8").split("\n").filter((l) => !l.trim().startsWith("//")).join("\n");
    assert.ok(!/storeFailureScreenShotPath\s*:/.test(src));
    assert.ok(!/verbose\s*:\s*true/.test(src));
    assert.ok(!/console\.(log|error)\([^)]*(password|userCode|code\b)/.test(src));
  });
});
