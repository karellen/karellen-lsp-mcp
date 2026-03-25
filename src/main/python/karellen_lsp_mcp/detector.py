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

"""Project language and build system autodetection.

Scans directory artifacts (build system markers, IDE metadata, language files)
to identify project languages, build systems, and configuration details.

Detection uses a credibility hierarchy when multiple sources provide the same
fact (e.g., Java version from build config vs IDE settings):

    Tier 1: Build system config (build.gradle.kts, pom.xml) — ground truth
    Tier 2: IDE build-sync metadata (.idea/compiler.xml, .idea/gradle.xml)
    Tier 3: IDE project-level settings (.idea/misc.xml, .idea/kotlinc.xml)
    Tier 4: IDE workspace settings (.vscode/settings.json, Eclipse .settings/)
    Tier 5: File system inference (presence of .kt files, directory structure)
"""

import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

TIER_BUILD_CONFIG = 1
TIER_IDE_BUILD_SYNC = 2
TIER_IDE_PROJECT = 3
TIER_IDE_WORKSPACE = 4
TIER_FILESYSTEM = 5


@dataclass
class IdeMetadata:
    """Metadata extracted from a single IDE configuration source."""
    ide: str                                      # "jetbrains", "eclipse", "vscode"
    tier: int                                     # credibility tier
    java_sdk: Optional[str] = None                # e.g. "azul-17", "17"
    java_language_level: Optional[str] = None     # e.g. "JDK_17", "17"
    kotlin_version: Optional[str] = None          # e.g. "1.9.23"
    kotlin_api_version: Optional[str] = None      # e.g. "1.9"
    bytecode_target: Optional[str] = None         # e.g. "17"
    source_roots: list[str] = field(default_factory=list)
    gradle_home: Optional[str] = None
    gradle_jvm: Optional[str] = None              # e.g. "azul-17"
    gradle_modules: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class DetectedLanguage:
    """A detected language/build-system pair with optional configuration."""
    language: str
    build_system: Optional[str] = None
    lsp_command: Optional[list[str]] = None
    init_options: Optional[dict] = None
    build_info: Optional[dict] = None
    confidence: str = "high"       # "high", "medium", "low"
    details: Optional[dict] = None


@dataclass
class DetectionResult:
    """Result of project detection."""
    project_path: str
    languages: list[DetectedLanguage] = field(default_factory=list)
    ide_metadata: list[IdeMetadata] = field(default_factory=list)


# ---------------------------------------------------------------------------
# IDE Metadata Readers
# ---------------------------------------------------------------------------

def _parse_xml_safe(path):
    """Parse an XML file, returning the root Element or None on any error."""
    try:
        return ET.parse(path).getroot()
    except (ET.ParseError, OSError) as e:
        logger.debug("Failed to parse %s: %s", path, e)
        return None


def _resolve_jetbrains_sdk_path(sdk_name):
    """Resolve a JetBrains SDK name (e.g. 'azul-17') to an actual JDK home path.

    Searches JetBrains jdk.table.xml files (most recent IDE config first)
    for the SDK name and returns the homePath if the directory exists.

    Returns the resolved absolute path, or None if not found.
    """
    if not sdk_name:
        return None

    config_base = os.path.join(os.path.expanduser("~"), ".config", "JetBrains")
    if not os.path.isdir(config_base):
        return None

    try:
        ide_dirs = sorted(os.listdir(config_base), reverse=True)
    except OSError:
        return None

    home = os.path.expanduser("~")
    for ide_dir in ide_dirs:
        table_path = os.path.join(config_base, ide_dir, "options",
                                  "jdk.table.xml")
        root = _parse_xml_safe(table_path)
        if root is None:
            continue
        for jdk in root.iter("jdk"):
            name_elem = jdk.find("name")
            home_elem = jdk.find("homePath")
            if (name_elem is not None and home_elem is not None
                    and name_elem.get("value") == sdk_name):
                jdk_path = home_elem.get("value", "")
                jdk_path = jdk_path.replace("$USER_HOME$", home)
                if os.path.isdir(jdk_path):
                    return jdk_path

    return None


