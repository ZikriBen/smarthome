# Command Center

Internal execution boundary for the home assistant. It owns privileged
integrations; Hermes never receives the Docker socket, host mounts, or a shell.

## Current capability catalog

- Read immediately: host health, Docker container state, Uptime Kuma monitor
  health, media-download status, map lookup, price-watch status and alerts.
- SearchGram: search and paginate results. A selected result is handed to the
  existing Telegram Downloader only after the user explicitly confirms its
  number on the displayed page; that action is auditable.
- Jellyfin: read-only library search returns matching media metadata and
  watched state. The Jellyfin credential is held only by the Command Center.
- Approval required: Docker restart and creation of a price watch.
- Groceries: the working list lives in Google Tasks ("קניות", own token
  `data/google-tasks-token.json`, scope `tasks` only); `data/grocery/state/current_list.json`
  is a cache used when Tasks is unavailable. Price runs are read-only.
- Price checks run on their configured interval (minimum 60 minutes), retain the
  last known good price, and record a deduplicated alert when the target is met.

Only public `http(s)` product URLs are accepted. Private, Tailscale, loopback,
link-local and RFC1918 destinations are rejected before a fetch.

## Hermes integration

Hermes reaches this service through a separate internal Streamable HTTP MCP
adapter. The adapter has no Docker socket or host mounts; it only calls this
authenticated allowlisted API. The currently exposed tools are explicitly
allowlisted in `hermes/config.yaml`. Arbitrary shell access is never exposed
to Telegram; high-impact commands are proposed for independent approval and
the Command Center records the approval and execution state.

Uptime Kuma is queried through its read-only Prometheus metrics API. Its API
key is supplied only to the Command Center as `UPTIME_KUMA_API_KEY`; monitor
URLs and the key are never returned to Hermes.

## Code structure

`app.py` and `mcp-server.py` are transport launchers only. The implementation
is grouped by domain in the `command_center` package:

```text
command_center/
  api.py                 HTTP authentication and dispatch
  approvals.py           audited, single-use action registry
  config.py              environment and path configuration
  database.py            SQLite schema and connection
  files.py               attachments, workspace, and PDF reads
  google_workspace.py    OAuth, Calendar, and Gmail
  tasks_store.py         Google Tasks list access (grocery list)
  grocery_list.py        Tasks-backed working grocery list + offline cache
  groceries.py           grocery tools and background price runs
  docker.py              Docker reads and approved restarts
  host.py                host health
  jellyfin.py            read-only Jellyfin library access
  maps.py                 public place search
  price_watches.py        public price reads and watches
  searchgram.py           SearchGram and downloader workflow
  uptime_kuma.py          read-only monitor status
  mcp_tools/             MCP registrations grouped by the same domains
```

To add a capability, put its business logic and HTTP routes in the appropriate
domain module's `register_routes(router)`. Add the matching MCP-facing function
to that domain's `mcp_tools` module. New approved writes are registered once
through `approvals.register_action`; integrations do not implement their own
approval state machines.

Approvals are decided once: `POST /v1/approvals/<id>/(approve|deny)` (body
`{"via": "button"|"chat", "actor": "<name>"}`) claims the row atomically and
records who/how/when in `approval_decisions`; any later call returns the recorded
outcome with `already_decided: true` (HTTP 200) instead of an error. `GET
/v1/approvals/<id>` returns a payload-free status + one-line summary for the
Telegram prompt. Pending approvals expire after 15 minutes. The Hermes Telegram
adapter (`hermes/cc_approvals.py`) accepts both a button tap and a chat reply
("מאשר"/"כן"/"לא"/"בטל", or a reply to the prompt) and edits the prompt to the
outcome, removing its buttons.
