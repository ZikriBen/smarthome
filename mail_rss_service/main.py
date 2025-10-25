import asyncio
from fastapi import FastAPI, Response
from contextlib import asynccontextmanager, suppress
from settings import settings
import rss_manager
import email_poller as email_poller

@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    poll_task = asyncio.create_task(poll_loop())
    print("[INFO] Background poller started")
    try:
        yield
    finally:
        # shutdown
        poll_task.cancel()
        with suppress(asyncio.CancelledError):
            await poll_task
        print("[INFO] Background poller stopped")

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
            print(f"[WARN] Poll error: {e}")
            import traceback
            traceback.print_exc()
        await asyncio.sleep(settings.POLL_INTERVAL)

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
        import traceback
        return {
            "status": "error",
            "message": str(e),
            "traceback": traceback.format_exc(),
        }