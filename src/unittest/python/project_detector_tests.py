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

"""Unit tests for project_detector."""

import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from karellen_lsp_mcp.project_detector import (
    DetectionSource,
    LanguageDetection,
    detect_project,
    _detect_clion,
    _detect_vscode_cpp,
    _detect_cmake_presets,
    _detect_clangd_config,
    _detect_compile_commands,
    _detect_compile_flags,
    _detect_intellij_jvm,
    _detect_eclipse_jvm,
    _detect_vscode_jvm,
    _detect_gradle,
    _detect_maven,
    _merge_detections,
    _parse_jdk_level,
    _scan_for_kotlin_sources,
)


class ParseJdkLevelTest(unittest.TestCase):
    def test_jdk_21(self):
        self.assertEqual(_parse_jdk_level("JDK_21"), "21")

    def test_jdk_17(self):
        self.assertEqual(_parse_jdk_level("JDK_17"), "17")

    def test_jdk_1_8(self):
        self.assertEqual(_parse_jdk_level("JDK_1_8"), "1.8")

    def test_jdk_1_5(self):
        self.assertEqual(_parse_jdk_level("JDK_1_5"), "1.5")

    def test_none(self):
        self.assertIsNone(_parse_jdk_level(None))

    def test_empty(self):
        self.assertIsNone(_parse_jdk_level(""))


# ---------------------------------------------------------------------------
# C/C++ IDE metadata detectors
# ---------------------------------------------------------------------------

class DetectClionTest(unittest.TestCase):
    def test_valid_cmake_xml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            idea_dir = os.path.join(tmpdir, ".idea")
            os.makedirs(idea_dir)
            build_dir = os.path.join(tmpdir, "cmake-build-debug")
            os.makedirs(build_dir)
            with open(os.path.join(build_dir, "compile_commands.json"), "w") as f:
                f.write("[]")

            cmake_xml = os.path.join(idea_dir, "cmake.xml")
            with open(cmake_xml, "w") as f:
                f.write("""<project version="4">
  <component name="CMakeSharedSettings">
    <configurations>
      <configuration PROFILE_NAME="Debug" GENERATION_DIR="cmake-build-debug"
                     CONFIG_NAME="Debug" ENABLED="true" />
      <configuration PROFILE_NAME="Release" GENERATION_DIR="cmake-build-release"
                     CONFIG_NAME="Release" ENABLED="true" />
    </configurations>
  </component>
</project>""")

            results = _detect_clion(tmpdir)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].language, "cpp")
            self.assertEqual(results[0].build_info["compile_commands_dir"], build_dir)
            self.assertEqual(results[0].source, DetectionSource.CLION)
            self.assertIn("Debug", results[0].notes[0])

    def test_disabled_profile_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            idea_dir = os.path.join(tmpdir, ".idea")
            os.makedirs(idea_dir)

            cmake_xml = os.path.join(idea_dir, "cmake.xml")
            with open(cmake_xml, "w") as f:
                f.write("""<project version="4">
  <component name="CMakeSharedSettings">
    <configurations>
      <configuration PROFILE_NAME="Debug" GENERATION_DIR="cmake-build-debug"
                     ENABLED="false" />
    </configurations>
  </component>
</project>""")

            results = _detect_clion(tmpdir)
            self.assertEqual(len(results), 0)

    def test_missing_cmake_xml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            results = _detect_clion(tmpdir)
            self.assertEqual(len(results), 0)

    def test_malformed_xml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            idea_dir = os.path.join(tmpdir, ".idea")
            os.makedirs(idea_dir)
            with open(os.path.join(idea_dir, "cmake.xml"), "w") as f:
                f.write("not valid xml <<>")
            results = _detect_clion(tmpdir)
            self.assertEqual(len(results), 0)


