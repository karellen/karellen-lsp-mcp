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

"""Unit tests for project autodetection."""

import json
import os
import tempfile
import unittest

from karellen_lsp_mcp.detector import (
    detect_project,
    _detect_build_system,
    _detect_cpp_build_system,
    _find_compile_commands,
    _find_cmake_build_dirs,
    _read_cmake_cache_vars,
    _detect_cpp_language,
    _find_gradle_root,
    _parse_gradle_settings_modules,
    _parse_maven_modules,
    _scan_kotlin_files,
    _scan_source_roots,
    _merge_details_by_credibility,
    _read_jetbrains_metadata,
    _read_eclipse_metadata,
    _read_vscode_metadata,
    _read_all_ide_metadata,
    _is_pybuilder_project,
    _parse_setup_cfg,
    _detect_venv,
    _detect_src_layout,
    _find_cargo_workspace_root,
    _cargo_toml_has_workspace,
    _detect_rust_toolchain,
    IdeMetadata,
    CppDetector,
    PythonDetector,
    RustDetector,
    TIER_IDE_BUILD_SYNC,
    TIER_IDE_PROJECT,
    TIER_IDE_WORKSPACE,
    JavaKotlinDetector,
)


def _touch(path):
    """Create a file and its parent directories."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("")


def _write(path, content):
    """Write content to a file, creating parent directories."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# Build system detection
# ---------------------------------------------------------------------------

class DetectBuildSystemTest(unittest.TestCase):
    def test_detects_gradle_with_build_gradle(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "build.gradle"))
            _touch(os.path.join(d, "settings.gradle"))
            self.assertEqual(_detect_build_system(d), "gradle")

    def test_detects_gradle_kts(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "build.gradle.kts"))
            _touch(os.path.join(d, "settings.gradle.kts"))
            self.assertEqual(_detect_build_system(d), "gradle")

    def test_detects_maven(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "pom.xml"))
            self.assertEqual(_detect_build_system(d), "maven")

    def test_detects_ant(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "build.xml"))
            self.assertEqual(_detect_build_system(d), "ant")

    def test_detects_eclipse(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, ".classpath"))
            self.assertEqual(_detect_build_system(d), "eclipse")

    def test_gradle_priority_over_maven(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "build.gradle.kts"))
            _touch(os.path.join(d, "pom.xml"))
            self.assertEqual(_detect_build_system(d), "gradle")

    def test_maven_priority_over_ant(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "pom.xml"))
            _touch(os.path.join(d, "build.xml"))
            self.assertEqual(_detect_build_system(d), "maven")

    def test_no_detection_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(_detect_build_system(d))


# ---------------------------------------------------------------------------
# Gradle root walk-up
# ---------------------------------------------------------------------------

class FindGradleRootTest(unittest.TestCase):
    def test_settings_in_parent(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "settings.gradle"))
            child = os.path.join(d, "submodule")
            os.makedirs(child)
            _touch(os.path.join(child, "build.gradle"))
            self.assertEqual(_find_gradle_root(child), d)

    def test_settings_kts_in_parent(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "settings.gradle.kts"))
            child = os.path.join(d, "module", "submodule")
            os.makedirs(child, exist_ok=True)
            self.assertEqual(_find_gradle_root(child), d)

    def test_no_settings_above_returns_project_path(self):
        with tempfile.TemporaryDirectory() as d:
            child = os.path.join(d, "submodule")
            os.makedirs(child)
            self.assertEqual(_find_gradle_root(child), child)

    def test_settings_at_project_path_returns_project_path(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "settings.gradle"))
            self.assertEqual(_find_gradle_root(d), d)


# ---------------------------------------------------------------------------
# Gradle settings module parsing
# ---------------------------------------------------------------------------

class ParseGradleSettingsModulesTest(unittest.TestCase):
    def test_parses_kotlin_dsl_includes(self):
        with tempfile.TemporaryDirectory() as d:
            mod_a = os.path.join(d, "module-a")
            mod_b = os.path.join(d, "module-b")
            os.makedirs(mod_a)
            os.makedirs(mod_b)
            _write(os.path.join(d, "settings.gradle.kts"),
                   'rootProject.name = "myproject"\n'
                   'include("module-a")\n'
                   'include("module-b")\n')
            result = _parse_gradle_settings_modules(d)
            self.assertEqual(len(result), 2)
            self.assertIn(mod_a, result)
            self.assertIn(mod_b, result)

    def test_parses_groovy_includes(self):
        with tempfile.TemporaryDirectory() as d:
            mod_a = os.path.join(d, "mod.a")
            mod_b = os.path.join(d, "mod.b")
            os.makedirs(mod_a)
            os.makedirs(mod_b)
            _write(os.path.join(d, "settings.gradle"),
                   "rootProject.name = 'myproject'\n"
                   "include 'mod.a'\n"
                   "include 'mod.b'\n")
            result = _parse_gradle_settings_modules(d)
            self.assertEqual(len(result), 2)

    def test_skips_nonexistent_modules(self):
        with tempfile.TemporaryDirectory() as d:
            mod_a = os.path.join(d, "exists")
            os.makedirs(mod_a)
            _write(os.path.join(d, "settings.gradle.kts"),
                   'include("exists")\n'
                   'include("does-not-exist")\n')
            result = _parse_gradle_settings_modules(d)
            self.assertEqual(len(result), 1)
            self.assertIn(mod_a, result)

    def test_no_settings_file(self):
        with tempfile.TemporaryDirectory() as d:
            result = _parse_gradle_settings_modules(d)
            self.assertIsNone(result)

    def test_colon_separated_module_names(self):
        with tempfile.TemporaryDirectory() as d:
            mod = os.path.join(d, "sub", "module")
            os.makedirs(mod)
            _write(os.path.join(d, "settings.gradle.kts"),
                   'include(":sub:module")\n')
            result = _parse_gradle_settings_modules(d)
            self.assertEqual(len(result), 1)
            self.assertIn(mod, result)


# ---------------------------------------------------------------------------
# Maven module parsing
# ---------------------------------------------------------------------------

