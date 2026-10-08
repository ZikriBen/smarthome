"""Small, allowlisted Command Center. It intentionally has no shell endpoint."""
import base64, json, os, re, socket, sqlite3, threading, time, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN = os.environ["COMMAND_CENTER_TOKEN"]
KUMA_URL = os.environ.get("UPTIME_KUMA_URL", "").rstrip("/")
KUMA_API_KEY = os.environ.get("UPTIME_KUMA_API_KEY", "")
DB = Path("/data/command-center.sqlite3")
DB.parent.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DB, check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, action TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, created INTEGER NOT NULL)")
db.execute("CREATE TABLE IF NOT EXISTS watches (id TEXT PRIMARY KEY, url TEXT NOT NULL, target REAL NOT NULL, currency TEXT NOT NULL, every_minutes INTEGER NOT NULL, enabled INTEGER NOT NULL, last_price REAL, last_checked INTEGER)")
db.execute("CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, watch_id TEXT NOT NULL, price REAL NOT NULL, created INTEGER NOT NULL)")
db.commit()

PRIVATE = ("10.", "127.", "192.168.", "169.254.", "100.")
def public_url(value):
    u = urllib.parse.urlparse(value)
    if u.scheme not in ("http", "https") or not u.hostname: raise ValueError("URL must be public http(s)")
    for addr in socket.getaddrinfo(u.hostname, None):
        ip = addr[4][0]
        if ip.startswith(PRIVATE) or ip.startswith("172.") or ip.startswith("fc") or ip.startswith("fe80:"):
            raise ValueError("private destinations are not allowed")
    return value
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
    html=urllib.request.urlopen(req, timeout=20).read(1_500_000).decode("utf-8","ignore")
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
def audit(action, payload):
    ident=os.urandom(9).hex(); db.execute("INSERT INTO approvals VALUES (?,?,?,?,?)",(ident,action,json.dumps(payload),"pending",int(time.time()))); db.commit(); return ident
def execute(action, p):
    if action == "docker_restart": docker("POST",f"/containers/{urllib.parse.quote(p['container'],safe='')}/restart?t=20"); return {"restarted":p["container"]}
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
    def do_GET(self):
        if not self.auth(): return self.send(401,{"error":"unauthorized"})
        try:
            if self.path=="/v1/health":
                m={k:v for k,v in (x.split(":",1) for x in Path("/host/proc/meminfo").read_text().splitlines() if ":" in x)}
                return self.send(200,{"uptime_seconds":int(float(Path("/host/proc/uptime").read_text().split()[0])),"loadavg":Path("/host/proc/loadavg").read_text().split()[:3],"memory_available_kib":int(m["MemAvailable"].split()[0])})
            if self.path=="/v1/docker/containers":
                return self.send(200,[{"id":x["Id"][:12],"name":x["Names"][0].lstrip("/"),"image":x["Image"],"state":x["State"],"status":x["Status"]} for x in docker("GET","/containers/json?all=1")])
            if self.path=="/v1/uptime-kuma/monitors": return self.send(200,kuma_status())
            if self.path=="/v1/price-watches": return self.send(200,[dict(zip(["id","url","target","currency","every_minutes","enabled","last_price","last_checked"],r)) for r in db.execute("SELECT * FROM watches")])
            if self.path=="/v1/price-alerts": return self.send(200,[dict(zip(["watch_id","price","created"],r)) for r in db.execute("SELECT watch_id,price,created FROM alerts ORDER BY id DESC LIMIT 100")])
            return self.send(404,{"error":"not found"})
        except Exception as e: return self.send(502,{"error":str(e)})
    def do_POST(self):
        if not self.auth(): return self.send(401,{"error":"unauthorized"})
        try:
            p=self.body()
            if self.path=="/v1/maps/search":
                q=urllib.parse.quote(p["query"]); req=urllib.request.Request(f"https://nominatim.openstreetmap.org/search?q={q}&format=jsonv2&limit=5",headers={"User-Agent":"CommandCenter/1.0"}); return self.send(200,json.loads(urllib.request.urlopen(req,timeout=20).read()))
            if self.path=="/v1/price-watches/quote": return self.send(200,{"price":price(p["url"])})
            if self.path=="/v1/proposals/docker-restart": return self.send(202,{"approval_id":audit("docker_restart",{"container":p["container"]}),"status":"pending"})
            if self.path=="/v1/proposals/price-watch":
                watch={"id":os.urandom(8).hex(),"url":public_url(p["url"]),"target":float(p["target"]),"currency":p.get("currency","USD"),"every_minutes":max(60,int(p.get("every_minutes",360)))}
                watch["baseline_price"]=price(watch["url"]); return self.send(202,{"approval_id":audit("price_watch_create",watch),"proposal":watch})
            if self.path.startswith("/v1/approvals/") and self.path.endswith("/approve"):
                ident=self.path.split("/")[3]; row=db.execute("SELECT action,payload,status FROM approvals WHERE id=?",(ident,)).fetchone()
                if not row or row[2]!="pending": raise ValueError("unknown or already used approval")
                result=execute(row[0],json.loads(row[1])); db.execute("UPDATE approvals SET status='executed' WHERE id=?",(ident,)); db.commit(); return self.send(200,result)
            return self.send(404,{"error":"not found"})
        except Exception as e: return self.send(400,{"error":str(e)})
threading.Thread(target=watch_loop, daemon=True).start()
ThreadingHTTPServer(("0.0.0.0",8080),API).serve_forever()
