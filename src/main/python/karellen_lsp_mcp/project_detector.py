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

"""Project language and build configuration auto-detection.

Detects project languages and build configurations by inspecting IDE metadata
files, build system artifacts, and optionally running build tool commands.
"""

import asyncio
import enum
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class DetectionSource(enum.Enum):
    CLION = "clion"
    VSCODE = "vscode"
    INTELLIJ = "intellij"
    ECLIPSE = "eclipse"
    COMPILE_COMMANDS = "compile_commands"
    COMPILE_FLAGS = "compile_flags"
    CMAKE = "cmake"
    MESON = "meson"
    NINJA = "ninja"
    AUTOTOOLS = "autotools"
    MAKEFILE = "makefile"
    GRADLE = "gradle"
    MAVEN = "maven"
    SBT = "sbt"
    CLANGD_CONFIG = "clangd_config"
    CMAKE_PRESETS = "cmake_presets"


@dataclass
class LanguageDetection:
    language: str
    build_info: dict = field(default_factory=dict)
    lsp_command: list[str] | None = None
    source: DetectionSource | None = None
    confidence: float = 1.0
    workspace_root: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class DetectionResult:
    project_path: str
    languages: list[LanguageDetection] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# XML parsing helpers
# ---------------------------------------------------------------------------

def _parse_xml_safe(path):
    """Parse an XML file, returning None on any error."""
    try:
        return ET.parse(path)
    except (ET.ParseError, OSError, UnicodeDecodeError) as e:
        logger.debug("Failed to parse XML %s: %s", path, e)
        return None