class DetectVscodeCppTest(unittest.TestCase):
    def test_c_cpp_properties(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vscode_dir = os.path.join(tmpdir, ".vscode")
            os.makedirs(vscode_dir)
            build_dir = os.path.join(tmpdir, "build")
            os.makedirs(build_dir)
            cc_file = os.path.join(build_dir, "compile_commands.json")
            with open(cc_file, "w") as f:
                f.write("[]")

            props = {
                "version": 4,
                "configurations": [
                    {
                        "name": "Linux",
                        "compileCommands": "${workspaceFolder}/build/compile_commands.json",
                        "cStandard": "c17",
                    },
                    {
                        "name": "Mac",
                        "compileCommands": "${workspaceFolder}/build/compile_commands.json",
                    },
                ],
            }
            with open(os.path.join(vscode_dir, "c_cpp_properties.json"), "w") as f:
                json.dump(props, f)

            results = _detect_vscode_cpp(tmpdir)
            # Both configs point to same CDB, so we get two detections
            self.assertGreaterEqual(len(results), 1)
            self.assertEqual(results[0].language, "cpp")
            self.assertEqual(results[0].build_info["compile_commands_dir"], build_dir)
            self.assertEqual(results[0].source, DetectionSource.VSCODE)

    def test_settings_clangd_arguments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vscode_dir = os.path.join(tmpdir, ".vscode")
            os.makedirs(vscode_dir)
            build_dir = os.path.join(tmpdir, "my-build")
            os.makedirs(build_dir)
            with open(os.path.join(build_dir, "compile_commands.json"), "w") as f:
                f.write("[]")

            settings = {
                "clangd.arguments": [
                    "--background-index",
                    "--compile-commands-dir=%s" % build_dir,
                ],
            }
            with open(os.path.join(vscode_dir, "settings.json"), "w") as f:
                json.dump(settings, f)

            results = _detect_vscode_cpp(tmpdir)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].build_info["compile_commands_dir"], build_dir)

    def test_missing_vscode_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            results = _detect_vscode_cpp(tmpdir)
            self.assertEqual(len(results), 0)


class DetectCmakePresetsTest(unittest.TestCase):
    def test_valid_preset_with_cdb(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            build_dir = os.path.join(tmpdir, "build-debug")
            os.makedirs(build_dir)
            with open(os.path.join(build_dir, "compile_commands.json"), "w") as f:
                f.write("[]")

            presets = {
                "version": 6,
                "configurePresets": [
                    {
                        "name": "debug",
                        "binaryDir": "${sourceDir}/build-debug",
                        "cacheVariables": {
                            "CMAKE_EXPORT_COMPILE_COMMANDS": "ON",
                        },
                    },
                    {
                        "name": "release",
                        "binaryDir": "${sourceDir}/build-release",
                        "cacheVariables": {
                            "CMAKE_BUILD_TYPE": "Release",
                        },
                    },
                ],
            }
            with open(os.path.join(tmpdir, "CMakePresets.json"), "w") as f:
                json.dump(presets, f)

            results = _detect_cmake_presets(tmpdir)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].build_info["compile_commands_dir"], build_dir)
            self.assertEqual(results[0].source, DetectionSource.CMAKE_PRESETS)

    def test_no_cdb_on_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            presets = {
                "version": 6,
                "configurePresets": [{
                    "name": "debug",
                    "binaryDir": "${sourceDir}/build-debug",
                    "cacheVariables": {"CMAKE_EXPORT_COMPILE_COMMANDS": "ON"},
                }],
            }
            with open(os.path.join(tmpdir, "CMakePresets.json"), "w") as f:
                json.dump(presets, f)

            results = _detect_cmake_presets(tmpdir)
            self.assertEqual(len(results), 0)

    def test_missing_presets_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            results = _detect_cmake_presets(tmpdir)
            self.assertEqual(len(results), 0)


class DetectClangdConfigTest(unittest.TestCase):
    def test_valid_clangd_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            build_dir = os.path.join(tmpdir, "build")
            os.makedirs(build_dir)
            with open(os.path.join(build_dir, "compile_commands.json"), "w") as f:
                f.write("[]")

            with open(os.path.join(tmpdir, ".clangd"), "w") as f:
                f.write("CompileFlags:\n  CompilationDatabase: build\n")

            results = _detect_clangd_config(tmpdir)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].build_info["compile_commands_dir"], build_dir)
            self.assertEqual(results[0].source, DetectionSource.CLANGD_CONFIG)

    def test_missing_clangd_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            results = _detect_clangd_config(tmpdir)
            self.assertEqual(len(results), 0)


