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

"""Unit tests for LspNormalizer and ClangdNormalizer."""

import unittest

from karellen_lsp_mcp.lsp_client import LspClientError
from karellen_lsp_mcp.lsp_normalizer import (
    ServerState, LspNormalizer, ClangdNormalizer, create_normalizer,
)


class CreateNormalizerTest(unittest.TestCase):
    def test_clangd_command(self):
        normalizer = create_normalizer(["clangd"])
        self.assertIsInstance(normalizer, ClangdNormalizer)

    def test_clangd_with_path(self):
        normalizer = create_normalizer(["/usr/bin/clangd", "--background-index"])
        self.assertIsInstance(normalizer, ClangdNormalizer)

    def test_unknown_command(self):
        normalizer = create_normalizer(["rust-analyzer"])
        self.assertIsInstance(normalizer, LspNormalizer)
        self.assertNotIsInstance(normalizer, ClangdNormalizer)

    def test_empty_command(self):
        normalizer = create_normalizer([])
        self.assertIsInstance(normalizer, LspNormalizer)

    def test_none_command(self):
        normalizer = create_normalizer(None)
        self.assertIsInstance(normalizer, LspNormalizer)


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


if __name__ == "__main__":
    unittest.main()
