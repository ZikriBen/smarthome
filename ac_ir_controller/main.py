import logging
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from contextlib import asynccontextmanager
from pydantic import BaseModel
from settings import settings
from src.code_mapper import load_codes, lookup_ir


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
    logger.info("Starting application")
    codes_path = getattr(settings, "CODES_PATH", "data/ir_codes.json")
    try:
        app.state.codes = load_codes(codes_path)
    except Exception:
        logger.exception("Failed to load IR codes from %s", codes_path)
        raise
    try:
        yield
    finally:
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
        lookup_ir(
            app.state.codes,
            req.mode,
            req.fan_speed,
            req.temperature,
        )
    }