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

"""Shared daemon process that owns all LSP server instances and the project registry."""

import asyncio
import json
import logging
import os
import signal
import struct
import sys
import urllib.parse

from filelock import FileLock, Timeout
from platformdirs import user_data_dir as _user_data_dir
from platformdirs import user_log_dir as _user_log_dir
from platformdirs import user_runtime_dir as _user_runtime_dir

from karellen_lsp_mcp.project_registry import ProjectRegistry, ProjectRegistryError
from karellen_lsp_mcp.lsp_client import LspClientError
from karellen_lsp_mcp.lsp_normalizer import ServerState

logger = logging.getLogger(__name__)

_HEADER_FMT = "!I"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)
_MAX_MESSAGE_SIZE = 10 * 1024 * 1024  # 10 MB


def _get_runtime_dir():
    return _user_runtime_dir("karellen-lsp-mcp")


def _get_data_dir():
    return _user_data_dir("karellen-lsp-mcp")


def _get_log_dir():
    return _user_log_dir("karellen-lsp-mcp")


def get_socket_path():
    return os.path.join(_get_runtime_dir(), "daemon.sock")


def _get_lock_path():
    return os.path.join(_get_runtime_dir(), "daemon.lock")


async def _read_message(reader):
    """Read a length-prefixed JSON message from a stream."""
    header = await reader.readexactly(_HEADER_SIZE)
    (length,) = struct.unpack(_HEADER_FMT, header)
    if length > _MAX_MESSAGE_SIZE:
        raise ValueError("Message too large: %d bytes" % length)
    body = await reader.readexactly(length)
    return json.loads(body)


def _write_message(writer, msg):
    """Write a length-prefixed JSON message to a stream."""
    body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    writer.write(struct.pack(_HEADER_FMT, len(body)) + body)