def _read_json_safe(path):
    """Read a JSON file, returning None on any error."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        logger.debug("Failed to read JSON %s: %s", path, e)
        return None


def _resolve_variable(value, variables):
    """Replace known variables like ${workspaceFolder}, $PROJECT_DIR$, etc."""
    if not value:
        return value
    for var_name, var_value in variables.items():
        value = value.replace(var_name, var_value)
    return value


# ---------------------------------------------------------------------------
# C/C++ IDE metadata detectors
# ---------------------------------------------------------------------------

def _detect_clion(project_path):
    """Parse CLion .idea/cmake.xml for compile_commands_dir."""
    results = []
    cmake_xml = os.path.join(project_path, ".idea", "cmake.xml")
    tree = _parse_xml_safe(cmake_xml)
    if tree is None:
        return results

    root = tree.getroot()
    # Prefer CMakeSharedSettings (committed to VCS) over CMakeSettings (local)
    for component_name in ("CMakeSharedSettings", "CMakeSettings"):
        for component in root.iter("component"):
            if component.get("name") != component_name:
                continue
            for config in component.iter("configuration"):
                if config.get("ENABLED") == "false":
                    continue
                gen_dir = config.get("GENERATION_DIR")
                if not gen_dir:
                    continue
                abs_dir = os.path.join(project_path, gen_dir)
                cc_path = os.path.join(abs_dir, "compile_commands.json")
                if os.path.isfile(cc_path):
                    profile = config.get("PROFILE_NAME", "")
                    results.append(LanguageDetection(
                        language="cpp",
                        build_info={"compile_commands_dir": abs_dir},
                        source=DetectionSource.CLION,
                        confidence=0.95,
                        notes=["CLion profile: %s" % profile] if profile else [],
                    ))
    return results


def _detect_vscode_cpp(project_path):
    """Parse VS Code c_cpp_properties.json and settings.json for C/C++ config."""
    results = []
    variables = {
        "${workspaceFolder}": project_path,
        "${workspaceRoot}": project_path,
    }

    # c_cpp_properties.json
    props_path = os.path.join(project_path, ".vscode", "c_cpp_properties.json")
    props = _read_json_safe(props_path)
    if props and isinstance(props.get("configurations"), list):
        for config in props["configurations"]:
            compile_commands = config.get("compileCommands")
            if compile_commands:
                resolved = _resolve_variable(compile_commands, variables)
                cc_dir = os.path.dirname(resolved)
                if os.path.isfile(resolved):
                    results.append(LanguageDetection(
                        language="cpp",
                        build_info={"compile_commands_dir": cc_dir},
                        source=DetectionSource.VSCODE,
                        confidence=0.9,
                        notes=["VS Code config: %s" % config.get("name", "")],
                    ))

    # settings.json
    settings_path = os.path.join(project_path, ".vscode", "settings.json")
    settings = _read_json_safe(settings_path)
    if settings:
        # C_Cpp.default.compileCommands
        cc_setting = settings.get("C_Cpp.default.compileCommands")
        if cc_setting:
            resolved = _resolve_variable(cc_setting, variables)
            cc_dir = os.path.dirname(resolved)
            if os.path.isfile(resolved):
                results.append(LanguageDetection(
                    language="cpp",
                    build_info={"compile_commands_dir": cc_dir},
                    source=DetectionSource.VSCODE,
                    confidence=0.9,
                    notes=["VS Code C_Cpp.default.compileCommands"],
                ))

        # clangd.arguments -> --compile-commands-dir=
        clangd_args = settings.get("clangd.arguments")
        if isinstance(clangd_args, list):
            for arg in clangd_args:
                m = re.match(r"--compile-commands-dir=(.*)", arg)
                if m:
                    cc_dir = _resolve_variable(m.group(1), variables)
                    cc_file = os.path.join(cc_dir, "compile_commands.json")
                    if os.path.isfile(cc_file):
                        results.append(LanguageDetection(
                            language="cpp",
                            build_info={"compile_commands_dir": cc_dir},
                            source=DetectionSource.VSCODE,
                            confidence=0.9,
                            notes=["VS Code clangd.arguments"],
                        ))

    return results


def _detect_cmake_presets(project_path):
    """Parse CMakePresets.json for compile_commands.json locations."""
    results = []
    for filename in ("CMakePresets.json", "CMakeUserPresets.json"):
        presets_path = os.path.join(project_path, filename)
        presets = _read_json_safe(presets_path)
        if presets is None:
            continue

        configure_presets = presets.get("configurePresets", [])
        if not isinstance(configure_presets, list):
            continue

        for preset in configure_presets:
            cache_vars = preset.get("cacheVariables", {})
            if not isinstance(cache_vars, dict):
                continue
            export_cdb = cache_vars.get("CMAKE_EXPORT_COMPILE_COMMANDS")
            if export_cdb not in ("ON", "TRUE", "1", True):
                continue
            binary_dir = preset.get("binaryDir", "")
            if not binary_dir:
                continue
            resolved = _resolve_variable(binary_dir, {
                "${sourceDir}": project_path,
            })
            if not os.path.isabs(resolved):
                resolved = os.path.join(project_path, resolved)
            cc_file = os.path.join(resolved, "compile_commands.json")
            if os.path.isfile(cc_file):
                results.append(LanguageDetection(
                    language="cpp",
                    build_info={"compile_commands_dir": resolved},
                    source=DetectionSource.CMAKE_PRESETS,
                    confidence=0.85,
                    notes=["CMake preset: %s" % preset.get("name", "")],
                ))

    return results


def _detect_clangd_config(project_path):
    """Parse .clangd YAML config for CompileFlags.CompilationDatabase."""
    results = []
    clangd_path = os.path.join(project_path, ".clangd")
    if not os.path.isfile(clangd_path):
        return results

    try:
        with open(clangd_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return results

    # Simple YAML parsing for CompilationDatabase — avoid yaml dependency
    for line in content.splitlines():
        stripped = line.strip()
        m = re.match(r"CompilationDatabase\s*:\s*(.+)", stripped)
        if m:
            db_path = m.group(1).strip().strip("'\"")
            if not os.path.isabs(db_path):
                db_path = os.path.join(project_path, db_path)
            cc_file = os.path.join(db_path, "compile_commands.json")
            if os.path.isfile(cc_file):
                results.append(LanguageDetection(
                    language="cpp",
                    build_info={"compile_commands_dir": db_path},
                    source=DetectionSource.CLANGD_CONFIG,
                    confidence=0.9,
                    notes=[".clangd CompilationDatabase"],
                ))

    return results


def _detect_compile_commands(project_path):
    """Search for pre-existing compile_commands.json in standard locations."""
    results = []
    search_dirs = [
        project_path,
        os.path.join(project_path, "build"),
        os.path.join(project_path, "out"),
    ]

    # Also check cmake-build-* directories
    try:
        for entry in os.listdir(project_path):
            if entry.startswith("cmake-build-"):
                candidate = os.path.join(project_path, entry)
                if os.path.isdir(candidate):
                    search_dirs.append(candidate)
    except OSError:
        pass

    seen = set()
    for d in search_dirs:
        cc_file = os.path.join(d, "compile_commands.json")
        real_cc = os.path.realpath(cc_file)
        if real_cc in seen:
            continue
        if os.path.isfile(cc_file):
            seen.add(real_cc)
            results.append(LanguageDetection(
                language="cpp",
                build_info={"compile_commands_dir": os.path.realpath(d)},
                source=DetectionSource.COMPILE_COMMANDS,
                confidence=0.8,
                notes=["Found compile_commands.json in %s" % d],
            ))

    return results


def _detect_compile_flags(project_path):
    """Check for compile_flags.txt in the project root."""
    results = []
    flags_file = os.path.join(project_path, "compile_flags.txt")
    if os.path.isfile(flags_file):
        results.append(LanguageDetection(
            language="cpp",
            build_info={"compile_flags_file": flags_file},
            source=DetectionSource.COMPILE_FLAGS,
            confidence=0.7,
            notes=["Found compile_flags.txt"],
        ))
    return results


# ---------------------------------------------------------------------------
# Java/Kotlin IDE metadata detectors
# ---------------------------------------------------------------------------

def _parse_jdk_level(level_str):
    """Parse IntelliJ languageLevel like JDK_1_8 or JDK_21 to a version string."""
    if not level_str:
        return None
    level_str = level_str.upper()
    if level_str.startswith("JDK_"):
        ver = level_str[4:]
        if ver.startswith("1_"):
            return ver.replace("_", ".")
        return ver
    return None


def _detect_intellij_jvm(project_path):
    """Parse IntelliJ IDEA metadata for Java/Kotlin project configuration."""
    results = []
    idea_dir = os.path.join(project_path, ".idea")
    if not os.path.isdir(idea_dir):
        return results

    variables = {
        "$PROJECT_DIR$": project_path,
    }

    build_info = {}
    notes = []
    workspace_root = None
    has_kotlin = False

    # misc.xml -> languageLevel, project JDK
    misc_xml = os.path.join(idea_dir, "misc.xml")
    tree = _parse_xml_safe(misc_xml)
    if tree is not None:
        root = tree.getroot()
        for component in root.iter("component"):
            if component.get("name") != "ProjectRootManager":
                continue
            lang_level = component.get("languageLevel")
            jdk_version = _parse_jdk_level(lang_level)
            if jdk_version:
                build_info["java_version"] = jdk_version
                notes.append("IntelliJ languageLevel: %s" % lang_level)
            jdk_name = component.get("project-jdk-name")
            if jdk_name:
                build_info["jdk_name"] = jdk_name

    # gradle.xml -> externalProjectPath, distributionType
    gradle_xml = os.path.join(idea_dir, "gradle.xml")
    tree = _parse_xml_safe(gradle_xml)
    if tree is not None:
        root = tree.getroot()
        for settings in root.iter("GradleProjectSettings"):
            for option in settings.iter("option"):
                name = option.get("name")
                value = option.get("value", "")
                if name == "externalProjectPath":
                    resolved = _resolve_variable(value, variables)
                    workspace_root = resolved
                    build_info["build_system"] = "gradle"
                    notes.append("Gradle project path: %s" % resolved)
                elif name == "distributionType":
                    build_info["gradle_distribution"] = value.lower()

    # kotlinc.xml -> Kotlin compiler settings
    kotlinc_xml = os.path.join(idea_dir, "kotlinc.xml")
    tree = _parse_xml_safe(kotlinc_xml)
    if tree is not None:
        root = tree.getroot()
        kotlin_info = {}
        for component in root.iter("component"):
            for option in component.iter("option"):
                name = option.get("name")
                value = option.get("value", "")
                if name == "jvmTarget":
                    kotlin_info["jvm_target"] = value
                elif name == "languageVersion":
                    kotlin_info["language_version"] = value
                elif name == "apiVersion":
                    kotlin_info["api_version"] = value
        if kotlin_info:
            has_kotlin = True
            build_info["kotlin"] = kotlin_info
            notes.append("Kotlin settings from kotlinc.xml")

    # modules.xml + *.iml -> source roots
    modules_xml = os.path.join(idea_dir, "modules.xml")
    tree = _parse_xml_safe(modules_xml)
    if tree is not None:
        root = tree.getroot()
        source_roots = []
        test_roots = []
        for module_elem in root.iter("module"):
            filepath = module_elem.get("filepath", "")
            filepath = _resolve_variable(filepath, variables)
            if not os.path.isfile(filepath):
                continue
            module_dir = os.path.dirname(filepath)
            module_vars = dict(variables)
            module_vars["$MODULE_DIR$"] = module_dir

            iml_tree = _parse_xml_safe(filepath)
            if iml_tree is None:
                continue
            iml_root = iml_tree.getroot()
            for sf in iml_root.iter("sourceFolder"):
                url = sf.get("url", "")
                resolved = _resolve_variable(url, module_vars)
                resolved = resolved.replace("file://", "")
                is_test = sf.get("isTestSource") == "true"
                sf_type = sf.get("type", "")
                if is_test or sf_type.startswith("java-test"):
                    test_roots.append(resolved)
                else:
                    source_roots.append(resolved)
                # Check for Kotlin source folders
                if "/kotlin" in resolved:
                    has_kotlin = True

        if source_roots:
            build_info["source_roots"] = source_roots
        if test_roots:
            build_info["test_roots"] = test_roots

    if build_info or has_kotlin:
        if has_kotlin:
            notes.append("Kotlin sources detected")
        results.append(LanguageDetection(
            language="java",
            build_info=build_info,
            source=DetectionSource.INTELLIJ,
            confidence=0.95,
            workspace_root=workspace_root,
            notes=notes,
        ))

    return results


def _detect_eclipse_jvm(project_path):
    """Parse Eclipse metadata for Java project configuration."""
    results = []
    build_info = {}
    notes = []

    # .project -> check natures
    project_xml = os.path.join(project_path, ".project")
    tree = _parse_xml_safe(project_xml)
    is_java = False
    if tree is not None:
        root = tree.getroot()
        for nature in root.iter("nature"):
            text = nature.text or ""
            if "javanature" in text:
                is_java = True
            if "gradleprojectnature" in text:
                build_info["build_system"] = "gradle"
            elif "maven2Nature" in text:
                build_info["build_system"] = "maven"

    # .classpath -> source roots, JRE version
    classpath_xml = os.path.join(project_path, ".classpath")
    tree = _parse_xml_safe(classpath_xml)
    if tree is not None:
        root = tree.getroot()
        source_roots = []
        test_roots = []
        for entry in root.iter("classpathentry"):
            kind = entry.get("kind", "")
            path = entry.get("path", "")
            if kind == "src":
                is_test = False
                for attr in entry.iter("attribute"):
                    if attr.get("name") == "test" and attr.get("value") == "true":
                        is_test = True
                abs_path = os.path.join(project_path, path) if not os.path.isabs(path) else path
                if is_test:
                    test_roots.append(abs_path)
                else:
                    source_roots.append(abs_path)
            elif kind == "con" and "JRE_CONTAINER" in path:
                m = re.search(r"JavaSE-(\d+(?:\.\d+)?)", path)
                if m:
                    build_info["java_version"] = m.group(1)
                    notes.append("Eclipse JRE: JavaSE-%s" % m.group(1))
            elif kind == "con" and "gradleclasspathcontainer" in path:
                build_info["build_system"] = "gradle"
            elif kind == "con" and "MAVEN" in path.upper():
                build_info["build_system"] = "maven"
        is_java = is_java or bool(source_roots)
        if source_roots:
            build_info["source_roots"] = source_roots
        if test_roots:
            build_info["test_roots"] = test_roots

    # .settings/org.eclipse.jdt.core.prefs -> compiler version
    prefs_path = os.path.join(project_path, ".settings", "org.eclipse.jdt.core.prefs")
    if os.path.isfile(prefs_path):
        try:
            with open(prefs_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("org.eclipse.jdt.core.compiler.source="):
                        version = line.split("=", 1)[1].strip()
                        build_info["java_version"] = version
                        notes.append("Eclipse compiler.source: %s" % version)
                        is_java = True
                    elif line.startswith("org.eclipse.jdt.core.compiler.compliance="):
                        version = line.split("=", 1)[1].strip()
                        if "java_version" not in build_info:
                            build_info["java_version"] = version
        except OSError:
            pass

    if is_java:
        results.append(LanguageDetection(
            language="java",
            build_info=build_info,
            source=DetectionSource.ECLIPSE,
            confidence=0.9,
            notes=notes,
        ))

    return results


def _detect_vscode_jvm(project_path):
    """Parse VS Code settings.json for Java configuration."""
    results = []
    settings_path = os.path.join(project_path, ".vscode", "settings.json")
    settings = _read_json_safe(settings_path)
    if settings is None:
        return results

    build_info = {}
    notes = []
    found = False

    # java.configuration.runtimes
    runtimes = settings.get("java.configuration.runtimes")
    if isinstance(runtimes, list) and runtimes:
        for rt in runtimes:
            if rt.get("default"):
                name = rt.get("name", "")
                m = re.search(r"JavaSE-(\d+(?:\.\d+)?)", name)
                if m:
                    build_info["java_version"] = m.group(1)
                    notes.append("VS Code runtime: %s" % name)
                    found = True
                break

    # java.jdt.ls.java.home
    jdt_home = settings.get("java.jdt.ls.java.home")
    if jdt_home:
        build_info["java_home"] = jdt_home
        found = True

    # java.import.gradle.*
    gradle_home = settings.get("java.import.gradle.home")
    if gradle_home:
        build_info["build_system"] = "gradle"
        build_info["gradle_home"] = gradle_home
        found = True

    gradle_wrapper = settings.get("java.import.gradle.wrapper.enabled")
    if gradle_wrapper is not None:
        build_info["build_system"] = "gradle"
        found = True

    if found:
        results.append(LanguageDetection(
            language="java",
            build_info=build_info,
            source=DetectionSource.VSCODE,
            confidence=0.85,
            notes=notes,
        ))

    return results


# ---------------------------------------------------------------------------
# Build system detectors (passive)
# ---------------------------------------------------------------------------

def _detect_gradle(project_path):
    """Detect Gradle projects by finding settings.gradle(.kts) walking upward."""
    results = []
    workspace_root = None

    # Walk upward to find settings.gradle
    current = os.path.realpath(project_path)
    for _ in range(10):  # Safety limit
        for name in ("settings.gradle", "settings.gradle.kts"):
            if os.path.isfile(os.path.join(current, name)):
                workspace_root = current
                break
        if workspace_root:
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    if workspace_root is None:
        # Also check for build.gradle without settings.gradle (single-project)
        for name in ("build.gradle", "build.gradle.kts"):
            if os.path.isfile(os.path.join(project_path, name)):
                workspace_root = project_path
                break

    if workspace_root is None:
        return results

    build_info = {"build_system": "gradle"}
    notes = ["Gradle project at %s" % workspace_root]

    # Scan for Kotlin sources
    has_kotlin = _scan_for_kotlin_sources(project_path)
    if has_kotlin:
        notes.append("Kotlin sources detected")

    results.append(LanguageDetection(
        language="java",
        build_info=build_info,
        source=DetectionSource.GRADLE,
        confidence=0.85,
        workspace_root=workspace_root,
        notes=notes,
    ))

    return results


def _detect_maven(project_path):
    """Detect Maven projects by finding pom.xml."""
    results = []
    pom_path = os.path.join(project_path, "pom.xml")
    if not os.path.isfile(pom_path):
        return results

    build_info = {"build_system": "maven"}
    notes = ["Maven project"]

    tree = _parse_xml_safe(pom_path)
    if tree is not None:
        root = tree.getroot()
        ns = ""
        # Handle Maven's default namespace
        m = re.match(r"\{(.+)\}", root.tag)
        if m:
            ns = m.group(1)

        def _find(element, tag):
            if ns:
                return element.find("{%s}%s" % (ns, tag))
            return element.find(tag)

        def _findall(element, tag):
            if ns:
                return element.findall("{%s}%s" % (ns, tag))
            return element.findall(tag)

        # Check for modules
        modules_elem = _find(root, "modules")
        if modules_elem is not None:
            modules = [m.text for m in _findall(modules_elem, "module") if m.text]
            if modules:
                build_info["modules"] = modules
                notes.append("Multi-module: %s" % ", ".join(modules[:5]))

        # Check properties for Java version
        props = _find(root, "properties")
        if props is not None:
            for prop_name in ("maven.compiler.source", "java.version"):
                elem = _find(props, prop_name)
                if elem is not None and elem.text:
                    build_info["java_version"] = elem.text.strip()
                    notes.append("Maven %s: %s" % (prop_name, elem.text.strip()))
                    break

    # Scan for Kotlin sources
    has_kotlin = _scan_for_kotlin_sources(project_path)
    if has_kotlin:
        notes.append("Kotlin sources detected")

    results.append(LanguageDetection(
        language="java",
        build_info=build_info,
        source=DetectionSource.MAVEN,
        confidence=0.85,
        notes=notes,
    ))

    return results


def _scan_for_kotlin_sources(project_path):
    """Check if the project contains .kt or .kts source files under src/."""
    src_dir = os.path.join(project_path, "src")
    if not os.path.isdir(src_dir):
        return False
    try:
        for root, _dirs, files in os.walk(src_dir):
            for f in files:
                if f.endswith((".kt", ".kts")):
                    return True
            # Limit depth to avoid scanning too deep
            depth = root[len(src_dir):].count(os.sep)
            if depth > 5:
                break
    except OSError:
        pass
    return False


# ---------------------------------------------------------------------------
# C/C++ Active detection (build tool commands)
# ---------------------------------------------------------------------------

async def _run_command(cmd, cwd, timeout=30):
    """Run a command and return (returncode, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
    except FileNotFoundError:
        return -1, "", "Command not found: %s" % cmd[0]
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return -1, "", "Command timed out after %ds" % timeout


async def _generate_cmake(project_path):
    """Run cmake to generate compile_commands.json."""
    results = []

    # Check for existing CMakeLists.txt
    if not os.path.isfile(os.path.join(project_path, "CMakeLists.txt")):
        return results

    # Check for existing build directory with CMakeCache.txt
    build_dir = None
    for candidate in ("build", "cmake-build-debug", "cmake-build-release"):
        cache = os.path.join(project_path, candidate, "CMakeCache.txt")
        if os.path.isfile(cache):
            build_dir = os.path.join(project_path, candidate)
            break

    if build_dir is None:
        build_dir = os.path.join(project_path, "build")

    rc, stdout, stderr = await _run_command(
        ["cmake", "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON", "-S", project_path, "-B", build_dir],
        cwd=project_path,
        timeout=60,
    )

    if rc == 0:
        cc_file = os.path.join(build_dir, "compile_commands.json")
        if os.path.isfile(cc_file):
            results.append(LanguageDetection(
                language="cpp",
                build_info={"compile_commands_dir": build_dir},
                source=DetectionSource.CMAKE,
                confidence=0.9,
                notes=["Generated by cmake"],
            ))
    else:
        logger.debug("cmake failed (rc=%d): %s", rc, stderr[:200])

    return results


async def _generate_meson(project_path):
    """Run meson setup to generate compile_commands.json."""
    results = []

    if not os.path.isfile(os.path.join(project_path, "meson.build")):
        return results

    build_dir = os.path.join(project_path, "build")
    if os.path.isdir(build_dir):
        # Already has a build dir — check if CDB exists already
        cc_file = os.path.join(build_dir, "compile_commands.json")
        if os.path.isfile(cc_file):
            return results

    rc, stdout, stderr = await _run_command(
        ["meson", "setup", build_dir],
        cwd=project_path,
        timeout=60,
    )

    if rc == 0:
        cc_file = os.path.join(build_dir, "compile_commands.json")
        if os.path.isfile(cc_file):
            results.append(LanguageDetection(
                language="cpp",
                build_info={"compile_commands_dir": build_dir},
                source=DetectionSource.MESON,
                confidence=0.9,
                notes=["Generated by meson setup"],
            ))
    else:
        logger.debug("meson setup failed (rc=%d): %s", rc, stderr[:200])

    return results


async def _generate_ninja_compdb(project_path):
    """Run ninja -t compdb to output compile_commands.json."""
    results = []

    # Need an existing build.ninja
    build_ninja = None
    for candidate in ("build.ninja", os.path.join("build", "build.ninja")):
        path = os.path.join(project_path, candidate)
        if os.path.isfile(path):
            build_ninja = path
            break

    if build_ninja is None:
        return results

    cwd = os.path.dirname(build_ninja)
    rc, stdout, stderr = await _run_command(
        ["ninja", "-t", "compdb"],
        cwd=cwd,
        timeout=10,
    )

    if rc == 0 and stdout.strip().startswith("["):
        cc_file = os.path.join(cwd, "compile_commands.json")
        try:
            with open(cc_file, "w", encoding="utf-8") as f:
                f.write(stdout)
            results.append(LanguageDetection(
                language="cpp",
                build_info={"compile_commands_dir": cwd},
                source=DetectionSource.NINJA,
                confidence=0.85,
                notes=["Generated by ninja -t compdb"],
            ))
        except OSError as e:
            logger.debug("Failed to write compile_commands.json: %s", e)

    return results


async def _generate_compiledb_make(project_path):
    """Run compiledb -n make to generate compile_commands.json."""
    results = []

    makefile_exists = any(
        os.path.isfile(os.path.join(project_path, f))
        for f in ("Makefile", "makefile", "GNUmakefile")
    )
    if not makefile_exists:
        return results

    rc, stdout, stderr = await _run_command(
        ["compiledb", "-n", "make"],
        cwd=project_path,
        timeout=30,
    )

    if rc == 0:
        cc_file = os.path.join(project_path, "compile_commands.json")
        if os.path.isfile(cc_file):
            results.append(LanguageDetection(
                language="cpp",
                build_info={"compile_commands_dir": project_path},
                source=DetectionSource.MAKEFILE,
                confidence=0.75,
                notes=["Generated by compiledb -n make"],
            ))
    else:
        logger.debug("compiledb failed (rc=%d): %s", rc, stderr[:200])

    return results


async def _generate_make_flags(project_path):
    """Parse make -pq to extract compiler flags and generate compile_flags.txt."""
    results = []

    makefile_exists = any(
        os.path.isfile(os.path.join(project_path, f))
        for f in ("Makefile", "makefile", "GNUmakefile")
    )
    if not makefile_exists:
        return results

    rc, stdout, stderr = await _run_command(
        ["make", "-pq", "--no-builtin-rules"],
        cwd=project_path,
        timeout=10,
    )

    # make -pq may return non-zero but still produce valid output
    if not stdout:
        return results

    flags = set()
    for line in stdout.splitlines():
        line = line.strip()
        for var in ("CFLAGS", "CXXFLAGS", "CPPFLAGS"):
            if line.startswith(var + " =") or line.startswith(var + " :="):
                value = line.split("=", 1)[1].strip()
                for part in value.split():
                    if part.startswith(("-I", "-D", "-std=", "-W")):
                        flags.add(part)

    if flags:
        flags_file = os.path.join(project_path, "compile_flags.txt")
        try:
            with open(flags_file, "w", encoding="utf-8") as f:
                for flag in sorted(flags):
                    f.write(flag + "\n")
            results.append(LanguageDetection(
                language="cpp",
                build_info={"compile_flags_file": flags_file},
                source=DetectionSource.MAKEFILE,
                confidence=0.6,
                notes=["Generated compile_flags.txt from make -pq"],
            ))
        except OSError as e:
            logger.debug("Failed to write compile_flags.txt: %s", e)

    return results


# ---------------------------------------------------------------------------
# JVM Active detection (build tool commands)
# ---------------------------------------------------------------------------

async def _detect_maven_active(project_path):
    """Run Maven commands to extract project configuration."""
    results = []

    if not os.path.isfile(os.path.join(project_path, "pom.xml")):
        return results

    build_info = {"build_system": "maven"}
    notes = ["Maven active detection"]

    # Get Java version
    rc, stdout, stderr = await _run_command(
        ["mvn", "help:evaluate", "-Dexpression=maven.compiler.source",
         "-DforceStdout", "-q", "-B"],
        cwd=project_path,
        timeout=30,
    )
    if rc == 0 and stdout.strip() and not stdout.strip().startswith("["):
        build_info["java_version"] = stdout.strip()
        notes.append("Maven compiler.source: %s" % stdout.strip())

    # Get source roots
    rc, stdout, stderr = await _run_command(
        ["mvn", "help:evaluate", "-Dexpression=project.compileSourceRoots",
         "-DforceStdout", "-q", "-B"],
        cwd=project_path,
        timeout=30,
    )
    if rc == 0 and stdout.strip():
        roots = [r.strip() for r in stdout.strip().splitlines() if r.strip()]
        if roots:
            build_info["source_roots"] = roots

    if len(build_info) > 1:
        results.append(LanguageDetection(
            language="java",
            build_info=build_info,
            source=DetectionSource.MAVEN,
            confidence=0.9,
            notes=notes,
        ))

    return results


async def _detect_gradle_active(project_path):
    """Run Gradle commands to extract project configuration."""
    results = []

    has_gradle = any(
        os.path.isfile(os.path.join(project_path, f))
        for f in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")
    )
    if not has_gradle:
        return results

    build_info = {"build_system": "gradle"}
    notes = ["Gradle active detection"]

    # Use gradlew if available
    gradle_cmd = "gradle"
    for wrapper in ("./gradlew", "gradlew", "gradlew.bat"):
        if os.path.isfile(os.path.join(project_path, wrapper)):
            gradle_cmd = os.path.join(project_path, wrapper)
            break

    rc, stdout, stderr = await _run_command(
        [gradle_cmd, "properties", "-q"],
        cwd=project_path,
        timeout=60,
    )
    if rc == 0:
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("sourceCompatibility:"):
                value = line.split(":", 1)[1].strip()
                if value and value != "null":
                    build_info["java_version"] = value
                    notes.append("Gradle sourceCompatibility: %s" % value)
            elif line.startswith("targetCompatibility:"):
                value = line.split(":", 1)[1].strip()
                if value and value != "null":
                    build_info["target_version"] = value

    if len(build_info) > 1:
        results.append(LanguageDetection(
            language="java",
            build_info=build_info,
            source=DetectionSource.GRADLE,
            confidence=0.9,
            notes=notes,
        ))

    return results


# ---------------------------------------------------------------------------
# Merging and orchestration
# ---------------------------------------------------------------------------

def _merge_detections(detections):
    """Merge multiple detections for the same language.

    IDE-sourced detections take priority. Higher confidence wins for build_info keys.
    """
    by_language = {}
    for d in detections:
        if d.language not in by_language:
            by_language[d.language] = d
            continue

        existing = by_language[d.language]
        # Higher confidence wins overall
        if d.confidence > existing.confidence:
            # Merge build_info from existing into new (new takes priority)
            merged_info = dict(existing.build_info)
            merged_info.update(d.build_info)
            d.build_info = merged_info
            d.notes = existing.notes + d.notes
            by_language[d.language] = d
        else:
            # Merge build_info from new into existing (existing takes priority)
            for k, v in d.build_info.items():
                if k not in existing.build_info:
                    existing.build_info[k] = v
            existing.notes.extend(d.notes)
            if d.workspace_root and not existing.workspace_root:
                existing.workspace_root = d.workspace_root

    return list(by_language.values())


async def detect_project(project_path, allow_active=False):
    """Detect project languages and build configurations.

    Args:
        project_path: Absolute path to the project root.
        allow_active: If True, run build tool commands for deeper detection.

    Returns:
        DetectionResult with detected languages and any errors.
    """
    project_path = os.path.realpath(project_path)
    result = DetectionResult(project_path=project_path)

    if not os.path.isdir(project_path):
        result.errors.append("Project path does not exist: %s" % project_path)
        return result

    all_detections = []
    errors = []

    # --- C/C++ detection ---

    # Phase 1: IDE metadata (highest priority)
    cpp_ide_detections = []
    for detector in (_detect_clion, _detect_vscode_cpp, _detect_cmake_presets, _detect_clangd_config):
        try:
            cpp_ide_detections.extend(detector(project_path))
        except Exception as e:
            errors.append("C/C++ IDE detection error (%s): %s" % (detector.__name__, e))

    has_cpp_cdb = any(d.build_info.get("compile_commands_dir") for d in cpp_ide_detections)

    # Phase 2: Build artifacts (skip if IDE already found CDB)
    cpp_artifact_detections = []
    if not has_cpp_cdb:
        for detector in (_detect_compile_commands, _detect_compile_flags):
            try:
                cpp_artifact_detections.extend(detector(project_path))
            except Exception as e:
                errors.append("C/C++ artifact detection error (%s): %s" % (detector.__name__, e))

    has_cpp_cdb = has_cpp_cdb or any(
        d.build_info.get("compile_commands_dir") for d in cpp_artifact_detections)

    # Phase 3: Active generation (only if allowed and no CDB found)
    cpp_active_detections = []
    if allow_active and not has_cpp_cdb:
        for generator in (_generate_cmake, _generate_meson, _generate_ninja_compdb,
                          _generate_compiledb_make, _generate_make_flags):
            try:
                detections = await generator(project_path)
                cpp_active_detections.extend(detections)
                if any(d.build_info.get("compile_commands_dir") for d in detections):
                    break  # Got CDB, stop trying
            except Exception as e:
                errors.append("C/C++ active detection error (%s): %s" % (generator.__name__, e))

    all_detections.extend(cpp_ide_detections)
    all_detections.extend(cpp_artifact_detections)
    all_detections.extend(cpp_active_detections)

    # --- Java/Kotlin detection ---

    # Phase 1: IDE metadata
    jvm_ide_detections = []
    for detector in (_detect_intellij_jvm, _detect_eclipse_jvm, _detect_vscode_jvm):
        try:
            jvm_ide_detections.extend(detector(project_path))
        except Exception as e:
            errors.append("JVM IDE detection error (%s): %s" % (detector.__name__, e))

    # Phase 2: Build system (passive)
    jvm_build_detections = []
    if not jvm_ide_detections:
        for detector in (_detect_gradle, _detect_maven):
            try:
                jvm_build_detections.extend(detector(project_path))
            except Exception as e:
                errors.append("JVM build detection error (%s): %s" % (detector.__name__, e))

    # Phase 3: Active queries
    jvm_active_detections = []
    if allow_active and not jvm_ide_detections:
        for generator in (_detect_maven_active, _detect_gradle_active):
            try:
                jvm_active_detections.extend(await generator(project_path))
            except Exception as e:
                errors.append("JVM active detection error (%s): %s" % (generator.__name__, e))

    all_detections.extend(jvm_ide_detections)
    all_detections.extend(jvm_build_detections)
    all_detections.extend(jvm_active_detections)

    # Merge detections per language
    result.languages = _merge_detections(all_detections)
    result.errors = errors

    return result
