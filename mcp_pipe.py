"""
Simple MCP stdio <-> WebSocket pipe.
Adapted from the official Xiaozhi MCP sample (https://github.com/78/mcp-calculator,
MIT licensed) - this is the standard bridge used to connect a local stdio MCP
server to a Xiaozhi MCP access point (the wss://api.xiaozhi.me/mcp/?token=...
URL from your Xiaozhi agent console).

Usage:
    export MCP_ENDPOINT=<wss endpoint from your Xiaozhi console>
    python mcp_pipe.py app/mcp_server/server_stdio.py
"""

import asyncio
import websockets
import subprocess
import logging
import os
import signal
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('MCP_PIPE')

INITIAL_BACKOFF = 1
MAX_BACKOFF = 600


async def connect_with_retry(uri, script_path):
    reconnect_attempt = 0
    backoff = INITIAL_BACKOFF
    while True:
        try:
            if reconnect_attempt > 0:
                logger.info(f"Waiting {backoff}s before reconnection attempt {reconnect_attempt}...")
                await asyncio.sleep(backoff)
            await connect_to_server(uri, script_path)
        except Exception as e:
            reconnect_attempt += 1
            logger.warning(f"Connection closed (attempt {reconnect_attempt}): {e}")
            backoff = min(backoff * 2, MAX_BACKOFF)


async def connect_to_server(uri, script_path):
    try:
        logger.info("Connecting to WebSocket server...")
        async with websockets.connect(uri) as websocket:
            logger.info("Successfully connected to WebSocket server")

            process = subprocess.Popen(
                [sys.executable, script_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding='utf-8',
                text=True,
                env=os.environ.copy(),
            )
            logger.info(f"Started server process: {script_path}")

            await asyncio.gather(
                pipe_websocket_to_process(websocket, process),
                pipe_process_to_websocket(process, websocket),
                pipe_process_stderr_to_terminal(process),
            )
    except websockets.exceptions.ConnectionClosed as e:
        logger.error(f"WebSocket connection closed: {e}")
        raise
    except Exception as e:
        logger.error(f"Connection error: {e}")
        raise
    finally:
        if 'process' in locals():
            logger.info("Terminating server process")
            try:
                process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            logger.info("Server process terminated")


async def pipe_websocket_to_process(websocket, process):
    try:
        while True:
            message = await websocket.recv()
            if isinstance(message, bytes):
                message = message.decode('utf-8')
            process.stdin.write(message + '\n')
            process.stdin.flush()
    except Exception as e:
        logger.error(f"Error in WebSocket to process pipe: {e}")
        raise
    finally:
        if not process.stdin.closed:
            process.stdin.close()


async def pipe_process_to_websocket(process, websocket):
    try:
        while True:
            data = await asyncio.to_thread(process.stdout.readline)
            if not data:
                logger.info("Process has ended output")
                break
            await websocket.send(data)
    except Exception as e:
        logger.error(f"Error in process to WebSocket pipe: {e}")
        raise


async def pipe_process_stderr_to_terminal(process):
    try:
        while True:
            data = await asyncio.to_thread(process.stderr.readline)
            if not data:
                logger.info("Process has ended stderr output")
                break
            sys.stderr.write(data)
            sys.stderr.flush()
    except Exception as e:
        logger.error(f"Error in process stderr pipe: {e}")
        raise


def signal_handler(sig, frame):
    logger.info("Received interrupt signal, shutting down...")
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)

    endpoint_url = (os.environ.get('MCP_ENDPOINT') or '').strip()
    if not endpoint_url or 'YOUR_TOKEN' in endpoint_url:
        # Xiaozhi integration is optional: idle instead of crash-looping.
        logger.info("MCP_ENDPOINT is not set - Xiaozhi integration disabled, idling. "
                    "Set it in docker-compose.yml and recreate this container to enable.")
        import time
        while True:
            time.sleep(3600)

    if len(sys.argv) < 2:
        logger.error("Usage: python mcp_pipe.py <path/to/server_stdio.py>")
        sys.exit(1)

    script_path = sys.argv[1]

    try:
        asyncio.run(connect_with_retry(endpoint_url, script_path))
    except KeyboardInterrupt:
        logger.info("Program interrupted by user")
    except Exception as e:
        logger.error(f"Program execution error: {e}")
