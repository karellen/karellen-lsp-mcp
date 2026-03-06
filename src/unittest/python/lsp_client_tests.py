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

"""Unit tests for LspClient."""

import asyncio
import json
import unittest
from unittest.mock import MagicMock, patch

from karellen_lsp_mcp.lsp_client import LspClient, LspClientError


class _FakeStdin:
    def __init__(self):
        self.written = bytearray()

    def write(self, data):
        self.written.extend(data)

    async def drain(self):
        pass


class LspClientTextDocumentPositionTest(unittest.TestCase):
    def test_text_document_position(self):
        client = LspClient()
        result = client._text_document_position("file:///foo.c", 10, 5)
        self.assertEqual(result, {
            "textDocument": {"uri": "file:///foo.c"},
            "position": {"line": 10, "character": 5},
        })


class LspClientMessageIdTest(unittest.TestCase):
    def test_next_id_increments(self):
        client = LspClient()
        id1 = client._next_id()
        id2 = client._next_id()
        self.assertEqual(id1, 1)
        self.assertEqual(id2, 2)


class LspClientDispatchTest(unittest.TestCase):
    def test_dispatch_response_resolves_future(self):
        client = LspClient()
        loop = asyncio.new_event_loop()
        fut = loop.create_future()
        client._pending[42] = fut

        loop.run_until_complete(
            client._dispatch_message({"id": 42, "result": {"capabilities": {}}}))

        self.assertTrue(fut.done())
        self.assertEqual(fut.result(), {"capabilities": {}})
        loop.close()

    def test_dispatch_error_response(self):
        client = LspClient()
        loop = asyncio.new_event_loop()
        fut = loop.create_future()
        client._pending[7] = fut

        loop.run_until_complete(
            client._dispatch_message({
                "id": 7,
                "error": {"code": -32600, "message": "Invalid Request"}
            }))

        self.assertTrue(fut.done())
        with self.assertRaises(LspClientError) as ctx:
            fut.result()
        self.assertIn("-32600", str(ctx.exception))
        self.assertIn("Invalid Request", str(ctx.exception))
        loop.close()

    def test_dispatch_notification_diagnostics(self):
        client = LspClient()
        diag1 = {"range": {"start": {"line": 0, "character": 0},
                           "end": {"line": 0, "character": 5}},
                 "severity": 1, "message": "error1"}
        diag2 = {"range": {"start": {"line": 2, "character": 0},
                           "end": {"line": 2, "character": 3}},
                 "severity": 2, "message": "warning1"}
        loop = asyncio.new_event_loop()
        loop.run_until_complete(
            client._dispatch_message({
                "method": "textDocument/publishDiagnostics",
                "params": {
                    "uri": "file:///test.c",
                    "diagnostics": [diag1, diag2],
                }
            }))

        self.assertEqual(len(client.get_diagnostics("file:///test.c")), 2)
        self.assertEqual(client.get_diagnostics("file:///other.c"), [])
        loop.close()


class LspClientEnsureFileOpenTest(unittest.TestCase):
    def test_ensure_file_open_reads_and_sends(self):
        client = LspClient()
        client._process = MagicMock()
        client._process.stdin = _FakeStdin()

        loop = asyncio.new_event_loop()
        with patch("karellen_lsp_mcp.lsp_client.Path") as mock_path_cls:
            mock_path_inst = MagicMock()
            mock_path_cls.return_value = mock_path_inst
            mock_path_inst.read_text.return_value = "int main() {}"
            mock_path_inst.suffix = ".c"

            loop.run_until_complete(client.ensure_file_open("file:///test.c"))

        self.assertIn("file:///test.c", client._open_files)

        # Second call should be no-op
        written_before = len(client._process.stdin.written)
        loop.run_until_complete(client.ensure_file_open("file:///test.c"))
        self.assertEqual(len(client._process.stdin.written), written_before)
        loop.close()

    def test_ensure_file_open_nonexistent_file(self):
        client = LspClient()
        client._process = MagicMock()
        client._process.stdin = _FakeStdin()

        loop = asyncio.new_event_loop()
        with patch("karellen_lsp_mcp.lsp_client.Path") as mock_path_cls:
            mock_path_inst = MagicMock()
            mock_path_cls.return_value = mock_path_inst
            mock_path_inst.read_text.side_effect = FileNotFoundError("not found")
            mock_path_inst.suffix = ".c"

            with self.assertRaises(LspClientError):
                loop.run_until_complete(client.ensure_file_open("file:///missing.c"))
        loop.close()


