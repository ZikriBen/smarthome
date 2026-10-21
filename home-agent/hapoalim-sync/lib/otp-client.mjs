// otpCodeRetriever backed by the Command Center's one-code broker (a Unix socket mounted ONLY into this
// container). One request per run: the retriever refuses to be called twice. The code is never logged.
import net from "node:net";

export function brokerRequest(socketPath, message, timeoutMs) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(socketPath);
    let buffer = "";
    const timer = setTimeout(() => {
      socket.destroy();
      reject(Object.assign(new Error("broker timeout"), { otpErrorCode: "OTP_TIMEOUT" }));
    }, timeoutMs);
    socket.setEncoding("utf8");
    socket.on("connect", () => socket.write(`${JSON.stringify(message)}\n`));
    socket.on("data", (chunk) => {
      buffer += chunk;
      if (buffer.length > 4096) socket.destroy();
    });
    socket.on("error", () => {
      clearTimeout(timer);
      reject(Object.assign(new Error("broker unavailable"), { otpErrorCode: "OTP_BROKER_UNAVAILABLE" }));
    });
    socket.on("close", () => {
      clearTimeout(timer);
      try {
        resolve(JSON.parse(buffer.trim().split("\n")[0]));
      } catch {
        reject(Object.assign(new Error("broker bad reply"), { otpErrorCode: "OTP_BROKER_UNAVAILABLE" }));
      }
    });
    socket.on("end", () => socket.end());
  });
}

const BROKER_ERRORS = {
  OTP_TIMEOUT: "OTP_TIMEOUT",
  OTP_AMBIGUOUS: "OTP_AMBIGUOUS",
  ALREADY_USED: "OTP_ALREADY_USED",
  NOT_ARMED: "OTP_NOT_ARMED",
  BAD_REQUEST: "OTP_BROKER_UNAVAILABLE",
};

export function createOtpCodeRetriever({ socketPath, runId, since, waitSeconds = 150, onAsked } = {}) {
  let used = false;
  return async function otpCodeRetriever() {
    if (used) throw Object.assign(new Error("single attempt only"), { otpErrorCode: "OTP_ALREADY_USED" });
    used = true;
    if (onAsked) onAsked();
    const reply = await brokerRequest(
      socketPath,
      { op: "take_code", run_id: runId, since, wait: waitSeconds },
      (waitSeconds + 20) * 1000,
    );
    if (!reply || reply.ok !== true || typeof reply.code !== "string") {
      const code = BROKER_ERRORS[reply?.error] || "OTP_BROKER_UNAVAILABLE";
      throw Object.assign(new Error("no code"), { otpErrorCode: code });
    }
    return reply.code;
  };
}

export async function notifyOtpRequired(socketPath, runId) {
  try {
    await brokerRequest(socketPath, { op: "otp_required", run_id: runId }, 5000);
  } catch {
    // informational only
  }
}
