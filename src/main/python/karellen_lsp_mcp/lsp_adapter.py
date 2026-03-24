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


def _managed_dir(subdir, project_path):
    """Return a per-project managed directory under the platform data dir.

    Structure: <user_data_dir>/karellen-lsp-mcp/{subdir}/{hash}/
    """
    h = hashlib.sha256(project_path.encode("utf-8")).hexdigest()[:16]
    return os.path.join(_user_data_dir("karellen-lsp-mcp"), subdir, h)


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
    # One adapter can register for multiple languages (e.g., jdtls handles
    # java, kotlin, scala, groovy).
    languages = ()

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


def _path_to_uri(path):
    return "file://%s" % urllib.parse.quote(path, safe="/:@")


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

        compile_commands_dir = self._resolve_compile_commands_dir(
            project_path, bi, details)

        if cmd[0].endswith("clangd") and compile_commands_dir:
            cmd.append("--compile-commands-dir=%s" % compile_commands_dir)

        return LspAdapterConfig(
            command=cmd,
            root_uri=_path_to_uri(project_path),
        )

    def _resolve_compile_commands_dir(self, project_path, build_info, details):
        """Find or generate compile_commands.json in a managed directory.

        Never pollutes the project tree. Copies existing compile_commands.json
        to a managed dir, or generates one via cmake/meson and copies it.
        Returns the managed directory path, or None.
        """
        managed = _managed_dir("compile-commands", project_path)

        # 1. Explicit from build_info — copy to managed dir
        source_dir = build_info.get("compile_commands_dir")
        if source_dir:
            return self._copy_to_managed(source_dir, managed)

        if build_info.get("build_dir"):
            cc_path = os.path.join(build_info["build_dir"],
                                   "compile_commands.json")
            if os.path.exists(cc_path):
                return self._copy_to_managed(build_info["build_dir"], managed)

        # 2. From detection — copy to managed dir
        if details.get("compile_commands_dir"):
            return self._copy_to_managed(details["compile_commands_dir"],
                                         managed)

        # 3. Generate for CMake projects
        build_system = details.get("build_system")
        if build_system == "cmake":
            return self._generate_cmake_compile_commands(
                project_path, details, managed)

        # 4. Generate for Meson projects
        if build_system == "meson":
            return self._generate_meson_compile_commands(
                project_path, managed)

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
        """Re-run cmake with -DCMAKE_EXPORT_COMPILE_COMMANDS=ON in the
        existing build dir, then copy the result to managed_dir.

        If no existing build dir, creates an out-of-tree build under
        the managed dir itself (not in the project tree).
        """
        import subprocess

        cmake_build_dirs = details.get("cmake_build_dirs", [])

        # Check if an existing build dir already has compile_commands
        for build_dir in cmake_build_dirs:
            cc_path = os.path.join(build_dir, "compile_commands.json")
            if os.path.exists(cc_path):
                return self._copy_to_managed(build_dir, managed_dir)

        # Re-configure existing build dir to enable export
        if cmake_build_dirs:
            build_dir = cmake_build_dirs[0]
            cmake_args = [
                "cmake",
                "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
                build_dir,
            ]
        else:
            # No existing build — create out-of-tree under managed dir
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
                if build_dir.startswith(managed_dir):
                    # Already under managed dir
                    logger.info("Generated compile_commands.json at %s",
                                build_dir)
                    return build_dir
                return self._copy_to_managed(build_dir, managed_dir)
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
    return _managed_dir("jdtls-workspaces", project_path)


class JdtlsAdapter(LspAdapter):
    """Adapter for Eclipse jdtls (Java, Kotlin, Scala, Groovy)."""

    languages = ("java", "kotlin")

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

        # Java home from detected SDK
        java_sdk = details.get("java_sdk")
        if java_sdk and os.path.isdir(java_sdk):
            # java_sdk is an absolute path (e.g., from VS Code settings)
            settings["java.home"] = java_sdk

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


def get_supported_languages():
    """Return set of all languages with registered adapters."""
    return set(_ADAPTERS.keys())


# Register built-in adapters
register_adapter(ClangdAdapter())
register_adapter(JdtlsAdapter())
