# smarthome-secrets

One way to keep service secrets out of `.env` files and out of `docker inspect`:

```
Ben types value ──stdin──▶ smarthome-secret set <svc> <NAME>
                               │ systemd-creds encrypt --with-key=host --name=<svc>.<NAME>
                               ▼
        /etc/credstore.encrypted/smarthome/<svc>.<NAME>      (root 0600, encrypted at rest)
                               │ smarthome-secrets.service (root oneshot, Before=docker.service)
                               ▼
        /run/smarthome-secrets/<svc>/<NAME>                  (tmpfs, 0400, owner from owners.conf; dir 0500)
                               │ compose top-level `secrets:` (file bind mount, read-only)
                               ▼
        container: /run/secrets/<NAME>  ──▶ native *_FILE support   (homarr, beszel-agent)
                                       └─▶ secrets-env-shim → env var → exec original entrypoint
```

Password-based website logins keep their own root brokers (`hapoalim-run`, unchanged).
Hermes keeps calling MCP tools exactly as today.

## Pieces

| path (repo) | installed to | what |
|---|---|---|
| `bin/smarthome_secrets.py` | `/usr/local/lib/smarthome-secrets/` + 4 symlinks in `/usr/local/sbin` | CLI, oneshot, migrate, rollback (python3 stdlib) |
| `bin/secrets-env-shim` | `/usr/local/lib/smarthome-secrets/secrets-env-shim` (mounted `:ro` at `/usr/local/bin/secrets-env-shim`) | POSIX sh env shim |
| `systemd/smarthome-secrets.service` | `/etc/systemd/system/` | oneshot, `RemainAfterExit`, `WantedBy=multi-user.target docker.service` |
| `manifest.yaml` | `/etc/smarthome-secrets/manifest.yaml` | services and secret NAMES (never values) |
| `etc/owners.conf`, `etc/config` | `/etc/smarthome-secrets/` (installed once, never overwritten) | runtime owners; `REPO_ROOT`, `WITH_KEY=host` |
| `host/install-host.sh` | n/a | install, `daemon-reload`; `--enable` / `--start` only when asked |
| `<stack>/compose.secrets*.yaml` | n/a | per-service overrides, inert until `migrate` adds them to `COMPOSE_FILE` |
| `test/` | n/a | offline tests (mock `systemd-creds`) + optional real-image chroot tests |

The policy files are read from `/etc` (root-owned). Nothing a non-root repo edit changes can alter
who owns a decrypted secret.

## Commands

```
sudo smarthome-secret set <svc> <NAME>        # value: hidden prompt (TTY) or stdin; NEVER argv
sudo smarthome-secret list [<svc>]            # names only
sudo smarthome-secret rm <svc> <NAME>         # also deletes the /run copy
sudo smarthome-secrets refresh                # = systemctl restart smarthome-secrets.service
sudo smarthome-secrets status                 # names + runtime mode/owner, no values
sudo smarthome-secrets-migrate <svc> [--dry-run] [--force] [--overwrite]
sudo smarthome-secrets-rollback <svc> [--dry-run] [--purge]
```

* Names: `svc` must match `^[a-z][a-z0-9-]{0,31}$` and `NAME` must match `^[A-Z][A-Z0-9_]{0,63}$`. No dots, slashes or `..`.
* `set` strips exactly one trailing newline. It rejects empty values, NUL bytes and values over 64 KiB.
  An extra argument (a pasted value) is refused without being echoed.
* `apply` is idempotent and writes atomically: tmp file, `fchown`/`fchmod 0400`, then `rename`.
  It removes orphans. If a decrypt fails it logs only `FAILED <svc>.<NAME>`, keeps the previous
  runtime file, carries on with the rest and exits 1 at the end. It refuses to write anywhere
  that is not tmpfs.
* **After changing a secret:** run `refresh`, then restart that container. A bind-mounted file is
  pinned to its inode, and env-shim values are read once at start.

## Migration (`smarthome-secrets-migrate <svc>`)

1. Reads the unit from the manifest. `needs-review` units need `--force`. `plan-only` units are refused.
2. Refuses if a key it would remove is still a **required** interpolation (`${X:?…}`) in a
   compose file. Removing it would break `docker compose` for the whole project.
