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

"""FastMCP server (thin MCP stdio frontend) that delegates all LSP operations to the daemon."""

import atexit
import asyncio
import functools
import logging
import os
import signal
import traceback

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from karellen_lsp_mcp.daemon_client import DaemonClient, DaemonClientError
from karellen_lsp_mcp.types import (
    LocationResult, Location, HoverResult, DocumentSymbolsResult, SymbolInfo,
    CallHierarchyResult, CallHierarchyItem, TypeHierarchyResult, TypeHierarchyItem,
    DiagnosticsResult, Diagnostic, ProjectInfo, RegisterResult, StringResult,
    IndexingStatusResult, IndexingTask, DetectedLanguageInfo, DetectResult,
)

logger = logging.getLogger(__name__)

mcp = FastMCP("karellen-lsp-mcp", instructions=(
    "LSP-backed code intelligence server. Use lsp_register_project to register a project "
    "with its language, then use query tools (lsp_read_definition, lsp_find_references, "
    "lsp_hover, lsp_document_symbols, lsp_call_hierarchy_incoming, lsp_call_hierarchy_outgoing, "
    "lsp_type_hierarchy_supertypes, lsp_type_hierarchy_subtypes, lsp_diagnostics) "
    "to introspect code. All line/character positions are 0-based (LSP convention)."
))

_client = None
_client_lock = asyncio.Lock()


async def _get_client():
    global _client
    async with _client_lock:
        if _client is None:
            _client = DaemonClient()
            await _client.connect()
        return _client


def _cleanup():
    global _client
    if _client is not None:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_client.close())
            else:
                loop.run_until_complete(_client.close())
        except Exception:
            pass
        _client = None


atexit.register(_cleanup)


def _handle_signal(signum, frame):
    _cleanup()
    os._exit(0)


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def _tag_errors(fn):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except DaemonClientError as e:
            raise ToolError("lsp: %s" % e) from e
        except ToolError:
            raise
        except Exception as e:
            tb = traceback.extract_tb(e.__traceback__)
            tb_lines = ["%s:%d in %s" % (f.filename, f.lineno, f.name) for f in tb[-3:]]
            raise ToolError("internal: %s: %s\n  %s" % (
                type(e).__name__, e, "\n  ".join(tb_lines))) from e
    return wrapper


async def _request(method, params=None):
    client = await _get_client()
    return await client.send_request(method, params)


def _to_location_result(data):
    locations = [Location(file=loc["file"], line=loc["line"], character=loc["character"])
                 for loc in data.get("locations", [])]
    return LocationResult(locations=locations, indexing=data.get("indexing", False))


def _to_hover_result(data):
    if "parts" in data:
        parts_text = []
        for p in data["parts"]:
            parts_text.append(p.get("content", ""))
        return HoverResult(content="\n\n".join(parts_text),
                           language=data["parts"][0].get("language") if data["parts"] else None)
    return HoverResult(content=data.get("content"), language=data.get("language"))


def _to_symbol_info(s):
    children = [_to_symbol_info(c) for c in s.get("children", [])]
    return SymbolInfo(name=s["name"], kind=s["kind"], line=s["line"],
                      detail=s.get("detail"), children=children)


def _to_document_symbols_result(data):
    symbols = [_to_symbol_info(s) for s in data.get("symbols", [])]
    return DocumentSymbolsResult(symbols=symbols)


def _to_call_hierarchy_result(data):
    items = [CallHierarchyItem(name=i["name"], kind=i["kind"], file=i["file"],
                               line=i["line"], call_sites=i.get("call_sites", 1))
             for i in data.get("items", [])]
    return CallHierarchyResult(direction=data["direction"], items=items,
                               indexing=data.get("indexing", False))


def _to_type_hierarchy_result(data):
    items = [TypeHierarchyItem(name=i["name"], kind=i["kind"], file=i["file"],
                               line=i["line"])
             for i in data.get("items", [])]
    return TypeHierarchyResult(direction=data["direction"], items=items,
                               indexing=data.get("indexing", False))


def _to_diagnostics_result(data):
    diagnostics = [Diagnostic(line=d["line"], character=d["character"],
                              severity=d["severity"], message=d["message"],
                              source=d.get("source"))
                   for d in data.get("diagnostics", [])]
    return DiagnosticsResult(diagnostics=diagnostics, indexing=data.get("indexing", False))


