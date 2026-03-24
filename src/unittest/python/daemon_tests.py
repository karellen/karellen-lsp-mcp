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

"""Unit tests for daemon parsing functions and protocol."""

import asyncio
import logging
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from karellen_lsp_mcp.daemon import (
    _FrontendSession,
    _parse_locations, _parse_hover, _parse_document_symbols,
    _parse_call_hierarchy, _parse_type_hierarchy, _parse_diagnostics,
    _uri_to_path, _read_message, _write_message, _env_int,
    _walk_call_tree, _walk_type_tree, _make_call_tree_node,
    _make_type_tree_node,
)


class UriToPathTest(unittest.TestCase):
    def test_file_uri(self):
        self.assertEqual(_uri_to_path("file:///home/user/test.c"), "/home/user/test.c")

    def test_none(self):
        self.assertEqual(_uri_to_path(None), "")

    def test_non_file_uri(self):
        self.assertEqual(_uri_to_path("https://example.com"), "https://example.com")


class ParseLocationsTest(unittest.TestCase):
    def test_none_result(self):
        result = _parse_locations(None)
        self.assertEqual(result["locations"], [])
        self.assertNotIn("indexing", result)

    def test_empty_list(self):
        result = _parse_locations([])
        self.assertEqual(result["locations"], [])

    def test_single_location(self):
        loc = {"uri": "file:///test.c", "range": {
            "start": {"line": 9, "character": 4},
            "end": {"line": 9, "character": 10},
        }}
        result = _parse_locations([loc])
        self.assertEqual(len(result["locations"]), 1)
        self.assertEqual(result["locations"][0]["file"], "/test.c")
        self.assertEqual(result["locations"][0]["line"], 10)
        self.assertEqual(result["locations"][0]["character"], 5)

    def test_multiple_locations(self):
        locs = [
            {"uri": "file:///a.c",
             "range": {"start": {"line": 0, "character": 0},
                       "end": {"line": 0, "character": 5}}},
            {"uri": "file:///b.c",
             "range": {"start": {"line": 4, "character": 2},
                       "end": {"line": 4, "character": 8}}},
        ]
        result = _parse_locations(locs)
        self.assertEqual(len(result["locations"]), 2)
        self.assertEqual(result["locations"][0]["file"], "/a.c")
        self.assertEqual(result["locations"][0]["line"], 1)
        self.assertEqual(result["locations"][0]["character"], 1)
        self.assertEqual(result["locations"][1]["file"], "/b.c")
        self.assertEqual(result["locations"][1]["line"], 5)
        self.assertEqual(result["locations"][1]["character"], 3)

    def test_location_link(self):
        link = {
            "targetUri": "file:///impl.c",
            "targetSelectionRange": {
                "start": {"line": 19, "character": 0},
                "end": {"line": 19, "character": 10},
            },
            "targetRange": {
                "start": {"line": 19, "character": 0},
                "end": {"line": 25, "character": 1},
            },
        }
        result = _parse_locations([link])
        self.assertEqual(len(result["locations"]), 1)
        self.assertEqual(result["locations"][0]["file"], "/impl.c")
        self.assertEqual(result["locations"][0]["line"], 20)
        self.assertEqual(result["locations"][0]["character"], 1)

    def test_dict_single_location(self):
        loc = {"uri": "file:///test.c", "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 5},
        }}
        result = _parse_locations(loc)
        self.assertEqual(len(result["locations"]), 1)
        self.assertEqual(result["locations"][0]["file"], "/test.c")

    def test_indexing_flag_when_true(self):
        result = _parse_locations([], indexing=True)
        self.assertTrue(result["indexing"])

    def test_no_indexing_flag_when_false(self):
        result = _parse_locations([], indexing=False)
        self.assertNotIn("indexing", result)