class ParseMavenModulesTest(unittest.TestCase):
    def test_parses_modules_from_pom(self):
        with tempfile.TemporaryDirectory() as d:
            mod_a = os.path.join(d, "module-a")
            mod_b = os.path.join(d, "module-b")
            os.makedirs(mod_a)
            os.makedirs(mod_b)
            _write(os.path.join(d, "pom.xml"),
                   '<?xml version="1.0"?>\n'
                   '<project><modules>\n'
                   '  <module>module-a</module>\n'
                   '  <module>module-b</module>\n'
                   '</modules></project>\n')
            result = _parse_maven_modules(d)
            self.assertEqual(len(result), 2)
            self.assertIn(mod_a, result)
            self.assertIn(mod_b, result)

    def test_parses_modules_with_namespace(self):
        with tempfile.TemporaryDirectory() as d:
            mod = os.path.join(d, "core")
            os.makedirs(mod)
            _write(os.path.join(d, "pom.xml"),
                   '<?xml version="1.0"?>\n'
                   '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
                   '  <modules><module>core</module></modules>\n'
                   '</project>\n')
            result = _parse_maven_modules(d)
            self.assertEqual(len(result), 1)

    def test_no_pom(self):
        with tempfile.TemporaryDirectory() as d:
            result = _parse_maven_modules(d)
            self.assertIsNone(result)

    def test_pom_without_modules(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "pom.xml"),
                   '<?xml version="1.0"?>\n'
                   '<project><groupId>com.example</groupId></project>\n')
            result = _parse_maven_modules(d)
            self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Kotlin file scanning
# ---------------------------------------------------------------------------

class ScanKotlinFilesTest(unittest.TestCase):
    def test_finds_kt_files_in_src(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "src", "main", "kotlin", "Foo.kt"))
            _touch(os.path.join(d, "src", "main", "kotlin", "Bar.kt"))
            self.assertTrue(_scan_kotlin_files(d))

    def test_finds_kts_files(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "src", "main", "kotlin", "build.gradle.kts"))
            self.assertTrue(_scan_kotlin_files(d))

    def test_no_kotlin_files(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "src", "main", "java", "Foo.java"))
            _touch(os.path.join(d, "src", "main", "java", "Bar.java"))
            self.assertFalse(_scan_kotlin_files(d))

    def test_no_src_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(_scan_kotlin_files(d))

    def test_depth_limit(self):
        with tempfile.TemporaryDirectory() as d:
            # File at depth 4 (beyond max_depth=3)
            deep_path = os.path.join(d, "src", "a", "b", "c", "d", "Foo.kt")
            _touch(deep_path)
            self.assertFalse(_scan_kotlin_files(d, max_depth=3))


# ---------------------------------------------------------------------------
# JetBrains metadata reader
# ---------------------------------------------------------------------------

class ReadJetbrainsMetadataTest(unittest.TestCase):
    def test_no_idea_dir(self):
        with tempfile.TemporaryDirectory() as d:
            result = _read_jetbrains_metadata(d)
            self.assertEqual(result, [])

    def test_misc_xml(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".idea", "misc.xml"), '''<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="ProjectRootManager" version="2"
             languageLevel="JDK_17" default="true"
             project-jdk-name="azul-17" project-jdk-type="JavaSDK" />
</project>''')
            result = _read_jetbrains_metadata(d)
            misc_entries = [m for m in result
                            if m.raw.get("source") == "misc.xml"]
            self.assertEqual(len(misc_entries), 1)
            meta = misc_entries[0]
            self.assertEqual(meta.java_sdk, "azul-17")
            self.assertEqual(meta.java_language_level, "JDK_17")
            self.assertEqual(meta.tier, TIER_IDE_PROJECT)

    def test_misc_xml_python_sdk_ignored(self):
        """A Python SDK in misc.xml must not be treated as a Java SDK."""
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".idea", "misc.xml"), '''<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="ProjectRootManager" version="2"
             project-jdk-name="Python 3.9 virtualenv at ~/.pyenv/versions/pyb-3.9"
             project-jdk-type="Python SDK" />
</project>''')
            result = _read_jetbrains_metadata(d)
            misc_entries = [m for m in result
                            if m.raw.get("source") == "misc.xml"]
            self.assertEqual(len(misc_entries), 0)

    def test_misc_xml_no_jdk_type_accepted(self):
        """Absent project-jdk-type should be accepted (backwards compat)."""
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".idea", "misc.xml"), '''<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="ProjectRootManager" version="2"
             project-jdk-name="corretto-17" />
</project>''')
            result = _read_jetbrains_metadata(d)
            misc_entries = [m for m in result
                            if m.raw.get("source") == "misc.xml"]
            self.assertEqual(len(misc_entries), 1)
            self.assertEqual(misc_entries[0].java_sdk, "corretto-17")

    def test_compiler_xml(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".idea", "compiler.xml"), '''<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="CompilerConfiguration">
    <bytecodeTargetLevel target="17" />
  </component>
</project>''')
            result = _read_jetbrains_metadata(d)
            compiler_entries = [m for m in result
                                if m.raw.get("source") == "compiler.xml"]
            self.assertEqual(len(compiler_entries), 1)
            meta = compiler_entries[0]
            self.assertEqual(meta.bytecode_target, "17")
            self.assertEqual(meta.tier, TIER_IDE_BUILD_SYNC)

    def test_gradle_xml(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".idea", "gradle.xml"), '''<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="GradleSettings">
    <option name="linkedExternalProjectsSettings">
      <GradleProjectSettings>
        <option name="externalProjectPath" value="$PROJECT_DIR$" />
        <option name="gradleJvm" value="azul-17" />
        <option name="modules">
          <set>
            <option value="$PROJECT_DIR$" />
            <option value="$PROJECT_DIR$/submodule-a" />
          </set>
        </option>
      </GradleProjectSettings>
    </option>
  </component>
</project>''')
            result = _read_jetbrains_metadata(d)
            gradle_entries = [m for m in result
                              if m.raw.get("source") == "gradle.xml"]
            self.assertEqual(len(gradle_entries), 1)
            meta = gradle_entries[0]
            self.assertEqual(meta.gradle_jvm, "azul-17")
            self.assertEqual(len(meta.gradle_modules), 2)
            self.assertIn(d, meta.gradle_modules)
            self.assertIn(os.path.join(d, "submodule-a"), meta.gradle_modules)
            self.assertEqual(meta.tier, TIER_IDE_BUILD_SYNC)

    def test_kotlinc_xml(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".idea", "kotlinc.xml"), '''<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="KotlinJpsPluginSettings">
    <option name="version" value="1.9.23" />
  </component>
</project>''')
            result = _read_jetbrains_metadata(d)
            kotlinc_entries = [m for m in result
                               if m.raw.get("source") == "kotlinc.xml"]
            self.assertEqual(len(kotlinc_entries), 1)
            meta = kotlinc_entries[0]
            self.assertEqual(meta.kotlin_version, "1.9.23")
            self.assertEqual(meta.tier, TIER_IDE_PROJECT)

    def test_kotlinc_xml_with_language_and_api_version(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".idea", "kotlinc.xml"), '''<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="Kotlin2JvmCompilerArguments">
    <option name="languageVersion" value="2.0" />
    <option name="apiVersion" value="2.0" />
  </component>
</project>''')
            result = _read_jetbrains_metadata(d)
            kotlinc_entries = [m for m in result
                               if m.raw.get("source") == "kotlinc.xml"]
            self.assertEqual(len(kotlinc_entries), 1)
            meta = kotlinc_entries[0]
            self.assertEqual(meta.kotlin_version, "2.0")
            self.assertEqual(meta.kotlin_api_version, "2.0")

    def test_modules_xml_with_iml_source_roots(self):
        with tempfile.TemporaryDirectory() as d:
            iml_dir = os.path.join(d, ".idea", "modules", "mod-a")
            os.makedirs(iml_dir, exist_ok=True)
            iml_path = os.path.join(iml_dir, "project.mod-a.main.iml")
            _write(iml_path, '''<?xml version="1.0" encoding="UTF-8"?>
<module version="4">
  <component name="AdditionalModuleElements">
    <content url="file://$MODULE_DIR$/../../../mod-a/src/main/java">
      <sourceFolder url="file://$MODULE_DIR$/../../../mod-a/src/main/java"
                    isTestSource="false" />
    </content>
    <content url="file://$MODULE_DIR$/../../../mod-a/src/test/java">
      <sourceFolder url="file://$MODULE_DIR$/../../../mod-a/src/test/java"
                    isTestSource="true" />
    </content>
  </component>
</module>''')
            _write(os.path.join(d, ".idea", "modules.xml"),
                   '''<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="ProjectModuleManager">
    <modules>
      <module fileurl="file://$PROJECT_DIR$/.idea/modules/mod-a/project.mod-a.main.iml"
              filepath="$PROJECT_DIR$/.idea/modules/mod-a/project.mod-a.main.iml" />
    </modules>
  </component>
</project>''')
            result = _read_jetbrains_metadata(d)
            src_entries = [m for m in result
                           if m.raw.get("source") == "modules.xml+iml"]
            self.assertEqual(len(src_entries), 1)
            meta = src_entries[0]
            # Only non-test source root should be included
            self.assertEqual(len(meta.source_roots), 1)
            self.assertTrue(meta.source_roots[0].endswith(
                os.path.join("mod-a", "src", "main", "java")))

    def test_malformed_xml_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".idea", "misc.xml"), "not valid xml <><>")
            result = _read_jetbrains_metadata(d)
            misc_entries = [m for m in result
                            if m.raw.get("source") == "misc.xml"]
            self.assertEqual(len(misc_entries), 0)


