#   -*- coding: utf-8 -*-
#   Copyright 2026 Karellen, Inc.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""Async client for MCP frontend -> daemon communication over Unix socket."""

import asyncio
import json
import logging
import os
import struct
import subprocess
import sys

from karellen_lsp_mcp.daemon import get_socket_path, _HEADER_FMT, _HEADER_SIZE

logger = logging.getLogger(__name__)


def _env_int(name, default):
    """Read an integer from environment variable, falling back to default."""
    value = os.environ.get(name)
    if value is not None:
        try:
            return int(value)
        except ValueError:
            logger.warning("Invalid value for %s: %r, using default %d",
                           name, value, default)
    return default


class DaemonClientError(Exception):
    pass


class DaemonClient:
    """Async client for communicating with the karellen-lsp-mcp daemon."""

    def __init__(self, request_timeout=None):
        self._reader = None
        self._writer = None
        self._msg_id = 0
        self._pending = {}
        self._reader_task = None
        if request_timeout is None:
            request_timeout = _env_int("LSP_MCP_CLIENT_TIMEOUT", 180)
        self._request_timeout = request_timeout

    async def connect(self):
        """Connect to the daemon, auto-starting it if needed."""
        sock_path = get_socket_path()

        if not await self._try_connect(sock_path):
            # Stale socket?
            if os.path.exists(sock_path):
                try:
                    os.unlink(sock_path)
                except OSError:
                    pass

            await self._start_daemon()

            # Wait for the daemon to become connectable
            for _attempt in range(50):
                await asyncio.sleep(0.1)
                if await self._try_connect(sock_path):
                    break
            else:
                raise DaemonClientError("Failed to connect to daemon after starting it")

        self._reader_task = asyncio.create_task(self._read_loop())
        logger.info("Connected to daemon at %s", sock_path)

    async def _try_connect(self, sock_path):
        if not os.path.exists(sock_path):
            return False
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(sock_path)
            return True
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            return False

    async def _start_daemon(self):
        """Start the daemon as a detached subprocess."""
        logger.info("Starting daemon process...")
        subprocess.Popen(
            [sys.executable, "-m", "karellen_lsp_mcp.daemon"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    async def send_request(self, method, params=None):
        """Send a request to the daemon and return the result."""
        if self._writer is None:
            raise DaemonClientError("Not connected to daemon")

        self._msg_id += 1
        msg_id = self._msg_id

        msg = {"id": msg_id, "method": method, "params": params or {}}

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[msg_id] = fut

        body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        self._writer.write(struct.pack(_HEADER_FMT, len(body)) + body)
        await self._writer.drain()

        try:
            return await asyncio.wait_for(fut, timeout=self._request_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise DaemonClientError("Timeout waiting for daemon response to %s" % method)

    async def close(self):
        """Disconnect from the daemon."""
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(DaemonClientError("Disconnected from daemon"))
        self._pending.clear()

    async def _read_loop(self):
        """Read responses from the daemon."""
        try:
            while True:
                header = await self._reader.readexactly(_HEADER_SIZE)
                (length,) = struct.unpack(_HEADER_FMT, header)
                body = await self._reader.readexactly(length)
                msg = json.loads(body)

                msg_id = msg.get("id")
                fut = self._pending.pop(msg_id, None)
                if fut is None:
                    logger.warning("Received response for unknown id: %s", msg_id)
                    continue

                if "error" in msg:
                    fut.set_exception(DaemonClientError(msg["error"].get("message", "Unknown error")))
                else:
                    fut.set_result(msg.get("result"))
        except asyncio.IncompleteReadError:
            logger.warning("Daemon connection closed")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Daemon client reader error", exc_info=True)
        finally:
            # Fail pending
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(DaemonClientError("Daemon connection lost"))
            self._pending.clear()
