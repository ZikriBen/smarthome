import unittest
from datetime import date

from command_center import calendar_recurrence as cr


def timed(start, end):
    return {"summary": "בריכה", "start": start, "end": end, "location": "", "all_day": False}


class RecurrenceTests(unittest.TestCase):
    def test_mon_fri_half_year_one_rule(self):
        p = cr.build(timed("2026-10-23T08:00:00+03:00", "2026-10-23T10:00:00+03:00"),
                     freq="WEEKLY", by_weekday=["MO", "FR"], until="2027-04-23")
        r = p["recurrence"]
        # 23:59:59 IDT (+03:00) on 2027-04-23 == 20:59:59Z
        self.assertEqual(r["rrule"], "RRULE:FREQ=WEEKLY;BYDAY=MO,FR;UNTIL=20270423T205959Z")
        self.assertEqual(p["time_zone"], "Asia/Jerusalem")
        self.assertEqual(r["first"], "2026-10-23")
        self.assertEqual(r["last"], "2027-04-23")
        self.assertEqual(r["occurrences"], 53)
        self.assertNotIn("shifted_from", r)

    def test_start_moves_to_first_matching_weekday_keeping_wall_clock_over_dst(self):
        # Thu 2026-10-22 is not MO/FR; next match is Fri 23; DST ends Sun 25 -> later dates +02:00
        p = cr.build(timed("2026-10-22T08:00:00+03:00", "2026-10-22T09:00:00+03:00"),
                     freq="WEEKLY", by_weekday="FR", count=3)
        self.assertEqual(p["start"], "2026-10-23T08:00:00+03:00")
        self.assertEqual(p["recurrence"]["shifted_from"], "2026-10-22T08:00:00+03:00")
        p = cr.build(timed("2026-10-24T08:00:00+03:00", "2026-10-24T09:00:00+03:00"),
                     freq="WEEKLY", by_weekday=["MO"], count=2)
        self.assertEqual(p["start"], "2026-10-26T08:00:00+02:00")  # 08:00 local after DST
        self.assertEqual(p["end"], "2026-10-26T09:00:00+02:00")

    def test_utc_input_is_converted_to_local_wall_clock(self):
        p = cr.build(timed("2026-11-02T06:00:00Z", "2026-11-02T07:00:00Z"),
                     freq="DAILY", count=5)
        self.assertEqual(p["start"], "2026-11-02T08:00:00+02:00")
        self.assertEqual(p["recurrence"]["rrule"], "RRULE:FREQ=DAILY;COUNT=5")

    def test_all_day_until_is_a_date(self):
        p = cr.build({"summary": "x", "start": "2026-11-01", "end": "2026-11-02",
                      "location": "", "all_day": True},
                     freq="MONTHLY", until="2027-03-01")
        self.assertEqual(p["recurrence"]["rrule"], "RRULE:FREQ=MONTHLY;UNTIL=20270301")
        self.assertEqual(p["recurrence"]["occurrences"], 5)

    def test_interval_and_human(self):
        p = cr.build(timed("2026-11-02T18:00:00+02:00", "2026-11-02T19:00:00+02:00"),
                     freq="weekly", by_weekday=["mo"], interval=2, count=4)
        self.assertEqual(p["recurrence"]["rrule"], "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;COUNT=4")
        self.assertIn("every 2 weeks on Mon 18:00-19:00 (Asia/Jerusalem), 4 times", p["recurrence"]["human"])
        self.assertEqual(p["recurrence"]["last"], "2026-12-14")

    def test_monthly_skips_missing_days(self):
        days = cr.occurrences(date(2027, 1, 31), "MONTHLY", 1, [], count=3)
        self.assertEqual(days, [date(2027, 1, 31), date(2027, 3, 31), date(2027, 5, 31)])

    def test_raw_rrule_passthrough(self):
        p = cr.build(timed("2026-11-03T08:00:00+02:00", "2026-11-03T09:00:00+02:00"),
                     rrule="rrule:freq=monthly;byday=1TU;count=6")
        self.assertEqual(p["recurrence"]["rrule"], "RRULE:FREQ=MONTHLY;BYDAY=1TU;COUNT=6")

    def test_rejections(self):
        base = timed("2026-11-02T08:00:00+02:00", "2026-11-02T09:00:00+02:00")
        bad = [dict(freq="WEEKLY"),                                     # endless
               dict(freq="WEEKLY", count=3, until="2027-01-01"),        # both
               dict(freq="HOURLY", count=3),
               dict(freq="DAILY", by_weekday=["MO"], count=3),
               dict(freq="WEEKLY", by_weekday=["XX"], count=3),
               dict(freq="WEEKLY", count=731),
               dict(freq="DAILY", until="2029-01-01"),                  # > 2 years
               dict(freq="DAILY", until="2026-01-01"),                  # before start
               dict(freq="DAILY", count=3, tz_name="Mars/Base"),
               dict(rrule="FREQ=WEEKLY;BYDAY=MO"),                      # endless raw
               dict(rrule="FREQ=WEEKLY;COUNT=3;EXDATE=20261109"),
               dict(rrule="FREQ=SECONDLY;COUNT=3"),
               dict(rrule="FREQ=WEEKLY;COUNT=3", freq="WEEKLY")]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                cr.build(base, **kwargs)


if __name__ == "__main__":
    unittest.main()
