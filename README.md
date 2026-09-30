# Telegram Media Downloader

A self-hosted Telegram-to-Jellyfin media ingestion service.

It listens to a dedicated Telegram group, accepts media from group members,
downloads it reliably, classifies it as TV / movie / unknown, organizes it
into Jellyfin-compatible folders, and triggers a Jellyfin library refresh.

---

# Current Flow

```text
Telegram group
    │
    ▼
media message
    │
    ▼
📥 Accepted
    │
    ▼
SQLite queue
    │
    ▼
download worker
    │
    ▼
resumable .part file
    │
    ▼
classification
    │
    ├── TV
    │    └── /media/tv/Show/Season XX/...
    │
    ├── Movie
    │    └── /media/movies/Movie (Year)/...
    │
    └── Unknown
         └── /media/incoming/...
    │
    ▼
Jellyfin refresh
    │
    ▼
✅ Available
```

---

# Telegram

Configured group:

```text
ZikriMedia
```

The service uses a persistent Telethon user session.

Any member of the configured group can submit supported media.

The service replies to the original message when accepted:

```text
📥 Accepted
```

and again when the file becomes available:

```text
✅ Available
```

---

# Architecture

```text
Telegram
   │
   ▼
Telethon listener
   │
   ▼
SQLite
   │
   ▼
Download queue
   │
   ├── worker 1
   ├── worker 2
   ├── worker 3
   ├── worker 4
   └── worker 5
   │
   ▼
/media/incoming/.part
   │
   ▼
classifier
   │
   ├── TV
   ├── Movie
   └── Unknown
   │
   ▼
Jellyfin libraries
```

---

# Job States

```text
RECEIVED
   ↓
QUEUED
   ↓
DOWNLOADING
   ↓
PROCESSING
   ↓
AVAILABLE
```

Retry flow:

```text
DOWNLOADING
   ↓
RETRY_WAIT
   ↓
DOWNLOADING
```

Permanent failure:

```text
FAILED
```

---

# Concurrency

Configured through:

```env
MAX_CONCURRENT_DOWNLOADS=5
```

This controls the number of separate files downloaded simultaneously.

It does not yet parallelize one file across multiple MTProto connections.

Observed behavior:

```text
1 file  ≈ 1.5 MB/s
2 files ≈ 1.5 MB/s each
```

So aggregate throughput increases with multiple workers.

Future work will experiment with parallel MTProto chunk downloading for a
single large file.

---

# Resumable Downloads

Incomplete files live under:

```text
/media/incoming/.part
```

If the service or server restarts, the downloader resumes from the existing
partial file.

Example:

```text
1114 MB / 1381 MB downloaded
        ↓
container restart
        ↓
job recovered from SQLite
        ↓
.part file found
        ↓
resume from 1114 MB
```

This has been tested across container restarts.

---

# Classification

The classifier is deterministic and regex-based.

## TV

Supported patterns include:

```text
S03E07
S3E7
3x07
Season 3 Episode 7
Season 3 Ep 7

עונה 3 פרק 7
פרק 7 עונה 3
ע3 פ7
ע'3 פ'7
```

Multi-episode examples:

```text
S02E05E06
S02E05-06
S02E05-E06
```

TV path example:

```text
Breaking.Bad.S03E07.1080p.mkv
```

becomes:

```text
/media/tv/Breaking Bad/Season 03/Breaking Bad - S03E07.mkv
```

Hebrew example:

```text
המנטליסט ע3 פ21.mp4
```

becomes:

```text
/media/tv/המנטליסט/Season 03/המנטליסט - S03E21.mp4
```

---

# Movie Classification

Movies are primarily detected using a clean title plus a standalone year.

Example:

```text
לולו_סרטים_לדפוק_חתונה_2005_ת.מ_720P.mkv
```

becomes:

```text
/media/movies/לדפוק חתונה (2005)/לדפוק חתונה (2005).mkv
```

Other real examples:

```text
לולו_סרטים_הדרקון:_סיפורו_של_ברוס_לי_1993_ת_מ_720P.mkv

→
/media/movies/
  הדרקון: סיפורו של ברוס לי (1993)/
  הדרקון: סיפורו של ברוס לי (1993).mkv
```

Release noise is removed when possible, including:

```text
720p
1080p
WEB-DL
WEBRip
BluRay
DVDRip
x264
x265
HEVC

תרגום מובנה
ת.מ
ישראלי
```

---

# Title Cleanup

The classifier prefers the filename when it contains a sane title.

Captions are used as fallback.

Telegram Markdown and invite links are stripped so values such as:

```text
+YSofgZNVx71hNTY0
```

do not become show names.

This issue was discovered with later episodes of The Mentalist and is covered
by regression tests.

---

# Jellyfin Layout

