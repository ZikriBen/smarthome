# Homarr

Homarr is the main dashboard for the home server.

## Location

Project:

```text
~/smarthome/homarr
```

Local URL:

```text
http://<home-server-ip>:7575
```

## Running

Start:

```bash
cd ~/smarthome/homarr
docker compose up -d
```

Stop:

```bash
docker compose down
```

Logs:

```bash
docker logs homarr-homarr-1 --tail 100
```

## Persistent data

Host:

```text
~/smarthome/homarr/appdata
```

Container:

```text
/appdata
```

Database:

```text
~/smarthome/homarr/appdata/db/db.sqlite
```

Do not commit `appdata/` to Git.

## Encryption key

Homarr uses:

```text
SECRET_ENCRYPTION_KEY
```

Stored in:

```text
~/smarthome/homarr/.env
```

IMPORTANT:

The database and encryption key belong together.

Never generate a new encryption key for an existing Homarr database.
Doing so can make encrypted integrations/config unreadable.

Never commit `.env`.

## Docker integration

Homarr can access Docker through:

```text
/var/run/docker.sock
```

Compose mount:

```yaml
- /var/run/docker.sock:/var/run/docker.sock
```

Verify:

```bash
docker exec homarr-homarr-1 \
  ls -l /var/run/docker.sock
```

## Recovery history

Original Homarr installation:

```text
/home/ben/home-services/homarr
```

Current installation:

```text
/home/ben/smarthome/homarr
```

The original installation was recovered using both:

```text
/home/ben/home-services/homarr/appdata
/home/ben/home-services/homarr/.env
```

Both the database and matching encryption key were required.

## Restore procedure

Stop Homarr:

```bash
cd ~/smarthome/homarr
docker compose down
```

Restore:

```text
appdata/
.env
```

Then:

```bash
docker compose up -d
```

Check:

```bash
docker logs homarr-homarr-1 --tail 100
```

## Styling

Files:

```text
CUSTOM_CSS.md
board.css
```

Custom classes:

```text
glass-widget
glass-light
glass-strong
theme-inner
hide-mobile
```

## Important rule

Before changing Homarr storage, volumes, or encryption configuration:

1. Back up `.env`
2. Back up `appdata`
3. Verify the backup
4. Only then recreate the container
