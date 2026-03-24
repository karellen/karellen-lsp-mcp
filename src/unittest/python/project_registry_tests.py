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

"""Unit tests for ProjectRegistry."""

import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from karellen_lsp_mcp.project_registry import (
    ProjectRegistry, ProjectRegistryError,
    _compute_project_id,
)
from karellen_lsp_mcp.lsp_adapter import (
    ClangdAdapter, JdtlsAdapter, get_adapter, get_supported_languages,
)


class ComputeProjectIdTest(unittest.TestCase):
    def test_deterministic(self):
        id1 = _compute_project_id("/home/user/project", "cpp")
        id2 = _compute_project_id("/home/user/project", "cpp")
        self.assertEqual(id1, id2)

    def test_different_for_different_languages(self):
        id_c = _compute_project_id("/home/user/project", "c")
        id_cpp = _compute_project_id("/home/user/project", "cpp")
        self.assertNotEqual(id_c, id_cpp)

    def test_different_for_different_paths(self):
        id1 = _compute_project_id("/home/user/project1", "cpp")
        id2 = _compute_project_id("/home/user/project2", "cpp")
        self.assertNotEqual(id1, id2)

    def test_length_is_16(self):
        project_id = _compute_project_id("/some/path", "c")
        self.assertEqual(len(project_id), 16)


class AdapterRegistryTest(unittest.TestCase):
    def test_clangd_registered_for_c_and_cpp(self):
        self.assertIsInstance(get_adapter("c"), ClangdAdapter)
        self.assertIsInstance(get_adapter("cpp"), ClangdAdapter)

    def test_jdtls_registered_for_java_kotlin(self):
        for lang in ("java", "kotlin"):
            self.assertIsInstance(get_adapter(lang), JdtlsAdapter)

    def test_unknown_language_returns_none(self):
        self.assertIsNone(get_adapter("brainfuck"))

    def test_supported_languages(self):
        langs = get_supported_languages()
        for expected in ("c", "cpp", "java", "kotlin"):
            self.assertIn(expected, langs)


