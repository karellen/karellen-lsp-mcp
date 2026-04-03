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

"""Unit tests for LspNormalizer, ProgressNormalizer, ClangdNormalizer, and JdtlsNormalizer."""

import unittest

from karellen_lsp_mcp.lsp_client import LspClientError
from karellen_lsp_mcp.lsp_normalizer import (
    ServerState, LspNormalizer, ProgressNormalizer,
    ClangdNormalizer, JdtlsNormalizer,
    create_normalizer, _jdt_uri_to_jar_uri,
)


class CreateNormalizerTest(unittest.TestCase):
    def test_clangd_command(self):
        normalizer = create_normalizer(["clangd"])
        self.assertIsInstance(normalizer, ClangdNormalizer)

    def test_clangd_with_path(self):
        normalizer = create_normalizer(["/usr/bin/clangd", "--background-index"])
        self.assertIsInstance(normalizer, ClangdNormalizer)

    def test_unknown_command(self):
        normalizer = create_normalizer(["some-lsp-server"])
        self.assertIsInstance(normalizer, LspNormalizer)
        self.assertNotIsInstance(normalizer, ProgressNormalizer)

    def test_empty_command(self):
        normalizer = create_normalizer([])
        self.assertIsInstance(normalizer, LspNormalizer)
        self.assertNotIsInstance(normalizer, ProgressNormalizer)

    def test_none_command(self):
        normalizer = create_normalizer(None)
        self.assertIsInstance(normalizer, LspNormalizer)

    def test_pyright_langserver_command(self):
        normalizer = create_normalizer(["pyright-langserver", "--stdio"])
        self.assertIsInstance(normalizer, ProgressNormalizer)
        self.assertNotIsInstance(normalizer, ClangdNormalizer)

    def test_pyright_command(self):
        normalizer = create_normalizer(["pyright"])
        self.assertIsInstance(normalizer, ProgressNormalizer)

    def test_pyright_via_node_with_label(self):
        normalizer = create_normalizer(
            ["node", "/path/to/langserver.index.js", "--", "--stdio"],
            server_label="pyright")
        self.assertIsInstance(normalizer, ProgressNormalizer)
        self.assertNotIsInstance(normalizer, ClangdNormalizer)

    def test_rust_analyzer_command(self):
        normalizer = create_normalizer(["rust-analyzer"])
        self.assertIsInstance(normalizer, ProgressNormalizer)
        self.assertNotIsInstance(normalizer, ClangdNormalizer)

    def test_jdtls_command(self):
        normalizer = create_normalizer(["jdtls"])
        self.assertIsInstance(normalizer, JdtlsNormalizer)

    def test_jdtls_with_path(self):
        normalizer = create_normalizer(["/home/user/.local/bin/jdtls", "-data", "/tmp/ws"])
        self.assertIsInstance(normalizer, JdtlsNormalizer)

    def test_jdtls_warmup_minimum_300s(self):
        normalizer = create_normalizer(["jdtls"], warmup_timeout=30)
        self.assertGreaterEqual(normalizer.warmup_timeout, 300)


class LspNormalizerBaseTest(unittest.TestCase):
    def test_initial_state_starting(self):
        n = LspNormalizer()
        self.assertEqual(n.state, ServerState.STARTING)
        self.assertEqual(n.state_name, "starting")

    def test_on_started_transitions_to_ready(self):
        n = LspNormalizer()
        n.on_started()
        self.assertEqual(n.state, ServerState.READY)

    def test_on_stopped_transitions_to_stopped(self):
        n = LspNormalizer()
        n.on_started()
        n.on_stopped()
        self.assertEqual(n.state, ServerState.STOPPED)

    def test_is_transient_error_always_false(self):
        n = LspNormalizer()
        self.assertFalse(n.is_transient_error("not indexed"))
        self.assertFalse(n.is_transient_error("some error"))

    def test_normalize_error_passthrough(self):
        n = LspNormalizer()
        err = LspClientError("original")
        self.assertIs(n.normalize_error(err), err)

    def test_max_retries_is_one(self):
        n = LspNormalizer()
        self.assertEqual(n.max_retries, 1)

    def test_retry_delay_is_zero(self):
        n = LspNormalizer()
        self.assertEqual(n.retry_delay, 0)


