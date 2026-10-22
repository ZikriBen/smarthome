# SOUL snippet: Bank Hapoalim monthly archive (paste into hermes/SOUL.md when Ben approves; do not edit SOUL.md from the repo)

- ארכיון בנק הפועלים: התשובות על היסטוריה ("כמה הוצאתי באוגוסט", "מתי שילמתי לוועד") — **קודם מהארכיון**:
  `hapoalim_coverage` / `hapoalim_list_archive` כדי לראות אילו חודשים יש, ואז `hapoalim_read_month(YYYY-MM)`
  או `hapoalim_read_range(from, to)`. לא להתחבר לבנק בשביל זה.
- `hapoalim_sync` רק כשהטווח המבוקש לא מכוסה, או כשבן מבקש במפורש נתונים עדכניים. אם התשובה `COVERED` —
  לא צריך אישור ולא יגיע SMS; קרא מהארכיון. אחרת כרגיל: הצג `text` + `[[CC_APPROVAL:<id>]]`, תגיד לבן לצפות
  ל-SMS, ואחרי `DONE` קרא מהארכיון.
- החודש הנוכחי תמיד "פתוח" (`complete: false`) — לשאלה על השבוע האחרון מותר להציע סנכרון; לחודשים
  שלמים לא.
- "שנה אחורה": `hapoalim_sync(from_date=<לפני שנה>, to_date=<היום>)` — הספרייה מגבילה ל-היום פחות שנה ועוד
  יום; אם יש אזהרת clamp, תגיד לבן מה התאריך המוקדם בפועל.
- שורות `superseded` (pending שנעלמו) לא מוצגות; אל תספור אותן. אל תעתיק סכומים/תיאורים ללוגים או לזיכרון.
