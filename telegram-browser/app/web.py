import asyncio
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv
from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import (
    Jinja2Templates,
)
from telethon import TelegramClient

from app.database import CatalogDatabase
from app.searchgram import SearchGram
from app.stream import signing as stream_signing
from app.stream import watch as stream_watch
from app.stream.cache import ChunkCache
from app.stream.server import EmbeddedServer, build_stream_app
from app.stream import subs as stream_subs
from app.stream.service import StreamService, is_matroska, is_media_mime
from app.stream.telegram_source import TelegramChunkSource
from app.sync import CatalogSyncer


load_dotenv()


API_ID = int(
    os.environ[
        "TELEGRAM_API_ID"
    ]
)

API_HASH = os.environ[
    "TELEGRAM_API_HASH"
]

SOURCE_CHAT_ID = int(
    os.environ[
        "TELEGRAM_SOURCE_CHAT_ID"
    ]
)

SEARCH_CHAT_ID = int(
    os.getenv(
        "SEARCH_CHAT_ID",
        "-1000000000000",
    )
)

SEARCH_DELIVERY_BOT = os.getenv(
    "SEARCH_DELIVERY_BOT",
    "searchgram_bbot",
)

SEARCH_DELIVERY_BOTS = [
    bot.strip()
    for bot in os.getenv(
        "SEARCH_DELIVERY_BOTS",
        f"{SEARCH_DELIVERY_BOT},searchgram_robot",
    ).split(",")
    if bot.strip()
]

SESSION = os.getenv(
    "TELEGRAM_SESSION",
    "/data/telegram-browser",
)

DATABASE_PATH = os.getenv(
    "CATALOG_DATABASE_PATH",
    "/data/catalog.db",
)

INITIAL_SYNC_MESSAGES = int(
    os.getenv(
        "INITIAL_SYNC_MESSAGES",
        "600",
    )
)

BACKFILL_MESSAGES = int(
    os.getenv(
        "BACKFILL_MESSAGES",
        "500",
    )
)

BACKFILL_PAUSE_SECONDS = float(
    os.getenv(
        "BACKFILL_PAUSE_SECONDS",
        "1",
    )
)

NEWER_SYNC_INTERVAL_SECONDS = int(
    os.getenv(
        "NEWER_SYNC_INTERVAL_SECONDS",
        "60",
    )
)

POSTER_CACHE_MAX_GB = float(
    os.getenv(
        "POSTER_CACHE_MAX_GB",
        "2",
    )
)

DOWNLOADER_URL = os.getenv(
    "DOWNLOADER_URL",
    "http://<LAN_IP>:8787",
).rstrip("/")


BASE_DIR = (
    Path(__file__)
    .resolve()
    .parent
)

POSTER_CACHE_DIR = Path(
    os.getenv(
        "POSTER_CACHE_DIR",
        "/data/posters",
    )
)


