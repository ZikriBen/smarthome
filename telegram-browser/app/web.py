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
        "-1002468837108",
    )
)

SEARCH_DELIVERY_BOT = os.getenv(
    "SEARCH_DELIVERY_BOT",
    "searchgram_bbot",
)

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
    "http://10.0.0.13:8787",
).rstrip("/")


BASE_DIR = (
    Path(__file__)
    .resolve()
    .parent
)

POSTER_CACHE_DIR = Path(
    "/data/posters"
)

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
        delivery_bot=(
            SEARCH_DELIVERY_BOT
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
):
    result = (
        database.query_movies(
            page=page,
            page_size=page_size,
            search=search,
            genre=genre,
            sort=sort,
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

    if (
        searchgram is not None
        and searchgram
        .delivery_chat_id
        is not None
    ):
        chat_ids.append(
            searchgram
            .delivery_chat_id
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
