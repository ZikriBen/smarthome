"""Google OAuth, Calendar, and Gmail capabilities."""

import base64
import os
import re
import secrets
import threading
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import approvals
from .config import GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI, GOOGLE_SCOPES, GOOGLE_TOKEN

_auth_lock = threading.Lock()
_pending_state = None
_pending_code_verifier = None


def google_flow():
    from google_auth_oauthlib.flow import Flow
    if not GOOGLE_CLIENT_SECRET.is_file():
        raise ValueError("Google OAuth client is not configured")
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    return Flow.from_client_secrets_file(
        str(GOOGLE_CLIENT_SECRET), scopes=GOOGLE_SCOPES, redirect_uri=GOOGLE_REDIRECT_URI)


def save_google_token(credentials):
    descriptor = os.open(GOOGLE_TOKEN, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
        os.fchmod(descriptor, 0o600)
        token_file.write(credentials.to_json())


def google_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    if not GOOGLE_TOKEN.is_file():
        raise ValueError("Google is not connected; complete the one-time authorization first")
    credentials = Credentials.from_authorized_user_file(str(GOOGLE_TOKEN), GOOGLE_SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        save_google_token(credentials)
    if not credentials.valid:
        raise ValueError("Google authorization expired; complete authorization again")
    return credentials


def google_status():
    return {"client_configured": GOOGLE_CLIENT_SECRET.is_file(),
            "authorized": GOOGLE_TOKEN.is_file(),
            "scopes": ["calendar.readonly", "calendar.events.owned", "gmail.readonly",
                       "gmail.send"]}


def google_auth_start():
    global _pending_state, _pending_code_verifier
    flow = google_flow()
    flow.code_verifier = secrets.token_urlsafe(64)
    authorization_url, state = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true")
    with _auth_lock:
        _pending_state = state
        _pending_code_verifier = flow.code_verifier
    return {"authorization_url": authorization_url, "redirect_uri": GOOGLE_REDIRECT_URI}


def google_calendars():
    from googleapiclient.discovery import build
    service = build("calendar", "v3", credentials=google_credentials(), cache_discovery=False)
    response = service.calendarList().list(maxResults=250, showHidden=False).execute()
    calendars = [{"id": item.get("id"),
                  "name": item.get("summaryOverride") or item.get("summary"),
                  "primary": bool(item.get("primary")), "access_role": item.get("accessRole")}
                 for item in response.get("items", []) if not item.get("hidden")]
    return {"calendars": calendars}


def google_calendar_events(days=7, max_results=25):
    from googleapiclient.discovery import build
    days = max(1, min(int(days), 366))
    max_results = max(1, min(int(max_results), 100))
    now = datetime.now(timezone.utc)
    service = build("calendar", "v3", credentials=google_credentials(), cache_discovery=False)
    calendars = google_calendars()["calendars"]
    events = []
    for calendar in calendars:
        if not calendar["id"]:
            continue
        response = service.events().list(
            calendarId=calendar["id"], timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=days)).isoformat(), singleEvents=True,
            orderBy="startTime", maxResults=max_results).execute()
        for event in response.get("items", []):
            events.append({"event_id": event.get("id"),
                "title": event.get("summary", "(untitled)"),
                "start": event.get("start", {}).get("dateTime", event.get("start", {}).get("date")),
                "end": event.get("end", {}).get("dateTime", event.get("end", {}).get("date")),
                "location": event.get("location"), "calendar": calendar["name"],
                "calendar_id": calendar["id"], "primary": calendar["primary"],
                "all_day": "date" in event.get("start", {})})
    events.sort(key=lambda event: event.get("start") or "")
    return {"days": days, "calendars_checked": [item["name"] for item in calendars],
            "events": events[:max_results]}


def calendar_event_payload(summary, start, end, location=""):
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 500:
        raise ValueError("an event summary is required")
    if not isinstance(start, str) or not isinstance(end, str):
        raise ValueError("event start and end must be ISO 8601 dates or datetimes with timezone")
    start_is_date = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", start))
    end_is_date = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", end))
    if start_is_date != end_is_date:
        raise ValueError("event start and end must both be dates or both be datetimes")
    try:
        if start_is_date:
            start_at, end_at = date.fromisoformat(start), date.fromisoformat(end)
        else:
            start_at = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_at = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            "event start and end must be ISO 8601 dates or datetimes with timezone") from exc
    if not start_is_date and (start_at.tzinfo is None or end_at.tzinfo is None):
        raise ValueError("event start and end datetimes must include timezone")
    if end_at <= start_at:
        raise ValueError("event end must be after start")
    if not isinstance(location, str) or len(location) > 1000:
        raise ValueError("event location is invalid")
    return {"summary": summary.strip(), "start": start, "end": end,
            "location": location.strip(), "all_day": start_is_date}


def calendar_event_times(payload):
    key = "date" if payload.get("all_day") else "dateTime"
    return {"start": {key: payload["start"]}, "end": {key: payload["end"]}}


