# Telegram Media Downloader — Design

## 1. Goal

Build an always-on home-server service that accepts media submitted to a
dedicated Telegram group, downloads it reliably, and makes it available to
Jellyfin.

The desired user experience is intentionally simple:

```text
User sends video to Telegram group
              │
              ▼
       📥 Accepted
              │
              ▼
         Download queue
              │
              ▼
           Download
              │
              ▼
          Processing
              │
              ▼
       Jellyfin media
              │
              ▼
       ✅ Available
```

The Telegram group is the user interface.

Users should not need access to the server, Docker, Jellyfin administration,
or any other infrastructure.

---

# 2. Proven PoC

The existing PoC has verified:

- Telegram MTProto authentication using Telethon
- Persistent Telegram user session
- Access to Telegram messages
- Detection of message media
- Downloading large media files
- Download progress reporting
- Retry handling
- Writing downloaded media to local storage
- Jellyfin reading the download directory
- Playback from another device on the home LAN

Current proven flow:

```text
Telegram
    │
    ▼
Telethon
    │
    ▼
Ubuntu server
    │
    ▼
media file
    │
    ▼
Jellyfin
    │
    ├── Phone
    ├── Computer
    └── TV
```

The production implementation replaces manual message selection with an
always-running Telegram listener and persistent queue.

---

# 3. Deployment phases

## Phase 1 — Reliable ingestion

P1 creates the production foundation.

### Telegram

- Listen to one configured Telegram group.
- Any member of that group may submit media.
- Detect supported video/document media automatically.
- One Telegram message represents one download job.
- Preserve Telegram media-group/album ID when present.
- Identify the submitting user.
- Reply to the original message when accepted.
- Reply again when the media becomes available.
- Report permanent failures.

Example:

```text
Ben:
<movie.avi>

Downloader:
📥 Accepted
movie.avi
700 MB
Queue position: 2
```

When finished:

```text
Downloader:
✅ Available
movie.avi is ready to watch in Jellyfin.
Downloaded in 6m 24s.
```

Failure:

```text
Downloader:
❌ Download failed
movie.avi could not be downloaded after 5 attempts.
```

### Downloading

- Persistent queue.
- Two concurrent downloads initially.
- Configurable concurrency.
- Retry transient Telegram/network failures.
- Exponential/backoff retry.
- Prevent duplicate downloads.
- Use temporary files while downloading.
- Never expose partially downloaded files to Jellyfin.
- Recover queued/in-progress jobs after process restart.
- Graceful shutdown.
- Track download duration and errors.

### Storage

Downloads first enter:

```text
/home/ben/media/incoming
```

Temporary files:

```text
/home/ben/media/incoming/<file>.part
```

Only successfully completed files are moved/renamed to their final path.

P1 may initially expose completed files directly through a generic Jellyfin
library.

Later phases will organize them into movies and TV libraries.

### State

SQLite stores persistent job state.

Initial fields:

```text
id
telegram_chat_id
telegram_chat_name
telegram_message_id
telegram_media_group_id

sender_id
sender_name

caption
original_filename
file_size

status
local_path

created_at
started_at
completed_at

attempt_count
last_error
```

Uniqueness:

```text
(telegram_chat_id, telegram_message_id)
```

This prevents duplicate ingestion.

### Job states

```text
RECEIVED
    │
    ▼
QUEUED       ← send 📥 Accepted
    │
    ▼
DOWNLOADING
    │
    ▼
PROCESSING
    │
    ▼
AVAILABLE    ← send ✅ Available
```

Failure path:

```text
DOWNLOADING
    │
    ▼
RETRY_WAIT
    │
    ├── retry → DOWNLOADING
    │
    └── attempts exhausted
              │
              ▼
            FAILED
              │
              └── send ❌ Failed
```

---

# 4. Phase 2 — Download performance

Once P1 is stable, optimize throughput.

There are two forms of concurrency:

```text
Multiple files:

file A ─────────────►
file B ─────────────►


Parallel chunks of one file:

file A:
chunk 1 ─────►
chunk 2 ─────►
chunk 3 ─────►
```

Start with multiple-file concurrency.

Initial configuration:

```text
MAX_CONCURRENT_DOWNLOADS=2
```

Measure:

- aggregate throughput
- per-file throughput
- Telegram FloodWait responses
- timeout frequency
- retries
- CPU usage
- disk throughput

Then test:

```text
1 concurrent download
2 concurrent downloads
3 concurrent downloads
4 concurrent downloads
```

Do not assume Telegram mobile's two-download behavior represents an
account-level API restriction.

Choose concurrency based on observed throughput and Telegram rate limiting.

Later we may implement parallel chunk downloading for individual large files.

---

# 5. Phase 3 — Telegram bot UX

Separate downloading from user interaction where useful.

Possible architecture:

```text
Telegram Group
      │
      ├── Bot
      │    ├── status
      │    ├── commands
      │    ├── classification questions
      │    └── notifications
      │
      └── Telethon user session
             │
             └── actual media download
```

Possible commands:

```text
/status
/queue
/retry
/cancel
/disk
```

Example:

```text
/queue

1. Breaking Bad S02E03    62%
2. Breaking Bad S02E04    waiting
3. Movie.mkv              waiting
```

---

# 6. Phase 4 — Media classification

Automatically determine whether submitted media is:

```text
Movie
TV episode
Other
```

Start with deterministic parsing.