class _FrontendSession:
    """Tracks one connected MCP frontend and its registered projects."""

    def __init__(self, session_id, reader, writer, daemon):
        self.session_id = session_id
        self.reader = reader
        self.writer = writer
        self.daemon = daemon
        self.registered_projects = set()  # project_ids registered by this frontend
        self._write_lock = asyncio.Lock()
        self._pending_tasks = set()

    async def handle(self):
        """Process requests from this frontend until disconnect."""
        try:
            while True:
                try:
                    msg = await _read_message(self.reader)
                except (asyncio.IncompleteReadError, ConnectionError):
                    break

                task = asyncio.create_task(self._handle_request(msg))
                self._pending_tasks.add(task)
                task.add_done_callback(self._pending_tasks.discard)
        finally:
            # Wait for in-flight requests to complete
            if self._pending_tasks:
                await asyncio.gather(*self._pending_tasks, return_exceptions=True)
            await self._cleanup()

    async def _handle_request(self, msg):
        """Dispatch a single request and write the response."""
        response = await self._dispatch(msg)
        async with self._write_lock:
            try:
                _write_message(self.writer, response)
                await self.writer.drain()
            except (ConnectionError, RuntimeError):
                pass

    async def _dispatch(self, msg):
        msg_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params", {})

        try:
            result = await self._handle_method(method, params)
            return {"id": msg_id, "result": result}
        except (ProjectRegistryError, LspClientError) as e:
            return {"id": msg_id, "error": {"message": str(e)}}
        except Exception as e:
            logger.error("Error handling %s", method, exc_info=True)
            return {"id": msg_id, "error": {"message": "Internal error: %s" % e}}

    async def _handle_method(self, method, params):
        registry = self.daemon.registry

        if method == "detect_project":
            from karellen_lsp_mcp.detector import detect_project
            result = detect_project(params["project_path"])
            return _serialize_detection_result(result)

        elif method == "register_project":
            language = params.get("language")
            lsp_command = params.get("lsp_command")
            build_info = params.get("build_info")
            init_options = params.get("init_options")
            detection_details = None

            if language is None:
                from karellen_lsp_mcp.detector import detect_project
                result = detect_project(params["project_path"])
                if not result.languages:
                    raise ProjectRegistryError(
                        "Could not detect language for project: %s"
                        % params["project_path"])
                detected = result.languages[0]
                language = detected.language
                if lsp_command is None and detected.lsp_command:
                    lsp_command = detected.lsp_command
                if build_info is None and detected.build_info:
                    build_info = detected.build_info
                if init_options is None and detected.init_options:
                    init_options = detected.init_options
                detection_details = detected.details

            project_id = await registry.register(
                project_path=params["project_path"],
                language=language,
                lsp_command=lsp_command,
                build_info=build_info,
                init_options=init_options,
                detection_details=detection_details,
                force=params.get("force", False),
            )
            self.registered_projects.add(project_id)
            return {"project_id": project_id}

        elif method == "deregister_project":
            project_id = params["project_id"]
            await registry.deregister(project_id)
            self.registered_projects.discard(project_id)
            return {"ok": True}

        elif method == "list_projects":
            return registry.list_projects()

        elif method == "indexing_status":
            project_id = params["project_id"]
            entry = registry.get_client(project_id)
            return entry.client.get_indexing_status()

        elif method.startswith("lsp_"):
            return await self._handle_lsp_request(method, params)

        else:
            raise ProjectRegistryError("Unknown method: %s" % method)

    # Single-file queries work immediately from clangd's AST built on
    # didOpen — no need to wait for background indexing.
    _SINGLE_FILE_METHODS = frozenset({
        "lsp_read_definition", "lsp_hover", "lsp_document_symbols",
    })

    # MCP tool name -> LSP method for feature support checks
    _LSP_METHOD_MAP = {
        "lsp_call_hierarchy_outgoing": "callHierarchy/outgoingCalls",
        "lsp_call_hierarchy_incoming": "callHierarchy/incomingCalls",
        "lsp_type_hierarchy_supertypes": "typeHierarchy/supertypes",
        "lsp_type_hierarchy_subtypes": "typeHierarchy/subtypes",
    }

    async def _handle_lsp_request(self, method, params):
        registry = self.daemon.registry
        project_id = params["project_id"]
        entry = registry.get_client(project_id)
        client = entry.client

        # Check version-based feature support before dispatching
        lsp_method = self._LSP_METHOD_MAP.get(method)
        if lsp_method and not client.supports_method(lsp_method):
            raise LspClientError(
                "This LSP server does not support %s "
                "(requires a newer version)" % method)

        if method in self._SINGLE_FILE_METHODS:
            # Single-file queries work from the AST built on didOpen —
            # only need the server to finish the initialize handshake,
            # not background indexing.
            if client.state == ServerState.STARTING:
                initialized = await client.wait_initialized(
                    timeout=self.daemon.ready_timeout)
                if not initialized:
                    raise LspClientError(
                        "LSP server not initialized after %ds (state: %s)"
                        % (self.daemon.ready_timeout, client.state_name))
        else:
            # Cross-file queries need the background index.
            # Use estimated remaining time for a dynamic timeout.
            if client.state != ServerState.READY:
                est = client.estimated_remaining_seconds()
                if est is not None:
                    timeout = max(self.daemon.ready_timeout,
                                  est + 30)
                else:
                    timeout = self.daemon.ready_timeout
                ready = await client.wait_ready(timeout=timeout)
                if not ready:
                    status = client.get_indexing_status()
                    pct_parts = []
                    for task in status.get("active_tasks", []):
                        if task.get("percentage") is not None:
                            pct_parts.append("%d%%" % task["percentage"])
                    pct_str = (", ".join(pct_parts)) if pct_parts else "unknown"
                    raise LspClientError(
                        "LSP server not ready after %ds "
                        "(state: %s, progress: %s)"
                        % (int(timeout), client.state_name, pct_str))

        indexing = client.state == ServerState.INDEXING

        if method in ("lsp_read_definition", "lsp_find_references", "lsp_hover",
                      "lsp_call_hierarchy_incoming", "lsp_call_hierarchy_outgoing",
                      "lsp_type_hierarchy_supertypes", "lsp_type_hierarchy_subtypes"):
            file_uri = registry.validate_file_path(project_id, params["file_path"])
            await client.ensure_file_open(file_uri)
            line = params["line"]
            character = params["character"]

            if method == "lsp_read_definition":
                result = await client.definition(file_uri, line, character)
                return _parse_locations(result, indexing=False)

            elif method == "lsp_find_references":
                include_decl = params.get("include_declaration", True)
                result = await client.references(
                    file_uri, line, character, include_decl)
                return _parse_locations(result, indexing=indexing)

            elif method == "lsp_hover":
                result = await client.hover(file_uri, line, character)
                return _parse_hover(result)

            elif method == "lsp_call_hierarchy_incoming":
                items = await client.prepare_call_hierarchy(
                    file_uri, line, character)
                if not items:
                    return {"direction": "incoming", "items": [],
                            "indexing": indexing}
                calls = await client.incoming_calls(items[0])
                return _parse_call_hierarchy(calls, "incoming",
                                             indexing=indexing)

            elif method == "lsp_call_hierarchy_outgoing":
                items = await client.prepare_call_hierarchy(
                    file_uri, line, character)
                if not items:
                    return {"direction": "outgoing", "items": [],
                            "indexing": indexing}
                calls = await client.outgoing_calls(items[0])
                return _parse_call_hierarchy(calls, "outgoing",
                                             indexing=indexing)

            elif method == "lsp_type_hierarchy_supertypes":
                items = await client.prepare_type_hierarchy(
                    file_uri, line, character)
                if not items:
                    return {"direction": "supertypes", "items": [],
                            "indexing": indexing}
                result = await client.supertypes(items[0])
                return _parse_type_hierarchy(result, "supertypes",
                                             indexing=indexing)

            elif method == "lsp_type_hierarchy_subtypes":
                items = await client.prepare_type_hierarchy(
                    file_uri, line, character)
                if not items:
                    return {"direction": "subtypes", "items": [],
                            "indexing": indexing}
                result = await client.subtypes(items[0])
                return _parse_type_hierarchy(result, "subtypes",
                                             indexing=indexing)

        elif method == "lsp_document_symbols":
            file_uri = registry.validate_file_path(project_id, params["file_path"])
            await client.ensure_file_open(file_uri)
            result = await client.document_symbols(file_uri)
            return _parse_document_symbols(result)

        elif method == "lsp_diagnostics":
            file_uri = registry.validate_file_path(project_id, params["file_path"])
            await client.ensure_file_open(file_uri)
            diags = client.get_diagnostics(file_uri)
            return _parse_diagnostics(diags, indexing=indexing)

        else:
            raise ProjectRegistryError("Unknown LSP method: %s" % method)

    async def _cleanup(self):
        """Deregister all projects this frontend registered."""
        registry = self.daemon.registry
        for project_id in list(self.registered_projects):
            try:
                await registry.deregister(project_id)
            except Exception:
                logger.warning("Error deregistering %s on disconnect", project_id, exc_info=True)
        self.registered_projects.clear()
        self.daemon.remove_frontend(self.session_id)