# ---------------------------------------------------------------- tg-stream PoC
# Direct watch (Lulu catalog only). Served on a SEPARATE listener (default
# :8789) by the same process, so it uses this app's live Telethon client.
TG_STREAM_HMAC_KEY_FILE = os.getenv("TG_STREAM_HMAC_KEY_FILE", "")
TG_STREAM_BIND = os.getenv("TG_STREAM_BIND", "0.0.0.0")  # container-internal; publish LAN-only in compose
TG_STREAM_PORT = int(os.getenv("TG_STREAM_PORT", "8789"))
TG_STREAM_PUBLIC_BASE = os.getenv("TG_STREAM_PUBLIC_BASE", "").rstrip("/")
TG_STREAM_CONNECTIONS = min(2, max(1, int(os.getenv("TG_STREAM_CONNECTIONS", "2"))))
TG_STREAM_MAX_STREAMS = min(2, max(1, int(os.getenv("TG_STREAM_MAX_STREAMS", "2"))))
TG_STREAM_CACHE_MB = int(os.getenv("TG_STREAM_CACHE_MB", "128"))
TG_STREAM_PREFETCH_MB = float(os.getenv("TG_STREAM_PREFETCH_MB", "2"))
TG_STREAMS_DIR = Path(os.getenv("TG_STREAMS_DIR", "/streams"))
JELLYFIN_URL = os.getenv("JELLYFIN_URL", "http://<LAN_IP>:8096").rstrip("/")
JELLYFIN_PUBLIC_URL = os.getenv("JELLYFIN_PUBLIC_URL", JELLYFIN_URL).rstrip("/")
JELLYFIN_API_KEY_FILE = os.getenv("JELLYFIN_API_KEY_FILE", "")
JELLYFIN_STREAMS_PATH = os.getenv("JELLYFIN_STREAMS_PATH", "/media/telegram-streams")
JELLYFIN_STREAMS_LIBRARY_ID = os.getenv("JELLYFIN_STREAMS_LIBRARY_ID", "").strip() or None
JELLYFIN_POLL_TIMEOUT_S = float(os.getenv("JELLYFIN_POLL_TIMEOUT_S", "45"))
JELLYFIN_FALLBACK_AFTER_S = float(os.getenv("JELLYFIN_FALLBACK_AFTER_S", "6"))
# Subtitle sidecars (background, low priority; see app/stream/subs.py).
TG_SUBS_ENABLED = os.getenv("TG_SUBS_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
TG_SUBS_MAX_MB = int(os.getenv("TG_SUBS_MAX_MB", "4096"))
TG_SUBS_MAX_MINUTES = float(os.getenv("TG_SUBS_MAX_MINUTES", "30"))
# Watch now waits this long for the subtitle job to parse the MKV header and put
# the (placeholder) Hebrew sidecar in place BEFORE asking Jellyfin to scan, so
# the sidecar is in Jellyfin's very first directory listing of the folder.
TG_SUBS_HEADER_WAIT_S = float(os.getenv("TG_SUBS_HEADER_WAIT_S", "6"))


def _origin(url: str) -> str | None:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else None


# Exact origins allowed to read streams cross-origin (only matters if Jellyfin
# Web uses crossorigin=use-credentials). Default: the Jellyfin public URL.
TG_STREAM_CORS_ORIGINS = frozenset(
    o for o in (
        [x.strip().rstrip("/") for x in os.getenv("TG_STREAM_CORS_ORIGINS", "").split(",") if x.strip()]
        or [_origin(JELLYFIN_PUBLIC_URL)]
    ) if o
)
JELLYFIN_LIBRARY = stream_watch.LibraryState(JELLYFIN_STREAMS_LIBRARY_ID, JELLYFIN_STREAMS_PATH)

POSTER_CACHE_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


database = CatalogDatabase(
    DATABASE_PATH
)

client: TelegramClient | None = None

syncer: CatalogSyncer | None = None

searchgram: SearchGram | None = None

background_sync_task: (
    asyncio.Task | None
) = None

stream_service: StreamService | None = None

stream_server: EmbeddedServer | None = None

stream_source: TelegramChunkSource | None = None

subtitle_jobs: stream_subs.SubtitleJobs | None = None

_stream_tasks: set = set()


def stream_key() -> bytes | None:
    # Re-read lazily so a rotated key file is picked up without restart;
    # the value is never logged or returned.
    try:
        return stream_signing.load_key(TG_STREAM_HMAC_KEY_FILE)
    except stream_signing.HmacKeyError as exc:
        print(f"tg-stream disabled: {exc}", flush=True)
        return None


def make_stream_service(tg_client) -> StreamService:
    async def fetch_message(variant_id: int):
        # Chat is fixed to the Lulu source; callers can only pass a variant id.
        return await tg_client.get_messages(SOURCE_CHAT_ID, ids=int(variant_id))

    global stream_source
    stream_source = TelegramChunkSource(tg_client, connections=TG_STREAM_CONNECTIONS)
    return StreamService(
        catalog_lookup=database.get_variant_details,
        fetch_message=fetch_message,
        source=stream_source,
        cache=ChunkCache(TG_STREAM_CACHE_MB * 1024 * 1024),
        max_streams=TG_STREAM_MAX_STREAMS,
    )


def jellyfin_client() -> "stream_watch.JellyfinClient | None":
    api_key = stream_watch.jellyfin_api_key(
        {"JELLYFIN_API_KEY_FILE": JELLYFIN_API_KEY_FILE, "JELLYFIN_API_KEY": os.getenv("JELLYFIN_API_KEY", "")}
    )
    return stream_watch.JellyfinClient(JELLYFIN_URL, api_key) if api_key else None


async def refresh_item_for_subs(variant_id: int, folder: Path, names: list[str]) -> None:
    """After sidecars are written: refresh the Jellyfin item so it lists them."""
    jf = jellyfin_client()
    row = database.get_variant_details(variant_id)
    if jf is None or not row:
        return
    entry = stream_watch.entry_paths(TG_STREAMS_DIR, row, row)
    lib = await JELLYFIN_LIBRARY.library_id(jf)
    item_id = await jf.find_by_path(
        stream_watch.jellyfin_path(entry.strm, TG_STREAMS_DIR, JELLYFIN_STREAMS_PATH),
        row.get("english_title") or row.get("title"),
        lib,
        folder=stream_watch.jellyfin_path(entry.folder, TG_STREAMS_DIR, JELLYFIN_STREAMS_PATH),
    )
    if item_id:
        await jf.refresh_item(item_id)
        print(f"[subs] {int(variant_id)}: jellyfin item refreshed", flush=True)
    elif lib:
        await jf.refresh_library(lib)
        print(f"[subs] {int(variant_id)}: jellyfin library refreshed (item not indexed yet)", flush=True)


def make_subtitle_jobs(service: StreamService) -> stream_subs.SubtitleJobs:
    return stream_subs.SubtitleJobs(
        service,
        marker_name=stream_watch.MARKER,
        max_bytes=TG_SUBS_MAX_MB * 1024 * 1024,
        max_seconds=TG_SUBS_MAX_MINUTES * 60,
        on_done=refresh_item_for_subs,
    )


def cleanup_poster_cache() -> None:
    max_bytes = int(
        POSTER_CACHE_MAX_GB
        * 1024
        * 1024
        * 1024
    )

    if max_bytes <= 0:
        return

    entries = []
    total = 0

    for path in (
        POSTER_CACHE_DIR
        .glob("*.jpg")
    ):
        if not path.is_file():
            continue

        try:
            stat = path.stat()

        except FileNotFoundError:
            continue

        total += stat.st_size

        entries.append(
            (
                stat.st_mtime,
                stat.st_size,
                path,
            )
        )

    if total <= max_bytes:
        return

    entries.sort(
        key=lambda item:
            item[0]
    )

    for _, size, path in entries:
        try:
            path.unlink()

        except FileNotFoundError:
            continue

        total -= size

        if total <= max_bytes:
            break


def _decode_json_response(
    response,
) -> dict:
    raw = response.read()

    if not raw:
        return {}

    return json.loads(
        raw.decode(
            "utf-8"
        )
    )


def _downloader_post_sync(
    path: str,
    payload: dict,
) -> tuple[int, dict]:
    url = (
        f"{DOWNLOADER_URL}"
        f"{path}"
    )

    request = urllib.request.Request(
        url,
        data=json.dumps(
            payload
        ).encode(
            "utf-8"
        ),
        headers={
            "Content-Type":
                "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=15,
        ) as response:
            return (
                response.status,
                _decode_json_response(
                    response
                ),
            )

    except urllib.error.HTTPError as exc:
        try:
            data = (
                _decode_json_response(
                    exc
                )
            )

        except Exception:
            data = {
                "status": "error",
                "error": (
                    f"downloader_http_"
                    f"{exc.code}"
                ),
            }

        return (
            exc.code,
            data,
        )


def _downloader_get_sync(
    path: str,
    params: dict,
) -> tuple[int, dict]:
    query = urllib.parse.urlencode(
        {
            key: value
            for key, value
            in params.items()
            if value not in (
                None,
                "",
            )
        }
    )

    url = (
        f"{DOWNLOADER_URL}"
        f"{path}"
    )

    if query:
        url += f"?{query}"

    request = urllib.request.Request(
        url,
        method="GET",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=15,
        ) as response:
            return (
                response.status,
                _decode_json_response(
                    response
                ),
            )

    except urllib.error.HTTPError as exc:
        try:
            data = (
                _decode_json_response(
                    exc
                )
            )

        except Exception:
            data = {
                "status": "error",
                "error": (
                    f"downloader_http_"
                    f"{exc.code}"
                ),
            }

        return (
            exc.code,
            data,
        )


async def downloader_post(
    path: str,
    payload: dict,
) -> tuple[int, dict]:
    try:
        return await asyncio.to_thread(
            _downloader_post_sync,
            path,
            payload,
        )

    except Exception as exc:
        print(
            "Downloader POST failed: "
            f"{type(exc).__name__}: "
            f"{exc}",
            flush=True,
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "Downloader unavailable"
            ),
        )


async def downloader_get(
    path: str,
    params: dict,
) -> tuple[int, dict]:
    try:
        return await asyncio.to_thread(
            _downloader_get_sync,
            path,
            params,
        )

    except Exception as exc:
        print(
            "Downloader GET failed: "
            f"{type(exc).__name__}: "
            f"{exc}",
            flush=True,
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "Downloader unavailable"
            ),
        )


