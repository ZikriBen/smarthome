# Command Center

Internal execution boundary for the home assistant. It owns privileged
integrations; Hermes never receives the Docker socket, host mounts, or a shell.

## Current capability catalog

- Read immediately: host health, Docker container state, Uptime Kuma monitor
  health, map lookup, price-watch status and alerts.
- Approval required: Docker restart and creation of a price watch.
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