# ---------------------------------------------------------------------------
# Eclipse metadata reader
# ---------------------------------------------------------------------------

class ReadEclipseMetadataTest(unittest.TestCase):
    def test_no_eclipse_files(self):
        with tempfile.TemporaryDirectory() as d:
            result = _read_eclipse_metadata(d)
            self.assertEqual(result, [])

    def test_classpath_with_source_and_jre(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".classpath"), '''<?xml version="1.0" encoding="UTF-8"?>
<classpath>
  <classpathentry kind="src" path="src/main/java"/>
  <classpathentry kind="src" path="src/test/java"/>
  <classpathentry kind="con"
    path="org.eclipse.jdt.launching.JRE_CONTAINER/org.eclipse.jdt.internal.debug.ui.launcher.StandardVMType/JavaSE-17"/>
  <classpathentry kind="output" path="bin"/>
</classpath>''')
            result = _read_eclipse_metadata(d)
            self.assertEqual(len(result), 1)
            meta = result[0]
            self.assertEqual(meta.java_sdk, "17")
            self.assertEqual(len(meta.source_roots), 2)
            self.assertEqual(meta.tier, TIER_IDE_WORKSPACE)

    def test_eclipse_prefs(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".settings", "org.eclipse.jdt.core.prefs"),
                   "eclipse.preferences.version=1\n"
                   "org.eclipse.jdt.core.compiler.compliance=17\n"
                   "org.eclipse.jdt.core.compiler.source=17\n")
            result = _read_eclipse_metadata(d)
            self.assertEqual(len(result), 1)
            meta = result[0]
            self.assertEqual(meta.java_language_level, "17")
            self.assertEqual(meta.tier, TIER_IDE_WORKSPACE)


# ---------------------------------------------------------------------------
# VS Code metadata reader
# ---------------------------------------------------------------------------

class ReadVscodeMetadataTest(unittest.TestCase):
    def test_no_vscode_dir(self):
        with tempfile.TemporaryDirectory() as d:
            result = _read_vscode_metadata(d)
            self.assertEqual(result, [])

    def test_java_home_setting(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".vscode", "settings.json"),
                   json.dumps({"java.jdt.ls.java.home": "/usr/lib/jvm/java-17"}))
            result = _read_vscode_metadata(d)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].java_sdk, "/usr/lib/jvm/java-17")

    def test_runtimes_with_default(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".vscode", "settings.json"), json.dumps({
                "java.configuration.runtimes": [
                    {"name": "JavaSE-11", "path": "/usr/lib/jvm/java-11"},
                    {"name": "JavaSE-17", "path": "/usr/lib/jvm/java-17",
                     "default": True},
                ]
            }))
            result = _read_vscode_metadata(d)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].java_sdk, "/usr/lib/jvm/java-17")

    def test_no_java_settings(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".vscode", "settings.json"),
                   json.dumps({"editor.fontSize": 14}))
            result = _read_vscode_metadata(d)
            self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# Source root scanning
# ---------------------------------------------------------------------------