class ProgressNormalizerStateTest(unittest.TestCase):
    def test_initial_state(self):
        n = ProgressNormalizer()
        self.assertEqual(n.state, ServerState.STARTING)

    def test_on_started_transitions_to_indexing(self):
        n = ProgressNormalizer()
        n.on_started()
        self.assertEqual(n.state, ServerState.INDEXING)

    def test_on_stopped(self):
        n = ProgressNormalizer()
        n.on_started()
        n.on_stopped()
        self.assertEqual(n.state, ServerState.STOPPED)


class ProgressNormalizerProgressTest(unittest.TestCase):
    def test_progress_begin_end_transitions_to_ready(self):
        n = ProgressNormalizer()
        ready_called = []
        n.set_ready_callback(lambda: ready_called.append(True))
        n.on_started()

        n.on_notification("$/progress", {
            "token": "indexing",
            "value": {"kind": "begin", "title": "Indexing"}
        })
        self.assertEqual(n.state, ServerState.INDEXING)

        n.on_notification("$/progress", {
            "token": "indexing",
            "value": {"kind": "end", "message": "done"}
        })
        self.assertEqual(n.state, ServerState.READY)
        self.assertEqual(len(ready_called), 1)

    def test_multiple_progress_tokens(self):
        n = ProgressNormalizer()
        n.on_started()

        n.on_notification("$/progress", {
            "token": "t1",
            "value": {"kind": "begin", "title": "Task 1"}
        })
        n.on_notification("$/progress", {
            "token": "t2",
            "value": {"kind": "begin", "title": "Task 2"}
        })
        n.on_notification("$/progress", {
            "token": "t1",
            "value": {"kind": "end", "message": "done"}
        })
        self.assertEqual(n.state, ServerState.INDEXING)
        n.on_notification("$/progress", {
            "token": "t2",
            "value": {"kind": "end", "message": "done"}
        })
        self.assertEqual(n.state, ServerState.READY)

    def test_no_progress_timeout_marks_ready(self):
        n = ProgressNormalizer()
        n.on_started()
        n.on_no_progress_timeout()
        self.assertEqual(n.state, ServerState.READY)

    def test_no_progress_timeout_noop_if_progress_seen(self):
        n = ProgressNormalizer()
        n.on_started()
        n.on_notification("$/progress", {
            "token": "t1",
            "value": {"kind": "begin", "title": "Indexing"}
        })
        n.on_no_progress_timeout()
        self.assertEqual(n.state, ServerState.INDEXING)

    def test_warmup_timeout_forces_ready(self):
        """ProgressNormalizer forces ready on warmup timeout even with active progress."""
        n = ProgressNormalizer()
        n.on_started()
        n.on_notification("$/progress", {
            "token": "t1",
            "value": {"kind": "begin", "title": "Indexing"}
        })
        n.on_warmup_timeout()
        self.assertEqual(n.state, ServerState.READY)

    def test_max_retries_during_indexing(self):
        n = ProgressNormalizer(max_retries=5)
        n.on_started()
        self.assertEqual(n.max_retries, 5)

    def test_max_retries_when_ready(self):
        n = ProgressNormalizer(max_retries=5)
        n.on_started()
        n.on_no_progress_timeout()
        self.assertEqual(n.max_retries, 1)

    def test_is_not_transient_error(self):
        n = ProgressNormalizer()
        self.assertFalse(n.is_transient_error("some error"))

    def test_clangd_inherits_progress_normalizer(self):
        n = ClangdNormalizer()
        self.assertIsInstance(n, ProgressNormalizer)


class ClangdNormalizerStateTest(unittest.TestCase):
    def test_initial_state(self):
        n = ClangdNormalizer()
        self.assertEqual(n.state, ServerState.STARTING)

    def test_on_started_transitions_to_indexing(self):
        n = ClangdNormalizer()
        n.on_started()
        self.assertEqual(n.state, ServerState.INDEXING)

    def test_on_stopped(self):
        n = ClangdNormalizer()
        n.on_started()
        n.on_stopped()
        self.assertEqual(n.state, ServerState.STOPPED)