def require_searchgram() -> SearchGram:
    if searchgram is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "SearchGram unavailable"
            ),
        )

    return searchgram


@asynccontextmanager
async def lifespan(
    app: FastAPI,
):
    global client
    global syncer
    global searchgram
    global background_sync_task

    client = TelegramClient(
        SESSION,
        API_ID,
        API_HASH,
    )

    await client.start()

    me = await client.get_me()

    print(
        "Telegram browser connected as "
        f"{me.first_name or ''} "
        f"({me.id})",
        flush=True,
    )

    syncer = CatalogSyncer(
        client=client,
        database=database,
        chat_id=SOURCE_CHAT_ID,
        initial_messages=(
            INITIAL_SYNC_MESSAGES
        ),
        backfill_messages=(
            BACKFILL_MESSAGES
        ),
    )

    await syncer.initialize()

    await syncer.sync_newer()

    searchgram = SearchGram(
        client=client,
        search_chat_id=(
            SEARCH_CHAT_ID
        ),
        delivery_bots=(
            SEARCH_DELIVERY_BOTS
        ),
    )

    try:
        await searchgram.initialize()

    except Exception as exc:
        print(
            "SearchGram initialization "
            "failed: "
            f"{type(exc).__name__}: "
            f"{exc}",
            flush=True,
        )

        searchgram = None

    cleanup_poster_cache()

    global stream_service
    global stream_server
    global subtitle_jobs

    stream_service = make_stream_service(client)
    subtitle_jobs = make_subtitle_jobs(stream_service)

    if stream_key():
        stream_server = EmbeddedServer(
            build_stream_app(stream_service, stream_key, TG_STREAM_CORS_ORIGINS),
            TG_STREAM_BIND,
            TG_STREAM_PORT,
        )
        stream_server.start()
        print(f"tg-stream listening on container port {TG_STREAM_PORT}", flush=True)
    else:
        print("tg-stream disabled: no TG_STREAM_HMAC_KEY_FILE", flush=True)

    background_sync_task = (
        asyncio.create_task(
            syncer.run_background(
                newer_interval_seconds=(
                    NEWER_SYNC_INTERVAL_SECONDS
                ),
                backfill_pause_seconds=(
                    BACKFILL_PAUSE_SECONDS
                ),
            )
        )
    )

    try:
        yield

    finally:
        if (
            background_sync_task
            is not None
        ):
            background_sync_task.cancel()

            try:
                await background_sync_task

            except asyncio.CancelledError:
                pass

        if stream_server is not None:
            await stream_server.stop()

        if stream_source is not None:
            await stream_source.close()

        await client.disconnect()