class ParseHoverTest(unittest.TestCase):
    def test_none(self):
        result = _parse_hover(None)
        self.assertIsNone(result["content"])

    def test_no_contents(self):
        result = _parse_hover({})
        self.assertIsNone(result["content"])

    def test_string_contents(self):
        result = _parse_hover({"contents": "hello"})
        self.assertEqual(result["content"], "hello")

    def test_markup_content_markdown(self):
        result = _parse_hover({"contents": {"kind": "markdown", "value": "**bold**"}})
        self.assertEqual(result["content"], "**bold**")
        self.assertEqual(result["language"], "markdown")

    def test_markup_content_plaintext(self):
        result = _parse_hover({"contents": {"kind": "plaintext", "value": "just text"}})
        self.assertEqual(result["content"], "just text")
        self.assertNotIn("language", result)

    def test_language_content(self):
        result = _parse_hover({"contents": {"language": "cpp", "value": "int x;"}})
        self.assertEqual(result["content"], "int x;")
        self.assertEqual(result["language"], "cpp")

    def test_list_contents(self):
        result = _parse_hover({"contents": [
            {"language": "c", "value": "int foo();"},
            "Documentation for foo",
        ]})
        self.assertIn("parts", result)
        self.assertEqual(len(result["parts"]), 2)
        self.assertEqual(result["parts"][0]["content"], "int foo();")
        self.assertEqual(result["parts"][0]["language"], "c")
        self.assertEqual(result["parts"][1]["content"], "Documentation for foo")


class ParseDocumentSymbolsTest(unittest.TestCase):
    def test_empty(self):
        result = _parse_document_symbols([])
        self.assertEqual(result["symbols"], [])

    def test_none(self):
        result = _parse_document_symbols(None)
        self.assertEqual(result["symbols"], [])

    def test_flat_symbols(self):
        symbols = [
            {"name": "main", "kind": 12,
             "range": {"start": {"line": 0, "character": 0},
                       "end": {"line": 5, "character": 1}}},
            {"name": "helper", "kind": 12,
             "range": {"start": {"line": 7, "character": 0},
                       "end": {"line": 10, "character": 1}}},
        ]
        result = _parse_document_symbols(symbols)
        self.assertEqual(len(result["symbols"]), 2)
        self.assertEqual(result["symbols"][0]["name"], "main")
        self.assertEqual(result["symbols"][0]["kind"], "Function")
        self.assertEqual(result["symbols"][0]["line"], 1)
        self.assertEqual(result["symbols"][1]["name"], "helper")
        self.assertEqual(result["symbols"][1]["line"], 8)

    def test_nested_symbols(self):
        symbols = [{
            "name": "MyClass", "kind": 5,
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 20, "character": 1}},
            "children": [
                {"name": "method1", "kind": 6,
                 "range": {"start": {"line": 2, "character": 4}, "end": {"line": 5, "character": 5}}},
                {"name": "method2", "kind": 6,
                 "range": {"start": {"line": 7, "character": 4}, "end": {"line": 10, "character": 5}}},
            ],
        }]
        result = _parse_document_symbols(symbols)
        self.assertEqual(len(result["symbols"]), 1)
        sym = result["symbols"][0]
        self.assertEqual(sym["name"], "MyClass")
        self.assertEqual(sym["kind"], "Class")
        self.assertEqual(len(sym["children"]), 2)
        self.assertEqual(sym["children"][0]["name"], "method1")
        self.assertEqual(sym["children"][0]["kind"], "Method")
        self.assertEqual(sym["children"][1]["name"], "method2")


