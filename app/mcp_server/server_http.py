"""
Optional: the same tools over streamable HTTP, for MCP clients that can reach
this host directly (not needed for the Xiaozhi cloud tunnel).

    python -m app.mcp_server.server_http      # serves http://0.0.0.0:8766/mcp
"""

import os

from mcp.server.fastmcp import FastMCP

from app.mcp_server.tools import register

mcp = FastMCP("bambu-printer-status",
              host=os.getenv("MCP_HOST", "0.0.0.0"), port=int(os.getenv("MCP_PORT", "8766")))
register(mcp)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