app = FastAPI(
    title=(
        "Telegram Media Browser"
    ),
    lifespan=lifespan,
)


app.mount(
    "/static",
    StaticFiles(
        directory=(
            BASE_DIR
            / "static"
        )
    ),
    name="static",
)


templates = Jinja2Templates(
    directory=(
        BASE_DIR
        / "templates"
    )
)


@app.get(
    "/",
    response_class=HTMLResponse,
)
async def index(
    request: Request,
):
    return (
        templates.TemplateResponse(
            request=request,
            name="index.html",
            context={},
        )
    )


@app.get(
    "/downloads",
    response_class=HTMLResponse,
)
async def downloads_page(
    request: Request,
):
    return (
        templates.TemplateResponse(
            request=request,
            name="downloads.html",
            context={},
        )
    )


@app.get(
    "/search",
    response_class=HTMLResponse,
)
async def search_page(
    request: Request,
):
    return (
        templates.TemplateResponse(
            request=request,
            name="search.html",
            context={},
        )
    )


@app.post(
    "/api/search",
)
async def api_search(
    request: Request,
):
    try:
        payload = await request.json()

    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON",
        )

    query = str(
        payload.get(
            "query",
            "",
        )
    ).strip()

    if not query:
        raise HTTPException(
            status_code=400,
            detail="Search query is empty",
        )

    service = require_searchgram()

    try:
        result = (
            await service.search(
                query
            )
        )

    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=str(exc),
        )

    except Exception as exc:
        print(
            "SearchGram search failed: "
            f"{type(exc).__name__}: "
            f"{exc}",
            flush=True,
        )

        raise HTTPException(
            status_code=502,
            detail="SearchGram search failed",
        )

    return result.to_dict()


@app.post(
    "/api/search/navigate",
)
async def api_search_navigate(
    request: Request,
):
    try:
        payload = await request.json()

        message_id = int(
            payload["message_id"]
        )

        callback_data = str(
            payload["callback_data"]
        )

        query = str(
            payload["query"]
        )

    except (
        Exception,
        KeyError,
        TypeError,
        ValueError,
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid search "
                "navigation request"
            ),
        )

    service = require_searchgram()

    try:
        result = (
            await service.navigate(
                message_id=message_id,
                callback_data=callback_data,
                query=query,
            )
        )

    except Exception as exc:
        print(
            "SearchGram navigation failed: "
            f"{type(exc).__name__}: "
            f"{exc}",
            flush=True,
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "SearchGram navigation failed"
            ),
        )

    return result.to_dict()