class ClangdNormalizerProgressTest(unittest.TestCase):
    def test_progress_begin_end_transitions_to_ready(self):
        n = ClangdNormalizer()
        ready_called = []
        n.set_ready_callback(lambda: ready_called.append(True))
        n.on_started()

        n.on_notification("$/progress", {
            "token": "bg-index",
            "value": {"kind": "begin", "title": "Indexing"}
        })
        self.assertEqual(n.state, ServerState.INDEXING)
        self.assertTrue(n.saw_any_progress)

        n.on_notification("$/progress", {
            "token": "bg-index",
            "value": {"kind": "end", "message": "done"}
        })
        self.assertEqual(n.state, ServerState.READY)
        self.assertEqual(len(ready_called), 1)

    def test_multiple_progress_tokens(self):
        n = ClangdNormalizer()
        n.on_started()

        n.on_notification("$/progress", {
            "token": "token-1",
            "value": {"kind": "begin", "title": "Indexing"}
        })
        n.on_notification("$/progress", {
            "token": "token-2",
            "value": {"kind": "begin", "title": "Building preamble"}
        })

        # End first token — still indexing
        n.on_notification("$/progress", {
            "token": "token-1",
            "value": {"kind": "end", "message": "done"}
        })
        self.assertEqual(n.state, ServerState.INDEXING)

        # End second token — now ready
        n.on_notification("$/progress", {
            "token": "token-2",
            "value": {"kind": "end", "message": "done"}
        })
        self.assertEqual(n.state, ServerState.READY)

    def test_report_does_not_change_state(self):
        n = ClangdNormalizer()
        n.on_started()

        n.on_notification("$/progress", {
            "token": "bg-index",
            "value": {"kind": "begin", "title": "Indexing"}
        })
        n.on_notification("$/progress", {
            "token": "bg-index",
            "value": {"kind": "report", "message": "5 files", "percentage": 50}
        })
        self.assertEqual(n.state, ServerState.INDEXING)

    def test_ignores_non_progress_notifications(self):
        n = ClangdNormalizer()
        n.on_started()
        n.on_notification("textDocument/publishDiagnostics", {"uri": "file:///a.c"})
        self.assertEqual(n.state, ServerState.INDEXING)
        self.assertFalse(n.saw_any_progress)

    def test_ignores_empty_params(self):
        n = ClangdNormalizer()
        n.on_started()
        n.on_notification("$/progress", None)
        n.on_notification("$/progress", {})
        self.assertFalse(n.saw_any_progress)

    def test_no_progress_timeout(self):
        n = ClangdNormalizer()
        n.on_started()
        n.on_no_progress_timeout()
        self.assertEqual(n.state, ServerState.READY)

    def test_no_progress_timeout_noop_if_progress_seen(self):
        n = ClangdNormalizer()
        n.on_started()
        n.on_notification("$/progress", {
            "token": "t1",
            "value": {"kind": "begin", "title": "Indexing"}
        })
        n.on_no_progress_timeout()
        # Should still be INDEXING because progress was seen
        self.assertEqual(n.state, ServerState.INDEXING)

    def test_warmup_timeout_skips_when_progress_active(self):
        n = ClangdNormalizer()
        n.on_started()
        n.on_notification("$/progress", {
            "token": "t1",
            "value": {"kind": "begin", "title": "Indexing"}
        })
        n.on_warmup_timeout()
        # Active progress tokens → don't force READY
        self.assertEqual(n.state, ServerState.INDEXING)

    def test_warmup_timeout_forces_ready_no_active_progress(self):
        n = ClangdNormalizer()
        n.on_started()
        n.on_notification("$/progress", {
            "token": "t1",
            "value": {"kind": "begin", "title": "Indexing"}
        })
        # End the progress so no active tokens remain
        n.on_notification("$/progress", {
            "token": "t1",
            "value": {"kind": "end", "message": "done"}
        })
        # Reset state back to INDEXING to simulate a second indexing phase
        # with no active tokens (e.g. after completion of first wave)
        n._state = ServerState.INDEXING
        n.on_warmup_timeout()
        self.assertEqual(n.state, ServerState.READY)


