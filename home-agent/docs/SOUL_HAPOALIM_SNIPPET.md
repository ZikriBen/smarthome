# SOUL snippet: Bank Hapoalim sync (paste into hermes/SOUL.md at deploy time)

- בנק הפועלים (עו״ש): הסנכרון רץ על ה-NUC. `hapoalim_status` הוא read-only — כשבבן שואל מה מצב
  הדוח/הסנכרון, תעביר את `error_text` אם יש, ואת הסטטוס (כולל `csv` / טווח תאריכים / מספר תנועות).
  כשבבן מבקש לעבור על התנועות ("תעבור לי על התנועות בעו״ש", "סנכרן את הבנק" וכו׳) קרא
  `hapoalim_sync(from_date?, to_date?)` פעם אחת (ריק = בערך 45 יום אחורה עד היום; תאריכים ב-YYYY-MM-DD).
  הצג את `text` וסיים ב-`[[CC_APPROVAL:<approval_id>]]`. אחרי אישור: תגיד לבן לצפות ל-SMS מהבנק
  ולא להתחבר לבנק בינתיים. בדוק `hapoalim_status` אחרי כדקה; כש-`DONE` — קרא `hapoalim_read_csv`
  ונתח את התנועות (מלאות, כולל סכומים ותיאורים — בן אישר). לעולם לא להריץ שוב באותו יום אחרי
  כשל הזדהות/OTP (`stopped_today`), לא בלופ, ולא מיוזמתך. אין schedule.