class DetectCompileCommandsTest(unittest.TestCase):
    def test_found_in_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "compile_commands.json"), "w") as f:
                f.write("[]")

            results = _detect_compile_commands(tmpdir)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].source, DetectionSource.COMPILE_COMMANDS)

    def test_found_in_build_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            build_dir = os.path.join(tmpdir, "build")
            os.makedirs(build_dir)
            with open(os.path.join(build_dir, "compile_commands.json"), "w") as f:
                f.write("[]")

            results = _detect_compile_commands(tmpdir)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].build_info["compile_commands_dir"],
                             os.path.realpath(build_dir))

    def test_found_in_cmake_build_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cmake_dir = os.path.join(tmpdir, "cmake-build-debug")
            os.makedirs(cmake_dir)
            with open(os.path.join(cmake_dir, "compile_commands.json"), "w") as f:
                f.write("[]")

            results = _detect_compile_commands(tmpdir)
            self.assertEqual(len(results), 1)

    def test_deduplicates_symlinks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            build_dir = os.path.join(tmpdir, "build")
            os.makedirs(build_dir)
            cc_file = os.path.join(build_dir, "compile_commands.json")
            with open(cc_file, "w") as f:
                f.write("[]")
            # Symlink from root to build
            os.symlink(cc_file, os.path.join(tmpdir, "compile_commands.json"))

            results = _detect_compile_commands(tmpdir)
            # Should deduplicate since both point to same real file
            self.assertEqual(len(results), 1)

    def test_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            results = _detect_compile_commands(tmpdir)
            self.assertEqual(len(results), 0)


class DetectCompileFlagsTest(unittest.TestCase):
    def test_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "compile_flags.txt"), "w") as f:
                f.write("-std=c++17\n-I/usr/include\n")

            results = _detect_compile_flags(tmpdir)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].source, DetectionSource.COMPILE_FLAGS)

    def test_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            results = _detect_compile_flags(tmpdir)
            self.assertEqual(len(results), 0)


# ---------------------------------------------------------------------------
# JVM IDE metadata detectors
# ---------------------------------------------------------------------------