3. `--dry-run` prints the plan (names only) and stops.
4. Parses each value exactly as compose would. It refuses `$` interpolation, odd escapes and
   multi-line values. The value goes to `systemd-creds` on stdin only. If a credential already
   exists with a different value, it stops unless you pass `--overwrite`.
5. Runs `apply` and checks that the runtime files exist. **Only then** does it touch `.env`.
6. Backups: `.env.pre-secrets` (first migration only) and `.env.pre-secrets.<svc>`. Both are 0600
   and never overwritten.
7. Removes the keys and appends the override to `COMPOSE_FILE` in that `.env`. A marker line is
   added if `COMPOSE_FILE` was not there before. Mode and owner are kept.
8. Gate: `docker compose config --quiet`, then an in-memory render (`config --format json`)
   that checks each migrated env var is gone and a secret is mounted. On failure `.env` is
   restored. **No restart.** It prints the `docker compose up -d …` command for you to run.

`smarthome-secrets-rollback <svc>` puts the original lines back verbatim from
`.env.pre-secrets.<svc>`, falling back to the decrypted credential. It removes the override from
`COMPOSE_FILE` and drops the marker. Credentials are kept unless you pass `--purge`. It is
idempotent and never restarts anything.

## Per-service status (images checked 2026-10-22)

