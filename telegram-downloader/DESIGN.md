# Telegram Media Platform — Design

## Goal

Provide a simple self-hosted media workflow backed by Telegram and Jellyfin.

There are two supported entry points:

1. Direct media submission through ZikriMedia.
2. Movie discovery and selection through telegram-browser.

Normal users should not need access to Docker, the server filesystem, or Jellyfin administration.

## Architecture

    ZikriMedia
        |
        | media
        v
    telegram-downloader
        |
        |-- Telethon
        |-- SQLite queue
        |-- concurrent workers
        |-- parallel MTProto downloader
        |-- classifier
        |
        +--------------------------+
                                   |
    Lulu movie channel             |
        |                          |
        | read-only                |
        v                          |
    telegram-browser               |
        |                          |
        |-- catalog SQLite         |
        |-- search/filter/sort     |
        |-- pagination             |
        |-- download UI            |
        |-- status UI              |
        |                          |
        +--------------------------+
                                   |
                                   v
                             Media storage
                                   |
                                   v
                                Jellyfin

## Telegram Flow

ZikriMedia is the interactive Telegram interface.

    media message
        |
        v
       👀
        |
        v
      QUEUED
        |
        v
    DOWNLOADING
        |
        v
    PROCESSING
        |
        v
    AVAILABLE
        |
        v
       👍

Permanent failure:

    👀 -> FAILED -> 👎

Telegram reactions are best-effort.

A reaction failure must never change download state or trigger a retry.

The downloader listens for Telegram updates globally and filters ZikriMedia by chat ID in application code.

## Browser Flow

telegram-browser indexes a read-only Telegram movie source.

The catalog provides:

- posters
- Hebrew and English titles
- year
- IMDb rating
- genres
- descriptions
- quality variants
- search
- genre filtering
- IMDb/year sorting
- pagination

Selecting a quality sends the Telegram source chat ID and message ID to telegram-downloader.

The Lulu channel is read-only.

The downloader must never attempt to send messages or reactions to Lulu.

Browser download state comes from the downloader SQLite database.

    BLUE
    ready to download

    ORANGE
    QUEUED
    DOWNLOADING
    PROCESSING
    RETRY_WAIT

    GREEN
    AVAILABLE

    RED
    FAILED

The browser exposes `/downloads` for persistent browser-download status and history.

## Job State

SQLite is the source of truth.

    QUEUED
       |
       v
    DOWNLOADING
       |
       v
    PROCESSING
       |
       v
    AVAILABLE

Retry:

    DOWNLOADING
       |
       v
    RETRY_WAIT
       |
       +----> DOWNLOADING

Terminal failure:

    FAILED

Job uniqueness is:

    (telegram_chat_id, telegram_message_id)

This prevents the same Telegram message from being queued twice.

## Success Boundary

Download state and notification state are deliberately separate.

Once the final media file exists:

    completed file
         |
         v
    mark AVAILABLE in SQLite
         |
         +-- Jellyfin refresh (best effort)
         |
         +-- Telegram reaction (best effort)

Neither Jellyfin nor Telegram notification failure may cause the media to be downloaded again.

If a recovered job already has its final file, it is restored to AVAILABLE instead of being redownloaded.

## Downloads

Multiple files can download concurrently.

Configuration:

    MAX_CONCURRENT_DOWNLOADS=5
    TELEGRAM_DOWNLOAD_CONNECTIONS=2

A single file may use multiple MTProto senders.

Parallel lane state is preserved across transient failures so downloads can resume.

Temporary download state lives under:

    /media/incoming/.part

Incomplete files are never exposed to Jellyfin.

## Classification

Classification is deterministic and conservative.

Supported media types:

    MOVIE
    TV
    UNKNOWN

TV examples:

    S03E07
    S3E7
    3x07
    Season 3 Episode 7
    עונה 3 פרק 7
    ע3 פ7

Movie detection primarily uses a clean title plus year.

Release noise such as resolution, codec, translation markers, and source tags is removed where possible.

