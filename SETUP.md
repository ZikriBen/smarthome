# Smart Home Server

This repository documents the configuration and recovery procedure for the home server running on an Intel NUC with Ubuntu.

The goal is that this document plus the Git repository and application backups are sufficient to rebuild the server after a failure.

## Services

- Home Assistant OS — home automation
- AdGuard Home — network-wide DNS filtering
- Homarr — home-server dashboard
- Uptime Kuma — availability monitoring
- Beszel — host/container monitoring
- Speedtest Tracker — internet performance history
- Portainer — Docker management
- Caddy — reverse proxy
- Tailscale — private remote access and selected public endpoints
- Jellyfin — LAN media server
- Telegram Downloader — Telegram → media → Jellyfin ingestion

---

# Architecture

```mermaid
flowchart TD
    Internet((Internet))
    LAN[Home LAN]
    Telegram[Telegram]
    TS[Tailscale Funnel]

    Internet --> TS
    LAN --> Ubuntu
    Telegram --> Downloader

    subgraph Ubuntu["Intel NUC - Ubuntu - 10.0.0.13"]
        Docker[Docker]
        Libvirt[libvirt / KVM]
        Tailscaled[Tailscale]

        subgraph Containers["Docker"]
            Homarr[Homarr :7575]
            Kuma[Uptime Kuma :3001]
            AdGuard[AdGuard :53 / :8080]
            Beszel[Beszel :8090]
            Agent[Beszel Agent]
            Speed[Speedtest :8765]
            Portainer[Portainer :9443]
            Caddy[Caddy :8088]
            Jellyfin[Jellyfin :8096]
            Downloader[Telegram Downloader :8787]
        end

        subgraph VM["Virtual Machines"]
            HAOS[HAOS<br/>192.168.122.119:8123]
        end

        Docker --> Containers
        Libvirt --> HAOS
    end

    TS --> Tailscaled
    Tailscaled --> Caddy
    Tailscaled --> HAOS

    Caddy --> Homarr
    Caddy --> Beszel

    Downloader --> Media[/home/ben/media/incoming]
    Media --> Jellyfin

    LAN --> AdGuard
    LAN --> Jellyfin
```

---

# Network Architecture

## Home LAN

```text
Network:       10.0.0.0/24
Router:        10.0.0.138
Ubuntu NUC:    10.0.0.13
Wi-Fi device:  wlp6s0
```

The router has a **DHCP reservation** for the NUC.

The NUC therefore remains configured for DHCP, while the router guarantees:

```text
NUC MAC → 10.0.0.13
```

This is preferable to configuring a manual static address in Ubuntu because the router knows that `.13` is reserved and will not assign it to another client.

Verify:

```bash
ip route
```

Expected:

```text
default via 10.0.0.138 dev wlp6s0 ... src 10.0.0.13
```

## Important service addresses

```text
Homarr             http://10.0.0.13:7575
Uptime Kuma        http://10.0.0.13:3001
AdGuard            http://10.0.0.13:8080
Beszel             http://10.0.0.13:8090
Speedtest Tracker  http://10.0.0.13:8765
Portainer          https://10.0.0.13:9443
Jellyfin           http://10.0.0.13:8096
Telegram health    http://10.0.0.13:8787/health
```

---

# DNS / AdGuard

AdGuard runs on the NUC:

```text
DNS: 10.0.0.13:53
```

The router advertises `10.0.0.13` as DNS to LAN DHCP clients.

Flow:

```text
Phone / PC / TV
      │
      │ DNS
      ▼
10.0.0.13:53
      │
   AdGuard
      │
      ├── blocked → reject
      │
      └── allowed → upstream DNS
```

## Important: host DNS

The NUC itself should NOT depend exclusively on its own Docker-hosted AdGuard instance.

Otherwise this dependency can occur:

```text
Docker needs DNS
      ↓
AdGuard provides DNS
      ↓
AdGuard itself requires Docker
```

This caused Docker image pulls to fail with errors such as:

```text
Temporary failure in name resolution
```

Configure external DNS specifically for the Ubuntu host:

```bash
sudo nmcli connection modify "Setup" \
  ipv4.method auto \
  ipv4.ignore-auto-dns yes \
  ipv4.dns "1.1.1.1 8.8.8.8"
```

Reconnect:

```bash
sudo nmcli connection down "Setup"
sudo nmcli connection up "Setup"
```

Verify:

```bash
nmcli device show wlp6s0 | grep IP4.DNS
```

---

# libvirt / Home Assistant Network

Home Assistant OS runs as a dedicated VM.

```text
Ubuntu
│
│ virbr0
│
├── 192.168.122.1       Ubuntu/libvirt gateway
│
└── 192.168.122.119     HAOS
        │
        └── :8123       Home Assistant
```