# ---------------------------------------------------------------------------
# LSP result parsers — convert raw LSP JSON to structured dicts
# ---------------------------------------------------------------------------

def _uri_to_path(uri):
    if uri and uri.startswith("file://"):
        return urllib.parse.unquote(uri)[7:]
    return uri or ""


_SYMBOL_KIND_NAMES = {
    1: "File", 2: "Module", 3: "Namespace", 4: "Package", 5: "Class",
    6: "Method", 7: "Property", 8: "Field", 9: "Constructor", 10: "Enum",
    11: "Interface", 12: "Function", 13: "Variable", 14: "Constant",
    15: "String", 16: "Number", 17: "Boolean", 18: "Array", 19: "Object",
    20: "Key", 21: "Null", 22: "EnumMember", 23: "Struct", 24: "Event",
    25: "Operator", 26: "TypeParameter",
}


def _parse_locations(result, indexing=False):
    locations = []
    if result is not None:
        if isinstance(result, dict):
            result = [result]
        for loc in result:
            if "targetUri" in loc:
                uri = loc["targetUri"]
                rng = loc.get("targetSelectionRange") or loc.get("targetRange", {})
            else:
                uri = loc.get("uri", "")
                rng = loc.get("range", {})

            start = rng.get("start", {})
            locations.append({
                "file": _uri_to_path(uri),
                "line": start.get("line", 0) + 1,
                "character": start.get("character", 0) + 1,
            })

    result_dict = {"locations": locations}
    if indexing:
        result_dict["indexing"] = True
    return result_dict


