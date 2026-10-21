"""'Watch now': .strm + movie.nfo + poster.jpg writer and targeted Jellyfin refresh."""

import asyncio
import json
import os
import re
import secrets as _secrets
import shutil
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MARKER = ".tgstream.json"
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
class JellyfinClient:
    """Minimal API client. Auth header per Jellyfin's current scheme
    ``Authorization: MediaBrowser Token="<key>"`` (key never logged)."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self._key = api_key
        self.timeout = timeout

    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f'MediaBrowser Token="{self._key}"')
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)

    async def call(self, *args, **kwargs):
        return await asyncio.to_thread(self._request, *args, **kwargs)

    async def report_path(self, path: str, update_type: str = "Created") -> int:
        status, _ = await self.call("POST", "/Library/Media/Updated", body={"Updates": [{"Path": path, "UpdateType": update_type}]})
        return status

    async def refresh_library(self, library_id: str) -> int:
        status, _ = await self.call(
            "POST",
            f"/Items/{urllib.parse.quote(library_id)}/Refresh",
            params={"Recursive": "true", "MetadataRefreshMode": "Default", "ImageRefreshMode": "Default",
                    "ReplaceAllMetadata": "false", "ReplaceAllImages": "false"},
        )
        return status

    async def find_by_path(self, path: str, search_term: str | None, parent_id: str | None) -> str | None:
        params = {"Recursive": "true", "IncludeItemTypes": "Movie", "Fields": "Path,MediaSources", "Limit": "50"}
        if parent_id:
            params["ParentId"] = parent_id
        if search_term:
            params["searchTerm"] = search_term
        _, data = await self.call("GET", "/Items", params=params)
        for item in (data or {}).get("Items", []):
            paths = {item.get("Path")} | {s.get("Path") for s in item.get("MediaSources") or []}
            if path in paths:
                return item.get("Id")
        return None


async def refresh_and_find(
    jf: JellyfinClient,
    *,
    strm_path: str,
    folder_path: str,
    search_term: str,
    library_id: str | None,
    public_url: str,
    poll_interval: float = 2.0,
    timeout: float = 45.0,
    fallback_after: float = 10.0,
    sleep=asyncio.sleep,
    clock=time.monotonic,
) -> dict:
    try:
        await jf.report_path(folder_path, "Created")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"status": "refresh_failed", "detail": type(exc).__name__}
    start = clock()
    fallback_done = False
    while True:
        try:
            item_id = await jf.find_by_path(strm_path, search_term, library_id)
        except (urllib.error.URLError, OSError, ValueError):
            item_id = None
        if item_id:
            return {"status": "ready", "item_id": item_id,
                    "url": f"{public_url.rstrip('/')}/web/#/details?id={urllib.parse.quote(item_id)}"}
        elapsed = clock() - start
        if elapsed >= timeout:
            return {"status": "pending", "detail": "Added; it will appear in Jellyfin within a minute"}
        if library_id and not fallback_done and elapsed >= fallback_after:
            fallback_done = True
            try:
                await jf.refresh_library(library_id)
            except (urllib.error.URLError, OSError, ValueError):
                pass
        await sleep(poll_interval)


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
