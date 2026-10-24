// Everything printed goes through here. Every secret value (user code, password, SMS code, passphrase) is
// replaced, every run of 4+ digits (account numbers, codes, IDs, amounts) is masked, URLs keep only the host.
export function makeRedactor(initial = []) {
  const values = [];
  const add = (value) => {
    if (typeof value === "string" && value.length >= 3 && !values.includes(value)) values.push(value);
  };
  initial.forEach(add);
  function redact(text) {
    let out = String(text ?? "");
    for (const value of values) out = out.split(value).join("[redacted]");
    out = out.replace(/https?:\/\/[^\s"')]+/g, (url) => {
      try {
        return new URL(url).host;
      } catch {
        return "[url]";
      }
    });
    out = out.replace(/[0-9]{4,}/g, (match) => "#".repeat(match.length));
    return out.slice(0, 300);
  }
  redact.add = add;
  redact.forget = () => { values.length = 0; };
  return redact;
}