Host layout:

```text
/home/ben/media/
├── incoming/
│   └── .part/
├── movies/
└── tv/
```

Container layout:

```text
/media/movies
/media/tv
```

Jellyfin libraries:

```text
Movies   → /media/movies
TV Shows → /media/tv
```

Unknown files remain in:

```text
/home/ben/media/incoming
```

They are not automatically exposed as part of the normal movie/TV libraries.

---

# Jellyfin Refresh

After a media file becomes available, the downloader requests a Jellyfin
library refresh.

Configuration:

```env
JELLYFIN_URL=http://10.0.0.13:8096
JELLYFIN_API_KEY=
```

The API key must be created in Jellyfin and stored only in the local `.env`.

Authentication uses:

```text
Authorization: MediaBrowser Token="..."
```

The refresh endpoint is:

```text
POST /Library/Refresh
```

A successful request returns:

```text
204 No Content
```

---

# Existing Media Migration

Existing files can be reclassified without downloading them again.

Dry run:

```bash
python3 scripts/reclassify_existing.py
```

Apply:

```bash
python3 scripts/reclassify_existing.py --apply
```

The script:

- scans `/home/ben/media/incoming`
- classifies each file
- prints the planned destination
- moves only when `--apply` is provided
- does not overwrite existing files
- leaves unknown files untouched

Always run the dry run first.

---

# SQLite

Persistent database:

```text
data/downloader.db
```

Useful inspection:

```bash
sqlite3 data/downloader.db \
'SELECT id, status, original_filename, local_path FROM download_jobs ORDER BY id;'
```

Job IDs are persistent database IDs.

For example:

```text
Worker 4 claimed job 11
```

means:

```text
Worker 4 = one of the active download workers
Job 11  = persistent SQLite job ID
```

---

# Health

Health endpoint:

```text
http://10.0.0.13:8787/health
```

Operational status:

```text
http://10.0.0.13:8787/status
```

Uptime Kuma monitors `/health`.

Example health response:

```json
{
  "status": "ok",
  "free_disk_gb": 600.0,
  "min_free_disk_gb": 50
}
```

---

# Disk Protection

Configured using:

```env
MIN_FREE_DISK_GB=50
```

If available storage drops below the minimum, new downloads are rejected.

---

# Environment

Example:

```env
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_CHAT_ID=

DATABASE_PATH=/data/downloader.db
MEDIA_ROOT=/media

MAX_CONCURRENT_DOWNLOADS=5
MAX_RETRIES=5
RETRY_BASE_SECONDS=5

MIN_FREE_DISK_GB=50

HEALTH_HOST=0.0.0.0
HEALTH_PORT=8787

JELLYFIN_URL=http://10.0.0.13:8096
JELLYFIN_API_KEY=
```

Never commit the real `.env`.

---

# Running

Build/start:

```bash
docker compose up -d --build
```

Logs:

```bash
docker logs -f telegram-downloader
```

Restart:

```bash
docker compose up -d --build --force-recreate
```

---

# Tests

Run all tests:

```bash
python3 -m unittest discover -s tests -v
```

Tests cover:

- English episode naming
- Hebrew episode naming
- multi-episode files
- title extraction
- movie title/year extraction
- release-noise cleanup
- Telegram Markdown/link cleanup
- Mentalist regression case
- Jellyfin path generation

Do not deploy classifier changes unless tests pass.

---

# Secrets

Never commit:

```text
.env
Telegram API hash
Telegram session
Jellyfin API key
SQLite runtime data
```

Persistent sensitive files include:

```text
data/telegram-downloader.session
data/downloader.db
```

---

# Current Completed Phases

## Completed

- Telegram listener
- persistent SQLite jobs
- accepted/available notifications
- concurrent workers
- retries
- resumable downloads
- health endpoint
- disk guard
- TV classification
- movie classification
- Jellyfin folder organization
- existing-media migration
- automatic Jellyfin refresh

## Next

1. Faster single-file downloads
2. Watched-channel discovery

---

# Planned: Faster Single-File Downloads

Current Telethon transfer speed is approximately:

```text
~1.5 MB/s per file
```

Multiple files can each sustain similar throughput.

The next performance phase will investigate multiple MTProto connections /
parallel chunk downloading for a single file while preserving:

- retry behavior
- resume support
- correct chunk ordering
- FloodWait handling
- file integrity

---

# Planned: Watched-Channel Discovery

The downloader will later watch selected Telegram channels.

Flow:

```text
watched source channel
       ↓
new media/link discovered
       ↓
present suggestion in ZikriMedia
       ↓
user chooses Download / Ignore
       ↓
normal existing queue
```

Important: watched channels will not automatically download everything.

The existing download/classification/Jellyfin pipeline will be reused.
