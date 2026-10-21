# tg-stream PoC: direct watch from the Lulu catalog

Design: `/workspace/tg-stream-design/DESIGN.md` (box). Lulu catalog only; SearchGram, the downloader throttle, Save-reuse and Hermes tools are out of scope.

## How it works
- `POST /api/watch/{variant_id}` (main app, :8788) checks the variant is in the catalog with a `video/*` mime and writes
  `TG_STREAMS_DIR/<Title> (<Year>)/<Title> (<Year>) - <quality>.strm` (signed URL), `movie.nfo` (lockdata on), `poster.jpg` and a `.tgstream.json` marker.
  It warms the first/last 2 MB in the background (Lulu has no delivery quota, DESIGN §3b), queues the subtitle sidecar job (MKV only, below),
  then makes Jellyfin index it (field-tested order, NUC 2026-10-21):
  1. `POST /Library/Media/Updated` with the Jellyfin-side folder (on 12.1 this alone did **not** trigger a scan), then poll `/Items` by path every 1.5 s
     (newest-first listing, then `searchTerm`; a second quality grouped into the same movie matches by folder; if `/Items` is refused without a user, `/Users/{admin}/Items`).
  2. After `JELLYFIN_FALLBACK_AFTER_S` (6 s): `POST /Items/{lib}/Refresh?Recursive=true…` (surfaced the item in ~5 s on the NUC). The library id is
     `JELLYFIN_STREAMS_LIBRARY_ID`, else discovered once via `GET /Library/VirtualFolders` (Locations contains `JELLYFIN_STREAMS_PATH`) and cached.
     If the library still has 0 items (created while empty: "inaccessible or empty, skipping"), or no library is found: one `POST /Library/Refresh`.
  3. After 18 s still nothing: one `POST /Library/Refresh`. Full refreshes are guarded by a 15 min cooldown (process-wide).
  The whole thing is bounded by `JELLYFIN_POLL_TIMEOUT_S` (45 s) **including request time** (each call ≤ 8 s). The response always has `status` + `message`
  (+ `steps`, `elapsed_s`, `subtitles`): `ready` (+ `url` …/web/#/details?id=…), `pending`, `refresh_skipped` (no key), `refresh_failed` (unreachable), `auth_failed` (401/403).
- `GET /api/watch/{variant_id}`: one cheap lookup (no refresh) → `ready`+`url` / `pending` / `not_prepared`. The UI polls it for ~60 s after a `pending`.
- Jellyfin auth everywhere: `Authorization: MediaBrowser Token="<key>"` only (12.1 answers 401 to the legacy Emby token header); the key is never in a URL.
  `tests/test_jellyfin_auth_audit.py` enforces it for telegram-browser and telegram-downloader.
- `GET/HEAD /s/{variant_id}/{sig}` is served ONLY by a second listener (`TG_STREAM_PORT`, 8789) in the same process, so it uses the app's live Telethon client.
  No second session or client is created. The main app answers 404 for `/s/*`.
  - 200/206/416, `Accept-Ranges`, `Content-Range`, suffix ranges and HEAD are supported; malformed or multi-range requests are answered with 200.
  - Reads use aligned 512 KiB `upload.getFile` chunks over at most 2 senders per DC (ported from the downloader's `fast_download.SenderPool`), with read-ahead of 1 and an in-memory LRU cache (`TG_STREAM_CACHE_MB`, 128).
  - At most 2 concurrent streams (distinct variants), 16 requests per stream (was 4: a browser + Jellyfin's probe/subtitle ffmpeg hit it on first play → 503 → stall/failed seek).
  - Headers: `Content-Type` from the file extension (`.mkv` → `video/x-matroska`; Telegram mime types vary), strong `ETag` (`"tg<variant>-<size>"`), `If-Range` honoured
    (mismatch → full 200), `Content-Length` on HEAD, CORS only for exact `TG_STREAM_CORS_ORIGINS` (default: the Jellyfin public URL's origin; only used if Jellyfin Web sends `crossorigin=use-credentials`).
  - Priority lane: background readers (subtitle extraction) fetch only while no foreground fetch is in flight, one chunk at a time, never into the LRU cache.
  - The file reference is refreshed once. A deleted message gives 410. FloodWait of 10 s or less is waited once; longer gives 503 + `Retry-After` and new streams pause until it expires. Errors mid-body close the connection.
  - HMAC-SHA256 per-variant signature (128-bit, constant-time compare); the signature is stripped from access logs.
- Subtitle sidecars (`app/stream/subs.py`, MKV only, `TG_SUBS_ENABLED=1`): pure-Python Matroska reader; text tracks (`S_TEXT/UTF8`, zlib / header-stripping
  decoded; PGS/ASS ignored) → `<strm stem>.<lang>[.forced].srt` (one per language+forced, Hebrew first, max 3), written atomically, recorded in the marker
  (`subs`), then `POST /Items/{item}/Refresh` (non-recursive) so Jellyfin lists them; Jellyfin's default-subtitle pick puts external tracks first.
  Cost: subtitle blocks are interleaved, so the whole file is read once (~size / 6-8 MB/s ≈ 2.5-3 min for 1.1 GB when idle; slower while someone plays).
  Non-cluster data (attachments, cues) is skipped without download; a file with no text track stops after the header (1-2 chunks).
  One job at a time, cap `TG_SUBS_MAX_MB` (4096) and `TG_SUBS_MAX_MINUTES` (30). Status in the watch response and `/health` (`tg_stream.subs`).
- Cleanup: `docker exec telegram-browser python -m app.stream.cleanup --all` is a dry run; add `--apply` to delete, or use `--variant ID` (removes that variant's `.strm` and its `.srt` sidecars).

## Environment (compose sets the non-secret ones)
| Var | Default | Notes |
|---|---|---|
| `TG_STREAM_HMAC_KEY_FILE` | — | ≥32 bytes. Without it, streaming is off and watch returns 503 |
| `TG_STREAM_PUBLIC_BASE` | — | e.g. `http://<LAN_IP>:8789`, written into .strm |
| `TG_STREAM_PORT` / `TG_STREAM_BIND` | 8789 / 0.0.0.0 | in-container; host publish is LAN-IP only |
| `TG_STREAM_CONNECTIONS` / `TG_STREAM_MAX_STREAMS` | 2 / 2 | capped at 2 |
| `TG_STREAM_CACHE_MB` / `TG_STREAM_PREFETCH_MB` | 128 / 2 | |
| `TG_STREAMS_DIR` | /streams | |
| `JELLYFIN_URL` / `JELLYFIN_PUBLIC_URL` | http://<LAN_IP>:8096 / = URL | |
| `JELLYFIN_API_KEY_FILE` / `JELLYFIN_API_KEY` | — | file wins if readable, else env (wired by reference to an existing key, see deploy README); none → files written, refresh skipped |
| `JELLYFIN_STREAMS_PATH` | /media/telegram-streams | path as Jellyfin sees the streams dir |
| `JELLYFIN_STREAMS_LIBRARY_ID` | — | optional; else discovered by path via `/Library/VirtualFolders` (cached) |
| `JELLYFIN_POLL_TIMEOUT_S` / `JELLYFIN_FALLBACK_AFTER_S` | 45 / 6 | wall budget incl. requests / when the library refresh kicks in |
| `TG_SUBS_ENABLED` / `TG_SUBS_MAX_MB` / `TG_SUBS_MAX_MINUTES` | 1 / 4096 / 30 | subtitle sidecars |
| `TG_STREAM_CORS_ORIGINS` | origin of `JELLYFIN_PUBLIC_URL` | comma list of exact origins |
| `POSTER_CACHE_DIR` | /data/posters | now overridable (tests) |

## Tests
`cd telegram-browser && pip install -r requirements.txt -r tests/requirements-dev.txt && PYTHONPATH=.:../shared python -m pytest -q tests`
(fakes only: no Telegram, no Jellyfin).

Credits: see `NOTICE` (FileToLink, Apache-2.0, range semantics reviewed; no code copied).
