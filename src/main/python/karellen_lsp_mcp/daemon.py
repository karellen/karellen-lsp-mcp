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
import time
import urllib.parse

from filelock import FileLock, Timeout
from platformdirs import user_data_dir as _user_data_dir
from platformdirs import user_log_dir as _user_log_dir
from platformdirs import user_runtime_dir as _user_runtime_dir

from karellen_lsp_mcp.project_registry import ProjectRegistry, ProjectRegistryError
from karellen_lsp_mcp.lsp_client import LspClientError, request_timeout_override
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
        cancelled = False
        try:
            while True:
                try:
                    msg = await _read_message(self.reader)
                except (asyncio.IncompleteReadError, ConnectionError):
                    break

                task = asyncio.create_task(self._handle_request(msg))
                self._pending_tasks.add(task)
                task.add_done_callback(self._pending_tasks.discard)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            # Cancel in-flight requests
            for t in self._pending_tasks:
                t.cancel()
            if self._pending_tasks:
                await asyncio.gather(*self._pending_tasks, return_exceptions=True)
            # Skip cleanup if cancelled during daemon shutdown —
            # the daemon's shutdown sequence handles registry cleanup.
            if not cancelled:
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

        if method == "scan_languages":
            from karellen_lsp_mcp.detector import scan_languages
            return scan_languages(params["project_path"])

        elif method == "detect_project":
            from karellen_lsp_mcp.detector import detect_project
            result = detect_project(params["project_path"])
            return _serialize_detection_result(result)

        elif method == "register_project":
            language = params.get("language")
            lsp_command = params.get("lsp_command")
            build_info = params.get("build_info")
            init_options = params.get("init_options")
            detection_details = None

            # Always run detection to discover project configuration
            # (JDK paths, source roots, build info). When language is
            # explicitly provided, use it as a filter; when omitted,
            # take the first detected language.
            from karellen_lsp_mcp.detector import detect_project
            result = detect_project(params["project_path"])
            if result.languages:
                # Find matching language or take first
                detected = None
                if language is not None:
                    for dl in result.languages:
                        if dl.language == language:
                            detected = dl
                            break
                if detected is None:
                    detected = result.languages[0]
                if language is None:
                    language = detected.language
                if lsp_command is None and detected.lsp_command:
                    lsp_command = detected.lsp_command
                if build_info is None and detected.build_info:
                    build_info = detected.build_info
                if init_options is None and detected.init_options:
                    init_options = detected.init_options
                detection_details = detected.details

            if language is None:
                raise ProjectRegistryError(
                    "Could not detect language for project: %s"
                    % params["project_path"])

            force = params.get("force", False)
            regenerate = params.get("regenerate", False)
            if regenerate:
                force = True
                from karellen_lsp_mcp.lsp_adapter import get_adapter
                adapter = get_adapter(language)
                if adapter is not None:
                    adapter.clean_managed_data(
                        params["project_path"])

            project_id = await registry.register(
                project_path=params["project_path"],
                language=language,
                lsp_command=lsp_command,
                build_info=build_info,
                init_options=init_options,
                detection_details=detection_details,
                force=force,
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

        elif method == "regenerate_index":
            project_id = params["project_id"]
            entry = registry.get_client(project_id)
            from karellen_lsp_mcp.lsp_adapter import get_adapter
            adapter = get_adapter(entry.language)
            if adapter is not None:
                adapter.clean_managed_data(entry.path)
            # Force re-register: stops existing server, re-runs
            # detection and adapter configuration from scratch
            new_id = await registry.register(
                project_path=entry.path,
                language=entry.language,
                lsp_command=entry.lsp_command,
                build_info=entry.build_info,
                force=True,
            )
            # Update session tracking
            self.registered_projects.discard(project_id)
            self.registered_projects.add(new_id)
            return {"project_id": new_id}

        elif method == "indexing_status":
            project_id = params["project_id"]
            entry = registry.get_client(project_id)
            return entry.client.get_indexing_status()

        elif method == "lsp_proxy":
            return await self._handle_lsp_proxy(params)

        elif method == "lsp_proxy_workspace_symbols":
            return await self._handle_lsp_proxy_workspace_symbols(
                params)

        elif method == "lsp_proxy_did_open":
            return await self._handle_lsp_proxy_did_open(params)

        elif method.startswith("lsp_"):
            return await self._handle_lsp_request(method, params)

        else:
            raise ProjectRegistryError("Unknown method: %s" % method)

    # Single-file queries work immediately from clangd's AST built on
    # didOpen — no need to wait for background indexing.
    _SINGLE_FILE_METHODS = frozenset({
        "lsp_read_definition", "lsp_read_declaration",
        "lsp_read_type_definition",
        "lsp_hover", "lsp_document_symbols",
    })

    @staticmethod
    async def _await_readiness(client, is_single_file,
                               ready_timeout):
        """Wait for LSP server readiness. Single-file queries wait
        only for initialization; cross-file queries wait for full
        indexing with dynamic timeout extension."""
        if is_single_file:
            if client.state == ServerState.STARTING:
                initialized = await client.wait_initialized(
                    timeout=ready_timeout)
                if not initialized:
                    raise LspClientError(
                        "LSP server not initialized after %ds "
                        "(state: %s)"
                        % (ready_timeout, client.state_name))
        else:
            if client.state != ServerState.READY:
                est = client.estimated_remaining_seconds()
                if est is not None:
                    timeout = max(ready_timeout, est + 30)
                else:
                    timeout = ready_timeout
                ready = await client.wait_ready(timeout=timeout)
                if not ready:
                    status = client.get_indexing_status()
                    pct_parts = []
                    for task in status.get("active_tasks", []):
                        if task.get("percentage") is not None:
                            pct_parts.append(
                                "%d%%" % task["percentage"])
                    pct_str = (", ".join(pct_parts)
                               ) if pct_parts else "unknown"
                    raise LspClientError(
                        "LSP server not ready after %ds "
                        "(state: %s, progress: %s)"
                        % (int(timeout), client.state_name,
                           pct_str))

    # MCP tool name -> LSP method for feature support checks
    _LSP_METHOD_MAP = {
        "lsp_call_hierarchy_outgoing": "callHierarchy/outgoingCalls",
        "lsp_call_hierarchy_incoming": "callHierarchy/incomingCalls",
        "lsp_call_tree_outgoing": "callHierarchy/outgoingCalls",
        "lsp_call_tree_incoming": "callHierarchy/incomingCalls",
        "lsp_type_hierarchy_supertypes": "typeHierarchy/supertypes",
        "lsp_type_hierarchy_subtypes": "typeHierarchy/subtypes",
        "lsp_type_tree_supertypes": "typeHierarchy/supertypes",
        "lsp_type_tree_subtypes": "typeHierarchy/subtypes",
    }

    async def _handle_lsp_request(self, method, params):
        registry = self.daemon.registry
        project_id = params["project_id"]
        entry = registry.get_client(project_id)
        client = entry.client
        normalizer = client.normalizer

        # Check version-based feature support before dispatching
        lsp_method = self._LSP_METHOD_MAP.get(method)
        if lsp_method and not client.supports_method(lsp_method):
            raise LspClientError(
                "This LSP server does not support %s "
                "(requires a newer version)" % method)

        ready_timeout = params.get("timeout", self.daemon.ready_timeout)

        token = None
        if "timeout" in params:
            token = request_timeout_override.set(ready_timeout)

        try:
            t0 = time.monotonic()
            result = await self._dispatch_lsp(
                method, params, client, normalizer, registry,
                project_id, ready_timeout)
            if isinstance(result, dict):
                result["elapsed_ms"] = round(
                    (time.monotonic() - t0) * 1000)
            return result
        finally:
            if token is not None:
                request_timeout_override.reset(token)

    async def _dispatch_lsp(self, method, params, client, normalizer,
                            registry, project_id, ready_timeout):
        await self._await_readiness(
            client, method in self._SINGLE_FILE_METHODS,
            ready_timeout)
        indexing = client.state == ServerState.INDEXING

        if method in ("lsp_read_definition", "lsp_read_declaration",
                      "lsp_find_implementations", "lsp_read_type_definition",
                      "lsp_find_references", "lsp_hover",
                      "lsp_call_hierarchy_incoming", "lsp_call_hierarchy_outgoing",
                      "lsp_type_hierarchy_supertypes", "lsp_type_hierarchy_subtypes",
                      "lsp_call_tree_incoming", "lsp_call_tree_outgoing",
                      "lsp_type_tree_supertypes", "lsp_type_tree_subtypes"):
            file_uri = registry.validate_file_path(project_id, params["file_path"])
            await client.ensure_file_open(file_uri)
            line = params["line"] - 1
            character = params["character"] - 1

            if method == "lsp_read_definition":
                result = await client.definition(file_uri, line, character)
                return _parse_locations(
                    result, indexing=False, normalizer=normalizer)

            elif method == "lsp_read_declaration":
                result = await client.declaration(file_uri, line, character)
                return _parse_locations(
                    result, indexing=False, normalizer=normalizer)

            elif method == "lsp_find_implementations":
                result = await _query_with_fallback(
                    client, file_uri, line, character,
                    client.implementation)
                return _parse_locations(
                    result, indexing=indexing, normalizer=normalizer)

            elif method == "lsp_read_type_definition":
                result = await client.type_definition(
                    file_uri, line, character)
                return _parse_locations(
                    result, indexing=False, normalizer=normalizer)

            elif method == "lsp_find_references":
                include_decl = params.get("include_declaration", True)

                async def _refs_query(uri, ln, ch):
                    return await client.references(
                        uri, ln, ch, include_decl)

                result = await _query_with_fallback(
                    client, file_uri, line, character, _refs_query)
                return _parse_locations(
                    result, indexing=indexing, normalizer=normalizer)

            elif method == "lsp_hover":
                result = await client.hover(file_uri, line, character)
                return _parse_hover(result)

            elif method == "lsp_call_hierarchy_incoming":
                calls, _ = await _prepare_with_fallback(
                    client, file_uri, line, character,
                    client.prepare_call_hierarchy,
                    client.incoming_calls)
                if calls is None:
                    return {"direction": "incoming", "items": [],
                            "indexing": indexing}
                return _parse_call_hierarchy(calls, "incoming",
                                             indexing=indexing,
                                             normalizer=normalizer)

            elif method == "lsp_call_hierarchy_outgoing":
                calls, _ = await _prepare_with_fallback(
                    client, file_uri, line, character,
                    client.prepare_call_hierarchy,
                    client.outgoing_calls)
                if calls is None:
                    return {"direction": "outgoing", "items": [],
                            "indexing": indexing}
                return _parse_call_hierarchy(calls, "outgoing",
                                             indexing=indexing,
                                             normalizer=normalizer)

            elif method == "lsp_type_hierarchy_supertypes":
                result, _ = await _prepare_with_fallback(
                    client, file_uri, line, character,
                    client.prepare_type_hierarchy,
                    client.supertypes)
                if result is None:
                    return {"direction": "supertypes", "items": [],
                            "indexing": indexing}
                return _parse_type_hierarchy(result, "supertypes",
                                             indexing=indexing,
                                             normalizer=normalizer)

            elif method == "lsp_type_hierarchy_subtypes":
                result, _ = await _prepare_with_fallback(
                    client, file_uri, line, character,
                    client.prepare_type_hierarchy,
                    client.subtypes)
                if result is None:
                    return {"direction": "subtypes", "items": [],
                            "indexing": indexing}
                return _parse_type_hierarchy(result, "subtypes",
                                             indexing=indexing,
                                             normalizer=normalizer)

            elif method in (
                    "lsp_call_tree_incoming",
                    "lsp_call_tree_outgoing",
                    "lsp_type_tree_supertypes",
                    "lsp_type_tree_subtypes"):
                return await _handle_tree_query(
                    client, method, params, file_uri, line,
                    character, indexing, normalizer)

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

        elif method == "lsp_workspace_symbols":
            query = params.get("query", "")
            result = await client.workspace_symbol(query)
            return _parse_workspace_symbols(
                result, indexing=indexing, normalizer=normalizer)

        else:
            raise ProjectRegistryError("Unknown LSP method: %s" % method)

    # LSP methods that only need initialization (not full indexing)
    # for the proxy path — uses native LSP method names.
    _PROXY_SINGLE_FILE_METHODS = frozenset({
        "textDocument/definition", "textDocument/declaration",
        "textDocument/typeDefinition",
        "textDocument/hover", "textDocument/documentSymbol",
    })

    async def _handle_lsp_proxy(self, params):
        """Forward a raw LSP request via typed LspClient methods.

        Reuses normalizer retry, readiness waiting, position fallback,
        and URI normalization — same infrastructure as the MCP path.
        Returns raw LSP JSON with normalized URIs.
        """
        registry = self.daemon.registry
        lsp_method = params["method"]
        lsp_params = params["params"]
        file_uri = params.get("file_uri")

        # Resolve project: by explicit project_id or by file path
        project_id = params.get("project_id")
        if project_id:
            entry = registry.get_client(project_id)
        elif file_uri:
            file_path = urllib.parse.unquote(
                file_uri.removeprefix("file://"))
            entry = registry.find_project_for_file(file_path)
        else:
            raise LspClientError(
                "lsp_proxy requires project_id or file_uri")
        client = entry.client
        normalizer = client.normalizer

        # Feature support check (reuse normalizer)
        if not client.supports_method(lsp_method):
            raise LspClientError(
                "This LSP server does not support %s" % lsp_method)

        ready_timeout = params.get("timeout", self.daemon.ready_timeout)

        token = None
        if "timeout" in params:
            token = request_timeout_override.set(ready_timeout)

        try:
            await self._await_readiness(
                client,
                lsp_method in self._PROXY_SINGLE_FILE_METHODS,
                ready_timeout)

            if file_uri:
                await client.ensure_file_open(file_uri)

            # Denormalize incoming params (reverse previously
            # normalized URIs so the backend recognizes them).
            normalizer.denormalize_params(lsp_params)

            # Dispatch to typed LspClient methods (retry via normalizer)
            result = await self._proxy_dispatch(
                client, lsp_method, lsp_params, file_uri)

            # Normalize URIs in response. The normalizer records
            # reverse mappings internally for future denormalization.
            normalizer.normalize_response(result)
            return result
        finally:
            if token is not None:
                request_timeout_override.reset(token)

    async def _proxy_dispatch(self, client, method, params,
                              file_uri):
        """Route to typed LspClient methods with fallback."""
        if method in ("textDocument/definition",
                      "textDocument/declaration",
                      "textDocument/typeDefinition"):
            pos = params["position"]
            fn = {
                "textDocument/definition": client.definition,
                "textDocument/declaration": client.declaration,
                "textDocument/typeDefinition": client.type_definition,
            }[method]
            return await fn(
                file_uri, pos["line"], pos["character"])

        elif method == "textDocument/implementation":
            pos = params["position"]
            return await _query_with_fallback(
                client, file_uri,
                pos["line"], pos["character"],
                client.implementation)

        elif method == "textDocument/references":
            pos = params["position"]
            include_decl = params.get(
                "context", {}).get("includeDeclaration", True)

            async def _refs(uri, ln, ch):
                return await client.references(
                    uri, ln, ch, include_decl)

            return await _query_with_fallback(
                client, file_uri,
                pos["line"], pos["character"], _refs)

        elif method == "textDocument/hover":
            pos = params["position"]
            return await client.hover(
                file_uri, pos["line"], pos["character"])

        elif method == "textDocument/documentSymbol":
            return await client.document_symbols(file_uri)

        elif method == "workspace/symbol":
            return await client.workspace_symbol(
                params.get("query", ""))

        elif method == "textDocument/prepareCallHierarchy":
            pos = params["position"]
            items = await client.prepare_call_hierarchy(
                file_uri, pos["line"], pos["character"])
            if not items and client.needs_position_fallback:
                positions = await _resolve_positions(
                    client, file_uri,
                    pos["line"], pos["character"])
                for uri, ln, ch in positions[1:]:
                    try:
                        alt = await client.prepare_call_hierarchy(
                            uri, ln, ch)
                    except Exception:
                        continue
                    if alt:
                        items = alt
                        break
            return items

        elif method == "callHierarchy/incomingCalls":
            return await client.incoming_calls(params["item"])

        elif method == "callHierarchy/outgoingCalls":
            return await client.outgoing_calls(params["item"])

        elif method == "textDocument/prepareTypeHierarchy":
            pos = params["position"]
            return await client.prepare_type_hierarchy(
                file_uri, pos["line"], pos["character"])

        elif method == "typeHierarchy/supertypes":
            return await client.supertypes(params["item"])

        elif method == "typeHierarchy/subtypes":
            return await client.subtypes(params["item"])

        else:
            raise LspClientError(
                "Unknown proxy method: %s" % method)

    async def _handle_lsp_proxy_workspace_symbols(self, params):
        """Query workspace/symbol across all projects under root_path."""
        registry = self.daemon.registry
        root_path = params["root_path"]
        query = params.get("query", "")
        entries = [e for e in
                   registry.find_projects_under_path(root_path)
                   if e.client is not None]

        async def _query_one(entry):
            try:
                result = await entry.client.workspace_symbol(query)
                if isinstance(result, list):
                    entry.client.normalizer.normalize_response(
                        result)
                    return result
            except Exception:
                logger.debug("workspace/symbol failed for %s",
                             entry.project_id, exc_info=True)
            return []

        results = await asyncio.gather(
            *[_query_one(e) for e in entries])
        all_results = []
        for r in results:
            all_results.extend(r)
        return all_results

    async def _handle_lsp_proxy_did_open(self, params):
        """Forward a textDocument/didOpen from the LSP proxy client."""
        registry = self.daemon.registry
        file_uri = params["uri"]
        file_path = urllib.parse.unquote(
            file_uri.removeprefix("file://"))

        try:
            entry = registry.find_project_for_file(file_path)
        except ProjectRegistryError:
            logger.debug("No project for didOpen: %s", file_path)
            return {}

        client = entry.client
        if client is None:
            return {}

        if client.state == ServerState.STARTING:
            await client.wait_initialized(timeout=60)

        await client.proxy_did_open(
            params["uri"], params["language_id"],
            params["version"], params["text"])
        return {}

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


def _uri_to_path(uri, normalizer=None):
    if normalizer:
        uri = normalizer.normalize_uri(uri)
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


def _parse_locations(result, indexing=False, normalizer=None):
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
                "file": _uri_to_path(uri, normalizer),
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
    kind = _SYMBOL_KIND_NAMES.get(kind_num, "")
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


def _parse_call_hierarchy(calls, direction, indexing=False,
                          normalizer=None):
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
                "file": _uri_to_path(item.get("uri", ""), normalizer),
                "line": start.get("line", 0) + 1,
                "call_sites": len(from_ranges) if from_ranges else 1,
            })

    result_dict = {"direction": direction, "items": items}
    if indexing:
        result_dict["indexing"] = True
    return result_dict


