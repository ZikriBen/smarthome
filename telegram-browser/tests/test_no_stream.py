"""tg-stream was removed (0013): guard against it coming back half-way, and keep downloads + season routes."""
import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

import app.web as web

ROOT = Path(__file__).resolve().parents[1]


def test_no_stream_package_or_routes():
    assert importlib.util.find_spec("app.stream") is None
    paths = {getattr(r, "path", "") for r in web.app.routes}
    assert not any(p.startswith(("/s/", "/api/watch")) for p in paths)
    assert {"/api/download/{message_id}", "/api/search/download", "/api/search/season"} <= paths


def test_no_stream_settings_or_ui():
    assert not [n for n in vars(web) if n.startswith(("TG_STREAM", "TG_SUBS", "JELLYFIN"))]
    page = TestClient(web.app).get("/")
    assert page.status_code == 200
    assert "צפייה ישירה" not in page.text and "watchOverlay" not in page.text
    assert "quality-button" in page.text          # the catalog card still downloads on tap


def test_compose_has_no_stream_listener_or_mounts():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    for needle in ("8789", "TG_STREAM", "tgstream", "/streams"):
        assert needle not in compose
    assert '"8788:8788"' in compose
