#!/bin/sh
# Block requests from Hermes/Chromium to private and link-local networks.
# This is network-level enforcement: unlike Hermes's URL check it also applies
# after redirects and DNS resolution. SearXNG, the constrained Command Center
# MCP adapter, and the internal approval callback are the only exceptions.
set -eu

searxng_ip="$(getent ahostsv4 searxng | awk 'NR == 1 { print $1 }')"
command_center_mcp_ip="$(getent ahostsv4 command-center-mcp | awk 'NR == 1 { print $1 }')"
command_center_ip="$(getent ahostsv4 command-center | awk 'NR == 1 { print $1 }')"
if [ -z "$searxng_ip" ] || [ -z "$command_center_mcp_ip" ] || [ -z "$command_center_ip" ]; then
    echo "[network-guard] Cannot resolve an internal required service" >&2
    exit 1
fi

install_ipv4_guard() {
    iptables -N HERMES_EGRESS_GUARD 2>/dev/null || true
    iptables -F HERMES_EGRESS_GUARD
    iptables -D OUTPUT -j HERMES_EGRESS_GUARD 2>/dev/null || true

    # SearXNG is internal. Loopback stays inside this bridge-network container
    # (not on the NUC) and is required by Docker DNS and Chromium's local CDP.
    iptables -A HERMES_EGRESS_GUARD -d "$searxng_ip" -j ACCEPT
    iptables -A HERMES_EGRESS_GUARD -d "$command_center_mcp_ip" -j ACCEPT
    # Only the in-image approval callback uses this direct route. Telegram
    # conversations have no raw network capability to reach this API.
    iptables -A HERMES_EGRESS_GUARD -d "$command_center_ip" -j ACCEPT
    iptables -A HERMES_EGRESS_GUARD -d 127.0.0.0/8 -j ACCEPT

    # RFC 1918, link-local, and Tailscale CGNAT ranges.
    iptables -A HERMES_EGRESS_GUARD -d 10.0.0.0/8 -j REJECT
    iptables -A HERMES_EGRESS_GUARD -d 100.64.0.0/10 -j REJECT
    iptables -A HERMES_EGRESS_GUARD -d 169.254.0.0/16 -j REJECT
    iptables -A HERMES_EGRESS_GUARD -d 172.16.0.0/12 -j REJECT
    iptables -A HERMES_EGRESS_GUARD -d 192.168.0.0/16 -j REJECT
    iptables -I OUTPUT 1 -j HERMES_EGRESS_GUARD
}

install_ipv6_guard() {
    command -v ip6tables >/dev/null || return 0
    ip6tables -N HERMES_EGRESS_GUARD 2>/dev/null || true
    ip6tables -F HERMES_EGRESS_GUARD
    ip6tables -D OUTPUT -j HERMES_EGRESS_GUARD 2>/dev/null || true
    ip6tables -A HERMES_EGRESS_GUARD -d ::1/128 -j REJECT
    ip6tables -A HERMES_EGRESS_GUARD -d fc00::/7 -j REJECT
    ip6tables -A HERMES_EGRESS_GUARD -d fe80::/10 -j REJECT
    ip6tables -I OUTPUT 1 -j HERMES_EGRESS_GUARD
}

install_ipv4_guard
install_ipv6_guard
echo "[network-guard] Private-network egress blocked; internal services allowed"

exec /opt/hermes/docker/entrypoint-dispatch.sh "$@"