@app.post(
    "/api/search/download",
)
async def api_search_download(
    request: Request,
):
    try:
        payload = await request.json()

        message_id = int(
            payload["message_id"]
        )

        callback_data = str(
            payload["callback_data"]
        )

    except (
        Exception,
        KeyError,
        TypeError,
        ValueError,
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid search "
                "download request"
            ),
        )

    service = require_searchgram()

    try:
        media = (
            await service.request_media(
                message_id=message_id,
                callback_data=(
                    callback_data
                ),
            )
        )

    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=str(exc),
        )

    except Exception as exc:
        print(
            "SearchGram delivery failed: "
            f"{type(exc).__name__}: "
            f"{exc}",
            flush=True,
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "SearchGram media "
                "delivery failed"
            ),
        )

    status_code, result = (
        await downloader_post(
            "/enqueue",
            {
                "chat_id":
                    media.chat_id,

                "message_id":
                    media.message_id,
            },
        )
    )

    if status_code >= 400:
        raise HTTPException(
            status_code=status_code,
            detail=(
                result.get(
                    "error",
                    "Downloader error",
                )
            ),
        )

    return {
        "status":
            result.get(
                "status",
                "queued",
            ),

        "delivery": {
            "chat_id":
                media.chat_id,

            "message_id":
                media.message_id,

            "filename":
                media.filename,

            "file_size":
                media.file_size,
        },

        "job":
            result,
    }


@app.post(
    "/api/job-status",
)
async def api_job_status(
    request: Request,
):
    try:
        payload = await request.json()

        chat_id = int(
            payload["chat_id"]
        )

        message_id = int(
            payload["message_id"]
        )

    except (
        Exception,
        KeyError,
        TypeError,
        ValueError,
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid job status request"
            ),
        )

    status_code, result = (
        await downloader_post(
            "/jobs/status",
            {
                "chat_id":
                    chat_id,

                "message_ids": [
                    message_id
                ],
            },
        )
    )

    if status_code >= 400:
        raise HTTPException(
            status_code=status_code,
            detail=(
                result.get(
                    "error",
                    "Downloader error",
                )
            ),
        )

    return {
        "job": (
            result.get(
                "jobs",
                {},
            ).get(
                str(
                    message_id
                )
            )
        )
    }


@app.get(
    "/api/items",
)
async def api_items(
    page: int = Query(
        1,
        ge=1,
    ),
    page_size: int = Query(
        25,
        ge=1,
        le=100,
    ),
    search: str = "",
    genre: str = "",
    sort: str = "newest",
    saved_only: bool = False,
):
    result = (
        database.query_movies(
            page=page,
            page_size=page_size,
            search=search,
            genre=genre,
            sort=sort,
            saved_only=saved_only,
        )
    )

    return {
        "items": [
            asdict(item)
            for item
            in result.items
        ],

        "pagination": {
            "page":
                result.page,

            "page_size":
                result.page_size,

            "total":
                result.total,

            "total_pages":
                result.total_pages,

            "has_previous":
                result.has_previous,

            "has_next":
                result.has_next,

            "history_complete":
                database
                .get_state_bool(
                    "history_complete"
                ),
        },
    }


@app.get(
    "/api/genres",
)
async def api_genres():
    return database.get_genres()


@app.post(
    "/api/items/{message_id}/save",
)
async def api_save_item(
    message_id: int,
    request: Request,
):
    try:
        payload = await request.json()

    except Exception:
        payload = {}

    saved = bool(
        payload.get(
            "saved",
            True,
        )
    )

    database.set_saved(
        message_id,
        saved,
    )

    return {
        "details_message_id":
            message_id,

        "saved":
            saved,
    }


@app.post(
    "/api/refresh",
)
async def refresh_catalog():
    if syncer is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Sync service unavailable"
            ),
        )

    synced = (
        await syncer.sync_newer()
    )

    return {
        "status": "ok",
        "new_messages": synced,
        "movies":
            database.count_movies(),
        "history_complete":
            database.get_state_bool(
                "history_complete"
            ),
    }


@app.get(
    "/api/index-status",
)
async def index_status():
    return {
        "movies":
            database.count_movies(),

        "history_complete":
            database.get_state_bool(
                "history_complete"
            ),

        "newest_scanned_message_id":
            database.get_state_int(
                "newest_scanned_message_id"
            ),

        "oldest_scanned_message_id":
            database.get_state_int(
                "oldest_scanned_message_id"
            ),
    }


