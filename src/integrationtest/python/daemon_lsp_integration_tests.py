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

"""Integration tests: daemon + real clangd over Unix socket.

Exercises the full LSP round-trip against clangd with a representative
C++ project that has:
- Multiple translation units (main.cpp, math_utils.cpp, shapes.cpp)
- Header files (math_utils.h, shapes.h)
- Class hierarchy with inheritance (Shape -> Circle, Rectangle)
- Virtual methods, overrides
- Free functions called across translation units
- Struct with fields
- Enum
- compile_commands.json

This simulates how an LLM would use the MCP tools to understand a codebase.
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import textwrap
import unittest

from karellen_lsp_mcp.daemon import Daemon, _read_message, _write_message


def _skip_if_no_clangd():
    if shutil.which("clangd") is None:
        raise unittest.SkipTest("clangd not found on PATH")


# ---------------------------------------------------------------------------
# Dummy C++ project source files
# ---------------------------------------------------------------------------

_MATH_UTILS_H = textwrap.dedent("""\
    #ifndef MATH_UTILS_H
    #define MATH_UTILS_H

    enum class Operation {
        ADD,
        SUBTRACT,
        MULTIPLY,
    };

    struct Vector2D {
        double x;
        double y;
    };

    int add(int a, int b);
    int subtract(int a, int b);
    int apply_op(Operation op, int a, int b);
    double dot_product(const Vector2D& a, const Vector2D& b);

    #endif
""")

_MATH_UTILS_CPP = textwrap.dedent("""\
    #include "math_utils.h"

    int add(int a, int b) {
        return a + b;
    }

    int subtract(int a, int b) {
        return a - b;
    }

    int apply_op(Operation op, int a, int b) {
        switch (op) {
            case Operation::ADD: return add(a, b);
            case Operation::SUBTRACT: return subtract(a, b);
            case Operation::MULTIPLY: return a * b;
        }
        return 0;
    }

    double dot_product(const Vector2D& a, const Vector2D& b) {
        return a.x * b.x + a.y * b.y;
    }
""")

_SHAPES_H = textwrap.dedent("""\
    #ifndef SHAPES_H
    #define SHAPES_H

    class Shape {
    public:
        virtual ~Shape() = default;
        virtual double area() const = 0;
        virtual const char* name() const = 0;
    };

    class Circle : public Shape {
    public:
        explicit Circle(double radius);
        double area() const override;
        const char* name() const override;
        double radius() const;
    private:
        double radius_;
    };

    class Rectangle : public Shape {
    public:
        Rectangle(double width, double height);
        double area() const override;
        const char* name() const override;
    private:
        double width_;
        double height_;
    };

    void print_shape_info(const Shape& shape);

    #endif
""")

_SHAPES_CPP = textwrap.dedent("""\
    #include "shapes.h"
    #include "math_utils.h"
    #include <cstdio>

    static constexpr double PI = 3.14159265358979323846;

    Circle::Circle(double radius) : radius_(radius) {}

    double Circle::area() const {
        return PI * radius_ * radius_;
    }

    const char* Circle::name() const {
        return "Circle";
    }

    double Circle::radius() const {
        return radius_;
    }

    Rectangle::Rectangle(double width, double height)
        : width_(width), height_(height) {}

    double Rectangle::area() const {
        return width_ * height_;
    }

    const char* Rectangle::name() const {
        return "Rectangle";
    }

    void print_shape_info(const Shape& shape) {
        printf("%s: area=%.2f\\n", shape.name(), shape.area());
    }
""")

_MAIN_CPP = textwrap.dedent("""\
    #include "math_utils.h"
    #include "shapes.h"
    #include <cstdio>

    int main(int argc, char* argv[]) {
        int sum = add(10, 20);
        int diff = subtract(30, 15);
        int product = apply_op(Operation::MULTIPLY, 5, 6);

        printf("sum=%d diff=%d product=%d\\n", sum, diff, product);

        Vector2D v1 = {1.0, 2.0};
        Vector2D v2 = {3.0, 4.0};
        double dp = dot_product(v1, v2);
        printf("dot_product=%.1f\\n", dp);

        Circle c(5.0);
        Rectangle r(3.0, 4.0);
        print_shape_info(c);
        print_shape_info(r);

        return 0;
    }
