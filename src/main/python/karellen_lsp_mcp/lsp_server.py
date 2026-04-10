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

"""LSP proxy frontend: polyglot LSP server that routes to backend LSP
servers via the karellen-lsp-mcp daemon.

Speaks standard LSP JSON-RPC 2.0 over stdio to the client (e.g., Claude
Code) and delegates requests to the appropriate backend through the
shared daemon. Reuses normalizers (URI normalization, transient error
retry), adapters (server config, init options), and readiness tracking.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import urllib.parse

from karellen_lsp_mcp.daemon_client import DaemonClient, DaemonClientError

logger = logging.getLogger(__name__)

# LSP methods that require a textDocument URI in params
_TEXT_DOCUMENT_METHODS = frozenset({
    "textDocument/definition",
    "textDocument/declaration",
    "textDocument/typeDefinition",
    "textDocument/implementation",
    "textDocument/references",
    "textDocument/hover",
    "textDocument/documentSymbol",
    "textDocument/prepareCallHierarchy",
    "textDocument/prepareTypeHierarchy",
})

# LSP methods that carry a hierarchy item with a URI
_HIERARCHY_ITEM_METHODS = frozenset({
    "callHierarchy/incomingCalls",
    "callHierarchy/outgoingCalls",
    "typeHierarchy/supertypes",
    "typeHierarchy/subtypes",
})


class LspProxyServer:
    """One instance per Claude Code session. All state is per-session."""

    def __init__(self):
        self._daemon = DaemonClient()
        self._root_path = None  # set during initialize
        self._registered_ids = set()  # for shutdown cleanup

        # I/O
        self._reader = None
        self._writer = None
        self._write_lock = asyncio.Lock()
        self._shutdown_requested = False

    async def run(self):
        """Start the LSP proxy server on stdin/stdout."""
        loop = asyncio.get_event_loop()

        # Setup async stdin reader
        self._reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(self._reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)

        # Setup async stdout writer
        w_transport, w_protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout.buffer)
        self._writer = asyncio.StreamWriter(
            w_transport, w_protocol, self._reader, loop)

        logger.info("LSP proxy server started")

        try:
            while True:
                msg = await self._read_message()
                if msg is None:
                    break
                asyncio.create_task(self._dispatch(msg))
        except asyncio.CancelledError:
            pass
        finally:
            await self._close()

    async def _close(self):
        """Clean up daemon connection."""
        try:
            await self._daemon.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # JSON-RPC framing (Content-Length headers, same as lsp_client.py)
    # ------------------------------------------------------------------

    async def _read_message(self):
        """Read one Content-Length framed JSON-RPC message from stdin."""
        headers = {}
        while True:
            line = await self._reader.readline()
            if not line:
                return None
            line = line.decode("ascii").strip()
            if not line:
                break
            if ":" in line:
                key, _, value = line.partition(":")
                headers[key.strip().lower()] = value.strip()

        content_length = int(headers.get("content-length", 0))
        if content_length == 0:
            return None

        body = await self._reader.readexactly(content_length)
        return json.loads(body)

    async def _write_message(self, msg):
        """Write a Content-Length framed JSON-RPC message to stdout."""
        body = json.dumps(msg, separators=(",", ":"))
        body_bytes = body.encode("utf-8")
        header = "Content-Length: %d\r\n\r\n" % len(body_bytes)
        async with self._write_lock:
            self._writer.write(header.encode("ascii") + body_bytes)
            await self._writer.drain()

    async def _send_response(self, msg_id, result):
        """Send a JSON-RPC response."""
        await self._write_message(
            {"jsonrpc": "2.0", "id": msg_id, "result": result})

    async def _send_error(self, msg_id, code, message):
        """Send a JSON-RPC error response."""
        await self._write_message({
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message},
        })

    # ------------------------------------------------------------------
    # Message dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, msg):
        """Route an incoming JSON-RPC message."""
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params", {})

        if method is not None and msg_id is not None:
            # Request (has both method and id)
            try:
                result = await self._handle_request(method, params)
                await self._send_response(msg_id, result)
            except Exception as e:
                logger.error("Error handling %s: %s", method, e,
                             exc_info=True)
                await self._send_error(msg_id, -32603, str(e))
        elif method is not None:
            # Notification (has method, no id)
            try:
                await self._handle_notification(method, params)
            except Exception:
                logger.error("Error handling notification %s",
                             method, exc_info=True)

    async def _handle_request(self, method, params):
        """Handle a JSON-RPC request and return the result."""
        if method == "initialize":
            return await self._handle_initialize(params)
        elif method == "shutdown":
            return await self._handle_shutdown()

        # Proxy query methods — daemon handles routing
        if method in _TEXT_DOCUMENT_METHODS:
            uri = params.get("textDocument", {}).get("uri", "")
            return await self._daemon.send_request("lsp_proxy", {
                "method": method,
                "params": params,
                "file_uri": uri,
            })

        elif method in _HIERARCHY_ITEM_METHODS:
            item_uri = params.get("item", {}).get("uri", "")
            return await self._daemon.send_request("lsp_proxy", {
                "method": method,
                "params": params,
                "file_uri": item_uri,
            })

        elif method == "workspace/symbol":
            return await self._daemon.send_request(
                "lsp_proxy_workspace_symbols", {
                    "root_path": self._root_path,
                    "query": params.get("query", ""),
                })

        else:
            raise Exception("Unsupported method: %s" % method)

    async def _handle_notification(self, method, params):
        """Handle a JSON-RPC notification (no response)."""
        if method == "initialized":
            pass  # no-op
        elif method == "textDocument/didOpen":
            await self._handle_did_open(params)
        elif method == "textDocument/didClose":
            pass  # routing is path-based, no per-file tracking
        elif method == "textDocument/didSave":
            pass  # backends don't need this via our proxy
        elif method == "exit":
            await self._close()
            os._exit(0 if self._shutdown_requested else 1)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _handle_initialize(self, params):
        """Handle initialize: detect languages, register projects."""
        await self._daemon.connect()

        root_uri = params.get("rootUri") or params.get("rootPath", "")
        if root_uri.startswith("file://"):
            root_path = urllib.parse.unquote(root_uri[7:])
        else:
            root_path = root_uri or os.getcwd()

        self._root_path = root_path
        logger.info("Initializing LSP proxy for %s", root_path)

        # Detect languages via daemon (reuses adapters + detector)
        try:
            detection = await self._daemon.send_request(
                "detect_project", {"project_path": root_path})
        except DaemonClientError:
            logger.warning("Project detection failed for %s",
                           root_path, exc_info=True)
            detection = {"languages": []}

        # Register a project for each detected language
        for lang_info in detection.get("languages", []):
            language = lang_info.get("language")
            if not language:
                continue
            if not lang_info.get("server_available", True):
                logger.info("Skipping %s: server not available",
                            language)
                continue
            try:
                result = await self._daemon.send_request(
                    "register_project", {
                        "project_path": root_path,
                        "language": language,
                        "lsp_command": lang_info.get("lsp_command"),
                        "build_info": lang_info.get(
                            "details", {}).get("build_info"),
                    })
                project_id = result["project_id"]
                registration_id = result["registration_id"]
                self._registered_ids.add(registration_id)
                logger.info("Registered %s → %s",
                            language, project_id)
            except Exception as e:
                logger.warning("Failed to register %s: %s",
                               language, e)

        return {
            "capabilities": {
                "textDocumentSync": {
                    "openClose": True,
                    "change": 0,  # None — we forward didOpen only
                },
                "definitionProvider": True,
                "declarationProvider": True,
                "typeDefinitionProvider": True,
                "implementationProvider": True,
                "referencesProvider": True,
                "hoverProvider": True,
                "documentSymbolProvider": True,
                "workspaceSymbolProvider": True,
                "callHierarchyProvider": True,
                "typeHierarchyProvider": True,
            },
            "serverInfo": {
                "name": "karellen-lsp",
            },
        }

    async def _handle_shutdown(self):
        """Handle shutdown: deregister all registrations."""
        self._shutdown_requested = True
        for reg_id in list(self._registered_ids):
            try:
                await self._daemon.send_request(
                    "deregister_project",
                    {"registration_id": reg_id})
            except Exception:
                logger.warning("Error deregistering %s",
                               reg_id, exc_info=True)
        self._registered_ids.clear()
        return None

    # ------------------------------------------------------------------
    # Document sync
    # ------------------------------------------------------------------

    async def _handle_did_open(self, params):
        """Forward didOpen to the daemon for routing."""
        td = params.get("textDocument", {})
        try:
            await self._daemon.send_request("lsp_proxy_did_open", {
                "uri": td.get("uri", ""),
                "language_id": td.get("languageId", ""),
                "version": td.get("version", 0),
                "text": td.get("text", ""),
            })
        except DaemonClientError:
            logger.debug("Failed to forward didOpen for %s",
                         td.get("uri", ""), exc_info=True)




def _watch_parent():
    """Background thread: exit when parent process dies."""
    import threading
    import time as _time

    ppid = os.getppid()

    def _monitor():
        while True:
            _time.sleep(2)
            if os.getppid() != ppid:
                os._exit(0)

    t = threading.Thread(target=_monitor, daemon=True)
    t.start()


def main():
    log_level = os.environ.get("LSP_MCP_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,  # stdout is the LSP channel
    )
    _watch_parent()

    loop = asyncio.new_event_loop()
    server = LspProxyServer()

    def _signal_handler(sig, frame):
        loop.call_soon_threadsafe(loop.stop)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    try:
        loop.run_until_complete(server.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(server._close())
        loop.close()
