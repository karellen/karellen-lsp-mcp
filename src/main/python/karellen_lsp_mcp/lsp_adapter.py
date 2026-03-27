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

"""LSP adapters: bridge between project detection and LSP server registration.

Each adapter registers for one or more languages and knows how to produce
an LSP command, init_options, and workspace root from detection results.
The registry consults the adapter instead of hardcoding per-language logic.
"""

import hashlib
import logging
import os
import shutil as _shutil
import urllib.parse

from platformdirs import user_data_dir as _user_data_dir

logger = logging.getLogger(__name__)


def _project_managed_dir(project_path, adapter_name):
    """Return a per-project, per-adapter managed directory.

    Structure: <user_data_dir>/karellen-lsp-mcp/projects/{name}-{hash}/{adapter_name}/

    Uses the project directory basename for readability, with a short hash
    suffix for uniqueness when two projects share the same basename.
    """
    name = os.path.basename(project_path) or "root"
    h = hashlib.sha256(project_path.encode("utf-8")).hexdigest()[:8]
    project_dir = "%s-%s" % (name, h)
    return os.path.join(_user_data_dir("karellen-lsp-mcp"),
                        "projects", project_dir, adapter_name)


class LspAdapterConfig:
    """Configuration produced by an adapter for starting an LSP server."""
    __slots__ = ("command", "root_uri", "init_options")

    def __init__(self, command, root_uri, init_options=None):
        self.command = command
        self.root_uri = root_uri
        self.init_options = init_options


class LspAdapter:
    """Base class for LSP server adapters.

    Subclasses declare which languages they handle and implement
    configure() to produce an LspAdapterConfig from registration parameters.
    """

    # Subclasses set this to a list of language identifiers they handle.
    # The first entry is the canonical language used for project ID
    # computation, so all aliases share one LSP server instance.
    languages = ()

    # Name used for per-project managed directories. Subclasses should set this.
    managed_dir_name = None

    def check_server(self):
        """Check if the LSP server binary is available.

        Returns:
            (available, install_hint) tuple. available is True if the server
            is on PATH. install_hint is a human-readable installation
            instruction string (returned regardless of availability).
        """
        return True, None

    def configure(self, project_path, language, lsp_command=None,
                  build_info=None, detection_details=None):
        """Produce an LspAdapterConfig for starting the LSP server.

        Args:
            project_path: Absolute real path to the project root.
            language: The language being registered.
            lsp_command: Explicit LSP command override, or None for default.
            build_info: Build configuration dict from detection or explicit.
            detection_details: The details dict from DetectedLanguage, or None.

        Returns:
            LspAdapterConfig with command, root_uri, and init_options.

        Raises:
            ValueError: If the adapter cannot configure (e.g., LSP server not found).
        """
        raise NotImplementedError

    def clean_managed_data(self, project_path):
        """Remove all managed data directories for this adapter and project.

        Called before force-restarting an LSP server to ensure a clean
        regeneration of indexes, compilation databases, etc.
        """
        if self.managed_dir_name is None:
            return
        managed = _project_managed_dir(project_path, self.managed_dir_name)
        if os.path.isdir(managed):
            _shutil.rmtree(managed)
            logger.info("Cleaned managed data: %s", managed)


def _path_to_uri(path):
    return "file://%s" % urllib.parse.quote(path, safe="/:@")


# ---------------------------------------------------------------------------
# Compile commands staleness detection
# ---------------------------------------------------------------------------

# Build config files whose modification invalidates compile_commands.json
_BUILD_CONFIG_GLOBS = (
    "CMakeLists.txt",
    "meson.build",
    "meson_options.txt",
)


def _newest_mtime_under(project_path, filenames):
    """Return the newest mtime of any file matching filenames in the tree.

    Walks the project tree looking for files with the given basenames.
    Returns 0.0 if none are found.
    """
    newest = 0.0
    for dirpath, dirnames, files in os.walk(project_path):
        # Skip hidden dirs and common non-source dirs
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".")
                       and d not in ("node_modules", "__pycache__",
                                     ".git", "target")]
        for name in filenames:
            if name in files:
                path = os.path.join(dirpath, name)
                try:
                    mt = os.path.getmtime(path)
                    if mt > newest:
                        newest = mt
                except OSError:
                    pass
    return newest