HAOS has a libvirt DHCP reservation for:

```text
192.168.122.119
```

Verify:

```bash
sudo virsh net-dumpxml default
```

Look for the HA MAC/IP reservation.

Check HA:

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  http://192.168.122.119:8123
```

Expected:

```text
200
```

---

# Home Assistant OS

HA runs as HAOS rather than a Docker container.

Benefits:

- Supervisor
- Add-ons
- HAOS updates
- HA backups
- appliance-style isolation

Check:

```bash
sudo virsh list --all
```

VM name:

```text
haos
```

Enable automatic startup:

```bash
sudo virsh autostart haos
```

Verify:

```bash
sudo virsh dominfo haos | grep Autostart
```

Expected:

```text
Autostart: enable
```

---

# Automatic Recovery After Power Failure

The NUC BIOS is configured to automatically power on when AC power returns.

Ubuntu does **not** require a desktop login for services to start.

Expected sequence:

```text
Power restored
      ↓
BIOS powers NUC on
      ↓
Ubuntu boots
      │
      ├── Docker
      │    └── containers restart
      │
      ├── tailscaled
      │
      └── libvirt
           └── HAOS autostarts
```

This has been tested while Ubuntu remained at the login screen.

Verify after reboot:

```bash
docker ps
systemctl is-active tailscaled
sudo virsh list --all
```

---

# Docker

Docker runs directly on Ubuntu.

The admin user belongs to the `docker` group so Docker commands do not require `sudo`.

Configure:

```bash
sudo usermod -aG docker $USER
```

Then log out/in, reboot, or:

```bash
newgrp docker
```

Verify:

```bash
groups
docker ps
```

> Membership in the Docker group effectively provides root-level control over the host.

---

# Homarr

Homarr is the main navigation dashboard.

```text
http://10.0.0.13:7575
```

It provides convenient access to the home services without remembering ports.

---

# Uptime Kuma

Kuma answers:

> Can the service actually be reached?

```text
http://10.0.0.13:3001
```

Current monitoring should include:

- Home Assistant
- Homarr
- AdGuard
- Caddy / Funnel
- Beszel
- Speedtest Tracker
- Portainer
- Telegram Downloader
- Jellyfin

Examples:

```text
Telegram Downloader:
http://10.0.0.13:8787/health

Jellyfin:
http://10.0.0.13:8096/health
```

Kuma and Beszel serve different purposes:

```text
Kuma   → availability
Beszel → resource usage / performance
```

---

# Beszel

Beszel monitors:

- CPU
- RAM
- disk
- network
- containers
- historical utilization

Architecture:

```text
Beszel
  │
  ▼
Beszel Agent
  │
  ▼
Docker socket
  │
  ├── Homarr
  ├── Kuma
  ├── Jellyfin
  ├── Telegram Downloader
  └── ...
```

Dashboard:

```text
http://10.0.0.13:8090
```

---

# Speedtest Tracker

Tracks historical internet performance.

```text
http://10.0.0.13:8765
```

Measured home internet speed is around:

```text
462 Mbps
```

This was useful when diagnosing Telegram downloads: individual Telethon downloads were around 1.5 MB/s even though the internet connection had substantially more capacity.

---

# Portainer

Portainer manages Docker.

```text
https://10.0.0.13:9443
```

Portainer has access to:

```text
/var/run/docker.sock
```

This effectively gives it control of Docker and therefore the host.

Do not expose Portainer publicly without appropriate protection.

---

# Caddy

Caddy acts as a reverse proxy.

Local endpoint:

```text
127.0.0.1:8088
```

Configuration:

```text
caddy/conf/Caddyfile
```

Current example:

```text
Tailscale Funnel
      │
      ▼
Caddy
      │
      ├── /        → Homarr
      └── /beszel  → Beszel
```

Reload:

```bash
docker exec caddy \
  caddy reload --config /etc/caddy/Caddyfile
```

Test:

```bash
curl -I http://127.0.0.1:8088
```

---

# Tailscale

Tailscale runs directly on Ubuntu via systemd rather than Docker.

Install:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Verify:

```bash
systemctl is-active tailscaled
tailscale status
```

Tailscale starts automatically after reboot.

## Private access

Devices connected to the tailnet can access services through Tailscale without exposing them publicly.

## Funnel

Funnel exposes selected services publicly without requiring the client to connect to Tailscale.

Current endpoints:

```text
https://home-server-ubuntu.tailf473.ts.net
    ↓
:443
    ↓
Caddy
    ↓
Homarr / proxied services
```

and:

```text
https://home-server-ubuntu.tailf473.ts.net:8443
    ↓