@app.post(
    "/api/download/{message_id}",
)
async def select_download(
    message_id: int,
):
    status_code, result = (
        await downloader_post(
            "/enqueue",
            {
                "chat_id":
                    SOURCE_CHAT_ID,

                "message_id":
                    message_id,
            },
        )
    )

    if status_code >= 400:
        raise HTTPException(
            status_code=status_code,
            detail=(
                result.get(
                    "error",
                    "Downloader error",
                )
            ),
        )

    return JSONResponse(
        result,
        status_code=status_code,
    )


@app.post(
    "/api/download-status",
)
async def download_status(
    request: Request,
):
    try:
        payload = (
            await request.json()
        )

    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON",
        )

    message_ids = payload.get(
        "message_ids"
    )

    if not isinstance(
        message_ids,
        list,
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "message_ids must "
                "be a list"
            ),
        )

    clean_ids = []

    for value in message_ids:
        if (
            isinstance(
                value,
                bool,
            )
            or not isinstance(
                value,
                int,
            )
            or value <= 0
        ):
            continue

        clean_ids.append(
            value
        )

    clean_ids = list(
        dict.fromkeys(
            clean_ids
        )
    )

    if len(clean_ids) > 200:
        raise HTTPException(
            status_code=400,
            detail=(
                "Too many message IDs"
            ),
        )

    status_code, result = (
        await downloader_post(
            "/jobs/status",
            {
                "chat_id":
                    SOURCE_CHAT_ID,

                "message_ids":
                    clean_ids,
            },
        )
    )

    if status_code >= 400:
        raise HTTPException(
            status_code=status_code,
            detail=(
                result.get(
                    "error",
                    "Downloader error",
                )
            ),
        )

    return result


@app.get(
    "/api/downloads",
)
async def downloads_api(
    page: int = Query(
        1,
        ge=1,
    ),
    page_size: int = Query(
        30,
        ge=1,
        le=100,
    ),
    status: str = "",
):
    chat_ids = [
        SOURCE_CHAT_ID
    ]

    if searchgram is not None:
        chat_ids.extend(
            searchgram.delivery_chat_ids
        )

    status_code, result = (
        await downloader_get(
            "/jobs",
            {
                "chat_ids":
                    ",".join(
                        str(value)
                        for value
                        in chat_ids
                    ),

                "page":
                    page,

                "page_size":
                    page_size,

                "status":
                    status,
            },
        )
    )

    if status_code >= 400:
        raise HTTPException(
            status_code=status_code,
            detail=(
                result.get(
                    "error",
                    "Downloader error",
                )
            ),
        )

    return result


@app.get(
    "/poster/{message_id}",
)
async def poster(
    message_id: int,
):
    if client is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Telegram unavailable"
            ),
        )

    cache_path = (
        POSTER_CACHE_DIR
        / f"{message_id}.jpg"
    )

    if cache_path.exists():
        try:
            cache_path.touch()

        except OSError:
            pass

        return Response(
            content=(
                cache_path
                .read_bytes()
            ),
            media_type=(
                "image/jpeg"
            ),
            headers={
                "Cache-Control":
                    "public, max-age=86400"
            },
        )

    message = (
        await client.get_messages(
            SOURCE_CHAT_ID,
            ids=message_id,
        )
    )

    if (
        not message
        or not message.photo
    ):
        raise HTTPException(
            status_code=404,
            detail=(
                "Poster not found"
            ),
        )

    buffer = io.BytesIO()

    await client.download_media(
        message,
        file=buffer,
    )

    data = buffer.getvalue()

    if not data:
        raise HTTPException(
            status_code=404,
            detail=(
                "Poster download failed"
            ),
        )

    cache_path.write_bytes(
        data
    )

    cleanup_poster_cache()

    return Response(
        content=data,
        media_type="image/jpeg",
        headers={
            "Cache-Control":
                "public, max-age=86400"
        },
    )


@app.api_route(
    "/s/{rest:path}",
    methods=["GET", "HEAD", "POST"],
)
async def no_streams_here(rest: str):
    # Streams are served ONLY by the separate LAN-only listener.
    raise HTTPException(status_code=404, detail="Not Found")


async def poster_bytes(poster_message_id: int | None) -> bytes | None:
    if not poster_message_id:
        return None
    cached = POSTER_CACHE_DIR / f"{int(poster_message_id)}.jpg"
    if cached.exists():
        return cached.read_bytes()
    try:
        response = await poster(int(poster_message_id))
    except HTTPException:
        return None
    return bytes(response.body)


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _stream_tasks.add(task)
    task.add_done_callback(_stream_tasks.discard)


def _watch_row(variant_id: int) -> dict:
    row = database.get_variant_details(variant_id)
    if not row or not is_media_mime(row.get("mime_type")):
        raise HTTPException(status_code=404, detail="Unknown variant")
    return row


