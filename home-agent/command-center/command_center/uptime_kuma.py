"""Read-only Uptime Kuma metrics capability."""

import base64
import re
import urllib.request

from .config import KUMA_API_KEY, KUMA_URL


def status():
    if not KUMA_URL or not KUMA_API_KEY:
        raise ValueError("Uptime Kuma monitoring is not configured")
    credentials = base64.b64encode((":" + KUMA_API_KEY).encode()).decode()
    request = urllib.request.Request(
        KUMA_URL + "/metrics", headers={"Authorization": "Basic " + credentials})
    metrics = urllib.request.urlopen(request, timeout=15).read(1_000_000).decode("utf-8", "replace")
    states = {0: "down", 1: "up", 2: "pending", 3: "maintenance"}
    monitors, response_times = [], {}
    for line in metrics.splitlines():
        match = re.match(r"^(monitor_status|monitor_response_time)\{(.*)\} ([0-9.eE+-]+)$", line)
        if not match:
            continue
        labels = {key: bytes(value, "utf-8").decode("unicode_escape")
                  for key, value in re.findall(
                      r'([A-Za-z_][A-Za-z0-9_]*)="((?:\\.|[^"])*)"', match.group(2))}
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
    summary = {state: sum(monitor["status"] == state for monitor in monitors)
               for state in states.values()}
    return {"summary": summary, "monitors": monitors}


def register_routes(router):
    router.get("/v1/uptime-kuma/monitors", lambda _p, _q: status())
