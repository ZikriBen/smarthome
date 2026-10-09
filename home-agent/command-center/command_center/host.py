"""Read-only host health capability."""

from pathlib import Path


def health():
    memory = {key: value for key, value in
              (line.split(":", 1) for line in Path("/host/proc/meminfo").read_text().splitlines()
               if ":" in line)}
    return {"uptime_seconds": int(float(Path("/host/proc/uptime").read_text().split()[0])),
            "loadavg": Path("/host/proc/loadavg").read_text().split()[:3],
            "memory_available_kib": int(memory["MemAvailable"].split()[0])}


def register_routes(router):
    router.get("/v1/health", lambda _p, _q: health())
