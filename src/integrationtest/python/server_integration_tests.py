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
import logging
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
# Base class for server integration tests
# ---------------------------------------------------------------------------

class _ServerTestBase(unittest.TestCase):
    """Base class providing daemon lifecycle, socket patching, and logging setup.

    Daemon is started once per test class (setUpClass) and shared across all
    test methods, so clangd is only spawned/indexed once.
    """

    @classmethod
    def setUpClass(cls):
        _skip_if_no_clangd()
        cls._log_handler = logging.StreamHandler()
        cls._log_handler.setLevel(logging.DEBUG)
        logging.getLogger("karellen_lsp_mcp").addHandler(cls._log_handler)
        cls._loop = asyncio.new_event_loop()
        cls._daemon_dir = tempfile.mkdtemp(prefix="karellen-lsp-mcp-daemon-")
        cls._data_dir = tempfile.mkdtemp(prefix="karellen-lsp-mcp-data-")
        cls._data_patch = unittest.mock.patch(
            "karellen_lsp_mcp.lsp_adapter._user_data_dir",
            return_value=cls._data_dir)
        cls._data_patch.start()
        cls._sock_patch = unittest.mock.patch(
            "karellen_lsp_mcp.daemon_client.get_socket_path",
            return_value=os.path.join(cls._daemon_dir, "daemon.sock"))
        cls._sock_patch.start()
        cls._daemon = Daemon(idle_timeout=5, runtime_dir=cls._daemon_dir)
        cls._daemon_task = cls._loop.create_task(cls._daemon.run())
        sock_path = os.path.join(cls._daemon_dir, "daemon.sock")
        cls._loop.run_until_complete(cls._wait_for_socket(sock_path))
        server_mod._client = None

    @classmethod
    async def _wait_for_socket(cls, sock_path):
        for _ in range(50):
            if os.path.exists(sock_path):
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("Daemon socket did not appear")

    @classmethod
    def tearDownClass(cls):
        async def _shutdown():
            if server_mod._client is not None:
                await server_mod._client.close()
                server_mod._client = None
            cls._daemon._shutdown_event.set()
            try:
                await asyncio.wait_for(cls._daemon_task, timeout=30)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            # Cancel any remaining tasks
            pending = [t for t in asyncio.all_tasks()
                       if t is not asyncio.current_task() and not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        try:
            cls._loop.run_until_complete(_shutdown())
        except Exception:
            pass
        cls._loop.run_until_complete(cls._loop.shutdown_asyncgens())
        cls._loop.run_until_complete(cls._loop.shutdown_default_executor())
        cls._loop.close()
        cls._sock_patch.stop()
        cls._data_patch.stop()
        shutil.rmtree(cls._daemon_dir, ignore_errors=True)
        shutil.rmtree(cls._data_dir, ignore_errors=True)
        logging.getLogger("karellen_lsp_mcp").removeHandler(cls._log_handler)

    def _run(self, coro):
        return self.__class__._loop.run_until_complete(coro)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class ServerEndToEndTest(_ServerTestBase):
    """End-to-end tests exercising MCP tool functions through the full stack."""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix="karellen-lsp-mcp-e2e-")
        cls._files = _create_project(cls._tmpdir)
        super().setUpClass()
        # Register once and index — shared across all query tests
        reg = cls._loop.run_until_complete(server_mod.lsp_register_project(
            project_path=cls._tmpdir, language="cpp",
            lsp_command=["clangd", "--background-index"],
            build_info={"compile_commands_dir": cls._tmpdir}))
        cls._project_id = reg.project_id
        cls._registration_id = reg.registration_id
        # Open files to trigger indexing; daemon waits for readiness automatically
        for f in cls._files.values():
            cls._loop.run_until_complete(
                server_mod.lsp_document_symbols(cls._project_id, f))

    @classmethod
    def tearDownClass(cls):
        try:
            cls._loop.run_until_complete(
                server_mod.lsp_deregister_project(cls._registration_id))
        except Exception:
            pass
        shutil.rmtree(cls._tmpdir, ignore_errors=True)
        super().tearDownClass()

    # --- Lifecycle (these use refcount increments, not new clangd instances) ---

    def test_register_returns_dataclass(self):
        # Re-registering same project just increments refcount
        result = self._run(server_mod.lsp_register_project(
            project_path=self._tmpdir, language="cpp",
            lsp_command=["clangd", "--background-index"],
            build_info={"compile_commands_dir": self._tmpdir}))
        self.assertIsInstance(result, RegisterResult)
        self.assertTrue(len(result.project_id) > 0)
        self._run(server_mod.lsp_deregister_project(result.registration_id))

    def test_deregister_returns_string_result(self):
        reg = self._run(server_mod.lsp_register_project(
            project_path=self._tmpdir, language="cpp",
            build_info={"compile_commands_dir": self._tmpdir}))
        result = self._run(server_mod.lsp_deregister_project(reg.registration_id))
        self.assertIsInstance(result, StringResult)
        self.assertIn(reg.registration_id, result.result)

    def test_list_projects_returns_typed_list(self):
        result = self._run(server_mod.lsp_list_projects())
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        self.assertIsInstance(result[0], ProjectInfo)
        self.assertEqual(result[0].project_id, self._project_id)
        self.assertEqual(result[0].language, "c")
        self.assertGreaterEqual(result[0].refcount, 1)

    # --- Query tools return correct dataclass types (use shared project) ---

    def test_read_definition_returns_location_result(self):
        # main.cpp line 6 (1-based): "int sum = add(1, 2);"
        result = self._run(server_mod.lsp_read_definition(
            self._project_id, self._files["main.cpp"], 6, 15))
        self.assertIsInstance(result, LocationResult)
        self.assertGreater(len(result.locations), 0)
        self.assertTrue(any("math" in loc.file for loc in result.locations))
        self.assertFalse(result.indexing)

    def test_find_references_returns_location_result(self):
        # math.cpp line 3 (1-based): "int add(int a, int b) {"
        result = self._run(server_mod.lsp_find_references(
            self._project_id, self._files["math.cpp"], 3, 5))
        self.assertIsInstance(result, LocationResult)
        self.assertGreaterEqual(len(result.locations), 2)

    def test_hover_returns_hover_result(self):
        # main.cpp line 6 (1-based): "int sum = add(1, 2);"
        result = self._run(server_mod.lsp_hover(
            self._project_id, self._files["main.cpp"], 6, 15))
        self.assertIsInstance(result, HoverResult)
        self.assertIn("int", str(result.content))

    def test_document_symbols_returns_typed_result(self):
        result = self._run(server_mod.lsp_document_symbols(
            self._project_id, self._files["main.cpp"]))
        self.assertIsInstance(result, DocumentSymbolsResult)
        names = [s.name for s in result.symbols]
        self.assertIn("main", names)

    def test_call_hierarchy_incoming_returns_typed_result(self):
        # math.cpp line 3 (1-based): "int add(int a, int b) {"
        result = self._run(server_mod.lsp_call_hierarchy_incoming(
            self._project_id, self._files["math.cpp"], 3, 5))
        self.assertIsInstance(result, CallHierarchyResult)
        self.assertEqual(result.direction, "incoming")
        names = [item.name for item in result.items]
        self.assertIn("main", names)

    def test_call_hierarchy_outgoing_returns_typed_result(self):
        # main.cpp line 5 (1-based): "int main() {"
        try:
            result = self._run(server_mod.lsp_call_hierarchy_outgoing(
                self._project_id, self._files["main.cpp"], 5, 5))
        except Exception as e:
            if "does not support" in str(e):
                self.skipTest(str(e))
            raise
        self.assertIsInstance(result, CallHierarchyResult)
        self.assertEqual(result.direction, "outgoing")
        names = [item.name for item in result.items]
        has_callees = "add" in names or "multiply" in names or "printf" in names
        self.assertTrue(has_callees, "Expected callees in: %s" % names)

    def test_type_hierarchy_supertypes_returns_typed_result(self):
        # shapes.h line 10 (1-based): "class Circle : public Shape {"
        result = self._run(server_mod.lsp_type_hierarchy_supertypes(
            self._project_id, self._files["shapes.h"], 10, 7))
        self.assertIsInstance(result, TypeHierarchyResult)
        self.assertEqual(result.direction, "supertypes")
        names = [item.name for item in result.items]
        self.assertIn("Shape", names)

    def test_type_hierarchy_subtypes_returns_typed_result(self):
        # shapes.h line 4 (1-based): "class Shape {"
        result = self._run(server_mod.lsp_type_hierarchy_subtypes(
            self._project_id, self._files["shapes.h"], 4, 7))
        self.assertIsInstance(result, TypeHierarchyResult)
        self.assertEqual(result.direction, "subtypes")
        names = [item.name for item in result.items]
        self.assertIn("Circle", names)

    def test_diagnostics_returns_typed_result(self):
        result = self._run(server_mod.lsp_diagnostics(
            self._project_id, self._files["math.cpp"]))
        self.assertIsInstance(result, DiagnosticsResult)
        errors = [d for d in result.diagnostics if d.severity == "Error"]
        self.assertEqual(len(errors), 0)

    def test_diagnostics_on_broken_file(self):
        broken = os.path.join(self._tmpdir, "broken_e2e.cpp")
        with open(broken, "w") as f:
            f.write("int foo() { return undefined_var; }\n"
                    "void bar() { unknown_func(); }\n")
        result = self._run(server_mod.lsp_diagnostics(
            self._project_id, broken))
        self.assertIsInstance(result, DiagnosticsResult)
        if result.diagnostics:
            messages = " ".join(d.message.lower() for d in result.diagnostics)
            self.assertTrue(
                "undeclared" in messages or "undefined" in messages or "error" in messages,
                "Expected diagnostic error in: %s" % [d.message for d in result.diagnostics])

    # --- Error handling via ToolError ---

    def test_invalid_project_id_raises_tool_error(self):
        self._run(server_mod._get_client())
        with self.assertRaises(ToolError) as ctx:
            self._run(server_mod.lsp_read_definition(
                "nonexistent", self._files["main.cpp"], 0, 0))
        self.assertIn("Unknown project", str(ctx.exception))

    def test_relative_path_raises_tool_error(self):
        with self.assertRaises(ToolError) as ctx:
            self._run(server_mod.lsp_read_definition(
                self._project_id, "relative/path.cpp", 0, 0))
        self.assertIn("must be absolute", str(ctx.exception))

    # --- Concurrent _get_client (race condition test) ---

    def test_concurrent_get_client_returns_same_instance(self):
        """Verify the asyncio.Lock in _get_client prevents duplicate connections."""
        old_client = server_mod._client
        server_mod._client = None

        async def run():
            results = await asyncio.gather(
                server_mod._get_client(),
                server_mod._get_client(),
                server_mod._get_client(),
            )
            self.assertIs(results[0], results[1])
            self.assertIs(results[1], results[2])

        try:
            self._run(run())
        finally:
            # Close the test client and restore the original
            if server_mod._client is not None and server_mod._client is not old_client:
                self._run(server_mod._client.close())
            server_mod._client = old_client

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

        self._run(server_mod.lsp_deregister_project(reg.registration_id))

    # --- Error handling via ToolError ---

    def test_file_outside_project_raises_tool_error(self):
        outside = tempfile.mktemp(suffix=".cpp", dir="/tmp")
        try:
            with open(outside, "w") as f:
                f.write("int x;")
            with self.assertRaises(ToolError) as ctx:
                self._run(server_mod.lsp_read_definition(
                    self._project_id, outside, 1, 1))
            self.assertIn("not under project root", str(ctx.exception))
        finally:
            if os.path.exists(outside):
                os.unlink(outside)