class ParseCallHierarchyTest(unittest.TestCase):
    def test_empty_incoming(self):
        result = _parse_call_hierarchy([], "incoming")
        self.assertEqual(result["direction"], "incoming")
        self.assertEqual(result["items"], [])

    def test_empty_outgoing(self):
        result = _parse_call_hierarchy([], "outgoing")
        self.assertEqual(result["direction"], "outgoing")
        self.assertEqual(result["items"], [])

    def test_incoming_calls(self):
        calls = [
            {"from": {"name": "caller1", "kind": 12, "uri": "file:///a.c",
                      "selectionRange": {"start": {"line": 4, "character": 0},
                                         "end": {"line": 4, "character": 7}}},
             "fromRanges": [{"start": {"line": 6, "character": 4}}]},
            {"from": {"name": "caller2", "kind": 12, "uri": "file:///b.c",
                      "selectionRange": {"start": {"line": 9, "character": 0},
                                         "end": {"line": 9, "character": 7}}},
             "fromRanges": [{"start": {"line": 11, "character": 4}},
                            {"start": {"line": 15, "character": 4}}]},
        ]
        result = _parse_call_hierarchy(calls, "incoming")
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["name"], "caller1")
        self.assertEqual(result["items"][0]["kind"], "Function")
        self.assertEqual(result["items"][0]["file"], "/a.c")
        self.assertEqual(result["items"][0]["line"], 5)
        self.assertEqual(result["items"][0]["call_sites"], 1)
        self.assertEqual(result["items"][1]["name"], "caller2")
        self.assertEqual(result["items"][1]["call_sites"], 2)

    def test_outgoing_calls(self):
        calls = [
            {"to": {"name": "callee", "kind": 12, "uri": "file:///c.c",
                    "selectionRange": {"start": {"line": 0, "character": 0},
                                       "end": {"line": 0, "character": 6}}},
             "fromRanges": [{"start": {"line": 3, "character": 4}}]},
        ]
        result = _parse_call_hierarchy(calls, "outgoing")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["name"], "callee")
        self.assertEqual(result["items"][0]["file"], "/c.c")
        self.assertEqual(result["items"][0]["line"], 1)

    def test_indexing_flag(self):
        result = _parse_call_hierarchy([], "incoming", indexing=True)
        self.assertTrue(result["indexing"])

    def test_no_indexing_flag(self):
        result = _parse_call_hierarchy([], "incoming", indexing=False)
        self.assertNotIn("indexing", result)


class ParseTypeHierarchyTest(unittest.TestCase):
    def test_empty(self):
        result = _parse_type_hierarchy([], "supertypes")
        self.assertEqual(result["direction"], "supertypes")
        self.assertEqual(result["items"], [])

    def test_items(self):
        items = [
            {"name": "BaseClass", "kind": 5, "uri": "file:///base.h",
             "selectionRange": {"start": {"line": 0, "character": 0},
                                "end": {"line": 0, "character": 9}}},
            {"name": "Interface", "kind": 11, "uri": "file:///iface.h",
             "selectionRange": {"start": {"line": 5, "character": 0},
                                "end": {"line": 5, "character": 9}}},
        ]
        result = _parse_type_hierarchy(items, "supertypes")
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["name"], "BaseClass")
        self.assertEqual(result["items"][0]["kind"], "Class")
        self.assertEqual(result["items"][0]["file"], "/base.h")
        self.assertEqual(result["items"][0]["line"], 1)
        self.assertEqual(result["items"][1]["name"], "Interface")
        self.assertEqual(result["items"][1]["kind"], "Interface")

    def test_indexing_flag(self):
        result = _parse_type_hierarchy([], "subtypes", indexing=True)
        self.assertTrue(result["indexing"])


class ParseDiagnosticsTest(unittest.TestCase):
    def test_empty(self):
        result = _parse_diagnostics([])
        self.assertEqual(result["diagnostics"], [])

    def test_diagnostics(self):
        diags = [
            {"range": {"start": {"line": 4, "character": 10}, "end": {"line": 4, "character": 15}},
             "severity": 1, "message": "undeclared identifier", "source": "clang"},
            {"range": {"start": {"line": 8, "character": 0}, "end": {"line": 8, "character": 5}},
             "severity": 2, "message": "unused variable", "source": "clang"},
        ]
        result = _parse_diagnostics(diags)
        self.assertEqual(len(result["diagnostics"]), 2)
        d0 = result["diagnostics"][0]
        self.assertEqual(d0["line"], 5)
        self.assertEqual(d0["character"], 11)
        self.assertEqual(d0["severity"], "Error")
        self.assertEqual(d0["message"], "undeclared identifier")
        self.assertEqual(d0["source"], "clang")
        d1 = result["diagnostics"][1]
        self.assertEqual(d1["severity"], "Warning")

    def test_indexing_flag(self):
        result = _parse_diagnostics([], indexing=True)
        self.assertTrue(result["indexing"])