# --- Lifecycle Tools ---

@mcp.tool()
@_tag_errors
async def lsp_detect_project(project_path: str) -> DetectResult:
    """Detect languages and build systems in a project without registering.

    Scans the project directory for build system markers (build.gradle, pom.xml, etc.)
    and IDE metadata (.idea/, .classpath, .vscode/) to determine what languages and
    build systems are present. Uses a credibility hierarchy when multiple sources
    provide conflicting information (build config > IDE sync > IDE settings > filesystem).

    Args:
        project_path: Absolute path to the project root directory.
    """
    result = await _request("detect_project", {"project_path": project_path})
    languages = [DetectedLanguageInfo(
        language=lang["language"],
        build_system=lang.get("build_system"),
        confidence=lang.get("confidence", "high"),
        lsp_command=lang.get("lsp_command"),
        details=lang.get("details"),
        server_available=lang.get("server_available", True),
        install_hint=lang.get("install_hint"),
    ) for lang in result.get("languages", [])]
    return DetectResult(project_path=result["project_path"], languages=languages)


@mcp.tool()
@_tag_errors
async def lsp_register_project(project_path: str, language: str = None,
                               lsp_command: list[str] = None,
                               build_info: dict = None,
                               force: bool = False) -> RegisterResult:
    """Register a project for LSP analysis. Returns a project_id for subsequent queries.

    Multiple sessions sharing the same project_path + language get the same LSP server instance.

    Args:
        project_path: Absolute path to the project root directory.
        language: Language identifier (e.g. "c", "cpp", "java", "python", "rust").
                  If omitted, auto-detects from build system markers and IDE metadata.
        lsp_command: Custom LSP server command (e.g. ["clangd", "--background-index"]).
                     If omitted, uses the default for the language.
        build_info: Optional build configuration dict. For C/C++:
                    compile_commands_dir, build_dir, compiler_flags.
        force: If true, kill any existing LSP server for this project and start fresh.
    """
    result = await _request("register_project", {
        "project_path": project_path,
        "language": language,
        "lsp_command": lsp_command,
        "build_info": build_info,
        "force": force,
    })
    return RegisterResult(project_id=result["project_id"])


@mcp.tool()
@_tag_errors
async def lsp_deregister_project(project_id: str) -> StringResult:
    """Deregister a project. Decrements refcount; stops LSP server when it reaches 0.

    Args:
        project_id: The project_id returned by lsp_register_project.
    """
    await _request("deregister_project", {"project_id": project_id})
    return StringResult(result="Project %s deregistered." % project_id)


@mcp.tool()
@_tag_errors
async def lsp_list_projects() -> list[ProjectInfo]:
    """List all registered projects with their status and refcounts."""
    projects = await _request("list_projects")
    return [ProjectInfo(project_id=p["project_id"], path=p["path"],
                        language=p["language"], refcount=p["refcount"],
                        status=p["status"])
            for p in projects]


@mcp.tool()
@_tag_errors
async def lsp_indexing_status(project_id: str) -> IndexingStatusResult:
    """Query the LSP server's indexing progress for a project.

    Returns the current state (starting, indexing, ready, stopped), elapsed time,
    active indexing tasks with progress percentages, and count of completed tasks.
    Use this to check if the server is still indexing before making cross-file queries
    on large codebases. Does not wait for readiness — returns immediately.

    Args:
        project_id: Project identifier from lsp_register_project.
    """
    result = await _request("indexing_status", {"project_id": project_id})
    active = [IndexingTask(title=t["title"], message=t.get("message"),
                           percentage=t.get("percentage"))
              for t in result.get("active_tasks", [])]
    return IndexingStatusResult(
        state=result.get("state", "unknown"),
        elapsed_seconds=result.get("elapsed_seconds", 0.0),
        active_tasks=active,
        completed_tasks=result.get("completed_tasks", 0),
    )


# --- Query Tools ---

@mcp.tool()
@_tag_errors
async def lsp_read_definition(project_id: str, file_path: str,
                              line: int, character: int) -> LocationResult:
    """Go to definition of the symbol at the given position.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        line: 0-based line number.
        character: 0-based character offset.
    """
    result = await _request("lsp_read_definition", {
        "project_id": project_id, "file_path": file_path,
        "line": line, "character": character,
    })
    return _to_location_result(result)