def calendar_event_snapshot(event):
    return {"event_id": event.get("id"), "summary": event.get("summary", "(untitled)"),
            "start": event.get("start", {}).get("dateTime", event.get("start", {}).get("date")),
            "end": event.get("end", {}).get("dateTime", event.get("end", {}).get("date")),
            "location": event.get("location") or "", "all_day": "date" in event.get("start", {})}


def calendar_create_event(summary, start, end, location=""):
    payload = calendar_event_payload(summary, start, end, location)
    from googleapiclient.discovery import build
    service = build("calendar", "v3", credentials=google_credentials(), cache_discovery=False)
    event = service.events().insert(calendarId="primary", body={
        "summary": payload["summary"], **calendar_event_times(payload),
        **({"location": payload["location"]} if payload["location"] else {})}).execute()
    return {"created": calendar_event_snapshot(event)}


def calendar_event_id(event_id):
    if (not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 1024
            or any(character.isspace() for character in event_id)):
        raise ValueError("a valid primary-calendar event ID is required")
    return event_id.strip()


def calendar_primary_event(event_id):
    from googleapiclient.discovery import build
    event_id = calendar_event_id(event_id)
    service = build("calendar", "v3", credentials=google_credentials(), cache_discovery=False)
    return service, service.events().get(calendarId="primary", eventId=event_id).execute()


def calendar_update_proposal(event_id, summary, start, end, location=""):
    _, event = calendar_primary_event(event_id)
    before = calendar_event_snapshot(event)
    after = calendar_event_payload(summary, start, end, location)
    return {"event_id": before["event_id"], "version": event.get("etag"),
            "before": before, "after": after}


def calendar_delete_proposal(event_id):
    _, event = calendar_primary_event(event_id)
    return {"event_id": event["id"], "version": event.get("etag"),
            "event": calendar_event_snapshot(event)}


def calendar_update_event(event_id, after, expected, version):
    service, current = calendar_primary_event(event_id)
    if calendar_event_snapshot(current) != expected or current.get("etag") != version:
        raise ValueError("calendar event changed after approval was requested; review it and try again")
    payload = calendar_event_payload(
        after["summary"], after["start"], after["end"], after.get("location", ""))
    request = service.events().patch(calendarId="primary", eventId=event_id, body={
        "summary": payload["summary"], **calendar_event_times(payload),
        "location": payload["location"]})
    if version:
        request.headers["If-Match"] = version
    return {"updated": calendar_event_snapshot(request.execute())}


def calendar_delete_event(event_id, expected, version):
    service, current = calendar_primary_event(event_id)
    if calendar_event_snapshot(current) != expected or current.get("etag") != version:
        raise ValueError("calendar event changed after approval was requested; review it and try again")
    request = service.events().delete(calendarId="primary", eventId=event_id)
    if version:
        request.headers["If-Match"] = version
    request.execute()
    return {"deleted": expected}


def gmail_text(payload):
    parts, text = [payload], []
    while parts and sum(len(value) for value in text) < 100_000:
        part = parts.pop(0)
        parts.extend(part.get("parts", []))
        body = part.get("body", {}).get("data")
        if body and part.get("mimeType", "").startswith("text/"):
            try:
                text.append(base64.urlsafe_b64decode(body + "===").decode("utf-8", "replace"))
            except Exception:
                continue
    return "\n\n".join(text)[:100_000]


