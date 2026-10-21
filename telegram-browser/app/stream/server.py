"""Separate stream listener (default :8789). Only GET/HEAD /s/{variant_id}/{sig}
and a stats-only /health. No URL/chat parameters, no redirects."""

import asyncio
import contextlib
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.stream import signing
from app.stream.ranges import RangeNotSatisfiable, parse_range
from app.stream.service import StreamError, StreamService, stream_mime


class RedactSigFilter(logging.Filter):
    """uvicorn.access args are (client, method, path, http_version, status)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = args[:2] + (signing.redact_path(args[2]),) + args[3:]
        return True


def install_log_redaction() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactSigFilter) for f in logger.filters):
        logger.addFilter(RedactSigFilter())


def _error(exc: StreamError) -> JSONResponse:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else {}
    return JSONResponse({"detail": exc.detail}, status_code=exc.status, headers=headers)


def etag_for(variant_id: int, size: int) -> str:
    # Strong validator: a catalog variant is an immutable Telegram document.
    return f'"tg{int(variant_id)}-{int(size)}"'


def build_stream_app(service: StreamService, key_provider, cors_origins: frozenset[str] = frozenset()) -> FastAPI:
    """``cors_origins``: exact origins (scheme://host:port) allowed to read the
    stream cross-origin. Only needed when Jellyfin Web runs with "include CORS
    credentials" (crossorigin=use-credentials); plain <video src> does not use CORS."""
    app = FastAPI(title="tg-stream", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health():
        return service.health()

    @app.api_route("/s/{variant_id}/{sig}", methods=["GET", "HEAD"])
    async def stream(variant_id: int, sig: str, request: Request):
        if not signing.verify(key_provider(), variant_id, sig):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        try:
            row = service.variant(variant_id)
            service.acquire(variant_id)
        except StreamError as exc:
            return _error(exc)
        released = False

        def release():
            nonlocal released
            if not released:
                released = True
                service.release(variant_id)

        try:
            loc = await service.location(variant_id)
            size = loc.size
            etag = etag_for(variant_id, size)
            base = {
                "Accept-Ranges": "bytes",
                "Content-Type": stream_mime(row),
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": "inline",
                "Cache-Control": "private, no-transform",
                "ETag": etag,
            }
            origin = request.headers.get("origin")
            if origin and origin in cors_origins:
                base.update({
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Credentials": "true",
                    "Access-Control-Expose-Headers": "Accept-Ranges, Content-Range, Content-Length, ETag",
                    "Vary": "Origin",
                })
            range_header = request.headers.get("range")
            if_range = request.headers.get("if-range")
            if range_header and if_range and if_range.strip() != etag:
                range_header = None  # RFC 9110 13.1.5: validator mismatch -> full 200
            try:
                rng = parse_range(range_header, size)
            except RangeNotSatisfiable:
                release()
                return Response(status_code=416, headers={**base, "Content-Range": f"bytes */{size}"})
            start, end = rng if rng else (0, size - 1)
            status = 206 if rng else 200
            headers = {**base, "Content-Length": str(end - start + 1)}
            if rng:
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            if request.method == "HEAD":
                release()
                return Response(status_code=status, headers=headers)
            first, rest = await service.first_and_rest(variant_id, start, end)
        except StreamError as exc:
            release()
            return _error(exc)
        except Exception as exc:  # pragma: no cover - defensive
            release()
            service.log(f"[stream] open failed: {type(exc).__name__}")
            return JSONResponse({"detail": "Upstream error"}, status_code=502)

        expected = end - start + 1

        async def body():
            # Never finish a response cleanly with fewer bytes than its
            # Content-Length: a clean short body looks like EOF to ffmpeg, which
            # then caches a truncated subtitle extraction forever. Any failure
            # after the headers raises out of the ASGI app, so uvicorn drops the
            # connection (no terminating chunk, Content-Length unmet) and the
            # client sees an error and retries.
            sent = 0
            try:
                yield first
                sent += len(first)
                async for piece in rest:
                    yield piece
                    sent += len(piece)
                if sent != expected:
                    raise StreamError(502, f"body length mismatch {sent}/{expected}")
            except StreamError as exc:
                service.stats["aborted_responses"] += 1
                service.log(f"[stream] mid-stream abort: {exc.status} {exc.detail} at {sent}/{expected}")
                raise
            except asyncio.CancelledError:
                raise  # client went away: nothing to tell it
            except Exception as exc:
                service.stats["aborted_responses"] += 1
                service.log(f"[stream] mid-stream abort: {type(exc).__name__} at {sent}/{expected}")
                raise
            finally:
                with contextlib.suppress(Exception):
                    await rest.aclose()
                release()

        return StreamingResponse(body(), status_code=status, headers=headers)

    return app


class EmbeddedServer:
    """Second uvicorn server in the SAME process/event loop as the main app,
    so it uses the main app's live Telethon client. Signal handling stays with
    the main server."""

    def __init__(self, app: FastAPI, host: str, port: int):
        import uvicorn

        class _Server(uvicorn.Server):
            def install_signal_handlers(self):  # uvicorn < 0.29
                return None

            @contextlib.contextmanager
            def capture_signals(self):  # uvicorn >= 0.29
                yield

        config = uvicorn.Config(app, host=host, port=port, log_level="info", access_log=True, lifespan="off")
        self.server = _Server(config)
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        install_log_redaction()
        self.task = asyncio.create_task(self.server.serve())

    async def stop(self) -> None:
        if self.task is None:
            return
        self.server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.task, timeout=5)