class ScanSourceRootsTest(unittest.TestCase):
    def test_finds_conventional_source_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "src", "main", "java"))
            os.makedirs(os.path.join(d, "src", "main", "kotlin"))
            os.makedirs(os.path.join(d, "src", "test", "java"))
            os.makedirs(os.path.join(d, "src", "main", "resources"))
            result = _scan_source_roots(d)
            self.assertEqual(len(result["source_roots"]), 2)
            self.assertEqual(len(result["test_source_roots"]), 1)
            self.assertEqual(len(result["resource_roots"]), 1)

    def test_no_source_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            result = _scan_source_roots(d)
            self.assertEqual(result["source_roots"], [])
            self.assertEqual(result["test_source_roots"], [])
            self.assertEqual(result["resource_roots"], [])

    def test_multi_module_scanning(self):
        with tempfile.TemporaryDirectory() as d:
            mod_a = os.path.join(d, "module-a")
            mod_b = os.path.join(d, "module-b")
            os.makedirs(os.path.join(mod_a, "src", "main", "java"))
            os.makedirs(os.path.join(mod_a, "src", "main", "kotlin"))
            os.makedirs(os.path.join(mod_b, "src", "main", "java"))
            os.makedirs(os.path.join(mod_b, "src", "test", "java"))
            result = _scan_source_roots(d, modules=[mod_a, mod_b])
            # 3 main source roots: mod_a/java, mod_a/kotlin, mod_b/java
            self.assertEqual(len(result["source_roots"]), 3)
            # 1 test source root: mod_b/test/java
            self.assertEqual(len(result["test_source_roots"]), 1)

    def test_skips_nonexistent_modules(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "src", "main", "java"))
            result = _scan_source_roots(d, modules=["/nonexistent/module"])
            self.assertEqual(len(result["source_roots"]), 1)


# ---------------------------------------------------------------------------
# Credibility merge
# ---------------------------------------------------------------------------

class MergeDetailsByCredibilityTest(unittest.TestCase):
    def test_higher_tier_wins(self):
        meta_tier2 = IdeMetadata(ide="jetbrains", tier=TIER_IDE_BUILD_SYNC,
                                 bytecode_target="17")
        meta_tier2.raw["source"] = "compiler.xml"
        meta_tier3 = IdeMetadata(ide="jetbrains", tier=TIER_IDE_PROJECT,
                                 java_sdk="azul-21",
                                 java_language_level="JDK_21")
        meta_tier3.raw["source"] = "misc.xml"
        # Sorted by tier (tier 2 first)
        details = _merge_details_by_credibility([meta_tier2, meta_tier3])
        self.assertEqual(details["bytecode_target"], "17")
        self.assertEqual(details["bytecode_target_source"], "compiler.xml")
        self.assertEqual(details["bytecode_target_tier"], TIER_IDE_BUILD_SYNC)
        self.assertEqual(details["java_sdk"], "azul-21")
        self.assertEqual(details["java_language_level"], "JDK_21")

    def test_first_value_wins_at_same_tier(self):
        meta1 = IdeMetadata(ide="jetbrains", tier=TIER_IDE_BUILD_SYNC,
                            bytecode_target="17")
        meta1.raw["source"] = "compiler.xml"
        meta2 = IdeMetadata(ide="jetbrains", tier=TIER_IDE_BUILD_SYNC,
                            bytecode_target="11")
        meta2.raw["source"] = "compiler2.xml"
        details = _merge_details_by_credibility([meta1, meta2])
        self.assertEqual(details["bytecode_target"], "17")

    def test_empty_metadata(self):
        details = _merge_details_by_credibility([])
        self.assertEqual(details, {})

    def test_kotlin_fields_merged(self):
        meta = IdeMetadata(ide="jetbrains", tier=TIER_IDE_PROJECT,
                           kotlin_version="1.9.23",
                           kotlin_api_version="1.9")
        meta.raw["source"] = "kotlinc.xml"
        details = _merge_details_by_credibility([meta])
        self.assertEqual(details["kotlin_version"], "1.9.23")
        self.assertEqual(details["kotlin_api_version"], "1.9")

    def test_gradle_fields_merged(self):
        meta = IdeMetadata(ide="jetbrains", tier=TIER_IDE_BUILD_SYNC,
                           gradle_jvm="azul-17",
                           gradle_modules=["/proj", "/proj/mod-a"])
        meta.raw["source"] = "gradle.xml"
        details = _merge_details_by_credibility([meta])
        self.assertEqual(details["gradle_jvm"], "azul-17")
        self.assertEqual(len(details["gradle_modules"]), 2)


# ---------------------------------------------------------------------------
# All IDE metadata reader
# ---------------------------------------------------------------------------

class ReadAllIdeMetadataTest(unittest.TestCase):
    def test_no_ide_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            result = _read_all_ide_metadata(d)
            self.assertEqual(result, [])

    def test_multiple_sources_sorted_by_tier(self):
        with tempfile.TemporaryDirectory() as d:
            # JetBrains misc.xml (tier 3)
            _write(os.path.join(d, ".idea", "misc.xml"), '''<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="ProjectRootManager" project-jdk-name="17" languageLevel="JDK_17" />
</project>''')
            # Eclipse .classpath (tier 4)
            _write(os.path.join(d, ".classpath"), '''<?xml version="1.0" encoding="UTF-8"?>
<classpath>
  <classpathentry kind="src" path="src"/>
  <classpathentry kind="con"
    path="org.eclipse.jdt.launching.JRE_CONTAINER/org.eclipse.jdt.internal.debug.ui.launcher.StandardVMType/JavaSE-11"/>
</classpath>''')
            result = _read_all_ide_metadata(d)
            self.assertGreaterEqual(len(result), 2)
            # First entry should have tier <= second entry
            self.assertLessEqual(result[0].tier, result[1].tier)


# ---------------------------------------------------------------------------
# JavaKotlinDetector
# ---------------------------------------------------------------------------

