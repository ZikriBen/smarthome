"""Season-level SearchGram result parsing and ranking. Pure functions: no Telegram, no network.

The idea (one season-level search, re-parse every result title, validate against an episode list) comes from
the prototype on branch feature/searchgram-season-candidates (9cea2f9). This is a fresh implementation that
fixes the gaps found in its review: Hebrew episode ranges, a year between the show name and the marker, the
``1x07`` form, case-insensitive titles, season-pack detection, and a deterministic ranking.

Ranking (Ben, 2026-10-24): file size first: "smallest sufficient". A candidate is
  * BROKEN        size < hard_min_mb_per_min x runtime        never auto-picked (sample / truncated file)
  * SUFFICIENT    resolution known: resolution >= min_res
                  resolution unknown: size >= floor_mb_per_min x runtime
  * LARGE         sufficient but size > max_mb_per_min x runtime (remux-sized outlier)
  * LOW           known resolution below min_res, or unknown resolution under the floor
  * UNKNOWN_SIZE  no parsable size
Tier order: SUFFICIENT < LARGE < LOW < UNKNOWN_SIZE (BROKEN excluded). Inside a tier the smallest wins; files
within size_tie_pct of each other are a tie, broken by: higher resolution, Hebrew-subtitle marker, the season's
majority release group, then SearchGram result order (earlier first; used as a recency proxy).
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field

_HEB = "\u0590-\u05ff"
_HEB_RE = re.compile(f"[{_HEB}]")
_LATIN_RE = re.compile(r"[a-z]", re.I)


def has_hebrew(text: str) -> bool:
    return bool(_HEB_RE.search(text or ""))


def has_latin(text: str) -> bool:
    return bool(_LATIN_RE.search(text or ""))


# --------------------------------------------------------------------------- normalisation

_QUOTES = dict.fromkeys(map(ord, "'\"`\u2019\u2018\u05f3\u05f4\u201c\u201d"), None)


def norm(text: str) -> str:
    """casefold; quotes/geresh removed; every separator except '-' becomes one space."""
    text = unicodedata.normalize("NFKC", text or "").casefold().translate(_QUOTES)
    text = re.sub(r"[^\w\-]+|_", " ", text)
    text = re.sub(r"\s*-\s*", "-", text)          # "פ1 - 2" -> "פ1-2"; keeps hyphenated names joined
    return re.sub(r"\s+", " ", text).strip()


def title_key(text: str) -> str:
    """Comparison key for show titles: letters and digits only."""
    return "".join(c for c in norm(text) if c.isalnum())


# --------------------------------------------------------------------------- size / resolution / hints

_SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kb|mb|gb|tb)\b", re.I)
_UNIT_MB = {"kb": 1 / 1024, "mb": 1.0, "gb": 1024.0, "tb": 1024.0 * 1024}


def parse_size_mb(size: str | None) -> float | None:
    if not size:
        return None
    match = _SIZE_RE.search(str(size))
    if not match:
        return None
    return float(match.group(1).replace(",", ".")) * _UNIT_MB[match.group(2).lower()]


def format_mb(mb: float | None) -> str | None:
    if mb is None:
        return None
    return f"{mb / 1024:.2f} GB" if mb >= 1024 else f"{mb:.0f} MB"


_RES_RE = re.compile(r"(?<![0-9])(2160|1080|720|576|480|360)[pi](?![a-z0-9])|(?<![a-z0-9])(4k|uhd)(?![a-z0-9])")


def parse_resolution(n: str) -> int | None:
    best = None
    for match in _RES_RE.finditer(n):
        value = 2160 if match.group(2) else int(match.group(1))
        best = value if best is None else max(best, value)
    return best


_HEB_SUBS_RE = re.compile(r"מתורגם|תרגום|כתוביות|hebsub|heb-?sub|hebrew|(?<![a-z])heb(?![a-z])|(?:^| )ת מ(?: |$)")
_DUB_RE = re.compile(r"מדובב|dubbed|(?<![a-z])dub(?![a-z])")
_NOISE_TOKENS = re.compile(r"(?<![0-9])(?:2160|1080|720|576|480|360)[pi]|4k|uhd|web[ -]?dl|web[ -]?rip|hdtv|"
                           r"bluray|x26[45]|"
                           r"h26[45]|hevc|aac|mkv|mp4|avi|ת מ|מתורגם|מדובב|תרגום|כתוביות|hebsub|heb", re.I)


# --------------------------------------------------------------------------- episode markers

_SEP = r"[ \-]*"
# Each marker: (kind, regex). Groups: s=season, a=first episode, b=last episode (optional).
_MARKERS = [
    ("sxe", re.compile(r"(?<![a-z0-9])s(?P<s>\d{1,2})" + _SEP + r"e(?P<a>\d{1,3})"
                       r"(?:(?:-e?|e)(?P<b>\d{1,3}))?(?![0-9p])")),
    ("x", re.compile(r"(?<![a-z0-9])(?P<s>\d{1,2})x(?P<a>\d{1,3})(?:-(?:\d{1,2}x)?(?P<b>\d{1,3}))?(?![0-9p])")),
    ("en_verbose", re.compile(r"(?<![a-z])season ?(?P<s>\d{1,2})" + _SEP + r"(?:episodes?|eps?) ?(?P<a>\d{1,3})"
                              r"(?: ?(?:-|to) ?(?P<b>\d{1,3}))?(?![0-9])")),
    ("he", re.compile(rf"(?<![{_HEB}])(?:עונה|ע) ?(?P<s>\d{{1,2}})" + _SEP +
                      r"(?:פרקים|פרק|פ) ?(?P<a>\d{1,3})(?: ?(?:-|עד ) ?(?:פ ?)?(?P<b>\d{1,3}))?(?![0-9])")),
]
_SEASON_ONLY = [
    re.compile(r"(?<![a-z0-9])s(?P<s>\d{1,2})(?![0-9]|[ \-]*e[0-9])"),
    re.compile(r"(?<![a-z])season ?(?P<s>\d{1,2})(?![0-9])"),
    re.compile(rf"(?<![{_HEB}])(?:עונה|ע) ?(?P<s>\d{{1,2}})(?![0-9])"),
]
_COMPLETE_RE = re.compile(r"complete|full season|מלאה|כל הפרקים|כל העונה")


@dataclass
class Marker:
    season: int
    episodes: list[int] | None      # None = season pack without an episode list
    start: int                      # index in the normalised title
    end: int
    multi: bool = False
    pack: bool = False


def find_marker(n: str, pack_min_range: int = 4) -> Marker | None:
    """First season/episode marker in a normalised title. Ranges map to every covered episode."""
    best = None
    for _kind, rx in _MARKERS:
        match = rx.search(n)
        if match and (best is None or match.start() < best.start()):
            best = match
    if best is not None:
        season, first = int(best.group("s")), int(best.group("a"))
        last = int(best.group("b")) if best.group("b") else first
        if last < first or last - first > 60:
            last = first
        episodes = list(range(first, last + 1))
        pack = len(episodes) >= pack_min_range or bool(_COMPLETE_RE.search(n))
        return Marker(season, episodes, best.start(), best.end(), multi=len(episodes) > 1 and not pack, pack=pack)
    for rx in _SEASON_ONLY:
        match = rx.search(n)
        if match:
            return Marker(int(match.group("s")), None, match.start(), match.end(), pack=True)
    return None


# --------------------------------------------------------------------------- show-title match

# Between the show title and the marker only separators, a year and quality tags are allowed.
_GAP_TOKEN = r"(?:\(?(?:19|20)\d{2}\)?|\d{3,4}[pi]|\d{3,4}x\d{3,4}|4k|uhd)"
_GAP_OK = re.compile(rf"^[ \-]*(?:{_GAP_TOKEN}[ \-]*)*$")


def _show_regex(show: str) -> re.Pattern | None:
    tokens = norm(show).replace("-", " ").split()
    if not tokens:
        return None
    body = r"[ \-]+".join(re.escape(t) for t in tokens)
    return re.compile(rf"(?<![\w]){body}(?![\w])")


def match_show(n: str, marker: Marker, shows: list[str]) -> tuple[str, str] | None:
    """(matched show, text before it) when a show title ends right before the marker.

    Only separators and an optional year may sit between the title and the marker, so "בית הנייר קוריאה ע1"
    (a different show) does not match "בית הנייר". Anything may precede the title (uploader prefixes)."""
    for show in shows:
        rx = _show_regex(show)
        if rx is None:
            continue
        for match in rx.finditer(n, 0, marker.start + 1):
            if match.end() <= marker.start and _GAP_OK.match(n[match.end():marker.start]):
                return show, n[:match.start()]
    return None


def _group(prefix: str, suffix: str) -> str | None:
    for part in (prefix, suffix):
        cleaned = re.sub(r"\s+", " ", _NOISE_TOKENS.sub(" ", part).replace("-", " ")).strip()
        cleaned = re.sub(r"(?<![\w])(?:19|20)\d{2}(?![\w])", "", cleaned).strip()
        if cleaned and len(cleaned.split()) <= 3:
            return cleaned
    return None


# --------------------------------------------------------------------------- candidates

@dataclass
class Candidate:
    id: str
    title: str
    size: str | None
    size_mb: float | None
    res: int | None
    heb: bool
    dubbed: bool
    group: str | None
    season: int
    episodes: list[int] | None
    kind: str                       # episode | multi | pack
    order: int
    raw: dict = field(default_factory=dict)   # message_id / callback_data / page / page_callback / query
    tier: str = ""
    mb_per_min: float | None = None


def parse_item(item: dict, *, season: int, shows: list[str], order: int, pack_min_range: int = 4) -> Candidate | None:
    title = str(item.get("title") or "")
    n = norm(title)
    marker = find_marker(n, pack_min_range)
    if marker is None or marker.season != season:
        return None
    matched = match_show(n, marker, shows)
    if matched is None:
        return None
    _show, prefix = matched
    kind = "pack" if marker.pack else ("multi" if marker.multi else "episode")
    size_mb = parse_size_mb(item.get("size"))
    return Candidate(id="", title=title, size=item.get("size"), size_mb=size_mb, res=parse_resolution(n),
                     heb=bool(_HEB_SUBS_RE.search(n)), dubbed=bool(_DUB_RE.search(n)),
                     group=_group(prefix, n[marker.end:]), season=season,
                     episodes=None if marker.episodes is None else list(marker.episodes),
                     kind=kind, order=order, raw={k: item.get(k) for k in
                                                  ("message_id", "callback_data", "page", "page_callback", "query")})


# --------------------------------------------------------------------------- ranking

@dataclass
class RankConfig:
    min_res: int = 720
    floor_mb_per_min: float = 4.0
    hard_min_mb_per_min: float = 1.5
    max_mb_per_min: float = 60.0
    default_runtime_min: float = 45.0
    size_tie_pct: float = 10.0
    pack_min_range: int = 4

    @classmethod
    def from_env(cls, env=os.environ) -> "RankConfig":
        def num(name, default, cast=float):
            try:
                return cast(env.get(name, default))
            except (TypeError, ValueError):
                return default
        return cls(min_res=num("SEASON_MIN_RES", 720, int),
                   floor_mb_per_min=num("SEASON_FLOOR_MB_PER_MIN", 4.0),
                   hard_min_mb_per_min=num("SEASON_HARD_MIN_MB_PER_MIN", 1.5),
                   max_mb_per_min=num("SEASON_MAX_MB_PER_MIN", 60.0),
                   default_runtime_min=num("SEASON_DEFAULT_RUNTIME_MIN", 45.0),
                   size_tie_pct=num("SEASON_SIZE_TIE_PCT", 10.0),
                   pack_min_range=num("SEASON_PACK_MIN_RANGE", 4, int))

    def as_dict(self) -> dict:
        return dict(self.__dict__)


TIERS = ("sufficient", "large", "low", "unknown_size")
_TIER_INDEX = {name: i for i, name in enumerate(TIERS)}


def classify(c: Candidate, runtime_min: float, cfg: RankConfig) -> str:
    if c.size_mb is None:
        return "unknown_size"
    runtime_min = runtime_min if runtime_min and runtime_min > 0 else cfg.default_runtime_min
    c.mb_per_min = round(c.size_mb / runtime_min, 1)
    if c.mb_per_min < cfg.hard_min_mb_per_min:
        return "broken"
    if c.res is not None and cfg.min_res:
        sufficient = c.res >= cfg.min_res
    else:
        sufficient = c.mb_per_min >= cfg.floor_mb_per_min
    if not sufficient:
        return "low"
    return "large" if c.mb_per_min > cfg.max_mb_per_min else "sufficient"


def _bucketed(cands: list[Candidate], tie_pct: float) -> dict[str, int]:
    buckets, start, index = {}, None, -1
    for c in sorted(cands, key=lambda x: (x.size_mb is None, x.size_mb or 0, x.order)):
        if c.size_mb is None:
            buckets[c.id] = 10 ** 6
            continue
        if start is None or c.size_mb > start * (1 + tie_pct / 100.0):
            start, index = c.size_mb, index + 1
        buckets[c.id] = index
    return buckets


def _rank(cands: list[Candidate], cfg: RankConfig, majority: str | None) -> list[Candidate]:
    out = []
    for tier in TIERS:
        group = [c for c in cands if c.tier == tier]
        buckets = _bucketed(group, cfg.size_tie_pct)
        group.sort(key=lambda c: (buckets[c.id], -(c.res or 0), not c.heb,
                                  not (majority and c.group == majority), c.order))
        out.extend(group)
    return out


def _why(c: Candidate, cfg: RankConfig, majority: str | None, alternatives: int) -> str:
    size = format_mb(c.size_mb) or "size unknown"
    rate = f", {c.mb_per_min:g} MB/min" if c.mb_per_min is not None else ""
    res = f"{c.res}p" if c.res else "resolution unknown"
    head = {
        "sufficient": f"smallest sufficient ({size}{rate}, {res})",
        "large": f"only remux-sized files ({size}{rate}, above {cfg.max_mb_per_min:g} MB/min)",
        "low": (f"best available but below {cfg.min_res}p ({size}, {res})" if c.res
                else f"best available but under the size floor ({size}{rate})"),
        "unknown_size": "size unknown",
    }[c.tier]
    bits = [head]
    if c.kind == "multi":
        bits.append(f"multi-episode file E{c.episodes[0]}-E{c.episodes[-1]} (no single-episode file)")
    if c.heb:
        bits.append("Hebrew subtitles")
    if c.dubbed:
        bits.append("dubbed")
    if majority and c.group == majority:
        bits.append("same release group as the season")
    if alternatives:
        bits.append(f"{alternatives} alternative(s)")
    return "; ".join(bits)


def rank_season(items: list[dict], *, season: int, shows: list[str], episodes: list[dict] | None,
                have: list[int] = (), cfg: RankConfig | None = None) -> dict:
    """Parse, validate and rank. ``episodes``: [{"n": int, "runtime": minutes|None}] or None when unknown."""
    cfg = cfg or RankConfig()
    have = set(int(x) for x in have or ())
    known = None if episodes is None else {int(e["n"]): e.get("runtime") for e in episodes}
    cands: list[Candidate] = []
    seen = set()
    for order, item in enumerate(items):
        key = item.get("callback_data") or (item.get("title"), item.get("size"))
        if key in seen:
            continue
        seen.add(key)
        c = parse_item(item, season=season, shows=shows, order=order, pack_min_range=cfg.pack_min_range)
        if c is not None:
            c.id = f"c{len(cands) + 1}"
            cands.append(c)

    def runtime(eps):
        if not eps:
            return cfg.default_runtime_min * max(1, len(known or ()) or 1)
        total = 0.0
        for e in eps:
            r = (known or {}).get(e)
            total += r if r else cfg.default_runtime_min
        return total

    outside: set[int] = set()
    usable: list[Candidate] = []
    packs: list[Candidate] = []
    for c in cands:
        if c.kind == "pack":
            c.tier = classify(c, runtime(c.episodes or list((known or {}).keys())), cfg)
            packs.append(c)
            continue
        if known is not None:
            bad = [e for e in c.episodes if e not in known]
            if bad:
                outside.update(bad)
                continue
        c.tier = classify(c, runtime(c.episodes), cfg)
        usable.append(c)

    observed = sorted({e for c in usable for e in c.episodes})
    target = sorted(known) if known is not None else observed
    needed = [e for e in target if e not in have]

    def pickable(c):
        return c.tier in _TIER_INDEX

    singles = {e: [c for c in usable if c.kind == "episode" and c.episodes == [e] and pickable(c)] for e in needed}
    # Majority release group of the provisional (group-blind) single-episode picks.
    provisional = [_rank(lst, cfg, None)[0] for lst in singles.values() if lst]
    counts: dict[str, int] = {}
    for c in provisional:
        if c.group:
            counts[c.group] = counts.get(c.group, 0) + 1
    majority = max(sorted(counts), key=lambda g: counts[g]) if counts else None
    if majority and counts[majority] < 2:
        majority = None

    picks: dict[int, dict] = {}
    ranked: dict[int, list[Candidate]] = {}
    for e in needed:
        lst = _rank(singles[e], cfg, majority)
        ranked[e] = lst
        if lst and lst[0].tier in ("sufficient", "large"):
            picks[e] = {"ep": e, "id": lst[0].id, "why": _why(lst[0], cfg, majority, len(lst) - 1)}
    # Episodes without a sufficient single: a sufficient multi-episode file, else the best low/unknown single.
    multis = [c for c in usable if c.kind == "multi" and pickable(c)]
    for e in needed:
        if e in picks:
            continue
        covering = _rank([c for c in multis if e in c.episodes], cfg, majority)
        good_multi = next((c for c in covering if c.tier in ("sufficient", "large")), None)
        if good_multi is not None:
            for covered in good_multi.episodes:
                if covered in needed and covered not in picks:
                    picks[covered] = {"ep": covered, "id": good_multi.id,
                                      "why": _why(good_multi, cfg, majority, len(covering) - 1)}
            continue
        fallback = (ranked.get(e) or []) + covering
        fallback = _rank(fallback, cfg, majority)
        if fallback:
            best = fallback[0]
            for covered in best.episodes:
                if covered in needed and covered not in picks:
                    picks[covered] = {"ep": covered, "id": best.id, "why": _why(best, cfg, majority, len(fallback) - 1)}

    best_pack = None
    if packs:
        ordered = _rank([p for p in packs if p.tier in _TIER_INDEX] or packs, cfg, majority)
        p = ordered[0]
        best_pack = {"id": p.id, "title": p.title, "size": format_mb(p.size_mb) or p.size,
                     "covers": "season" if p.episodes is None else f"E{p.episodes[0]}-E{p.episodes[-1]}",
                     "count": len(packs)}

    missing = [e for e in needed if e not in picks]
    gaps = []
    if known is None and observed:
        gaps = [e for e in range(1, observed[-1] + 1) if e not in observed and e not in have]
    return {
        "candidates": [_cand_dict(c) for c in cands],
        "picks": [picks[e] for e in sorted(picks)],
        "missing": missing,
        "needed": needed,
        "observed": observed,
        "gaps": gaps,
        "outside_list": sorted(outside),
        "pack": best_pack,
        "majority_group": majority,
        "config": cfg.as_dict(),
    }


def _cand_dict(c: Candidate) -> dict:
    return {"id": c.id, "title": c.title, "size": format_mb(c.size_mb) or c.size, "size_mb": c.size_mb,
            "res": c.res, "heb": c.heb, "dubbed": c.dubbed, "group": c.group, "season": c.season,
            "episodes": c.episodes, "kind": c.kind, "tier": c.tier or None, "mb_per_min": c.mb_per_min,
            "order": c.order, **c.raw}