class ProtocolMessageTest(unittest.TestCase):
    def test_write_and_read_message(self):
        loop = asyncio.new_event_loop()

        async def run():
            reader_transport = asyncio.StreamReader()

            class FakeWriter:
                def write(self, data):
                    reader_transport.feed_data(data)

            writer = FakeWriter()

            msg = {"id": 1, "method": "test", "params": {"key": "value"}}
            _write_message(writer, msg)

            result = await _read_message(reader_transport)
            self.assertEqual(result["id"], 1)
            self.assertEqual(result["method"], "test")
            self.assertEqual(result["params"]["key"], "value")

        loop.run_until_complete(run())
        loop.close()

    def test_roundtrip_multiple_messages(self):
        loop = asyncio.new_event_loop()

        async def run():
            reader_transport = asyncio.StreamReader()

            class FakeWriter:
                def write(self, data):
                    reader_transport.feed_data(data)

            writer = FakeWriter()

            msgs = [
                {"id": 1, "method": "first", "params": {}},
                {"id": 2, "method": "second", "params": {"a": 42}},
            ]
            for m in msgs:
                _write_message(writer, m)

            result1 = await _read_message(reader_transport)
            result2 = await _read_message(reader_transport)
            self.assertEqual(result1["method"], "first")
            self.assertEqual(result2["method"], "second")
            self.assertEqual(result2["params"]["a"], 42)

        loop.run_until_complete(run())
        loop.close()


class FrontendSessionHandleRequestTest(unittest.TestCase):
    def test_handle_request_survives_connection_error(self):
        """_handle_request must not crash on ConnectionError from drain."""
        daemon = MagicMock()
        writer = MagicMock()
        writer.drain = AsyncMock(side_effect=ConnectionError("broken pipe"))
        reader = MagicMock()

        session = _FrontendSession(1, reader, writer, daemon)

        msg = {"id": 1, "method": "list_projects", "params": {}}

        async def run():
            with patch.object(session, "_dispatch",
                              return_value={"id": 1, "result": []}) as mock_dispatch:
                await session._handle_request(msg)
                mock_dispatch.assert_called_once_with(msg)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(run())
        loop.close()

    def test_handle_request_survives_runtime_error(self):
        """_handle_request must not crash on RuntimeError from closed StreamWriter."""
        daemon = MagicMock()
        writer = MagicMock()
        writer.drain = AsyncMock(
            side_effect=RuntimeError("unable to perform operation on closed transport"))
        reader = MagicMock()

        session = _FrontendSession(1, reader, writer, daemon)

        msg = {"id": 2, "method": "list_projects", "params": {}}

        async def run():
            with patch.object(session, "_dispatch",
                              return_value={"id": 2, "result": []}) as mock_dispatch:
                await session._handle_request(msg)
                mock_dispatch.assert_called_once_with(msg)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(run())
        loop.close()

    def test_handle_request_write_lock_serializes(self):
        """Concurrent _handle_request calls must serialize writes."""
        daemon = MagicMock()
        writer = MagicMock()
        writer.drain = AsyncMock()
        reader = MagicMock()

        session = _FrontendSession(1, reader, writer, daemon)

        write_order = []

        original_write = _write_message

        def tracking_write(w, msg):
            write_order.append(msg["id"])
            original_write(w, msg)

        async def run():
            with patch("karellen_lsp_mcp.daemon._write_message",
                       side_effect=tracking_write):
                with patch.object(session, "_dispatch",
                                  side_effect=[
                                      {"id": 1, "result": "first"},
                                      {"id": 2, "result": "second"},
                                  ]):
                    t1 = asyncio.create_task(session._handle_request(
                        {"id": 1, "method": "list_projects", "params": {}}))
                    t2 = asyncio.create_task(session._handle_request(
                        {"id": 2, "method": "list_projects", "params": {}}))
                    await asyncio.gather(t1, t2)

            self.assertEqual(len(write_order), 2)
            self.assertEqual(set(write_order), {1, 2})

        loop = asyncio.new_event_loop()
        loop.run_until_complete(run())
        loop.close()


class EnvIntTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._log_handler = logging.StreamHandler()
        cls._log_handler.setLevel(logging.DEBUG)
        logging.getLogger("karellen_lsp_mcp").addHandler(cls._log_handler)

    @classmethod
    def tearDownClass(cls):
        logging.getLogger("karellen_lsp_mcp").removeHandler(cls._log_handler)

    def test_env_int_returns_default(self):
        result = _env_int("LSP_MCP_TEST_NONEXISTENT_VAR_12345", 42)
        self.assertEqual(result, 42)

    def test_env_int_reads_env(self):
        os.environ["LSP_MCP_TEST_VAR_UNIT"] = "99"
        try:
            result = _env_int("LSP_MCP_TEST_VAR_UNIT", 42)
            self.assertEqual(result, 99)
        finally:
            del os.environ["LSP_MCP_TEST_VAR_UNIT"]

    def test_env_int_invalid_value_returns_default(self):
        os.environ["LSP_MCP_TEST_VAR_UNIT2"] = "not_a_number"
        try:
            result = _env_int("LSP_MCP_TEST_VAR_UNIT2", 42)
            self.assertEqual(result, 42)
        finally:
            del os.environ["LSP_MCP_TEST_VAR_UNIT2"]


def _test_sem():
    return asyncio.Semaphore(8)


def _lsp_item(name, kind, uri, line):
    """Helper to create a mock LSP CallHierarchyItem/TypeHierarchyItem."""
    return {
        "name": name, "kind": kind, "uri": uri,
        "selectionRange": {"start": {"line": line, "character": 0},
                           "end": {"line": line, "character": len(name)}},
        "range": {"start": {"line": line, "character": 0},
                  "end": {"line": line + 5, "character": 1}},
    }


