"""MCP transport launcher for Command Center tools."""

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from command_center.mcp_tools import register_all
from command_center.mcp_tools.client import CommandCenterClient


mcp = FastMCP(
    "Home Command Center",
    host="0.0.0.0",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=["command-center-mcp:8000"],
    ),
)
register_all(mcp, CommandCenterClient())


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