class DetectIntellijJvmTest(unittest.TestCase):
    def test_full_intellij_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            idea_dir = os.path.join(tmpdir, ".idea")
            os.makedirs(idea_dir)

            # misc.xml
            with open(os.path.join(idea_dir, "misc.xml"), "w") as f:
                f.write("""<project version="4">
  <component name="ProjectRootManager" version="2"
             languageLevel="JDK_21"
             project-jdk-name="21"
             project-jdk-type="JavaSDK">
    <output url="file://$PROJECT_DIR$/out"/>
  </component>
</project>""")

            # gradle.xml
            with open(os.path.join(idea_dir, "gradle.xml"), "w") as f:
                f.write("""<project version="4">
  <component name="GradleSettings">
    <option name="linkedExternalProjectsSettings">
      <GradleProjectSettings>
        <option name="distributionType" value="DEFAULT_WRAPPED"/>
        <option name="externalProjectPath" value="$PROJECT_DIR$"/>
      </GradleProjectSettings>
    </option>
  </component>
</project>""")

            # kotlinc.xml
            with open(os.path.join(idea_dir, "kotlinc.xml"), "w") as f:
                f.write("""<project version="4">
  <component name="Kotlin2JvmCompilerArguments">
    <option name="jvmTarget" value="21"/>
  </component>
  <component name="KotlinCommonCompilerArguments">
    <option name="apiVersion" value="2.0"/>
    <option name="languageVersion" value="2.0"/>
  </component>
</project>""")

            results = _detect_intellij_jvm(tmpdir)
            self.assertEqual(len(results), 1)
            det = results[0]
            self.assertEqual(det.language, "java")
            self.assertEqual(det.build_info["java_version"], "21")
            self.assertEqual(det.build_info["build_system"], "gradle")
            self.assertEqual(det.build_info["kotlin"]["jvm_target"], "21")
            self.assertEqual(det.build_info["kotlin"]["language_version"], "2.0")
            self.assertEqual(det.build_info["kotlin"]["api_version"], "2.0")
            self.assertEqual(det.workspace_root, tmpdir)
            self.assertEqual(det.source, DetectionSource.INTELLIJ)

    def test_modules_and_iml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            idea_dir = os.path.join(tmpdir, ".idea")
            os.makedirs(idea_dir)

            # Create IML file
            iml_path = os.path.join(tmpdir, "my-module.iml")
            with open(iml_path, "w") as f:
                f.write("""<module type="JAVA_MODULE" version="4">
  <component name="NewModuleRootManager" LANGUAGE_LEVEL="JDK_17">
    <content url="file://$MODULE_DIR$">
      <sourceFolder url="file://$MODULE_DIR$/src/main/java" isTestSource="false"/>
      <sourceFolder url="file://$MODULE_DIR$/src/main/kotlin" isTestSource="false"/>
      <sourceFolder url="file://$MODULE_DIR$/src/test/java" isTestSource="true"/>
    </content>
  </component>
</module>""")

            # modules.xml
            with open(os.path.join(idea_dir, "modules.xml"), "w") as f:
                f.write("""<project version="4">
  <component name="ProjectModuleManager">
    <modules>
      <module fileurl="file://$PROJECT_DIR$/my-module.iml"
              filepath="$PROJECT_DIR$/my-module.iml"/>
    </modules>
  </component>
</project>""")

            results = _detect_intellij_jvm(tmpdir)
            self.assertEqual(len(results), 1)
            det = results[0]
            self.assertIn(os.path.join(tmpdir, "src/main/java"), det.build_info["source_roots"])
            self.assertIn(os.path.join(tmpdir, "src/main/kotlin"), det.build_info["source_roots"])
            self.assertIn(os.path.join(tmpdir, "src/test/java"), det.build_info["test_roots"])
            # Kotlin detected from source folder name
            self.assertIn("Kotlin sources detected", det.notes)

    def test_no_idea_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            results = _detect_intellij_jvm(tmpdir)
            self.assertEqual(len(results), 0)


class DetectEclipseJvmTest(unittest.TestCase):
    def test_full_eclipse_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # .project
            with open(os.path.join(tmpdir, ".project"), "w") as f:
                f.write("""<projectDescription>
  <natures>
    <nature>org.eclipse.jdt.core.javanature</nature>
    <nature>org.eclipse.buildship.core.gradleprojectnature</nature>
  </natures>
</projectDescription>""")

            # .classpath
            with open(os.path.join(tmpdir, ".classpath"), "w") as f:
                f.write("""<classpath>
  <classpathentry kind="src" path="src/main/java"/>
  <classpathentry kind="src" path="src/test/java">
    <attributes><attribute name="test" value="true"/></attributes>
  </classpathentry>
  <classpathentry kind="con"
    path="org.eclipse.jdt.launching.JRE_CONTAINER/org.eclipse.jdt.internal.debug.ui.launcher.StandardVMType/JavaSE-21"/>
  <classpathentry kind="con" path="org.eclipse.buildship.core.gradleclasspathcontainer"/>
</classpath>""")

            # .settings/org.eclipse.jdt.core.prefs
            settings_dir = os.path.join(tmpdir, ".settings")
            os.makedirs(settings_dir)
            with open(os.path.join(settings_dir, "org.eclipse.jdt.core.prefs"), "w") as f:
                f.write("org.eclipse.jdt.core.compiler.source=21\n"
                        "org.eclipse.jdt.core.compiler.compliance=21\n")

            results = _detect_eclipse_jvm(tmpdir)
            self.assertEqual(len(results), 1)
            det = results[0]
            self.assertEqual(det.language, "java")
            self.assertEqual(det.build_info["java_version"], "21")
            self.assertEqual(det.build_info["build_system"], "gradle")
            self.assertIn(os.path.join(tmpdir, "src/main/java"), det.build_info["source_roots"])
            self.assertIn(os.path.join(tmpdir, "src/test/java"), det.build_info["test_roots"])
            self.assertEqual(det.source, DetectionSource.ECLIPSE)

    def test_no_eclipse_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            results = _detect_eclipse_jvm(tmpdir)
            self.assertEqual(len(results), 0)

    def test_maven_nature(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, ".project"), "w") as f:
                f.write("""<projectDescription>
  <natures>
    <nature>org.eclipse.jdt.core.javanature</nature>
    <nature>org.eclipse.m2e.core.maven2Nature</nature>
  </natures>
</projectDescription>""")

            with open(os.path.join(tmpdir, ".classpath"), "w") as f:
                f.write("""<classpath>
  <classpathentry kind="src" path="src/main/java"/>
  <classpathentry kind="src" path="src/test/java">
    <attributes><attribute name="test" value="true"/></attributes>
  </classpathentry>
</classpath>""")

            results = _detect_eclipse_jvm(tmpdir)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].build_info["build_system"], "maven")


