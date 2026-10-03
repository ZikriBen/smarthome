# Mail → RSS Service

Fetches the latest email from a configured sender, extracts the halachot text with a deterministic regex parser, and exposes it as an RSS feed for Homarr / Home Assistant.

No external API key required — extraction is fully local (see `src/determinstic_parser.py`).

## Running

```bash
cp .env.example .env
# fill in IMAP_USER, IMAP_PASS (Gmail app password), EMAIL_SENDER, RSS_LINK

docker compose up -d --build
```

Feed:

    http://10.0.0.13:8000/rss

Health:

    http://10.0.0.13:8000/health

Manual poll (checks IMAP immediately instead of waiting for `POLL_INTERVAL`):

```bash
curl -X POST http://10.0.0.13:8000/trigger
```

## Configuration

See `.env.example` for all settings. Notable ones:

- `IMAP_USER` / `IMAP_PASS` — Gmail requires an [app password](https://myaccount.google.com/apppasswords), not your normal login password
- `EMAIL_SENDER` — exact sender address to match (`EXACT_MATCH=true` by default)
- `STATE_FILE` — defaults to `/data/state.json`, persisted via the `./data` bind mount in `compose.yaml`

## Testing without a real email

State is just a JSON file read fresh on every request, so you can inject a test item directly without touching IMAP:

```bash
docker compose exec mail-rss-service sh -c "cat > /data/state.json" <<'EOF'
{"last_uid": "test-1", "items": [{"guid": "test-1", "title": "Test Item", "link": "http://localhost:8000/rss/test-1", "summary": "test body", "published": "2026-01-01T00:00:00+00:00", "from": "test@example.com"}]}
EOF
```

Note: `./data` on the host is owned by `root` (the container runs as root), so edit it via `docker compose exec` rather than directly from the host shell.

The next real poll (or `/trigger`) overwrites this with actual email data.
