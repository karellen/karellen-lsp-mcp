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

"""Tests that drive the server through a real MCP client rather than calling tools directly.

Neither test calls a tool, so neither connects to (or spawns) the LSP daemon.
"""

import os
import sys
import unittest

from mcp import Client, StdioServerParameters

import karellen_lsp_mcp.server as server


class ToolListingTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_tools_listed_over_protocol(self):
        registered = await server.mcp.list_tools()
        async with Client(server.mcp) as client:
            result = await client.list_tools()
        names = {t.name for t in result.tools}
        self.assertEqual(names, {t.name for t in registered})
        self.assertIn("lsp_register_project", names)
        self.assertIn("lsp_find_references", names)
        for tool in result.tools:
            self.assertEqual(tool.input_schema.get("type"), "object", tool.name)


class StdioTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_negotiates_modern_protocol(self):
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", "from karellen_lsp_mcp.server import main; main()"],
            env={"PYTHONPATH": os.pathsep.join(sys.path)},
        )
        async with Client(params) as client:
            self.assertEqual(client.protocol_version, "2026-07-28")
            self.assertEqual(client.server_info.name, "karellen-lsp-mcp")
            self.assertIn("lsp_register_project", client.instructions)
            result = await client.list_tools()
        names = {t.name for t in result.tools}
        self.assertIn("lsp_register_project", names)
        self.assertIn("lsp_find_references", names)