| unit | names | image / entrypoint / runtime uid | method | status |
|---|---|---|---|---|
| **searxng** (pilot) | SEARXNG_SECRET | `searxng/searxng@sha256:cc026d…` (Void, busybox sh). ENTRYPOINT `/usr/local/searxng/entrypoint.sh`, no CMD, root | shim (no native `_FILE`). Base line changed to pass-through | **ready** (tested in the real image rootfs) |
| beszel-agent | BESZEL_TOKEN → `TOKEN_FILE` | `henrygd/beszel-agent:latest` (2026-10-02 build), scratch (**no shell**), ENTRYPOINT `/agent`, root | native `TOKEN_FILE` / `KEY_FILE` (from the binary's strings) | ready. Confirm the NUC's image is that recent |
| homarr | SECRET_ENCRYPTION_KEY | `ghcr.io/homarr-labs/homarr:latest`, ENTRYPOINT `/app/entrypoint.sh`, CMD `sh run.sh`, root then `su-exec $PUID` (0) | native: the entrypoint loads every `*_FILE` | **ready** (tested in the real image rootfs). `pull_policy: always`, see risks |
| mail-rss-service | IMAP_USER, IMAP_PASS | own Dockerfile (python:3.11-slim, dash), no ENTRYPOINT, CMD `uv run uvicorn …`, root | shim, keys removed from `env_file` | ready |
| telegram-downloader | TELEGRAM_API_ID, TELEGRAM_API_HASH, JELLYFIN_API_KEY | own Dockerfile (python:3.13-slim), CMD `python -m app.main`, root | shim | needs-review: a restart interrupts downloads. Check the NUC `.env` for `COMPOSE_FILE` |
| telegram-browser | TELEGRAM_API_ID, TELEGRAM_API_HASH | own Dockerfile (python:3.13-slim), CMD `uvicorn app.web:app …`, root | shim. Its existing `JELLYFIN_API_KEY_FILE` / `TG_STREAM_HMAC_KEY_FILE` are left alone (not allowlisted) | needs-review: the tg-stream scripts and the untracked `compose.jellyfin-key.yaml` may run compose with an explicit `-f`, which bypasses `COMPOSE_FILE` |
| command-center (+ -mcp, + hermes's callback token) | COMMAND_CENTER_TOKEN, COMMAND_CENTER_CALLBACK_TOKEN, UPTIME_KUMA_API_KEY, JELLYFIN_API_KEY | own Dockerfile (python:3.13-alpine, busybox), CMD `python /app/app.py`, root (mounts docker.sock) | shim | needs-review: needs the base edits below |
| hermes | OPENAI_API_KEY, TELEGRAM_BOT_TOKEN (+ COMMAND_CENTER_CALLBACK_TOKEN) | `smarthome-hermes-agent`, ENTRYPOINT `hermes-network-guard` (root, sh) → `entrypoint-dispatch.sh` (needs PID 1 for s6) → drops to HERMES_UID 1000 | shim in front of the guard (exec keeps PID 1) | needs-review, migrate **last** |
| hermes-internal | API_SERVER_KEY (`hermes/.env`, which Hermes reads itself from `/opt/data`) | n/a | n/a | plan-only |
| google OAuth | `command-center/data` client-secret.json + tokens | file-based, written back by the app on refresh | n/a | plan-only (below) |
| Telethon `.session` | telegram-browser / -downloader `./data` | written by Telethon | n/a | plan-only (last) |

Not secrets, so they stay in `.env`: TELEGRAM_ALLOWED_USERS and OTP_FORWARD_BOT_ID / CHAT_ID
(numeric ids, also visible to chat members), BESZEL_KEY (the hub's *public* key),
TELEGRAM_SESSION (a path), hosts and URLs.

### home-agent base edits (for command-center / hermes, NOT part of this package)

Only the pilot line is changed now. Before `migrate command-center --force`, change these lines
in `home-agent/compose.yaml` from `X: ${X:?set X in .env}` to the pass-through form `X:`:
COMMAND_CENTER_TOKEN (command-center **and** command-center-mcp), COMMAND_CENTER_CALLBACK_TOKEN
(command-center **and** hermes), UPTIME_KUMA_API_KEY, JELLYFIN_API_KEY. Before `migrate hermes`,
do the same for OPENAI_API_KEY and TELEGRAM_BOT_TOKEN. The test
`TestComposeRender.test_home_agent_later_overrides` renders exactly that end state.
**Do not rely on `!reset` across several override files.** docker compose 2.39 honours it only
in the *first* override of a `COMPOSE_FILE` chain. Pass-through plus removing the key from `.env`
is the real mechanism, `!reset` is defence in depth, and migrate's render gate catches any leftover.

### Plan: file-based secrets (do not migrate now)

* **Google OAuth** (`command-center/data`): the app rewrites the token file on refresh, so it
  cannot be a read-only credential. Plan: keep `client-secret.json` as credential
  `command-center.GOOGLE_CLIENT_SECRET_JSON`, mounted read-only and pointed to by an env path.
  Keep the refresh token in `data/` (0600). Optionally store it encrypted at rest with a key
  that is itself a credential.
* **Telethon sessions**: a session is a live SQLite DB that Telethon writes constantly. It cannot
  live in tmpfs without losing state on reboot, and it must never be opened by a second
  process (Ben's rule). Plan: leave it in `./data` (0600, root). If ever wanted, stop the
  container, encrypt it to a credential while it is stopped, and restore it to `./data` before
  start through an `ExecStartPre` on a wrapper unit. Never copy it, export a StringSession, or
  open a second client.

## Security notes (read these)

* **docker.sock = root.** command-center, homarr and beszel-agent mount `/var/run/docker.sock`
  (beszel and command-center read-only, but `:ro` on a socket does **not** limit the API).
  Anyone with Docker API access, or anyone who compromises one of those containers, can
  `docker exec`/`cp` into any container and read its `/run/secrets/*`, or start a container
  that bind-mounts `/run/smarthome-secrets`. This mechanism protects against *casual*
  exposure (`.env` in backups, `docker inspect`, `docker compose config`, logs). It does not
  protect against Docker API holders.
* **systemd-creds `--with-key=host`** protects the credstore at rest against someone who
  copies `/etc/credstore.encrypted` *without* `/var/lib/systemd/credential.secret` (for example a
  backup of `/etc` only). It does **not** protect against root on the running NUC, or against
  theft of the whole unencrypted disk (the host key sits on the same disk). A TPM
  (`WITH_KEY=auto`) would add disk-theft protection, but a firmware or TPM reset then loses
  every credential, so keep re-entry possible.
* An env-shim value lives in the target process's environment. Root inside the container can
  read it through `/proc/<pid>/environ`, and s6 copies it into `/run/s6/container_environment`
  (hermes). That is the same as today, minus `docker inspect`.
* `.env.pre-secrets*` backups still hold plaintext (0600, owned like `.env`). Delete them once
  the service has run fine for a while (`shred -u`). The repo `.gitignore` already covers
  `.env.*`, so git ignores them.
* Values never go in argv, environment or logs. Every error names `<svc>.<NAME>` only. The tests
  grep every output for the dummy values.
