// Everything that leaves the container (stdout/stderr, last-run.json) goes through here.
// Free text (library error messages) is scrubbed: every secret value and every run of 4+ digits
// (account numbers, codes, IDs) is masked. Structured status objects are built from an allowlist.

export function makeRedactor(secrets = []) {
  const values = secrets.filter((value) => typeof value === "string" && value.length >= 3);
  return function redact(text) {
    let out = String(text ?? "");
    for (const value of values) out = out.split(value).join("[redacted]");
    out = out.replace(/[0-9]{4,}/g, (match) => "#".repeat(match.length));
    out = out.replace(/https?:\/\/[^\s"')]+/g, (url) => {
      try {
        return new URL(url).host; // keep the host, drop path + query (they can carry account numbers)
      } catch {
        return "[url]";
      }
    });
    return out.slice(0, 300);
  };
}