Unknown or ambiguous media should remain unknown rather than use risky fuzzy matching.

## Media Layout

Host:

    /home/ben/media/
    ├── incoming/
    │   └── .part/
    ├── movies/
    └── tv/

Movie example:

    /media/movies/Movie Name (2026)/Movie Name (2026).mkv

TV example:

    /media/tv/Show Name/Season 03/Show Name - S03E07.mkv

Unknown media remains under:

    /media/incoming

## Jellyfin

After successful media placement the downloader requests:

    POST /Library/Refresh

Jellyfin refresh is best-effort.

A temporary Jellyfin failure does not turn a successful download into a failed job.

## Browser Catalog

telegram-browser maintains its own SQLite catalog.

It performs:

1. initial Telegram scan
2. historical backfill
3. incremental synchronization for new messages

Pagination operates on the local catalog rather than directly on Telegram history.

This means new Telegram messages do not destabilize older pages.

Poster files are cached locally with a bounded cache.

## Downloader API

Default:

    http://10.0.0.13:8787

Important endpoints:

    GET  /health
    GET  /status
    GET  /jobs
    POST /enqueue
    POST /jobs/status

telegram-browser talks to this API rather than opening downloader SQLite directly.

## Browser

Default:

    http://10.0.0.13:8788

Pages:

    /
        Movie catalog

    /downloads
        Download status/history

## Persistence

Downloader database:

    telegram-downloader/data/downloader.db

Browser database:

    telegram-browser/data/catalog.db

Telegram sessions and runtime databases are persistent local data and must never be committed.

## Security

Never commit:

- .env
- Telegram API credentials
- Telegram session files
- Jellyfin API keys
- runtime SQLite databases

The browser is intended for the trusted home LAN.

Authentication should be added before exposing it publicly.

## Completed — v1

### Downloader

- persistent SQLite queue
- restart recovery
- duplicate protection
- concurrent workers
- parallel single-file MTProto downloads
- resumable transfer state
- retry handling
- disk protection
- movie classification
- TV classification
- Jellyfin-compatible paths
- automatic Jellyfin refresh
- Telegram status reactions
- health/status API
- browser enqueue/status API

### Browser

- persistent Telegram catalog
- complete history backfill
- incremental synchronization
- bounded poster cache
- metadata parsing
- quality grouping
- search
- genre filtering
- IMDb sorting
- year sorting
- stable pagination
- skeleton loading
- downloader integration
- live button status
- persistent download-status page

## v2

v2 should focus on operational control and observability rather than rebuilding the ingestion pipeline.

### Download Progress

Persist:

    downloaded_bytes
    total_bytes
    current_speed

Example browser status:

    63% · 3.1 MB/s

### Queue Controls

Possible `/downloads` actions:

    retry
    cancel
    requeue
    download next

### Explicit Job Origin

Add:

    origin = TELEGRAM | BROWSER

This removes the need to infer origin from Telegram chat IDs.

### Cleanup

Automatic retention policies for:

- abandoned .part files
- stale MTProto lane files
- old failed jobs
- old completed operational records

Media files themselves should not be automatically deleted.

### Better Duplicate Detection

Current uniqueness prevents duplicate requests for the same Telegram message.

Future detection may recognize that equivalent media already exists in storage even when requested from another Telegram message.

### Observability

Potential endpoint:

    GET /metrics

Useful metrics:

- queue size
- active downloads
- current throughput
- retry rate
- failures
- free disk
- Telegram FloodWait events

### Tests

Add regression/state-machine coverage for:

- restart recovery
- notification failure after successful download
- duplicate enqueue
- existing final-file recovery
- browser-origin read-only behavior
- retry exhaustion

## Guiding Principle

Telegram flow:

    send media
       |
       v
      👀
       |
       v
      wait
       |
       v
      👍
       |
       v
    open Jellyfin

Browser flow:

    find movie
       |
       v
    choose quality
       |
       v
    blue -> orange -> green
       |
       v
    open Jellyfin

Infrastructure should remain invisible to normal users.