def _is_compile_commands_stale(cc_path, project_path):
    """Check if compile_commands.json is stale.

    Stale if:
    1. Any build config file (CMakeLists.txt, meson.build) in the project
       tree is newer than compile_commands.json, OR
    2. A sample of source files referenced in compile_commands.json no
       longer exist.

    Returns True if stale, False if fresh or if staleness cannot be
    determined (e.g., file unreadable).
    """
    try:
        cc_mtime = os.path.getmtime(cc_path)
    except OSError:
        return False

    # Check 1: build config files newer than compile_commands.json
    config_mtime = _newest_mtime_under(project_path,
                                       _BUILD_CONFIG_GLOBS)
    if config_mtime > cc_mtime:
        logger.info("compile_commands.json (%s) is older than build config "
                    "files in %s", cc_path, project_path)
        return True

    # Check 2: all source files for dead references
    try:
        import json
        with open(cc_path, "r") as f:
            entries = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return False

    if not isinstance(entries, list) or not entries:
        return False

    total = 0
    missing = []
    for entry in entries:
        src = entry.get("file", "")
        if not src:
            continue
        total += 1
        if not os.path.isabs(src):
            directory = entry.get("directory", project_path)
            src = os.path.join(directory, src)
        if not os.path.exists(src):
            missing.append(src)

    if total > 0 and len(missing) / total >= 0.05:
        logger.info("compile_commands.json (%s) has %.0f%% missing "
                    "source files (%d/%d): %s", cc_path,
                    100.0 * len(missing) / total,
                    len(missing), total,
                    ", ".join(missing[:5]))
        return True

    return False


# ---------------------------------------------------------------------------
# Clangd Adapter
# ---------------------------------------------------------------------------

