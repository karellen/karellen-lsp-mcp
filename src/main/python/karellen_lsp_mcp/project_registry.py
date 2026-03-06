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

"""Refcounted project registry mapping project_id -> (LspClient, refcount, metadata)."""

import asyncio
import hashlib
import logging
import os
import urllib.parse

from karellen_lsp_mcp.lsp_client import LspClient

logger = logging.getLogger(__name__)

DEFAULT_LSP_COMMANDS = {
    "c": ["clangd"],
    "cpp": ["clangd"],
}


class ProjectRegistryError(Exception):
    pass


class _ProjectEntry:
    __slots__ = ("project_id", "path", "language", "lsp_command", "build_info",
                 "client", "refcount", "status")

    def __init__(self, project_id, path, language, lsp_command, build_info):
        self.project_id = project_id
        self.path = path
        self.language = language
        self.lsp_command = lsp_command
        self.build_info = build_info or {}
        self.client = None
        self.refcount = 0
        self.status = "stopped"


def _compute_project_id(real_path, language):
    key = "%s|%s" % (real_path, language)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _build_lsp_command(language, lsp_command, build_info):
    """Build the full LSP server command, applying build_info options."""
    if lsp_command:
        cmd = list(lsp_command)
    else:
        default = DEFAULT_LSP_COMMANDS.get(language)
        if default is None:
            raise ProjectRegistryError("No default LSP command for language '%s'" % language)
        cmd = list(default)

    if language in ("c", "cpp") and cmd[0].endswith("clangd"):
        bi = build_info or {}
        if bi.get("compile_commands_dir"):
            cmd.append("--compile-commands-dir=%s" % bi["compile_commands_dir"])
        elif bi.get("build_dir"):
            cc_path = os.path.join(bi["build_dir"], "compile_commands.json")
            if os.path.exists(cc_path):
                cmd.append("--compile-commands-dir=%s" % bi["build_dir"])

    return cmd


class ProjectRegistry:
    """Manages project registrations and their LSP client instances."""

    def __init__(self, request_timeout=60, ready_timeout=120):
        self._projects = {}  # project_id -> _ProjectEntry
        self._request_timeout = request_timeout
        self._ready_timeout = ready_timeout
        self._lock = asyncio.Lock()

    async def register(self, project_path, language, lsp_command=None,
                       build_info=None, force=False):
        """Register a project, starting LSP server if new. Returns project_id."""
        real_path = os.path.realpath(project_path)
        if not os.path.isdir(real_path):
            raise ProjectRegistryError("Project path does not exist: %s" % real_path)

        language = language.lower()
        project_id = _compute_project_id(real_path, language)

        async with self._lock:
            if project_id in self._projects and not force:
                entry = self._projects[project_id]
                entry.refcount += 1
                if build_info:
                    entry.build_info.update(build_info)
                logger.info("Project %s refcount incremented to %d", project_id, entry.refcount)
                return project_id

            if project_id in self._projects and force:
                await self._stop_entry(self._projects[project_id])

            cmd = _build_lsp_command(language, lsp_command, build_info)
            root_uri = "file://%s" % urllib.parse.quote(real_path, safe="/:@")

            entry = _ProjectEntry(project_id, real_path, language, cmd, build_info)
            entry.status = "starting"
            self._projects[project_id] = entry

            try:
                client = LspClient(request_timeout=self._request_timeout,
                                   ready_timeout=self._ready_timeout)
                await client.start(cmd, root_uri)
                entry.client = client
                entry.status = client.state_name
                entry.refcount = 1
                logger.info("Project %s registered: %s (%s)", project_id, real_path, language)
            except Exception as e:
                entry.status = "error"
                del self._projects[project_id]
                raise ProjectRegistryError("Failed to start LSP server: %s" % e) from e

            return project_id

    async def deregister(self, project_id):
        """Decrement refcount; stop LSP server if it reaches 0."""
        async with self._lock:
            entry = self._projects.get(project_id)
            if entry is None:
                raise ProjectRegistryError("Unknown project: %s" % project_id)

            entry.refcount -= 1
            logger.info("Project %s refcount decremented to %d", project_id, entry.refcount)

            if entry.refcount <= 0:
                await self._stop_entry(entry)
                del self._projects[project_id]
                logger.info("Project %s removed", project_id)

    def list_projects(self):
        """Return list of project info dicts."""
        result = []
        for entry in self._projects.values():
            status = entry.client.state_name if entry.client else entry.status
            result.append({
                "project_id": entry.project_id,
                "path": entry.path,
                "language": entry.language,
                "refcount": entry.refcount,
                "status": status,
            })
        return result

    def get_client(self, project_id):
        """Get the LspClient for a project_id."""
        entry = self._projects.get(project_id)
        if entry is None:
            raise ProjectRegistryError("Unknown project: %s" % project_id)
        if entry.client is None:
            raise ProjectRegistryError("Project %s has no running LSP server" % project_id)
        return entry

    def has_projects(self):
        return len(self._projects) > 0

    async def shutdown_all(self):
        """Stop all LSP servers."""
        for entry in list(self._projects.values()):
            await self._stop_entry(entry)
        self._projects.clear()

    async def _stop_entry(self, entry):
        if entry.client is not None:
            try:
                await entry.client.stop()
            except Exception:
                logger.warning("Error stopping LSP server for %s", entry.project_id, exc_info=True)
            entry.client = None
        entry.status = "stopped"

    def validate_file_path(self, project_id, file_path):
        """Validate that file_path is absolute and under the project root. Returns file URI."""
        if not os.path.isabs(file_path):
            raise ProjectRegistryError("file_path must be absolute: %s" % file_path)

        entry = self._projects.get(project_id)
        if entry is None:
            raise ProjectRegistryError("Unknown project: %s" % project_id)

        real_file = os.path.realpath(file_path)
        if not real_file.startswith(entry.path + os.sep) and real_file != entry.path:
            raise ProjectRegistryError(
                "File %s is not under project root %s" % (real_file, entry.path)
            )

        return "file://%s" % urllib.parse.quote(real_file, safe="/:@")