def _parse_hover(result):
    if result is None:
        return {"content": None}
    contents = result.get("contents")
    if contents is None:
        return {"content": None}

    if isinstance(contents, str):
        return {"content": contents}
    if isinstance(contents, dict):
        value = contents.get("value", "")
        lang = contents.get("language") or contents.get("kind")
        r = {"content": value}
        if lang and lang != "plaintext":
            r["language"] = lang
        return r
    if isinstance(contents, list):
        parts = []
        for item in contents:
            if isinstance(item, str):
                parts.append({"content": item})
            elif isinstance(item, dict):
                value = item.get("value", "")
                lang = item.get("language")
                p = {"content": value}
                if lang:
                    p["language"] = lang
                parts.append(p)
        if len(parts) == 1:
            return parts[0]
        return {"parts": parts}
    return {"content": str(contents)}


def _parse_symbol(sym):
    kind_num = sym.get("kind", 0)
    kind = _SYMBOL_KIND_NAMES.get(kind_num, "Unknown(%d)" % kind_num)
    rng = (sym.get("selectionRange") or sym.get("range")
           or sym.get("location", {}).get("range", {}))
    start = rng.get("start", {})
    s = {
        "name": sym.get("name", "?"),
        "kind": kind,
        "line": start.get("line", 0) + 1,
    }
    detail = sym.get("detail")
    if detail:
        s["detail"] = detail
    children = sym.get("children")
    if children:
        s["children"] = [_parse_symbol(c) for c in children]
    return s


def _parse_document_symbols(result):
    if not result:
        return {"symbols": []}
    return {"symbols": [_parse_symbol(sym) for sym in result]}


def _parse_call_hierarchy(calls, direction, indexing=False):
    items = []
    if calls:
        for call in calls:
            item = call.get("from") if direction == "incoming" else call.get("to")
            if item is None:
                continue
            kind_num = item.get("kind", 0)
            rng = item.get("selectionRange") or item.get("range", {})
            start = rng.get("start", {})
            from_ranges = call.get("fromRanges", [])
            items.append({
                "name": item.get("name", "?"),
                "kind": _SYMBOL_KIND_NAMES.get(kind_num, ""),
                "file": _uri_to_path(item.get("uri", "")),
                "line": start.get("line", 0) + 1,
                "call_sites": len(from_ranges) if from_ranges else 1,
            })

    result_dict = {"direction": direction, "items": items}
    if indexing:
        result_dict["indexing"] = True
    return result_dict


def _parse_type_hierarchy(items_raw, direction, indexing=False):
    items = []
    if items_raw:
        for item in items_raw:
            kind_num = item.get("kind", 0)
            rng = item.get("selectionRange") or item.get("range", {})
            start = rng.get("start", {})
            items.append({
                "name": item.get("name", "?"),
                "kind": _SYMBOL_KIND_NAMES.get(kind_num, ""),
                "file": _uri_to_path(item.get("uri", "")),
                "line": start.get("line", 0) + 1,
            })

    result_dict = {"direction": direction, "items": items}
    if indexing:
        result_dict["indexing"] = True
    return result_dict


_DIAG_SEVERITY = {1: "Error", 2: "Warning", 3: "Information", 4: "Hint"}


def _parse_diagnostics(diags, indexing=False):
    items = []
    if diags:
        for d in diags:
            rng = d.get("range", {})
            start = rng.get("start", {})
            item = {
                "line": start.get("line", 0) + 1,
                "character": start.get("character", 0) + 1,
                "severity": _DIAG_SEVERITY.get(d.get("severity", 0), "Unknown"),
                "message": d.get("message", ""),
            }
            source = d.get("source")
            if source:
                item["source"] = source
            items.append(item)

    result_dict = {"diagnostics": items}
    if indexing:
        result_dict["indexing"] = True
    return result_dict