class ClangdNormalizerTransientErrorTest(unittest.TestCase):
    def test_not_indexed_is_transient(self):
        n = ClangdNormalizer()
        self.assertTrue(n.is_transient_error("File not indexed yet"))

    def test_not_ready_is_transient(self):
        n = ClangdNormalizer()
        self.assertTrue(n.is_transient_error("Server not ready"))

    def test_building_preamble_is_transient(self):
        n = ClangdNormalizer()
        self.assertTrue(n.is_transient_error("Building preamble for file.cpp"))

    def test_background_indexing_is_transient(self):
        n = ClangdNormalizer()
        self.assertTrue(n.is_transient_error("background indexing in progress"))

    def test_compilation_database_is_transient(self):
        n = ClangdNormalizer()
        self.assertTrue(n.is_transient_error("file not found in compilation database"))

    def test_random_error_not_transient(self):
        n = ClangdNormalizer()
        self.assertFalse(n.is_transient_error("Internal error"))
        self.assertFalse(n.is_transient_error("Method not found"))


class ClangdNormalizerRetryPolicyTest(unittest.TestCase):
    def test_max_retries_during_indexing(self):
        n = ClangdNormalizer(max_retries=7)
        n.on_started()
        self.assertEqual(n.max_retries, 7)

    def test_max_retries_when_ready(self):
        n = ClangdNormalizer(max_retries=7)
        n.on_started()
        n.on_no_progress_timeout()  # force to READY
        self.assertEqual(n.max_retries, 1)

    def test_retry_delay(self):
        n = ClangdNormalizer(retry_delay=2.5)
        self.assertEqual(n.retry_delay, 2.5)


class ClangdNormalizerErrorNormalizationTest(unittest.TestCase):
    def test_method_not_found(self):
        n = ClangdNormalizer()
        err = n.normalize_error(LspClientError("LSP error -32601: method not found"))
        self.assertIn("does not support", str(err))

    def test_not_initialized(self):
        n = ClangdNormalizer()
        err = n.normalize_error(LspClientError("LSP error -32002: not initialized"))
        self.assertIn("still initializing", str(err))

    def test_internal_error(self):
        n = ClangdNormalizer()
        err = n.normalize_error(LspClientError("LSP error -32603: internal"))
        self.assertIn("internal error", str(err))

    def test_request_cancelled(self):
        n = ClangdNormalizer()
        err = n.normalize_error(LspClientError("LSP error -32800: request cancelled"))
        self.assertIn("cancelled", str(err))

    def test_content_modified(self):
        n = ClangdNormalizer()
        err = n.normalize_error(LspClientError("LSP error -32801: content modified"))
        self.assertIn("content changed", str(err))

    def test_timeout(self):
        n = ClangdNormalizer()
        err = n.normalize_error(LspClientError("Timeout waiting for response"))
        self.assertIn("did not respond in time", str(err))

    def test_unknown_error_passthrough(self):
        n = ClangdNormalizer()
        orig = LspClientError("something unexpected")
        err = n.normalize_error(orig)
        self.assertIs(err, orig)


class ClangdNormalizerVersionDetectionTest(unittest.TestCase):
    def test_parses_major_version_from_server_info(self):
        n = ClangdNormalizer()
        n.on_server_info({"name": "clangd", "version": "18.1.3"})
        self.assertEqual(n._major_version, 18)
        self.assertEqual(n.server_version, "18.1.3")

    def test_parses_version_with_prefix(self):
        n = ClangdNormalizer()
        n.on_server_info({"name": "clangd", "version": "22.1.0-rc1"})
        self.assertEqual(n._major_version, 22)

    def test_no_server_info(self):
        n = ClangdNormalizer()
        n.on_server_info(None)
        self.assertIsNone(n._major_version)
        self.assertIsNone(n.server_version)

    def test_no_version_in_server_info(self):
        n = ClangdNormalizer()
        n.on_server_info({"name": "clangd"})
        self.assertIsNone(n._major_version)


