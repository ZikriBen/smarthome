"""Allowlisted Docker reads and approved actions."""

import json
import socket
import urllib.parse

from . import approvals


def request(method, path):
    connection = socket.socket(socket.AF_UNIX)
    connection.connect("/var/run/docker.sock")
    connection.sendall(f"{method} {path} HTTP/1.0\r\nHost: docker\r\n\r\n".encode())
    response = b""
    while True:
        chunk = connection.recv(65536)
        if not chunk:
            break
        response += chunk
    status = int(response.split(b" ", 2)[1])
    body = response.split(b"\r\n\r\n", 1)[1]
    if status >= 300:
        raise RuntimeError(f"Docker returned {status}")
    return json.loads(body or b"{}")


def containers():
    return [{"id": item["Id"][:12], "name": item["Names"][0].lstrip("/"),
             "image": item["Image"], "state": item["State"], "status": item["Status"]}
            for item in request("GET", "/containers/json?all=1")]


def restart_container(payload):
    container = payload["container"]
    request("POST", f"/containers/{urllib.parse.quote(container, safe='')}/restart?t=20")
    return {"restarted": container}


def register_routes(router):
    router.get("/v1/docker/containers", lambda _p, _q: containers())
    router.post("/v1/proposals/docker-restart", lambda p, _q: {
        "approval_id": approvals.create("docker_restart", {"container": p["container"]}),
        "status": "pending"}, 202)


def register_actions():
    approvals.register_action("docker_restart", restart_container)