class ServerMultiProjectTest(_ServerTestBase):
    """Tests for multiple registrations, refcounting, and garbage collection
    through the MCP tool functions.

    These tests exercise lifecycle (register/deregister/force) so they
    manage their own clangd instances within the shared daemon.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmpdir1 = tempfile.mkdtemp(prefix="karellen-lsp-mcp-e2e-proj1-")
        cls._tmpdir2 = tempfile.mkdtemp(prefix="karellen-lsp-mcp-e2e-proj2-")
        cls._files1 = _create_project(cls._tmpdir1)
        cls._files2 = _create_project(cls._tmpdir2)
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmpdir1, ignore_errors=True)
        shutil.rmtree(cls._tmpdir2, ignore_errors=True)
        super().tearDownClass()

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
        self._run(server_mod.lsp_deregister_project(reg1.registration_id))
        projects = self._run(server_mod.lsp_list_projects())
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0].refcount, 1)

        # Second deregister -> project removed (refcount=0)
        self._run(server_mod.lsp_deregister_project(reg2.registration_id))
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
        self._run(server_mod.lsp_deregister_project(reg1.registration_id))
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

        self._run(server_mod.lsp_deregister_project(reg2.registration_id))
        projects = self._run(server_mod.lsp_list_projects())
        self.assertEqual(len(projects), 0)

    def test_same_project_language_aliases_share_registration(self):
        # c and cpp are aliases — registering as either should share one project
        reg_cpp = self._run(server_mod.lsp_register_project(
            project_path=self._tmpdir1, language="cpp",
            build_info={"compile_commands_dir": self._tmpdir1}))
        reg_c = self._run(server_mod.lsp_register_project(
            project_path=self._tmpdir1, language="c",
            build_info={"compile_commands_dir": self._tmpdir1}))

        self.assertEqual(reg_cpp.project_id, reg_c.project_id)

        projects = self._run(server_mod.lsp_list_projects())
        langs = {p.language for p in projects
                 if p.project_id == reg_cpp.project_id}
        self.assertEqual(langs, {"c"})

        self._run(server_mod.lsp_deregister_project(reg_cpp.registration_id))
        self._run(server_mod.lsp_deregister_project(reg_c.registration_id))

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

        self._run(server_mod.lsp_deregister_project(reg2.registration_id))

    def test_queries_after_deregister_raise_tool_error(self):
        reg = self._run(server_mod.lsp_register_project(
            project_path=self._tmpdir1, language="cpp",
            build_info={"compile_commands_dir": self._tmpdir1}))

        self._run(server_mod.lsp_deregister_project(reg.registration_id))

        # All query types should fail with ToolError
        with self.assertRaises(ToolError):
            self._run(server_mod.lsp_read_definition(
                reg.project_id, self._files1["main.cpp"], 1, 1))
        with self.assertRaises(ToolError):
            self._run(server_mod.lsp_find_references(
                reg.project_id, self._files1["main.cpp"], 1, 1))
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
