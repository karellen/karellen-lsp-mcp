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

"""End-to-end integration tests: MCP tool functions -> DaemonClient -> Daemon -> clangd.

Exercises the full stack through the server.py MCP tool functions, verifying that
structured dataclass responses are returned correctly and that ToolError is raised
on invalid input.
"""

import asyncio
import json
import os
import shutil
import tempfile
import textwrap
import unittest
import unittest.mock

from mcp.server.fastmcp.exceptions import ToolError

from karellen_lsp_mcp.daemon import Daemon
from karellen_lsp_mcp.types import (
    RegisterResult, StringResult, LocationResult, HoverResult,
    DocumentSymbolsResult, CallHierarchyResult, TypeHierarchyResult,
    DiagnosticsResult, ProjectInfo,
)
import karellen_lsp_mcp.server as server_mod


def _skip_if_no_clangd():
    if shutil.which("clangd") is None:
        raise unittest.SkipTest("clangd not found on PATH")


# ---------------------------------------------------------------------------
# Dummy C++ project
# ---------------------------------------------------------------------------

_MATH_H = textwrap.dedent("""\
    #ifndef MATH_H
    #define MATH_H

    int add(int a, int b);
    int multiply(int a, int b);

    #endif
""")

_MATH_CPP = textwrap.dedent("""\
    #include "math.h"

    int add(int a, int b) {
        return a + b;
    }

    int multiply(int a, int b) {
        return a * b;
    }
""")

_SHAPES_H = textwrap.dedent("""\
    #ifndef SHAPES_H
    #define SHAPES_H

    class Shape {
    public:
        virtual ~Shape() = default;
        virtual double area() const = 0;
    };

    class Circle : public Shape {
    public:
        explicit Circle(double r) : r_(r) {}
        double area() const override;
    private:
        double r_;
    };

    #endif
""")

_SHAPES_CPP = textwrap.dedent("""\
    #include "shapes.h"

    static constexpr double PI = 3.14159265358979323846;

    double Circle::area() const {
        return PI * r_ * r_;
    }
""")

_MAIN_CPP = textwrap.dedent("""\
    #include "math.h"
    #include "shapes.h"
    #include <cstdio>

    int main() {
        int sum = add(1, 2);
        int prod = multiply(3, 4);
        printf("sum=%d prod=%d\\n", sum, prod);

        Circle c(5.0);
        printf("area=%.2f\\n", c.area());
        return 0;
    }
""")