class JavaKotlinDetectorTest(unittest.TestCase):
    def setUp(self):
        self.detector = JavaKotlinDetector()

    def test_detects_gradle_project(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "build.gradle"))
            _touch(os.path.join(d, "settings.gradle"))
            result = self.detector.detect(d, [])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].language, "java")
            self.assertEqual(result[0].build_system, "gradle")
            self.assertEqual(result[0].confidence, "high")

    def test_detects_maven_project(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "pom.xml"))
            result = self.detector.detect(d, [])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].language, "java")
            self.assertEqual(result[0].build_system, "maven")

    def test_detects_kotlin_from_files(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "build.gradle.kts"))
            _touch(os.path.join(d, "src", "main", "kotlin", "Foo.kt"))
            _touch(os.path.join(d, "src", "main", "kotlin", "Bar.kt"))
            result = self.detector.detect(d, [])
            self.assertEqual(len(result), 1)
            self.assertTrue(result[0].details["kotlin_detected"])

    def test_detects_kotlin_from_ide_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "build.gradle"))
            meta = IdeMetadata(ide="jetbrains", tier=TIER_IDE_PROJECT,
                               kotlin_version="1.9.23")
            result = self.detector.detect(d, [meta])
            self.assertEqual(len(result), 1)
            self.assertTrue(result[0].details["kotlin_detected"])
            self.assertEqual(result[0].details["kotlin_version"], "1.9.23")

    def test_no_detection_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            result = self.detector.detect(d, [])
            self.assertEqual(result, [])

    def test_ide_metadata_only_detection(self):
        """If no build markers but IDE metadata indicates Java, detect as unknown build system."""
        with tempfile.TemporaryDirectory() as d:
            meta = IdeMetadata(ide="jetbrains", tier=TIER_IDE_PROJECT,
                               java_sdk="azul-17",
                               java_language_level="JDK_17")
            result = self.detector.detect(d, [meta])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].language, "java")
            self.assertEqual(result[0].build_system, "unknown")
            self.assertEqual(result[0].details["java_sdk"], "azul-17")

    def test_gradle_root_walk_up_sets_build_info(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "settings.gradle"))
            child = os.path.join(d, "submodule")
            os.makedirs(child)
            _touch(os.path.join(child, "build.gradle"))
            result = self.detector.detect(child, [])
            self.assertEqual(len(result), 1)
            self.assertIsNotNone(result[0].build_info)
            self.assertEqual(result[0].build_info["project_root"], d)

    def test_merges_ide_metadata_with_credibility(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "build.gradle"))
            # Tier 2: bytecode target 17
            meta_t2 = IdeMetadata(ide="jetbrains", tier=TIER_IDE_BUILD_SYNC,
                                  bytecode_target="17")
            meta_t2.raw["source"] = "compiler.xml"
            # Tier 3: SDK says 21 (stale IDE setting)
            meta_t3 = IdeMetadata(ide="jetbrains", tier=TIER_IDE_PROJECT,
                                  java_sdk="azul-21",
                                  java_language_level="JDK_21")
            meta_t3.raw["source"] = "misc.xml"
            result = self.detector.detect(d, [meta_t2, meta_t3])
            self.assertEqual(len(result), 1)
            details = result[0].details
            # Bytecode target from tier 2
            self.assertEqual(details["bytecode_target"], "17")
            self.assertEqual(details["bytecode_target_tier"], TIER_IDE_BUILD_SYNC)
            # SDK from tier 3 (no tier 2 value for this field)
            self.assertEqual(details["java_sdk"], "azul-21")


# ---------------------------------------------------------------------------
# End-to-end detect_project
# ---------------------------------------------------------------------------

class DetectProjectTest(unittest.TestCase):
    def test_gradle_with_jetbrains_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "build.gradle.kts"))
            _touch(os.path.join(d, "settings.gradle.kts"))
            _write(os.path.join(d, ".idea", "misc.xml"), '''<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="ProjectRootManager" project-jdk-name="azul-17"
             languageLevel="JDK_17" project-jdk-type="JavaSDK" />
</project>''')
            _write(os.path.join(d, ".idea", "kotlinc.xml"), '''<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="KotlinJpsPluginSettings">
    <option name="version" value="1.9.23" />
  </component>
</project>''')
            result = detect_project(d)
            self.assertEqual(result.project_path, os.path.realpath(d))
            self.assertEqual(len(result.languages), 1)
            lang = result.languages[0]
            self.assertEqual(lang.language, "java")
            self.assertEqual(lang.build_system, "gradle")
            self.assertTrue(lang.details["kotlin_detected"])
            self.assertEqual(lang.details["kotlin_version"], "1.9.23")
            self.assertEqual(lang.details["java_sdk"], "azul-17")
            # IDE metadata should also be populated
            self.assertGreater(len(result.ide_metadata), 0)

    def test_nonexistent_path(self):
        result = detect_project("/nonexistent/path/that/does/not/exist")
        self.assertEqual(len(result.languages), 0)

    def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as d:
            result = detect_project(d)
            self.assertEqual(len(result.languages), 0)
            self.assertEqual(len(result.ide_metadata), 0)

    def test_maven_project(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "pom.xml"))
            result = detect_project(d)
            self.assertEqual(len(result.languages), 1)
            self.assertEqual(result.languages[0].build_system, "maven")

    def test_cmake_project_with_compile_commands(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "CMakeLists.txt"))
            _write(os.path.join(d, "main.cpp"), "int main() {}")
            _write(os.path.join(d, "util.cpp"), "void util() {}")
            build_dir = os.path.join(d, "build")
            os.makedirs(build_dir)
            _write(os.path.join(build_dir, "compile_commands.json"), "[]")
            result = detect_project(d)
            self.assertTrue(any(lang.language in ("c", "cpp")
                                for lang in result.languages))
            cpp_lang = [lang for lang in result.languages
                        if lang.language in ("c", "cpp")][0]
            self.assertEqual(cpp_lang.build_system, "cmake")
            self.assertEqual(cpp_lang.confidence, "high")
            self.assertEqual(cpp_lang.build_info["compile_commands_dir"],
                             build_dir)


# ---------------------------------------------------------------------------
# C/C++ Detector
# ---------------------------------------------------------------------------

class DetectCppBuildSystemTest(unittest.TestCase):
    def test_detects_cmake(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "CMakeLists.txt"))
            self.assertEqual(_detect_cpp_build_system(d), "cmake")

    def test_detects_meson(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "meson.build"))
            self.assertEqual(_detect_cpp_build_system(d), "meson")

    def test_detects_autotools(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "configure.ac"))
            self.assertEqual(_detect_cpp_build_system(d), "autotools")

    def test_detects_bazel(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "MODULE.bazel"))
            self.assertEqual(_detect_cpp_build_system(d), "bazel")

    def test_detects_plain_makefile(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "Makefile"))
            self.assertEqual(_detect_cpp_build_system(d), "make")

    def test_cmake_generated_makefile_not_make(self):
        """Makefile + CMakeCache.txt = CMake-generated, not plain Make."""
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "CMakeLists.txt"))
            _touch(os.path.join(d, "Makefile"))
            _touch(os.path.join(d, "CMakeCache.txt"))
            self.assertEqual(_detect_cpp_build_system(d), "cmake")

    def test_cmake_priority_over_makefile(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "CMakeLists.txt"))
            _touch(os.path.join(d, "Makefile"))
            self.assertEqual(_detect_cpp_build_system(d), "cmake")

    def test_no_detection_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(_detect_cpp_build_system(d))


