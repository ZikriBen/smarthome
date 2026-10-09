"""Authenticated HTTP client for the internal Command Center API."""

import json
import os
import urllib.request


class CommandCenterClient:
    def __init__(self, base="http://command-center:8080/v1", token=None):
        self.base = base.rstrip("/")
        self.token = token if token is not None else os.environ["COMMAND_CENTER_TOKEN"]

    def request(self, method, path, payload=None, timeout=30):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
