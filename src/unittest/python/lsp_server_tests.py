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

"""Unit tests for LSP proxy server and daemon proxy support."""

import unittest
import unittest.mock

from karellen_lsp_mcp.lsp_normalizer import LspNormalizer


class _TestNormalizer(LspNormalizer):
    """Normalizer for testing that applies a URI mapping dict."""

    def __init__(self, mapping, schemes=("jdt",)):
        super().__init__()
        self._mapping = mapping
        self._schemes = schemes

    @property
    def uri_schemes(self):
        return self._schemes

    def normalize_uri(self, uri):
        return self._mapping.get(uri, uri)


class NormalizeResponseUrisTest(unittest.TestCase):
    """Tests for LspNormalizer.normalize_response."""

    def _make_normalizer(self, mapping, schemes=("jdt",)):
        return _TestNormalizer(mapping, schemes)

    def test_none_result(self):
        normalizer = self._make_normalizer({})
        result = normalizer.normalize_response(None)
        self.assertIsNone(result)

    def test_no_schemes_passthrough(self):
        normalizer = self._make_normalizer({}, schemes=())
        result = [{"uri": "file:///test.c"}]
        normalizer.normalize_response(result)
        self.assertEqual(result[0]["uri"], "file:///test.c")

    def test_normalizes_uri_field(self):
        normalizer = self._make_normalizer({
            "jdt://foo": "jar:file:///foo.jar!/Foo.class",
            "jdt://bar": "jar:file:///bar.jar!/Bar.class",
        })
        result = [
            {"uri": "jdt://foo", "range": {"start": {"line": 0}}},
            {"uri": "jdt://bar", "range": {"start": {"line": 5}}},
        ]
        normalizer.normalize_response(result)
        self.assertEqual(
            result[0]["uri"], "jar:file:///foo.jar!/Foo.class")
        self.assertEqual(
            result[1]["uri"], "jar:file:///bar.jar!/Bar.class")

    def test_normalizes_target_uri_field(self):
        normalizer = self._make_normalizer({
            "jdt://impl": "jar:file:///impl.jar!/Impl.class",
        })
        result = [{"targetUri": "jdt://impl",
                   "targetRange": {"start": {"line": 0}}}]
        normalizer.normalize_response(result)
        self.assertEqual(
            result[0]["targetUri"],
            "jar:file:///impl.jar!/Impl.class")

    def test_records_reverse_map(self):
        normalizer = self._make_normalizer({
            "jdt://foo": "jar:file:///foo.jar!/Foo.class",
        })
        result = [{"uri": "jdt://foo"}]
        normalizer.normalize_response(result)
        self.assertEqual(
            normalizer._uri_reverse_map[
                "jar:file:///foo.jar!/Foo.class"],
            "jdt://foo")

    def test_reverse_map_empty_for_unchanged(self):
        normalizer = self._make_normalizer({})  # no changes
        result = [{"uri": "file:///test.c"}]
        normalizer.normalize_response(result)
        self.assertEqual(normalizer._uri_reverse_map, {})

    def test_normalizes_nested_dicts(self):
        normalizer = self._make_normalizer({
            "jdt://inner": "jar:file:///inner.jar!/I.class",
        })
        result = [{"from": {"uri": "jdt://inner", "name": "test"},
                   "fromRanges": []}]
        normalizer.normalize_response(result)
        self.assertEqual(
            result[0]["from"]["uri"],
            "jar:file:///inner.jar!/I.class")
        self.assertIn(
            "jar:file:///inner.jar!/I.class",
            normalizer._uri_reverse_map)

    def test_handles_non_string_uri(self):
        normalizer = self._make_normalizer({})
        result = [{"uri": 42}]
        # Should not crash
        normalizer.normalize_response(result)
        self.assertEqual(result[0]["uri"], 42)

    def test_handles_dict_result(self):
        normalizer = self._make_normalizer({
            "jdt://x": "jar:file:///x.jar!/X.class",
        })
        result = {"contents": {"uri": "jdt://x"}}
        normalizer.normalize_response(result)
        self.assertEqual(
            result["contents"]["uri"],
            "jar:file:///x.jar!/X.class")

    def test_normalizes_uris_in_string_values(self):
        normalizer = self._make_normalizer({
            "jdt://contents/lib.jar/pkg/Cls.class": (
                "jar:file:///lib.jar!/pkg/Cls.class"),
        })
        result = {
            "contents": {
                "kind": "markdown",
                "value": (
                    "Source: [lib.jar]"
                    "(jdt://contents/lib.jar/pkg/Cls.class)"
                ),
            }
        }
        normalizer.normalize_response(result)
        self.assertIn(
            "jar:file:///lib.jar!/pkg/Cls.class",
            result["contents"]["value"])
        self.assertNotIn(
            "jdt://", result["contents"]["value"])
        self.assertIn(
            "jar:file:///lib.jar!/pkg/Cls.class",
            normalizer._uri_reverse_map)

    def test_normalizes_uris_in_string_list_items(self):
        normalizer = self._make_normalizer({
            "jdt://contents/a.jar/A.class": (
                "jar:file:///a.jar!/A.class"),
        })
        result = [
            "See jdt://contents/a.jar/A.class for details",
            "No URI here",
        ]
        normalizer.normalize_response(result)
        self.assertIn("jar:file:///a.jar!/A.class", result[0])
        self.assertEqual(result[1], "No URI here")

    def test_string_uri_reverse_map_recorded(self):
        normalizer = self._make_normalizer({
            "jdt://x": "jar:file:///x.jar!/X.class",
        })
        result = {"value": "link: jdt://x end"}
        normalizer.normalize_response(result)
        self.assertEqual(
            normalizer._uri_reverse_map[
                "jar:file:///x.jar!/X.class"],
            "jdt://x")