class FindCompileCommandsTest(unittest.TestCase):
    def test_finds_in_root(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "compile_commands.json"), "[]")
            self.assertEqual(_find_compile_commands(d), d)

    def test_finds_in_build_dir(self):
        with tempfile.TemporaryDirectory() as d:
            build = os.path.join(d, "build")
            os.makedirs(build)
            _write(os.path.join(build, "compile_commands.json"), "[]")
            self.assertEqual(_find_compile_commands(d), build)

    def test_finds_in_cmake_build_debug(self):
        with tempfile.TemporaryDirectory() as d:
            build = os.path.join(d, "cmake-build-debug")
            os.makedirs(build)
            _write(os.path.join(build, "compile_commands.json"), "[]")
            self.assertEqual(_find_compile_commands(d), build)

    def test_not_found(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(_find_compile_commands(d))


class FindCmakeBuildDirsTest(unittest.TestCase):
    def test_finds_in_tree_build(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "CMakeCache.txt"))
            dirs = _find_cmake_build_dirs(d)
            self.assertEqual(dirs, [d])

    def test_finds_out_of_tree_build(self):
        with tempfile.TemporaryDirectory() as d:
            build = os.path.join(d, "build")
            os.makedirs(build)
            _touch(os.path.join(build, "CMakeCache.txt"))
            dirs = _find_cmake_build_dirs(d)
            self.assertEqual(dirs, [build])

    def test_finds_multiple_build_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("build", "cmake-build-debug"):
                bd = os.path.join(d, name)
                os.makedirs(bd)
                _touch(os.path.join(bd, "CMakeCache.txt"))
            dirs = _find_cmake_build_dirs(d)
            self.assertEqual(len(dirs), 2)

    def test_no_build_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(_find_cmake_build_dirs(d), [])


class ReadCmakeCacheVarsTest(unittest.TestCase):
    def test_reads_vars(self):
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "CMakeCache.txt")
            _write(cache,
                   "# comment line\n"
                   "//help text\n"
                   "CMAKE_C_COMPILER:FILEPATH=/usr/bin/gcc\n"
                   "CMAKE_CXX_COMPILER:FILEPATH=/usr/bin/g++\n"
                   "CMAKE_BUILD_TYPE:STRING=RelWithDebInfo\n"
                   "OTHER_VAR:BOOL=ON\n")
            result = _read_cmake_cache_vars(
                cache, ("CMAKE_C_COMPILER", "CMAKE_CXX_COMPILER",
                        "CMAKE_BUILD_TYPE"))
            self.assertEqual(result["CMAKE_C_COMPILER"], "/usr/bin/gcc")
            self.assertEqual(result["CMAKE_CXX_COMPILER"], "/usr/bin/g++")
            self.assertEqual(result["CMAKE_BUILD_TYPE"], "RelWithDebInfo")
            self.assertNotIn("OTHER_VAR", result)

    def test_nonexistent_file(self):
        result = _read_cmake_cache_vars("/nonexistent", ("FOO",))
        self.assertEqual(result, {})


class DetectCppLanguageTest(unittest.TestCase):
    def test_cpp_files(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "main.cpp"))
            _touch(os.path.join(d, "util.hpp"))
            self.assertEqual(_detect_cpp_language(d), "cpp")

    def test_c_only(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "main.c"))
            _touch(os.path.join(d, "util.h"))
            self.assertEqual(_detect_cpp_language(d), "c")

    def test_mixed_c_and_cpp(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "main.c"))
            _touch(os.path.join(d, "wrapper.cpp"))
            self.assertEqual(_detect_cpp_language(d), "cpp")

    def test_no_source_files(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "readme.md"))
            self.assertIsNone(_detect_cpp_language(d))

    def test_scans_src_subdir(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "src", "lib.cc"))
            _touch(os.path.join(d, "src", "api.hh"))
            self.assertEqual(_detect_cpp_language(d), "cpp")


class CppDetectorTest(unittest.TestCase):
    def setUp(self):
        self.detector = CppDetector()

    def test_cmake_project_with_compile_commands(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "CMakeLists.txt"))
            _write(os.path.join(d, "main.cpp"), "int main() {}")
            _write(os.path.join(d, "lib.cpp"), "void lib() {}")
            build = os.path.join(d, "build")
            os.makedirs(build)
            _write(os.path.join(build, "compile_commands.json"), "[]")
            result = self.detector.detect(d, [])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].language, "cpp")
            self.assertEqual(result[0].build_system, "cmake")
            self.assertEqual(result[0].confidence, "high")
            self.assertEqual(result[0].build_info["compile_commands_dir"], build)
            self.assertEqual(result[0].details["build_system"], "cmake")

    def test_cmake_project_without_compile_commands(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "CMakeLists.txt"))
            _write(os.path.join(d, "main.c"), "int main() {}")
            _write(os.path.join(d, "util.c"), "void util() {}")
            result = self.detector.detect(d, [])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].language, "c")
            self.assertEqual(result[0].build_system, "cmake")
            self.assertEqual(result[0].confidence, "medium")

    def test_cmake_with_cache(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "CMakeLists.txt"))
            _touch(os.path.join(d, "main.cpp"))
            build = os.path.join(d, "build")
            os.makedirs(build)
            _write(os.path.join(build, "CMakeCache.txt"),
                   "CMAKE_C_COMPILER:FILEPATH=/usr/bin/gcc\n"
                   "CMAKE_CXX_COMPILER:FILEPATH=/usr/bin/g++\n"
                   "CMAKE_BUILD_TYPE:STRING=Debug\n")
            result = self.detector.detect(d, [])
            self.assertEqual(len(result), 1)
            details = result[0].details
            self.assertEqual(details["c_compiler"], "/usr/bin/gcc")
            self.assertEqual(details["cxx_compiler"], "/usr/bin/g++")
            self.assertEqual(details["cmake_build_type"], "Debug")
            self.assertIn(build, details["cmake_build_dirs"])

    def test_meson_project(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "meson.build"))
            _write(os.path.join(d, "main.c"), "int main() {}")
            _write(os.path.join(d, "lib.c"), "void lib() {}")
            result = self.detector.detect(d, [])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].build_system, "meson")
            self.assertEqual(result[0].details["build_system"], "meson")

    def test_autotools_project(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "configure.ac"))
            _write(os.path.join(d, "main.c"), "int main() {}")
            result = self.detector.detect(d, [])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].build_system, "autotools")
            self.assertEqual(result[0].details["build_system"], "autotools")

    def test_compile_flags_txt_detected(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "CMakeLists.txt"))
            _write(os.path.join(d, "main.cpp"), "int main() {}")
            _write(os.path.join(d, "compile_flags.txt"), "-std=c++17\n-Wall\n")
            result = self.detector.detect(d, [])
            self.assertTrue(result[0].details.get("compile_flags_txt"))

    def test_clangd_config_detected(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "CMakeLists.txt"))
            _write(os.path.join(d, "main.cpp"), "int main() {}")
            _write(os.path.join(d, ".clangd"), "CompileFlags:\n  Add: [-Wall]\n")
            result = self.detector.detect(d, [])
            self.assertTrue(result[0].details.get("clangd_config"))

    def test_no_detection_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            result = self.detector.detect(d, [])
            self.assertEqual(result, [])

    def test_standalone_compile_commands_no_build_system(self):
        """compile_commands.json without any build system marker still detects."""
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "compile_commands.json"), "[]")
            _write(os.path.join(d, "main.c"), "int main() {}")
            _write(os.path.join(d, "lib.c"), "void lib() {}")
            result = self.detector.detect(d, [])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].build_system, "unknown")
            self.assertEqual(result[0].confidence, "high")
            self.assertEqual(result[0].details["build_system"], "unknown")


