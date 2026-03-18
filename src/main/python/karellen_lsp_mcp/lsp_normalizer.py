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

"""LSP server normalizers: server-specific behavior adapters.

Each LSP server (clangd, rust-analyzer, pyright, etc.) has its own quirks:
different readiness detection, transient error patterns, response formats.

Normalizers encapsulate these quirks behind a common interface so that
LspClient remains generic. LspClient delegates policy decisions
(is this error transient? is the server ready?) to its normalizer.
"""

import enum
import logging
import os
import re
import time

logger = logging.getLogger(__name__)


class ServerState(enum.Enum):
    STARTING = "starting"
    INDEXING = "indexing"
    READY = "ready"
    ERROR = "error"
    STOPPED = "stopped"


class LspNormalizer:
    """Base normalizer — no quirks, assumes server is always ready."""

    def __init__(self):
        self._state = ServerState.STARTING
        self._ready_callback = None
        self._server_version = None

    @property
    def state(self):
        return self._state

    @property
    def state_name(self):
        return self._state.value

    @property
    def server_version(self):
        return self._server_version

    def set_ready_callback(self, callback):
        """Set a callback to be invoked when the server becomes ready.

        The callback receives no arguments. LspClient uses this to
        set its ready event.
        """
        self._ready_callback = callback

    def on_server_info(self, server_info):
        """Called with the serverInfo from the initialize response.

        Subclasses can override to extract version information.
        """
        if server_info:
            self._server_version = server_info.get("version")

    def on_started(self):
        """Called after the LSP initialize/initialized handshake completes."""
        self._state = ServerState.READY
        if self._ready_callback:
            self._ready_callback()

    def on_stopped(self):
        """Called when the LSP server is being stopped."""
        self._state = ServerState.STOPPED

    def on_notification(self, method, params):
        """Called for every LSP notification. Override to track state."""
        pass

    def on_no_progress_timeout(self):
        """Called when the grace period expires with no progress notifications."""
        pass

    def on_warmup_timeout(self):
        """Called when the absolute warmup timeout expires."""
        pass

    @property
    def warmup_timeout(self):
        """Max seconds to wait for server warmup."""
        return 5

    def get_indexing_status(self):
        """Return current indexing/readiness status."""
        return {"state": self._state.value}

    def estimated_remaining_seconds(self):
        """Estimate seconds remaining for indexing, or None if unknown."""
        return None

    def is_transient_error(self, error_msg):
        """Return True if the error is transient and the request should be retried."""
        return False

    def normalize_error(self, error):
        """Transform an LspClientError into a more user-friendly one.

        Return the original error unchanged if no normalization applies.
        """
        return error

    def supports_method(self, method):
        """Return True if the LSP server supports the given method.

        Base implementation assumes all methods are supported.
        Subclasses can override based on detected server version.
        """
        return True

    @property
    def max_retries(self):
        """Max retry attempts for transient errors during warmup."""
        return 1

    @property
    def retry_delay(self):
        """Seconds between retries."""
        return 0


