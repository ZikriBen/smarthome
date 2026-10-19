"""Validate a recurrence request and build one RFC 5545 RRULE for Google Calendar.

Pure stdlib (no Google imports) so it is unit-testable without credentials.
"""

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIME_ZONE = "Asia/Jerusalem"
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
WEEKDAY_NAMES = {"MO": "Mon", "TU": "Tue", "WE": "Wed", "TH": "Thu", "FR": "Fri",
                 "SA": "Sat", "SU": "Sun"}
FREQS = ("DAILY", "WEEKLY", "MONTHLY")
UNITS = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year"}
MAX_COUNT = 730
MAX_SPAN_DAYS = 2 * 366
MAX_INTERVAL = 99
RAW_KEYS = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "BYMONTH",
            "BYSETPOS", "WKST"}
RAW_FREQS = FREQS + ("YEARLY",)
_BYDAY_TOKEN = re.compile(r"^([+-]?[1-5])?(MO|TU|WE|TH|FR|SA|SU)$")
_UNTIL_VALUE = re.compile(r"^\d{8}(T\d{6}Z)?$")


def time_zone(name):
    name = (name or DEFAULT_TIME_ZONE).strip()
    try:
        return name, ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("time_zone must be an IANA zone such as Asia/Jerusalem") from exc


def wants_recurrence(freq="", rrule=""):
    return bool((freq or "").strip() or (rrule or "").strip())


def _weekdays(values):
    if values in (None, "", []):
        return []
    if isinstance(values, str):
        values = re.split(r"[,\s]+", values.strip())
    days = []
    for value in values:
        token = str(value).strip().upper()[:2]
        if token not in WEEKDAYS:
            raise ValueError("by_weekday items must be MO, TU, WE, TH, FR, SA or SU")
        if token not in days:
            days.append(token)
    return sorted(days, key=WEEKDAYS.index)


def _parse_raw(rrule):
    text = rrule.strip()
    if text.upper().startswith("RRULE:"):
        text = text[6:]
    if not text or "\n" in text or len(text) > 300:
        raise ValueError("rrule must be a single RRULE line")
    parts = {}
    for item in text.upper().split(";"):
        key, sep, value = item.partition("=")
        if not sep or key not in RAW_KEYS or key in parts or not value:
            raise ValueError(f"unsupported or repeated RRULE part: {item}")
        parts[key] = value
    if parts.get("FREQ") not in RAW_FREQS:
        raise ValueError("RRULE FREQ must be DAILY, WEEKLY, MONTHLY or YEARLY")
    if ("COUNT" in parts) == ("UNTIL" in parts):
        raise ValueError("RRULE needs exactly one of COUNT or UNTIL (no endless series)")
    if "COUNT" in parts and not (parts["COUNT"].isdigit() and 1 <= int(parts["COUNT"]) <= MAX_COUNT):
        raise ValueError(f"RRULE COUNT must be 1..{MAX_COUNT}")
    if "UNTIL" in parts and not _UNTIL_VALUE.match(parts["UNTIL"]):
        raise ValueError("RRULE UNTIL must be YYYYMMDD or YYYYMMDDTHHMMSSZ")
    if "INTERVAL" in parts and not (parts["INTERVAL"].isdigit()
                                    and 1 <= int(parts["INTERVAL"]) <= MAX_INTERVAL):
        raise ValueError(f"RRULE INTERVAL must be 1..{MAX_INTERVAL}")
    if "BYDAY" in parts and not all(_BYDAY_TOKEN.match(t) for t in parts["BYDAY"].split(",")):
        raise ValueError("RRULE BYDAY is invalid")
    return parts


def _shift_to_weekday(start_day, days):
    """Days to move forward so the first occurrence is on one of `days` (0 if already)."""
    if not days:
        return 0
    wanted = {WEEKDAYS.index(d) for d in days}
    return next(delta for delta in range(7) if (start_day.weekday() + delta) % 7 in wanted)


def _add_months(day, months):
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    try:
        return day.replace(year=year, month=month)
    except ValueError:
        return None  # RFC 5545: an invalid date (e.g. 31 Feb) is skipped, not clamped


def occurrences(first, freq, interval, days, count=None, until=None, limit=MAX_COUNT + 1):
    """Expand the structured rules this module builds (local dates). Used for the preview/tests."""
    out = []
    if freq == "DAILY":
        step = 0
        while len(out) < limit:
            day = first + timedelta(days=step * interval)
            if until and day > until or count and len(out) >= count:
                break
            out.append(day)
            step += 1
    elif freq == "WEEKLY":
        wanted = [WEEKDAYS.index(d) for d in (days or [WEEKDAYS[first.weekday()]])]
        week_start = first - timedelta(days=first.weekday())  # WKST=MO (Google default)
        week = 0
        while len(out) < limit:
            base = week_start + timedelta(weeks=week * interval)
            if until and base > until or count and len(out) >= count:
                break
            for index in wanted:
                day = base + timedelta(days=index)
                if day < first or until and day > until or count and len(out) >= count:
                    continue
                out.append(day)
            week += 1
    else:  # MONTHLY on the start's day of month
        step = 0
        while len(out) < limit and step < 12 * 10:
            day = _add_months(first, step * interval)
            step += 1
            if day is None:
                continue
            if until and day > until or count and len(out) >= count:
                break
            out.append(day)
    return out


