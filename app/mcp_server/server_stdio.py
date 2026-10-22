"""
Stdio MCP server exposing printer status as tools.

Run it through mcp_pipe.py, which bridges stdio <-> the WebSocket
"MCP接入点" URL from your Xiaozhi agent console (wss://api.xiaozhi.me/mcp/?token=...):

    export MCP_ENDPOINT="wss://api.xiaozhi.me/mcp/?token=YOUR_TOKEN"
    python mcp_pipe.py app/mcp_server/server_stdio.py
"""

from mcp.server.fastmcp import FastMCP

from app.mcp_server.tools import register

mcp = FastMCP("bambu-printer-status")
register(mcp)

if __name__ == "__main__":
    mcp.run(transport="stdio")