def _serialize_detection_result(result):
    """Convert a DetectionResult to a JSON-serializable dict.

    Checks LSP server availability for each detected language via the
    adapter registry, so the caller knows which servers need to be installed.
    """
    from karellen_lsp_mcp.lsp_adapter import get_adapter

    languages = []
    for lang in result.languages:
        entry = {
            "language": lang.language,
            "build_system": lang.build_system,
            "confidence": lang.confidence,
        }
        if lang.lsp_command:
            entry["lsp_command"] = lang.lsp_command
        if lang.details:
            entry["details"] = lang.details

        # Check if the LSP server for this language is available
        adapter = get_adapter(lang.language)
        if adapter is not None:
            available, hint = adapter.check_server()
            entry["server_available"] = available
            if hint:
                entry["install_hint"] = hint
        else:
            entry["server_available"] = True

        languages.append(entry)
    return {
        "project_path": result.project_path,
        "languages": languages,
    }


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class Daemon:
    """The shared daemon process."""

    def __init__(self, idle_timeout=300, ready_timeout=120, request_timeout=60,
                 runtime_dir=None):
        self.registry = ProjectRegistry(request_timeout=request_timeout,
                                        ready_timeout=ready_timeout)
        self._idle_timeout = idle_timeout
        self.ready_timeout = ready_timeout
        self._runtime_dir = runtime_dir or _get_runtime_dir()
        self._frontends = {}  # session_id -> _FrontendSession
        self._next_session_id = 0
        self._server = None
        self._shutdown_event = asyncio.Event()
        self._had_client = False
        self._idle_task = None

    def remove_frontend(self, session_id):
        self._frontends.pop(session_id, None)
        logger.info("Frontend %d disconnected (%d remaining)", session_id, len(self._frontends))
        if self._had_client and not self._frontends:
            logger.info("Last frontend disconnected, shutting down")
            self._shutdown_event.set()

    async def _handle_connection(self, reader, writer):
        self._next_session_id += 1
        session_id = self._next_session_id
        session = _FrontendSession(session_id, reader, writer, self)
        self._frontends[session_id] = session
        self._had_client = True
        logger.info("Frontend %d connected (%d total)", session_id, len(self._frontends))

        try:
            await session.handle()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _idle_monitor(self):
        """Safety net: shut down if no client ever connects within the timeout."""
        try:
            await asyncio.sleep(self._idle_timeout)
        except asyncio.CancelledError:
            return
        if not self._had_client:
            logger.info("No client connected within %ds, shutting down",
                        self._idle_timeout)
            self._shutdown_event.set()

    async def run(self):
        """Start the daemon and serve until shutdown."""
        runtime_dir = self._runtime_dir
        os.makedirs(runtime_dir, exist_ok=True)
        sock_path = os.path.join(runtime_dir, "daemon.sock")
        lock_path = os.path.join(runtime_dir, "daemon.lock")

        # Acquire exclusive lock — if another daemon holds it, exit
        self._lock = FileLock(lock_path)
        try:
            self._lock.acquire(timeout=0)
        except Timeout:
            logger.info("Another daemon is already running, exiting")
            return

        # Clean up stale socket
        if os.path.exists(sock_path):
            os.unlink(sock_path)

        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=sock_path
        )
        os.chmod(sock_path, 0o600)

        logger.info("Daemon listening on %s", sock_path)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._shutdown_event.set)

        self._idle_task = asyncio.create_task(self._idle_monitor())

        try:
            await self._shutdown_event.wait()
        finally:
            logger.info("Daemon shutting down...")
            self._server.close()
            await self._server.wait_closed()

            if self._idle_task:
                self._idle_task.cancel()
                try:
                    await self._idle_task
                except asyncio.CancelledError:
                    pass

            # Close all frontend connections
            for session in list(self._frontends.values()):
                try:
                    session.writer.close()
                except Exception:
                    pass

            await self.registry.shutdown_all()

            # Remove socket and release lock
            if os.path.exists(sock_path):
                os.unlink(sock_path)

            self._lock.release()
            logger.info("Daemon stopped")


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


def _get_log_path():
    """Return the path for the daemon log file."""
    return os.path.join(_get_log_dir(), "daemon.log")


def main():
    runtime_dir = _get_runtime_dir()
    os.makedirs(runtime_dir, exist_ok=True)
    log_dir = _get_log_dir()
    os.makedirs(log_dir, exist_ok=True)
    log_path = _get_log_path()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        filename=log_path,
    )

    def _unhandled_exception(exc_type, exc_value, exc_tb):
        logger.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = _unhandled_exception

    logger.info("Daemon starting (pid=%d)", os.getpid())
    daemon = Daemon(
        idle_timeout=_env_int("LSP_MCP_IDLE_TIMEOUT", 300),
        ready_timeout=_env_int("LSP_MCP_READY_TIMEOUT", 120),
        request_timeout=_env_int("LSP_MCP_REQUEST_TIMEOUT", 60),
    )
    asyncio.run(daemon.run())


if __name__ == "__main__":
    main()
