"""Cart links that Ben opens in his own browser. Nothing here talks to a site or touches an account.

Rami Levy  https://www.rami-levy.co.il/he/basket?items=<barcode>,<qty>;…   (fills = REPLACES the browser cart)
Shufersal  https://www.shufersal.co.il/online/he/affiliate?items=P_<code>:<qty>:true,…&title=…
           (opens a pre-ticked list; Ben presses "הוסף לסל" once; ADDS to the existing cart)
Both verified by Ben 2026-10-19. Weighed qty: Shufersal = kg (verified); Rami = kg [unverified].
"""
from __future__ import annotations

import urllib.parse
from typing import Dict, List, Optional, Tuple

RAMI_BASKET = "https://www.rami-levy.co.il/he/basket"
SHUFERSAL_AFFILIATE = "https://www.shufersal.co.il/online/he/affiliate"
MAX_URL = 3500          # a longer URL is split into "חלק 1/2" links (Telegram message limit 4096)
MAX_MESSAGE = 3800
SPLIT_MARK = "\n---\n"


def fmt_qty(q) -> str:
    """1 -> '1', 0.75 -> '0.75', 1.0 -> '1', 0.3333 -> '0.333'."""
    q = round(float(q), 3)
    return f"{q:g}" if q != int(q) else str(int(q))


def _usable(line: dict) -> Optional[Tuple[str, float]]:
    code = line.get("link_code")
    qty = line.get("link_qty", line.get("qty"))
    if not code or not qty or line.get("to_check") or line.get("price") is None:
        return None
    return str(code), float(qty)


def _collect(lines: List[dict]) -> Tuple[List[Tuple[str, float]], List[str]]:
    """(code, qty) merged per code in list order + names of items that could not be linked."""
    merged: Dict[str, float] = {}
    missing = []
    for line in lines:
        u = _usable(line)
        if u is None:
            missing.append(line.get("item") or "?")
            continue
        merged[u[0]] = round(merged.get(u[0], 0) + u[1], 3)
    return list(merged.items()), missing


def _chunks(parts: List[str], build) -> List[str]:
    """Greedy split so that every built URL stays <= MAX_URL."""
    out, cur = [], []
    for p in parts:
        if cur and len(build(cur + [p])) > MAX_URL:
            out.append(build(cur))
            cur = []
        cur.append(p)
    if cur:
        out.append(build(cur))
    return out


def rami_basket_urls(lines: List[dict]) -> Tuple[List[str], List[str]]:
    pairs, missing = _collect(lines)
    parts = [f"{c},{fmt_qty(q)}" for c, q in pairs]
    build = lambda ps: f"{RAMI_BASKET}?items=" + ";".join(ps)   # noqa: E731 — site expects raw , and ;
    return (_chunks(parts, build) if parts else []), missing


def rami_basket_url(lines: List[dict]) -> Optional[str]:
    urls, _ = rami_basket_urls(lines)
    return urls[0] if urls else None


def shufersal_affiliate_urls(lines: List[dict], title: str = "רשימת קניות") -> Tuple[List[str], List[str]]:
    pairs, missing = _collect(lines)
    parts = [f"{c if c.startswith('P_') else 'P_' + c}:{fmt_qty(q)}:true" for c, q in pairs]
    t = urllib.parse.quote(title, safe="")

    def build(ps):
        return f"{SHUFERSAL_AFFILIATE}?items=" + urllib.parse.quote(",".join(ps), safe="") + f"&title={t}"
    return (_chunks(parts, build) if parts else []), missing


def shufersal_affiliate_url(lines: List[dict], title: str = "רשימת קניות") -> Optional[str]:
    urls, _ = shufersal_affiliate_urls(lines, title)
    return urls[0] if urls else None


def product_url(site: str, code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    if site == "rami_levy":
        return f"https://www.rami-levy.co.il/he/share/{code}"
    if site == "shufersal":
        return f"https://www.shufersal.co.il/online/he/p/{code if str(code).startswith('P_') else 'P_' + str(code)}"
    return None


def site_urls(site: str, lines: List[dict], title: str = "רשימת קניות") -> Tuple[List[str], List[str]]:
    if site == "rami_levy":
        return rami_basket_urls(lines)
    if site == "shufersal":
        return shufersal_affiliate_urls(lines, title)
    raise ValueError(f"no cart link for site {site}")


def split_message(text: str, limit: int = MAX_MESSAGE) -> str:
    """Insert SPLIT_MARK between lines so that every part is <= limit. Lines (and URLs) are never cut."""
    if len(text) <= limit:
        return text
    parts, cur = [], ""
    for line in text.split("\n"):
        cand = f"{cur}\n{line}" if cur else line
        if cur and len(cand) > limit:
            parts.append(cur)
            cur = line
        else:
            cur = cand
    if cur:
        parts.append(cur)
    return SPLIT_MARK.join(parts)
