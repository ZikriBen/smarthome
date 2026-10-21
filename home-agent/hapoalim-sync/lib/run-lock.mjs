// Lockout safety: at most one run per day (Asia/Jerusalem), never a run after an authentication failure, and
// never two runs at once. State lives in the scraper's own state dir (not readable by Hermes or the agents).
// Clearing a lockout is a manual step on the NUC (delete "lockout" from run-state.json) - never automatic.
import { closeSync, openSync, readFileSync, statSync, unlinkSync, writeSync } from "node:fs";
import { rename, writeFile } from "node:fs/promises";
import path from "node:path";

export const LOCKOUT_ERRORS = new Set([
  "INVALID_PASSWORD", "CHANGE_PASSWORD", "ACCOUNT_BLOCKED", "OTP_REJECTED", "OTP_NO_RESULT", "OTP_FORM_CHANGED",
]);
const STALE_LOCK_MS = 2 * 60 * 60 * 1000;

export function jerusalemDay(now = new Date()) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Jerusalem", year: "numeric", month: "2-digit", day: "2-digit" }).format(now);
}

export function readState(stateDir) {
  try {
    const data = JSON.parse(readFileSync(path.join(stateDir, "run-state.json"), "utf8"));
    return data && typeof data === "object" ? data : {};
  } catch {
    return {};
  }
}

export async function writeState(stateDir, state) {
  const file = path.join(stateDir, "run-state.json");
  const temporary = `${file}.tmp-${process.pid}`;
  await writeFile(temporary, `${JSON.stringify(state, null, 2)}\n`, { mode: 0o600 });
  await rename(temporary, file);
}

/** Throws {name: LOCKED_OUT|DAILY_LIMIT|RUN_IN_PROGRESS} or returns a release() for the run lock. */
export function acquireRun(stateDir, now = new Date()) {
  const state = readState(stateDir);
  if (state.lockout) throw Object.assign(new Error("locked out after an authentication failure"), { name: "LOCKED_OUT" });
  if (state.lastRunDay === jerusalemDay(now)) throw Object.assign(new Error("already ran today"), { name: "DAILY_LIMIT" });
  const lockFile = path.join(stateDir, "run.lock");
  try {
    const age = now.getTime() - statSync(lockFile).mtimeMs;
    if (age > STALE_LOCK_MS) unlinkSync(lockFile);
  } catch {
    // no lock file
  }
  let fd;
  try {
    fd = openSync(lockFile, "wx", 0o600);
  } catch {
    throw Object.assign(new Error("another run is in progress"), { name: "RUN_IN_PROGRESS" });
  }
  writeSync(fd, `${process.pid}\n`);
  closeSync(fd);
  return () => {
    try {
      unlinkSync(lockFile);
    } catch {
      // already gone
    }
  };
}

/** Record the attempt BEFORE touching the bank: a crash still counts as today's run. */
export async function markStarted(stateDir, now = new Date()) {
  const state = readState(stateDir);
  await writeState(stateDir, { ...state, lastRunDay: jerusalemDay(now), lastStartedAt: now.toISOString() });
}

export async function markFinished(stateDir, errorType, now = new Date()) {
  const state = readState(stateDir);
  const next = { ...state, lastFinishedAt: now.toISOString(), lastResult: errorType || "succeeded" };
  if (errorType && LOCKOUT_ERRORS.has(errorType)) next.lockout = { reason: errorType, at: now.toISOString() };
  await writeState(stateDir, next);
  return next;
}