class ProjectRegistryRoutingTest(unittest.TestCase):
    """Tests for ProjectRegistry.find_project_for_file."""

    def setUp(self):
        from karellen_lsp_mcp.project_registry import (
            ProjectRegistry, _ProjectEntry)
        self.registry = ProjectRegistry()
        self._ProjectEntry = _ProjectEntry

    def _add_project(self, path, language, project_id):
        entry = self._ProjectEntry(
            project_id, path, language, None, None)
        entry.client = unittest.mock.MagicMock()
        self.registry._projects[project_id] = entry

    def test_route_single_language(self):
        self._add_project("/home/user/project", "c", "proj_c")
        entry = self.registry.find_project_for_file(
            "/home/user/project/src/main.c")
        self.assertEqual(entry.project_id, "proj_c")

    def test_route_polyglot_by_extension(self):
        self._add_project("/home/user/project", "c", "proj_c")
        self._add_project("/home/user/project", "java", "proj_java")
        c_entry = self.registry.find_project_for_file(
            "/home/user/project/src/main.c")
        self.assertEqual(c_entry.project_id, "proj_c")
        java_entry = self.registry.find_project_for_file(
            "/home/user/project/src/Foo.java")
        self.assertEqual(java_entry.project_id, "proj_java")

    def test_route_cpp_canonicalizes_to_c(self):
        self._add_project("/home/user/project", "c", "proj_c")
        entry = self.registry.find_project_for_file(
            "/home/user/project/src/main.cpp")
        self.assertEqual(entry.project_id, "proj_c")

    def test_route_h_maps_to_c(self):
        self._add_project("/home/user/project", "c", "proj_c")
        entry = self.registry.find_project_for_file(
            "/home/user/project/include/foo.h")
        self.assertEqual(entry.project_id, "proj_c")

    def test_route_outside_project_raises(self):
        from karellen_lsp_mcp.project_registry import (
            ProjectRegistryError)
        self._add_project("/home/user/project", "c", "proj_c")
        with self.assertRaises(ProjectRegistryError):
            self.registry.find_project_for_file(
                "/other/path/main.c")

    def test_route_no_projects_raises(self):
        from karellen_lsp_mcp.project_registry import (
            ProjectRegistryError)
        with self.assertRaises(ProjectRegistryError):
            self.registry.find_project_for_file("/any/file.c")

    def test_route_longest_path_wins(self):
        self._add_project(
            "/home/user/workspace", "c", "proj_outer")
        self._add_project(
            "/home/user/workspace/subproject", "c", "proj_inner")
        inner = self.registry.find_project_for_file(
            "/home/user/workspace/subproject/src/x.c")
        self.assertEqual(inner.project_id, "proj_inner")
        outer = self.registry.find_project_for_file(
            "/home/user/workspace/other/y.c")
        self.assertEqual(outer.project_id, "proj_outer")

    def test_same_language_different_paths(self):
        self._add_project(
            "/home/user/proj_a", "c", "proj_a_c")
        self._add_project(
            "/home/user/proj_b", "c", "proj_b_c")
        a = self.registry.find_project_for_file(
            "/home/user/proj_a/src/main.c")
        self.assertEqual(a.project_id, "proj_a_c")
        b = self.registry.find_project_for_file(
            "/home/user/proj_b/src/main.c")
        self.assertEqual(b.project_id, "proj_b_c")

    def test_single_backend_ignores_extension(self):
        self._add_project(
            "/home/user/project", "java", "proj_java")
        entry = self.registry.find_project_for_file(
            "/home/user/project/README.md")
        self.assertEqual(entry.project_id, "proj_java")

    def test_find_projects_under_path(self):
        self._add_project("/home/user/ws/proj_a", "c", "id_a")
        self._add_project("/home/user/ws/proj_b", "java", "id_b")
        self._add_project("/other/proj", "c", "id_other")
        entries = self.registry.find_projects_under_path(
            "/home/user/ws")
        ids = {e.project_id for e in entries}
        self.assertEqual(ids, {"id_a", "id_b"})

    def test_find_projects_empty(self):
        entries = self.registry.find_projects_under_path(
            "/nonexistent")
        self.assertEqual(entries, [])


