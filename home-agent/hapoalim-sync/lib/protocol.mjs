// Line protocol between the host runner (hapoalim-runner.py) and this one-shot container. stdin/stdout only:
// no mounts, no env secrets, no files.
//   runner -> container, line 1 : {"userCode": "...", "password": "..."}            (credentials, once)
//   container -> runner          : {"event": "otp_needed"}                           (bank showed the SMS form)
//   runner -> container, line 2 : {"otp": "123456"} | {"otp": null, "error": "OTP_TIMEOUT" | "OTP_AMBIGUOUS"}
//   container -> runner          : {"event": "result", "report": {...}, "csv": "..." | null}   (last line)
// Nothing else is ever written to stdout (console.* is silenced in auto.mjs). Secrets never appear in output.
import { createInterface } from "node:readline";

export const MAX_LINE = 4096;

export function createLineReader(input) {
  const rl = createInterface({ input, crlfDelay: Infinity, terminal: false });
  const queue = [];
  const waiters = [];
  let closed = false;
  rl.on("line", (line) => {
    const value = line.length > MAX_LINE ? null : line;
    const waiter = waiters.shift();
    if (waiter) waiter.resolve(value);
    else queue.push(value);
  });
  rl.on("close", () => {
    closed = true;
    while (waiters.length) waiters.shift().resolve(undefined);
  });
  return {
    /** next line (string), null for an over-long line, undefined on EOF/timeout. */
    next(timeoutMs) {
      if (queue.length) return Promise.resolve(queue.shift());
      if (closed) return Promise.resolve(undefined);
      return new Promise((resolve) => {
        const entry = { resolve: (v) => { clearTimeout(timer); resolve(v); } };
        const timer = setTimeout(() => {
          const i = waiters.indexOf(entry);
          if (i >= 0) waiters.splice(i, 1);
          resolve(undefined);
        }, timeoutMs);
        waiters.push(entry);
      });
    },
    close() { rl.close(); },
  };
}

export function parseJsonLine(line) {
  if (typeof line !== "string") return null;
  try {
    const v = JSON.parse(line);
    return v && typeof v === "object" && !Array.isArray(v) ? v : null;
  } catch {
    return null;
  }
}

export function makeEmitter(write) {
  return (obj) => write(`${JSON.stringify(obj)}\n`);
}