# ---------------------------------------------------------------------------
# Python detector helpers
# ---------------------------------------------------------------------------

class IsPybuilderProjectTest(unittest.TestCase):
    def test_detects_pybuilder_from_import(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.py"),
                   "from pybuilder.core import use_plugin\n"
                   "use_plugin('python.core')\n")
            self.assertTrue(_is_pybuilder_project(d))

    def test_detects_pybuilder_import_statement(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.py"),
                   "import pybuilder\n")
            self.assertTrue(_is_pybuilder_project(d))

    def test_rejects_non_pybuilder_build_py(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.py"),
                   "import os\nos.system('make')\n")
            self.assertFalse(_is_pybuilder_project(d))

    def test_no_build_py(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(_is_pybuilder_project(d))

    def test_comment_before_import(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.py"),
                   "# Build configuration\n"
                   "from pybuilder.core import use_plugin\n")
            self.assertTrue(_is_pybuilder_project(d))


class ParseSetupCfgTest(unittest.TestCase):
    def test_extracts_python_requires(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "setup.cfg"),
                   "[options]\n"
                   "python_requires = >=3.8\n"
                   "packages = find:\n")
            result = _parse_setup_cfg(d)
            self.assertEqual(result["python_requires"], ">=3.8")
            self.assertEqual(result["packages"], "find:")

    def test_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(_parse_setup_cfg(d))

    def test_empty_options(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "setup.cfg"),
                   "[metadata]\nname = myproject\n")
            self.assertIsNone(_parse_setup_cfg(d))


class DetectVenvTest(unittest.TestCase):
    def test_detects_dot_venv_with_pyvenv_cfg(self):
        with tempfile.TemporaryDirectory() as d:
            venv_dir = os.path.join(d, ".venv")
            os.makedirs(os.path.join(venv_dir, "bin"))
            _write(os.path.join(venv_dir, "pyvenv.cfg"),
                   "home = /usr/bin\n"
                   "version = 3.11.5\n")
            _write(os.path.join(venv_dir, "bin", "python"), "")
            result = _detect_venv(d)
            self.assertEqual(result["venv_path"], venv_dir)
            self.assertEqual(result["venv_version"], "3.11.5")
            self.assertIn("venv_python", result)

    def test_detects_venv_dir(self):
        with tempfile.TemporaryDirectory() as d:
            venv_dir = os.path.join(d, "venv")
            os.makedirs(os.path.join(venv_dir, "bin"))
            _write(os.path.join(venv_dir, "pyvenv.cfg"),
                   "home = /usr/bin\n"
                   "version = 3.10.0\n")
            result = _detect_venv(d)
            self.assertEqual(result["venv_path"], venv_dir)

    def test_detects_conda_environment(self):
        with tempfile.TemporaryDirectory() as d:
            venv_dir = os.path.join(d, ".venv")
            os.makedirs(os.path.join(venv_dir, "conda-meta"))
            os.makedirs(os.path.join(venv_dir, "bin"))
            _write(os.path.join(venv_dir, "conda-meta", "history"),
                   "# history\n")
            _write(os.path.join(venv_dir, "bin", "python"), "")
            result = _detect_venv(d)
            self.assertTrue(result.get("is_conda"))
            self.assertEqual(result["venv_path"], venv_dir)

    def test_no_venv(self):
        with tempfile.TemporaryDirectory() as d:
            result = _detect_venv(d)
            self.assertEqual(result, {})

    def test_dot_venv_preferred_over_venv(self):
        with tempfile.TemporaryDirectory() as d:
            for name in (".venv", "venv"):
                vdir = os.path.join(d, name)
                os.makedirs(os.path.join(vdir, "bin"))
                _write(os.path.join(vdir, "pyvenv.cfg"),
                       "home = /usr/bin\nversion = 3.11.0\n")
            result = _detect_venv(d)
            self.assertEqual(result["venv_path"],
                             os.path.join(d, ".venv"))


class DetectSrcLayoutTest(unittest.TestCase):
    def test_src_with_package(self):
        with tempfile.TemporaryDirectory() as d:
            pkg = os.path.join(d, "src", "mypackage")
            os.makedirs(pkg)
            _touch(os.path.join(pkg, "__init__.py"))
            self.assertTrue(_detect_src_layout(d))

    def test_src_with_py_file(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "src"))
            _write(os.path.join(d, "src", "main.py"), "print('hello')")
            self.assertTrue(_detect_src_layout(d))

    def test_no_src_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(_detect_src_layout(d))

    def test_empty_src_dir(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "src"))
            self.assertFalse(_detect_src_layout(d))


# ---------------------------------------------------------------------------
# Python detector
# ---------------------------------------------------------------------------