class DetectVscodeJvmTest(unittest.TestCase):
    def test_java_settings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vscode_dir = os.path.join(tmpdir, ".vscode")
            os.makedirs(vscode_dir)
            settings = {
                "java.configuration.runtimes": [
                    {"name": "JavaSE-11", "path": "/usr/lib/jvm/java-11"},
                    {"name": "JavaSE-21", "path": "/usr/lib/jvm/java-21", "default": True},
                ],
                "java.import.gradle.wrapper.enabled": True,
            }
            with open(os.path.join(vscode_dir, "settings.json"), "w") as f:
                json.dump(settings, f)

            results = _detect_vscode_jvm(tmpdir)
            self.assertEqual(len(results), 1)
            det = results[0]
            self.assertEqual(det.language, "java")
            self.assertEqual(det.build_info["java_version"], "21")
            self.assertEqual(det.build_info["build_system"], "gradle")

    def test_no_java_settings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vscode_dir = os.path.join(tmpdir, ".vscode")
            os.makedirs(vscode_dir)
            with open(os.path.join(vscode_dir, "settings.json"), "w") as f:
                json.dump({"editor.fontSize": 14}, f)

            results = _detect_vscode_jvm(tmpdir)
            self.assertEqual(len(results), 0)


# ---------------------------------------------------------------------------
# Build system detectors
# ---------------------------------------------------------------------------

class DetectGradleTest(unittest.TestCase):
    def test_settings_gradle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "settings.gradle"), "w") as f:
                f.write("rootProject.name = 'test'\n")
            with open(os.path.join(tmpdir, "build.gradle"), "w") as f:
                f.write("apply plugin: 'java'\n")

            results = _detect_gradle(tmpdir)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].language, "java")
            self.assertEqual(results[0].build_info["build_system"], "gradle")
            self.assertEqual(results[0].workspace_root, tmpdir)

    def test_settings_gradle_kts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "settings.gradle.kts"), "w") as f:
                f.write("rootProject.name = \"test\"\n")

            results = _detect_gradle(tmpdir)
            self.assertEqual(len(results), 1)

    def test_gradle_in_parent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "settings.gradle"), "w") as f:
                f.write("include 'app'\n")
            subproject = os.path.join(tmpdir, "app")
            os.makedirs(subproject)

            results = _detect_gradle(subproject)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].workspace_root, tmpdir)

    def test_no_gradle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            results = _detect_gradle(tmpdir)
            self.assertEqual(len(results), 0)

    def test_kotlin_sources_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "build.gradle.kts"), "w") as f:
                f.write("plugins { kotlin(\"jvm\") }\n")
            src_dir = os.path.join(tmpdir, "src", "main", "kotlin")
            os.makedirs(src_dir)
            with open(os.path.join(src_dir, "Main.kt"), "w") as f:
                f.write("fun main() {}\n")

            results = _detect_gradle(tmpdir)
            self.assertEqual(len(results), 1)
            self.assertIn("Kotlin sources detected", results[0].notes)


