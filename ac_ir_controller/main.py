import logging
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from contextlib import asynccontextmanager
from pydantic import BaseModel
from settings import settings

# Configure logging with timestamps
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper()),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

class RequestCodePost(BaseModel):
    mode: str
    fan_speed: str
    temperature: int

@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    logger.info("Starting application")
    try:
        yield
    finally:
        # shutdown
        logger.info("Stopping application")

app = FastAPI(
    title="Fetch AC IR Codes",
    lifespan=lifespan,
)

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


@app.get("/health")
def health():
    """Health check endpoint."""
    return {
        "status": "ok",
    }

@app.post("/fetch_ac_ir_codes")
def fetch_ac_ir_codes(req: RequestCodePost):
    """Fetch AC IR codes endpoint."""
    # Placeholder implementation
    logger.info("Fetching AC IR codes")

    return {
        "payload": req,
    }