"""Minimal unauthenticated health checks for private infrastructure monitoring."""

from . import docker


def status():
    return True, {"status": "ok"}


def hermes_status():
    detail = docker.request("GET", "/containers/hermes/json")
    state = detail.get("State", {})
    health = state.get("Health", {}).get("Status")
    available = bool(state.get("Running")) and health != "unhealthy"
    return available, {
        "status": "ok" if available else "unavailable",
    }
