"""Small, allowlisted Command Center. It intentionally has no shell endpoint."""
import base64, ipaddress, json, os, re, secrets, shutil, socket, sqlite3, threading, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

TOKEN = os.environ["COMMAND_CENTER_TOKEN"]
CALLBACK_TOKEN = os.environ.get("COMMAND_CENTER_CALLBACK_TOKEN", "")
KUMA_URL = os.environ.get("UPTIME_KUMA_URL", "").rstrip("/")
KUMA_API_KEY = os.environ.get("UPTIME_KUMA_API_KEY", "")
BROWSER_URL = os.environ.get("TELEGRAM_BROWSER_URL", "").rstrip("/")
DOWNLOADER_URL = os.environ.get("TELEGRAM_DOWNLOADER_URL", "").rstrip("/")
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")
DB = Path("/data/command-center.sqlite3")
ATTACHMENTS = Path("/attachments").resolve()
WORKSPACE = Path("/workspace").resolve()
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TEXT_BYTES = 512 * 1024
GOOGLE_CLIENT_SECRET = Path("/google/client-secret.json")
GOOGLE_TOKEN = Path("/data/google-token.json")
GOOGLE_REDIRECT_URI = "http://localhost:8766/"
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly", "https://www.googleapis.com/auth/calendar.events.owned", "https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.send"]
google_auth_lock = threading.Lock()
google_pending_state = None
google_pending_code_verifier = None
DB.parent.mkdir(parents=True, exist_ok=True)
WORKSPACE.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DB, check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, action TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, created INTEGER NOT NULL)")
db.execute("CREATE TABLE IF NOT EXISTS watches (id TEXT PRIMARY KEY, url TEXT NOT NULL, target REAL NOT NULL, currency TEXT NOT NULL, every_minutes INTEGER NOT NULL, enabled INTEGER NOT NULL, last_price REAL, last_checked INTEGER)")
db.execute("CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, watch_id TEXT NOT NULL, price REAL NOT NULL, created INTEGER NOT NULL)")
db.execute("CREATE TABLE IF NOT EXISTS searchgram_sessions (id TEXT PRIMARY KEY, query TEXT NOT NULL, page_json TEXT NOT NULL, created INTEGER NOT NULL)")
db.commit()
searchgram_delivery_lock = threading.Lock()

def public_url(value):
    u = urllib.parse.urlparse(value)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        raise ValueError("URL must be a public http(s) URL without credentials")
    for addr in socket.getaddrinfo(u.hostname, None):
        if not ipaddress.ip_address(addr[4][0]).is_global:
            raise ValueError("private destinations are not allowed")
    return value

class PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Apply the same SSRF boundary to every redirect hop, not only the first URL."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

PUBLIC_URL_OPENER = urllib.request.build_opener(PublicRedirectHandler())

def scoped_path(root, relative, *, require_exists=True):
    """Resolve a relative file name without permitting path or symlink escapes."""
    if not isinstance(relative, str) or not relative or len(relative) > 512:
        raise ValueError("file path is required")
    parsed = PurePosixPath(relative)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise ValueError("file path must stay inside its designated workspace")
    candidate = (root / parsed).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("file path escapes its designated workspace")
    if require_exists and not candidate.exists():
        raise ValueError("file not found")
    return candidate

def file_summary(root):
    if not root.exists():
        return []
    files = []
    for candidate in sorted(root.rglob("*")):
        if not candidate.is_file():
            continue
        try:
            resolved = candidate.resolve()
            if root not in resolved.parents or resolved.stat().st_size > MAX_FILE_BYTES:
                continue
            stat = resolved.stat()
            files.append({"name": str(resolved.relative_to(root)), "size_bytes": stat.st_size,
                          "modified": int(stat.st_mtime)})
        except OSError:
            continue
        if len(files) >= 100:
            break
    return files

def attachment_files():
    """List Telegram-uploaded documents, exposed only as a read-only import source."""
    return file_summary(ATTACHMENTS)

def workspace_files():
    return file_summary(WORKSPACE)

