# tg-stream PoC: direct watch from the Lulu catalog

Design: `/workspace/tg-stream-design/DESIGN.md` (box). Lulu catalog only; SearchGram, the downloader throttle, Save-reuse and Hermes tools are out of scope.

## How it works
- `POST /api/watch/{variant_id}` (main app, :8788) checks the variant is in the catalog with a `video/*` mime and writes
  `TG_STREAMS_DIR/<Title> (<Year>)/<Title> (<Year>) - <quality>.strm` (signed URL), `movie.nfo` (lockdata on), `poster.jpg` and a `.tgstream.json` marker.
  It warms the first/last 2 MB in the background (Lulu has no delivery quota, DESIGN §3b), then calls `POST /Library/Media/Updated` with the Jellyfin-side path.
  It polls `/Items` by path every 2 s for up to 45 s. If `JELLYFIN_STREAMS_LIBRARY_ID` is set, it does one library-scoped refresh after 10 s.
  It returns `{status: ready, url: .../web/#/details?id=…}`, `pending`, `refresh_skipped` (no API key) or `refresh_failed`.
- `GET/HEAD /s/{variant_id}/{sig}` is served ONLY by a second listener (`TG_STREAM_PORT`, 8789) in the same process, so it uses the app's live Telethon client.
  No second session or client is created. The main app answers 404 for `/s/*`.
  - 200/206/416, `Accept-Ranges`, `Content-Range`, suffix ranges and HEAD are supported; malformed or multi-range requests are answered with 200.
  - Reads use aligned 512 KiB `upload.getFile` chunks over at most 2 senders per DC (ported from the downloader's `fast_download.SenderPool`), with read-ahead of 1 and an in-memory LRU cache (`TG_STREAM_CACHE_MB`, 128).
  - At most 2 concurrent streams (distinct variants), 4 requests per stream.
  - The file reference is refreshed once. A deleted message gives 410. FloodWait of 10 s or less is waited once; longer gives 503 + `Retry-After` and new streams pause until it expires. Errors mid-body close the connection.
  - HMAC-SHA256 per-variant signature (128-bit, constant-time compare); the signature is stripped from access logs.
- Cleanup: `docker exec telegram-browser python -m app.stream.cleanup --all` is a dry run; add `--apply` to delete, or use `--variant ID`.

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
| `JELLYFIN_STREAMS_LIBRARY_ID` | — | optional, enables the library-scoped fallback refresh |
| `POSTER_CACHE_DIR` | /data/posters | now overridable (tests) |

## Tests
`cd telegram-browser && pip install -r requirements.txt -r tests/requirements-dev.txt && PYTHONPATH=.:../shared python -m pytest -q tests`
(fakes only: no Telegram, no Jellyfin).

Credits: see `NOTICE` (FileToLink, Apache-2.0, range semantics reviewed; no code copied).