class ClangdAdapter(LspAdapter):
    """Adapter for clangd (C/C++).

    Handles compile_commands.json discovery and generation:
    1. Uses explicit build_info.compile_commands_dir if provided
    2. Uses compile_commands_dir from detection details
    3. For CMake projects without compile_commands.json, generates one
       by running cmake with -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
    """

    languages = ("c", "cpp")
    managed_dir_name = "clangd"

    def check_server(self):
        available = _shutil.which("clangd") is not None
        hint = ("Install clang-tools-extra (Fedora/RHEL), "
                "clangd (Debian/Ubuntu), or llvm (macOS).")
        return available, hint

    def configure(self, project_path, language, lsp_command=None,
                  build_info=None, detection_details=None):
        cmd = list(lsp_command) if lsp_command else ["clangd"]
        bi = build_info or {}
        details = detection_details or {}

        is_clangd = cmd[0].endswith("clangd")

        compile_commands_dir = self._resolve_compile_commands_dir(
            project_path, bi, details)

        if is_clangd:
            # Only add --compile-commands-dir if not already specified
            has_cc_dir = any(
                a.startswith("--compile-commands-dir") for a in cmd)
            if compile_commands_dir and not has_cc_dir:
                cmd.append("--compile-commands-dir=%s"
                           % compile_commands_dir)

            # Enable background indexing for cross-file queries
            if "--background-index" not in cmd:
                cmd.append("--background-index")

        return LspAdapterConfig(
            command=cmd,
            root_uri=_path_to_uri(project_path),
        )

    def _resolve_compile_commands_dir(self, project_path, build_info, details):
        """Find or generate compile_commands.json in a managed directory.

        Never pollutes the project tree. Copies existing compile_commands.json
        to a managed dir, or generates one via cmake/meson and copies it.
        If an existing compile_commands.json is stale (older than build config
        files or referencing missing source files), regenerates instead of
        copying for CMake/Meson projects.
        Returns the managed directory path, or None.
        """
        managed = _project_managed_dir(project_path, "clangd")
        build_system = details.get("build_system")
        can_regenerate = build_system in ("cmake", "meson")

        # Collect candidate compile_commands.json sources
        candidates = []

        # 1. Explicit from build_info
        source_dir = build_info.get("compile_commands_dir")
        if source_dir:
            candidates.append(source_dir)

        if not candidates and build_info.get("build_dir"):
            cc_path = os.path.join(build_info["build_dir"],
                                   "compile_commands.json")
            if os.path.exists(cc_path):
                candidates.append(build_info["build_dir"])

        # 2. From detection
        if not candidates and details.get("compile_commands_dir"):
            candidates.append(details["compile_commands_dir"])

        # Try each candidate — if stale and we can regenerate, skip it
        for candidate in candidates:
            cc_path = os.path.join(candidate, "compile_commands.json")
            if not os.path.isfile(cc_path):
                continue
            if can_regenerate and _is_compile_commands_stale(
                    cc_path, project_path):
                logger.info("Stale compile_commands.json in %s, "
                            "will regenerate", candidate)
                continue
            logger.info("Fresh compile_commands.json in %s, copying",
                        candidate)
            return self._copy_to_managed(candidate, managed)

        # 3. Generate for CMake projects
        if build_system == "cmake":
            return self._generate_cmake_compile_commands(
                project_path, details, managed)

        # 4. Generate for Meson projects
        if build_system == "meson":
            return self._generate_meson_compile_commands(
                project_path, managed)

        # 5. Stale candidates are better than nothing when we can't regenerate
        for candidate in candidates:
            result = self._copy_to_managed(candidate, managed)
            if result:
                logger.warning("Using stale compile_commands.json from %s "
                               "(no build system to regenerate)", candidate)
                return result

        return None

    def _copy_to_managed(self, source_dir, managed_dir):
        """Copy compile_commands.json from source_dir to managed_dir."""
        source = os.path.join(source_dir, "compile_commands.json")
        if not os.path.isfile(source):
            return None
        os.makedirs(managed_dir, exist_ok=True)
        dest = os.path.join(managed_dir, "compile_commands.json")
        _shutil.copy2(source, dest)
        logger.info("Copied compile_commands.json to %s", managed_dir)
        return managed_dir

    def _generate_cmake_compile_commands(self, project_path, details,
                                         managed_dir):
        """Generate compile_commands.json for a CMake project.

        First checks if any existing build dir already has a fresh
        compile_commands.json and copies it. Otherwise, creates an
        out-of-tree build under the managed dir — never modifies the
        project tree.
        """
        import subprocess

        cmake_build_dirs = details.get("cmake_build_dirs", [])

        # Check if an existing build dir has a fresh compile_commands
        for build_dir in cmake_build_dirs:
            cc_path = os.path.join(build_dir, "compile_commands.json")
            if os.path.exists(cc_path):
                if _is_compile_commands_stale(cc_path, project_path):
                    logger.info("Stale compile_commands.json in %s, "
                                "skipping", build_dir)
                    continue
                return self._copy_to_managed(build_dir, managed_dir)

        # Create out-of-tree build under managed dir
        build_dir = os.path.join(managed_dir, "cmake-build")
        cmake_args = [
            "cmake",
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
            "-S", project_path,
            "-B", build_dir,
        ]

        try:
            logger.info("Generating compile_commands.json: %s",
                        " ".join(cmake_args))
            result = subprocess.run(
                cmake_args,
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                logger.warning("cmake failed (exit %d): %s",
                               result.returncode,
                               result.stderr[:500] if result.stderr else "")
                return None

            cc_path = os.path.join(build_dir, "compile_commands.json")
            if os.path.exists(cc_path):
                logger.info("Generated compile_commands.json at %s",
                            build_dir)
                return build_dir
        except FileNotFoundError:
            logger.debug("cmake not found on PATH")
        except subprocess.TimeoutExpired:
            logger.warning("cmake timed out after 120s")
        except OSError as e:
            logger.debug("Failed to run cmake: %s", e)

        return None

    def _generate_meson_compile_commands(self, project_path, managed_dir):
        """Run meson setup under managed dir to generate compile_commands.json."""
        import subprocess

        build_dir = os.path.join(managed_dir, "meson-build")
        cc_path = os.path.join(build_dir, "compile_commands.json")
        if os.path.isfile(cc_path):
            return build_dir

        try:
            meson_args = ["meson", "setup", build_dir, project_path]
            logger.info("Generating compile_commands.json: %s",
                        " ".join(meson_args))
            result = subprocess.run(
                meson_args,
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                logger.warning("meson setup failed (exit %d): %s",
                               result.returncode,
                               result.stderr[:500] if result.stderr else "")
                return None

            if os.path.exists(cc_path):
                logger.info("Generated compile_commands.json at %s",
                            build_dir)
                return build_dir
        except FileNotFoundError:
            logger.debug("meson not found on PATH")
        except subprocess.TimeoutExpired:
            logger.warning("meson timed out after 120s")
        except OSError as e:
            logger.debug("Failed to run meson: %s", e)

        return None


# ---------------------------------------------------------------------------
# jdtls Adapter
# ---------------------------------------------------------------------------

def _jdtls_workspace_dir(project_path):
    """Generate a per-project jdtls workspace directory path."""
    return os.path.join(_project_managed_dir(project_path, "jdtls"),
                        "workspace")


class JdtlsAdapter(LspAdapter):
    """Adapter for Eclipse jdtls (Java, Kotlin, Scala, Groovy)."""

    languages = ("java", "kotlin")
    managed_dir_name = "jdtls"

    def check_server(self):
        available = _shutil.which("jdtls") is not None
        hint = "Run: pip install karellen-jdtls-kotlin"
        return available, hint

    def configure(self, project_path, language, lsp_command=None,
                  build_info=None, detection_details=None):
        bi = build_info or {}
        details = detection_details or {}

        # Determine workspace root — for multi-module Gradle projects,
        # use the settings.gradle root instead of the submodule
        workspace_root = bi.get("project_root", project_path)

        # Build command
        if lsp_command:
            cmd = list(lsp_command)
        else:
            jdtls = _shutil.which("jdtls")
            if jdtls is None:
                raise ValueError(
                    "jdtls not found on PATH. Install it or specify lsp_command explicitly.")
            cmd = [jdtls]

        # Ensure -data workspace directory is set
        if not any(arg == "-data" for arg in cmd):
            data_dir = _jdtls_workspace_dir(workspace_root)
            cmd.extend(["-data", data_dir])

        # Build init_options from detection details
        init_options = self._build_init_options(details)

        return LspAdapterConfig(
            command=cmd,
            root_uri=_path_to_uri(workspace_root),
            init_options=init_options,
        )

    def _build_init_options(self, details):
        settings = {}

        # Java home — resolved path from JetBrains SDK, VS Code, or explicit
        java_sdk_path = details.get("java_sdk_path")
        if not java_sdk_path:
            # VS Code stores java_sdk as an absolute path directly
            java_sdk = details.get("java_sdk")
            if java_sdk and os.path.isdir(java_sdk):
                java_sdk_path = java_sdk

        if java_sdk_path:
            settings["java.home"] = java_sdk_path
            # Also set Gradle JDK so Gradle uses the project JDK, not system
            settings["java.import.gradle.java.home"] = java_sdk_path

        # Gradle wrapper
        gradle_modules_source = details.get("gradle_modules_source")
        if gradle_modules_source:
            settings["java.import.gradle.wrapper.enabled"] = True

        if settings:
            return {"settings": settings}
        return None


# ---------------------------------------------------------------------------
# Adapter Registry
# ---------------------------------------------------------------------------

_ADAPTERS = {}  # language -> LspAdapter


def register_adapter(adapter):
    """Register an adapter for its declared languages."""
    for lang in adapter.languages:
        _ADAPTERS[lang] = adapter


def get_adapter(language):
    """Get the adapter for a language, or None if not registered."""
    return _ADAPTERS.get(language)


def canonicalize_language(language):
    """Return the canonical language for project ID computation.

    All aliases sharing an adapter map to the adapter's first declared
    language, so e.g. "c" and "cpp" share one LSP server instance.
    Returns the language unchanged if no adapter is registered.
    """
    adapter = _ADAPTERS.get(language)
    if adapter is not None and adapter.languages:
        return adapter.languages[0]
    return language


def get_supported_languages():
    """Return set of all languages with registered adapters."""
    return set(_ADAPTERS.keys())


# Register built-in adapters
register_adapter(ClangdAdapter())
register_adapter(JdtlsAdapter())
