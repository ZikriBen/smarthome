# What Ben sets up himself, and the first live run

## A. Phone forwarding rule (Forward SMS app, next to the existing Super-Hist rule)

* **Sender:** the bank's SMS sender only ⚠️ (unknown yet — check one real bank SMS: likely an alphanumeric
  sender such as `Hapoalim` / `BankHapoalim`, or a short number). Never "all senders".
* **Content filter (regex, if the app supports it):**
  `^(?!.*(העברה|העברת|להעברת|תשלום|מוטב|אישור פעולה|אישור עסקה|עסקה|כרטיס|הוראת קבע|הרשאה לחיוב)).*(כניסה|התחברות|הזדהות).*$`
  (if only "contains" is supported: contains `כניסה`; the relay still rejects the transfer words).
* **Template:** `#OTP hapoalim {msg}` → the same chat with `@<your_sms_relay_bot>` as today.
* Send nothing to test it with a real code. To test the rule, wait for the first live run (D3).

## B. One-time host setup (after Ben approves the deploy; Ben types these himself on the NUC)

```sh
cd ~/smarthome/home-agent            # the reviewed feat/hapoalim-otp checkout
sudo ./hapoalim-sync/host/setup-host.sh     # users, dirs, image pinned by id, proxy, systemd units
sudo ./hapoalim-sync/host/setup-secrets.sh  # prompts (hidden) for user code + password; seals them
docker compose up -d command-center         # picks up the two new mounts (restart = Ben's call)
id ben                                      # ⚠️ if 'docker' or passwordless sudo: see DESIGN §7
```

`setup-secrets.sh` prints only `hapoalim.password: sealed, N bytes`. Values never appear on screen, in argv,
env, shell history, or a plaintext file. To change the password later: run it again.

## C. Dry checks (no bank traffic)

1. `docker run --rm --network none -e CHECK_ONLY=true "$(. /etc/hapoalim/image.env; echo $HAPOALIM_SYNC_IMAGE)"`
   → `{"ready":true,...,"patched":true}`.
2. `sudo ls -l /etc/credstore.encrypted/ /var/lib/hapoalim /var/lib/hapoalim-otp` → 0600/0700/0750, socket
   `root hapoalim-otp srw-rw----`.
3. Proxy allowlist: `docker run --rm --network hapoalim-egress curlimages/curl -sS -o /dev/null -w '%{http_code}\n' -x hapoalim-proxy:8888 https://example.com`
   → refused (403/000). (Optional: the same to `https://login.bankhapoalim.co.il/` → 200/302; that one is a
   single anonymous GET to the bank's public login page — only with Ben's OK.)
4. In Telegram: "מה מצב דוח הבנק?" → `hapoalim_status` → `can_run_today: true`, no lockout.

## D. First live run (Ben present, phone in hand) — stopping points

| step | what happens | STOP if |
|---|---|---|
| D1 | Ben: "תריץ את דוח הבנק לספטמבר" → approval card → Ben approves | anything but one card |
| D2 | One run starts (first run = new device: no profile yet) | status shows `SETUP_INCOMPLETE` / `INVALID_PASSWORD` (→ lockout; Ben checks the password on the bank site himself, then `reset-lockout.sh`; never retry the same day) |
| D3 | Bank sends the login SMS → phone rule → relay → broker → typed once | `OTP_TIMEOUT` (rule/wording didn't match: Ben checks the SMS text; tune the filter/`OTP_HAPOALIM_REQUIRE_RE` with `OTP_RELAY_DEBUG_SHAPE=1`; retry tomorrow). `OTP_FORM_CHANGED` (selectors wrong: lockout; we inspect markup offline, no retry) |
| D4 | Homepage, transactions, CSV on the NUC; profile saved encrypted | `OTP_REJECTED`/`OTP_NO_RESULT` (lockout; Ben checks the bank app for alerts before reset) |
| D5 | `hapoalim_status`: `succeeded`, count, `otpUsed:true`, `blockedHosts` | `blockedHosts` non-empty → review each host before adding to `host/egress/filter` |
| D6 | **Next day**, a second approved run: expect `otpRequested:false` (trust held, headless) | it asks for an SMS again → trust is not kept; stop and rethink (cookies/fingerprint) before more runs |

Never more than one run a day; never two runs in a row after any failure without looking at why.
