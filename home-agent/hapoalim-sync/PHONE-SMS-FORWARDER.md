# Phone SMS forwarder — add a Bank Hapoalim rule

Super-Hist already uses the **Forward SMS** app → `@<your_sms_relay_bot>` → Hermes `otp-relay` →
`hermes/otp/codes.sqlite`. Add a **second rule** next to it for the bank. Do not change the Super-Hist rule.

## New rule (Ben, on the phone)

1. Open **Forward SMS** (same app as the Super-Hist rule).
2. **Sender:** only the bank's SMS sender. Check one real bank SMS (alphanumeric like `Hapoalim` /
   `BankHapoalim`, or a short number). Never "all senders".
3. **Content filter** (if the app supports regex):
   ```
   ^(?!.*(העברה|העברת|להעברת|תשלום|מוטב|אישור פעולה|אישור עסקה|עסקה|כרטיס|הוראת קבע|הרשאה לחיוב)).*(כניסה|התחברות|הזדהות|אימות|הפועלים).*$
   ```
   If only "contains" is available: contains `הפועלים` or `כניסה`. The relay still rejects transfer/approval words.
4. **Template:** `#OTP hapoalim {msg}`
5. **Destination:** the same private chat with `@<your_sms_relay_bot>` as Super-Hist.

## What the relay does with it

* Only messages from `OTP_FORWARD_BOT_ID` in its private chat are considered.
* `#OTP hapoalim <raw SMS>` → extract one 5–6 digit code → store under site `hapoalim` in `codes.sqlite`.
* Super-Hist codes stay under site `super_hist` and are never returned for a Hapoalim run (and the reverse:
  a message that mentions Super-Hist/ההסתדרות is dropped for `hapoalim`).
* Transfer/payment/beneficiary SMSes match the reject list and are dropped (fail closed).
* The runner only accepts a code with `received >= requested_at` (the moment Ben asked), waits ≤ 120 s,
  reads once, then deletes every `hapoalim` row.

## Do not

* Send a real bank code just to test the rule — wait for the first approved sync.
* Forward "all SMS" or non-bank senders.
* Put bank codes into any chat other than the forward bot's private chat.
