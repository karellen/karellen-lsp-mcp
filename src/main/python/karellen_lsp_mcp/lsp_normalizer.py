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
import re
import time
import urllib.parse

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
        self._uri_reverse_map = {}  # normalized → original

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

    @property
    def needs_position_fallback(self):
        """Whether cross-file queries should try def/decl fallback positions.

        Some servers (e.g., clangd) resolve cross-TU references only from
        certain positions (declaration vs definition). When True, the daemon
        tries alternate positions if the initial query returns empty.
        """
        return False

    @property
    def uri_schemes(self):
        """Return non-standard URI schemes this normalizer transforms.

        Used by the proxy to find URIs embedded in string values
        (e.g., markdown hover content). Return an empty tuple if the
        normalizer only handles structured URI fields.
        """
        return ()

    def normalize_uri(self, uri):
        """Normalize a server-specific URI to a standard form.

        Subclasses can override to convert proprietary URI schemes
        (e.g. jdt://) into standard forms (e.g. jar:file://).
        """
        return uri

    def normalize_response(self, result):
        """Normalize URIs in an LSP JSON response.

        Walks the response structure and normalizes both structured
        URI fields ('uri', 'targetUri') and URIs embedded in string
        values (e.g., jdt:// links in hover markdown).

        Records normalized → original mappings in the internal
        reverse map for subsequent denormalization of incoming params.
        """
        if result is None:
            return result
        scan_strings = bool(self.uri_schemes)
        self._normalize_response_walk(result, scan_strings)
        return result

    def _normalize_response_walk(self, result, scan_strings):
        if isinstance(result, list):
            for i, item in enumerate(result):
                if isinstance(item, str) and scan_strings:
                    result[i] = self._normalize_uris_in_string(item)
                elif isinstance(item, (dict, list)):
                    self._normalize_response_walk(
                        item, scan_strings)
        elif isinstance(result, dict):
            for key in ("uri", "targetUri"):
                if key in result and isinstance(result[key], str):
                    original = result[key]
                    normalized = self.normalize_uri(original)
                    if normalized != original:
                        result[key] = normalized
                        self._record_reverse(normalized, original)
            for key, v in result.items():
                if key in ("uri", "targetUri"):
                    continue
                if isinstance(v, str) and scan_strings:
                    result[key] = self._normalize_uris_in_string(v)
                elif isinstance(v, (dict, list)):
                    self._normalize_response_walk(
                        v, scan_strings)

    def _record_reverse(self, normalized, original):
        """Record a reverse URI mapping."""
        self._uri_reverse_map[normalized] = original

    def denormalize_params(self, params):
        """Reverse-map normalized URIs in incoming request params.

        Replaces normalized URIs (in structured 'uri' fields and
        string values) with their originals so the backend server
        recognizes them. Used for hierarchy item roundtripping.
        """
        if not self._uri_reverse_map or params is None:
            return params
        if isinstance(params, list):
            for i, item in enumerate(params):
                if isinstance(item, str):
                    params[i] = self._denormalize_uris_in_string(
                        item)
                else:
                    self.denormalize_params(item)
        elif isinstance(params, dict):
            for key in ("uri", "targetUri"):
                if key in params and isinstance(params[key], str):
                    original = self._uri_reverse_map.get(
                        params[key])
                    if original is not None:
                        params[key] = original
            for key, v in params.items():
                if key in ("uri", "targetUri"):
                    continue
                if isinstance(v, str):
                    denorm = self._denormalize_uris_in_string(v)
                    if denorm is not v:
                        params[key] = denorm
                elif isinstance(v, (dict, list)):
                    self.denormalize_params(v)
        return params

    def _normalize_uris_in_string(self, text):
        """Replace non-standard URIs embedded in a string value."""
        pattern = self._get_uri_scheme_re()
        if pattern is None:
            return text

        def _replace(match):
            original = match.group(0)
            normalized = self.normalize_uri(original)
            if normalized != original:
                self._record_reverse(normalized, original)
                return normalized
            return original
        return pattern.sub(_replace, text)

    def _denormalize_uris_in_string(self, text):
        """Replace normalized URIs in text with originals."""
        if not self._uri_reverse_map:
            return text
        result = text
        for normalized, original in self._uri_reverse_map.items():
            if normalized in result:
                result = result.replace(normalized, original)
        return result

    _uri_scheme_re_cache = {}

    def _get_uri_scheme_re(self):
        """Return compiled regex for URI schemes, or None."""
        schemes = self.uri_schemes
        if not schemes:
            return None
        key = frozenset(schemes)
        pattern = LspNormalizer._uri_scheme_re_cache.get(key)
        if pattern is None:
            alts = "|".join(re.escape(s) for s in schemes)
            pattern = re.compile(r'(?:%s)://[^\s)\]>]+' % alts)
            LspNormalizer._uri_scheme_re_cache[key] = pattern
        return pattern

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
        self._ready_time = None
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

    @property
    def needs_position_fallback(self):
        return True

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
        self._ready_time = None
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
        if self._ready_time:
            elapsed = self._ready_time - self._start_time
        elif self._start_time:
            elapsed = time.monotonic() - self._start_time
        else:
            elapsed = 0
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
            self._ready_time = time.monotonic()
            elapsed = self._ready_time - self._start_time
            logger.info("LSP server ready after %.1fs", elapsed)
            self._state = ServerState.READY
            if self._ready_callback:
                self._ready_callback()