class ClangdNormalizer(LspNormalizer):
    """Normalizer for clangd-specific behavior.

    clangd needs time after startup to index a project. During this warmup:

    - Single-file queries (hover, definition, document symbols) work immediately
      because clangd parses the preamble on didOpen.
    - Cross-file queries (references, incoming calls) return valid but incomplete
      results until background indexing finishes.
    - clangd reports indexing progress via $/progress notifications.

    This normalizer:
    - Tracks $/progress notifications to detect when indexing completes
    - Classifies clangd-specific transient errors for retry
    - Normalizes LSP JSON-RPC errors into user-friendly messages
    - Detects clangd version and reports feature availability
    """

    # Error message fragments that indicate a transient warmup condition
    _TRANSIENT_ERROR_FRAGMENTS = (
        "not indexed",
        "not ready",
        "building preamble",
        "background indexing",
        "file not found in compilation database",
    )

    # Minimum clangd major version required for each LSP method.
    # Methods not listed here are assumed supported by all versions.
    _METHOD_MIN_VERSION = {
        "callHierarchy/outgoingCalls": 20,
    }

    _VERSION_RE = re.compile(r"(\d+)\.")

    def __init__(self, warmup_timeout=60, max_retries=5, retry_delay=1.0):
        super().__init__()
        self._warmup_timeout = warmup_timeout
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._start_time = None
        self._active_progress_tokens = set()
        self._saw_any_progress = False
        self._progress = {}  # token -> {title, message, percentage}
        self._completed_progress = []  # [{title, message}]
        self._major_version = None

    def on_server_info(self, server_info):
        super().on_server_info(server_info)
        if self._server_version:
            m = self._VERSION_RE.search(self._server_version)
            if m:
                self._major_version = int(m.group(1))
                logger.info("Detected clangd major version: %d", self._major_version)

    def supports_method(self, method):
        min_ver = self._METHOD_MIN_VERSION.get(method)
        if min_ver is None:
            return True
        if self._major_version is None:
            # Unknown version — assume supported, let runtime error handle it
            return True
        return self._major_version >= min_ver

    def on_started(self):
        self._start_time = time.monotonic()
        self._state = ServerState.INDEXING

    def on_stopped(self):
        self._state = ServerState.STOPPED

    def on_notification(self, method, params):
        if method != "$/progress" or not params:
            return

        token = params.get("token")
        value = params.get("value", {})
        kind = value.get("kind", "")

        if kind == "begin":
            self._saw_any_progress = True
            self._active_progress_tokens.add(token)
            title = value.get("title", "")
            message = value.get("message", "")
            self._progress[token] = {
                "title": title,
                "message": message,
                "percentage": value.get("percentage"),
            }
            logger.info("LSP progress begin [%s]: %s %s",
                        token, title, message)
        elif kind == "report":
            if token in self._progress:
                if value.get("message") is not None:
                    self._progress[token]["message"] = value["message"]
                if value.get("percentage") is not None:
                    self._progress[token]["percentage"] = value["percentage"]
            msg_parts = []
            if value.get("message"):
                msg_parts.append(value["message"])
            if value.get("percentage") is not None:
                msg_parts.append("%d%%" % value["percentage"])
            logger.debug("LSP progress [%s]: %s",
                         token, " ".join(msg_parts))
        elif kind == "end":
            self._active_progress_tokens.discard(token)
            entry = self._progress.pop(token, None)
            if entry:
                self._completed_progress.append({
                    "title": entry["title"],
                    "message": value.get("message", ""),
                })
            logger.info("LSP progress end [%s]: %s",
                        token, value.get("message", ""))
            if self._saw_any_progress and not self._active_progress_tokens:
                self._mark_ready()

    def get_indexing_status(self):
        """Return current indexing status with progress details."""
        elapsed = time.monotonic() - self._start_time if self._start_time else 0
        active = []
        for token in self._active_progress_tokens:
            entry = self._progress.get(token, {})
            item = {"title": entry.get("title", "")}
            if entry.get("message"):
                item["message"] = entry["message"]
            if entry.get("percentage") is not None:
                item["percentage"] = entry["percentage"]
            active.append(item)
        return {
            "state": self._state.value,
            "elapsed_seconds": round(elapsed, 1),
            "active_tasks": active,
            "completed_tasks": len(self._completed_progress),
        }

    def on_no_progress_timeout(self):
        """Called by LspClient when the grace period expires with no
        $/progress notifications. Assumes server is ready."""
        if not self._saw_any_progress and self._state == ServerState.INDEXING:
            logger.info("No $/progress notifications received, "
                        "marking LSP server as READY")
            self._mark_ready()

    def on_warmup_timeout(self):
        """Called by LspClient when the absolute warmup timeout expires."""
        if self._state == ServerState.INDEXING:
            if self._active_progress_tokens:
                pct = self._best_percentage()
                logger.info("LSP indexing timeout after %ds but progress is active "
                            "(percentage=%s), not forcing READY",
                            self._warmup_timeout,
                            "%d%%" % pct if pct is not None else "unknown")
                return
            logger.warning("LSP indexing timeout after %ds, marking as READY",
                           self._warmup_timeout)
            self._mark_ready()

    @property
    def saw_any_progress(self):
        return self._saw_any_progress

    @property
    def warmup_timeout(self):
        return self._warmup_timeout

    def estimated_remaining_seconds(self):
        """Estimate seconds remaining based on elapsed time and percentage."""
        pct = self._best_percentage()
        if pct is None or pct <= 0 or self._start_time is None:
            return None
        elapsed = time.monotonic() - self._start_time
        estimated_total = elapsed / (pct / 100.0)
        return max(0.0, estimated_total - elapsed)

    def _best_percentage(self):
        """Return the highest percentage across active progress tokens, or None."""
        best = None
        for token in self._active_progress_tokens:
            entry = self._progress.get(token, {})
            pct = entry.get("percentage")
            if pct is not None and (best is None or pct > best):
                best = pct
        return best

    def is_transient_error(self, error_msg):
        lower = error_msg.lower()
        return any(frag in lower
                   for frag in self._TRANSIENT_ERROR_FRAGMENTS)

    def normalize_error(self, error):
        msg = str(error)

        if "method not found" in msg.lower() or "-32601" in msg:
            from karellen_lsp_mcp.lsp_client import LspClientError
            return LspClientError(
                "This LSP server does not support the requested operation")

        if "not initialized" in msg.lower() or "-32002" in msg:
            from karellen_lsp_mcp.lsp_client import LspClientError
            return LspClientError(
                "LSP server is still initializing. "
                "Please try again in a moment.")

        if "-32603" in msg:
            from karellen_lsp_mcp.lsp_client import LspClientError
            return LspClientError(
                "LSP server internal error: %s" % msg)

        if "-32800" in msg or "request cancelled" in msg.lower():
            from karellen_lsp_mcp.lsp_client import LspClientError
            return LspClientError(
                "Request was cancelled by the LSP server "
                "(it may be busy processing other requests)")

        if "-32801" in msg or "content modified" in msg.lower():
            from karellen_lsp_mcp.lsp_client import LspClientError
            return LspClientError(
                "File content changed during the request. Please retry.")

        if "timeout" in msg.lower():
            from karellen_lsp_mcp.lsp_client import LspClientError
            return LspClientError(
                "LSP server did not respond in time. "
                "The server may be busy indexing the project.")

        return error

    @property
    def max_retries(self):
        if self._state == ServerState.INDEXING:
            return self._max_retries
        return 1

    @property
    def retry_delay(self):
        return self._retry_delay

    def _mark_ready(self):
        if self._state == ServerState.INDEXING:
            elapsed = time.monotonic() - self._start_time
            logger.info("LSP server ready after %.1fs", elapsed)
            self._state = ServerState.READY
            if self._ready_callback:
                self._ready_callback()