def _subs_status(variant_id: int) -> dict | None:
    return subtitle_jobs.public(variant_id) if subtitle_jobs is not None else None


WATCH_MESSAGES = {
    "ready": "Ready in Jellyfin",
    "refresh_skipped": "Files written; Jellyfin refresh skipped (no API key configured)",
}


# In-flight Watch-now jobs per variant: a double tap (or a second device)
# joins the running job instead of starting a second refresh cycle. The job
# is shielded, so it finishes even if the browser navigates away.
_watch_inflight: dict[int, dict] = {}


def library_url() -> str:
    """Fallback link: the Telegram Streams library in Jellyfin (home if unknown)."""
    lib = JELLYFIN_LIBRARY.configured_id or JELLYFIN_LIBRARY.discovered_id
    if lib:
        return (f"{JELLYFIN_PUBLIC_URL}/web/#/movies?topParentId={urllib.parse.quote(lib)}"
                "&collectionType=movies")
    return f"{JELLYFIN_PUBLIC_URL}/web/#/home"


async def _do_watch(variant_id: int, row: dict, key: bytes, progress: dict) -> dict:
    progress["phase"] = "writing"
    url = f"{TG_STREAM_PUBLIC_BASE}/s/{int(variant_id)}/{stream_signing.sign(key, variant_id)}"
    picture = await poster_bytes(row.get("poster_message_id"))
    entry, changed = await asyncio.to_thread(
        stream_watch.write_entry, TG_STREAMS_DIR, row, row, url, picture
    )
    if stream_service is not None and TG_STREAM_PREFETCH_MB > 0:
        # Lulu has no delivery quota (DESIGN 3b), so warming is free of slots.
        _spawn(stream_service.prefetch_edges(int(variant_id), int(TG_STREAM_PREFETCH_MB * 1024 * 1024)))
    if subtitle_jobs is not None and TG_SUBS_ENABLED:
        subtitle_jobs.schedule(int(variant_id), row, entry.folder, entry.strm.name, is_matroska(row))
        subtitle_jobs.boost(int(variant_id))
        progress["phase"] = "subs_header"
        await subtitle_jobs.wait_header(int(variant_id), TG_SUBS_HEADER_WAIT_S)
    result = {"variant_id": int(variant_id), "folder": entry.name, "written": changed}
    jf = jellyfin_client()
    if jf is None:
        return {**result, "status": "refresh_skipped", "message": WATCH_MESSAGES["refresh_skipped"]}
    found = await stream_watch.refresh_and_find(
        jf,
        strm_path=stream_watch.jellyfin_path(entry.strm, TG_STREAMS_DIR, JELLYFIN_STREAMS_PATH),
        folder_path=stream_watch.jellyfin_path(entry.folder, TG_STREAMS_DIR, JELLYFIN_STREAMS_PATH),
        search_term=row.get("english_title") or row.get("title"),
        library=JELLYFIN_LIBRARY,
        public_url=JELLYFIN_PUBLIC_URL,
        timeout=JELLYFIN_POLL_TIMEOUT_S,
        fallback_after=JELLYFIN_FALLBACK_AFTER_S,
        progress=progress,
    )
    found.setdefault("message", WATCH_MESSAGES.get(found["status"], found["status"]))
    print(f"[watch] {int(variant_id)}: {found['status']} steps={','.join(found.get('steps', []))} "
          f"elapsed={found.get('elapsed_s')}", flush=True)
    return {**result, **found}


@app.post(
    "/api/watch/{variant_id}",
)
async def watch_now(
    variant_id: int,
):
    key = stream_key()
    if key is None or not TG_STREAM_PUBLIC_BASE:
        raise HTTPException(status_code=503, detail="Streaming is not configured")
    row = _watch_row(variant_id)
    vid = int(variant_id)
    job = _watch_inflight.get(vid)
    joined = job is not None and not job["task"].done()
    if not joined:
        progress: dict = {"phase": "queued", "steps": []}
        task = asyncio.create_task(_do_watch(vid, row, key, progress))
        job = {"task": task, "progress": progress, "started": asyncio.get_running_loop().time()}
        _watch_inflight[vid] = job
        task.add_done_callback(lambda t, v=vid, j=job: _watch_inflight.pop(v, None)
                               if _watch_inflight.get(v) is j else None)
    try:
        result = await asyncio.shield(job["task"])
    except Exception as exc:  # noqa: BLE001
        print(f"[watch] {vid}: failed {type(exc).__name__}: {exc}", flush=True)
        return {"variant_id": vid, "status": "error", "message": "Watch now failed",
                "library_url": library_url(), "subtitles": _subs_status(vid)}
    return {**result, "joined": joined, "library_url": library_url(), "subtitles": _subs_status(vid)}