def _parse_type_hierarchy(items_raw, direction, indexing=False,
                          normalizer=None):
    items = []
    if items_raw:
        for item in items_raw:
            kind_num = item.get("kind", 0)
            rng = item.get("selectionRange") or item.get("range", {})
            start = rng.get("start", {})
            items.append({
                "name": item.get("name", "?"),
                "kind": _SYMBOL_KIND_NAMES.get(kind_num, ""),
                "file": _uri_to_path(item.get("uri", ""), normalizer),
                "line": start.get("line", 0) + 1,
            })

    result_dict = {"direction": direction, "items": items}
    if indexing:
        result_dict["indexing"] = True
    return result_dict


# ---------------------------------------------------------------------------
# Recursive tree walkers for call/type hierarchy
# ---------------------------------------------------------------------------

async def _resolve_positions(client, file_uri, line, character):
    """Resolve a position to all candidate (uri, line, char) tuples.

    Returns the original position first, then declaration and definition
    positions as fallbacks. Deduplicates and opens files as needed.
    Some LSP servers resolve cross-TU queries only from specific
    positions (e.g., clangd resolves incoming callers from the header
    declaration but not the source definition).
    """
    seen = set()
    positions = []

    def _add(uri, ln, ch):
        key = (uri, ln, ch)
        if key not in seen:
            seen.add(key)
            positions.append(key)

    _add(file_uri, line, character)

    for lookup_fn in (client.declaration, client.definition):
        try:
            result = await lookup_fn(file_uri, line, character)
        except Exception:
            continue
        if not result:
            continue
        if isinstance(result, dict):
            result = [result]
        for loc in result:
            uri = loc.get("targetUri") or loc.get("uri", "")
            rng = (loc.get("targetSelectionRange")
                   or loc.get("range", {}))
            start = rng.get("start", {})
            _add(uri, start.get("line", 0), start.get("character", 0))

    # Ensure all files are open
    for uri, _, _ in positions[1:]:
        try:
            await client.ensure_file_open(uri)
        except Exception:
            pass

    return positions


