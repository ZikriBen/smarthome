import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "shared"))

_TMP = Path(tempfile.mkdtemp(prefix="tgb-tests-"))
# Fake, non-secret values only: app.web needs them at import time.
os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test-not-a-real-hash")
os.environ.setdefault("TELEGRAM_SOURCE_CHAT_ID", "-1001")
os.environ.setdefault("SEARCH_CHAT_ID", "-1002")
os.environ.setdefault("CATALOG_DATABASE_PATH", str(_TMP / "catalog.db"))
os.environ.setdefault("POSTER_CACHE_DIR", str(_TMP / "posters"))