def save_attachment_to_workspace(attachment_name, destination=None):
    source = scoped_path(ATTACHMENTS, attachment_name)
    if not source.is_file() or source.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("attachment is not an allowed file")
    destination = destination or source.name
    target = scoped_path(WORKSPACE, destination, require_exists=False)
    if target.exists():
        raise ValueError("workspace destination already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return {"name": str(target.relative_to(WORKSPACE)), "size_bytes": target.stat().st_size}

def read_workspace_text(name):
    path = scoped_path(WORKSPACE, name)
    if not path.is_file() or path.stat().st_size > MAX_TEXT_BYTES:
        raise ValueError("text file is missing or too large")
    if path.suffix.lower() == ".pdf":
        raise ValueError("use the PDF reader for PDF files")
    return {"name": str(path.relative_to(WORKSPACE)),
            "content": path.read_bytes()[:MAX_TEXT_BYTES].decode("utf-8", "replace")}

def write_workspace_text(name, content):
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("text content is required and limited to 512 KiB")
    path = scoped_path(WORKSPACE, name, require_exists=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"name": str(path.relative_to(WORKSPACE)), "size_bytes": path.stat().st_size}

def delete_workspace_file(name):
    path = scoped_path(WORKSPACE, name)
    if not path.is_file():
        raise ValueError("only files in the workspace can be deleted")
    path.unlink()
    return {"deleted": str(path.relative_to(WORKSPACE))}

def read_pdf(source, name, max_pages=20):
    roots = {"attachment": ATTACHMENTS, "workspace": WORKSPACE}
    if source not in roots:
        raise ValueError("PDF source must be attachment or workspace")
    path = scoped_path(roots[source], name)
    if not path.is_file() or path.suffix.lower() != ".pdf" or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("PDF is missing or too large")
    try:
        max_pages = max(1, min(int(max_pages), 30))
        from pypdf import PdfReader
        reader = PdfReader(str(path), strict=False)
        text = "\n\n".join(page.extract_text() or "" for page in reader.pages[:max_pages])
    except Exception as exc:
        raise ValueError(f"could not read PDF: {exc}") from exc
    return {"name": str(path.relative_to(roots[source])), "source": source,
            "pages_read": min(len(reader.pages), max_pages), "total_pages": len(reader.pages),
            "text": text[:100_000], "truncated": len(text) > 100_000 or len(reader.pages) > max_pages}
def docker(method, path):
    s=socket.socket(socket.AF_UNIX); s.connect("/var/run/docker.sock")
    # HTTP/1.0 requests make Docker return a length-delimited response rather
    # than a chunked stream, keeping this deliberately tiny Unix-socket client
    # deterministic.
    s.sendall(f"{method} {path} HTTP/1.0\r\nHost: docker\r\n\r\n".encode())
    b=b""
    while True:
        x=s.recv(65536)
        if not x: break
        b+=x
    status=int(b.split(b" ",2)[1]); body=b.split(b"\r\n\r\n",1)[1]
    if status >= 300: raise RuntimeError(f"Docker returned {status}")
    return json.loads(body or b"{}")
def price(url):
    public_url(url)
    req=urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0 (compatible; CommandCenter/1.0)"})
    html=PUBLIC_URL_OPENER.open(req, timeout=20).read(1_500_000).decode("utf-8","ignore")
    matches=re.findall(r'(?:product:price:amount|"price"|itemprop=["\']price["\'])[^>]{0,180}?content=["\']?([0-9]+(?:[.,][0-9]{1,2})?)|"price"\s*:\s*"?([0-9]+(?:\.[0-9]{1,2})?)', html, re.I)
    values=[float((a or b).replace(",","")) for a,b in matches if a or b]
    if not values: raise ValueError("no machine-readable public price found")
    return min(values)
def kuma_status():
    """Return a safe monitor summary from Uptime Kuma's read-only metrics API."""
    if not KUMA_URL or not KUMA_API_KEY:
        raise ValueError("Uptime Kuma monitoring is not configured")
    credentials = base64.b64encode((":" + KUMA_API_KEY).encode()).decode()
    req = urllib.request.Request(
        KUMA_URL + "/metrics",
        headers={"Authorization": "Basic " + credentials},
    )
    metrics = urllib.request.urlopen(req, timeout=15).read(1_000_000).decode("utf-8", "replace")
    # Values are Uptime Kuma's documented monitor states: 0=down, 1=up,
    # 2=pending, 3=maintenance. Deliberately omit monitor URLs from the result.
    states = {0: "down", 1: "up", 2: "pending", 3: "maintenance"}
    monitors, response_times = [], {}
    for line in metrics.splitlines():
        match = re.match(r"^(monitor_status|monitor_response_time)\{(.*)\} ([0-9.eE+-]+)$", line)
        if not match:
            continue
        labels = {key: bytes(value, "utf-8").decode("unicode_escape")
                  for key, value in re.findall(r'([A-Za-z_][A-Za-z0-9_]*)="((?:\\.|[^"])*)"', match.group(2))}
        name = labels.get("monitor_name")
        if not name:
            continue
        value = float(match.group(3))
        if match.group(1) == "monitor_response_time":
            response_times[name] = round(value)
        else:
            monitors.append({"name": name, "type": labels.get("monitor_type", "unknown"),
                             "status": states.get(int(value), "unknown")})
    for monitor in monitors:
        if monitor["name"] in response_times:
            monitor["response_time_ms"] = response_times[monitor["name"]]
    monitors.sort(key=lambda monitor: monitor["name"].lower())
    summary = {state: sum(monitor["status"] == state for monitor in monitors) for state in states.values()}
    return {"summary": summary, "monitors": monitors}
def internal_json(method, base, path, payload=None):
    if not base:
        raise ValueError("internal media service is not configured")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as response:
        return json.load(response)
def searchgram_page(session_id, page):
    items = page.get("items")
    if not isinstance(items, list):
        raise ValueError("SearchGram returned an invalid result page")
    return {"search_id": session_id, "query": page.get("query"), "page": page.get("page"),
            "total_pages": page.get("total_pages"), "total_results": page.get("total_results"),
            "results": [{"number": index + 1, "title": item.get("title"), "size": item.get("size")}
                        for index, item in enumerate(items)]}
def load_searchgram_session(session_id):
    row = db.execute("SELECT page_json,created FROM searchgram_sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        raise ValueError("unknown SearchGram search; search again")
    if int(time.time()) - row[1] > 900:
        raise ValueError("SearchGram search expired; search again")
    return json.loads(row[0])
def save_searchgram_session(session_id, page):
    db.execute("INSERT OR REPLACE INTO searchgram_sessions VALUES (?,?,?,?)",
        (session_id, page.get("query", ""), json.dumps(page), int(time.time())))
    db.commit()
def searchgram_search(query):
    query = str(query).strip()
    if not query:
        raise ValueError("search query is empty")
    page = internal_json("POST", BROWSER_URL, "/api/search", {"query": query})
    session_id = os.urandom(9).hex()
    save_searchgram_session(session_id, page)
    return searchgram_page(session_id, page)
def searchgram_navigate(session_id, direction):
    page = load_searchgram_session(session_id)
    current = page.get("page")
    if isinstance(current, bool) or not isinstance(current, int):
        raise ValueError("SearchGram returned an invalid current page")
    candidates = []
    for nav in page.get("navigation", []):
        callback = nav.get("callback_data", "")
        if not callback.startswith("search#"):
            continue
        try:
            target = int(callback.rsplit("#", 1)[1])
        except (IndexError, ValueError):
            continue
        if (direction == "next" and target > current) or (direction == "previous" and target < current):
            candidates.append((target, callback))
    if not candidates:
        raise ValueError(f"no {direction} SearchGram page is available")
    target, callback = (min(candidates) if direction == "next" else max(candidates))
    result = internal_json("POST", BROWSER_URL, "/api/search/navigate", {
        "message_id": page["message_id"], "callback_data": callback, "query": page["query"]})
    save_searchgram_session(session_id, result)
    return searchgram_page(session_id, result)
def queue_searchgram_result(session_id, result_number):
    page = load_searchgram_session(session_id)
    if isinstance(result_number, bool) or not isinstance(result_number, int) or result_number < 1:
        raise ValueError("result number must be a positive integer")
    items = page.get("items", [])
    if result_number > len(items):
        raise ValueError("result number is not on the current SearchGram page")
    item = items[result_number - 1]
    audit_id = audit("searchgram_download", {"query": page["query"], "title": item.get("title"), "size": item.get("size")})
    db.execute("UPDATE approvals SET status='processing' WHERE id=?", (audit_id,)); db.commit()
    def deliver():
        # SearchGram's Telegram delivery bot can rate-limit bursts. Serializing
        # requests prevents the agent from turning one temporary limit into a
        # flood of overlapping retries.
        with searchgram_delivery_lock:
            try:
                internal_json("POST", BROWSER_URL, "/api/search/download", {
                    "message_id": page["message_id"], "callback_data": item["callback_data"]})
            except Exception as exc:
                row = db.execute("SELECT payload FROM approvals WHERE id=?", (audit_id,)).fetchone()
                detail = json.loads(row[0]) if row else {}
                detail["delivery_error"] = str(exc)
                db.execute("UPDATE approvals SET payload=?,status='failed' WHERE id=?",
                           (json.dumps(detail), audit_id)); db.commit()
            else:
                db.execute("UPDATE approvals SET status='executed' WHERE id=?", (audit_id,)); db.commit()
    threading.Thread(target=deliver, daemon=True).start()
    return {"audit_id": audit_id, "queued_result": {"title": item.get("title"), "size": item.get("size")},
            "status": "processing"}
def media_download_status():
    deliveries = []
    for audit_id, payload, status, created in db.execute(
            "SELECT id,payload,status,created FROM approvals WHERE action='searchgram_download' ORDER BY created DESC LIMIT 10"):
        detail = json.loads(payload)
        deliveries.append({"audit_id": audit_id, "title": detail.get("title"),
                           "size": detail.get("size"), "status": status, "created": created,
                           "error": detail.get("delivery_error")})
    return {"downloader": internal_json("GET", DOWNLOADER_URL, "/status"),
            "recent_searchgram_deliveries": deliveries}
def jellyfin_search(query):
    """Search Jellyfin's library through its read-only item lookup endpoint."""
    query = str(query).strip()
    if not query:
        raise ValueError("Jellyfin search query is empty")
    if not JELLYFIN_URL or not JELLYFIN_API_KEY:
        raise ValueError("Jellyfin library search is not configured")
    params = urllib.parse.urlencode({
        "SearchTerm": query, "Recursive": "true", "Limit": 20,
        "IncludeItemTypes": "Movie,Series,Episode",
        "Fields": "ProductionYear,UserData,SeriesName",
    })
    req = urllib.request.Request(
        JELLYFIN_URL + "/Items?" + params,
        headers={"Authorization": f'MediaBrowser Token="{JELLYFIN_API_KEY}"'},
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        data = json.load(response)
    items = data.get("Items", [])
    if not isinstance(items, list):
        raise ValueError("Jellyfin returned an invalid search response")
    return {"query": query, "total": int(data.get("TotalRecordCount", len(items))),
            "results": [{
                "title": item.get("Name"), "type": item.get("Type"),
                "year": item.get("ProductionYear"), "series": item.get("SeriesName"),
                "watched": bool(item.get("UserData", {}).get("Played", False)),
            } for item in items]}

def jellyfin_get(path, params):
    req = urllib.request.Request(
        JELLYFIN_URL + path + "?" + urllib.parse.urlencode(params),
        headers={"Authorization": f'MediaBrowser Token="{JELLYFIN_API_KEY}"'},
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)

def normalized_title(value):
    return "".join(char for char in str(value).casefold() if char.isalnum())

def jellyfin_series_episodes(query, season):
    """Return one season's episode inventory, falling back to the complete series list."""
    query = str(query).strip()
    if not query:
        raise ValueError("series query is empty")
    if isinstance(season, bool) or not isinstance(season, int) or season < 0:
        raise ValueError("season must be a non-negative number")
    if not JELLYFIN_URL or not JELLYFIN_API_KEY:
        raise ValueError("Jellyfin library search is not configured")
    fields = "ProductionYear,SortName"
    result = jellyfin_get("/Items", {"SearchTerm": query, "Recursive": "true", "Limit": 20,
                                      "IncludeItemTypes": "Series", "Fields": fields})
    candidates = result.get("Items", [])
    fallback_used = False
    if not candidates:
        # A title can be indexed differently (for example a localized title).
        # At this library size, a bounded full-series scan is cheap and gives
        # the assistant a useful fallback without widening filesystem access.
        result = jellyfin_get("/Items", {"Recursive": "true", "Limit": 10000,
                                          "IncludeItemTypes": "Series", "Fields": fields})
        needle = normalized_title(query)
        candidates = [item for item in result.get("Items", [])
                      if needle in normalized_title(item.get("Name", ""))
                      or needle in normalized_title(item.get("SortName", ""))]
        fallback_used = True
    if not candidates:
        raise ValueError("series not found in Jellyfin")
    needle = normalized_title(query)
    series = next((item for item in candidates if normalized_title(item.get("Name", "")) == needle), candidates[0])
    episodes = jellyfin_get(f"/Shows/{urllib.parse.quote(series['Id'], safe='')}/Episodes", {
        "Season": season, "Fields": "UserData,Overview",
    })
    items = episodes.get("Items", [])
    if not isinstance(items, list):
        raise ValueError("Jellyfin returned an invalid episode response")
    return {"series": series.get("Name"), "year": series.get("ProductionYear"), "season": season,
            "total": int(episodes.get("TotalRecordCount", len(items))), "fallback_used": fallback_used,
            "episodes": [{"number": item.get("IndexNumber"), "title": item.get("Name"),
                          "watched": item.get("UserData", {}).get("Played") if isinstance(item.get("UserData"), dict) else None}
                         for item in items]}

def google_flow():
    from google_auth_oauthlib.flow import Flow
    if not GOOGLE_CLIENT_SECRET.is_file():
        raise ValueError("Google OAuth client is not configured")
    # Google installed-app OAuth explicitly permits an HTTP *loopback*
    # redirect. The callback port is host-loopback-only and reached via SSH;
    # Google token/API traffic remains HTTPS.
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    return Flow.from_client_secrets_file(str(GOOGLE_CLIENT_SECRET), scopes=GOOGLE_SCOPES,
                                         redirect_uri=GOOGLE_REDIRECT_URI)

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
    return {"client_configured": GOOGLE_CLIENT_SECRET.is_file(), "authorized": GOOGLE_TOKEN.is_file(),
            "scopes": ["calendar.readonly", "calendar.events.owned", "gmail.readonly", "gmail.send"]}

def save_google_token(credentials):
    """Persist refreshed OAuth credentials without a permissive-file window."""
    descriptor = os.open(GOOGLE_TOKEN, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
        os.fchmod(descriptor, 0o600)
        token_file.write(credentials.to_json())

def google_auth_start():
    global google_pending_state, google_pending_code_verifier
    flow = google_flow()
    # The installed-app flow uses PKCE. Keep only this short-lived verifier in
    # memory until the local callback exchanges the one-time authorization
    # code; credentials themselves never enter Hermes.
    flow.code_verifier = secrets.token_urlsafe(64)
    authorization_url, state = flow.authorization_url(access_type="offline", prompt="consent",
                                                       include_granted_scopes="true")
    with google_auth_lock:
        google_pending_state = state
        google_pending_code_verifier = flow.code_verifier
    return {"authorization_url": authorization_url, "redirect_uri": GOOGLE_REDIRECT_URI}

def google_calendars():
    from googleapiclient.discovery import build
    service = build("calendar", "v3", credentials=google_credentials(), cache_discovery=False)
    response = service.calendarList().list(maxResults=250, showHidden=False).execute()
    calendars = [{"id": item.get("id"), "name": item.get("summaryOverride") or item.get("summary"),
                  "primary": bool(item.get("primary")), "access_role": item.get("accessRole")}
                 for item in response.get("items", []) if not item.get("hidden")]
    return {"calendars": calendars}

def google_calendar_events(days=7, max_results=25):
    from googleapiclient.discovery import build
    days = max(1, min(int(days), 31))
    max_results = max(1, min(int(max_results), 100))
    now = datetime.now(timezone.utc)
    service = build("calendar", "v3", credentials=google_credentials(), cache_discovery=False)
    calendars = google_calendars()["calendars"]
    events = []
    for calendar in calendars:
        if not calendar["id"]:
            continue
        response = service.events().list(calendarId=calendar["id"], timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=days)).isoformat(), singleEvents=True,
            orderBy="startTime", maxResults=max_results).execute()
        for event in response.get("items", []):
            events.append({"title": event.get("summary", "(untitled)"),
                "start": event.get("start", {}).get("dateTime", event.get("start", {}).get("date")),
                "end": event.get("end", {}).get("dateTime", event.get("end", {}).get("date")),
                "location": event.get("location"), "calendar": calendar["name"]})
    events.sort(key=lambda event: event.get("start") or "")
    return {"days": days, "calendars_checked": [calendar["name"] for calendar in calendars],
            "events": events[:max_results]}

def calendar_event_payload(summary, start, end, location=""):
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 500:
        raise ValueError("an event summary is required")
    if not isinstance(start, str) or not isinstance(end, str):
        raise ValueError("event start and end must be ISO 8601 datetimes with timezone")
    try:
        start_at = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_at = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("event start and end must be ISO 8601 datetimes with timezone") from exc
    if start_at.tzinfo is None or end_at.tzinfo is None or end_at <= start_at:
        raise ValueError("event end must be after start, and both must include timezone")
    if not isinstance(location, str) or len(location) > 1000:
        raise ValueError("event location is invalid")
    return {"summary": summary.strip(), "start": start, "end": end, "location": location.strip()}

def calendar_create_event(summary, start, end, location=""):
    payload = calendar_event_payload(summary, start, end, location)
    from googleapiclient.discovery import build
    service = build("calendar", "v3", credentials=google_credentials(), cache_discovery=False)
    event = service.events().insert(calendarId="primary", body={
        "summary": payload["summary"], "start": {"dateTime": payload["start"]},
        "end": {"dateTime": payload["end"]}, **({"location": payload["location"]} if payload["location"] else {}),
    }).execute()
    return {"event_id": event.get("id"), "summary": event.get("summary"),
            "start": event.get("start", {}).get("dateTime"), "end": event.get("end", {}).get("dateTime")}

def gmail_text(payload):
    """Extract a bounded plain-text representation from Gmail's MIME payload."""
    parts = [payload]
    text = []
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
    listing = service.users().messages().list(userId="me", q=str(query), maxResults=max_results).execute()
    messages = []
    for ref in listing.get("messages", []):
        message = service.users().messages().get(userId="me", id=ref["id"], format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"]).execute()
        headers = {item["name"].lower(): item["value"] for item in message.get("payload", {}).get("headers", [])}
        messages.append({"id": message["id"], "from": headers.get("from"), "to": headers.get("to"),
                         "subject": headers.get("subject"), "date": headers.get("date"),
                         "snippet": message.get("snippet", "")})
    return {"query": str(query), "messages": messages}

def gmail_message(message_id):
    from googleapiclient.discovery import build
    service = build("gmail", "v1", credentials=google_credentials(), cache_discovery=False)
    message = service.users().messages().get(userId="me", id=str(message_id), format="full").execute()
    headers = {item["name"].lower(): item["value"] for item in message.get("payload", {}).get("headers", [])}
    return {"id": message["id"], "from": headers.get("from"), "to": headers.get("to"),
            "subject": headers.get("subject"), "date": headers.get("date"),
            "body": gmail_text(message.get("payload", {}))}

def gmail_send(to, subject, body):
    """Send an already-approved plain-text email through the home Gmail account."""
    if not isinstance(to, str) or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", to.strip()):
        raise ValueError("a single valid recipient email address is required")
    if not isinstance(subject, str) or not subject.strip() or "\r" in subject or "\n" in subject:
        raise ValueError("an email subject is required")
    if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > 100_000:
        raise ValueError("an email body is required and limited to 100 KiB")
    message = EmailMessage()
    message["To"] = to.strip()
    message["Subject"] = subject.strip()
    message.set_content(body)
    from googleapiclient.discovery import build
    service = build("gmail", "v1", credentials=google_credentials(), cache_discovery=False)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {"message_id": sent.get("id"), "to": to.strip(), "subject": subject.strip()}
def audit(action, payload):
    ident=os.urandom(9).hex(); db.execute("INSERT INTO approvals VALUES (?,?,?,?,?)",(ident,action,json.dumps(payload),"pending",int(time.time()))); db.commit(); return ident
def execute(action, p):
    if action == "docker_restart": docker("POST",f"/containers/{urllib.parse.quote(p['container'],safe='')}/restart?t=20"); return {"restarted":p["container"]}
    if action == "gmail_send": return gmail_send(p["to"], p["subject"], p["body"])
    if action == "calendar_create": return calendar_create_event(p["summary"], p["start"], p["end"], p.get("location", ""))
    if action == "price_watch_create":
        db.execute("INSERT INTO watches VALUES (?,?,?,?,?,?,?,?)",(p["id"],p["url"],p["target"],p["currency"],p["every_minutes"],1,None,None)); db.commit(); return {"watch_id":p["id"]}
    raise ValueError("action is not allowlisted")
def watch_loop():
    while True:
        now=int(time.time())
        for watch in db.execute("SELECT id,url,target,every_minutes,last_checked FROM watches WHERE enabled=1").fetchall():
            ident,url,target,every,last=watch
            if last and now-last < every*60: continue
            try:
                observed=price(url)
                db.execute("UPDATE watches SET last_price=?,last_checked=? WHERE id=?",(observed,now,ident))
                if observed <= target:
                    previous=db.execute("SELECT price FROM alerts WHERE watch_id=? ORDER BY id DESC LIMIT 1",(ident,)).fetchone()
                    if not previous or previous[0] != observed:
                        db.execute("INSERT INTO alerts(watch_id,price,created) VALUES (?,?,?)",(ident,observed,now))
                db.commit()
            except Exception: pass # retain the last known good value on a failed site fetch
        time.sleep(60)
class API(BaseHTTPRequestHandler):
    def log_message(self,*_): pass
    def send(self, code, data):
        raw=json.dumps(data).encode(); self.send_response(code); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def body(self): return json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))) or b"{}")
    def auth(self): return self.headers.get("Authorization")==f"Bearer {TOKEN}"
    def callback_auth(self):
        return bool(CALLBACK_TOKEN) and self.headers.get("X-Command-Center-Callback") == CALLBACK_TOKEN
    def do_GET(self):
        if not self.auth(): return self.send(401,{"error":"unauthorized"})
        try:
            if self.path=="/v1/health":
                m={k:v for k,v in (x.split(":",1) for x in Path("/host/proc/meminfo").read_text().splitlines() if ":" in x)}
                return self.send(200,{"uptime_seconds":int(float(Path("/host/proc/uptime").read_text().split()[0])),"loadavg":Path("/host/proc/loadavg").read_text().split()[:3],"memory_available_kib":int(m["MemAvailable"].split()[0])})
            if self.path=="/v1/docker/containers":
                return self.send(200,[{"id":x["Id"][:12],"name":x["Names"][0].lstrip("/"),"image":x["Image"],"state":x["State"],"status":x["Status"]} for x in docker("GET","/containers/json?all=1")])
            if self.path=="/v1/uptime-kuma/monitors": return self.send(200,kuma_status())
            if self.path=="/v1/media/download-status": return self.send(200,media_download_status())
            if self.path=="/v1/google/status": return self.send(200,google_status())
            if self.path=="/v1/files/attachments": return self.send(200,attachment_files())
            if self.path=="/v1/files/workspace": return self.send(200,workspace_files())
            if self.path.startswith("/v1/jellyfin/search?"):
                return self.send(200,jellyfin_search(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("query", [""])[0]))
            if self.path=="/v1/price-watches": return self.send(200,[dict(zip(["id","url","target","currency","every_minutes","enabled","last_price","last_checked"],r)) for r in db.execute("SELECT * FROM watches")])
            if self.path=="/v1/price-alerts": return self.send(200,[dict(zip(["watch_id","price","created"],r)) for r in db.execute("SELECT watch_id,price,created FROM alerts ORDER BY id DESC LIMIT 100")])
            return self.send(404,{"error":"not found"})
        except Exception as e: return self.send(502,{"error":str(e)})
    def do_POST(self):
        callback_route = self.path.startswith("/v1/approvals/") and self.path.endswith(("/approve", "/deny"))
        if not self.auth() and not (callback_route and self.callback_auth()): return self.send(401,{"error":"unauthorized"})
        try:
            p=self.body()
            if self.path=="/v1/maps/search":
                q=urllib.parse.quote(p["query"]); req=urllib.request.Request(f"https://nominatim.openstreetmap.org/search?q={q}&format=jsonv2&limit=5",headers={"User-Agent":"CommandCenter/1.0"}); return self.send(200,json.loads(urllib.request.urlopen(req,timeout=20).read()))
            if self.path=="/v1/google/auth/start": return self.send(200,google_auth_start())
            if self.path=="/v1/google/calendar/list": return self.send(200,google_calendars())
            if self.path=="/v1/google/calendar/events": return self.send(200,google_calendar_events(p.get("days", 7), p.get("max_results", 25)))
            if self.path=="/v1/google/gmail/search": return self.send(200,gmail_messages(p.get("query", ""), p.get("max_results", 10)))
            if self.path=="/v1/google/gmail/message": return self.send(200,gmail_message(p["message_id"]))
            if self.path=="/v1/proposals/calendar-create":
                event = calendar_event_payload(p["summary"], p["start"], p["end"], p.get("location", ""))
                return self.send(202,{"approval_id":audit("calendar_create",event),"status":"pending","proposal":event})
            if self.path=="/v1/proposals/gmail-send":
                # Validate now, before creating an approval card; execution
                # re-validates immediately before it sends.
                gmail_send_payload = {"to": p["to"], "subject": p["subject"], "body": p["body"]}
                if not isinstance(gmail_send_payload["to"], str) or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", gmail_send_payload["to"].strip()): raise ValueError("a single valid recipient email address is required")
                if not isinstance(gmail_send_payload["subject"], str) or not gmail_send_payload["subject"].strip() or "\r" in gmail_send_payload["subject"] or "\n" in gmail_send_payload["subject"]: raise ValueError("an email subject is required")
                if not isinstance(gmail_send_payload["body"], str) or not gmail_send_payload["body"].strip() or len(gmail_send_payload["body"].encode("utf-8")) > 100_000: raise ValueError("an email body is required and limited to 100 KiB")
                return self.send(202,{"approval_id":audit("gmail_send",gmail_send_payload),"status":"pending","proposal":{"to":gmail_send_payload["to"],"subject":gmail_send_payload["subject"]}})
            if self.path=="/v1/searchgram/search": return self.send(200,searchgram_search(p["query"]))
            if self.path=="/v1/searchgram/next-page": return self.send(200,searchgram_navigate(p["search_id"], "next"))
            if self.path=="/v1/searchgram/previous-page": return self.send(200,searchgram_navigate(p["search_id"], "previous"))
            if self.path=="/v1/searchgram/queue": return self.send(202,queue_searchgram_result(p["search_id"], p["result_number"]))
            if self.path=="/v1/jellyfin/series-episodes": return self.send(200,jellyfin_series_episodes(p["query"], p["season"]))
            if self.path=="/v1/files/import-attachment": return self.send(201,save_attachment_to_workspace(p["attachment_name"], p.get("destination")))
            if self.path=="/v1/files/read-text": return self.send(200,read_workspace_text(p["name"]))
            if self.path=="/v1/files/read-pdf": return self.send(200,read_pdf(p["source"], p["name"], p.get("max_pages", 20)))
            if self.path=="/v1/files/write-text": return self.send(201,write_workspace_text(p["name"], p["content"]))
            if self.path=="/v1/files/delete": return self.send(200,delete_workspace_file(p["name"]))
            if self.path=="/v1/price-watches/quote": return self.send(200,{"price":price(p["url"])})
            if self.path=="/v1/proposals/docker-restart": return self.send(202,{"approval_id":audit("docker_restart",{"container":p["container"]}),"status":"pending"})
            if self.path=="/v1/proposals/price-watch":
                watch={"id":os.urandom(8).hex(),"url":public_url(p["url"]),"target":float(p["target"]),"currency":p.get("currency","USD"),"every_minutes":max(60,int(p.get("every_minutes",360)))}
                watch["baseline_price"]=price(watch["url"]); return self.send(202,{"approval_id":audit("price_watch_create",watch),"proposal":watch})
            if self.path.startswith("/v1/approvals/") and self.path.endswith("/approve"):
                ident=self.path.split("/")[3]; row=db.execute("SELECT action,payload,status,created FROM approvals WHERE id=?",(ident,)).fetchone()
                if not row or row[2]!="pending": raise ValueError("unknown or already used approval")
                if int(time.time()) - row[3] > 900:
                    db.execute("UPDATE approvals SET status='expired' WHERE id=?",(ident,)); db.commit()
                    raise ValueError("approval expired")
                result=execute(row[0],json.loads(row[1])); db.execute("UPDATE approvals SET status='executed' WHERE id=?",(ident,)); db.commit(); return self.send(200,result)
            if self.path.startswith("/v1/approvals/") and self.path.endswith("/deny"):
                ident=self.path.split("/")[3]; row=db.execute("SELECT status FROM approvals WHERE id=?",(ident,)).fetchone()
                if not row or row[0]!="pending": raise ValueError("unknown or already used approval")
                db.execute("UPDATE approvals SET status='denied' WHERE id=?",(ident,)); db.commit()
                return self.send(200,{"approval_id":ident,"status":"denied"})
            return self.send(404,{"error":"not found"})
        except Exception as e: return self.send(400,{"error":str(e)})