async def _query_with_fallback(client, file_uri, line, character,
                               query_fn):
    """Run a positional query with def/decl fallback.

    Tries the original position first. If the result is empty and the
    server needs position fallback, tries declaration and definition
    positions. Returns the first non-empty result.
    """
    result = await query_fn(file_uri, line, character)
    if result or not client.needs_position_fallback:
        return result

    positions = await _resolve_positions(
        client, file_uri, line, character)

    for uri, ln, ch in positions[1:]:  # skip original
        try:
            alt = await query_fn(uri, ln, ch)
        except Exception:
            continue
        if alt:
            return alt
    return result


async def _prepare_with_fallback(client, file_uri, line, character,
                                 prepare_fn, query_fn):
    """Prepare a hierarchy item and query it, with def/decl fallback.

    Tries prepareCallHierarchy/prepareTypeHierarchy from the original
    position. If the query returns empty and the server needs position
    fallback, tries declaration and definition positions.
    Returns (results, lsp_item) or (None, None).
    """
    items = await prepare_fn(file_uri, line, character)
    if not items:
        return None, None

    lsp_item = items[0]
    results = await query_fn(lsp_item)
    if results or not client.needs_position_fallback:
        return results, lsp_item

    positions = await _resolve_positions(
        client, file_uri, line, character)

    for uri, ln, ch in positions[1:]:  # skip original
        try:
            alt_items = await prepare_fn(uri, ln, ch)
        except Exception:
            continue
        if not alt_items:
            continue
        try:
            alt_results = await query_fn(alt_items[0])
        except Exception:
            continue
        if alt_results:
            return alt_results, alt_items[0]

    return results, lsp_item


