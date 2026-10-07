#!/bin/sh
# Block requests from Hermes/Chromium to private and link-local networks.
# This is network-level enforcement: unlike Hermes's URL check it also applies
# after redirects and DNS resolution. SearXNG is the single private exception.
set -eu

searxng_ip="$(getent ahostsv4 searxng | awk 'NR == 1 { print $1 }')"
if [ -z "$searxng_ip" ]; then
    echo "[network-guard] Cannot resolve the internal SearXNG service" >&2
    exit 1
fi

install_ipv4_guard() {
    iptables -N HERMES_EGRESS_GUARD 2>/dev/null || true
    iptables -F HERMES_EGRESS_GUARD
    iptables -D OUTPUT -j HERMES_EGRESS_GUARD 2>/dev/null || true

    # SearXNG is internal; Docker's embedded DNS is needed to resolve public sites.
    iptables -A HERMES_EGRESS_GUARD -d "$searxng_ip" -j ACCEPT
    iptables -A HERMES_EGRESS_GUARD -d 127.0.0.11 -p udp --dport 53 -j ACCEPT
    iptables -A HERMES_EGRESS_GUARD -d 127.0.0.11 -p tcp --dport 53 -j ACCEPT

    # RFC 1918, loopback, link-local, and Tailscale CGNAT ranges.
    iptables -A HERMES_EGRESS_GUARD -d 10.0.0.0/8 -j REJECT
    iptables -A HERMES_EGRESS_GUARD -d 100.64.0.0/10 -j REJECT
    iptables -A HERMES_EGRESS_GUARD -d 127.0.0.0/8 -j REJECT
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
echo "[network-guard] Private-network egress blocked; SearXNG allowed at $searxng_ip"

exec /opt/hermes/docker/entrypoint-dispatch.sh "$@"
