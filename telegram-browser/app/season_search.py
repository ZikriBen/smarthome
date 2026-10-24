"""Season-level SearchGram scan: a few query variants, bounded paging, every page's results kept addressable.

Every result keeps the ``message_id`` it was shown on plus the page callback (``search#<query>#<page>``) so an
item from an earlier page can be queued after paging on (``/api/search/download`` re-opens that page once if the
direct callback is refused). Credits: the season-level search idea is from 9cea2f9.
"""

from __future__ import annotations

import asyncio
import os
import re
import time

from app.season_parse import RankConfig, has_hebrew, has_latin, norm, rank_season


def _num(name, default, cast=float):
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


MAX_PAGES_PER_QUERY = _num("SEASON_MAX_PAGES_PER_QUERY", 6, int)
MAX_PAGES_TOTAL = _num("SEASON_MAX_PAGES_TOTAL", 14, int)
PAGE_DELAY_SECONDS = _num("SEASON_PAGE_DELAY_SECONDS", 1.0)
# SearchGram.search waits up to 15 s for the bot's reply and navigate up to 10 s plus a fallback read; the
# per-step guard sits above both. The whole scan stays well inside Command Center's 200 s call timeout.
STEP_TIMEOUT_SECONDS = _num("SEASON_STEP_TIMEOUT_SECONDS", 40.0)
BUDGET_SECONDS = _num("SEASON_BUDGET_SECONDS", 150.0)
MAX_QUERIES = 4


def build_queries(titles: list[str], season: int) -> list[str]:
    """Hebrew variants first (better odds of Hebrew subtitles), then English. At most MAX_QUERIES."""
    heb, lat, seen = [], [], set()
    for title in titles:
        title = re.sub(r"\s+", " ", str(title or "")).strip()
        key = norm(title)
        if not title or key in seen:
            continue
        seen.add(key)
        if has_hebrew(title):
            heb += [f"{title} עונה {season}", f"{title} ע{season}"]
        elif has_latin(title):
            lat.append(f"{title} S{season:02d}")
    return list(dict.fromkeys(heb[:3] + lat[:2] + heb[3:]))[:MAX_QUERIES]


def _target(callback: str) -> int | None:
    try:
        return int(str(callback).rsplit("#", 1)[1])
    except (IndexError, ValueError):
        return None


def _page_dict(page) -> dict:
    return page.to_dict() if hasattr(page, "to_dict") else dict(page)


async def scan(service, queries: list[str], *, done=None, sleep=asyncio.sleep, clock=time.monotonic,
               max_pages_per_query=None, max_pages_total=None, budget_seconds=None, step_timeout=None) -> dict:
    """Run the query variants in order; stop early once ``done(items)`` is true after a finished query."""
    max_pages_per_query = max_pages_per_query or MAX_PAGES_PER_QUERY
    max_pages_total = max_pages_total or MAX_PAGES_TOTAL
    budget_seconds = budget_seconds or BUDGET_SECONDS
    step_timeout = step_timeout or STEP_TIMEOUT_SECONDS
    started = clock()
    items, attempted, ok, errors = [], [], [], []
    pages_total, truncated = 0, False

    def over_budget():
        return clock() - started > budget_seconds

    for query in queries:
        if pages_total >= max_pages_total or over_budget():
            truncated = True
            break
        attempted.append(query)
        try:
            page = _page_dict(await asyncio.wait_for(service.search(query), step_timeout))
        except (asyncio.TimeoutError, TimeoutError):
            errors.append({"query": query, "error": "timeout"})
            continue
        except Exception as exc:  # SearchGram down / parse failure: try the next variant
            errors.append({"query": query, "error": type(exc).__name__})
            continue
        ok.append(query)
        pages_q = 0
        clicked = None          # the real callback that opened this page (Telegram may truncate the query)
        first_page_items = []
        while True:
            pages_q += 1
            pages_total += 1
            number = page.get("page") or 1
            for index, item in enumerate(page.get("items") or []):
                entry = {**item, "message_id": page.get("message_id"), "page": number, "query": query,
                         "page_callback": clicked or f"search#{query}#{number}", "index": index}
                items.append(entry)
                if number == 1:
                    first_page_items.append(entry)
            total = page.get("total_pages") or number
            if number >= total:
                break
            if pages_q >= max_pages_per_query or pages_total >= max_pages_total or over_budget():
                truncated = True
                break
            nxt = next((n.get("callback_data") for n in page.get("navigation") or []
                        if str(n.get("callback_data", "")).startswith("search#")
                        and _target(n.get("callback_data")) == number + 1), None)
            if nxt is None:
                break
            await sleep(PAGE_DELAY_SECONDS)
            try:
                page = _page_dict(await asyncio.wait_for(
                    service.navigate(message_id=page["message_id"], callback_data=nxt, query=query), step_timeout))
                clicked = nxt
                if (page.get("page") or 0) == 2:
                    back = next((n.get("callback_data") for n in page.get("navigation") or []
                                 if _target(n.get("callback_data")) == 1), None)
                    for entry in first_page_items:
                        entry["page_callback"] = back or entry["page_callback"]
            except Exception as exc:
                errors.append({"query": query, "page": number + 1,
                               "error": "timeout" if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                               else type(exc).__name__})
                truncated = True
                break
        if done is not None and done(items):
            break
    return {"items": items, "queries_attempted": attempted, "queries_ok": ok, "errors": errors,
            "pages_fetched": pages_total, "truncated": truncated,
            "elapsed_seconds": round(clock() - started, 1)}


async def find_season(service, *, titles: list[str], season: int, episodes: list[dict] | None,
                      have: list[int] = (), cfg: RankConfig | None = None, **scan_kwargs) -> dict:
    cfg = cfg or RankConfig.from_env()
    queries = build_queries(titles, season)
    if not queries:
        raise ValueError("no usable title")

    def done(items):
        if episodes is None:
            return False
        ranked = rank_season(items, season=season, shows=titles, episodes=episodes, have=have, cfg=cfg)
        good = {c["id"] for c in ranked["candidates"] if c["tier"] in ("sufficient", "large")}
        return not ranked["missing"] and all(p["id"] in good for p in ranked["picks"])

    result = await scan(service, queries, done=done, **scan_kwargs)
    ranked = rank_season(result.pop("items"), season=season, shows=titles, episodes=episodes, have=have, cfg=cfg)
    return {**result, **ranked}