def _create_project(tmpdir):
    files = {
        "math.h": _MATH_H,
        "math.cpp": _MATH_CPP,
        "shapes.h": _SHAPES_H,
        "shapes.cpp": _SHAPES_CPP,
        "main.cpp": _MAIN_CPP,
    }
    paths = {}
    for name, content in files.items():
        path = os.path.join(tmpdir, name)
        with open(path, "w") as f:
            f.write(content)
        paths[name] = path

    cpp_files = [f for f in files if f.endswith(".cpp")]
    cc = [{"directory": tmpdir,
           "file": os.path.join(tmpdir, f),
           "command": "c++ -std=c++17 -c -o %s.o %s" % (f, os.path.join(tmpdir, f))}
          for f in cpp_files]
    with open(os.path.join(tmpdir, "compile_commands.json"), "w") as f:
        json.dump(cc, f)

    return paths


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class ServerEndToEndTest(unittest.TestCase):
    """End-to-end tests exercising MCP tool functions through the full stack."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_clangd()
        cls._tmpdir = tempfile.mkdtemp(prefix="karellen-lsp-mcp-e2e-")
        cls._files = _create_project(cls._tmpdir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def setUp(self):
        self._loop = asyncio.new_event_loop()
        # Use a temp data dir so we don't collide with a real daemon
        self._daemon_dir = tempfile.mkdtemp(prefix="karellen-lsp-mcp-daemon-")
        self._sock_patch = unittest.mock.patch(
            "karellen_lsp_mcp.daemon_client.get_socket_path",
            return_value=os.path.join(self._daemon_dir, "daemon.sock"))
        self._sock_patch.start()
        # Start daemon in-process
        self._daemon = Daemon(idle_timeout=5, data_dir=self._daemon_dir)
        self._daemon_task = self._loop.create_task(self._daemon.run())
        # Wait for socket
        sock_path = os.path.join(self._daemon_dir, "daemon.sock")
        self._loop.run_until_complete(self._wait_for_socket(sock_path))
        # Reset server module's client state so it connects fresh
        server_mod._client = None

    async def _wait_for_socket(self, sock_path):
        for _ in range(50):
            if os.path.exists(sock_path):
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("Daemon socket did not appear")

    def tearDown(self):
        # Close the server module's client
        if server_mod._client is not None:
            try:
                self._loop.run_until_complete(server_mod._client.close())
            except Exception:
                pass
            server_mod._client = None
        # Shutdown daemon
        self._daemon._shutdown_event.set()
        try:
            self._loop.run_until_complete(
                asyncio.wait_for(self._daemon_task, timeout=10))
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        self._loop.close()
        self._sock_patch.stop()
        shutil.rmtree(self._daemon_dir, ignore_errors=True)

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    # --- Lifecycle ---

    def test_register_returns_dataclass(self):
        result = self._run(server_mod.lsp_register_project(
            project_path=self._tmpdir, language="cpp",
            lsp_command=["clangd", "--background-index"],
            build_info={"compile_commands_dir": self._tmpdir}))
        self.assertIsInstance(result, RegisterResult)
        self.assertTrue(len(result.project_id) > 0)

        # Cleanup
        self._run(server_mod.lsp_deregister_project(result.project_id))

    def test_deregister_returns_string_result(self):
        reg = self._run(server_mod.lsp_register_project(
            project_path=self._tmpdir, language="cpp",
            build_info={"compile_commands_dir": self._tmpdir}))
        result = self._run(server_mod.lsp_deregister_project(reg.project_id))
        self.assertIsInstance(result, StringResult)
        self.assertIn(reg.project_id, result.result)

    def test_list_projects_returns_typed_list(self):
        reg = self._run(server_mod.lsp_register_project(
            project_path=self._tmpdir, language="cpp",
            build_info={"compile_commands_dir": self._tmpdir}))
        result = self._run(server_mod.lsp_list_projects())
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        self.assertIsInstance(result[0], ProjectInfo)
        self.assertEqual(result[0].project_id, reg.project_id)
        self.assertEqual(result[0].language, "cpp")
        self.assertGreaterEqual(result[0].refcount, 1)

        self._run(server_mod.lsp_deregister_project(reg.project_id))

    # --- Query tools return correct dataclass types ---

    def _register_and_index(self):
        reg = self._run(server_mod.lsp_register_project(
            project_path=self._tmpdir, language="cpp",
            lsp_command=["clangd", "--background-index"],
            build_info={"compile_commands_dir": self._tmpdir}))
        pid = reg.project_id
        # Open files to trigger indexing; daemon waits for readiness automatically
        for f in self._files.values():
            self._run(server_mod.lsp_document_symbols(pid, f))
        return pid

    def test_read_definition_returns_location_result(self):
        pid = self._register_and_index()
        # main.cpp line 5: "int sum = add(1, 2);"
        result = self._run(server_mod.lsp_read_definition(
            pid, self._files["main.cpp"], 5, 14))
        self.assertIsInstance(result, LocationResult)
        self.assertGreater(len(result.locations), 0)
        self.assertTrue(any("math" in loc.file for loc in result.locations))
        self.assertFalse(result.indexing)

        self._run(server_mod.lsp_deregister_project(pid))

    def test_find_references_returns_location_result(self):
        pid = self._register_and_index()
        # math.cpp line 2: "int add(int a, int b) {"
        result = self._run(server_mod.lsp_find_references(
            pid, self._files["math.cpp"], 2, 4))
        self.assertIsInstance(result, LocationResult)
        self.assertGreaterEqual(len(result.locations), 2)

        self._run(server_mod.lsp_deregister_project(pid))

    def test_hover_returns_hover_result(self):
        pid = self._register_and_index()
        # main.cpp line 5: "int sum = add(1, 2);"
        result = self._run(server_mod.lsp_hover(
            pid, self._files["main.cpp"], 5, 14))
        self.assertIsInstance(result, HoverResult)
        self.assertIn("int", str(result.content))

        self._run(server_mod.lsp_deregister_project(pid))

    def test_document_symbols_returns_typed_result(self):
        pid = self._register_and_index()
        result = self._run(server_mod.lsp_document_symbols(
            pid, self._files["main.cpp"]))
        self.assertIsInstance(result, DocumentSymbolsResult)
        names = [s.name for s in result.symbols]
        self.assertIn("main", names)

        self._run(server_mod.lsp_deregister_project(pid))

    def test_call_hierarchy_incoming_returns_typed_result(self):
        pid = self._register_and_index()
        # math.cpp line 2: "int add(int a, int b) {"
        result = self._run(server_mod.lsp_call_hierarchy_incoming(
            pid, self._files["math.cpp"], 2, 4))
        self.assertIsInstance(result, CallHierarchyResult)
        self.assertEqual(result.direction, "incoming")
        names = [item.name for item in result.items]
        self.assertIn("main", names)

        self._run(server_mod.lsp_deregister_project(pid))

    def test_call_hierarchy_outgoing_returns_typed_result(self):
        pid = self._register_and_index()
        # main.cpp line 4: "int main() {"
        result = self._run(server_mod.lsp_call_hierarchy_outgoing(
            pid, self._files["main.cpp"], 4, 4))
        self.assertIsInstance(result, CallHierarchyResult)
        self.assertEqual(result.direction, "outgoing")
        names = [item.name for item in result.items]
        has_callees = "add" in names or "multiply" in names or "printf" in names
        self.assertTrue(has_callees, "Expected callees in: %s" % names)

        self._run(server_mod.lsp_deregister_project(pid))

    def test_type_hierarchy_supertypes_returns_typed_result(self):
        pid = self._register_and_index()
        # shapes.h line 9: "class Circle : public Shape {"
        result = self._run(server_mod.lsp_type_hierarchy_supertypes(
            pid, self._files["shapes.h"], 9, 6))
        self.assertIsInstance(result, TypeHierarchyResult)
        self.assertEqual(result.direction, "supertypes")
        names = [item.name for item in result.items]
        self.assertIn("Shape", names)

        self._run(server_mod.lsp_deregister_project(pid))

    def test_type_hierarchy_subtypes_returns_typed_result(self):
        pid = self._register_and_index()
        # shapes.h line 3: "class Shape {"
        result = self._run(server_mod.lsp_type_hierarchy_subtypes(
            pid, self._files["shapes.h"], 3, 6))
        self.assertIsInstance(result, TypeHierarchyResult)
        self.assertEqual(result.direction, "subtypes")
        names = [item.name for item in result.items]
        self.assertIn("Circle", names)

        self._run(server_mod.lsp_deregister_project(pid))

    def test_diagnostics_returns_typed_result(self):
        pid = self._register_and_index()
        result = self._run(server_mod.lsp_diagnostics(
            pid, self._files["math.cpp"]))
        self.assertIsInstance(result, DiagnosticsResult)
        # Clean file should have no errors
        errors = [d for d in result.diagnostics if d.severity == "Error"]
        self.assertEqual(len(errors), 0)

        self._run(server_mod.lsp_deregister_project(pid))

    def test_diagnostics_on_broken_file(self):
        pid = self._register_and_index()
        broken = os.path.join(self._tmpdir, "broken_e2e.cpp")
        with open(broken, "w") as f:
            f.write("int foo() { return undefined_var; }\n"
                    "void bar() { unknown_func(); }\n")
        result = self._run(server_mod.lsp_diagnostics(pid, broken))
        self.assertIsInstance(result, DiagnosticsResult)
        if result.diagnostics:
            messages = " ".join(d.message.lower() for d in result.diagnostics)
            self.assertTrue(
                "undeclared" in messages or "undefined" in messages or "error" in messages,
                "Expected diagnostic error in: %s" % [d.message for d in result.diagnostics])

        self._run(server_mod.lsp_deregister_project(pid))

    # --- Error handling via ToolError ---

    def test_invalid_project_id_raises_tool_error(self):
        # Force connection to daemon first
        self._run(server_mod._get_client())
        with self.assertRaises(ToolError) as ctx:
            self._run(server_mod.lsp_read_definition(
                "nonexistent", self._files["main.cpp"], 0, 0))
        self.assertIn("Unknown project", str(ctx.exception))

    def test_relative_path_raises_tool_error(self):
        reg = self._run(server_mod.lsp_register_project(
            project_path=self._tmpdir, language="cpp",
            build_info={"compile_commands_dir": self._tmpdir}))
        with self.assertRaises(ToolError) as ctx:
            self._run(server_mod.lsp_read_definition(
                reg.project_id, "relative/path.cpp", 0, 0))
        self.assertIn("must be absolute", str(ctx.exception))

        self._run(server_mod.lsp_deregister_project(reg.project_id))

    # --- Concurrent _get_client (race condition test) ---

    def test_concurrent_get_client_returns_same_instance(self):
        """Verify the asyncio.Lock in _get_client prevents duplicate connections."""
        server_mod._client = None

        async def run():
            results = await asyncio.gather(
                server_mod._get_client(),
                server_mod._get_client(),
                server_mod._get_client(),
            )
            # All should be the same instance
            self.assertIs(results[0], results[1])
            self.assertIs(results[1], results[2])

        self._run(run())

    # --- Path with spaces (URI encoding) ---

    def test_project_with_spaces_in_path(self):
        """Validate that paths with spaces are properly percent-encoded in URIs."""
        spaced_dir = os.path.join(self._tmpdir, "sub dir with spaces")
        os.makedirs(spaced_dir, exist_ok=True)
        src = os.path.join(spaced_dir, "hello.cpp")
        with open(src, "w") as f:
            f.write("int hello() { return 42; }\n")
        cc = [{"directory": spaced_dir,
               "file": src,
               "command": "c++ -std=c++17 -c -o hello.o %s" % src}]
        with open(os.path.join(spaced_dir, "compile_commands.json"), "w") as f:
            json.dump(cc, f)

        reg = self._run(server_mod.lsp_register_project(
            project_path=spaced_dir, language="cpp",
            build_info={"compile_commands_dir": spaced_dir}))
        self.assertIsInstance(reg, RegisterResult)

        result = self._run(server_mod.lsp_document_symbols(
            reg.project_id, src))
        self.assertIsInstance(result, DocumentSymbolsResult)
        names = [s.name for s in result.symbols]
        self.assertIn("hello", names)

        self._run(server_mod.lsp_deregister_project(reg.project_id))

    # --- Error handling via ToolError ---

    def test_file_outside_project_raises_tool_error(self):
        reg = self._run(server_mod.lsp_register_project(
            project_path=self._tmpdir, language="cpp",
            build_info={"compile_commands_dir": self._tmpdir}))
        outside = tempfile.mktemp(suffix=".cpp", dir="/tmp")
        try:
            with open(outside, "w") as f:
                f.write("int x;")
            with self.assertRaises(ToolError) as ctx:
                self._run(server_mod.lsp_read_definition(
                    reg.project_id, outside, 0, 0))
            self.assertIn("not under project root", str(ctx.exception))
        finally:
            if os.path.exists(outside):
                os.unlink(outside)

        self._run(server_mod.lsp_deregister_project(reg.project_id))


class ServerMultiProjectTest(unittest.TestCase):
    """Tests for multiple registrations, refcounting, and garbage collection
    through the MCP tool functions."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_clangd()
        cls._tmpdir1 = tempfile.mkdtemp(prefix="karellen-lsp-mcp-e2e-proj1-")
        cls._tmpdir2 = tempfile.mkdtemp(prefix="karellen-lsp-mcp-e2e-proj2-")
        cls._files1 = _create_project(cls._tmpdir1)
        cls._files2 = _create_project(cls._tmpdir2)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmpdir1, ignore_errors=True)
        shutil.rmtree(cls._tmpdir2, ignore_errors=True)

    def setUp(self):
        self._loop = asyncio.new_event_loop()
        self._daemon_dir = tempfile.mkdtemp(prefix="karellen-lsp-mcp-daemon-")
        self._sock_patch = unittest.mock.patch(
            "karellen_lsp_mcp.daemon_client.get_socket_path",
            return_value=os.path.join(self._daemon_dir, "daemon.sock"))
        self._sock_patch.start()
        self._daemon = Daemon(idle_timeout=5, data_dir=self._daemon_dir)
        self._daemon_task = self._loop.create_task(self._daemon.run())
        sock_path = os.path.join(self._daemon_dir, "daemon.sock")
        self._loop.run_until_complete(self._wait_for_socket(sock_path))
        server_mod._client = None

    async def _wait_for_socket(self, sock_path):
        for _ in range(50):
            if os.path.exists(sock_path):
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("Daemon socket did not appear")

    def tearDown(self):
        if server_mod._client is not None:
            try:
                self._loop.run_until_complete(server_mod._client.close())
            except Exception:
                pass
            server_mod._client = None
        self._daemon._shutdown_event.set()
        try:
            self._loop.run_until_complete(
                asyncio.wait_for(self._daemon_task, timeout=10))
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        self._loop.close()
        self._sock_patch.stop()
        shutil.rmtree(self._daemon_dir, ignore_errors=True)

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def test_same_project_twice_increments_refcount(self):
        reg1 = self._run(server_mod.lsp_register_project(
            project_path=self._tmpdir1, language="cpp",
            build_info={"compile_commands_dir": self._tmpdir1}))
        reg2 = self._run(server_mod.lsp_register_project(
            project_path=self._tmpdir1, language="cpp"))

        # Same project_id
        self.assertEqual(reg1.project_id, reg2.project_id)

        projects = self._run(server_mod.lsp_list_projects())
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0].refcount, 2)

        # First deregister -> refcount=1
        self._run(server_mod.lsp_deregister_project(reg1.project_id))
        projects = self._run(server_mod.lsp_list_projects())
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0].refcount, 1)

        # Second deregister -> project removed (refcount=0)
        self._run(server_mod.lsp_deregister_project(reg2.project_id))
        projects = self._run(server_mod.lsp_list_projects())
        self.assertEqual(len(projects), 0)

    def test_two_different_projects_independent(self):
        reg1 = self._run(server_mod.lsp_register_project(
            project_path=self._tmpdir1, language="cpp",
            build_info={"compile_commands_dir": self._tmpdir1}))
        reg2 = self._run(server_mod.lsp_register_project(
            project_path=self._tmpdir2, language="cpp",
            build_info={"compile_commands_dir": self._tmpdir2}))

        # Different project_ids
        self.assertNotEqual(reg1.project_id, reg2.project_id)

        projects = self._run(server_mod.lsp_list_projects())
        self.assertEqual(len(projects), 2)
        pids = {p.project_id for p in projects}
        self.assertIn(reg1.project_id, pids)
        self.assertIn(reg2.project_id, pids)

        # Both projects work independently
        syms1 = self._run(server_mod.lsp_document_symbols(
            reg1.project_id, self._files1["main.cpp"]))
        self.assertIsInstance(syms1, DocumentSymbolsResult)
        names1 = [s.name for s in syms1.symbols]
        self.assertIn("main", names1)

        syms2 = self._run(server_mod.lsp_document_symbols(
            reg2.project_id, self._files2["main.cpp"]))
        self.assertIsInstance(syms2, DocumentSymbolsResult)
        names2 = [s.name for s in syms2.symbols]
        self.assertIn("main", names2)

        # Deregister project 1 -> project 2 still works
        self._run(server_mod.lsp_deregister_project(reg1.project_id))
        projects = self._run(server_mod.lsp_list_projects())
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0].project_id, reg2.project_id)

        syms2_again = self._run(server_mod.lsp_document_symbols(
            reg2.project_id, self._files2["math.cpp"]))
        names2_again = [s.name for s in syms2_again.symbols]
        self.assertIn("add", names2_again)

        # Deregistered project 1 should error
        with self.assertRaises(ToolError):
            self._run(server_mod.lsp_document_symbols(
                reg1.project_id, self._files1["main.cpp"]))

        self._run(server_mod.lsp_deregister_project(reg2.project_id))
        projects = self._run(server_mod.lsp_list_projects())
        self.assertEqual(len(projects), 0)

    def test_same_project_different_languages_are_separate(self):
        # Register same path as both "c" and "cpp" — should get different project_ids
        reg_cpp = self._run(server_mod.lsp_register_project(
            project_path=self._tmpdir1, language="cpp",
            build_info={"compile_commands_dir": self._tmpdir1}))
        reg_c = self._run(server_mod.lsp_register_project(
            project_path=self._tmpdir1, language="c",
            build_info={"compile_commands_dir": self._tmpdir1}))

        self.assertNotEqual(reg_cpp.project_id, reg_c.project_id)

        projects = self._run(server_mod.lsp_list_projects())
        self.assertEqual(len(projects), 2)
        langs = {p.language for p in projects}
        self.assertEqual(langs, {"c", "cpp"})

        self._run(server_mod.lsp_deregister_project(reg_cpp.project_id))
        self._run(server_mod.lsp_deregister_project(reg_c.project_id))

    def test_force_register_resets_lsp_server(self):
        reg1 = self._run(server_mod.lsp_register_project(
            project_path=self._tmpdir1, language="cpp",
            build_info={"compile_commands_dir": self._tmpdir1}))

        # Force re-register should return same project_id
        reg2 = self._run(server_mod.lsp_register_project(
            project_path=self._tmpdir1, language="cpp",
            build_info={"compile_commands_dir": self._tmpdir1},
            force=True))
        self.assertEqual(reg1.project_id, reg2.project_id)

        # LSP should still work after force restart
        syms = self._run(server_mod.lsp_document_symbols(
            reg2.project_id, self._files1["main.cpp"]))
        self.assertIsInstance(syms, DocumentSymbolsResult)
        names = [s.name for s in syms.symbols]
        self.assertIn("main", names)

        self._run(server_mod.lsp_deregister_project(reg2.project_id))

    def test_queries_after_deregister_raise_tool_error(self):
        reg = self._run(server_mod.lsp_register_project(
            project_path=self._tmpdir1, language="cpp",
            build_info={"compile_commands_dir": self._tmpdir1}))

        self._run(server_mod.lsp_deregister_project(reg.project_id))

        # All query types should fail with ToolError
        with self.assertRaises(ToolError):
            self._run(server_mod.lsp_read_definition(
                reg.project_id, self._files1["main.cpp"], 0, 0))
        with self.assertRaises(ToolError):
            self._run(server_mod.lsp_find_references(
                reg.project_id, self._files1["main.cpp"], 0, 0))
        with self.assertRaises(ToolError):
            self._run(server_mod.lsp_hover(
                reg.project_id, self._files1["main.cpp"], 0, 0))
        with self.assertRaises(ToolError):
            self._run(server_mod.lsp_document_symbols(
                reg.project_id, self._files1["main.cpp"]))
        with self.assertRaises(ToolError):
            self._run(server_mod.lsp_diagnostics(
                reg.project_id, self._files1["main.cpp"]))


if __name__ == "__main__":
    unittest.main()
