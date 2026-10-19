"""google_workspace calendar paths with a fake Google service (no network, no DB)."""
import os
import sys
import types
import unittest
from unittest import mock

os.environ.setdefault("COMMAND_CENTER_TOKEN", "test")
fake_approvals = types.ModuleType("command_center.approvals")
fake_approvals.created = []
fake_approvals.create = lambda action, payload: fake_approvals.created.append((action, payload)) or "a" * 18
fake_approvals.register_action = lambda *a: None
sys.modules["command_center.approvals"] = fake_approvals
import command_center  # noqa: E402
command_center.approvals = fake_approvals
from command_center import google_workspace as gw  # noqa: E402


class Call:
    def __init__(self, fn):
        self.fn, self.headers = fn, {}

    def execute(self):
        return self.fn()


class FakeEvents:
    def __init__(self, store):
        self.store, self.inserted, self.deleted, self.patched = store, [], [], []

    def get(self, calendarId, eventId):
        return Call(lambda: self.store[eventId])

    def insert(self, calendarId, body):
        self.inserted.append(body)
        return Call(lambda: dict(body, id="series1", etag="e1"))

    def delete(self, calendarId, eventId):
        self.deleted.append(eventId)
        return Call(lambda: None)

    def patch(self, calendarId, eventId, body):
        self.patched.append((eventId, body))
        return Call(lambda: dict(self.store[eventId], **body))


MASTER = {"id": "series1", "etag": "e1", "summary": "בריכה",
          "start": {"dateTime": "2026-10-23T08:00:00+03:00", "timeZone": "Asia/Jerusalem"},
          "end": {"dateTime": "2026-10-23T10:00:00+03:00", "timeZone": "Asia/Jerusalem"},
          "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO,FR;UNTIL=20270423T205959Z"]}
INSTANCE = {"id": "series1_20261109T060000Z", "etag": "e2", "summary": "בריכה",
            "recurringEventId": "series1",
            "start": {"dateTime": "2026-11-09T08:00:00+02:00", "timeZone": "Asia/Jerusalem"},
            "end": {"dateTime": "2026-11-09T10:00:00+02:00", "timeZone": "Asia/Jerusalem"}}


class SeriesTests(unittest.TestCase):
    def setUp(self):
        self.events = FakeEvents({"series1": MASTER, INSTANCE["id"]: INSTANCE})
        service = mock.Mock()
        service.events.return_value = self.events
        build = types.ModuleType("googleapiclient.discovery")
        build.build = lambda *a, **k: service
        self.patches = [mock.patch.dict(sys.modules, {"googleapiclient": types.ModuleType("googleapiclient"),
                                                     "googleapiclient.discovery": build}),
                        mock.patch.object(gw, "google_credentials", lambda: None)]
        for p in self.patches:
            p.start()
        fake_approvals.created.clear()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_one_proposal_then_one_insert_with_rrule_and_timezone(self):
        response = gw._calendar_create_response({
            "summary": "בריכה", "start": "2026-10-22T08:00:00+03:00", "end": "2026-10-22T10:00:00+03:00",
            "recurrence": {"freq": "WEEKLY", "by_weekday": ["MO", "FR"], "until": "2027-04-23"}})
        self.assertEqual(len(fake_approvals.created), 1)
        proposal = response["proposal"]
        self.assertEqual(proposal["start"], "2026-10-23T08:00:00+03:00")  # moved Thu -> Fri
        result = gw.calendar_create_event(proposal["summary"], proposal["start"], proposal["end"],
                                          "", proposal["recurrence"], proposal["time_zone"])
        body = self.events.inserted[0]
        self.assertEqual(body["recurrence"], ["RRULE:FREQ=WEEKLY;BYDAY=MO,FR;UNTIL=20270423T205959Z"])
        self.assertEqual(body["start"], {"dateTime": "2026-10-23T08:00:00+03:00", "timeZone": "Asia/Jerusalem"})
        self.assertEqual(result["series_id"], "series1")
        self.assertIn("53 times", result["rule"])

    def test_plain_event_unchanged(self):
        response = gw._calendar_create_response({
            "summary": "x", "start": "2026-11-01T08:00:00+02:00", "end": "2026-11-01T09:00:00+02:00"})
        self.assertNotIn("recurrence", response["proposal"])
        gw.calendar_create_event("x", "2026-11-01T08:00:00+02:00", "2026-11-01T09:00:00+02:00")
        self.assertNotIn("recurrence", self.events.inserted[0])
        self.assertNotIn("timeZone", self.events.inserted[0]["start"])

    def test_series_delete_from_instance_id_targets_master(self):
        proposal = gw._calendar_delete_response({"event_id": INSTANCE["id"], "scope": "series"})["proposal"]
        self.assertEqual(proposal["event_id"], "series1")
        gw.calendar_delete_event(proposal["event_id"], proposal["event"], proposal["version"])
        self.assertEqual(self.events.deleted, ["series1"])

    def test_single_instance_delete_stays_single(self):
        proposal = gw._calendar_delete_response({"event_id": INSTANCE["id"]})["proposal"]
        self.assertEqual(proposal["event_id"], INSTANCE["id"])

    def test_series_update_keeps_first_date_changes_time(self):
        proposal = gw._calendar_update_response({
            "event_id": INSTANCE["id"], "scope": "series", "summary": "בריכה",
            "start": "2026-11-09T09:00:00+02:00", "end": "2026-11-09T10:30:00+02:00"})["proposal"]
        self.assertEqual(proposal["after"]["start"], "2026-10-23T09:00:00+03:00")
        self.assertEqual(proposal["after"]["end"], "2026-10-23T10:30:00+03:00")

    def test_series_shorten_until(self):
        proposal = gw._calendar_update_response({
            "event_id": "series1", "scope": "series", "summary": "בריכה",
            "start": "2026-10-23T08:00:00+03:00", "end": "2026-10-23T10:00:00+03:00",
            "recurrence": {"freq": "WEEKLY", "by_weekday": ["MO", "FR"], "until": "2026-12-31"}})["proposal"]
        gw.calendar_update_event("series1", proposal["after"], proposal["before"], proposal["version"])
        self.assertEqual(self.events.patched[0][1]["recurrence"],
                         ["RRULE:FREQ=WEEKLY;BYDAY=MO,FR;UNTIL=20261231T215959Z"])

    def test_recurrence_on_single_instance_rejected(self):
        with self.assertRaises(ValueError):
            gw._calendar_update_response({
                "event_id": INSTANCE["id"], "summary": "x", "start": "2026-11-09T09:00:00+02:00",
                "end": "2026-11-09T10:00:00+02:00", "recurrence": {"freq": "DAILY", "count": 2}})

    def test_non_series_scope_series_rejected(self):
        self.events.store["one"] = {"id": "one", "etag": "x", "start": {"dateTime": "2026-11-01T08:00:00+02:00"},
                                    "end": {"dateTime": "2026-11-01T09:00:00+02:00"}}
        with self.assertRaises(ValueError):
            gw._calendar_delete_response({"event_id": "one", "scope": "series"})


if __name__ == "__main__":
    unittest.main()