class DetectMavenTest(unittest.TestCase):
    def test_simple_pom(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "pom.xml"), "w") as f:
                f.write("""<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>test</artifactId>
  <version>1.0</version>
  <properties>
    <maven.compiler.source>21</maven.compiler.source>
    <maven.compiler.target>21</maven.compiler.target>
  </properties>
</project>""")

            results = _detect_maven(tmpdir)
            self.assertEqual(len(results), 1)
            det = results[0]
            self.assertEqual(det.language, "java")
            self.assertEqual(det.build_info["build_system"], "maven")
            self.assertEqual(det.build_info["java_version"], "21")

    def test_multi_module_pom(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "pom.xml"), "w") as f:
                f.write("""<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>parent</artifactId>
  <packaging>pom</packaging>
  <modules>
    <module>core</module>
    <module>api</module>
  </modules>
</project>""")

            results = _detect_maven(tmpdir)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].build_info["modules"], ["core", "api"])

    def test_no_pom(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            results = _detect_maven(tmpdir)
            self.assertEqual(len(results), 0)


class ScanForKotlinSourcesTest(unittest.TestCase):
    def test_kotlin_files_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            kt_dir = os.path.join(tmpdir, "src", "main", "kotlin")
            os.makedirs(kt_dir)
            with open(os.path.join(kt_dir, "Main.kt"), "w") as f:
                f.write("fun main() {}\n")
            with open(os.path.join(kt_dir, "Build.kts"), "w") as f:
                f.write("// script\n")

            self.assertTrue(_scan_for_kotlin_sources(tmpdir))

    def test_no_kotlin_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            java_dir = os.path.join(tmpdir, "src", "main", "java")
            os.makedirs(java_dir)
            with open(os.path.join(java_dir, "Main.java"), "w") as f:
                f.write("class Main {}\n")

            self.assertFalse(_scan_for_kotlin_sources(tmpdir))

    def test_no_src_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertFalse(_scan_for_kotlin_sources(tmpdir))


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------

class MergeDetectionsTest(unittest.TestCase):
    def test_merge_same_language(self):
        d1 = LanguageDetection(
            language="cpp",
            build_info={"compile_commands_dir": "/build"},
            source=DetectionSource.CLION,
            confidence=0.95,
            notes=["CLion"],
        )
        d2 = LanguageDetection(
            language="cpp",
            build_info={"compile_commands_dir": "/other", "extra": "value"},
            source=DetectionSource.COMPILE_COMMANDS,
            confidence=0.8,
            notes=["filesystem"],
        )

        merged = _merge_detections([d1, d2])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].language, "cpp")
        # Higher confidence (d1) should win for compile_commands_dir
        self.assertEqual(merged[0].build_info["compile_commands_dir"], "/build")
        # But extra key from d2 should be merged in
        self.assertEqual(merged[0].build_info["extra"], "value")

    def test_merge_different_languages(self):
        d1 = LanguageDetection(language="cpp", confidence=0.9)
        d2 = LanguageDetection(language="java", confidence=0.85)

        merged = _merge_detections([d1, d2])
        self.assertEqual(len(merged), 2)
        languages = {d.language for d in merged}
        self.assertEqual(languages, {"cpp", "java"})

    def test_workspace_root_preserved(self):
        d1 = LanguageDetection(
            language="java",
            confidence=0.95,
            workspace_root=None,
        )
        d2 = LanguageDetection(
            language="java",
            confidence=0.85,
            workspace_root="/my/root",
        )
        merged = _merge_detections([d1, d2])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].workspace_root, "/my/root")


# ---------------------------------------------------------------------------
# Orchestration (detect_project)
# ---------------------------------------------------------------------------

