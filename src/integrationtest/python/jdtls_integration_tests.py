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

"""Integration tests: daemon + jdtls for Java/Kotlin projects.

Exercises autodetection → adapter → jdtls registration → LSP queries
against a minimal Java project with Gradle build markers.
"""

import asyncio
import logging
import os
import shutil
import tempfile
import textwrap
import unittest
import unittest.mock

from karellen_lsp_mcp.daemon import Daemon, _read_message, _write_message


def _skip_if_no_jdtls():
    if shutil.which("jdtls") is None:
        raise unittest.SkipTest("jdtls not found on PATH")


# ---------------------------------------------------------------------------
# Minimal Java project with Gradle markers
# ---------------------------------------------------------------------------

_BUILD_GRADLE = textwrap.dedent("""\
    plugins {
        id 'java'
    }
    repositories {
        mavenCentral()
    }
""")

_SETTINGS_GRADLE = textwrap.dedent("""\
    rootProject.name = 'test-project'
""")

_GREETER_JAVA = textwrap.dedent("""\
    package com.example;

    public class Greeter {
        private final String name;

        public Greeter(String name) {
            this.name = name;
        }

        public String greet() {
            return "Hello, " + name + "!";
        }
    }
""")

_MAIN_JAVA = textwrap.dedent("""\
    package com.example;

    public class Main {
        public static void main(String[] args) {
            Greeter greeter = new Greeter("World");
            System.out.println(greeter.greet());
        }
    }
""")

_CALCULATOR_JAVA = textwrap.dedent("""\
    package com.example;

    public class Calculator {
        public int add(int a, int b) {
            return a + b;
        }

        public int multiply(int a, int b) {
            return a * b;
        }
    }
""")


def _create_java_project(base_dir):
    """Create a minimal Java project with Gradle markers."""
    # Build files
    with open(os.path.join(base_dir, "build.gradle"), "w") as f:
        f.write(_BUILD_GRADLE)
    with open(os.path.join(base_dir, "settings.gradle"), "w") as f:
        f.write(_SETTINGS_GRADLE)

    # Source files
    src_dir = os.path.join(base_dir, "src", "main", "java", "com", "example")
    os.makedirs(src_dir, exist_ok=True)

    files = {}
    for name, content in [("Greeter.java", _GREETER_JAVA),
                          ("Main.java", _MAIN_JAVA),
                          ("Calculator.java", _CALCULATOR_JAVA)]:
        path = os.path.join(src_dir, name)
        with open(path, "w") as f:
            f.write(content)
        files[name] = path

    return files


# ---------------------------------------------------------------------------
# Test helper (reused pattern from daemon_lsp_integration_tests)
# ---------------------------------------------------------------------------

