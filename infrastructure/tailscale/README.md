# Tailscale

Tailscale runs directly on the Ubuntu host using systemd.

## Install

curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

## Verify

systemctl status tailscaled
tailscale status

## Funnel

Public Homarr/Caddy:

sudo tailscale funnel --bg --https=443 http://127.0.0.1:8088

Public Home Assistant:

sudo tailscale funnel --bg --https=8443 http://192.168.122.119:8123

## Current public endpoints

- 443 -> Caddy -> Homarr / proxied services
- 8443 -> Home Assistant

## Verify Funnel

tailscale funnel status

## Important

Do not commit Tailscale authentication keys or Tailscale state files.
