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
- `GET /api/watch/{variant_id}`: one cheap lookup (no refresh) → `ready`+`url` / `pending` (+ `in_progress`, `phase`, `steps`, `elapsed_s` while the POST
  job runs) / `not_prepared`, always with `library_url` and `subtitles` = `{state: queued|running|done|none|failed, hebrew, bytes, total, progress, eta_s, files}`.
  A second POST for the same variant joins the in-flight job (no second refresh). Every GET marks the subtitle job "boosted" for 15 s (popup open).
- UI (index.html): no new tab. An in-page popup with two live steps: (1) "מכינה את הסרט ב-Jellyfin" (phase text) and (2) "מכינה כתוביות בעברית"
  (% bar = bytes read / bytes planned, ETA). "לצפות עכשיו" appears once step 1 is done; when both are done (or the file has no Hebrew track:
  "אין כתוביות בעברית בקובץ הזה") the SAME tab navigates to Jellyfin. Errors: Hebrew text + "נסה שוב" + a link to the Telegram Streams library.
  Button disabled while pending (and the server joins duplicates). Cards: one row per variant: quality + size as text and two text buttons, "צפייה ישירה" (this popup) and "שמירה" (download, unchanged); side by side on wide cards, stacked on the ~170px cards at 360px; ≥ 44px tap targets.
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
- Shared in-flight chunk fetches (fix for the NUC truncated-srt bug, variant 34810): every Telegram chunk fetch runs in its own task with a waiter
  refcount. A reader that is cancelled (browser seek, Jellyfin closing a range) only leaves; the fetch is cancelled only when its last waiter leaves.
  A genuine mid-body failure is retried once (0.5 s), then the response is **aborted** (connection drop, never a clean short body; a body shorter than
  `Content-Length` is also aborted). Note: ffmpeg 7.1 still exits 0 on an aborted HTTP body, which is why the refcount fix matters.
  `/health` counters: `fetches_abandoned`, `mid_body_retries`, `aborted_responses`.
- Hebrew subtitle sidecar (`app/stream/subs.py`, MKV only, `TG_SUBS_ENABLED=1`), pure-Python Matroska reader, no external subtitle site/API:
  1. Header first (stops at the first Cluster). No Hebrew text track → state `none`, nothing written.
  2. Else a placeholder `<strm stem>.heb.default.srt` (`.heb.forced.srt` if the only Hebrew track is forced) is written **before** the Jellyfin refresh
     (`/api/watch` waits up to `TG_SUBS_HEADER_WAIT_S`, 6 s). Reason: Jellyfin 12.1 re-probes a `.strm` on every PlaybackInfo using a process-wide directory
     cache that only drops after 5 min idle; a sidecar added after the first listing is invisible, and is even removed from the item, until then.
  3. Hebrew blocks only. If the Cues index has `CueRelativePosition` for the track (ffmpeg and mkvmerge write it), only the chunks holding those blocks are read
     (sparse); else the whole file once. The final file replaces the placeholder atomically (same name: Jellyfin reads the file per request, no refresh needed),
     then `POST /Items/{item}/Refresh`.
  4. Exactly ONE sidecar, Hebrew: `<stem>.heb.default.srt`. The MKV **forced flag is ignored**: Lulu uploads flag their FULL Hebrew track
     default=1 forced=1 with a channel ad as the track name (NUC audit: 1003-1348 cues). `.heb.forced.srt` only for a truly forced track:
     the track title says forced/מאולצ/foreign, or it is sparse (< 15% of a full track's ~10 cues/min, i.e. < 1.5 cues/min; runtime unknown: < 50 cues).
     Round-1 `.heb.forced.srt` files that are dense are renamed in place to `.heb.default.srt` at startup and on Watch now (no Telegram read),
     then the Jellyfin item is refreshed. Sidecars from the first PoC build (`.heb.srt` + `.eng.srt`) are redone and deleted.
     Verified on 12.1.0 with a Lulu-style file (embedded heb subrip default+forced, ad title, eng audio): with `.heb.default.srt` the external
     track is DefaultSubtitleStreamIndex (/Items and PlaybackInfo, which jellyfin-web uses) for SubtitleMode Default/Always/Smart, pref '' or heb,
     RememberSubtitleSelections on or off, as long as nothing is remembered for the item. Exceptions: a remembered selection for the item
     (`-1` = Off, or the embedded index) wins while RememberSubtitleSelections is on; the stale directory cache (≤ 5 min after the file changed)
     hides the sidecar from PlaybackInfo; Smart with Hebrew AUDIO behaves like OnlyForced and would pick the embedded forced-flagged track.
     DisplayTitle of ours: "Hebrew - Default - SUBRIP - External" (no ad). The embedded ad title cannot be edited in Jellyfin; it can only be
     hidden with the library option AllowEmbeddedSubtitles=AllowNone (verified: hides ALL embedded subs of that library) or by remuxing.
  Cost: real mkvmerge file 224 MB/10 min, 98 Hebrew cues → 100 of 449 chunks (22%); a 4 GB 2 h 1080p file ≈ ≤1200 of ~7800. Dense low-bitrate files gain little.
  One job at a time, 6 parallel chunk reads while the popup is open (foreground playback still first), else 1 and only when no foreground fetch is in flight.
  Cap `TG_SUBS_MAX_MB` (4096) and `TG_SUBS_MAX_MINUTES` (30). Every outcome is logged (`[subs] <vid>: queued/skipped/already done/none/done/failed`, progress every 60 s).
- Jellyfin's own extraction cache (`/config/data/subtitles/<id[:2]>/<id GUID>/<index>.srt`) is never invalidated for `.strm` items and accepts partial ffmpeg
  output; a truncated extraction stays. The external sidecar avoids extraction entirely. `tools/subs-audit.sh` (read-only; `ssh ben@<nuc> 'bash -s' < tools/subs-audit.sh`) finds truncated cache files and
  prints the (approval-gated) `rm -rf` per item.
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
| `TG_SUBS_HEADER_WAIT_S` | 6 | how long Watch now waits for the placeholder sidecar before asking Jellyfin to scan |
| `TG_STREAM_CORS_ORIGINS` | origin of `JELLYFIN_PUBLIC_URL` | comma list of exact origins |
| `POSTER_CACHE_DIR` | /data/posters | now overridable (tests) |

## Tests
`cd telegram-browser && pip install -r requirements.txt -r tests/requirements-dev.txt && PYTHONPATH=.:../shared python -m pytest -q tests`
(fakes only: no Telegram, no Jellyfin). E2E with a real MKV + ffmpeg under overlapping range cancels:
`PYTHONPATH=.:../shared python -m tests.e2e_cancel_ffmpeg file.mkv` (cue count must match a direct extraction). `python -m tests.mkv_layout file.mkv`
prints the Cues/sparse-read cost.

## Recommended Jellyfin user prefs (not applied by anything here)
`RememberSubtitleSelections` **off** (the NUC's The Fix / Half Baked showed -1: a remembered "Off"; with it off the server ignores the saved
choice and clears it on the next play), `SubtitleMode` Default or Always, `SubtitleLanguagePreference` `heb` (optional with `.heb.default.srt`;
it keeps the right pick for files without our sidecar). Avoid Smart for Hebrew-audio titles. Keep RememberSubtitleSelections on only if
per-title choices matter more; then pick "Hebrew - Default - External" once per title.