def _read_jetbrains_metadata(project_path):
    """Read JetBrains .idea/ metadata. Returns list of IdeMetadata (one per source file)."""
    idea_dir = os.path.join(project_path, ".idea")
    if not os.path.isdir(idea_dir):
        return []

    results = []

    # misc.xml — project SDK and language level (tier 3: IDE project-level)
    misc_path = os.path.join(idea_dir, "misc.xml")
    root = _parse_xml_safe(misc_path)
    if root is not None:
        meta = IdeMetadata(ide="jetbrains", tier=TIER_IDE_PROJECT)
        for comp in root.iter("component"):
            if comp.get("name") == "ProjectRootManager":
                sdk_name = comp.get("project-jdk-name")
                meta.java_sdk = sdk_name
                # Resolve SDK name to actual JDK path
                resolved = _resolve_jetbrains_sdk_path(sdk_name)
                if resolved:
                    meta.raw["java_sdk_path"] = resolved
                meta.java_language_level = comp.get("languageLevel")
                meta.raw["source"] = "misc.xml"
                break
        if meta.java_sdk or meta.java_language_level:
            results.append(meta)

    # compiler.xml — bytecode target level (tier 2: IDE build-sync)
    compiler_path = os.path.join(idea_dir, "compiler.xml")
    root = _parse_xml_safe(compiler_path)
    if root is not None:
        meta = IdeMetadata(ide="jetbrains", tier=TIER_IDE_BUILD_SYNC)
        for elem in root.iter("bytecodeTargetLevel"):
            meta.bytecode_target = elem.get("target")
            meta.raw["source"] = "compiler.xml"
            break
        if meta.bytecode_target:
            results.append(meta)

    # gradle.xml — Gradle project settings (tier 2: IDE build-sync)
    gradle_path = os.path.join(idea_dir, "gradle.xml")
    root = _parse_xml_safe(gradle_path)
    if root is not None:
        for settings in root.iter("GradleProjectSettings"):
            meta = IdeMetadata(ide="jetbrains", tier=TIER_IDE_BUILD_SYNC)
            meta.raw["source"] = "gradle.xml"
            for opt in settings.iter("option"):
                name = opt.get("name", "")
                value = opt.get("value", "")
                if name == "gradleHome":
                    meta.gradle_home = value
                elif name == "gradleJvm":
                    meta.gradle_jvm = value
                elif name == "modules":
                    for mod_opt in opt.iter("option"):
                        mod_val = mod_opt.get("value", "")
                        if mod_val:
                            # Resolve $PROJECT_DIR$ placeholder
                            mod_val = mod_val.replace("$PROJECT_DIR$", project_path)
                            meta.gradle_modules.append(mod_val)
            if meta.gradle_home or meta.gradle_jvm or meta.gradle_modules:
                results.append(meta)
            break  # only first GradleProjectSettings

    # kotlinc.xml — Kotlin compiler settings (tier 3: IDE project-level)
    kotlinc_path = os.path.join(idea_dir, "kotlinc.xml")
    root = _parse_xml_safe(kotlinc_path)
    if root is not None:
        meta = IdeMetadata(ide="jetbrains", tier=TIER_IDE_PROJECT)
        meta.raw["source"] = "kotlinc.xml"
        for opt in root.iter("option"):
            name = opt.get("name", "")
            value = opt.get("value", "")
            if name == "version":
                meta.kotlin_version = value
            elif name == "languageVersion":
                meta.kotlin_version = value
            elif name == "apiVersion":
                meta.kotlin_api_version = value
        if meta.kotlin_version or meta.kotlin_api_version:
            results.append(meta)

    # modules.xml + *.iml — source roots (tier 2: IDE build-sync)
    modules_path = os.path.join(idea_dir, "modules.xml")
    root = _parse_xml_safe(modules_path)
    if root is not None:
        source_roots = []
        for module_elem in root.iter("module"):
            filepath = module_elem.get("filepath", "")
            filepath = filepath.replace("$PROJECT_DIR$", project_path)
            if filepath and os.path.isfile(filepath):
                iml_root = _parse_xml_safe(filepath)
                if iml_root is not None:
                    for sf in iml_root.iter("sourceFolder"):
                        url = sf.get("url", "")
                        is_test = sf.get("isTestSource", "false") == "true"
                        if url and not is_test:
                            # Convert file:// URL to path
                            path = url.replace("file://", "")
                            path = path.replace("$MODULE_DIR$",
                                                os.path.dirname(filepath))
                            path = os.path.normpath(path)
                            source_roots.append(path)
        if source_roots:
            meta = IdeMetadata(ide="jetbrains", tier=TIER_IDE_BUILD_SYNC,
                               source_roots=source_roots)
            meta.raw["source"] = "modules.xml+iml"
            results.append(meta)

    return results