def _make_tree_node(item, call_sites=None, normalizer=None):
    """Build a tree node dict from a raw LSP hierarchy item."""
    kind_num = item.get("kind", 0)
    rng = item.get("selectionRange") or item.get("range", {})
    start = rng.get("start", {})
    node = {
        "name": item.get("name", "?"),
        "kind": _SYMBOL_KIND_NAMES.get(kind_num, ""),
        "file": _uri_to_path(item.get("uri", ""), normalizer),
        "line": start.get("line", 0) + 1,
        "children": [],
    }
    if call_sites is not None:
        node["call_sites"] = call_sites
    return node


def _node_key(node):
    """Unique key for cycle detection."""
    return (node["file"], node["line"], node["name"])


_TREE_WALK_CONCURRENCY = 8
_TREE_MAX_NODES = 250


async def _walk_tree(client, node, lsp_item, direction,
                     depth, visited, sem, node_count,
                     query_fn, extract_children,
                     normalizer=None):
    """Recursively expand a hierarchy (call or type) into a tree.

    *query_fn(lsp_item)* fetches the next level from the LSP server.
    *extract_children(results)* yields ``(raw_item, call_sites_or_None)``
    pairs from the server response.

    Passes the original LSP item (with its opaque ``data`` field) so the
    server can resolve it correctly. Concurrency bounded by *sem*;
    peek-ahead at depth=0; circuit-breaker at ``_TREE_MAX_NODES``.
    """
    if node_count[0] >= _TREE_MAX_NODES:
        node["has_more"] = True
        return
    key = _node_key(node)
    if key in visited:
        return
    visited.add(key)

    try:
        async with sem:
            results = await query_fn(lsp_item)
    except Exception:
        return

    if not results:
        return

    if depth <= 0:
        node["has_more"] = True
        return

    expand = []
    for raw_item, call_sites in extract_children(results):
        child = _make_tree_node(raw_item, call_sites,
                                normalizer=normalizer)
        node["children"].append(child)
        node_count[0] += 1
        child_key = _node_key(child)
        if child_key not in visited:
            expand.append((child, raw_item))

    if expand:
        await asyncio.gather(*(
            _walk_tree(client, c, ri, direction,
                       depth - 1, visited, sem, node_count,
                       query_fn, extract_children, normalizer)
            for c, ri in expand
        ))