class ClangdAdapterTest(unittest.TestCase):
    def setUp(self):
        self.adapter = ClangdAdapter()
        self._data_dir = tempfile.mkdtemp(prefix="karellen-lsp-mcp-test-data-")
        self._data_patch = unittest.mock.patch(
            "karellen_lsp_mcp.lsp_adapter._user_data_dir",
            return_value=self._data_dir)
        self._data_patch.start()

    def tearDown(self):
        self._data_patch.stop()
        shutil.rmtree(self._data_dir, ignore_errors=True)

    def test_default_command(self):
        config = self.adapter.configure("/project", "c")
        self.assertEqual(config.command, ["clangd", "--background-index"])

    def test_custom_command(self):
        config = self.adapter.configure("/project", "c",
                                        lsp_command=["my-clangd", "--flag"])
        self.assertEqual(config.command,
                         ["my-clangd", "--flag", "--background-index"])

    def test_compile_commands_dir_copied_to_managed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Source compile_commands.json
            src_dir = os.path.join(tmpdir, "build")
            os.makedirs(src_dir)
            with open(os.path.join(src_dir, "compile_commands.json"), "w") as f:
                f.write("[]")
            config = self.adapter.configure(tmpdir, "cpp",
                                            build_info={"compile_commands_dir": src_dir})
            # Should point to managed dir, not the original
            cc_arg = [a for a in config.command if "--compile-commands-dir" in a]
            self.assertEqual(len(cc_arg), 1)
            managed_path = cc_arg[0].split("=", 1)[1]
            self.assertIn("karellen-lsp-mcp", managed_path)
            self.assertTrue(os.path.isfile(
                os.path.join(managed_path, "compile_commands.json")))

    def test_build_dir_with_compile_commands(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "compile_commands.json"), "w") as f:
                f.write("[]")
            config = self.adapter.configure(tmpdir, "c",
                                            build_info={"build_dir": tmpdir})
            cc_arg = [a for a in config.command if "--compile-commands-dir" in a]
            self.assertEqual(len(cc_arg), 1)
            managed_path = cc_arg[0].split("=", 1)[1]
            self.assertTrue(os.path.isfile(
                os.path.join(managed_path, "compile_commands.json")))

    def test_build_dir_without_compile_commands(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self.adapter.configure(tmpdir, "c",
                                            build_info={"build_dir": tmpdir})
            cc_args = [a for a in config.command
                       if a.startswith("--compile-commands-dir")]
            self.assertEqual(cc_args, [])
            self.assertIn("--background-index", config.command)

    def test_root_uri_is_project_path(self):
        config = self.adapter.configure("/my/project", "cpp")
        self.assertIn("/my/project", config.root_uri)

    def test_detection_compile_commands_copied(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            build = os.path.join(tmpdir, "build")
            os.makedirs(build)
            with open(os.path.join(build, "compile_commands.json"), "w") as f:
                f.write('[{"file": "main.c", "command": "cc main.c"}]')
            config = self.adapter.configure(
                tmpdir, "c",
                detection_details={"compile_commands_dir": build})
            cc_arg = [a for a in config.command if "--compile-commands-dir" in a]
            self.assertEqual(len(cc_arg), 1)
            managed_path = cc_arg[0].split("=", 1)[1]
            # Verify the copy has the right content
            with open(os.path.join(managed_path, "compile_commands.json")) as f:
                self.assertIn("main.c", f.read())


class JdtlsAdapterTest(unittest.TestCase):
    def setUp(self):
        self.adapter = JdtlsAdapter()
        self._data_dir = tempfile.mkdtemp(prefix="karellen-lsp-mcp-test-data-")
        self._data_patch = unittest.mock.patch(
            "karellen_lsp_mcp.lsp_adapter._user_data_dir",
            return_value=self._data_dir)
        self._data_patch.start()

    def tearDown(self):
        self._data_patch.stop()
        shutil.rmtree(self._data_dir, ignore_errors=True)

    @unittest.mock.patch("karellen_lsp_mcp.lsp_adapter._shutil.which", return_value="/usr/bin/jdtls")
    def test_default_command(self, mock_which):
        config = self.adapter.configure("/project", "java")
        self.assertEqual(config.command[0], "/usr/bin/jdtls")
        self.assertIn("-data", config.command)

    @unittest.mock.patch("karellen_lsp_mcp.lsp_adapter._shutil.which", return_value="/usr/bin/jdtls")
    def test_custom_command_with_data(self, mock_which):
        config = self.adapter.configure("/project", "java",
                                        lsp_command=["jdtls", "-data", "/custom"])
        self.assertIn("-data", config.command)
        self.assertIn("/custom", config.command)
        # Should not add a second -data
        self.assertEqual(config.command.count("-data"), 1)

    @unittest.mock.patch("karellen_lsp_mcp.lsp_adapter._shutil.which", return_value=None)
    def test_no_jdtls_on_path_raises(self, mock_which):
        with self.assertRaises(ValueError) as ctx:
            self.adapter.configure("/project", "java")
        self.assertIn("jdtls not found", str(ctx.exception))

    @unittest.mock.patch("karellen_lsp_mcp.lsp_adapter._shutil.which", return_value="/usr/bin/jdtls")
    def test_project_root_override(self, mock_which):
        config = self.adapter.configure("/project/submodule", "java",
                                        build_info={"project_root": "/project"})
        self.assertIn("/project", config.root_uri)
        self.assertNotIn("submodule", config.root_uri)

    @unittest.mock.patch("karellen_lsp_mcp.lsp_adapter._shutil.which", return_value="/usr/bin/jdtls")
    def test_works_for_kotlin(self, mock_which):
        config = self.adapter.configure("/project", "kotlin")
        self.assertEqual(config.command[0], "/usr/bin/jdtls")


class ProjectRegistryRegisterTest(unittest.TestCase):
    def test_register_nonexistent_path(self):
        registry = ProjectRegistry()
        loop = asyncio.new_event_loop()
        with self.assertRaises(ProjectRegistryError) as ctx:
            loop.run_until_complete(registry.register("/nonexistent/path/xyz", "c"))
        self.assertIn("does not exist", str(ctx.exception))
        loop.close()

    @patch("karellen_lsp_mcp.project_registry.LspClient")
    def test_register_and_refcount(self, mock_lsp_class):
        mock_client = AsyncMock()
        mock_client.state_name = "indexing"
        mock_lsp_class.return_value = mock_client

        registry = ProjectRegistry()
        loop = asyncio.new_event_loop()

        with tempfile.TemporaryDirectory() as tmpdir:
            pid1 = loop.run_until_complete(registry.register(tmpdir, "c"))
            pid2 = loop.run_until_complete(registry.register(tmpdir, "c"))

            self.assertEqual(pid1, pid2)

            projects = registry.list_projects()
            self.assertEqual(len(projects), 1)
            self.assertEqual(projects[0]["refcount"], 2)

            # LSP start called only once
            mock_client.start.assert_called_once()
        loop.close()

    @patch("karellen_lsp_mcp.project_registry.LspClient")
    def test_deregister_refcount(self, mock_lsp_class):
        mock_client = AsyncMock()
        mock_client.state_name = "indexing"
        mock_lsp_class.return_value = mock_client

        registry = ProjectRegistry()
        loop = asyncio.new_event_loop()

        with tempfile.TemporaryDirectory() as tmpdir:
            pid = loop.run_until_complete(registry.register(tmpdir, "cpp"))
            loop.run_until_complete(registry.register(tmpdir, "cpp"))

            # Refcount=2, deregister once -> still alive
            loop.run_until_complete(registry.deregister(pid))
            projects = registry.list_projects()
            self.assertEqual(len(projects), 1)
            self.assertEqual(projects[0]["refcount"], 1)
            mock_client.stop.assert_not_called()

            # Deregister again -> removed
            loop.run_until_complete(registry.deregister(pid))
            projects = registry.list_projects()
            self.assertEqual(len(projects), 0)
            mock_client.stop.assert_called_once()
        loop.close()

    def test_deregister_unknown_project(self):
        registry = ProjectRegistry()
        loop = asyncio.new_event_loop()
        with self.assertRaises(ProjectRegistryError):
            loop.run_until_complete(registry.deregister("nonexistent"))
        loop.close()

    @patch("karellen_lsp_mcp.project_registry.LspClient")
    def test_force_register_restarts(self, mock_lsp_class):
        mock_client1 = AsyncMock()
        mock_client1.state_name = "indexing"
        mock_client2 = AsyncMock()
        mock_client2.state_name = "indexing"
        mock_lsp_class.side_effect = [mock_client1, mock_client2]

        registry = ProjectRegistry()
        loop = asyncio.new_event_loop()

        with tempfile.TemporaryDirectory() as tmpdir:
            pid1 = loop.run_until_complete(registry.register(tmpdir, "c"))
            pid2 = loop.run_until_complete(registry.register(tmpdir, "c", force=True))

            self.assertEqual(pid1, pid2)
            mock_client1.stop.assert_called_once()
            mock_client2.start.assert_called_once()

            projects = registry.list_projects()
            self.assertEqual(len(projects), 1)
            self.assertEqual(projects[0]["refcount"], 1)
        loop.close()


class ProjectRegistryValidateFilePathTest(unittest.TestCase):
    @patch("karellen_lsp_mcp.project_registry.LspClient")
    def test_validate_absolute_path_under_root(self, mock_lsp_class):
        mock_client = AsyncMock()
        mock_client.state_name = "indexing"
        mock_lsp_class.return_value = mock_client

        registry = ProjectRegistry()
        loop = asyncio.new_event_loop()

        with tempfile.TemporaryDirectory() as tmpdir:
            pid = loop.run_until_complete(registry.register(tmpdir, "c"))

            test_file = os.path.join(tmpdir, "test.c")
            with open(test_file, "w") as f:
                f.write("int main() {}")

            uri = registry.validate_file_path(pid, test_file)
            self.assertTrue(uri.startswith("file://"))
            self.assertIn(tmpdir, uri)
        loop.close()

    @patch("karellen_lsp_mcp.project_registry.LspClient")
    def test_validate_relative_path_rejected(self, mock_lsp_class):
        mock_client = AsyncMock()
        mock_client.state_name = "indexing"
        mock_lsp_class.return_value = mock_client

        registry = ProjectRegistry()
        loop = asyncio.new_event_loop()

        with tempfile.TemporaryDirectory() as tmpdir:
            pid = loop.run_until_complete(registry.register(tmpdir, "c"))

            with self.assertRaises(ProjectRegistryError) as ctx:
                registry.validate_file_path(pid, "relative/path.c")
            self.assertIn("must be absolute", str(ctx.exception))
        loop.close()

    @patch("karellen_lsp_mcp.project_registry.LspClient")
    def test_validate_path_outside_root_rejected(self, mock_lsp_class):
        mock_client = AsyncMock()
        mock_client.state_name = "indexing"
        mock_lsp_class.return_value = mock_client

        registry = ProjectRegistry()
        loop = asyncio.new_event_loop()

        with tempfile.TemporaryDirectory() as tmpdir1, \
             tempfile.TemporaryDirectory() as tmpdir2:
            pid = loop.run_until_complete(registry.register(tmpdir1, "c"))

            outside_file = os.path.join(tmpdir2, "outside.c")
            with open(outside_file, "w") as f:
                f.write("")

            with self.assertRaises(ProjectRegistryError) as ctx:
                registry.validate_file_path(pid, outside_file)
            self.assertIn("not under project root", str(ctx.exception))
        loop.close()


class ProjectRegistryConcurrencyTest(unittest.TestCase):
    @patch("karellen_lsp_mcp.project_registry.LspClient")
    def test_concurrent_register_same_project_starts_once(self, mock_lsp_class):
        """Two concurrent register calls for the same project must not leak LSP servers."""
        started = asyncio.Event()
        mock_client = AsyncMock()
        mock_client.state_name = "indexing"

        original_start = mock_client.start

        async def slow_start(*args, **kwargs):
            started.set()
            await asyncio.sleep(0.1)
            return await original_start(*args, **kwargs)

        mock_client.start = slow_start
        mock_lsp_class.return_value = mock_client

        registry = ProjectRegistry()

        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                pid1_task = asyncio.create_task(registry.register(tmpdir, "c"))
                pid2_task = asyncio.create_task(registry.register(tmpdir, "c"))
                pid1, pid2 = await asyncio.gather(pid1_task, pid2_task)
                self.assertEqual(pid1, pid2)
                projects = registry.list_projects()
                self.assertEqual(len(projects), 1)
                self.assertEqual(projects[0]["refcount"], 2)
                # LspClient should only be instantiated once
                self.assertEqual(mock_lsp_class.call_count, 1)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(run())
        loop.close()

    @patch("karellen_lsp_mcp.project_registry.LspClient")
    def test_concurrent_deregister_stops_once(self, mock_lsp_class):
        """Two concurrent deregister calls must not double-stop the LSP server."""
        mock_client = AsyncMock()
        mock_client.state_name = "indexing"
        mock_lsp_class.return_value = mock_client

        registry = ProjectRegistry()

        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                pid = await registry.register(tmpdir, "c")
                # refcount=1, two concurrent deregisters: one succeeds, one errors
                t1 = asyncio.create_task(registry.deregister(pid))
                t2 = asyncio.create_task(registry.deregister(pid))
                results = await asyncio.gather(t1, t2, return_exceptions=True)
                # Exactly one should succeed and one should raise
                errors = [r for r in results if isinstance(r, Exception)]
                successes = [r for r in results if not isinstance(r, Exception)]
                self.assertEqual(len(errors), 1)
                self.assertEqual(len(successes), 1)
                self.assertIsInstance(errors[0], ProjectRegistryError)
                mock_client.stop.assert_called_once()

        loop = asyncio.new_event_loop()
        loop.run_until_complete(run())
        loop.close()


class ProjectRegistryShutdownAllTest(unittest.TestCase):
    @patch("karellen_lsp_mcp.project_registry.LspClient")
    def test_shutdown_all(self, mock_lsp_class):
        mock_client1 = AsyncMock()
        mock_client1.state_name = "indexing"
        mock_client2 = AsyncMock()
        mock_client2.state_name = "indexing"
        mock_lsp_class.side_effect = [mock_client1, mock_client2]

        registry = ProjectRegistry()
        loop = asyncio.new_event_loop()

        with tempfile.TemporaryDirectory() as tmpdir1, \
             tempfile.TemporaryDirectory() as tmpdir2:
            loop.run_until_complete(registry.register(tmpdir1, "c"))
            loop.run_until_complete(registry.register(tmpdir2, "cpp"))

            self.assertEqual(len(registry.list_projects()), 2)

            loop.run_until_complete(registry.shutdown_all())
            self.assertEqual(len(registry.list_projects()), 0)
            mock_client1.stop.assert_called_once()
            mock_client2.stop.assert_called_once()
        loop.close()


if __name__ == "__main__":
    unittest.main()
