"""Public price lookup and approved price-watch capabilities."""

import ipaddress
import os
import re
import socket
import threading
import time
import urllib.parse
import urllib.request

from . import approvals
from .database import connection


def public_url(value):
    parsed = urllib.parse.urlparse(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password):
        raise ValueError("URL must be a public http(s) URL without credentials")
    for address in socket.getaddrinfo(parsed.hostname, None):
        if not ipaddress.ip_address(address[4][0]).is_global:
            raise ValueError("private destinations are not allowed")
    return value


class PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        public_url(new_url)
        return super().redirect_request(
            request, file_pointer, code, message, headers, new_url)


PUBLIC_URL_OPENER = urllib.request.build_opener(PublicRedirectHandler())


def quote(url):
    public_url(url)
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; CommandCenter/1.0)"})
    html = PUBLIC_URL_OPENER.open(request, timeout=20).read(1_500_000).decode(
        "utf-8", "ignore")
    matches = re.findall(
        r'(?:product:price:amount|"price"|itemprop=["\']price["\'])[^>]{0,180}?'
        r'content=["\']?([0-9]+(?:[.,][0-9]{1,2})?)|'
        r'"price"\s*:\s*"?([0-9]+(?:\.[0-9]{1,2})?)', html, re.I)
    values = [float((first or second).replace(",", ""))
              for first, second in matches if first or second]
    if not values:
        raise ValueError("no machine-readable public price found")
    return min(values)


def watches():
    columns = ["id", "url", "target", "currency", "every_minutes", "enabled",
               "last_price", "last_checked"]
    return [dict(zip(columns, row)) for row in connection.execute("SELECT * FROM watches")]


def alerts():
    columns = ["watch_id", "price", "created"]
    return [dict(zip(columns, row)) for row in connection.execute(
        "SELECT watch_id,price,created FROM alerts ORDER BY id DESC LIMIT 100")]


def propose(payload):
    watch = {"id": os.urandom(8).hex(), "url": public_url(payload["url"]),
             "target": float(payload["target"]), "currency": payload.get("currency", "USD"),
             "every_minutes": max(60, int(payload.get("every_minutes", 360)))}
    watch["baseline_price"] = quote(watch["url"])
    return {"approval_id": approvals.create("price_watch_create", watch), "proposal": watch}


def create(payload):
    connection.execute("INSERT INTO watches VALUES (?,?,?,?,?,?,?,?)", (
        payload["id"], payload["url"], payload["target"], payload["currency"],
        payload["every_minutes"], 1, None, None))
    connection.commit()
    return {"watch_id": payload["id"]}


def watch_loop():
    while True:
        now = int(time.time())
        rows = connection.execute(
            "SELECT id,url,target,every_minutes,last_checked FROM watches WHERE enabled=1"
        ).fetchall()
        for ident, url, target, every, last_checked in rows:
            if last_checked and now - last_checked < every * 60:
                continue
            try:
                observed = quote(url)
                connection.execute("UPDATE watches SET last_price=?,last_checked=? WHERE id=?",
                                   (observed, now, ident))
                if observed <= target:
                    previous = connection.execute(
                        "SELECT price FROM alerts WHERE watch_id=? ORDER BY id DESC LIMIT 1",
                        (ident,)).fetchone()
                    if not previous or previous[0] != observed:
                        connection.execute(
                            "INSERT INTO alerts(watch_id,price,created) VALUES (?,?,?)",
                            (ident, observed, now))
                connection.commit()
            except Exception:
                pass
        time.sleep(60)


def start_background_tasks():
    threading.Thread(target=watch_loop, daemon=True).start()


def register_routes(router):
    router.get("/v1/price-watches", lambda _p, _q: watches())
    router.get("/v1/price-alerts", lambda _p, _q: alerts())
    router.post("/v1/price-watches/quote", lambda p, _q: {"price": quote(p["url"])})
    router.post("/v1/proposals/price-watch", lambda p, _q: propose(p), 202)


def register_actions():
    approvals.register_action("price_watch_create", create)