class JdtlsNormalizer(LspNormalizer):
    """Normalizer for Eclipse jdtls-specific behavior.

    jdtls needs time after startup to import the project (Gradle/Maven)
    and build the workspace. During this warmup:

    - jdtls reports import/build progress via $/progress notifications
    - jdtls sends language/status with "ServiceReady" when fully ready
    - Queries before readiness may timeout or return empty results

    This normalizer:
    - Requires three conditions for readiness:
      1. language/status ServiceReady received
      2. A "Searching..." progress task has been seen
      3. No active progress tokens remain
    - Tracks $/progress notifications for status reporting
    - Classifies jdtls-specific transient errors for retry
    """

    _TRANSIENT_ERROR_FRAGMENTS = (
        "not yet ready",
        "server is not ready",
        "service is not ready",
        "still loading",
    )

    def __init__(self, warmup_timeout=300, max_retries=10, retry_delay=2.0):
        super().__init__()
        self._warmup_timeout = warmup_timeout
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._start_time = None
        self._ready_time = None
        self._active_progress_tokens = set()
        self._saw_service_ready = False
        self._saw_searching = False
        self._progress = {}
        self._completed_progress = []

    def on_started(self):
        self._start_time = time.monotonic()
        self._ready_time = None
        self._state = ServerState.INDEXING

    def on_stopped(self):
        self._state = ServerState.STOPPED

    def on_notification(self, method, params):
        if method == "$/progress" and params:
            self._handle_progress(params)
        elif method == "language/status" and params:
            msg_type = params.get("type", "")
            message = params.get("message", "")
            logger.info("jdtls status: type=%s message=%s",
                        msg_type, message)
            if msg_type == "ServiceReady":
                self._saw_service_ready = True

    def _handle_progress(self, params):
        token = params.get("token")
        value = params.get("value", {})
        kind = value.get("kind", "")

        if kind == "begin":
            self._active_progress_tokens.add(token)
            title = value.get("title", "")
            message = value.get("message", "")
            if "Searching" in title:
                self._saw_searching = True
            self._progress[token] = {
                "title": title,
                "message": message,
                "percentage": value.get("percentage"),
            }
            logger.info("jdtls progress begin [%s]: %s %s",
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
            logger.debug("jdtls progress [%s]: %s",
                         token, " ".join(msg_parts))
        elif kind == "end":
            self._active_progress_tokens.discard(token)
            entry = self._progress.pop(token, None)
            if entry:
                self._completed_progress.append({
                    "title": entry["title"],
                    "message": value.get("message", ""),
                })
            logger.info("jdtls progress end [%s]: %s",
                        token, value.get("message", ""))
            if (self._saw_service_ready
                    and self._saw_searching
                    and not self._active_progress_tokens):
                self._mark_ready()

    def on_no_progress_timeout(self):
        pass  # Only progress drain marks jdtls as ready

    def on_warmup_timeout(self):
        if self._state == ServerState.INDEXING:
            elapsed = time.monotonic() - self._start_time
            logger.warning("jdtls not ready after %ds "
                           "(ServiceReady=%s, Searching=%s, "
                           "active=%d), forcing READY",
                           int(elapsed),
                           self._saw_service_ready,
                           self._saw_searching,
                           len(self._active_progress_tokens))
            self._mark_ready()

    @property
    def warmup_timeout(self):
        return self._warmup_timeout

    def get_indexing_status(self):
        if self._ready_time:
            elapsed = self._ready_time - self._start_time
        elif self._start_time:
            elapsed = time.monotonic() - self._start_time
        else:
            elapsed = 0
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

    @property
    def uri_schemes(self):
        return ("jdt",)

    def normalize_uri(self, uri):
        if uri and uri.startswith("jdt://"):
            return _jdt_uri_to_jar_uri(uri)
        return uri

    def is_transient_error(self, error_msg):
        lower = error_msg.lower()
        return any(frag in lower
                   for frag in self._TRANSIENT_ERROR_FRAGMENTS)

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
            self._ready_time = time.monotonic()
            elapsed = self._ready_time - self._start_time
            logger.info("jdtls ready after %.1fs", elapsed)
            self._state = ServerState.READY
            if self._ready_callback:
                self._ready_callback()


def _jdt_uri_to_jar_uri(uri):
    """Convert a jdt:// URI to a jar:file:// URI.

    Example input:
      jdt://contents/yavi-0.9.1.jar/am.ik.yavi.core/...
        ?=project/%5C/path%5C/to%5C/yavi-0.9.1.jar=...
         /%3Cam.ik.yavi.core%28ConstraintViolations.class

    The query string encodes:
    - JAR path: backslash-escaped between project name and '=' delimiter
    - Class ref: %3C=package, %28=class file separator
    """
    parsed = urllib.parse.urlparse(uri)
    query = urllib.parse.unquote(parsed.query)

    # Extract JAR path: between first / and next =
    jar_path = None
    if query:
        first_slash = query.find("/")
        if first_slash >= 0:
            rest = query[first_slash + 1:]
            eq_pos = rest.find("=")
            if eq_pos >= 0:
                raw_path = rest[:eq_pos]
                jar_path = raw_path.replace("\\", "")

    # Extract class: last < separates package root, ( separates class name
    class_path = None
    if query:
        lt_pos = query.rfind("<")
        if lt_pos >= 0:
            class_ref = query[lt_pos + 1:]
            paren_pos = class_ref.find("(")
            if paren_pos >= 0:
                package = class_ref[:paren_pos].replace(".", "/")
                class_file = class_ref[paren_pos + 1:]
                class_path = package + "/" + class_file

    if jar_path and class_path:
        return "jar:file://" + jar_path + "!/" + class_path
    return uri


def create_normalizer(command, warmup_timeout=60):
    """Factory: pick the right normalizer based on the LSP server command."""
    if command and command[0].endswith("clangd"):
        return ClangdNormalizer(warmup_timeout=warmup_timeout)
    if command and command[0].endswith("jdtls"):
        return JdtlsNormalizer(warmup_timeout=max(warmup_timeout, 300))
    # Default: no quirks
    return LspNormalizer()
