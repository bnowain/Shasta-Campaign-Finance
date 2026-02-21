"""Start netfile-tracker with zombie cleanup and SO_REUSEADDR.

Combines:
- Shasta-PRA-Backup pattern: kill zombies on port before start
- Atlas pattern: SO_REUSEADDR custom uvicorn server + SIGTERM handler

Usage: python run.py
"""

import socket
import sys
import signal

import uvicorn
from uvicorn.config import Config
from uvicorn.server import Server

from app.config import APP_HOST, APP_PORT
from app.utils.process_manager import kill_port


class ReuseAddrServer(Server):
    """Uvicorn server that sets SO_REUSEADDR on Windows."""

    async def startup(self, sockets=None):
        if sockets is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.config.host, self.config.port))
            sock.set_inheritable(True)
            sockets = [sock]
        await super().startup(sockets=sockets)


def main():
    # Step 1: Kill any zombie processes on our port
    print(f"Checking for zombie processes on port {APP_PORT}...")
    kill_port(APP_PORT)

    # Step 2: SIGTERM handler for clean shutdown
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    # Step 3: Start uvicorn with SO_REUSEADDR
    print(f"\nStarting netfile-tracker on http://{APP_HOST}:{APP_PORT}")
    config = Config(
        "app.main:app",
        host=APP_HOST,
        port=APP_PORT,
        timeout_graceful_shutdown=5,
    )
    server = ReuseAddrServer(config=config)
    server.run()


if __name__ == "__main__":
    main()