def _read_eclipse_metadata(project_path):
    """Read Eclipse .classpath/.project/.settings metadata. Returns list of IdeMetadata."""
    results = []

    # .classpath — source roots and JRE container (tier 4: IDE workspace)
    classpath_path = os.path.join(project_path, ".classpath")
    root = _parse_xml_safe(classpath_path)
    if root is not None:
        meta = IdeMetadata(ide="eclipse", tier=TIER_IDE_WORKSPACE)
        meta.raw["source"] = ".classpath"
        source_roots = []
        for entry in root.iter("classpathentry"):
            kind = entry.get("kind", "")
            path = entry.get("path", "")
            if kind == "src" and path:
                source_roots.append(os.path.join(project_path, path))
            elif kind == "con" and "JRE_CONTAINER" in path:
                # Extract Java version from path like
                # org.eclipse.jdt.launching.JRE_CONTAINER/.../JavaSE-17
                m = re.search(r"JavaSE-(\d+)", path)
                if m:
                    meta.java_sdk = m.group(1)
        if source_roots:
            meta.source_roots = source_roots
        if meta.java_sdk or meta.source_roots:
            results.append(meta)

    # .settings/org.eclipse.jdt.core.prefs — compiler compliance (tier 4)
    prefs_path = os.path.join(project_path, ".settings",
                              "org.eclipse.jdt.core.prefs")
    if os.path.isfile(prefs_path):
        meta = IdeMetadata(ide="eclipse", tier=TIER_IDE_WORKSPACE)
        meta.raw["source"] = "org.eclipse.jdt.core.prefs"
        try:
            with open(prefs_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("org.eclipse.jdt.core.compiler.compliance="):
                        meta.java_language_level = line.split("=", 1)[1].strip()
                    elif line.startswith("org.eclipse.jdt.core.compiler.source="):
                        meta.raw["compiler_source"] = line.split("=", 1)[1].strip()
        except OSError as e:
            logger.debug("Failed to read %s: %s", prefs_path, e)
        if meta.java_language_level:
            results.append(meta)

    return results


def _read_vscode_metadata(project_path):
    """Read VS Code .vscode/settings.json metadata. Returns list of IdeMetadata."""
    settings_path = os.path.join(project_path, ".vscode", "settings.json")
    if not os.path.isfile(settings_path):
        return []

    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Failed to read %s: %s", settings_path, e)
        return []

    meta = IdeMetadata(ide="vscode", tier=TIER_IDE_WORKSPACE)
    meta.raw["source"] = "settings.json"

    java_home = settings.get("java.jdt.ls.java.home") or settings.get("java.home")
    if java_home:
        meta.java_sdk = java_home

    runtimes = settings.get("java.configuration.runtimes")
    if isinstance(runtimes, list):
        for rt in runtimes:
            if isinstance(rt, dict) and rt.get("default"):
                meta.java_sdk = rt.get("path", meta.java_sdk)
                break

    if meta.java_sdk:
        return [meta]
    return []


def _read_all_ide_metadata(project_path):
    """Read metadata from all IDE sources. Returns list sorted by tier (best first)."""
    results = []
    results.extend(_read_jetbrains_metadata(project_path))
    results.extend(_read_eclipse_metadata(project_path))
    results.extend(_read_vscode_metadata(project_path))
    results.sort(key=lambda m: m.tier)
    return results


# ---------------------------------------------------------------------------
# Detector framework
# ---------------------------------------------------------------------------

class ProjectDetector:
    """Base class for language-specific project detectors."""

    def detect(self, project_path, ide_metadata):
        """Detect languages in the project.

        Args:
            project_path: Absolute path to the project root.
            ide_metadata: List of IdeMetadata from IDE metadata readers,
                         sorted by tier (most credible first).

        Returns:
            List of DetectedLanguage instances.
        """
        raise NotImplementedError


_DETECTORS = []


def register_detector(detector):
    """Register a project detector."""
    _DETECTORS.append(detector)


def detect_project(project_path):
    """Run all registered detectors against the project.

    Returns a DetectionResult with detected languages and IDE metadata.
    """
    real_path = os.path.realpath(project_path)
    if not os.path.isdir(real_path):
        return DetectionResult(project_path=real_path)

    ide_metadata = _read_all_ide_metadata(real_path)
    languages = []

    for detector in _DETECTORS:
        try:
            detected = detector.detect(real_path, ide_metadata)
            languages.extend(detected)
        except Exception:
            logger.warning("Detector %s failed for %s",
                           type(detector).__name__, real_path, exc_info=True)

    return DetectionResult(project_path=real_path,
                           languages=languages,
                           ide_metadata=ide_metadata)


# ---------------------------------------------------------------------------
# Java/Kotlin Detector
# ---------------------------------------------------------------------------

_GRADLE_MARKERS = ("settings.gradle", "settings.gradle.kts",
                   "build.gradle", "build.gradle.kts")
_MAVEN_MARKERS = ("pom.xml",)
_ANT_MARKERS = ("build.xml",)
_ECLIPSE_MARKERS = (".classpath",)

# Max directory depth when scanning for .kt files
_KOTLIN_SCAN_DEPTH = 3


def _has_any_file(directory, filenames):
    """Check if any of the given filenames exist in the directory."""
    for name in filenames:
        if os.path.exists(os.path.join(directory, name)):
            return True
    return False


def _detect_build_system(project_path):
    """Detect Java/Kotlin build system by marker file priority."""
    if _has_any_file(project_path, _GRADLE_MARKERS):
        return "gradle"
    if _has_any_file(project_path, _MAVEN_MARKERS):
        return "maven"
    if _has_any_file(project_path, _ANT_MARKERS):
        return "ant"
    if _has_any_file(project_path, _ECLIPSE_MARKERS):
        return "eclipse"
    return None


def _find_gradle_root(project_path, max_levels=5):
    """Walk up from project_path to find the Gradle settings file (multi-module root).

    Returns the root path if found above project_path, or project_path itself.
    """
    current = os.path.dirname(project_path)
    for _ in range(max_levels):
        if not current or current == os.path.dirname(current):
            break
        for marker in ("settings.gradle", "settings.gradle.kts"):
            if os.path.exists(os.path.join(current, marker)):
                return current
        current = os.path.dirname(current)
    return project_path


_INCLUDE_RE = re.compile(
    r"""include\s*\(?\s*["']([^"']+)["']\s*\)?""")


def _parse_gradle_settings_modules(project_path):
    """Parse settings.gradle(.kts) for include() declarations.

    Returns list of absolute module directory paths, or None if no settings file found.
    Handles both Groovy (settings.gradle) and Kotlin DSL (settings.gradle.kts).
    Module names like "headout.feature.auth" map to directory "headout.feature.auth".
    """
    for name in ("settings.gradle.kts", "settings.gradle"):
        settings_path = os.path.join(project_path, name)
        if not os.path.isfile(settings_path):
            continue
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as e:
            logger.debug("Failed to read %s: %s", settings_path, e)
            return None

        modules = []
        for match in _INCLUDE_RE.finditer(content):
            module_name = match.group(1)
            # Gradle uses ":" as path separator in module names
            module_dir = module_name.replace(":", os.sep).lstrip(os.sep)
            module_path = os.path.join(project_path, module_dir)
            if os.path.isdir(module_path):
                modules.append(module_path)
        return modules
    return None


def _parse_maven_modules(project_path):
    """Parse pom.xml for <modules><module> declarations.

    Returns list of absolute module directory paths, or None if no pom.xml found.
    """
    pom_path = os.path.join(project_path, "pom.xml")
    root = _parse_xml_safe(pom_path)
    if root is None:
        return None

    # Maven POM namespace handling — may or may not have namespace
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    modules = []
    for modules_elem in root.iter("%smodules" % ns):
        for module_elem in modules_elem.iter("%smodule" % ns):
            module_name = (module_elem.text or "").strip()
            if module_name:
                module_path = os.path.join(project_path, module_name)
                if os.path.isdir(module_path):
                    modules.append(module_path)
    return modules if modules else None


def _scan_kotlin_files(project_path, max_depth=_KOTLIN_SCAN_DEPTH):
    """Check if .kt or .kts source files exist under src/ with bounded depth.

    Returns True if any Kotlin source files are found.
    """
    src_dir = os.path.join(project_path, "src")
    if not os.path.isdir(src_dir):
        return False

    for root, dirs, files in os.walk(src_dir):
        depth = root[len(src_dir):].count(os.sep)
        if depth >= max_depth:
            dirs.clear()
            continue
        for f in files:
            if f.endswith(".kt") or f.endswith(".kts"):
                return True
    return False


# Standard source set directories for Gradle/Maven projects
_MAIN_SOURCE_DIRS = ("src/main/java", "src/main/kotlin")
_TEST_SOURCE_DIRS = ("src/test/java", "src/test/kotlin")
_RESOURCE_DIRS = ("src/main/resources", "src/test/resources")


def _scan_source_roots(project_path, modules=None):
    """Scan for conventional source directories that actually exist on disk.

    For multi-module projects, scans each module directory. Returns a dict with:
      - source_roots: list of existing main source directories
      - test_source_roots: list of existing test source directories
      - resource_roots: list of existing resource directories
    """
    dirs_to_scan = [project_path]
    if modules:
        dirs_to_scan.extend(m for m in modules if m != project_path)

    source_roots = []
    test_source_roots = []
    resource_roots = []

    for base in dirs_to_scan:
        if not os.path.isdir(base):
            continue
        for src_dir in _MAIN_SOURCE_DIRS:
            full = os.path.join(base, src_dir)
            if os.path.isdir(full):
                source_roots.append(full)
        for test_dir in _TEST_SOURCE_DIRS:
            full = os.path.join(base, test_dir)
            if os.path.isdir(full):
                test_source_roots.append(full)
        for res_dir in _RESOURCE_DIRS:
            full = os.path.join(base, res_dir)
            if os.path.isdir(full):
                resource_roots.append(full)

    return {
        "source_roots": source_roots,
        "test_source_roots": test_source_roots,
        "resource_roots": resource_roots,
    }


def _merge_details_by_credibility(ide_metadata):
    """Merge IDE metadata into a details dict, respecting credibility tiers.

    For each field, the first value found wins (metadata is sorted by tier,
    most credible first). All values are recorded with their source.
    """
    details = {}

    for meta in ide_metadata:
        source = meta.raw.get("source", meta.ide)

        if meta.java_sdk and "java_sdk" not in details:
            details["java_sdk"] = meta.java_sdk
            details["java_sdk_source"] = source
            details["java_sdk_tier"] = meta.tier
            # Resolved JDK home path (if available)
            sdk_path = meta.raw.get("java_sdk_path")
            if sdk_path:
                details["java_sdk_path"] = sdk_path

        if meta.java_language_level and "java_language_level" not in details:
            details["java_language_level"] = meta.java_language_level
            details["java_language_level_source"] = source
            details["java_language_level_tier"] = meta.tier

        if meta.bytecode_target and "bytecode_target" not in details:
            details["bytecode_target"] = meta.bytecode_target
            details["bytecode_target_source"] = source
            details["bytecode_target_tier"] = meta.tier

        if meta.kotlin_version and "kotlin_version" not in details:
            details["kotlin_version"] = meta.kotlin_version
            details["kotlin_version_source"] = source

        if meta.kotlin_api_version and "kotlin_api_version" not in details:
            details["kotlin_api_version"] = meta.kotlin_api_version

        if meta.source_roots and "source_roots" not in details:
            details["source_roots"] = meta.source_roots
            details["source_roots_source"] = source

        if meta.gradle_home and "gradle_home" not in details:
            details["gradle_home"] = meta.gradle_home

        if meta.gradle_jvm and "gradle_jvm" not in details:
            details["gradle_jvm"] = meta.gradle_jvm

        if meta.gradle_modules and "gradle_modules" not in details:
            details["gradle_modules"] = meta.gradle_modules

    return details


class JavaKotlinDetector(ProjectDetector):
    """Detects Java/Kotlin projects by build system markers and IDE metadata."""

    def detect(self, project_path, ide_metadata):
        build_system = _detect_build_system(project_path)

        if build_system is None:
            # Fall back: check if IDE metadata indicates Java project
            for meta in ide_metadata:
                if meta.java_sdk or meta.java_language_level or meta.bytecode_target:
                    build_system = "unknown"
                    break
            if build_system is None:
                return []

        # Detect Kotlin from IDE metadata or file scan
        has_kotlin = any(meta.kotlin_version for meta in ide_metadata)
        if not has_kotlin:
            has_kotlin = _scan_kotlin_files(project_path)

        # Build details from IDE metadata with credibility merge
        details = _merge_details_by_credibility(ide_metadata)

        if has_kotlin:
            details["kotlin_detected"] = True

        # Gradle multi-module root detection
        build_info = None
        if build_system == "gradle":
            gradle_root = _find_gradle_root(project_path)
            if gradle_root != project_path:
                build_info = {"project_root": gradle_root}

        # Discover modules: build config (tier 1) > IDE metadata (tier 2)
        modules = None
        if build_system == "gradle":
            modules = _parse_gradle_settings_modules(project_path)
            if modules is not None:
                details["gradle_modules"] = [project_path] + modules
                details["gradle_modules_source"] = "settings.gradle"
        elif build_system == "maven":
            modules = _parse_maven_modules(project_path)
            if modules is not None:
                details["maven_modules"] = [project_path] + modules
                details["maven_modules_source"] = "pom.xml"
        if modules is None:
            modules_from_ide = details.get("gradle_modules")
            if modules_from_ide:
                modules = modules_from_ide

        # Scan conventional source roots (tier 5: filesystem)
        scanned = _scan_source_roots(project_path, modules=modules)

        # IDE metadata source_roots (tier 2) are generated/additional roots.
        # Filesystem-scanned roots (tier 5) are the conventional ones.
        # Both are valuable — merge them, keeping IDE roots as "source_roots"
        # and adding conventional ones separately.
        if scanned["source_roots"]:
            details["source_roots"] = scanned["source_roots"]
            details["source_roots_source"] = "filesystem"
        if scanned["test_source_roots"]:
            details["test_source_roots"] = scanned["test_source_roots"]
        if scanned["resource_roots"]:
            details["resource_roots"] = scanned["resource_roots"]

        # Append IDE-detected generated source roots (annotation processors, etc.)
        ide_source_roots = None
        for meta in ide_metadata:
            if meta.source_roots:
                ide_source_roots = meta.source_roots
                break
        if ide_source_roots:
            details["generated_source_roots"] = ide_source_roots

        return [DetectedLanguage(
            language="java",
            build_system=build_system,
            confidence="high",
            details=details if details else None,
            build_info=build_info,
        )]


# ---------------------------------------------------------------------------
# C/C++ Detector
# ---------------------------------------------------------------------------

_CMAKE_MARKERS = ("CMakeLists.txt",)
_MESON_MARKERS = ("meson.build",)
_AUTOTOOLS_MARKERS = ("configure.ac", "configure.in")
_BAZEL_MARKERS = ("MODULE.bazel", "WORKSPACE", "WORKSPACE.bazel")

# Common build directory names to search for compile_commands.json
_BUILD_DIR_CANDIDATES = (
    "build", "cmake-build-debug", "cmake-build-release",
    "cmake-build-relwithdebinfo", "cmake-build-minsizerel",
    "out", "out/build", "builddir",
)

# Source/header extensions for C/C++
_C_EXTENSIONS = (".c", ".h")
_CPP_EXTENSIONS = (".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx")


def _find_compile_commands(project_path):
    """Search for compile_commands.json in project root and common build dirs.

    Returns the directory containing compile_commands.json, or None.
    """
    # Check project root first
    if os.path.isfile(os.path.join(project_path, "compile_commands.json")):
        return project_path

    # Check common build directories
    for candidate in _BUILD_DIR_CANDIDATES:
        build_dir = os.path.join(project_path, candidate)
        if os.path.isfile(os.path.join(build_dir, "compile_commands.json")):
            return build_dir

    return None


def _detect_cpp_build_system(project_path):
    """Detect C/C++ build system by marker file priority."""
    if _has_any_file(project_path, _CMAKE_MARKERS):
        return "cmake"
    if _has_any_file(project_path, _MESON_MARKERS):
        return "meson"
    if _has_any_file(project_path, _AUTOTOOLS_MARKERS):
        return "autotools"
    if _has_any_file(project_path, _BAZEL_MARKERS):
        return "bazel"
    # Check for standalone Makefile (not CMake-generated)
    makefile = os.path.join(project_path, "Makefile")
    if os.path.isfile(makefile):
        # If CMakeCache.txt exists alongside, this Makefile is CMake-generated
        if not os.path.isfile(os.path.join(project_path, "CMakeCache.txt")):
            return "make"
    return None


def _find_cmake_build_dirs(project_path):
    """Find existing CMake build directories by checking for CMakeCache.txt."""
    build_dirs = []
    for candidate in _BUILD_DIR_CANDIDATES:
        build_dir = os.path.join(project_path, candidate)
        if os.path.isfile(os.path.join(build_dir, "CMakeCache.txt")):
            build_dirs.append(build_dir)
    # Also check if project root is an in-tree build
    if os.path.isfile(os.path.join(project_path, "CMakeCache.txt")):
        build_dirs.insert(0, project_path)
    return build_dirs


def _read_cmake_cache_vars(cache_path, var_names):
    """Read specific variable values from CMakeCache.txt.

    Returns a dict of {var_name: value} for found variables.
    """
    result = {}
    wanted = set(var_names)
    try:
        with open(cache_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or line.startswith("//") or "=" not in line:
                    continue
                # Format: VAR_NAME:TYPE=VALUE
                key_type, _, value = line.partition("=")
                var_name = key_type.split(":")[0]
                if var_name in wanted:
                    result[var_name] = value
                    if len(result) == len(wanted):
                        break
    except OSError as e:
        logger.debug("Failed to read %s: %s", cache_path, e)
    return result


def _detect_cpp_language(project_path, max_files=50):
    """Determine whether the project is C, C++, or both.

    Scans top-level and src/ for source files. Returns "cpp" if any C++ files
    are found, "c" if only C files, or None if neither.
    """
    has_c = False
    has_cpp = False
    checked = 0

    dirs_to_check = [project_path]
    src_dir = os.path.join(project_path, "src")
    if os.path.isdir(src_dir):
        dirs_to_check.append(src_dir)

    for check_dir in dirs_to_check:
        for root, dirs, files in os.walk(check_dir):
            # Skip build dirs and hidden dirs
            dirs[:] = [d for d in dirs
                       if not d.startswith(".") and d not in
                       ("build", "cmake-build-debug", "cmake-build-release",
                        "out", "builddir", "node_modules", ".git")]
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in _CPP_EXTENSIONS:
                    has_cpp = True
                elif ext in _C_EXTENSIONS:
                    has_c = True
                else:
                    continue
                checked += 1
                if checked >= max_files:
                    break
            if checked >= max_files:
                break
        if checked >= max_files:
            break

    if has_cpp:
        return "cpp"
    if has_c:
        return "c"
    return None


class CppDetector(ProjectDetector):
    """Detects C/C++ projects by build system markers and compile databases."""

    def detect(self, project_path, ide_metadata):
        build_system = _detect_cpp_build_system(project_path)
        compile_commands_dir = _find_compile_commands(project_path)

        # If no build system markers and no compile_commands.json, check
        # if there are actually C/C++ source files
        if build_system is None and compile_commands_dir is None:
            return []

        # Determine language (C vs C++)
        language = _detect_cpp_language(project_path)
        if language is None:
            # Build system detected but no source files — still report it
            language = "cpp"

        details = {}

        details["build_system"] = build_system or "unknown"

        # compile_commands.json location
        if compile_commands_dir:
            details["compile_commands_dir"] = compile_commands_dir

        # Build system info
        if build_system == "cmake":
            build_dirs = _find_cmake_build_dirs(project_path)
            if build_dirs:
                details["cmake_build_dirs"] = build_dirs
                # Read compiler info from first build dir's cache
                cache_path = os.path.join(build_dirs[0], "CMakeCache.txt")
                cache_vars = _read_cmake_cache_vars(cache_path, (
                    "CMAKE_C_COMPILER", "CMAKE_CXX_COMPILER",
                    "CMAKE_BUILD_TYPE", "CMAKE_EXPORT_COMPILE_COMMANDS",
                ))
                if cache_vars.get("CMAKE_C_COMPILER"):
                    details["c_compiler"] = cache_vars["CMAKE_C_COMPILER"]
                if cache_vars.get("CMAKE_CXX_COMPILER"):
                    details["cxx_compiler"] = cache_vars["CMAKE_CXX_COMPILER"]
                if cache_vars.get("CMAKE_BUILD_TYPE"):
                    details["cmake_build_type"] = cache_vars["CMAKE_BUILD_TYPE"]

                # If compile_commands.json not found but a build dir exists,
                # check if CMAKE_EXPORT_COMPILE_COMMANDS was enabled
                if compile_commands_dir is None:
                    export_cdb = cache_vars.get("CMAKE_EXPORT_COMPILE_COMMANDS", "")
                    details["compile_commands_available"] = export_cdb.upper() == "ON"

        # Check for compile_flags.txt (simpler alternative)
        compile_flags_path = os.path.join(project_path, "compile_flags.txt")
        if os.path.isfile(compile_flags_path):
            details["compile_flags_txt"] = True

        # Check for clangd config
        clangd_config = os.path.join(project_path, ".clangd")
        if os.path.isfile(clangd_config):
            details["clangd_config"] = True

        # Build build_info for the adapter
        build_info = {}
        if compile_commands_dir:
            build_info["compile_commands_dir"] = compile_commands_dir

        return [DetectedLanguage(
            language=language,
            build_system=build_system or "unknown",
            confidence="high" if compile_commands_dir else "medium",
            details=details if details else None,
            build_info=build_info if build_info else None,
        )]


# Register detectors
register_detector(JavaKotlinDetector())
register_detector(CppDetector())


# ---------------------------------------------------------------------------
# Lightweight extension-based language scan
# ---------------------------------------------------------------------------

# Extension -> (language, label)
_EXT_LANGUAGE_MAP = {
    ".c": ("c", "C"),
    ".h": ("c", "C/C++ header"),
    ".cpp": ("c", "C++"),
    ".cxx": ("c", "C++"),
    ".cc": ("c", "C++"),
    ".hh": ("c", "C++ header"),
    ".hpp": ("c", "C++ header"),
    ".hxx": ("c", "C++ header"),
    ".java": ("java", "Java"),
    ".kt": ("kotlin", "Kotlin"),
    ".kts": ("kotlin", "Kotlin script"),
    ".scala": ("scala", "Scala"),
    ".groovy": ("groovy", "Groovy"),
    ".py": ("python", "Python"),
    ".pyx": ("python", "Cython"),
    ".rs": ("rust", "Rust"),
    ".go": ("go", "Go"),
    ".ts": ("typescript", "TypeScript"),
    ".tsx": ("typescript", "TypeScript/React"),
    ".js": ("javascript", "JavaScript"),
    ".jsx": ("javascript", "JavaScript/React"),
    ".rb": ("ruby", "Ruby"),
    ".cs": ("csharp", "C#"),
    ".fs": ("fsharp", "F#"),
    ".swift": ("swift", "Swift"),
    ".m": ("objc", "Objective-C"),
    ".mm": ("objc", "Objective-C++"),
    ".lua": ("lua", "Lua"),
    ".zig": ("zig", "Zig"),
    ".v": ("verilog", "Verilog"),
    ".sv": ("systemverilog", "SystemVerilog"),
    ".proto": ("protobuf", "Protocol Buffers"),
}

_SKIP_DIRS = frozenset({
    ".git", ".svn", ".hg", ".bzr",
    "node_modules", "__pycache__", ".tox", ".venv", "venv",
    "build", "dist", "target", "out", "bin", "obj",
    ".idea", ".vscode", ".eclipse", ".settings",
    ".gradle", ".mvn", ".cargo",
})


def scan_languages(project_path, max_files=50000):
    """Scan a project for file extensions and recommend LSP registrations.

    Returns a dict with:
      - languages: list of {language, label, extensions, file_count,
                            adapter_available, install_hint}
      - total_files: number of files scanned
      - skipped_dirs: directories skipped during scan
    """
    from karellen_lsp_mcp.lsp_adapter import get_adapter

    real_path = os.path.realpath(project_path)
    ext_counts = {}  # extension -> count
    total_files = 0

    for dirpath, dirnames, filenames in os.walk(real_path):
        # Prune skipped directories in-place
        dirnames[:] = [d for d in dirnames
                       if d not in _SKIP_DIRS and not d.startswith(".")]

        for fname in filenames:
            total_files += 1
            if total_files > max_files:
                break
            _, ext = os.path.splitext(fname)
            if ext:
                ext_counts[ext.lower()] = ext_counts.get(
                    ext.lower(), 0) + 1
        if total_files > max_files:
            break

    # Group by canonical language
    lang_info = {}  # language -> {label, extensions, file_count}
    for ext, count in ext_counts.items():
        entry = _EXT_LANGUAGE_MAP.get(ext)
        if entry is None:
            continue
        language, label = entry
        if language not in lang_info:
            lang_info[language] = {
                "label": label,
                "extensions": [],
                "file_count": 0,
            }
        info = lang_info[language]
        info["extensions"].append(ext)
        info["file_count"] += count
        # Use most specific label (prefer "C++" over "C/C++ header")
        if "header" not in label:
            info["label"] = label

    # Build recommendations sorted by file count
    recommendations = []
    for language, info in sorted(lang_info.items(),
                                 key=lambda x: -x[1]["file_count"]):
        adapter = get_adapter(language)
        rec = {
            "language": language,
            "label": info["label"],
            "extensions": sorted(info["extensions"]),
            "file_count": info["file_count"],
            "adapter_available": adapter is not None,
        }
        if adapter is not None:
            available, hint = adapter.check_server()
            rec["server_available"] = available
            if hint:
                rec["install_hint"] = hint
        else:
            rec["server_available"] = False
            rec["install_hint"] = ("No built-in adapter. Use "
                                   "lsp_register_project with explicit "
                                   "lsp_command.")
        recommendations.append(rec)

    return {
        "project_path": real_path,
        "languages": recommendations,
        "total_files": min(total_files, max_files),
    }
