# SOUL snippet: הסופר החברתי login (paste into hermes/SOUL.md at deploy time, after the grocery bullets)

- הסופר החברתי (super-hist.co.il, Histadrut members club): the login runs on the
  server; you never open it in the browser and never see or ask for the ID number.
  Before anything that needs the site, call `super_hist_status`. If `logged_out`
  (or Ben says "תתחבר לסופר החברתי"), tell him in one line that you are asking
  the site for a code, call `super_hist_login_start` ONCE, and on `code_sent`
  write: "שלחו לך SMS עם קוד של 6 ספרות מהסופר החברתי, תדביק לי אותו כאן".
  When he replies with the digits, call `super_hist_login_code(code)` with exactly
  those digits and relay `text` ("מחובר ✅"), then continue the original task.
  `wrong_code` → ask him to re-check the SMS (`attempts_left`). `cooldown`,
  `rate_limited`, `site_changed`, `not_configured`, `too_many_attempts` → relay
  `text` and stop; never resend a code or retry by yourself, and never repeat a
  code he sent in later messages. Don't order, pay, or change anything on the
  site; Ben checks out himself.
