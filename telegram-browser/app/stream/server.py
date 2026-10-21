"""Separate stream listener (default :8789). Only GET/HEAD /s/{variant_id}/{sig}
and a stats-only /health. No URL/chat parameters, no redirects."""

import asyncio
import contextlib
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.stream import signing
from app.stream.ranges import RangeNotSatisfiable, parse_range
from app.stream.service import StreamError, StreamService


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


def build_stream_app(service: StreamService, key_provider) -> FastAPI:
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
            base = {
                "Accept-Ranges": "bytes",
                "Content-Type": row["mime_type"],
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": "inline",
                "Cache-Control": "private, no-transform",
            }
            try:
                rng = parse_range(request.headers.get("range"), size)
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

        async def body():
            try:
                yield first
                async for piece in rest:
                    yield piece
            except StreamError as exc:
                # Headers are already sent: abort the connection so the
                # player retries with a new Range instead of hanging.
                service.log(f"[stream] mid-stream abort: {exc.status}")
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
