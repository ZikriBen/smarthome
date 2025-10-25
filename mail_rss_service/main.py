import asyncio
import logging
from fastapi import FastAPI, Response, Request
from fastapi.responses import RedirectResponse
from contextlib import asynccontextmanager, suppress
from settings import settings
import src.rss_manager as rss_manager
import src.email_poller as email_poller

# Configure logging with timestamps
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper()),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    poll_task = asyncio.create_task(poll_loop())
    logger.info("Background poller started")
    try:
        yield
    finally:
        # shutdown
        poll_task.cancel()
        with suppress(asyncio.CancelledError):
            await poll_task
        logger.info("Background poller stopped")

app = FastAPI(
    title="Mail to RSS Service",
    lifespan=lifespan,
)

async def poll_loop():
    """Background task that polls for new emails."""
    await asyncio.sleep(3)
    while True:
        try:
            email_poller.poll_once()
        except Exception as e:
            logger.warning(f"Poll error: {e}", exc_info=True)
        await asyncio.sleep(settings.POLL_INTERVAL)

# Middleware to suppress health check logs
@app.middleware("http")
async def log_requests(request: Request, call_next):
    # Don't log health checks
    if request.url.path != "/health":
        logger.info(f"{request.method} {request.url.path}")
    response = await call_next(request)
    return response

@app.get("/")
def root():
    """Redirect root to docs."""
    return RedirectResponse(url="/docs")

@app.get("/rss", response_class=Response)
def rss():
    """RSS feed endpoint."""
    xml = rss_manager.build_rss()
    return Response(
        content=xml,
        media_type="application/rss+xml; charset=utf-8"
    )

@app.get("/health")
def health():
    """Health check endpoint."""
    state = rss_manager.get_state()
    return {
        "status": "ok",
        "items_count": len(state.get("items", [])),
        "last_uid": state.get("last_uid"),
    }

@app.post("/trigger")
def trigger_manual_poll():
    """Manually trigger an email check and feed update."""
    try:
        logger.info("Manual poll triggered")
        result = email_poller.poll_once()
        state = rss_manager.get_state()

        return {
            "status": "success",
            "message": "Manual poll completed",
            "new_email": result is not None,
            "email_info": result,
            "items_count": len(state.get("items", [])),
            "last_uid": state.get("last_uid"),
        }
    except Exception as e:
        logger.error(f"Manual poll failed: {e}", exc_info=True)
        return {
            "status": "error",
            "message": str(e),
        }