Home Assistant
```

Configure:

```bash
sudo tailscale funnel --bg \
  --https=443 \
  http://127.0.0.1:8088
```

HA:

```bash
sudo tailscale funnel --bg \
  --https=8443 \
  http://192.168.122.119:8123
```

Check:

```bash
tailscale funnel status
```

Funnel endpoints are public. Do not automatically expose every service.

---

# LAN-only vs Remote Services

Not every service needs remote exposure.

## Prefer LAN-only

```text
Jellyfin
AdGuard administration
Portainer
Beszel administration
```

For example Jellyfin uses:

```text
http://10.0.0.13:8096
```

This provides direct LAN performance without unnecessary relay/proxy traffic.

Movies can be downloaded to a phone while at home for offline viewing later.

## Remote

Use Tailscale private access where practical.

Use Funnel only where unauthenticated-client/public browser access is specifically useful.

---

# Jellyfin

Jellyfin provides media playback to phones, computers and TVs.

```text
http://10.0.0.13:8096
```

Current Telegram media library:

```text
Host:
  /home/ben/media/incoming

Container:
  /media/telegram
```

Compose mount:

```yaml
- /home/ben/media/incoming:/media/telegram
```

## Important: write access

If Jellyfin should be able to delete files, the mount must be read-write.

Correct:

```yaml
- /home/ben/media/incoming:/media/telegram
```

Incorrect:

```yaml
- /home/ben/media/incoming:/media/telegram:ro
```

Verify:

```bash
docker inspect jellyfin \
  --format '{{range .Mounts}}{{println .Source "->" .Destination "RW=" .RW}}{{end}}'
```

Expected:

```text
/home/ben/media/incoming -> /media/telegram RW= true
```

Test:

```bash
docker exec jellyfin sh -c '
  touch /media/telegram/.write-test &&
  rm /media/telegram/.write-test &&
  echo "WRITE OK"
'
```

Health:

```text
http://10.0.0.13:8096/health
```

---

# Telegram Media Downloader

The custom Telegram downloader is located at:

```text
telegram-downloader/
```

The old proof-of-concept remains separately available while development continues.

## Telegram group

```text
Name: ZikriMedia
Chat ID: -5463728915
```

Any group member can submit supported media.

Flow:

```text
Telegram group
      │
      ▼
new media
      │
      ▼
📥 Accepted
      │
      ▼
SQLite queue
      │
      ▼
download worker
      │
      ▼
.part file
      │
      ▼
processing
      │
      ▼
/home/ben/media/incoming
      │
      ▼
✅ Available
      │
      ▼
Jellyfin
```

## Job states

```text
RECEIVED
   ↓
QUEUED
   ↓
DOWNLOADING
   ↓
PROCESSING
   ↓
AVAILABLE
```

Retry path:

```text
DOWNLOADING
   ↓
RETRY_WAIT
   ↓
DOWNLOADING
```

Permanent failures become:

```text
FAILED
```

## Persistent state

```text
telegram-downloader/data/downloader.db
telegram-downloader/data/telegram-downloader.session
```

The Telegram session must never be committed to Git.

## Concurrency

Configured with:

```env
MAX_CONCURRENT_DOWNLOADS=4
```

This means four separate files may download simultaneously.

It does NOT mean four chunks of one file.

Observed behavior:

```text
1 file  ≈ 1.5 MB/s
2 files ≈ 1.5 MB/s each
```

Therefore multiple downloads increase aggregate Telegram throughput.

Future optimization may use multiple MTProto connections/chunks for a single file.

## Resumable downloads

Incomplete files live under:

```text
/home/ben/media/incoming/.part/
```

Example:

```text
movie: 1114 / 1381 MB
       ↓
container restart
       ↓
SQLite job recovered
       ↓
.part detected
       ↓
resume from 1114 MB
```

This behavior has been tested successfully across container restarts.

## Disk protection

Configuration:

```env
MIN_FREE_DISK_GB=50
```

Downloads are rejected if available storage drops below the configured minimum.

## Health

```text
http://10.0.0.13:8787/health
```

Example:

```json
{
  "status": "ok",
  "free_disk_gb": 600.0,
  "min_free_disk_gb": 50
}
```

Operational status:

```text
http://10.0.0.13:8787/status
```

This reports queue/download/available/failed counts.

Kuma monitors `/health`.

## Future downloader phases

Planned functionality includes:

1. Telegram bot UX
2. Movie / TV classification
3. Clean Jellyfin movie/series organization
4. Metadata/provenance tracking
5. Parallel MTProto chunks for faster individual downloads
6. Telegram watched-channel discovery

Watched-channel discovery will:

```text
watched Telegram channel
       ↓
new media/link
       ↓
present option in ZikriMedia
       ↓
user selects Download
       ↓