class PythonDetectorTest(unittest.TestCase):
    def setUp(self):
        self.detector = PythonDetector()

    def test_detects_pyproject_toml(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "pyproject.toml"))
            result = self.detector.detect(d, [])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].language, "python")
            self.assertEqual(result[0].confidence, "high")

    def test_detects_setup_py(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "setup.py"),
                   "from setuptools import setup\nsetup(name='test')\n")
            result = self.detector.detect(d, [])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].language, "python")
            self.assertEqual(result[0].build_system, "setuptools")

    def test_detects_setup_cfg(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "setup.cfg"),
                   "[metadata]\nname = myproject\n"
                   "[options]\npython_requires = >=3.8\n")
            result = self.detector.detect(d, [])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].build_system, "setuptools")
            self.assertEqual(result[0].details["python_requires"], ">=3.8")

    def test_detects_pipfile(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "Pipfile"))
            result = self.detector.detect(d, [])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].build_system, "pipenv")
            self.assertEqual(result[0].confidence, "medium")

    def test_detects_requirements_txt(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "requirements.txt"), "flask>=2.0\nrequests\n")
            result = self.detector.detect(d, [])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].build_system, "pip")
            self.assertEqual(result[0].confidence, "medium")

    def test_detects_pybuilder(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.py"),
                   "from pybuilder.core import use_plugin\n"
                   "use_plugin('python.core')\n")
            result = self.detector.detect(d, [])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].build_system, "pybuilder")
            self.assertEqual(result[0].confidence, "high")

    def test_pybuilder_priority_over_pyproject(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "build.py"),
                   "from pybuilder.core import use_plugin\n")
            _touch(os.path.join(d, "pyproject.toml"))
            result = self.detector.detect(d, [])
            self.assertEqual(result[0].build_system, "pybuilder")

    def test_no_detection_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            result = self.detector.detect(d, [])
            self.assertEqual(result, [])

    def test_detects_venv(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "pyproject.toml"))
            venv_dir = os.path.join(d, ".venv")
            os.makedirs(os.path.join(venv_dir, "bin"))
            _write(os.path.join(venv_dir, "pyvenv.cfg"),
                   "home = /usr/bin\nversion = 3.11.0\n")
            result = self.detector.detect(d, [])
            self.assertEqual(result[0].details["venv_path"], venv_dir)

    def test_detects_pyrightconfig(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "pyproject.toml"))
            _write(os.path.join(d, "pyrightconfig.json"), "{}")
            result = self.detector.detect(d, [])
            self.assertTrue(result[0].details.get("pyrightconfig"))

    def test_detects_src_layout(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "pyproject.toml"))
            pkg = os.path.join(d, "src", "mypackage")
            os.makedirs(pkg)
            _touch(os.path.join(pkg, "__init__.py"))
            result = self.detector.detect(d, [])
            self.assertTrue(result[0].details.get("src_layout"))

    def test_setup_py_recorded_in_details(self):
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, "pyproject.toml"))
            _write(os.path.join(d, "setup.py"), "from setuptools import setup\n")
            result = self.detector.detect(d, [])
            self.assertTrue(result[0].details.get("has_setup_py"))


# ---------------------------------------------------------------------------
# Rust detector helpers
# ---------------------------------------------------------------------------

class CargoTomlHasWorkspaceTest(unittest.TestCase):
    def test_detects_workspace_section(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "Cargo.toml"),
                   "[workspace]\nmembers = [\"crate-a\", \"crate-b\"]\n")
            self.assertTrue(_cargo_toml_has_workspace(
                os.path.join(d, "Cargo.toml")))

    def test_no_workspace_section(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "Cargo.toml"),
                   "[package]\nname = \"my-crate\"\nversion = \"0.1.0\"\n")
            self.assertFalse(_cargo_toml_has_workspace(
                os.path.join(d, "Cargo.toml")))


class FindCargoWorkspaceRootTest(unittest.TestCase):
    def test_finds_workspace_root_above(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "Cargo.toml"),
                   "[workspace]\nmembers = [\"crates/a\"]\n")
            sub = os.path.join(d, "crates", "a")
            os.makedirs(sub)
            _write(os.path.join(sub, "Cargo.toml"),
                   "[package]\nname = \"a\"\n")
            result = _find_cargo_workspace_root(sub)
            self.assertEqual(result, d)

    def test_returns_self_when_no_workspace_above(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "Cargo.toml"),
                   "[package]\nname = \"standalone\"\n")
            result = _find_cargo_workspace_root(d)
            self.assertEqual(result, d)


class DetectRustToolchainTest(unittest.TestCase):
    def test_plain_toolchain_file(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "rust-toolchain"), "nightly\n")
            self.assertEqual(_detect_rust_toolchain(d), "nightly")

    def test_no_toolchain(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(_detect_rust_toolchain(d))


# ---------------------------------------------------------------------------
# Rust detector
# ---------------------------------------------------------------------------

class RustDetectorTest(unittest.TestCase):
    def setUp(self):
        self.detector = RustDetector()

    def test_detects_cargo_toml(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "Cargo.toml"),
                   "[package]\nname = \"my-crate\"\nversion = \"0.1.0\"\n")
            result = self.detector.detect(d, [])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].language, "rust")
            self.assertEqual(result[0].build_system, "cargo")
            self.assertEqual(result[0].confidence, "high")

    def test_no_detection_without_cargo_toml(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "main.rs"), "fn main() {}")
            result = self.detector.detect(d, [])
            self.assertEqual(result, [])

    def test_detects_build_rs(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "Cargo.toml"),
                   "[package]\nname = \"my-crate\"\n")
            _write(os.path.join(d, "build.rs"),
                   "fn main() { println!(\"cargo:rerun-if-changed=build.rs\"); }")
            result = self.detector.detect(d, [])
            self.assertTrue(result[0].details.get("has_build_rs"))

    def test_detects_rust_toolchain(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "Cargo.toml"),
                   "[package]\nname = \"my-crate\"\n")
            _write(os.path.join(d, "rust-toolchain"), "stable\n")
            result = self.detector.detect(d, [])
            self.assertEqual(result[0].details["rust_toolchain"], "stable")

    def test_detects_workspace_root(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "Cargo.toml"),
                   "[workspace]\nmembers = [\"crates/a\"]\n")
            sub = os.path.join(d, "crates", "a")
            os.makedirs(sub)
            _write(os.path.join(sub, "Cargo.toml"),
                   "[package]\nname = \"a\"\n")
            result = self.detector.detect(sub, [])
            self.assertEqual(result[0].details["workspace_root"], d)