Inputs:

- Telegram caption
- original filename
- sender
- source group
- media-group information

Recognize patterns such as:

```text
S01E04
S1E4
1x04
Season 1 Episode 4
Movie Name (2007)
```

Example:

```text
Breaking.Bad.S03E07.1080p.mkv
```

becomes:

```text
Type: TV
Series: Breaking Bad
Season: 3
Episode: 7
```

If confidence is insufficient, ask in Telegram rather than guessing.

Example:

```text
🤔 I couldn't identify this media.

[Movie]
[TV Episode]
[Other]
```

---

# 7. Phase 5 — Jellyfin organization

Final media layout:

```text
/home/ben/media/
│
├── incoming/
│
├── movies/
│   └── Sunshine (2007)/
│       └── Sunshine (2007).mkv
│
└── tv/
    └── Breaking Bad/
        └── Season 03/
            ├── Breaking Bad - S03E01.mkv
            └── Breaking Bad - S03E02.mkv
```

Jellyfin libraries:

```text
Movies → /media/movies
TV     → /media/tv
```

Jellyfin should never index `.part` files.

After successful classification:

```text
incoming
    │
    ▼
classify
    │
    ├── movie → movies/
    │
    └── TV → tv/
```

Optionally request a Jellyfin library refresh after media becomes available.

---

# 8. Sender and Telegram metadata

Telegram metadata should NOT normally be encoded into Jellyfin filenames.

For example, avoid:

```text
Ben_SearchGram_Breaking_Bad_S01E01.mkv
```

Instead use Jellyfin-compatible filenames:

```text
Breaking Bad - S01E01.mkv
```

Store provenance separately in SQLite:

```text
Sender: Ben
Telegram group: Family Media
Telegram message: 12837
Original filename: breaking.bad.s01e01.mkv
Final path: /media/tv/Breaking Bad/Season 01/...
```

This allows future UI/features without damaging Jellyfin metadata matching.

---

# 9. Phase 6 — Operations

Add operational functionality after the core workflow is stable.

## Health endpoint

Expose:

```text
GET /health
```

Healthy response:

```text
HTTP 200
```

Kuma monitors this endpoint.

Potential future endpoint:

```text
GET /metrics
```

## Disk protection

Never allow media downloads to fill the server disk.

Configuration:

```text
MIN_FREE_DISK_GB=50
```

When below the threshold:

```text
⚠️ Download rejected

Server storage is low.
42 GB free; minimum required is 50 GB.
```

## Monitoring

Track:

- service health
- queue size
- active downloads
- successful downloads
- failed downloads
- retry count
- Telegram FloodWait events
- current throughput
- free disk space

---

# 10. Container architecture

Production deployment uses Docker Compose.

Kubernetes is intentionally not required for the initial deployment.

```text
Docker
│
├── telegram-downloader
│      │
│      ├── Telethon
│      ├── queue workers
│      ├── SQLite
│      └── health server
│
└── jellyfin
```

Persistent host directories:

```text
/home/ben/media
/home/ben/smarthome/telegram-downloader/data
```

Container layout:

```text
/app
/data
/media
```

Example mounts:

```text
./data            → /data
/home/ben/media   → /media
```

---

# 11. Secrets

Never commit:

- Telegram API hash
- Telegram session
- bot token
- authentication credentials

Git contains:

```text
.env.example
```

Local machine contains:

```text
.env
```

Expected variables:

```text
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_CHAT_ID=

MAX_CONCURRENT_DOWNLOADS=2
MAX_RETRIES=5
MIN_FREE_DISK_GB=50

MEDIA_ROOT=/media
```

---

# 12. Proposed production source layout

```text
telegram-downloader/
│
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── telegram.py
│   ├── queue.py
│   ├── downloader.py
│   ├── database.py
│   ├── models.py
│   ├── notifications.py
│   └── health.py
│
├── data/
│
├── tests/
│
├── Dockerfile
├── compose.yaml
├── requirements.txt
├── .env.example
├── DESIGN.md
└── README.md
```

Responsibilities:

```text
main.py
    application lifecycle

config.py
    environment/config validation

telegram.py
    Telegram connection + event handling

queue.py
    job scheduling and concurrency

downloader.py
    media transfer/retry/progress

database.py
    SQLite persistence

models.py
    job/domain models

notifications.py
    Accepted / Available / Failed messages

health.py
    Kuma health endpoint
```

---

# 13. Phase 1 acceptance criteria

P1 is complete when:

1. Service starts automatically after server reboot.
2. It reconnects to Telegram without manual login.
3. A member sends a video to the configured group.
4. The service detects it.
5. A persistent job is created.
6. Telegram receives an `📥 Accepted` reply.
7. The media enters the queue.
8. Up to two files can download concurrently.
9. Temporary failures retry automatically.
10. Duplicate messages are not downloaded twice.
11. Completed media is moved atomically to the media directory.
12. Telegram receives a `✅ Available` reply.
13. Jellyfin can play the completed media.
14. Restarting the container does not lose queued jobs.
15. Kuma can monitor `/health`.
16. Low disk space prevents new downloads.
17. No credentials or Telegram sessions exist in Git.

---

# 14. Guiding principle

Telegram should feel like the application.

The family should only need to:

```text
Send media
     ↓
📥 Accepted
     ↓
wait
     ↓
✅ Available
     ↓
open Jellyfin
```

Everything else should happen automatically.