normal downloader pipeline
```

It will NOT automatically download everything from watched channels.

---

# Secrets

Never commit secrets.

Use:

```text
.env.example   → Git
.env           → local only
```

Sensitive data includes:

- Telegram API hash
- Telegram session
- Beszel token/key
- Speedtest APP_KEY
- Homarr encryption key
- API tokens
- passwords

Before committing:

```bash
git status
```

Optional scan:

```bash
grep -RniE \
'PASSWORD|TOKEN|SECRET|APP_KEY|API_HASH|ssh-ed25519' \
--exclude-dir=.git .
```

---

# Starting Services After Fresh Installation

Clone:

```bash
git clone https://github.com/ZikriBen/smarthome.git
cd smarthome
```

Restore required `.env` files and persistent data.

Then start services:

```bash
cd ~/smarthome/beszel
docker compose up -d

cd ~/smarthome/caddy
docker compose up -d

cd ~/smarthome/speedtest-tracker
docker compose up -d

cd ~/smarthome/homarr
docker compose up -d

cd ~/smarthome/uptime-kuma
docker compose up -d

cd ~/smarthome/adguard
docker compose up -d

cd ~/smarthome/portainer
docker compose up -d

cd ~/smarthome/jellyfin
docker compose up -d

cd ~/smarthome/telegram-downloader
docker compose up -d --build
```

Verify:

```bash
docker ps
```

Then:

```bash
sudo virsh list --all
tailscale status
tailscale funnel status
```

---

# Troubleshooting

## Docker DNS failure

Symptom:

```text
Temporary failure in name resolution
```

Check:

```bash
nmcli device show wlp6s0 | grep IP4.DNS
```

Remember that the NUC should not depend solely on its own AdGuard container.

## Container

```bash
docker ps -a
docker logs <container>
```

## Jellyfin deletion fails

Check whether the media mount is read-only:

```bash
docker inspect jellyfin \
  --format '{{range .Mounts}}{{println .Source "->" .Destination "RW=" .RW}}{{end}}'
```

It must show:

```text
RW= true
```

## Telegram downloader

```bash
docker logs -f telegram-downloader
```

Database:

```bash
sqlite3 ~/smarthome/telegram-downloader/data/downloader.db \
  'SELECT id, status, original_filename FROM download_jobs ORDER BY id;'
```

Partial downloads:

```bash
ls -lh /home/ben/media/incoming/.part/
```

## Caddy

```bash
curl -I http://127.0.0.1:8088
docker logs caddy
```

## Home Assistant

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  http://192.168.122.119:8123

sudo virsh list --all
```

## Tailscale Funnel

```bash
tailscale status
tailscale funnel status
```

Always troubleshoot from the inside outward:

```text
application
    ↓
local port
    ↓
proxy
    ↓
Tailscale/Funnel
    ↓
remote browser
```

---

# Backup Strategy

Git backs up **configuration and code**, not application state.

| Service | Data requiring backup |
|---|---|
| Home Assistant | HA backup |
| Homarr | configuration/database |
| Uptime Kuma | monitoring database |
| AdGuard | configuration |
| Beszel | historical metrics |
| Speedtest Tracker | database/history |
| Portainer | Portainer volume |
| Jellyfin | configuration/metadata |
| Telegram Downloader | SQLite DB + Telegram session |
| Media | `/home/ben/media` if preservation is required |

A complete recovery therefore requires:

```text
Git repository
      +
Secrets
      +
HA backup
      +
Docker persistent data
      +
Telegram session
      +
Media (if desired)
      =
Recoverable home server
```

---

# Disaster Recovery Checklist

For a fresh NUC:

1. Install Ubuntu.
2. Enable BIOS automatic power-on after AC loss.
3. Configure router DHCP reservation for `10.0.0.13`.
4. Configure Ubuntu host DNS.
5. Install Git.
6. Install Docker.
7. Add admin user to `docker` group.
8. Install libvirt/KVM.
9. Restore/create HAOS VM.
10. Restore HA backup.
11. Configure HAOS DHCP reservation (`192.168.122.119`).
12. Enable HAOS autostart.
13. Clone the `smarthome` repository.
14. Restore `.env` files/secrets.
15. Restore Docker persistent data.
16. Restore Telegram session/database.
17. Start Compose services.
18. Install/authenticate Tailscale.
19. Restore Funnel routes.
20. Verify AdGuard DNS.
21. Verify Jellyfin media.
22. Verify Telegram downloader.
23. Verify Kuma monitors.
24. Reboot.
25. **Do not log into Ubuntu.**
26. Verify everything remotely.

The final test is important:

```text
Power/reboot
     ↓
Ubuntu login screen
     ↓
all server services already operational
```