""")


def _create_project(tmpdir):
    """Write the dummy C++ project into tmpdir. Returns dict of file paths."""
    files = {
        "math_utils.h": _MATH_UTILS_H,
        "math_utils.cpp": _MATH_UTILS_CPP,
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

    # compile_commands.json for clangd
    cpp_files = [f for f in files if f.endswith(".cpp")]
    cc = [{"directory": tmpdir,
           "file": os.path.join(tmpdir, f),
           "command": "c++ -std=c++17 -c -o %s.o %s" % (f, os.path.join(tmpdir, f))}
          for f in cpp_files]
    with open(os.path.join(tmpdir, "compile_commands.json"), "w") as f:
        json.dump(cc, f)

    return paths


# ---------------------------------------------------------------------------
# Test helper
# ---------------------------------------------------------------------------

class _DaemonTestHelper:
    """Manages an in-process daemon and a Unix-socket client for testing."""

    def __init__(self):
        self.daemon = None
        self._daemon_task = None
        self._daemon_dir = None
        self._reader = None
        self._writer = None
        self._msg_id = 0

    async def start(self):
        self._daemon_dir = tempfile.mkdtemp(prefix="karellen-lsp-mcp-daemon-")
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
                await asyncio.wait_for(self._daemon_task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if self._daemon_dir:
            shutil.rmtree(self._daemon_dir, ignore_errors=True)

    async def request(self, method, params=None):
        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method, "params": params or {}}
        _write_message(self._writer, msg)
        await self._writer.drain()
        response = await asyncio.wait_for(_read_message(self._reader), timeout=60)
        if "error" in response:
            raise RuntimeError(response["error"]["message"])
        return response.get("result")


# ---------------------------------------------------------------------------
# Helper to collect symbol names from structured result
# ---------------------------------------------------------------------------

def _symbol_names(symbols):
    """Recursively collect all symbol names from a structured symbols list."""
    names = []
    for s in symbols:
        names.append(s["name"])
        if "children" in s:
            names.extend(_symbol_names(s["children"]))
    return names


# ---------------------------------------------------------------------------
# Tests: LSP introspection of the dummy C++ project
# ---------------------------------------------------------------------------

class DaemonLspIntegrationTest(unittest.TestCase):
    """Full round-trip tests: daemon + clangd + C++ project.

    Simulates how an LLM would use the tools:
    1. Register a project
    2. List projects to verify registration
    3. Query symbols, definitions, references, hover, call hierarchy,
       type hierarchy, diagnostics
    4. Deregister
    """

    @classmethod
    def setUpClass(cls):
        _skip_if_no_clangd()
        cls._log_handler = logging.StreamHandler()
        cls._log_handler.setLevel(logging.DEBUG)
        logging.getLogger("karellen_lsp_mcp").addHandler(cls._log_handler)
        cls._tmpdir = tempfile.mkdtemp(prefix="karellen-lsp-mcp-itest-")
        cls._files = _create_project(cls._tmpdir)
        cls._loop = asyncio.new_event_loop()
        cls._helper = _DaemonTestHelper()
        cls._loop.run_until_complete(cls._helper.start())
        # Register as C++ project with background indexing
        result = cls._loop.run_until_complete(
            cls._helper.request("register_project", {
                "project_path": cls._tmpdir,
                "language": "cpp",
                "lsp_command": ["clangd", "--background-index"],
                "build_info": {"compile_commands_dir": cls._tmpdir},
            })
        )
        cls._project_id = result["project_id"]
        # Pre-open all source files so clangd indexes them
        for f in cls._files.values():
            cls._loop.run_until_complete(cls._helper.request(
                "lsp_document_symbols", {
                    "project_id": cls._project_id,
                    "file_path": f,
                }))

    @classmethod
    def tearDownClass(cls):
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

    # --- Lifecycle ---

    def test_register_and_list_projects(self):
        projects = self._request("list_projects", {})
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["project_id"], self._project_id)
        self.assertEqual(projects[0]["language"], "cpp")
        self.assertEqual(projects[0]["refcount"], 1)
        self.assertIn(projects[0]["status"], ("indexing", "ready"))

    def test_register_same_project_increments_refcount(self):
        result = self._request("register_project", {
            "project_path": self._tmpdir,
            "language": "cpp",
        })
        self.assertEqual(result["project_id"], self._project_id)

        projects = self._request("list_projects", {})
        self.assertEqual(projects[0]["refcount"], 2)

        self._request("deregister_project", {"project_id": self._project_id})

    # --- Document symbols ---

    def test_document_symbols_main(self):
        result = self._request("lsp_document_symbols", {
            "project_id": self._project_id,
            "file_path": self._files["main.cpp"],
        })
        names = _symbol_names(result["symbols"])
        self.assertIn("main", names)

    def test_document_symbols_math_utils(self):
        result = self._request("lsp_document_symbols", {
            "project_id": self._project_id,
            "file_path": self._files["math_utils.cpp"],
        })
        names = _symbol_names(result["symbols"])
        self.assertIn("add", names)
        self.assertIn("subtract", names)
        self.assertIn("apply_op", names)
        self.assertIn("dot_product", names)

    def test_document_symbols_shapes(self):
        result = self._request("lsp_document_symbols", {
            "project_id": self._project_id,
            "file_path": self._files["shapes.cpp"],
        })
        names = _symbol_names(result["symbols"])
        # clangd uses qualified names like "Circle::area" in .cpp files
        has_area = any("area" in n for n in names)
        has_name = any("name" in n for n in names)
        self.assertTrue(has_area, "Expected 'area' in: %s" % names)
        self.assertTrue(has_name, "Expected 'name' in: %s" % names)

    def test_document_symbols_shapes_header(self):
        result = self._request("lsp_document_symbols", {
            "project_id": self._project_id,
            "file_path": self._files["shapes.h"],
        })
        names = _symbol_names(result["symbols"])
        self.assertIn("Shape", names)
        self.assertIn("Circle", names)
        self.assertIn("Rectangle", names)

    # --- Definition ---

    def test_definition_of_add_from_main(self):
        # main.cpp line 5: "int sum = add(10, 20);"
        result = self._request("lsp_read_definition", {
            "project_id": self._project_id,
            "file_path": self._files["main.cpp"],
            "line": 5,
            "character": 14,
        })
        self.assertGreater(len(result["locations"]), 0)
        files = [loc["file"] for loc in result["locations"]]
        self.assertTrue(any("math_utils" in f for f in files),
                        "Expected math_utils in: %s" % files)

    def test_definition_of_circle_constructor(self):
        # main.cpp line 16: "Circle c(5.0);"
        result = self._request("lsp_read_definition", {
            "project_id": self._project_id,
            "file_path": self._files["main.cpp"],
            "line": 16,
            "character": 4,
        })
        self.assertGreater(len(result["locations"]), 0)
        files = [loc["file"] for loc in result["locations"]]
        self.assertTrue(any("shapes" in f for f in files),
                        "Expected shapes in: %s" % files)

    def test_definition_of_dot_product(self):
        # main.cpp line 13: "double dp = dot_product(v1, v2);"
        result = self._request("lsp_read_definition", {
            "project_id": self._project_id,
            "file_path": self._files["main.cpp"],
            "line": 13,
            "character": 16,
        })
        self.assertGreater(len(result["locations"]), 0)
        files = [loc["file"] for loc in result["locations"]]
        self.assertTrue(any("math_utils" in f for f in files),
                        "Expected math_utils in: %s" % files)

    # --- References ---

    def test_find_references_of_add(self):
        # add is declared in math_utils.h, defined in math_utils.cpp,
        # called in main.cpp and apply_op in math_utils.cpp
        result = self._request("lsp_find_references", {
            "project_id": self._project_id,
            "file_path": self._files["math_utils.cpp"],
            "line": 2,
            "character": 4,
        })
        self.assertGreaterEqual(len(result["locations"]), 2)

    def test_find_references_of_area(self):
        # area() is declared in Shape, overridden in Circle and Rectangle,
        # called in print_shape_info
        # shapes.h line 6: "virtual double area() const = 0;"
        result = self._request("lsp_find_references", {
            "project_id": self._project_id,
            "file_path": self._files["shapes.h"],
            "line": 6,
            "character": 19,
        })
        # Declaration + overrides + call site
        self.assertGreaterEqual(len(result["locations"]), 2)

    # --- Hover ---

    def test_hover_on_add(self):
        # main.cpp line 5: "int sum = add(10, 20);"
        result = self._request("lsp_hover", {
            "project_id": self._project_id,
            "file_path": self._files["main.cpp"],
            "line": 5,
            "character": 14,
        })
        self.assertIsNotNone(result.get("content"))
        # Check full result dict contains "int" somewhere
        self.assertIn("int", str(result))

    def test_hover_on_vector2d(self):
        # main.cpp line 11: "    Vector2D v1 = {1.0, 2.0};"
        result = self._request("lsp_hover", {
            "project_id": self._project_id,
            "file_path": self._files["main.cpp"],
            "line": 11,
            "character": 4,
        })
        self.assertIn("Vector2D", str(result))

    def test_hover_on_circle(self):
        # main.cpp line 16: "Circle c(5.0);"
        result = self._request("lsp_hover", {
            "project_id": self._project_id,
            "file_path": self._files["main.cpp"],
            "line": 16,
            "character": 4,
        })
        self.assertIn("Circle", str(result))

    # --- Call hierarchy ---

    def test_call_hierarchy_incoming_for_add(self):
        # Who calls add()? -> main() and apply_op()
        result = self._request("lsp_call_hierarchy_incoming", {
            "project_id": self._project_id,
            "file_path": self._files["math_utils.cpp"],
            "line": 2,
            "character": 4,
        })
        self.assertEqual(result["direction"], "incoming")
        names = [item["name"] for item in result["items"]]
        self.assertIn("main", names)

    def test_call_hierarchy_outgoing_for_apply_op(self):
        # What does apply_op() call? -> add() and subtract()
        try:
            result = self._request("lsp_call_hierarchy_outgoing", {
                "project_id": self._project_id,
                "file_path": self._files["math_utils.cpp"],
                "line": 10,
                "character": 4,
            })
        except RuntimeError as e:
            if "does not support" in str(e):
                self.skipTest(str(e))
            raise
        self.assertEqual(result["direction"], "outgoing")
        names = [item["name"] for item in result["items"]]
        has_callees = "add" in names or "subtract" in names
        self.assertTrue(has_callees, "Expected callees in: %s" % names)

    def test_call_hierarchy_outgoing_for_main(self):
        # main() calls add, subtract, apply_op, dot_product, print_shape_info, printf
        try:
            result = self._request("lsp_call_hierarchy_outgoing", {
                "project_id": self._project_id,
                "file_path": self._files["main.cpp"],
                "line": 4,
                "character": 4,
            })
        except RuntimeError as e:
            if "does not support" in str(e):
                self.skipTest(str(e))
            raise
        names = [item["name"] for item in result["items"]]
        has_callees = "add" in names or "subtract" in names or "printf" in names
        self.assertTrue(has_callees, "Expected callees in: %s" % names)

    # --- Type hierarchy ---

    def test_type_hierarchy_supertypes_of_circle(self):
        # Circle inherits from Shape
        # shapes.h line 10: "class Circle : public Shape {"
        result = self._request("lsp_type_hierarchy_supertypes", {
            "project_id": self._project_id,
            "file_path": self._files["shapes.h"],
            "line": 10,
            "character": 6,
        })
        self.assertEqual(result["direction"], "supertypes")
        names = [item["name"] for item in result["items"]]
        self.assertIn("Shape", names)

    def test_type_hierarchy_supertypes_of_rectangle(self):
        # Rectangle inherits from Shape
        # shapes.h line 20: "class Rectangle : public Shape {"
        result = self._request("lsp_type_hierarchy_supertypes", {
            "project_id": self._project_id,
            "file_path": self._files["shapes.h"],
            "line": 20,
            "character": 6,
        })
        names = [item["name"] for item in result["items"]]
        self.assertIn("Shape", names)

    def test_type_hierarchy_subtypes_of_shape(self):
        # Shape has subtypes Circle and Rectangle
        # shapes.h line 3: "class Shape {"
        result = self._request("lsp_type_hierarchy_subtypes", {
            "project_id": self._project_id,
            "file_path": self._files["shapes.h"],
            "line": 3,
            "character": 6,
        })
        names = [item["name"] for item in result["items"]]
        has_subtypes = "Circle" in names or "Rectangle" in names
        self.assertTrue(has_subtypes, "Expected subtypes in: %s" % names)

    # --- Diagnostics ---

    def test_diagnostics_clean_file(self):
        result = self._request("lsp_diagnostics", {
            "project_id": self._project_id,
            "file_path": self._files["math_utils.cpp"],
        })
        # Clean file should have no Error-level diagnostics
        errors = [d for d in result["diagnostics"] if d["severity"] == "Error"]
        self.assertEqual(len(errors), 0)

    def test_diagnostics_broken_file(self):
        broken_cpp = os.path.join(self._tmpdir, "broken.cpp")
        with open(broken_cpp, "w") as f:
            f.write("int foo() { return undefined_var; }\n"
                    "void bar() { unknown_function(); }\n")

        result = self._request("lsp_diagnostics", {
            "project_id": self._project_id,
            "file_path": broken_cpp,
        })
        if result["diagnostics"]:
            messages = " ".join(d["message"].lower() for d in result["diagnostics"])
            self.assertTrue(
                "undeclared" in messages or "undefined" in messages or "error" in messages,
                "Expected diagnostic error in: %s" % result["diagnostics"])

    # --- Error handling ---

    def test_file_path_must_be_absolute(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._request("lsp_read_definition", {
                "project_id": self._project_id,
                "file_path": "relative/path.cpp",
                "line": 0,
                "character": 0,
            })
        self.assertIn("must be absolute", str(ctx.exception))

    def test_file_outside_project_rejected(self):
        outside = tempfile.mktemp(suffix=".cpp", dir="/tmp")
        try:
            with open(outside, "w") as f:
                f.write("int x;")
            with self.assertRaises(RuntimeError) as ctx:
                self._request("lsp_read_definition", {
                    "project_id": self._project_id,
                    "file_path": outside,
                    "line": 0,
                    "character": 0,
                })
            self.assertIn("not under project root", str(ctx.exception))
        finally:
            if os.path.exists(outside):
                os.unlink(outside)

    def test_unknown_project_id_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._request("lsp_read_definition", {
                "project_id": "nonexistent1234",
                "file_path": self._files["main.cpp"],
                "line": 0,
                "character": 0,
            })
        self.assertIn("Unknown project", str(ctx.exception))

    # test_force_register_restarts_lsp moved to DaemonForceRegisterTest


class DaemonForceRegisterTest(unittest.TestCase):
    """Tests that mutate daemon state (force restart) — isolated with own daemon."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_clangd()
        cls._log_handler = logging.StreamHandler()
        cls._log_handler.setLevel(logging.DEBUG)
        logging.getLogger("karellen_lsp_mcp").addHandler(cls._log_handler)
        cls._tmpdir = tempfile.mkdtemp(prefix="karellen-lsp-mcp-itest-force-")
        cls._files = _create_project(cls._tmpdir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmpdir, ignore_errors=True)
        logging.getLogger("karellen_lsp_mcp").removeHandler(cls._log_handler)

    def setUp(self):
        self._loop = asyncio.new_event_loop()
        self._helper = _DaemonTestHelper()
        self._loop.run_until_complete(self._helper.start())
        result = self._loop.run_until_complete(
            self._helper.request("register_project", {
                "project_path": self._tmpdir,
                "language": "cpp",
                "lsp_command": ["clangd", "--background-index"],
                "build_info": {"compile_commands_dir": self._tmpdir},
            }))
        self._project_id = result["project_id"]

    def tearDown(self):
        try:
            self._loop.run_until_complete(
                self._helper.request("deregister_project",
                                     {"project_id": self._project_id}))
        except Exception:
            pass
        self._loop.run_until_complete(self._helper.stop())
        self._loop.close()

    def test_force_register_restarts_lsp(self):
        result = self._loop.run_until_complete(
            self._helper.request("register_project", {
                "project_path": self._tmpdir,
                "language": "cpp",
                "build_info": {"compile_commands_dir": self._tmpdir},
                "force": True,
            }))
        self.assertEqual(result["project_id"], self._project_id)

        projects = self._loop.run_until_complete(
            self._helper.request("list_projects", {}))
        self.assertEqual(len(projects), 1)
        self.assertIn(projects[0]["status"], ("indexing", "ready"))


