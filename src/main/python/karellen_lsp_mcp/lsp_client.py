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

"""Async LSP client that manages a single LSP server subprocess.

Uses a pluggable LspNormalizer to handle server-specific quirks (readiness
detection, transient error classification, error normalization). The normalizer
is selected automatically based on the server command.
"""

import asyncio
import json
import logging
import urllib.parse
from pathlib import Path

from lsprotocol import converters, types

from karellen_lsp_mcp.lsp_normalizer import (
    ServerState, create_normalizer,
)

logger = logging.getLogger(__name__)

_converter = converters.get_converter()


class LspClientError(Exception):
    pass


class LspClient:
    """Async client managing a single LSP server subprocess via JSON-RPC 2.0 over stdio."""

    def __init__(self, request_timeout=60, ready_timeout=120):
        self._process = None
        self._msg_id = 0
        self._pending = {}  # id -> Future
        self._reader_task = None
        self._open_files = set()
        self._opening_files = {}  # uri -> asyncio.Event (in-progress didOpen)
        self._diagnostics = {}  # uri -> list of diagnostics
        self._server_capabilities = None
        self._root_uri = None
        self._notification_callbacks = {}  # method -> list of callbacks
        self._normalizer = None
        self._initialized_event = asyncio.Event()
        self._settings = {}  # flat settings from init_options (e.g. "java.home" -> path)
        self._ready_event = asyncio.Event()
        self._indexing_done_task = None
        self._request_timeout = request_timeout
        self._ready_timeout = ready_timeout

    @property
    def root_uri(self):
        return self._root_uri

    @property
    def server_capabilities(self):
        return self._server_capabilities

    @property
    def state(self):
        if self._normalizer is None:
            return ServerState.STOPPED
        return self._normalizer.state

    @property
    def state_name(self):
        if self._normalizer is None:
            return ServerState.STOPPED.value
        return self._normalizer.state_name

    async def start(self, command, root_uri, init_options=None):
        """Spawn LSP server and perform initialize/initialized handshake."""
        self._root_uri = root_uri
        root_path = urllib.parse.unquote(root_uri).removeprefix("file://")

        # Store settings for workspace/configuration responses
        if init_options and isinstance(init_options.get("settings"), dict):
            self._settings = init_options["settings"]

        self._normalizer = create_normalizer(command, warmup_timeout=self._ready_timeout)
        self._normalizer.set_ready_callback(self._on_normalizer_ready)

        logger.info("Starting LSP server: %s (root=%s)", command, root_uri)
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        self._reader_task = asyncio.create_task(self._read_loop())

        _all_symbol_kinds = list(types.SymbolKind)
        _symbol_tags = [types.SymbolTag.Deprecated]
        _diag_tags = [types.DiagnosticTag.Unnecessary,
                      types.DiagnosticTag.Deprecated]

        init_params = types.InitializeParams(
            process_id=None,
            root_uri=root_uri,
            root_path=root_path,
            capabilities=types.ClientCapabilities(
                general=types.GeneralClientCapabilities(
                    position_encodings=[
                        types.PositionEncodingKind.Utf32,
                        types.PositionEncodingKind.Utf16,
                    ],
                    markdown=types.MarkdownClientCapabilities(
                        parser="markdown", version="1.0"),
                ),
                window=types.WindowClientCapabilities(
                    work_done_progress=True,
                    show_message=types.ShowMessageRequestClientCapabilities(),
                    show_document=types.ShowDocumentClientCapabilities(
                        support=True),
                ),
                workspace=types.WorkspaceClientCapabilities(
                    configuration=True,
                    workspace_folders=True,
                    symbol=types.WorkspaceSymbolClientCapabilities(
                        dynamic_registration=False,
                        symbol_kind=types.ClientSymbolKindOptions(
                            value_set=_all_symbol_kinds),
                        tag_support=types.ClientSymbolTagOptions(
                            value_set=_symbol_tags),
                    ),
                ),
                text_document=types.TextDocumentClientCapabilities(
                    definition=types.DefinitionClientCapabilities(
                        dynamic_registration=False,
                        link_support=True,
                    ),
                    declaration=types.DeclarationClientCapabilities(
                        dynamic_registration=False,
                        link_support=True,
                    ),
                    implementation=types.ImplementationClientCapabilities(
                        dynamic_registration=False,
                        link_support=True,
                    ),
                    type_definition=types.TypeDefinitionClientCapabilities(
                        dynamic_registration=False,
                        link_support=True,
                    ),
                    references=types.ReferenceClientCapabilities(
                        dynamic_registration=False,
                    ),
                    hover=types.HoverClientCapabilities(
                        dynamic_registration=False,
                        content_format=[types.MarkupKind.Markdown,
                                        types.MarkupKind.PlainText],
                    ),
                    document_symbol=types.DocumentSymbolClientCapabilities(
                        dynamic_registration=False,
                        hierarchical_document_symbol_support=True,
                        symbol_kind=types.ClientSymbolKindOptions(
                            value_set=_all_symbol_kinds),
                        tag_support=types.ClientSymbolTagOptions(
                            value_set=_symbol_tags),
                    ),
                    document_highlight=types.DocumentHighlightClientCapabilities(
                        dynamic_registration=False,
                    ),
                    publish_diagnostics=types.PublishDiagnosticsClientCapabilities(
                        related_information=True,
                        tag_support=types.ClientDiagnosticsTagOptions(
                            value_set=_diag_tags),
                        code_description_support=True,
                    ),
                    diagnostic=types.DiagnosticClientCapabilities(
                        dynamic_registration=False,
                    ),
                    call_hierarchy=types.CallHierarchyClientCapabilities(
                        dynamic_registration=False,
                    ),
                    type_hierarchy=types.TypeHierarchyClientCapabilities(
                        dynamic_registration=False,
                    ),
                ),
            ),
            initialization_options=init_options,
        )

        result = await self._send_request("initialize", _converter.unstructure(init_params))
        self._server_capabilities = result.get("capabilities", {})
        self._normalizer.on_server_info(result.get("serverInfo"))
        await self._send_notification("initialized", {})
        logger.info("LSP server initialized successfully")

        self._normalizer.on_started()
        self._initialized_event.set()
        self._indexing_done_task = asyncio.create_task(self._indexing_timeout())

    async def stop(self):
        """Shutdown and exit the LSP server."""
        if self._normalizer:
            self._normalizer.on_stopped()
        self._initialized_event.set()
        self._ready_event.set()

        if self._indexing_done_task:
            self._indexing_done_task.cancel()
            try:
                await self._indexing_done_task
            except asyncio.CancelledError:
                pass
            self._indexing_done_task = None

        if self._process is None:
            return

        try:
            await self._send_request("shutdown", None)
        except Exception:
            logger.warning("Shutdown request failed", exc_info=True)

        try:
            await self._send_notification("exit", None)
        except Exception:
            pass

        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        if self._process.returncode is None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()

        self._process = None
        self._open_files.clear()
        self._diagnostics.clear()

        # Fail any pending requests
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(LspClientError("LSP server stopped"))
        self._pending.clear()

    async def wait_initialized(self, timeout=None):
        """Wait until the LSP initialize/initialized handshake completes.

        Returns True if past STARTING, False on timeout or STOPPED/ERROR.
        After this, the server is at least INDEXING (or READY).
        """
        t = timeout if timeout is not None else 60
        try:
            await asyncio.wait_for(self._initialized_event.wait(), timeout=t)
        except asyncio.TimeoutError:
            pass
        return self.state not in (ServerState.STARTING, ServerState.STOPPED,
                                  ServerState.ERROR)

    async def wait_ready(self, timeout=None):
        """Wait until the server transitions to READY or ERROR state.

        Returns True if READY, False if ERROR or timeout.
        """
        t = timeout if timeout is not None else 60
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=t)
        except asyncio.TimeoutError:
            pass
        return self.state == ServerState.READY

    async def ensure_file_open(self, uri):
        """Send textDocument/didOpen if the file is not already open."""
        if uri in self._open_files:
            return

        # If another coroutine is already opening this file, wait for it
        event = self._opening_files.get(uri)
        if event is not None:
            await event.wait()
            return

        event = asyncio.Event()
        self._opening_files[uri] = event
        try:
            file_path = urllib.parse.unquote(uri).removeprefix("file://")
            try:
                text = Path(file_path).read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                raise LspClientError("Cannot read file %s: %s" % (file_path, e))

            # Detect language ID from extension
            ext = Path(file_path).suffix.lower()
            lang_map = {
                ".c": "c", ".h": "c",
                ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
                ".hh": "cpp", ".hpp": "cpp", ".hxx": "cpp",
                ".py": "python",
                ".rs": "rust",
                ".java": "java",
                ".kt": "kotlin",
                ".kts": "kotlin",
                ".js": "javascript",
                ".ts": "typescript",
                ".go": "go",
            }
            language_id = lang_map.get(ext, "plaintext")

            params = types.DidOpenTextDocumentParams(
                text_document=types.TextDocumentItem(
                    uri=uri,
                    language_id=language_id,
                    version=0,
                    text=text,
                )
            )
            await self._send_notification("textDocument/didOpen", _converter.unstructure(params))
            self._open_files.add(uri)
        finally:
            self._opening_files.pop(uri, None)
            event.set()

    async def definition(self, uri, line, character):
        """textDocument/definition"""
        params = self._text_document_position(uri, line, character)
        return await self._request_with_retry("textDocument/definition", params)

    async def declaration(self, uri, line, character):
        """textDocument/declaration"""
        params = self._text_document_position(uri, line, character)
        return await self._request_with_retry("textDocument/declaration", params)

    async def implementation(self, uri, line, character):
        """textDocument/implementation"""
        params = self._text_document_position(uri, line, character)
        return await self._request_with_retry("textDocument/implementation", params)

    async def type_definition(self, uri, line, character):
        """textDocument/typeDefinition"""
        params = self._text_document_position(uri, line, character)
        return await self._request_with_retry("textDocument/typeDefinition", params)

    async def workspace_symbol(self, query):
        """workspace/symbol"""
        return await self._request_with_retry("workspace/symbol", {"query": query})

    async def references(self, uri, line, character, include_declaration=True):
        """textDocument/references"""
        params = self._text_document_position(uri, line, character)
        params["context"] = {"includeDeclaration": include_declaration}
        return await self._request_with_retry("textDocument/references", params)

    async def hover(self, uri, line, character):
        """textDocument/hover"""
        params = self._text_document_position(uri, line, character)
        return await self._request_with_retry("textDocument/hover", params)

    async def document_symbols(self, uri):
        """textDocument/documentSymbol"""
        params = {"textDocument": {"uri": uri}}
        return await self._request_with_retry("textDocument/documentSymbol", params)

    async def prepare_call_hierarchy(self, uri, line, character):
        """textDocument/prepareCallHierarchy"""
        params = self._text_document_position(uri, line, character)
        return await self._request_with_retry("textDocument/prepareCallHierarchy", params)

    async def incoming_calls(self, item):
        """callHierarchy/incomingCalls"""
        return await self._request_with_retry("callHierarchy/incomingCalls", {"item": item})

    async def outgoing_calls(self, item):
        """callHierarchy/outgoingCalls"""
        return await self._request_with_retry("callHierarchy/outgoingCalls", {"item": item})

    async def prepare_type_hierarchy(self, uri, line, character):
        """textDocument/prepareTypeHierarchy"""
        params = self._text_document_position(uri, line, character)
        return await self._request_with_retry("textDocument/prepareTypeHierarchy", params)

    async def supertypes(self, item):
        """typeHierarchy/supertypes"""
        return await self._request_with_retry("typeHierarchy/supertypes", {"item": item})

    async def subtypes(self, item):
        """typeHierarchy/subtypes"""
        return await self._request_with_retry("typeHierarchy/subtypes", {"item": item})

    def get_diagnostics(self, uri):
        """Return cached diagnostics for a URI."""
        return self._diagnostics.get(uri, [])

    def get_indexing_status(self):
        """Return indexing status from the normalizer."""
        if self._normalizer is None:
            return {"state": ServerState.STOPPED.value}
        return self._normalizer.get_indexing_status()

    def estimated_remaining_seconds(self):
        """Estimate seconds remaining for indexing, or None if unknown."""
        if self._normalizer is None:
            return None
        return self._normalizer.estimated_remaining_seconds()

    @property
    def needs_position_fallback(self):
        """Whether cross-file queries should try def/decl fallback."""
        if self._normalizer is None:
            return False
        return self._normalizer.needs_position_fallback

    def supports_method(self, method):
        """Check if the LSP server supports the given method."""
        if self._normalizer is None:
            return True
        return self._normalizer.supports_method(method)

    def on_notification(self, method, callback):
        """Register a callback for a specific notification method."""
        self._notification_callbacks.setdefault(method, []).append(callback)

    def _on_normalizer_ready(self):
        """Called by the normalizer when the server transitions to READY."""
        self._ready_event.set()

    async def _indexing_timeout(self):
        """Background task: grace period for $/progress, then absolute timeout.

        If the normalizer's on_warmup_timeout decides not to force-mark READY
        (because progress is actively being reported), we keep checking
        periodically until indexing finishes naturally.
        """
        try:
            await asyncio.sleep(5)
            if self._normalizer:
                self._normalizer.on_no_progress_timeout()

            if self._normalizer:
                remaining = self._normalizer.warmup_timeout - 5
                if remaining > 0 and self.state == ServerState.INDEXING:
                    await asyncio.sleep(remaining)

            # If still indexing (progress is active so normalizer didn't force READY),
            # keep checking periodically until progress completes
            while self._normalizer and self.state == ServerState.INDEXING:
                self._normalizer.on_warmup_timeout()
                if self.state != ServerState.INDEXING:
                    break
                # Still indexing — progress is active, check again in 30s
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            pass

    async def _request_with_retry(self, method, params):
        """Send an LSP request with normalizer-driven retry for transient errors."""
        normalizer = self._normalizer
        retries = normalizer.max_retries if normalizer else 1
        last_error = None

        for attempt in range(retries):
            try:
                return await self._send_request(method, params)
            except LspClientError as e:
                last_error = e
                if normalizer and normalizer.is_transient_error(str(e)):
                    if attempt < retries - 1:
                        logger.debug(
                            "Transient LSP error on %s (attempt %d/%d): %s",
                            method, attempt + 1, retries, e)
                        await asyncio.sleep(normalizer.retry_delay)
                        continue
                if normalizer:
                    raise normalizer.normalize_error(e)
                raise

        if normalizer:
            raise normalizer.normalize_error(last_error)
        raise last_error

    def _text_document_position(self, uri, line, character):
        return {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        }

    def _next_id(self):
        self._msg_id += 1
        return self._msg_id

    async def _send_request(self, method, params):
        """Send a JSON-RPC request and wait for the response."""
        if self._process is None or self._process.stdin is None:
            raise LspClientError("LSP server not running")

        msg_id = self._next_id()
        msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            msg["params"] = params

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[msg_id] = fut

        await self._write_message(msg)

        try:
            return await asyncio.wait_for(fut, timeout=self._request_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise LspClientError("Timeout waiting for response to %s" % method)

    async def _send_notification(self, method, params):
        """Send a JSON-RPC notification (no id, no response expected)."""
        if self._process is None or self._process.stdin is None:
            raise LspClientError("LSP server not running")

        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params

        await self._write_message(msg)

    async def _write_message(self, msg):
        body = json.dumps(msg, separators=(",", ":"))
        body_bytes = body.encode("utf-8")
        header = "Content-Length: %d\r\n\r\n" % len(body_bytes)
        self._process.stdin.write(header.encode("ascii") + body_bytes)
        await self._process.stdin.drain()

    async def _read_loop(self):
        """Background task reading JSON-RPC messages from the LSP server stdout."""
        try:
            while True:
                msg = await self._read_message()
                if msg is None:
                    break
                await self._dispatch_message(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("LSP reader loop error", exc_info=True)

    async def _read_message(self):
        """Read one Content-Length framed JSON-RPC message from stdout."""
        stdout = self._process.stdout
        if stdout is None:
            return None

        headers = {}
        while True:
            line = await stdout.readline()
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

        body = await stdout.readexactly(content_length)
        return json.loads(body)

    async def _dispatch_message(self, msg):
        """Route an incoming JSON-RPC message."""
        if "id" in msg and "method" not in msg:
            # Response
            msg_id = msg["id"]
            fut = self._pending.pop(msg_id, None)
            if fut is None:
                logger.warning("Received response for unknown id: %s", msg_id)
                return
            if "error" in msg:
                err = msg["error"]
                fut.set_exception(LspClientError(
                    "LSP error %s: %s" % (err.get("code", "?"), err.get("message", "unknown"))
                ))
            else:
                fut.set_result(msg.get("result"))
        elif "method" in msg and "id" not in msg:
            # Notification
            self._handle_notification(msg["method"], msg.get("params"))
        elif "method" in msg and "id" in msg:
            # Server request (e.g., window/showMessage, workspace/configuration)
            # Respond with null for unsupported requests
            await self._respond_to_server_request(msg["id"], msg["method"], msg.get("params"))

    async def _respond_to_server_request(self, msg_id, method, params):
        """Respond to server-initiated requests with sensible defaults."""
        result = None
        if method == "workspace/configuration":
            # Return settings matching each requested section
            items = (params or {}).get("items", [])
            result = []
            for item in items:
                section = item.get("section", "")
                if self._settings and section:
                    # Build a settings dict for this section by matching
                    # keys that start with "{section}." and stripping
                    # the prefix, or returning the full settings if
                    # section is empty
                    prefix = section + "."
                    section_settings = {}
                    for k, v in self._settings.items():
                        if k.startswith(prefix):
                            # Nest dotted keys: "import.gradle.java.home" -> {"import": {"gradle": {"java": {"home": v}}}}
                            parts = k[len(prefix):].split(".")
                            d = section_settings
                            for p in parts[:-1]:
                                d = d.setdefault(p, {})
                            d[parts[-1]] = v
                        elif k == section:
                            section_settings = v
                            break
                    result.append(section_settings if section_settings else None)
                else:
                    result.append(self._settings if self._settings else None)
        elif method == "client/registerCapability":
            result = None
        elif method == "window/workDoneProgress/create":
            result = None

        response = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        await self._write_message(response)

    def _handle_notification(self, method, params):
        """Handle LSP notifications."""
        if method == "textDocument/publishDiagnostics" and params:
            uri = params.get("uri", "")
            self._diagnostics[uri] = params.get("diagnostics", [])
        elif method == "window/logMessage" and params:
            level = params.get("type", 4)
            message = params.get("message", "")
            if level <= 1:
                logger.error("LSP: %s", message)
            elif level == 2:
                logger.warning("LSP: %s", message)
            else:
                logger.debug("LSP: %s", message)

        # Delegate to normalizer
        if self._normalizer:
            self._normalizer.on_notification(method, params)

        # Dispatch to registered callbacks
        for cb in self._notification_callbacks.get(method, []):
            try:
                cb(method, params)
            except Exception:
                logger.warning("Notification callback error for %s", method, exc_info=True)
