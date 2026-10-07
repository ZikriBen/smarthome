# Command Center

Internal execution boundary for the home assistant. It owns privileged
integrations; Hermes never receives the Docker socket, host mounts, or a shell.

## Current capability catalog

- Read immediately: host health, Docker container state, map lookup, price-watch
  status and alerts.
- Approval required: Docker restart and creation of a price watch.
- Price checks run on their configured interval (minimum 60 minutes), retain the
  last known good price, and record a deduplicated alert when the target is met.

Only public `http(s)` product URLs are accepted. Private, Tailscale, loopback,
link-local and RFC1918 destinations are rejected before a fetch.

## Architecture direction

The next layer is a constrained Command Center MCP adapter. It will expose a
small set of general capabilities—read-only inspection, scoped files,
browser/API calls, and approval-gated execution—rather than a separate agent
tool for every service. Arbitrary shell access will never be exposed directly
to Telegram; high-impact commands are proposed, independently approved, then
run in a scoped worker with an audit record.