def _extract_call_children(calls, direction):
    """Yield (raw_item, call_sites) from call hierarchy results."""
    for call in calls:
        raw_item = (call.get("from") if direction == "incoming"
                    else call.get("to"))
        if raw_item is None:
            continue
        from_ranges = call.get("fromRanges", [])
        yield raw_item, len(from_ranges) if from_ranges else 1


def _extract_type_children(items, direction):
    """Yield (raw_item, None) from type hierarchy results."""
    for item in items:
        yield item, None


_TREE_METHOD_CONFIG = {
    "lsp_call_tree_incoming": {
        "direction": "incoming",
        "prepare": "prepare_call_hierarchy",
        "query": "incoming_calls",
        "extract": _extract_call_children,
    },
    "lsp_call_tree_outgoing": {
        "direction": "outgoing",
        "prepare": "prepare_call_hierarchy",
        "query": "outgoing_calls",
        "extract": _extract_call_children,
    },
    "lsp_type_tree_supertypes": {
        "direction": "supertypes",
        "prepare": "prepare_type_hierarchy",
        "query": "supertypes",
        "extract": _extract_type_children,
    },
    "lsp_type_tree_subtypes": {
        "direction": "subtypes",
        "prepare": "prepare_type_hierarchy",
        "query": "subtypes",
        "extract": _extract_type_children,
    },
}


