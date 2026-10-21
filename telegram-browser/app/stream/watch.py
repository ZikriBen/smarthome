"""'Watch now': .strm + movie.nfo + poster.jpg writer and targeted Jellyfin refresh."""

import asyncio
import json
import os
import re
import secrets as _secrets
import shutil
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MARKER = ".tgstream.json"
MARKER_LOCK = threading.Lock()  # write_entry vs subtitle sidecar bookkeeping
_ILLEGAL = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')
MAX_NAME_BYTES = 180


def sanitize_component(name: str | None, fallback: str = "Untitled") -> str:
    text = unicodedata.normalize("NFC", str(name or ""))
    text = re.sub(r"[\t\n\r\v\f]", " ", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co"))
    text = _ILLEGAL.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    while len(text.encode("utf-8")) > MAX_NAME_BYTES:
        text = text[:-1].rstrip(" .")
    if text in ("", ".", ".."):
        return fallback
    return text


def quality_label(quality: str | None) -> str:
    q = sanitize_component(quality, fallback="")
    return q[:20]


@dataclass
class Entry:
    folder: Path
    strm: Path
    nfo: Path
    poster: Path
    name: str


def entry_paths(root: Path, movie: dict, variant: dict) -> Entry:
    title = movie.get("english_title") or movie.get("title")
    year = movie.get("year")
    base = sanitize_component(title)
    name = f"{base} ({int(year)})" if year else base
    folder = root / name
    label = quality_label(variant.get("quality"))
    stem = f"{name} - {label}" if label else name
    return Entry(folder, folder / f"{stem}.strm", folder / "movie.nfo", folder / "poster.jpg", name)


def build_nfo(movie: dict) -> bytes:
    root = ET.Element("movie")

    def add(tag, value):
        if value not in (None, ""):
            ET.SubElement(root, tag).text = str(value)

    add("title", movie.get("title"))
    add("originaltitle", movie.get("english_title"))
    add("year", movie.get("year"))
    add("rating", movie.get("imdb_rating"))
    genres = movie.get("genres")
    if genres is None:
        try:
            genres = json.loads(movie.get("genres_json") or "[]")
        except ValueError:
            genres = []
    for genre in genres or []:
        add("genre", genre)
    add("plot", movie.get("description"))
    add("lockdata", "true")
    ET.indent(root)
    return b'<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n' + ET.tostring(root, encoding="utf-8") + b"\n"


def _atomic_write(path: Path, data: bytes) -> bool:
    """Write via a hidden temp file + rename. Returns False if unchanged."""
    if path.exists() and path.read_bytes() == data:
        return False
    tmp = path.with_name(f".{path.name}.{_secrets.token_hex(4)}.tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return True


def write_entry(
    root: Path, movie: dict, variant: dict, stream_url: str, poster: bytes | None
) -> tuple[Entry, bool]:
    """Idempotent. A new folder is assembled under a hidden temp name and
    renamed into place, so Jellyfin never sees a half-written folder."""
    root.mkdir(parents=True, exist_ok=True)
    entry = entry_paths(root, movie, variant)
    if entry.folder.resolve().parent != root.resolve():
        raise ValueError("unsafe path")
    nfo = build_nfo(movie)
    strm = (stream_url.strip() + "\n").encode()
    marker_key = str(int(variant["message_id"]))
    changed = False
    if not entry.folder.exists():
        tmp = root / f".tmp-{_secrets.token_hex(6)}"
        tmp.mkdir()
        try:
            (tmp / entry.nfo.name).write_bytes(nfo)
            if poster:
                (tmp / entry.poster.name).write_bytes(poster)
            (tmp / MARKER).write_text(json.dumps({"variants": {marker_key: entry.strm.name}}))
            (tmp / entry.strm.name).write_bytes(strm)
            for p in tmp.iterdir():
                os.chmod(p, 0o644)
            os.chmod(tmp, 0o755)
            try:
                os.rename(tmp, entry.folder)
                changed = True
            except OSError:
                if not entry.folder.is_dir():  # lost a race only if it now exists
                    raise
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
        if changed:
            return entry, True
    changed |= _atomic_write(entry.nfo, nfo)
    if poster and not entry.poster.exists():
        changed |= _atomic_write(entry.poster, poster)
    marker = entry.folder / MARKER
    with MARKER_LOCK:
        try:
            data = json.loads(marker.read_text())
        except (OSError, ValueError):
            data = {"variants": {}}
        if data.get("variants", {}).get(marker_key) != entry.strm.name:
            data.setdefault("variants", {})[marker_key] = entry.strm.name
            _atomic_write(marker, json.dumps(data).encode())
    changed |= _atomic_write(entry.strm, strm)
    return entry, changed


def jellyfin_path(local: Path, root: Path, jellyfin_root: str) -> str:
    rel = local.resolve().relative_to(root.resolve())
    return str(PurePosixPath(jellyfin_root) / PurePosixPath(*rel.parts))


# --------------------------------------------------------------------- jellyfin
AUTH_SCHEME = 'MediaBrowser Token="{}"'


class JellyfinClient:
    """Minimal API client. Auth ONLY via ``Authorization: MediaBrowser Token="<key>"``
    (Jellyfin 12.1 answers 401 to the legacy Emby token header; the key is
    never put in a URL/query string and never logged)."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self._key = api_key
        self.timeout = timeout
        self._user_id: str | None = None

    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
                 timeout: float | None = None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", AUTH_SCHEME.format(self._key))
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)

    async def call(self, *args, **kwargs):
        return await asyncio.to_thread(self._request, *args, **kwargs)

    async def report_path(self, path: str, update_type: str = "Created", timeout: float | None = None) -> int:
        status, _ = await self.call("POST", "/Library/Media/Updated",
                                    body={"Updates": [{"Path": path, "UpdateType": update_type}]}, timeout=timeout)
        return status

    async def refresh_library(self, library_id: str, timeout: float | None = None) -> int:
        """Library-scoped recursive refresh (what surfaced the item in ~5 s on the NUC)."""
        status, _ = await self.call(
            "POST",
            f"/Items/{urllib.parse.quote(library_id)}/Refresh",
            params={"Recursive": "true", "MetadataRefreshMode": "Default", "ImageRefreshMode": "Default",
                    "ReplaceAllMetadata": "false", "ReplaceAllImages": "false"},
            timeout=timeout,
        )
        return status

    async def refresh_item(self, item_id: str, timeout: float | None = None) -> int:
        """Non-recursive item refresh: picks up new external subtitle files
        (ProbeProvider.HasChanged compares SubtitleFiles with the directory)."""
        status, _ = await self.call(
            "POST",
            f"/Items/{urllib.parse.quote(item_id)}/Refresh",
            params={"Recursive": "false", "MetadataRefreshMode": "Default", "ImageRefreshMode": "None",
                    "ReplaceAllMetadata": "false", "ReplaceAllImages": "false"},
            timeout=timeout,
        )
        return status

    async def refresh_all(self, timeout: float | None = None) -> int:
        """Full scan of all libraries (``POST /Library/Refresh``). Used once per
        cooldown: a library created while empty is skipped by targeted scans
        ('inaccessible or empty, skipping') until a full refresh."""
        status, _ = await self.call("POST", "/Library/Refresh", timeout=timeout)
        return status

    async def virtual_folders(self, timeout: float | None = None) -> list:
        _, data = await self.call("GET", "/Library/VirtualFolders", timeout=timeout)
        return data or []

    async def _admin_user_id(self, timeout: float | None = None) -> str | None:
        if self._user_id is None:
            _, users = await self.call("GET", "/Users", timeout=timeout)
            admins = [u for u in users or [] if (u.get("Policy") or {}).get("IsAdministrator")]
            pick = (admins or users or [{}])[0]
            self._user_id = pick.get("Id") or ""
        return self._user_id or None

    async def items(self, params: dict, timeout: float | None = None) -> list:
        """``GET /Items`` (API key, no user). If the server refuses that
        (4xx; unverified on 12.1), fall back to ``/Users/{adminId}/Items``."""
        try:
            _, data = await self.call("GET", "/Items", params=params, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                raise
            uid = await self._admin_user_id(timeout=timeout)
            if not uid:
                raise
            _, data = await self.call("GET", f"/Users/{urllib.parse.quote(uid)}/Items", params=params, timeout=timeout)
        return (data or {}).get("Items", [])

    async def count_items(self, parent_id: str, timeout: float | None = None) -> int | None:
        try:
            _, data = await self.call("GET", "/Items", params={"ParentId": parent_id, "Recursive": "true",
                                                                 "Limit": "0"}, timeout=timeout)
        except urllib.error.HTTPError:
            return None
        return int((data or {}).get("TotalRecordCount") or 0)

    async def find_by_path(self, path: str, search_term: str | None, parent_id: str | None,
                           folder: str | None = None, timeout: float | None = None) -> str | None:
        """Match by the .strm path (or, for a second quality grouped into the same
        movie as an alternate version, by its folder). Newest-first listing first
        (does not depend on how Jellyfin matches titles: the NFO title is Hebrew,
        the search term English), then a searchTerm query."""
        base = {"Recursive": "true", "IncludeItemTypes": "Movie,Video", "Fields": "Path", "Limit": "100"}
        if parent_id:
            base["ParentId"] = parent_id
        queries = [{**base, "SortBy": "DateCreated", "SortOrder": "Descending"}]
        if search_term:
            queries.append({**base, "searchTerm": search_term})
        for params in queries:
            for item in await self.items(params, timeout=timeout):
                p = item.get("Path") or ""
                if p == path or (folder and str(PurePosixPath(p).parent) == folder):
                    return item.get("Id")
        return None


class LibraryState:
    """Process-wide: the streams library id (env, else discovered by path and
    cached) and the guard for the one-off full refresh."""

    def __init__(self, configured_id: str | None, streams_path: str, full_refresh_cooldown: float = 900.0,
                 clock=time.monotonic):
        self.configured_id = (configured_id or "").strip() or None
        self.streams_path = streams_path.rstrip("/")
        self.discovered_id: str | None = None
        self.full_refresh_cooldown = full_refresh_cooldown
        self.last_full_refresh: float | None = None
        self.clock = clock

    async def library_id(self, jf: JellyfinClient, timeout: float | None = None) -> str | None:
        if self.configured_id:
            return self.configured_id
        if self.discovered_id:
            return self.discovered_id
        try:
            folders = await jf.virtual_folders(timeout=timeout)
        except (urllib.error.URLError, OSError, ValueError):
            return None
        for f in folders:
            locs = [str(x).rstrip("/") for x in f.get("Locations") or []]
            if self.streams_path in locs and f.get("ItemId"):
                self.discovered_id = str(f["ItemId"])
                return self.discovered_id
        return None

    def full_refresh_allowed(self) -> bool:
        return self.last_full_refresh is None or self.clock() - self.last_full_refresh >= self.full_refresh_cooldown

    def mark_full_refresh(self) -> None:
        self.last_full_refresh = self.clock()


_ERRORS = (urllib.error.URLError, OSError, ValueError)


def _http_code(exc) -> int | None:
    return exc.code if isinstance(exc, urllib.error.HTTPError) else None


async def refresh_and_find(
    jf: JellyfinClient,
    *,
    strm_path: str,
    folder_path: str,
    search_term: str,
    library_id: str | None = None,
    public_url: str,
    library: LibraryState | None = None,
    poll_interval: float = 1.5,
    timeout: float = 45.0,
    fallback_after: float = 6.0,
    full_refresh_after: float = 18.0,
    call_timeout: float = 8.0,
    progress: dict | None = None,
    sleep=asyncio.sleep,
    clock=time.monotonic,
) -> dict:
    """1) POST /Library/Media/Updated (targeted; on the NUC this alone did not
    trigger a scan), poll by path. 2) After ``fallback_after`` s: library-scoped
    refresh (library id from env or discovered by path), or POST /Library/Refresh
    if no id. If the library has no items yet (created while empty), the full
    refresh is done right away. 3) After ``full_refresh_after`` s still nothing:
    one full refresh (guarded by a cooldown). Overall wall budget ``timeout``
    including request time. Never a silent result: status + message."""
    if library is None:
        library = LibraryState(library_id, str(PurePosixPath(folder_path).parent))
    start = clock()
    # ``progress`` (optional, shared with GET /api/watch/{id}): live phase + steps.
    progress = progress if progress is not None else {}
    steps: list[str] = progress.setdefault("steps", [])

    def mark(phase: str) -> None:
        progress["phase"] = phase

    def remaining() -> float:
        return timeout - (clock() - start)

    def ct() -> float:
        return max(0.5, min(call_timeout, remaining()))

    async def full_refresh(reason: str) -> None:
        if not library.full_refresh_allowed():
            steps.append("full_refresh:cooldown")
            return
        library.mark_full_refresh()
        mark("full_refresh")
        try:
            await jf.refresh_all(timeout=ct())
            steps.append(f"full_refresh:{reason}")
        except _ERRORS as exc:
            steps.append(f"full_refresh:error:{_http_code(exc) or type(exc).__name__}")

    mark("media_updated")
    try:
        await jf.report_path(folder_path, "Created", timeout=ct())
        steps.append("media_updated")
    except _ERRORS as exc:
        code = _http_code(exc)
        if code in (401, 403):
            return {"status": "auth_failed", "message": f"Jellyfin rejected the API key (HTTP {code})", "steps": steps}
        if not isinstance(exc, urllib.error.HTTPError):
            return {"status": "refresh_failed", "detail": type(exc).__name__,
                    "message": "Jellyfin is unreachable; the files are written and will appear after its next scan",
                    "steps": steps}
        steps.append(f"media_updated:error:{code}")
    fallback_done = False
    full_done = False
    mark("polling")
    while True:
        try:
            item_id = await jf.find_by_path(strm_path, search_term, library.configured_id or library.discovered_id,
                                            folder=folder_path, timeout=ct())
        except _ERRORS:
            item_id = None
        if item_id:
            return {"status": "ready", "item_id": item_id,
                    "url": f"{public_url.rstrip('/')}/web/#/details?id={urllib.parse.quote(item_id)}",
                    "steps": steps, "elapsed_s": round(clock() - start, 1)}
        elapsed = clock() - start
        if remaining() <= 0:
            return {"status": "pending", "steps": steps, "elapsed_s": round(elapsed, 1),
                    "message": "Added to Jellyfin, but it has not indexed it yet; check again shortly"}
        if not fallback_done and elapsed >= fallback_after:
            fallback_done = True
            lib = await library.library_id(jf, timeout=ct())
            if lib:
                mark("library_refresh")
                try:
                    await jf.refresh_library(lib, timeout=ct())
                    steps.append("library_refresh")
                except _ERRORS as exc:
                    steps.append(f"library_refresh:error:{_http_code(exc) or type(exc).__name__}")
                if await jf.count_items(lib, timeout=ct()) == 0:
                    full_done = True
                    await full_refresh("empty_library")
            else:
                steps.append("library_id:unknown")
                full_done = True
                await full_refresh("no_library_id")
        elif fallback_done and not full_done and elapsed >= full_refresh_after:
            full_done = True
            await full_refresh("not_found")
        mark("polling")
        await sleep(max(0.0, min(poll_interval, remaining())))


def jellyfin_api_key(env=None) -> str | None:
    """JELLYFIN_API_KEY_FILE (if set and readable) wins, else JELLYFIN_API_KEY.
    The value is only ever used in the Authorization header, never logged."""
    import os as _os

    env = _os.environ if env is None else env
    return read_secret_file(env.get("JELLYFIN_API_KEY_FILE", "")) or (env.get("JELLYFIN_API_KEY", "").strip() or None)


def read_secret_file(path: str | None) -> str | None:
    if not path:
        return None
    try:
        value = Path(path).read_text().strip()
    except OSError:
        return None
    return value or None