@app.get(
    "/api/watch/{variant_id}",
)
async def watch_status(
    variant_id: int,
):
    """Cheap follow-up for a 'pending' Watch now: one lookup, no refresh."""
    row = _watch_row(variant_id)
    entry = stream_watch.entry_paths(TG_STREAMS_DIR, row, row)
    base = {"variant_id": int(variant_id), "folder": entry.name, "subtitles": _subs_status(variant_id),
            "library_url": library_url()}
    if subtitle_jobs is not None:
        subtitle_jobs.boost(int(variant_id))  # the popup is open and polling
        base["subtitles"] = _subs_status(variant_id)
    job = _watch_inflight.get(int(variant_id))
    if job is not None and not job["task"].done():
        prog = job["progress"]
        return {**base, "status": "pending", "in_progress": True, "phase": prog.get("phase"),
                "steps": list(prog.get("steps", [])),
                "elapsed_s": round(asyncio.get_running_loop().time() - job["started"], 1),
                "message": "Watch now is in progress"}
    if not entry.strm.exists():
        return {**base, "status": "not_prepared", "message": "Press Watch now first"}
    jf = jellyfin_client()
    if jf is None:
        return {**base, "status": "refresh_skipped", "message": WATCH_MESSAGES["refresh_skipped"]}
    try:
        item_id = await jf.find_by_path(
            stream_watch.jellyfin_path(entry.strm, TG_STREAMS_DIR, JELLYFIN_STREAMS_PATH),
            row.get("english_title") or row.get("title"),
            JELLYFIN_LIBRARY.configured_id or JELLYFIN_LIBRARY.discovered_id,
            folder=stream_watch.jellyfin_path(entry.folder, TG_STREAMS_DIR, JELLYFIN_STREAMS_PATH),
            timeout=5,
        )
    except (OSError, ValueError):
        item_id = None
    if not item_id:
        return {**base, "status": "pending", "message": "Not indexed by Jellyfin yet"}
    return {**base, "status": "ready", "item_id": item_id, "message": WATCH_MESSAGES["ready"],
            "url": f"{JELLYFIN_PUBLIC_URL}/web/#/details?id={urllib.parse.quote(item_id)}"}


@app.get(
    "/health",
)
async def health():
    database_file = Path(
        DATABASE_PATH
    )

    database_bytes = 0

    if database_file.exists():
        database_bytes = (
            database_file
            .stat()
            .st_size
        )

    poster_bytes = 0

    for path in (
        POSTER_CACHE_DIR
        .glob("*.jpg")
    ):
        if not path.is_file():
            continue

        try:
            poster_bytes += (
                path.stat().st_size
            )

        except FileNotFoundError:
            pass

    return {
        "status": "ok",

        "movies":
            database.count_movies(),

        "history_complete":
            database.get_state_bool(
                "history_complete"
            ),

        "database_mb":
            round(
                database_bytes
                / 1024
                / 1024,
                2,
            ),

        "poster_cache_mb":
            round(
                poster_bytes
                / 1024
                / 1024,
                2,
            ),

        "poster_cache_max_gb":
            POSTER_CACHE_MAX_GB,

        "downloader_url":
            DOWNLOADER_URL,

        "searchgram":
            searchgram is not None,

        "tg_stream":
            {
                **stream_service.health(),
                "subs": subtitle_jobs.summary() if subtitle_jobs is not None else None,
                "library_id_source": (
                    "env" if JELLYFIN_LIBRARY.configured_id
                    else "discovered" if JELLYFIN_LIBRARY.discovered_id
                    else None
                ),
            }
            if stream_server is not None
            and stream_service is not None
            else None,
    }


@app.get(
    "/api/debug/oldest-messages",
)
async def debug_oldest_messages():
    if client is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Telegram unavailable"
            ),
        )

    messages = []

    async for message in (
        client.iter_messages(
            SOURCE_CHAT_ID,
            reverse=True,
            limit=10,
        )
    ):
        messages.append(
            {
                "id":
                    message.id,

                "date": (
                    message.date
                    .isoformat()
                    if message.date
                    else None
                ),

                "text": (
                    message.message
                    or ""
                )[:120],
            }
        )

    return {
        "oldest_accessible_message_id": (
            messages[0]["id"]
            if messages
            else None
        ),

        "messages":
            messages,

        "indexed_oldest_message_id":
            database.get_state_int(
                "oldest_scanned_message_id"
            ),

        "history_complete":
            database.get_state_bool(
                "history_complete"
            ),
    }