class NormalizerDenormalizeTest(unittest.TestCase):
    """Tests for LspNormalizer.denormalize_params."""

    def _make_normalizer(self, mapping, schemes=("jdt",)):
        return _TestNormalizer(mapping, schemes)

    def _populated_normalizer(self):
        """Create a normalizer with pre-populated reverse map."""
        n = self._make_normalizer({
            "jdt://foo": "jar:file:///foo.jar!/Foo.class",
            "jdt://cls": "jar:file:///lib.jar!/Cls.class",
        })
        # Populate reverse map by normalizing a response
        n.normalize_response([
            {"uri": "jdt://foo"},
            {"uri": "jdt://cls"},
        ])
        return n

    def test_denormalize_replaces_cached_uri(self):
        n = self._populated_normalizer()
        params = {
            "item": {
                "uri": "jar:file:///foo.jar!/Foo.class",
                "name": "Foo",
                "kind": 5,
                "data": {"some": "opaque"},
            }
        }
        n.denormalize_params(params)
        self.assertEqual(params["item"]["uri"], "jdt://foo")
        # Data preserved unchanged
        self.assertEqual(params["item"]["data"],
                         {"some": "opaque"})

    def test_denormalize_noop_when_not_cached(self):
        n = self._make_normalizer({})
        params = {"item": {"uri": "file:///test.c", "name": "foo"}}
        n.denormalize_params(params)
        self.assertEqual(params["item"]["uri"], "file:///test.c")

    def test_denormalize_no_item(self):
        n = self._populated_normalizer()
        params = {"query": "hello"}
        n.denormalize_params(params)
        self.assertEqual(params, {"query": "hello"})

    def test_denormalize_string_values(self):
        n = self._populated_normalizer()
        params = {
            "item": {
                "uri": "jar:file:///lib.jar!/Cls.class",
                "detail": "See jar:file:///lib.jar!/Cls.class",
            }
        }
        n.denormalize_params(params)
        self.assertEqual(params["item"]["uri"], "jdt://cls")
        self.assertEqual(
            params["item"]["detail"], "See jdt://cls")

    def test_denormalize_noop_empty_reverse_map(self):
        n = self._make_normalizer({})
        params = {"item": {"uri": "file:///x"}}
        result = n.denormalize_params(params)
        self.assertIs(result, params)

    def test_normalize_then_denormalize_roundtrip(self):
        n = self._make_normalizer({
            "jdt://a": "jar:file:///a.jar!/A.class",
            "jdt://b": "jar:file:///b.jar!/B.class",
        })
        # Normalize a response (populates reverse map)
        response = [
            {"uri": "jdt://a", "name": "A"},
            {"uri": "jdt://b", "name": "B"},
        ]
        n.normalize_response(response)
        self.assertEqual(
            response[0]["uri"], "jar:file:///a.jar!/A.class")

        # Denormalize params (roundtrip)
        params = {"item": {"uri": "jar:file:///a.jar!/A.class"}}
        n.denormalize_params(params)
        self.assertEqual(params["item"]["uri"], "jdt://a")
