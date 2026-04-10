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
import uuid

from karellen_lsp_mcp.lsp_adapter import get_adapter, canonicalize_language
from karellen_lsp_mcp.lsp_client import LspClient

logger = logging.getLogger(__name__)


class ProjectRegistryError(Exception):
    pass


class _ProjectEntry:
    __slots__ = ("project_id", "path", "language", "lsp_command", "build_info",
                 "client", "refcount", "status", "registration_ids")

    def __init__(self, project_id, path, language, lsp_command, build_info):
        self.project_id = project_id
        self.path = path
        self.language = language
        self.lsp_command = lsp_command
        self.build_info = build_info or {}
        self.client = None
        self.refcount = 0
        self.status = "stopped"
        self.registration_ids = set()


def _compute_project_id(real_path, language):
    key = "%s|%s" % (real_path, language)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


class ProjectRegistry:
    """Manages project registrations and their LSP client instances."""

    def __init__(self, request_timeout=60, ready_timeout=120):
        self._projects = {}  # project_id -> _ProjectEntry
        self._registrations = {}  # registration_id -> project_id
        self._request_timeout = request_timeout
        self._ready_timeout = ready_timeout
        self._lock = asyncio.Lock()

    async def register(self, project_path, language, lsp_command=None,
                       build_info=None, init_options=None,
                       detection_details=None, force=False):
        """Register a project, starting LSP server if new.

        Returns (project_id, registration_id). The registration_id is a
        unique token for this specific registration that must be used to
        deregister. Multiple registrations of the same project receive
        different registration_ids sharing the same project_id.
        """
        real_path = os.path.realpath(project_path)
        if not os.path.isdir(real_path):
            raise ProjectRegistryError("Project path does not exist: %s" % real_path)

        language = canonicalize_language(language.lower())
        project_id = _compute_project_id(real_path, language)

        async with self._lock:
            registration_id = uuid.uuid4().hex[:16]

            if project_id in self._projects and not force:
                entry = self._projects[project_id]
                entry.refcount += 1
                entry.registration_ids.add(registration_id)
                self._registrations[registration_id] = project_id
                if build_info:
                    entry.build_info.update(build_info)
                logger.info("Project %s refcount incremented to %d "
                            "(reg=%s)", project_id, entry.refcount,
                            registration_id)
                return project_id, registration_id

            if project_id in self._projects and force:
                old_entry = self._projects[project_id]
                # Invalidate all existing registrations for this project
                for old_reg_id in old_entry.registration_ids:
                    self._registrations.pop(old_reg_id, None)
                await self._stop_entry(old_entry)

            # Use adapter to build LSP configuration
            adapter = get_adapter(language)
            if adapter is not None:
                try:
                    config = adapter.configure(
                        real_path, language,
                        lsp_command=lsp_command,
                        build_info=build_info,
                        detection_details=detection_details,
                    )
                    cmd = config.command
                    root_uri = config.root_uri
                    server_label = config.server_label
                    if config.init_options and init_options is None:
                        init_options = config.init_options
                except ValueError as e:
                    raise ProjectRegistryError(str(e)) from e
            else:
                if lsp_command:
                    cmd = list(lsp_command)
                else:
                    raise ProjectRegistryError(
                        "No LSP adapter for language '%s'" % language)
                root_uri = "file://%s" % urllib.parse.quote(real_path, safe="/:@")
                server_label = None

            entry = _ProjectEntry(project_id, real_path, language, cmd, build_info)
            entry.status = "starting"
            self._projects[project_id] = entry

            try:
                # Log dir: use adapter's managed directory if available
                log_dir = None
                if adapter is not None and adapter.managed_dir_name:
                    from karellen_lsp_mcp.lsp_adapter import (
                        _project_managed_dir)
                    log_dir = _project_managed_dir(
                        real_path, adapter.managed_dir_name)

                client = LspClient(request_timeout=self._request_timeout,
                                   ready_timeout=self._ready_timeout)
                try:
                    await asyncio.wait_for(
                        client.start(cmd, root_uri,
                                     init_options=init_options,
                                     log_dir=log_dir,
                                     server_label=server_label),
                        timeout=60)
                except asyncio.TimeoutError:
                    # Kill the process if start timed out
                    try:
                        await client.stop()
                    except Exception:
                        pass
                    raise ProjectRegistryError(
                        "LSP server start timed out after 60s: %s" % " ".join(cmd))
                entry.client = client
                entry.status = client.state_name
                entry.refcount = 1
                entry.registration_ids.add(registration_id)
                self._registrations[registration_id] = project_id
                logger.info("Project %s registered: %s (%s) reg=%s",
                            project_id, real_path, language,
                            registration_id)
            except Exception as e:
                entry.status = "error"
                del self._projects[project_id]
                raise ProjectRegistryError("Failed to start LSP server: %s" % e) from e

            return project_id, registration_id

    async def deregister(self, registration_id):
        """Deregister by registration token. Decrements refcount;
        stops LSP server if it reaches 0. Each token can only be
        used once."""
        async with self._lock:
            project_id = self._registrations.pop(registration_id, None)
            if project_id is None:
                raise ProjectRegistryError(
                    "Unknown or already-used registration: %s"
                    % registration_id)

            entry = self._projects.get(project_id)
            if entry is None:
                # Project was force-removed; token is stale
                return

            entry.registration_ids.discard(registration_id)
            entry.refcount -= 1
            logger.info("Project %s refcount decremented to %d "
                        "(dereg=%s)", project_id, entry.refcount,
                        registration_id)

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
        self._registrations.clear()

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

    def find_project_for_file(self, file_path):
        """Find the project that owns a file path.

        Matches the file against all registered project paths using
        longest-prefix matching. When multiple projects share the
        same path (polyglot), uses the file extension to pick the
        right language backend.

        Returns a _ProjectEntry, or raises ProjectRegistryError.
        """
        from pathlib import Path as _Path

        real_file = os.path.realpath(file_path)
        best_path = None
        best_entries = []

        for entry in self._projects.values():
            project_path = entry.path
            if (real_file.startswith(project_path + os.sep)
                    or real_file == project_path):
                if best_path is None or len(project_path) > len(
                        best_path):
                    best_path = project_path
                    best_entries = [entry]
                elif len(project_path) == len(best_path):
                    best_entries.append(entry)

        if not best_entries:
            raise ProjectRegistryError(
                "No registered project for %s" % file_path)

        if len(best_entries) == 1:
            return best_entries[0]

        # Multiple backends under same path — disambiguate by extension
        ext = _Path(real_file).suffix.lower()
        from karellen_lsp_mcp.lsp_client import EXT_TO_LANGUAGE
        lang = EXT_TO_LANGUAGE.get(ext)
        if lang:
            canonical = canonicalize_language(lang)
            for entry in best_entries:
                if entry.language == canonical:
                    return entry

        # Fall back to first
        return best_entries[0]

    def find_projects_under_path(self, root_path):
        """Find all projects whose path is under root_path.

        Returns a list of _ProjectEntry (may be empty).
        """
        real_root = os.path.realpath(root_path)
        results = []
        for entry in self._projects.values():
            if (entry.path.startswith(real_root + os.sep)
                    or entry.path == real_root):
                results.append(entry)
        return results