class ClangdNormalizerFeatureSupportTest(unittest.TestCase):
    def test_outgoing_calls_unsupported_on_v18(self):
        n = ClangdNormalizer()
        n.on_server_info({"name": "clangd", "version": "18.1.3"})
        self.assertFalse(n.supports_method("callHierarchy/outgoingCalls"))

    def test_outgoing_calls_unsupported_on_v19(self):
        n = ClangdNormalizer()
        n.on_server_info({"name": "clangd", "version": "19.1.0"})
        self.assertFalse(n.supports_method("callHierarchy/outgoingCalls"))

    def test_outgoing_calls_supported_on_v20(self):
        n = ClangdNormalizer()
        n.on_server_info({"name": "clangd", "version": "20.1.0"})
        self.assertTrue(n.supports_method("callHierarchy/outgoingCalls"))

    def test_outgoing_calls_supported_on_v22(self):
        n = ClangdNormalizer()
        n.on_server_info({"name": "clangd", "version": "22.1.0-rc1"})
        self.assertTrue(n.supports_method("callHierarchy/outgoingCalls"))

    def test_incoming_calls_supported_on_all_versions(self):
        n = ClangdNormalizer()
        n.on_server_info({"name": "clangd", "version": "18.1.3"})
        self.assertTrue(n.supports_method("callHierarchy/incomingCalls"))

    def test_type_hierarchy_supported_on_all_versions(self):
        n = ClangdNormalizer()
        n.on_server_info({"name": "clangd", "version": "18.1.3"})
        self.assertTrue(n.supports_method("typeHierarchy/supertypes"))
        self.assertTrue(n.supports_method("typeHierarchy/subtypes"))

    def test_unknown_version_assumes_supported(self):
        n = ClangdNormalizer()
        # No server info — unknown version
        self.assertTrue(n.supports_method("callHierarchy/outgoingCalls"))

    def test_unlisted_method_always_supported(self):
        n = ClangdNormalizer()
        n.on_server_info({"name": "clangd", "version": "18.1.3"})
        self.assertTrue(n.supports_method("textDocument/definition"))
        self.assertTrue(n.supports_method("textDocument/hover"))


class LspNormalizerBaseFeatureSupportTest(unittest.TestCase):
    def test_base_normalizer_supports_all_methods(self):
        n = LspNormalizer()
        self.assertTrue(n.supports_method("callHierarchy/outgoingCalls"))
        self.assertTrue(n.supports_method("textDocument/definition"))

    def test_base_normalizer_stores_server_version(self):
        n = LspNormalizer()
        n.on_server_info({"name": "test-server", "version": "1.2.3"})
        self.assertEqual(n.server_version, "1.2.3")


class JdtlsNormalizerStateTest(unittest.TestCase):
    def test_initial_state(self):
        n = JdtlsNormalizer()
        self.assertEqual(n.state, ServerState.STARTING)

    def test_on_started_transitions_to_indexing(self):
        n = JdtlsNormalizer()
        n.on_started()
        self.assertEqual(n.state, ServerState.INDEXING)

    def test_on_stopped(self):
        n = JdtlsNormalizer()
        n.on_started()
        n.on_stopped()
        self.assertEqual(n.state, ServerState.STOPPED)