def build(payload, freq="", by_weekday=None, interval=1, until="", count=0, rrule="",
          tz_name=""):
    """Return a new event payload with a validated, possibly shifted start and a `recurrence` block.

    payload is the output of calendar_event_payload (summary/start/end/all_day).
    """
    tz_name, tz = time_zone(tz_name)
    all_day = payload["all_day"]
    if all_day:
        start_local = datetime.combine(date.fromisoformat(payload["start"]), time())
        end_local = datetime.combine(date.fromisoformat(payload["end"]), time())
    else:
        start_aware = datetime.fromisoformat(payload["start"].replace("Z", "+00:00"))
        end_aware = datetime.fromisoformat(payload["end"].replace("Z", "+00:00"))
        # Wall-clock times in the event zone: DST changes keep 08:00 as 08:00.
        start_local = start_aware.astimezone(tz).replace(tzinfo=None)
        end_local = end_aware.astimezone(tz).replace(tzinfo=None)

    raw = (rrule or "").strip()
    structured = bool((freq or "").strip())
    if raw and (structured or by_weekday or until or count or (interval or 1) != 1):
        raise ValueError("give either rrule or freq/by_weekday/interval/until/count, not both")
    if raw:
        parts = _parse_raw(raw)
        freq = parts["FREQ"]
        plain_days = [t for t in parts.get("BYDAY", "").split(",") if t in WEEKDAYS]
        days = plain_days if freq == "WEEKLY" and plain_days else []
        interval = int(parts.get("INTERVAL", 1))
    else:
        freq = (freq or "").strip().upper()
        if freq not in FREQS:
            raise ValueError("freq must be DAILY, WEEKLY or MONTHLY")
        try:
            interval = int(interval or 1)
            count = int(count or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("interval and count must be whole numbers") from exc
        if not 1 <= interval <= MAX_INTERVAL:
            raise ValueError(f"interval must be 1..{MAX_INTERVAL}")
        days = _weekdays(by_weekday)
        if days and freq != "WEEKLY":
            raise ValueError("by_weekday is only supported with freq=WEEKLY")
        if bool(until) == bool(count):
            raise ValueError("give exactly one of until (YYYY-MM-DD) or count (no endless series)")
        if count and not 1 <= count <= MAX_COUNT:
            raise ValueError(f"count must be 1..{MAX_COUNT}")

    # The first instance must itself match BYDAY, otherwise Google adds it as an extra instance.
    shift = _shift_to_weekday(start_local.date(), days)
    shifted_from = None
    if shift:
        shifted_from = payload["start"]
        start_local += timedelta(days=shift)
        end_local += timedelta(days=shift)
    first = start_local.date()

    if all_day:
        new_start, new_end = first.isoformat(), end_local.date().isoformat()
    else:
        new_start = start_local.replace(tzinfo=tz).isoformat()
        new_end = end_local.replace(tzinfo=tz).isoformat()

    if raw:
        rule = "RRULE:" + ";".join(f"{k}={v}" for k, v in parts.items())
        human = f"custom rule {rule[6:]} ({tz_name})"
        preview = None
    else:
        until_day = None
        if until:
            try:
                until_day = date.fromisoformat(str(until).strip()[:10])
            except ValueError as exc:
                raise ValueError("until must be a date YYYY-MM-DD") from exc
            if until_day < first:
                raise ValueError("until is before the first occurrence")
            if (until_day - first).days > MAX_SPAN_DAYS:
                raise ValueError("a series may span at most two years")
        rule_parts = [f"FREQ={freq}"]
        if interval != 1:
            rule_parts.append(f"INTERVAL={interval}")
        if days:
            rule_parts.append("BYDAY=" + ",".join(days))
        if until_day:
            if all_day:
                rule_parts.append("UNTIL=" + until_day.strftime("%Y%m%d"))
            else:  # inclusive: the end of `until` in the event zone, expressed in UTC
                last = datetime.combine(until_day, time(23, 59, 59), tz).astimezone(timezone.utc)
                rule_parts.append("UNTIL=" + last.strftime("%Y%m%dT%H%M%SZ"))
        else:
            rule_parts.append(f"COUNT={count}")
        rule = "RRULE:" + ";".join(rule_parts)
        dates = occurrences(first, freq, interval, days, count or None, until_day)
        if len(dates) > MAX_COUNT:
            raise ValueError(f"a series may have at most {MAX_COUNT} occurrences")
        every = UNITS[freq] if interval == 1 else f"{interval} {UNITS[freq]}s"
        on = (" on " + ", ".join(WEEKDAY_NAMES[d] for d in days)) if days else ""
        clock = "" if all_day else f" {start_local:%H:%M}-{end_local:%H:%M}"
        human = (f"every {every}{on}{clock} ({tz_name}), {len(dates)} times, "
                 f"{dates[0].isoformat()} to {dates[-1].isoformat()}")
        preview = {"occurrences": len(dates), "first": dates[0].isoformat(),
                   "last": dates[-1].isoformat()}

    result = dict(payload, start=new_start, end=new_end, time_zone=tz_name,
                  recurrence={"rrule": rule, "human": human})
    if preview:
        result["recurrence"].update(preview)
    if shifted_from:
        result["recurrence"]["shifted_from"] = shifted_from
    return result