# ---------------------------------------------------------------------------
# Multi-frontend tests
# ---------------------------------------------------------------------------

class DaemonMultiFrontendTest(unittest.TestCase):
    """Test multiple frontends connecting to the same daemon."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_clangd()
        cls._log_handler = logging.StreamHandler()
        cls._log_handler.setLevel(logging.DEBUG)
        logging.getLogger("karellen_lsp_mcp").addHandler(cls._log_handler)
        cls._tmpdir = tempfile.mkdtemp(prefix="karellen-lsp-mcp-itest-multi-")
        cls._files = _create_project(cls._tmpdir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmpdir, ignore_errors=True)
        logging.getLogger("karellen_lsp_mcp").removeHandler(cls._log_handler)

    def test_two_frontends_share_project(self):
        loop = asyncio.new_event_loop()

        async def run():
            daemon_dir = tempfile.mkdtemp(prefix="karellen-lsp-mcp-daemon-")
            daemon = Daemon(idle_timeout=5, runtime_dir=daemon_dir)
            daemon_task = asyncio.create_task(daemon.run())

            sock_path = os.path.join(daemon_dir, "daemon.sock")
            for _ in range(50):
                if os.path.exists(sock_path):
                    break
                await asyncio.sleep(0.1)

            r1, w1 = await asyncio.open_unix_connection(sock_path)
            r2, w2 = await asyncio.open_unix_connection(sock_path)

            async def req(reader, writer, msg_id, method, params=None):
                msg = {"id": msg_id, "method": method, "params": params or {}}
                _write_message(writer, msg)
                await writer.drain()
                resp = await asyncio.wait_for(_read_message(reader), timeout=60)
                if "error" in resp:
                    raise RuntimeError(resp["error"]["message"])
                return resp.get("result")

            # Frontend 1 registers
            res1 = await req(r1, w1, 1, "register_project", {
                "project_path": self._tmpdir,
                "language": "cpp",
                "build_info": {"compile_commands_dir": self._tmpdir},
            })
            pid = res1["project_id"]

            # Frontend 2 registers same project
            res2 = await req(r2, w2, 1, "register_project", {
                "project_path": self._tmpdir,
                "language": "cpp",
            })
            self.assertEqual(res2["project_id"], pid)

            # Check refcount = 2
            projects = await req(r1, w1, 2, "list_projects")
            self.assertEqual(projects[0]["refcount"], 2)

            # Frontend 1 deregisters
            await req(r1, w1, 3, "deregister_project", {"project_id": pid})

            # Check refcount = 1
            projects = await req(r2, w2, 2, "list_projects")
            self.assertEqual(projects[0]["refcount"], 1)

            # Frontend 2 can still query
            result = await req(r2, w2, 3, "lsp_document_symbols", {
                "project_id": pid,
                "file_path": self._files["math_utils.cpp"],
            })
            names = _symbol_names(result["symbols"])
            self.assertIn("add", names)
            self.assertIn("subtract", names)

            # Frontend 2 deregisters
            await req(r2, w2, 4, "deregister_project", {"project_id": pid})

            # Project should be gone
            projects = await req(r1, w1, 4, "list_projects")
            self.assertEqual(len(projects), 0)

            w1.close()
            w2.close()
            daemon._shutdown_event.set()
            await asyncio.wait_for(daemon_task, timeout=10)
            shutil.rmtree(daemon_dir, ignore_errors=True)

        loop.run_until_complete(run())
        loop.close()

    def test_frontend_disconnect_deregisters(self):
        loop = asyncio.new_event_loop()

        async def run():
            daemon_dir = tempfile.mkdtemp(prefix="karellen-lsp-mcp-daemon-")
            daemon = Daemon(idle_timeout=5, runtime_dir=daemon_dir)
            daemon_task = asyncio.create_task(daemon.run())

            sock_path = os.path.join(daemon_dir, "daemon.sock")
            for _ in range(50):
                if os.path.exists(sock_path):
                    break
                await asyncio.sleep(0.1)

            r1, w1 = await asyncio.open_unix_connection(sock_path)
            msg = {"id": 1, "method": "register_project", "params": {
                "project_path": self._tmpdir,
                "language": "cpp",
                "build_info": {"compile_commands_dir": self._tmpdir},
            }}
            _write_message(w1, msg)
            await w1.drain()
            resp = await asyncio.wait_for(_read_message(r1), timeout=60)
            self.assertIn("project_id", resp["result"])

            projects = daemon.registry.list_projects()
            self.assertEqual(len(projects), 1)

            # Frontend disconnects abruptly
            w1.close()

            # Poll until deregistration propagates
            for _ in range(50):
                projects = daemon.registry.list_projects()
                if len(projects) == 0:
                    break
                await asyncio.sleep(0.1)

            projects = daemon.registry.list_projects()
            self.assertEqual(len(projects), 0)

            daemon._shutdown_event.set()
            await asyncio.wait_for(daemon_task, timeout=10)
            shutil.rmtree(daemon_dir, ignore_errors=True)

        loop.run_until_complete(run())
        loop.close()


if __name__ == "__main__":
    unittest.main()