async def _handle_tree_query(client, method, params, file_uri,
                             line, character, indexing,
                             normalizer=None):
    """Shared handler for all recursive tree queries."""
    cfg = _TREE_METHOD_CONFIG[method]
    direction = cfg["direction"]
    max_depth = params.get("max_depth", 3)

    _, lsp_item = await _prepare_with_fallback(
        client, file_uri, line, character,
        getattr(client, cfg["prepare"]),
        getattr(client, cfg["query"]))
    if lsp_item is None:
        return {"direction": direction, "root": None,
                "indexing": indexing}

    root = _make_tree_node(lsp_item, normalizer=normalizer)
    sem = asyncio.Semaphore(_TREE_WALK_CONCURRENCY)
    nc = [0]
    query_fn = getattr(client, cfg["query"])
    extract = cfg["extract"]
    await _walk_tree(client, root, lsp_item, direction,
                     max_depth, set(), sem, nc,
                     query_fn,
                     lambda results: extract(results, direction),
                     normalizer)
    r = {"direction": direction, "root": root, "indexing": indexing}
    if nc[0] >= _TREE_MAX_NODES:
        r["truncated"] = True
    return r


def _parse_workspace_symbols(result, indexing=False, normalizer=None):
    """Parse workspace/symbol results into structured dicts."""
    symbols = []
    if result:
        for sym in result:
            kind_num = sym.get("kind", 0)
            loc = sym.get("location", {})
            uri = loc.get("uri", "")
            rng = loc.get("range", {})
            start = rng.get("start", {})
            entry = {
                "name": sym.get("name", "?"),
                "kind": _SYMBOL_KIND_NAMES.get(kind_num, ""),
                "file": _uri_to_path(uri, normalizer),
                "line": start.get("line", 0) + 1,
            }
            container = sym.get("containerName")
            if container:
                entry["container"] = container
            symbols.append(entry)

    result_dict = {"symbols": symbols}
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
        self._connection_tasks = {}  # session_id -> asyncio.Task
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
        task = asyncio.current_task()
        self._connection_tasks[session_id] = task
        logger.info("Frontend %d connected (%d total)", session_id, len(self._frontends))

        try:
            await session.handle()
        finally:
            self._connection_tasks.pop(session_id, None)
            transport = writer.transport
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                if transport:
                    transport.close()

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

            # Cancel all connection handler tasks so they don't
            # try to deregister during shutdown_all
            for task in list(self._connection_tasks.values()):
                task.cancel()
            for task in list(self._connection_tasks.values()):
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            self._frontends.clear()
            self._connection_tasks.clear()

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
    log_level = os.environ.get("LSP_MCP_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
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
