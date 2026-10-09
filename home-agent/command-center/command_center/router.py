"""Small exact-path router used by integration modules."""

import urllib.parse


class Router:
    def __init__(self):
        self._routes = {}

    def get(self, path, handler, status=200):
        self._add("GET", path, handler, status)

    def post(self, path, handler, status=200):
        self._add("POST", path, handler, status)

    def _add(self, method, path, handler, status):
        key = (method, path)
        if key in self._routes:
            raise RuntimeError(f"route already registered: {method} {path}")
        self._routes[key] = (status, handler)

    def dispatch(self, method, raw_path, payload=None):
        parsed = urllib.parse.urlparse(raw_path)
        route = self._routes.get((method, parsed.path))
        if route is None:
            return None
        status, handler = route
        query = urllib.parse.parse_qs(parsed.query)
        return status, handler(payload or {}, query)
