# Smarthome

Self-hosted home services and automation running on the home server.

## Media Platform

The repository contains a Telegram-to-Jellyfin media platform with two user flows.

### Telegram flow

ZikriMedia is the direct media submission interface.

    ZikriMedia
        |
        v
    media message
        |
        +-- 👀 accepted
        |
        v
    persistent SQLite queue
        |
        v
    parallel MTProto download
        |
        v
    classification
        |
        +-- Movie -> /media/movies
        +-- TV    -> /media/tv
        +-- Other -> /media/incoming
        |
        v
    Jellyfin refresh
        |
        +-- 👍 available

Permanent failures receive a 👎 reaction.

### Browser flow

The browser provides a movie catalog backed by a read-only Telegram source channel.

    Lulu Telegram channel
        |
        v
    telegram-browser
        |
        +-- search
        +-- genre filter
        +-- IMDb/year sorting
        +-- pagination
        +-- quality selection
        |
        v
    telegram-downloader
        |
        v
    Jellyfin

The Lulu source channel is treated as read-only.

Download state is stored persistently in the downloader SQLite database and exposed in the browser:

    Blue    -> ready to download
    Orange  -> queued / downloading / processing
    Green   -> available
    Red     -> failed

The browser also exposes `/downloads` for persistent download history and status.

## Services

### telegram-downloader

Always-on Telegram media ingestion and download service.

Features:

- persistent SQLite queue
- restart recovery
- configurable concurrent workers
- parallel MTProto chunk downloading
- resumable downloads
- retries
- duplicate protection
- deterministic movie/TV classification
- Jellyfin-compatible media paths
- Jellyfin library refresh
- Telegram status reactions
- disk-space protection
- health/status API
- browser enqueue/status API

Default port:

    8787

### telegram-browser

Web catalog built from a Telegram movie channel.

Features:

- persistent SQLite catalog
- full Telegram history backfill
- incremental synchronization
- poster caching
- search
- genre filtering
- IMDb/year sorting
- stable pagination
- movie metadata
- quality variants
- downloader integration
- live download state
- persistent download status page

Default port:

    8788

## Media Storage

Host:

    /home/ben/media/
    ├── incoming/
    │   └── .part/
    ├── movies/
    └── tv/

Containers use:

    /media

Jellyfin libraries:

    Movies   -> /media/movies
    TV Shows -> /media/tv

## Running

Downloader:

    cd telegram-downloader
    docker compose up -d --build

Browser:

    cd telegram-browser
    docker compose up -d --build

## URLs

Telegram Browser:

    http://10.0.0.13:8788

Download Status:

    http://10.0.0.13:8788/downloads

Downloader Health:

    http://10.0.0.13:8787/health

Downloader Status:

    http://10.0.0.13:8787/status

Jellyfin:

    http://10.0.0.13:8096

## Secrets

Never commit:

- `.env`
- Telegram API credentials
- Telegram session files
- Jellyfin API keys
- SQLite runtime databases

Use `.env.example` files as configuration templates.

## Current State

The media platform is operational end-to-end:

- Telegram submission works
- browser discovery works
- downloads survive restarts
- parallel MTProto downloading is enabled
- classification and Jellyfin organization are automatic
- browser download state is persistent
- Telegram status uses reactions instead of reply messages

See `telegram-downloader/DESIGN.md` for architecture and future work.