def gmail_messages(query="", max_results=10):
    from googleapiclient.discovery import build
    max_results = max(1, min(int(max_results), 25))
    service = build("gmail", "v1", credentials=google_credentials(), cache_discovery=False)
    listing = service.users().messages().list(
        userId="me", q=str(query), maxResults=max_results).execute()
    messages = []
    for reference in listing.get("messages", []):
        message = service.users().messages().get(
            userId="me", id=reference["id"], format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"]).execute()
        headers = {item["name"].lower(): item["value"]
                   for item in message.get("payload", {}).get("headers", [])}
        messages.append({"id": message["id"], "from": headers.get("from"),
                         "to": headers.get("to"), "subject": headers.get("subject"),
                         "date": headers.get("date"), "snippet": message.get("snippet", "")})
    return {"query": str(query), "messages": messages}


def gmail_message(message_id):
    from googleapiclient.discovery import build
    service = build("gmail", "v1", credentials=google_credentials(), cache_discovery=False)
    message = service.users().messages().get(
        userId="me", id=str(message_id), format="full").execute()
    headers = {item["name"].lower(): item["value"]
               for item in message.get("payload", {}).get("headers", [])}
    return {"id": message["id"], "from": headers.get("from"), "to": headers.get("to"),
            "subject": headers.get("subject"), "date": headers.get("date"),
            "body": gmail_text(message.get("payload", {}))}


def gmail_send_payload(to, subject, body):
    if not isinstance(to, str) or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", to.strip()):
        raise ValueError("a single valid recipient email address is required")
    if not isinstance(subject, str) or not subject.strip() or "\r" in subject or "\n" in subject:
        raise ValueError("an email subject is required")
    if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > 100_000:
        raise ValueError("an email body is required and limited to 100 KiB")
    return {"to": to, "subject": subject, "body": body}


def gmail_send(to, subject, body):
    payload = gmail_send_payload(to, subject, body)
    message = EmailMessage()
    message["To"] = payload["to"].strip()
    message["Subject"] = payload["subject"].strip()
    message.set_content(payload["body"])
    from googleapiclient.discovery import build
    service = build("gmail", "v1", credentials=google_credentials(), cache_discovery=False)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {"message_id": sent.get("id"), "to": payload["to"].strip(),
            "subject": payload["subject"].strip()}


def propose_gmail_send(payload):
    proposal = gmail_send_payload(payload["to"], payload["subject"], payload["body"])
    return {"approval_id": approvals.create("gmail_send", proposal), "status": "pending",
            "proposal": {"to": proposal["to"], "subject": proposal["subject"]}}


def register_routes(router):
    router.get("/v1/google/status", lambda _p, _q: google_status())
    router.post("/v1/google/auth/start", lambda _p, _q: google_auth_start())
    router.post("/v1/google/calendar/list", lambda _p, _q: google_calendars())
    router.post("/v1/google/calendar/events", lambda p, _q: google_calendar_events(
        p.get("days", 7), p.get("max_results", 25)))
    router.post("/v1/google/gmail/search", lambda p, _q: gmail_messages(
        p.get("query", ""), p.get("max_results", 10)))
    router.post("/v1/google/gmail/message", lambda p, _q: gmail_message(p["message_id"]))
    router.post("/v1/proposals/calendar-create", lambda p, _q: _calendar_create_response(p), 202)
    router.post("/v1/proposals/calendar-update", lambda p, _q: _calendar_update_response(p), 202)
    router.post("/v1/proposals/calendar-delete", lambda p, _q: _calendar_delete_response(p), 202)
    router.post("/v1/proposals/gmail-send", lambda p, _q: propose_gmail_send(p), 202)


def _calendar_create_response(payload):
    proposal = calendar_event_payload(
        payload["summary"], payload["start"], payload["end"], payload.get("location", ""))
    return {"approval_id": approvals.create("calendar_create", proposal), "status": "pending",
            "proposal": proposal}


def _calendar_update_response(payload):
    proposal = calendar_update_proposal(
        payload["event_id"], payload["summary"], payload["start"], payload["end"],
        payload.get("location", ""))
    return {"approval_id": approvals.create("calendar_update", proposal), "status": "pending",
            "proposal": proposal}


def _calendar_delete_response(payload):
    proposal = calendar_delete_proposal(payload["event_id"])
    return {"approval_id": approvals.create("calendar_delete", proposal), "status": "pending",
            "proposal": proposal}


def register_actions():
    approvals.register_action("gmail_send", lambda p: gmail_send(p["to"], p["subject"], p["body"]))
    approvals.register_action("calendar_create", lambda p: calendar_create_event(
        p["summary"], p["start"], p["end"], p.get("location", "")))
    approvals.register_action("calendar_update", lambda p: calendar_update_event(
        p["event_id"], p["after"], p["before"], p.get("version")))
    approvals.register_action("calendar_delete", lambda p: calendar_delete_event(
        p["event_id"], p["event"], p.get("version")))


class GoogleOAuthCallback(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        global _pending_state, _pending_code_verifier
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if params.get("error"):
            return self._reply(400, "Google authorization was cancelled or denied. You can close this page.")
        code, state = params.get("code", [None])[0], params.get("state", [None])[0]
        with _auth_lock:
            valid_state, code_verifier = _pending_state, _pending_code_verifier
        if not code or not state or not secrets.compare_digest(state, valid_state or ""):
            return self._reply(
                400, "Invalid or expired Google authorization request. Start authorization again.")
        try:
            flow = google_flow()
            flow.code_verifier = code_verifier
            flow.fetch_token(authorization_response=GOOGLE_REDIRECT_URI + "?" +
                             urllib.parse.urlencode({"code": code, "state": state}))
            save_google_token(flow.credentials)
            with _auth_lock:
                _pending_state = None
                _pending_code_verifier = None
        except Exception as exc:
            print(f"[google-oauth] token exchange failed: {type(exc).__name__}: {exc}", flush=True)
            return self._reply(400, "Google authorization could not be completed. Return to the assistant and try again.")
        return self._reply(200, "Google Calendar and Gmail are connected. You can close this page.")

    def _reply(self, status, message):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(message.encode())


def start_oauth_server():
    threading.Thread(
        target=lambda: ThreadingHTTPServer(("0.0.0.0", 8765), GoogleOAuthCallback).serve_forever(),
        daemon=True).start()