class WalkCallTreeTest(unittest.TestCase):
    """Tests for recursive call hierarchy tree walking."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_single_level(self):
        """Root with two callers, no deeper levels."""
        client = MagicMock()
        caller_a = _lsp_item("caller_a", 12, "file:///a.c", 10)
        caller_b = _lsp_item("caller_b", 12, "file:///b.c", 20)
        client.incoming_calls = AsyncMock(side_effect=[
            [{"from": caller_a, "fromRanges": [{"start": {"line": 15}}]},
             {"from": caller_b, "fromRanges": [{"start": {"line": 25}},
                                               {"start": {"line": 30}}]}],
            [],  # caller_a has no callers
            [],  # caller_b has no callers
        ])

        root_item = _lsp_item("target", 12, "file:///t.c", 5)
        root = _make_call_tree_node(root_item)

        self._run(_walk_call_tree(
            client, root, root_item, "incoming", 5, set(), _test_sem()))

        self.assertEqual(len(root["children"]), 2)
        self.assertEqual(root["children"][0]["name"], "caller_a")
        self.assertEqual(root["children"][0]["call_sites"], 1)
        self.assertEqual(root["children"][1]["name"], "caller_b")
        self.assertEqual(root["children"][1]["call_sites"], 2)

    def test_multi_level(self):
        """Root -> caller_a -> main (3 levels)."""
        client = MagicMock()
        caller_a = _lsp_item("caller_a", 12, "file:///a.c", 10)
        main_item = _lsp_item("main", 12, "file:///main.c", 0)
        client.incoming_calls = AsyncMock(side_effect=[
            [{"from": caller_a, "fromRanges": [{"start": {"line": 15}}]}],
            [{"from": main_item, "fromRanges": [{"start": {"line": 5}}]}],
            [],  # main has no callers
        ])

        root_item = _lsp_item("target", 12, "file:///t.c", 5)
        root = _make_call_tree_node(root_item)

        self._run(_walk_call_tree(
            client, root, root_item, "incoming", 10, set(), _test_sem()))

        self.assertEqual(len(root["children"]), 1)
        self.assertEqual(root["children"][0]["name"], "caller_a")
        self.assertEqual(len(root["children"][0]["children"]), 1)
        self.assertEqual(
            root["children"][0]["children"][0]["name"], "main")

    def test_cycle_detection(self):
        """Recursive call: A -> B -> A must not loop."""
        client = MagicMock()
        item_a = _lsp_item("func_a", 12, "file:///a.c", 10)
        item_b = _lsp_item("func_b", 12, "file:///b.c", 20)
        # func_a calls func_b, func_b calls func_a (cycle)
        client.outgoing_calls = AsyncMock(side_effect=[
            [{"to": item_b, "fromRanges": [{"start": {"line": 15}}]}],
            [{"to": item_a, "fromRanges": [{"start": {"line": 25}}]}],
        ])

        root_item = _lsp_item("func_a", 12, "file:///a.c", 10)
        root = _make_call_tree_node(root_item)

        self._run(_walk_call_tree(
            client, root, root_item, "outgoing", 10, set(), _test_sem()))

        self.assertEqual(len(root["children"]), 1)
        self.assertEqual(root["children"][0]["name"], "func_b")
        # func_b has func_a as child in tree, but NOT expanded
        self.assertEqual(len(root["children"][0]["children"]), 1)
        b_child = root["children"][0]["children"][0]
        self.assertEqual(b_child["name"], "func_a")
        self.assertEqual(b_child["children"], [])

    def test_depth_limit(self):
        """Depth 0 should not expand."""
        client = MagicMock()
        client.incoming_calls = AsyncMock()

        root_item = _lsp_item("target", 12, "file:///t.c", 5)
        root = _make_call_tree_node(root_item)

        self._run(_walk_call_tree(
            client, root, root_item, "incoming", 0, set(), _test_sem()))

        client.incoming_calls.assert_not_called()
        self.assertEqual(root["children"], [])

    def test_depth_1_does_not_recurse(self):
        """Depth 1 fetches children but does not expand them."""
        client = MagicMock()
        caller = _lsp_item("caller", 12, "file:///c.c", 10)
        client.incoming_calls = AsyncMock(return_value=[
            {"from": caller, "fromRanges": [{"start": {"line": 5}}]},
        ])

        root_item = _lsp_item("target", 12, "file:///t.c", 5)
        root = _make_call_tree_node(root_item)

        self._run(_walk_call_tree(
            client, root, root_item, "incoming", 1, set(), _test_sem()))

        self.assertEqual(len(root["children"]), 1)
        self.assertEqual(root["children"][0]["name"], "caller")
        # Only 1 call: the root's incoming_calls
        client.incoming_calls.assert_called_once()

    def test_lsp_error_handled(self):
        """LSP errors should not crash the walker."""
        client = MagicMock()
        client.incoming_calls = AsyncMock(
            side_effect=Exception("server error"))

        root_item = _lsp_item("target", 12, "file:///t.c", 5)
        root = _make_call_tree_node(root_item)

        self._run(_walk_call_tree(
            client, root, root_item, "incoming", 5, set(), _test_sem()))

        self.assertEqual(root["children"], [])

    def test_outgoing_direction(self):
        """Outgoing call tree uses 'to' field."""
        client = MagicMock()
        callee = _lsp_item("callee", 12, "file:///c.c", 30)
        client.outgoing_calls = AsyncMock(side_effect=[
            [{"to": callee, "fromRanges": [{"start": {"line": 10}}]}],
            [],
        ])

        root_item = _lsp_item("target", 12, "file:///t.c", 5)
        root = _make_call_tree_node(root_item)

        self._run(_walk_call_tree(
            client, root, root_item, "outgoing", 5, set(), _test_sem()))

        self.assertEqual(len(root["children"]), 1)
        self.assertEqual(root["children"][0]["name"], "callee")


class WalkTypeTreeTest(unittest.TestCase):
    """Tests for recursive type hierarchy tree walking."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_supertypes_multi_level(self):
        """Child -> Parent -> GrandParent."""
        client = MagicMock()
        parent = _lsp_item("Parent", 5, "file:///parent.java", 10)
        grandparent = _lsp_item("GrandParent", 5,
                                "file:///gp.java", 5)
        client.supertypes = AsyncMock(side_effect=[
            [parent],
            [grandparent],
            [],
        ])

        root_item = _lsp_item("Child", 5, "file:///child.java", 20)
        root = _make_type_tree_node(root_item)

        self._run(_walk_type_tree(
            client, root, root_item, "supertypes", 10, set(), _test_sem()))

        self.assertEqual(len(root["children"]), 1)
        self.assertEqual(root["children"][0]["name"], "Parent")
        self.assertEqual(
            len(root["children"][0]["children"]), 1)
        self.assertEqual(
            root["children"][0]["children"][0]["name"], "GrandParent")

    def test_subtypes_diamond(self):
        """Interface with two implementations."""
        client = MagicMock()
        impl_a = _lsp_item("ImplA", 5, "file:///a.java", 10)
        impl_b = _lsp_item("ImplB", 5, "file:///b.java", 20)
        client.subtypes = AsyncMock(side_effect=[
            [impl_a, impl_b],
            [],  # ImplA has no subtypes
            [],  # ImplB has no subtypes
        ])

        root_item = _lsp_item("IFace", 11, "file:///iface.java", 5)
        root = _make_type_tree_node(root_item)

        self._run(_walk_type_tree(
            client, root, root_item, "subtypes", 10, set(), _test_sem()))

        self.assertEqual(len(root["children"]), 2)
        self.assertEqual(root["children"][0]["name"], "ImplA")
        self.assertEqual(root["children"][1]["name"], "ImplB")

    def test_cycle_detection(self):
        """Type cycle (shouldn't happen in practice) is handled."""
        client = MagicMock()
        item_a = _lsp_item("TypeA", 5, "file:///a.java", 10)
        item_b = _lsp_item("TypeB", 5, "file:///b.java", 20)
        client.supertypes = AsyncMock(side_effect=[
            [item_b],
            [item_a],
        ])

        root_item = _lsp_item("TypeA", 5, "file:///a.java", 10)
        root = _make_type_tree_node(root_item)

        self._run(_walk_type_tree(
            client, root, root_item, "supertypes", 10, set(), _test_sem()))

        self.assertEqual(len(root["children"]), 1)
        self.assertEqual(root["children"][0]["name"], "TypeB")
        # TypeB has TypeA as child but not expanded (cycle)
        self.assertEqual(len(root["children"][0]["children"]), 1)
        self.assertEqual(
            root["children"][0]["children"][0]["children"], [])

    def test_depth_limit(self):
        """Depth 0 should not expand."""
        client = MagicMock()
        client.supertypes = AsyncMock()

        root_item = _lsp_item("Child", 5, "file:///c.java", 20)
        root = _make_type_tree_node(root_item)

        self._run(_walk_type_tree(
            client, root, root_item, "supertypes", 0, set(), _test_sem()))

        client.supertypes.assert_not_called()

    def test_lsp_error_handled(self):
        """LSP errors should not crash the walker."""
        client = MagicMock()
        client.subtypes = AsyncMock(
            side_effect=Exception("server error"))

        root_item = _lsp_item("IFace", 11, "file:///i.java", 5)
        root = _make_type_tree_node(root_item)

        self._run(_walk_type_tree(
            client, root, root_item, "subtypes", 5, set(), _test_sem()))

        self.assertEqual(root["children"], [])


if __name__ == "__main__":
    unittest.main()