class _DaemonTestHelper:
    """Manages an in-process daemon and a Unix-socket client for testing."""

    def __init__(self):
        self.daemon = None
        self._daemon_task = None
        self._daemon_dir = None
        self._data_dir = None
        self._data_patch = None
        self._reader = None
        self._writer = None
        self._msg_id = 0

    async def start(self):
        self._daemon_dir = tempfile.mkdtemp(prefix="karellen-lsp-mcp-jdtls-")
        self._data_dir = tempfile.mkdtemp(prefix="karellen-lsp-mcp-data-")
        self._data_patch = unittest.mock.patch(
            "karellen_lsp_mcp.lsp_adapter._user_data_dir",
            return_value=self._data_dir)
        self._data_patch.start()
        self.daemon = Daemon(idle_timeout=5, runtime_dir=self._daemon_dir)
        self._daemon_task = asyncio.create_task(self.daemon.run())
        sock_path = os.path.join(self._daemon_dir, "daemon.sock")
        for _ in range(50):
            if os.path.exists(sock_path):
                try:
                    self._reader, self._writer = await asyncio.open_unix_connection(sock_path)
                    return
                except (ConnectionRefusedError, OSError):
                    pass
            await asyncio.sleep(0.1)
        raise RuntimeError("Daemon did not start within 5 seconds")

    async def stop(self):
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        if self.daemon:
            self.daemon._shutdown_event.set()
        if self._daemon_task:
            try:
                await asyncio.wait_for(self._daemon_task, timeout=30)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if self._data_patch:
            self._data_patch.stop()
        if self._daemon_dir:
            shutil.rmtree(self._daemon_dir, ignore_errors=True)
        if self._data_dir:
            shutil.rmtree(self._data_dir, ignore_errors=True)

    async def request(self, method, params=None):
        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method, "params": params or {}}
        _write_message(self._writer, msg)
        await self._writer.drain()
        response = await asyncio.wait_for(_read_message(self._reader), timeout=180)
        if "error" in response:
            raise RuntimeError(response["error"]["message"])
        return response.get("result")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class JdtlsIntegrationTest(unittest.TestCase):
    """Full round-trip tests: detection + daemon + jdtls + Java project."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_jdtls()
        cls._log_handler = logging.StreamHandler()
        cls._log_handler.setLevel(logging.DEBUG)
        logging.getLogger("karellen_lsp_mcp").addHandler(cls._log_handler)
        cls._tmpdir = tempfile.mkdtemp(prefix="karellen-lsp-mcp-jdtls-itest-")
        cls._files = _create_java_project(cls._tmpdir)
        cls._loop = asyncio.new_event_loop()
        cls._helper = _DaemonTestHelper()
        cls._loop.run_until_complete(cls._helper.start())

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, '_project_id'):
            try:
                cls._loop.run_until_complete(
                    cls._helper.request("deregister_project",
                                        {"project_id": cls._project_id}))
            except Exception:
                pass
        cls._loop.run_until_complete(cls._helper.stop())
        cls._loop.close()
        shutil.rmtree(cls._tmpdir, ignore_errors=True)
        logging.getLogger("karellen_lsp_mcp").removeHandler(cls._log_handler)

    def _request(self, method, params):
        return self._loop.run_until_complete(self.__class__._helper.request(method, params))

    # --- Detection ---

    def test_01_detect_project(self):
        result = self._request("detect_project", {
            "project_path": self._tmpdir,
        })
        self.assertEqual(result["project_path"], self._tmpdir)
        self.assertGreater(len(result["languages"]), 0)
        java_lang = result["languages"][0]
        self.assertEqual(java_lang["language"], "java")
        self.assertEqual(java_lang["build_system"], "gradle")
        self.assertTrue(java_lang["server_available"])

    # --- Registration via autodetection ---

    def test_02_register_with_autodetection(self):
        result = self._request("register_project", {
            "project_path": self._tmpdir,
            "language": "java",
        })
        self.assertIn("project_id", result)
        self.__class__._project_id = result["project_id"]

    # --- LSP queries ---

    def test_03_list_projects(self):
        projects = self._request("list_projects", {})
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["language"], "java")
        self.assertIn(projects[0]["status"], ("indexing", "ready"))

    def test_04_document_symbols(self):
        result = self._request("lsp_document_symbols", {
            "project_id": self._project_id,
            "file_path": self._files["Greeter.java"],
        })
        names = [s["name"] for s in result.get("symbols", [])]
        self.assertIn("Greeter", names)

    def test_05_hover(self):
        # Greeter.java line 6 (0-based): "public Greeter(String name) {"
        result = self._request("lsp_hover", {
            "project_id": self._project_id,
            "file_path": self._files["Greeter.java"],
            "line": 5,
            "character": 11,
        })
        # Should return type info about the constructor
        self.assertTrue(result.get("parts") or result.get("content"))

    def test_06_read_definition(self):
        # Main.java line 4 (0-based): "Greeter greeter = new Greeter("World");"
        # Cursor on "Greeter" at character 8
        result = self._request("lsp_read_definition", {
            "project_id": self._project_id,
            "file_path": self._files["Main.java"],
            "line": 4,
            "character": 8,
        })
        locations = result.get("locations", [])
        self.assertGreater(len(locations), 0)
        self.assertTrue(any("Greeter" in loc["file"] for loc in locations))

    def test_07_find_references(self):
        # Greeter.java line 2 (0-based): "public class Greeter {"
        # Cursor on "Greeter" at character 13
        result = self._request("lsp_find_references", {
            "project_id": self._project_id,
            "file_path": self._files["Greeter.java"],
            "line": 2,
            "character": 13,
        })
        locations = result.get("locations", [])
        # Should find at least the declaration and usage in Main.java
        self.assertGreaterEqual(len(locations), 2)

    def test_08_diagnostics(self):
        result = self._request("lsp_diagnostics", {
            "project_id": self._project_id,
            "file_path": self._files["Calculator.java"],
        })
        # Clean file should have no errors
        errors = [d for d in result.get("diagnostics", [])
                  if d.get("severity") == "Error"]
        self.assertEqual(len(errors), 0)