class JdtlsNormalizerProgressTest(unittest.TestCase):
    def setUp(self):
        self.n = JdtlsNormalizer()
        self.n.on_started()
        self.ready_called = False
        self.n.set_ready_callback(lambda: setattr(self, "ready_called", True))

    def test_progress_end_without_service_ready_not_ready(self):
        self.n.on_notification("$/progress", {
            "token": "search-1",
            "value": {"kind": "begin", "title": "Searching"},
        })
        self.n.on_notification("$/progress", {
            "token": "search-1",
            "value": {"kind": "end"},
        })
        self.assertEqual(self.n.state, ServerState.INDEXING)

    def test_service_ready_without_searching_not_ready(self):
        self.n.on_notification("language/status", {
            "type": "ServiceReady", "message": "Ready"})
        self.assertEqual(self.n.state, ServerState.INDEXING)

    def test_service_ready_and_non_searching_progress_not_ready(self):
        self.n.on_notification("language/status", {
            "type": "ServiceReady", "message": "Ready"})
        self.n.on_notification("$/progress", {
            "token": "build-1",
            "value": {"kind": "begin", "title": "Building"},
        })
        self.n.on_notification("$/progress", {
            "token": "build-1",
            "value": {"kind": "end"},
        })
        self.assertEqual(self.n.state, ServerState.INDEXING)

    def test_full_cold_start_sequence(self):
        # Import
        self.n.on_notification("$/progress", {
            "token": "import-1",
            "value": {"kind": "begin", "title": "Initialize Workspace"},
        })
        self.n.on_notification("$/progress", {
            "token": "import-1",
            "value": {"kind": "end"},
        })

        # ServiceReady (after import, before Building/Searching)
        self.n.on_notification("language/status", {
            "type": "ServiceReady", "message": "ServiceReady"})
        self.assertEqual(self.n.state, ServerState.INDEXING)

        # Building — ServiceReady seen but no Searching yet
        self.n.on_notification("$/progress", {
            "token": "build-1",
            "value": {"kind": "begin", "title": "Building"},
        })
        self.n.on_notification("$/progress", {
            "token": "build-1",
            "value": {"kind": "end"},
        })
        self.assertEqual(self.n.state, ServerState.INDEXING)

        # Searching — all three conditions met on end
        self.n.on_notification("$/progress", {
            "token": "search-1",
            "value": {"kind": "begin", "title": "Searching"},
        })
        self.assertEqual(self.n.state, ServerState.INDEXING)
        self.n.on_notification("$/progress", {
            "token": "search-1",
            "value": {"kind": "end"},
        })
        self.assertEqual(self.n.state, ServerState.READY)
        self.assertTrue(self.ready_called)

    def test_warm_start_sequence(self):
        # ServiceReady
        self.n.on_notification("language/status", {
            "type": "ServiceReady", "message": "ServiceReady"})

        # Instant Building
        self.n.on_notification("$/progress", {
            "token": "build-1",
            "value": {"kind": "begin", "title": "Building"},
        })
        self.n.on_notification("$/progress", {
            "token": "build-1",
            "value": {"kind": "end"},
        })
        self.assertEqual(self.n.state, ServerState.INDEXING)

        # Short Searching (3s on warm)
        self.n.on_notification("$/progress", {
            "token": "search-1",
            "value": {"kind": "begin", "title": "Searching"},
        })
        self.n.on_notification("$/progress", {
            "token": "search-1",
            "value": {"kind": "end"},
        })
        self.assertEqual(self.n.state, ServerState.READY)
        self.assertTrue(self.ready_called)

    def test_non_service_ready_status_ignored(self):
        self.n.on_notification("language/status", {
            "type": "Started", "message": "Starting"})
        self.assertEqual(self.n.state, ServerState.INDEXING)

    def test_progress_report_updates_percentage(self):
        self.n.on_notification("$/progress", {
            "token": "import-1",
            "value": {"kind": "begin", "title": "Importing",
                      "percentage": 0},
        })
        self.n.on_notification("$/progress", {
            "token": "import-1",
            "value": {"kind": "report", "percentage": 50},
        })
        status = self.n.get_indexing_status()
        self.assertEqual(status["active_tasks"][0]["percentage"], 50)

    def test_no_progress_timeout_does_not_mark_ready(self):
        self.n.on_no_progress_timeout()
        self.assertEqual(self.n.state, ServerState.INDEXING)

    def test_warmup_timeout_forces_ready_without_service_ready(self):
        self.n.on_warmup_timeout()
        self.assertEqual(self.n.state, ServerState.READY)

    def test_warmup_timeout_ignored_after_ready(self):
        self.n.on_notification("language/status", {
            "type": "ServiceReady", "message": "Ready"})
        self.n.on_notification("$/progress", {
            "token": "s1", "value": {"kind": "begin", "title": "Searching"}})
        self.n.on_notification("$/progress", {
            "token": "s1", "value": {"kind": "end"}})
        self.assertEqual(self.n.state, ServerState.READY)
        self.n.on_warmup_timeout()

    def test_indexing_status_tracks_tasks(self):
        self.n.on_notification("$/progress", {
            "token": "import-1",
            "value": {"kind": "begin", "title": "Importing Gradle project",
                      "message": "subproject :app"},
        })
        status = self.n.get_indexing_status()
        self.assertEqual(status["state"], "indexing")
        self.assertEqual(len(status["active_tasks"]), 1)
        self.assertEqual(status["active_tasks"][0]["title"],
                         "Importing Gradle project")
        self.assertEqual(status["active_tasks"][0]["message"],
                         "subproject :app")

        self.n.on_notification("$/progress", {
            "token": "import-1",
            "value": {"kind": "end"},
        })
        status = self.n.get_indexing_status()
        self.assertEqual(status["completed_tasks"], 1)