class DetectProjectTest(unittest.TestCase):
    def test_nonexistent_path(self):
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(detect_project("/nonexistent/path/xyz"))
        self.assertEqual(len(result.languages), 0)
        self.assertTrue(any("does not exist" in e for e in result.errors))
        loop.close()

    def test_empty_directory(self):
        loop = asyncio.new_event_loop()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = loop.run_until_complete(detect_project(tmpdir))
            self.assertEqual(len(result.languages), 0)
            self.assertEqual(len(result.errors), 0)
        loop.close()

    def test_ide_detection_wins_over_filesystem(self):
        loop = asyncio.new_event_loop()
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create CLion metadata pointing to cmake-build-debug
            idea_dir = os.path.join(tmpdir, ".idea")
            os.makedirs(idea_dir)
            clion_build = os.path.join(tmpdir, "cmake-build-debug")
            os.makedirs(clion_build)
            with open(os.path.join(clion_build, "compile_commands.json"), "w") as f:
                f.write("[]")

            with open(os.path.join(idea_dir, "cmake.xml"), "w") as f:
                f.write("""<project version="4">
  <component name="CMakeSharedSettings">
    <configurations>
      <configuration PROFILE_NAME="Debug" GENERATION_DIR="cmake-build-debug" ENABLED="true"/>
    </configurations>
  </component>
</project>""")

            # Also create a CDB in build/ (would be found by filesystem scan)
            build_dir = os.path.join(tmpdir, "build")
            os.makedirs(build_dir)
            with open(os.path.join(build_dir, "compile_commands.json"), "w") as f:
                f.write("[]")

            result = loop.run_until_complete(detect_project(tmpdir))
            self.assertEqual(len(result.languages), 1)
            # IDE detection should win (higher confidence)
            self.assertEqual(result.languages[0].build_info["compile_commands_dir"], clion_build)
        loop.close()

    def test_active_detection_gated(self):
        """Active detection should not run when allow_active=False."""
        loop = asyncio.new_event_loop()
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a CMakeLists.txt but no compile_commands.json
            with open(os.path.join(tmpdir, "CMakeLists.txt"), "w") as f:
                f.write("cmake_minimum_required(VERSION 3.10)\n")

            result = loop.run_until_complete(detect_project(tmpdir, allow_active=False))
            # Should not find anything — no CDB on disk, no active generation
            cpp_results = [d for d in result.languages if d.language in ("c", "cpp")]
            self.assertEqual(len(cpp_results), 0)
        loop.close()

    def test_multiple_languages_detected(self):
        loop = asyncio.new_event_loop()
        with tempfile.TemporaryDirectory() as tmpdir:
            # C++ via compile_commands.json
            with open(os.path.join(tmpdir, "compile_commands.json"), "w") as f:
                f.write("[]")

            # Java via pom.xml
            with open(os.path.join(tmpdir, "pom.xml"), "w") as f:
                f.write("""<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>test</artifactId>
</project>""")

            result = loop.run_until_complete(detect_project(tmpdir))
            languages = {d.language for d in result.languages}
            self.assertIn("cpp", languages)
            self.assertIn("java", languages)
        loop.close()

    def test_gradle_workspace_root_correction(self):
        loop = asyncio.new_event_loop()
        with tempfile.TemporaryDirectory() as tmpdir:
            # Parent has settings.gradle
            with open(os.path.join(tmpdir, "settings.gradle"), "w") as f:
                f.write("include 'app'\n")
            subproject = os.path.join(tmpdir, "app")
            os.makedirs(subproject)
            with open(os.path.join(subproject, "build.gradle"), "w") as f:
                f.write("apply plugin: 'java'\n")

            result = loop.run_until_complete(detect_project(subproject))
            self.assertEqual(len(result.languages), 1)
            self.assertEqual(result.languages[0].workspace_root, tmpdir)
        loop.close()


# ---------------------------------------------------------------------------
# Active detection (mocked subprocess)
# ---------------------------------------------------------------------------

