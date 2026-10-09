"""HTTP transport for the allowlisted Command Center capabilities."""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import (approvals, docker, files, google_workspace, host, jellyfin, maps,
               price_watches, searchgram, uptime_kuma)
from .config import CALLBACK_TOKEN, TOKEN
from .router import Router


def build_router():
    router = Router()
    for module in (host, docker, uptime_kuma, searchgram, jellyfin, files,
                   google_workspace, maps, price_watches):
        module.register_routes(router)
    return router


def register_actions():
    for module in (docker, google_workspace, price_watches):
        module.register_actions()


ROUTER = build_router()


class API(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_json(self, status, data):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")

    def authorized(self):
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def callback_authorized(self):
        return (bool(CALLBACK_TOKEN)
                and self.headers.get("X-Command-Center-Callback") == CALLBACK_TOKEN)

    def do_GET(self):
        if not self.authorized():
            return self.send_json(401, {"error": "unauthorized"})
        try:
            result = ROUTER.dispatch("GET", self.path)
            if result is None:
                return self.send_json(404, {"error": "not found"})
            status, data = result
            return self.send_json(status, data)
        except Exception as exc:
            return self.send_json(502, {"error": str(exc)})

    def do_POST(self):
        approval_match = re.fullmatch(
            r"/v1/approvals/([0-9a-f]{18})/(approve|deny)", self.path)
        callback_route = approval_match is not None
        if not self.authorized() and not (callback_route and self.callback_authorized()):
            return self.send_json(401, {"error": "unauthorized"})
        try:
            payload = self.body()
            if approval_match:
                ident, decision = approval_match.groups()
                result = approvals.approve(ident) if decision == "approve" else approvals.deny(ident)
                return self.send_json(200, result)
            result = ROUTER.dispatch("POST", self.path, payload)
            if result is None:
                return self.send_json(404, {"error": "not found"})
            status, data = result
            return self.send_json(status, data)
        except Exception as exc:
            return self.send_json(400, {"error": str(exc)})


def run():
    register_actions()
    price_watches.start_background_tasks()
    google_workspace.start_oauth_server()
    ThreadingHTTPServer(("0.0.0.0", 8080), API).serve_forever()