class GoogleOAuthCallback(BaseHTTPRequestHandler):
    """Local-only OAuth callback, reached through the user's SSH tunnel."""
    def log_message(self, *_): pass
    def do_GET(self):
        global google_pending_state, google_pending_code_verifier
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if params.get("error"):
            message = "Google authorization was cancelled or denied. You can close this page."
            self.send_response(400); self.end_headers(); self.wfile.write(message.encode()); return
        code, state = params.get("code", [None])[0], params.get("state", [None])[0]
        with google_auth_lock:
            valid_state = google_pending_state
            code_verifier = google_pending_code_verifier
        if not code or not state or not secrets.compare_digest(state, valid_state or ""):
            message = "Invalid or expired Google authorization request. Start authorization again."
            self.send_response(400); self.end_headers(); self.wfile.write(message.encode()); return
        try:
            flow = google_flow()
            flow.code_verifier = code_verifier
            flow.fetch_token(authorization_response=GOOGLE_REDIRECT_URI + "?" + urllib.parse.urlencode({"code": code, "state": state}))
            save_google_token(flow.credentials)
            with google_auth_lock:
                google_pending_state = None
                google_pending_code_verifier = None
        except Exception as exc:
            print(f"[google-oauth] token exchange failed: {type(exc).__name__}: {exc}", flush=True)
            message = "Google authorization could not be completed. Return to the assistant and try again."
            self.send_response(400); self.end_headers(); self.wfile.write(message.encode()); return
        message = "Google Calendar and Gmail are connected. You can close this page."
        self.send_response(200); self.send_header("Content-Type", "text/plain; charset=utf-8"); self.end_headers(); self.wfile.write(message.encode())
threading.Thread(target=watch_loop, daemon=True).start()
threading.Thread(target=lambda: ThreadingHTTPServer(("0.0.0.0", 8765), GoogleOAuthCallback).serve_forever(), daemon=True).start()
ThreadingHTTPServer(("0.0.0.0",8080),API).serve_forever()