class JdtlsNormalizer(LspNormalizer):
    """Normalizer for Eclipse JDT Language Server (jdtls) behavior.

    jdtls needs time to import the workspace after startup. During this import:

    - Single-file queries may work partially after initialization.
    - Cross-file queries return incomplete results until workspace import finishes.
    - jdtls reports progress via $/progress notifications.

    This normalizer:
    - Starts in INDEXING state (jdtls imports workspace before becoming READY)
    - Tracks $/progress notifications to detect import completion
    - Classifies jdtls-specific transient errors for retry
    - Uses a higher default warmup timeout (180s) for large projects
    """

    _TRANSIENT_ERROR_FRAGMENTS = (
        "not initialized",
        "server not ready",
        "project not found",
        "not yet ready",
        "workspace is initializing",
    )

    def __init__(self, warmup_timeout=180, max_retries=5, retry_delay=2.0):
        super().__init__()
        self._warmup_timeout = warmup_timeout
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._start_time = None
        self._active_progress_tokens = set()
        self._saw_any_progress = False
        self._progress = {}
        self._completed_progress = []

    def on_started(self):
        self._start_time = time.monotonic()
        self._state = ServerState.INDEXING

    def on_stopped(self):
        self._state = ServerState.STOPPED

    def on_notification(self, method, params):
        if method != "$/progress" or not params:
            return

        token = params.get("token")
        value = params.get("value", {})
        kind = value.get("kind", "")

        if kind == "begin":
            self._saw_any_progress = True
            self._active_progress_tokens.add(token)
            title = value.get("title", "")
            message = value.get("message", "")
            self._progress[token] = {
                "title": title,
                "message": message,
                "percentage": value.get("percentage"),
            }
            logger.info("LSP progress begin [%s]: %s %s", token, title, message)
        elif kind == "report":
            if token in self._progress:
                if value.get("message") is not None:
                    self._progress[token]["message"] = value["message"]
                if value.get("percentage") is not None:
                    self._progress[token]["percentage"] = value["percentage"]
        elif kind == "end":
            self._active_progress_tokens.discard(token)
            entry = self._progress.pop(token, None)
            if entry:
                self._completed_progress.append({
                    "title": entry["title"],
                    "message": value.get("message", ""),
                })
            logger.info("LSP progress end [%s]: %s", token, value.get("message", ""))
            if self._saw_any_progress and not self._active_progress_tokens:
                self._mark_ready()

    def get_indexing_status(self):
        elapsed = time.monotonic() - self._start_time if self._start_time else 0
        active = []
        for token in self._active_progress_tokens:
            entry = self._progress.get(token, {})
            item = {"title": entry.get("title", "")}
            if entry.get("message"):
                item["message"] = entry["message"]
            if entry.get("percentage") is not None:
                item["percentage"] = entry["percentage"]
            active.append(item)
        return {
            "state": self._state.value,
            "elapsed_seconds": round(elapsed, 1),
            "active_tasks": active,
            "completed_tasks": len(self._completed_progress),
        }

    def on_no_progress_timeout(self):
        if not self._saw_any_progress and self._state == ServerState.INDEXING:
            logger.info("No $/progress notifications received, "
                        "marking jdtls as READY")
            self._mark_ready()

    def on_warmup_timeout(self):
        if self._state == ServerState.INDEXING:
            if self._active_progress_tokens:
                logger.info("jdtls indexing timeout after %ds but progress is active, "
                            "not forcing READY", self._warmup_timeout)
                return
            logger.warning("jdtls indexing timeout after %ds, marking as READY",
                           self._warmup_timeout)
            self._mark_ready()

    @property
    def warmup_timeout(self):
        return self._warmup_timeout

    def estimated_remaining_seconds(self):
        pct = self._best_percentage()
        if pct is None or pct <= 0 or self._start_time is None:
            return None
        elapsed = time.monotonic() - self._start_time
        estimated_total = elapsed / (pct / 100.0)
        return max(0.0, estimated_total - elapsed)

    def _best_percentage(self):
        best = None
        for token in self._active_progress_tokens:
            entry = self._progress.get(token, {})
            pct = entry.get("percentage")
            if pct is not None and (best is None or pct > best):
                best = pct
        return best

    def is_transient_error(self, error_msg):
        lower = error_msg.lower()
        return any(frag in lower for frag in self._TRANSIENT_ERROR_FRAGMENTS)

    def normalize_error(self, error):
        msg = str(error)

        if "method not found" in msg.lower() or "-32601" in msg:
            from karellen_lsp_mcp.lsp_client import LspClientError
            return LspClientError(
                "This LSP server does not support the requested operation")

        if "not initialized" in msg.lower() or "-32002" in msg:
            from karellen_lsp_mcp.lsp_client import LspClientError
            return LspClientError(
                "LSP server is still initializing. "
                "Please try again in a moment.")

        return error

    @property
    def max_retries(self):
        if self._state == ServerState.INDEXING:
            return self._max_retries
        return 1

    @property
    def retry_delay(self):
        return self._retry_delay

    def _mark_ready(self):
        if self._state == ServerState.INDEXING:
            elapsed = time.monotonic() - self._start_time
            logger.info("jdtls ready after %.1fs", elapsed)
            self._state = ServerState.READY
            if self._ready_callback:
                self._ready_callback()


def create_normalizer(command, warmup_timeout=60):
    """Factory: pick the right normalizer based on the LSP server command."""
    if command and command[0].endswith("clangd"):
        return ClangdNormalizer(warmup_timeout=warmup_timeout)
    if command:
        cmd_basename = os.path.basename(command[0])
        if cmd_basename in ("jdtls", "jdtls.sh", "jdtls.bat"):
            return JdtlsNormalizer(warmup_timeout=max(warmup_timeout, 180))
    # Default: no quirks
    return LspNormalizer()
