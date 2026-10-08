import json, os, urllib.request
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

BASE = "http://command-center:8080/v1"
TOKEN = os.environ["COMMAND_CENTER_TOKEN"]
mcp = FastMCP(
    "Home Command Center",
    host="0.0.0.0",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=["command-center-mcp:8000"],
    ),
)

def request(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
        headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)

@mcp.tool()
def system_health() -> dict:
    """Read the home server's uptime, load average, and available memory."""
    return request("GET", "/health")

@mcp.tool()
def docker_containers() -> list[dict]:
    """List Docker containers and their state. This is read-only."""
    return request("GET", "/docker/containers")

@mcp.tool()
def map_search(query: str) -> list[dict]:
    """Find a place using OpenStreetMap. This is read-only."""
    return request("POST", "/maps/search", {"query": query})

@mcp.tool()
def price_watch_status() -> dict:
    """List price watches and recent price alerts. This is read-only."""
    return {"watches": request("GET", "/price-watches"), "alerts": request("GET", "/price-alerts")}

@mcp.tool()
def price_quote(url: str) -> dict:
    """Read a machine-readable price from a public product page; private URLs are rejected."""
    return request("POST", "/price-watches/quote", {"url": url})

@mcp.tool()
def propose_docker_restart(container: str) -> dict:
    """Create, but do not execute, a Docker container restart proposal requiring human approval."""
    return request("POST", "/proposals/docker-restart", {"container": container})

@mcp.tool()
def propose_price_watch(url: str, target: float, currency: str = "USD", every_minutes: int = 360) -> dict:
    """Create, but do not execute, a price-watch proposal requiring human approval."""
    return request("POST", "/proposals/price-watch", {"url": url, "target": target, "currency": currency, "every_minutes": every_minutes})

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