class JdtlsNormalizerTransientErrorTest(unittest.TestCase):
    def test_not_yet_ready_is_transient(self):
        n = JdtlsNormalizer()
        self.assertTrue(n.is_transient_error("Server not yet ready"))

    def test_service_not_ready_is_transient(self):
        n = JdtlsNormalizer()
        self.assertTrue(n.is_transient_error("Service is not ready"))

    def test_random_error_not_transient(self):
        n = JdtlsNormalizer()
        self.assertFalse(n.is_transient_error("NullPointerException"))


class JdtlsNormalizerRetryPolicyTest(unittest.TestCase):
    def test_max_retries_during_indexing(self):
        n = JdtlsNormalizer()
        n.on_started()
        self.assertEqual(n.max_retries, 10)

    def test_max_retries_when_ready(self):
        n = JdtlsNormalizer()
        n.on_started()
        n.on_notification("language/status", {
            "type": "ServiceReady", "message": "Ready"})
        n.on_notification("$/progress", {
            "token": "s1", "value": {"kind": "begin", "title": "Searching"}})
        n.on_notification("$/progress", {
            "token": "s1", "value": {"kind": "end"}})
        self.assertEqual(n.max_retries, 1)

    def test_retry_delay(self):
        n = JdtlsNormalizer()
        self.assertEqual(n.retry_delay, 2.0)


class JdtUriToJarUriTest(unittest.TestCase):
    def test_converts_full_jdt_uri(self):
        jdt_uri = (
            "jdt://contents/yavi-0.9.1.jar/am.ik.yavi.core"
            "/ConstraintViolations.java"
            "?=tourlandish.aries"
            "/%5C/home%5C/user%5C/.gradle%5C/caches%5C/modules-2"
            "%5C/files-2.1%5C/am.ik.yavi%5C/yavi%5C/0.9.1"
            "%5C/e90c540787468b5dbbf09bdf7e48b4585a8a8f38"
            "%5C/yavi-0.9.1.jar"
            "=/gradle_used_by_scope=/main,test=/"
            "=/org.eclipse.jst.component.nondependency=/=/"
            "%3Cam.ik.yavi.core%28ConstraintViolations.class"
        )
        result = _jdt_uri_to_jar_uri(jdt_uri)
        self.assertEqual(
            result,
            "jar:file:///home/user/.gradle/caches/modules-2"
            "/files-2.1/am.ik.yavi/yavi/0.9.1"
            "/e90c540787468b5dbbf09bdf7e48b4585a8a8f38"
            "/yavi-0.9.1.jar"
            "!/am/ik/yavi/core/ConstraintViolations.class"
        )

    def test_converts_nested_package(self):
        jdt_uri = (
            "jdt://contents/lib.jar/com.example/Foo.java"
            "?=proj/%5C/opt%5C/lib.jar"
            "=/%3Ccom.example.sub%28Bar.class"
        )
        result = _jdt_uri_to_jar_uri(jdt_uri)
        self.assertEqual(
            result,
            "jar:file:///opt/lib.jar!/com/example/sub/Bar.class"
        )

    def test_returns_uri_unchanged_when_no_query(self):
        uri = "jdt://contents/lib.jar/com.example/Foo.java"
        result = _jdt_uri_to_jar_uri(uri)
        self.assertEqual(result, uri)

    def test_normalize_uri_delegates_for_jdt(self):
        n = JdtlsNormalizer()
        jdt_uri = (
            "jdt://contents/lib.jar/com.example/Foo.java"
            "?=proj/%5C/opt%5C/lib.jar"
            "=/%3Ccom.example%28Foo.class"
        )
        result = n.normalize_uri(jdt_uri)
        self.assertEqual(
            result,
            "jar:file:///opt/lib.jar!/com/example/Foo.class"
        )

    def test_normalize_uri_passthrough_for_file(self):
        n = JdtlsNormalizer()
        uri = "file:///home/user/project/Foo.java"
        self.assertEqual(n.normalize_uri(uri), uri)

    def test_base_normalize_uri_passthrough(self):
        n = LspNormalizer()
        uri = "jdt://contents/lib.jar"
        self.assertEqual(n.normalize_uri(uri), uri)


if __name__ == "__main__":
    unittest.main()
