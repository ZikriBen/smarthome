"""MCP tool registration grouped by capability domain."""

from . import (docker, files, google_workspace, groceries, hapoalim, host, jellyfin, maps,
               price_watches, searchgram, super_hist, uptime_kuma)


def register_all(mcp, client):
    for module in (host, docker, uptime_kuma, searchgram, jellyfin, google_workspace,
                   files, maps, price_watches, groceries, super_hist, hapoalim):
        module.register(mcp, client)
