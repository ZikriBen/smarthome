# home-agent

Self-hosted personal assistant built on [Hermes Agent](https://github.com/NousResearch/hermes-agent).

Current scope (MVP): Telegram web assistant backed by OpenAI: search, browse pages, multi-step tasks.

    Telegram (allowlisted users)
        |
        v
    hermes (gateway) ---> OpenAI API
        |
        v
    searxng (internal, no published port)

- Tools: web search, headless browser, task planning. No terminal, file or Docker access (`platform_toolsets` in `hermes/config.yaml`).
- Browser cannot reach LAN, Tailscale or loopback addresses (`security.allow_private_urls: false`).
- No published ports; Telegram uses outbound long polling.
- Docker and other server capabilities will be added later through the Command Center, not via `docker.sock`.

## Setup

    cp .env.example .env && chmod 600 .env
    # fill OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USERS
    # SEARXNG_SECRET: openssl rand -hex 32
    docker compose up -d

## Files

| Path | Purpose |
|---|---|
| `hermes/config.yaml` | Model, web backend, allowed toolsets (tracked) |
| `hermes/SOUL.md` | Assistant persona (tracked) |
| `hermes/*` | Runtime state: sessions, memories, logs (ignored) |
| `searxng/settings.yml` | SearXNG config with JSON output enabled |

## Useful commands

    docker compose logs -f hermes
    docker compose run --rm --no-deps hermes doctor