class ActiveCppDetectionTest(unittest.TestCase):
    @patch("karellen_lsp_mcp.project_detector.asyncio.create_subprocess_exec")
    def test_cmake_generation(self, mock_exec):
        from karellen_lsp_mcp.project_detector import _generate_cmake

        loop = asyncio.new_event_loop()
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create CMakeLists.txt
            with open(os.path.join(tmpdir, "CMakeLists.txt"), "w") as f:
                f.write("cmake_minimum_required(VERSION 3.10)\n")

            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_exec.return_value = mock_proc

            # Pre-create the compile_commands.json that cmake would generate
            build_dir = os.path.join(tmpdir, "build")
            os.makedirs(build_dir, exist_ok=True)
            with open(os.path.join(build_dir, "compile_commands.json"), "w") as f:
                f.write("[]")

            results = loop.run_until_complete(_generate_cmake(tmpdir))
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].source, DetectionSource.CMAKE)

            # Verify cmake was called with correct arguments
            call_args = mock_exec.call_args
            cmd = call_args[0]
            self.assertEqual(cmd[0], "cmake")
            self.assertIn("-DCMAKE_EXPORT_COMPILE_COMMANDS=ON", cmd)
        loop.close()

    @patch("karellen_lsp_mcp.project_detector.asyncio.create_subprocess_exec")
    def test_cmake_failure(self, mock_exec):
        from karellen_lsp_mcp.project_detector import _generate_cmake

        loop = asyncio.new_event_loop()
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "CMakeLists.txt"), "w") as f:
                f.write("cmake_minimum_required(VERSION 3.10)\n")

            mock_proc = AsyncMock()
            mock_proc.returncode = 1
            mock_proc.communicate = AsyncMock(return_value=(b"", b"Error"))
            mock_exec.return_value = mock_proc

            results = loop.run_until_complete(_generate_cmake(tmpdir))
            self.assertEqual(len(results), 0)
        loop.close()

    def test_cmake_no_cmakelists(self):
        from karellen_lsp_mcp.project_detector import _generate_cmake

        loop = asyncio.new_event_loop()
        with tempfile.TemporaryDirectory() as tmpdir:
            results = loop.run_until_complete(_generate_cmake(tmpdir))
            self.assertEqual(len(results), 0)
        loop.close()


class ActiveJvmDetectionTest(unittest.TestCase):
    @patch("karellen_lsp_mcp.project_detector.asyncio.create_subprocess_exec")
    def test_maven_active(self, mock_exec):
        from karellen_lsp_mcp.project_detector import _detect_maven_active

        loop = asyncio.new_event_loop()
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "pom.xml"), "w") as f:
                f.write("<project/>")

            call_count = [0]

            async def mock_exec_fn(*args, **kwargs):
                proc = AsyncMock()
                if call_count[0] == 0:
                    # First call: maven.compiler.source
                    proc.returncode = 0
                    proc.communicate = AsyncMock(return_value=(b"21", b""))
                else:
                    # Second call: project.compileSourceRoots
                    proc.returncode = 0
                    proc.communicate = AsyncMock(return_value=(b"/src/main/java\n/src/main/kotlin", b""))
                call_count[0] += 1
                return proc

            mock_exec.side_effect = mock_exec_fn

            results = loop.run_until_complete(_detect_maven_active(tmpdir))
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].build_info["java_version"], "21")
            self.assertEqual(results[0].build_info["source_roots"],
                             ["/src/main/java", "/src/main/kotlin"])
        loop.close()

    def test_maven_active_no_pom(self):
        from karellen_lsp_mcp.project_detector import _detect_maven_active

        loop = asyncio.new_event_loop()
        with tempfile.TemporaryDirectory() as tmpdir:
            results = loop.run_until_complete(_detect_maven_active(tmpdir))
            self.assertEqual(len(results), 0)
        loop.close()

    @patch("karellen_lsp_mcp.project_detector.asyncio.create_subprocess_exec")
    def test_gradle_active(self, mock_exec):
        from karellen_lsp_mcp.project_detector import _detect_gradle_active

        loop = asyncio.new_event_loop()
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "build.gradle"), "w") as f:
                f.write("apply plugin: 'java'\n")

            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(
                b"sourceCompatibility: 21\ntargetCompatibility: 21\n", b""
            ))
            mock_exec.return_value = mock_proc

            results = loop.run_until_complete(_detect_gradle_active(tmpdir))
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].build_info["java_version"], "21")
            self.assertEqual(results[0].build_info["target_version"], "21")
        loop.close()


if __name__ == "__main__":
    unittest.main()