@mcp.tool()
@_tag_errors
async def lsp_find_references(project_id: str, file_path: str,
                              line: int, character: int,
                              include_declaration: bool = True) -> LocationResult:
    """Find all references to the symbol at the given position.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        line: 0-based line number.
        character: 0-based character offset.
        include_declaration: Include the declaration in results (default True).
    """
    result = await _request("lsp_find_references", {
        "project_id": project_id, "file_path": file_path,
        "line": line, "character": character,
        "include_declaration": include_declaration,
    })
    return _to_location_result(result)


@mcp.tool()
@_tag_errors
async def lsp_hover(project_id: str, file_path: str,
                    line: int, character: int) -> HoverResult:
    """Get hover information (type, documentation) for the symbol at the given position.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        line: 0-based line number.
        character: 0-based character offset.
    """
    result = await _request("lsp_hover", {
        "project_id": project_id, "file_path": file_path,
        "line": line, "character": character,
    })
    return _to_hover_result(result)


@mcp.tool()
@_tag_errors
async def lsp_document_symbols(project_id: str, file_path: str) -> DocumentSymbolsResult:
    """List all symbols (functions, classes, variables, etc.) in a file.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
    """
    result = await _request("lsp_document_symbols", {
        "project_id": project_id, "file_path": file_path,
    })
    return _to_document_symbols_result(result)


@mcp.tool()
@_tag_errors
async def lsp_call_hierarchy_incoming(project_id: str, file_path: str,
                                      line: int, character: int) -> CallHierarchyResult:
    """Find all callers of the function/method at the given position.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        line: 0-based line number.
        character: 0-based character offset.
    """
    result = await _request("lsp_call_hierarchy_incoming", {
        "project_id": project_id, "file_path": file_path,
        "line": line, "character": character,
    })
    return _to_call_hierarchy_result(result)


@mcp.tool()
@_tag_errors
async def lsp_call_hierarchy_outgoing(project_id: str, file_path: str,
                                      line: int, character: int) -> CallHierarchyResult:
    """Find all functions/methods called by the function at the given position.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        line: 0-based line number.
        character: 0-based character offset.
    """
    result = await _request("lsp_call_hierarchy_outgoing", {
        "project_id": project_id, "file_path": file_path,
        "line": line, "character": character,
    })
    return _to_call_hierarchy_result(result)


@mcp.tool()
@_tag_errors
async def lsp_type_hierarchy_supertypes(project_id: str, file_path: str,
                                        line: int, character: int) -> TypeHierarchyResult:
    """Find supertypes (base classes/interfaces) of the type at the given position.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        line: 0-based line number.
        character: 0-based character offset.
    """
    result = await _request("lsp_type_hierarchy_supertypes", {
        "project_id": project_id, "file_path": file_path,
        "line": line, "character": character,
    })
    return _to_type_hierarchy_result(result)


@mcp.tool()
@_tag_errors
async def lsp_type_hierarchy_subtypes(project_id: str, file_path: str,
                                      line: int, character: int) -> TypeHierarchyResult:
    """Find subtypes (derived classes/implementations) of the type at the given position.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        line: 0-based line number.
        character: 0-based character offset.
    """
    result = await _request("lsp_type_hierarchy_subtypes", {
        "project_id": project_id, "file_path": file_path,
        "line": line, "character": character,
    })
    return _to_type_hierarchy_result(result)


@mcp.tool()
@_tag_errors
async def lsp_diagnostics(project_id: str, file_path: str) -> DiagnosticsResult:
    """Get compiler diagnostics (errors, warnings) for a file.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
    """
    result = await _request("lsp_diagnostics", {
        "project_id": project_id, "file_path": file_path,
    })
    return _to_diagnostics_result(result)


def _watch_parent():
    """Background thread: exit when parent process dies.

    anyio.run() overrides SIGTERM handling, so os.kill(SIGTERM) would be
    swallowed. Instead, call _cleanup() directly and use os._exit(0) to
    force a clean exit that bypasses the blocked event loop.
    """
    import threading
    import time

    ppid = os.getppid()

    def _monitor():
        while True:
            time.sleep(2)
            if os.getppid() != ppid:
                _cleanup()
                os._exit(0)

    t = threading.Thread(target=_monitor, daemon=True)
    t.start()


def main():
    _watch_parent()
    mcp.run(transport="stdio")