class LspClientEnsureFileOpenConcurrencyTest(unittest.TestCase):
    def test_concurrent_ensure_file_open_sends_didopen_once(self):
        """Two concurrent ensure_file_open calls must only send didOpen once."""
        client = LspClient()
        client._process = MagicMock()
        client._process.stdin = _FakeStdin()

        notification_count = 0
        original_send = client._send_notification

        async def counting_send(method, params):
            nonlocal notification_count
            if method == "textDocument/didOpen":
                notification_count += 1
            return await original_send(method, params)

        client._send_notification = counting_send

        async def run():
            with patch("karellen_lsp_mcp.lsp_client.Path") as mock_path_cls:
                mock_path_inst = MagicMock()
                mock_path_cls.return_value = mock_path_inst
                mock_path_inst.read_text.return_value = "int main() {}"
                mock_path_inst.suffix = ".c"

                t1 = asyncio.create_task(client.ensure_file_open("file:///concurrent.c"))
                t2 = asyncio.create_task(client.ensure_file_open("file:///concurrent.c"))
                await asyncio.gather(t1, t2)

            self.assertEqual(notification_count, 1)
            self.assertIn("file:///concurrent.c", client._open_files)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(run())
        loop.close()

    def test_concurrent_ensure_file_open_different_files(self):
        """Concurrent ensure_file_open for different files both send didOpen."""
        client = LspClient()
        client._process = MagicMock()
        client._process.stdin = _FakeStdin()

        async def run():
            with patch("karellen_lsp_mcp.lsp_client.Path") as mock_path_cls:
                mock_path_inst = MagicMock()
                mock_path_cls.return_value = mock_path_inst
                mock_path_inst.read_text.return_value = "int main() {}"
                mock_path_inst.suffix = ".c"

                t1 = asyncio.create_task(client.ensure_file_open("file:///file_a.c"))
                t2 = asyncio.create_task(client.ensure_file_open("file:///file_b.c"))
                await asyncio.gather(t1, t2)

            self.assertIn("file:///file_a.c", client._open_files)
            self.assertIn("file:///file_b.c", client._open_files)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(run())
        loop.close()


class LspClientWriteMessageTest(unittest.TestCase):
    def test_write_message_format(self):
        client = LspClient()
        client._process = MagicMock()
        client._process.stdin = _FakeStdin()

        msg = {"jsonrpc": "2.0", "method": "initialized", "params": {}}
        loop = asyncio.new_event_loop()
        loop.run_until_complete(client._write_message(msg))
        loop.close()

        written = bytes(client._process.stdin.written)
        self.assertIn(b"Content-Length:", written)
        # Split header and body
        header_end = written.index(b"\r\n\r\n") + 4
        body = written[header_end:]
        parsed = json.loads(body)
        self.assertEqual(parsed["method"], "initialized")


class LspClientServerRequestTest(unittest.TestCase):
    def test_respond_to_workspace_configuration(self):
        client = LspClient()
        client._process = MagicMock()
        client._process.stdin = _FakeStdin()

        loop = asyncio.new_event_loop()
        loop.run_until_complete(
            client._respond_to_server_request(
                99, "workspace/configuration",
                {"items": [{"section": "clangd"}, {"section": "editor"}]}
            ))
        loop.close()

        written = bytes(client._process.stdin.written)
        header_end = written.index(b"\r\n\r\n") + 4
        body = json.loads(written[header_end:])
        self.assertEqual(body["id"], 99)
        self.assertEqual(body["result"], [None, None])

    def test_respond_to_register_capability(self):
        client = LspClient()
        client._process = MagicMock()
        client._process.stdin = _FakeStdin()

        loop = asyncio.new_event_loop()
        loop.run_until_complete(
            client._respond_to_server_request(5, "client/registerCapability", {}))
        loop.close()

        written = bytes(client._process.stdin.written)
        header_end = written.index(b"\r\n\r\n") + 4
        body = json.loads(written[header_end:])
        self.assertEqual(body["id"], 5)
        self.assertIsNone(body["result"])


class LspClientSendRequestNoProcessTest(unittest.TestCase):
    def test_send_request_without_process(self):
        client = LspClient()
        loop = asyncio.new_event_loop()
        with self.assertRaises(LspClientError):
            loop.run_until_complete(client._send_request("test", {}))
        loop.close()


if __name__ == "__main__":
    unittest.main()
