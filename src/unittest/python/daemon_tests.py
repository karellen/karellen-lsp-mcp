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
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from karellen_lsp_mcp.daemon import (
    _FrontendSession,
    _parse_locations, _parse_hover, _parse_document_symbols,
    _parse_call_hierarchy, _parse_type_hierarchy, _parse_diagnostics,
    _uri_to_path, _read_message, _write_message, _env_int,
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


if __name__ == "__main__":
    unittest.main()
