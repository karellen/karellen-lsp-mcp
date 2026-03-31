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
    CallTreeResult, CallTreeNode, TypeTreeResult, TypeTreeNode,
    WorkspaceSymbolsResult, WorkspaceSymbolInfo,
    DiagnosticsResult, Diagnostic, ProjectInfo, RegisterResult, StringResult,
    IndexingStatusResult, IndexingTask, DetectedLanguageInfo, DetectResult,
    ScannedLanguageInfo, ScanResult,
)

logger = logging.getLogger(__name__)

mcp = FastMCP("karellen-lsp-mcp", instructions=(
    "LSP-backed code intelligence server. Use lsp_register_project to register a project "
    "with its language, then use query tools to introspect code. "
    "Navigation: lsp_read_definition, lsp_read_declaration, lsp_read_type_definition, "
    "lsp_find_implementations, lsp_find_references, lsp_hover. "
    "Symbols: lsp_document_symbols, lsp_workspace_symbols. "
    "Hierarchy: lsp_call_tree_incoming/outgoing, lsp_type_tree_supertypes/subtypes "
    "(recursive; prefer over single-level lsp_call_hierarchy_*/lsp_type_hierarchy_*). "
    "Diagnostics: lsp_diagnostics. "
    "Index management: lsp_regenerate_index to force-rebuild from scratch. "
    "All line/character positions are 1-based. "
    "All tools accept an optional timeout parameter (seconds) to override the default "
    "readiness timeout — use higher values for large codebases."
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
    return LocationResult(locations=locations, indexing=data.get("indexing", False),
                          elapsed_ms=data.get("elapsed_ms", 0))


def _to_hover_result(data):
    elapsed = data.get("elapsed_ms", 0)
    if "parts" in data:
        parts_text = []
        for p in data["parts"]:
            parts_text.append(p.get("content", ""))
        return HoverResult(content="\n\n".join(parts_text),
                           language=data["parts"][0].get("language") if data["parts"] else None,
                           elapsed_ms=elapsed)
    return HoverResult(content=data.get("content"), language=data.get("language"),
                       elapsed_ms=elapsed)


def _to_symbol_info(s):
    children = [_to_symbol_info(c) for c in s.get("children", [])]
    return SymbolInfo(name=s["name"], kind=s["kind"], line=s["line"],
                      detail=s.get("detail"), children=children)


def _to_document_symbols_result(data):
    symbols = [_to_symbol_info(s) for s in data.get("symbols", [])]
    return DocumentSymbolsResult(symbols=symbols,
                                 elapsed_ms=data.get("elapsed_ms", 0))


def _to_call_hierarchy_result(data):
    items = [CallHierarchyItem(name=i["name"], kind=i["kind"], file=i["file"],
                               line=i["line"], call_sites=i.get("call_sites", 1))
             for i in data.get("items", [])]
    return CallHierarchyResult(direction=data["direction"], items=items,
                               indexing=data.get("indexing", False),
                               elapsed_ms=data.get("elapsed_ms", 0))


def _to_type_hierarchy_result(data):
    items = [TypeHierarchyItem(name=i["name"], kind=i["kind"], file=i["file"],
                               line=i["line"])
             for i in data.get("items", [])]
    return TypeHierarchyResult(direction=data["direction"], items=items,
                               indexing=data.get("indexing", False),
                               elapsed_ms=data.get("elapsed_ms", 0))


def _to_call_tree_node(data):
    if data is None:
        return None
    children = [_to_call_tree_node(c) for c in data.get("children", [])]
    return CallTreeNode(name=data["name"], kind=data["kind"], file=data["file"],
                        line=data["line"], call_sites=data.get("call_sites", 1),
                        children=children,
                        has_more=data.get("has_more", False))


def _to_call_tree_result(data):
    root = _to_call_tree_node(data.get("root"))
    return CallTreeResult(direction=data["direction"], root=root,
                          indexing=data.get("indexing", False),
                          truncated=data.get("truncated", False),
                          elapsed_ms=data.get("elapsed_ms", 0))


def _to_type_tree_node(data):
    if data is None:
        return None
    children = [_to_type_tree_node(c) for c in data.get("children", [])]
    return TypeTreeNode(name=data["name"], kind=data["kind"], file=data["file"],
                        line=data["line"], children=children,
                        has_more=data.get("has_more", False))


def _to_type_tree_result(data):
    root = _to_type_tree_node(data.get("root"))
    return TypeTreeResult(direction=data["direction"], root=root,
                          indexing=data.get("indexing", False),
                          truncated=data.get("truncated", False),
                          elapsed_ms=data.get("elapsed_ms", 0))


def _to_workspace_symbols_result(data):
    symbols = [WorkspaceSymbolInfo(
        name=s["name"], kind=s["kind"], file=s["file"],
        line=s["line"], container=s.get("container"))
        for s in data.get("symbols", [])]
    return WorkspaceSymbolsResult(symbols=symbols,
                                  indexing=data.get("indexing", False),
                                  elapsed_ms=data.get("elapsed_ms", 0))


def _to_diagnostics_result(data):
    diagnostics = [Diagnostic(line=d["line"], character=d["character"],
                              severity=d["severity"], message=d["message"],
                              source=d.get("source"))
                   for d in data.get("diagnostics", [])]
    return DiagnosticsResult(diagnostics=diagnostics, indexing=data.get("indexing", False),
                             elapsed_ms=data.get("elapsed_ms", 0))


# --- Lifecycle Tools ---

@mcp.tool()
@_tag_errors
async def lsp_scan_languages(project_path: str,
                             timeout: int = 30) -> ScanResult:
    """Scan a project directory for source file types and recommend LSP registrations.

    A lightweight alternative to lsp_detect_project — simply counts file extensions
    and maps them to known languages. Use this for a quick overview of what languages
    are present before deciding which to register. Does not analyze build systems
    or IDE metadata.

    Args:
        project_path: Absolute path to the project root directory.
        timeout: Maximum seconds to wait for LSP server readiness (default 30).
    """
    result = await _request("scan_languages",
                            {"project_path": project_path,
                             "timeout": timeout})
    languages = [ScannedLanguageInfo(
        language=lang["language"],
        label=lang["label"],
        extensions=lang["extensions"],
        file_count=lang["file_count"],
        adapter_available=lang.get("adapter_available", False),
        server_available=lang.get("server_available", False),
        install_hint=lang.get("install_hint"),
    ) for lang in result.get("languages", [])]
    return ScanResult(project_path=result["project_path"],
                      languages=languages,
                      total_files=result.get("total_files", 0))


@mcp.tool()
@_tag_errors
async def lsp_detect_project(project_path: str,
                             timeout: int = 30) -> DetectResult:
    """Detect languages and build systems in a project without registering.

    Scans the project directory for build system markers (build.gradle, pom.xml, etc.)
    and IDE metadata (.idea/, .classpath, .vscode/) to determine what languages and
    build systems are present. Uses a credibility hierarchy when multiple sources
    provide conflicting information (build config > IDE sync > IDE settings > filesystem).

    Args:
        project_path: Absolute path to the project root directory.
        timeout: Maximum seconds to wait for LSP server readiness (default 30).
    """
    result = await _request("detect_project",
                            {"project_path": project_path,
                             "timeout": timeout})
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
                               force: bool = False,
                               regenerate: bool = False,
                               timeout: int = 120) -> RegisterResult:
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
        regenerate: If true, clean all managed data (compilation databases, workspace
                    caches) and force-restart the LSP server. Implies force=True.
        timeout: Maximum seconds to wait for LSP server readiness (default 120).
    """
    result = await _request("register_project", {
        "project_path": project_path,
        "language": language,
        "lsp_command": lsp_command,
        "build_info": build_info,
        "force": force,
        "regenerate": regenerate,
        "timeout": timeout,
    })
    return RegisterResult(project_id=result["project_id"])


@mcp.tool()
@_tag_errors
async def lsp_regenerate_index(project_id: str,
                               timeout: int = 120) -> RegisterResult:
    """Regenerate the project index from scratch.

    Cleans all managed data (compilation databases, workspace caches) and
    force-restarts the LSP server. Use this when the index is stale or
    corrupt, e.g. after major build configuration changes.

    The returned project_id may differ from the input if the project
    configuration changed.

    Args:
        project_id: Project identifier from lsp_register_project.
        timeout: Maximum seconds to wait for LSP server readiness (default 120).
    """
    result = await _request("regenerate_index",
                            {"project_id": project_id,
                             "timeout": timeout})
    return RegisterResult(project_id=result["project_id"])


@mcp.tool()
@_tag_errors
async def lsp_deregister_project(project_id: str,
                                 timeout: int = 30) -> StringResult:
    """Deregister a project. Decrements refcount; stops LSP server when it reaches 0.

    Args:
        project_id: The project_id returned by lsp_register_project.
        timeout: Maximum seconds to wait for LSP server readiness (default 30).
    """
    await _request("deregister_project",
                   {"project_id": project_id,
                    "timeout": timeout})
    return StringResult(result="Project %s deregistered." % project_id)


@mcp.tool()
@_tag_errors
async def lsp_list_projects(timeout: int = 30) -> list[ProjectInfo]:
    """List all registered projects with their status and refcounts.

    Args:
        timeout: Maximum seconds to wait for LSP server readiness (default 30).
    """
    projects = await _request("list_projects",
                              {"timeout": timeout})
    return [ProjectInfo(project_id=p["project_id"], path=p["path"],
                        language=p["language"], refcount=p["refcount"],
                        status=p["status"])
            for p in projects]


@mcp.tool()
@_tag_errors
async def lsp_indexing_status(project_id: str,
                              timeout: int = 30) -> IndexingStatusResult:
    """Query the LSP server's indexing progress for a project.

    Returns the current state (starting, indexing, ready, stopped), elapsed time,
    active indexing tasks with progress percentages, and count of completed tasks.
    Use this to check if the server is still indexing before making cross-file queries
    on large codebases. Does not wait for readiness — returns immediately.

    Args:
        project_id: Project identifier from lsp_register_project.
        timeout: Maximum seconds to wait for LSP server readiness (default 30).
    """
    result = await _request("indexing_status",
                            {"project_id": project_id,
                             "timeout": timeout})
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
                              line: int, character: int,
                              timeout: int = 120) -> LocationResult:
    """Go to definition of the symbol at the given position.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        line: 1-based line number.
        character: 1-based character offset.
        timeout: Maximum seconds to wait for LSP server readiness (default 120).
    """
    result = await _request("lsp_read_definition", {
        "project_id": project_id, "file_path": file_path,
        "line": line, "character": character,
        "timeout": timeout,
    })
    return _to_location_result(result)


@mcp.tool()
@_tag_errors
async def lsp_read_declaration(project_id: str, file_path: str,
                               line: int, character: int,
                               timeout: int = 120) -> LocationResult:
    """Go to declaration of the symbol at the given position.

    In C/C++, this navigates to the header declaration (vs definition in
    the source file). In Java, this navigates to the interface method
    declaration (vs the implementation).

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        line: 1-based line number.
        character: 1-based character offset.
        timeout: Maximum seconds to wait for LSP server readiness (default 120).
    """
    result = await _request("lsp_read_declaration", {
        "project_id": project_id, "file_path": file_path,
        "line": line, "character": character,
        "timeout": timeout,
    })
    return _to_location_result(result)


@mcp.tool()
@_tag_errors
async def lsp_find_implementations(project_id: str, file_path: str,
                                   line: int, character: int,
                                   timeout: int = 120) -> LocationResult:
    """Find all implementations of an interface or abstract method/class.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        line: 1-based line number.
        character: 1-based character offset.
        timeout: Maximum seconds to wait for LSP server readiness (default 120).
    """
    result = await _request("lsp_find_implementations", {
        "project_id": project_id, "file_path": file_path,
        "line": line, "character": character,
        "timeout": timeout,
    })
    return _to_location_result(result)


@mcp.tool()
@_tag_errors
async def lsp_read_type_definition(project_id: str, file_path: str,
                                   line: int, character: int,
                                   timeout: int = 120) -> LocationResult:
    """Go to the type definition of the symbol at the given position.

    Navigates from a variable or expression to the definition of its type.
    For example, from a variable of type Foo to the class Foo definition.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        line: 1-based line number.
        character: 1-based character offset.
        timeout: Maximum seconds to wait for LSP server readiness (default 120).
    """
    result = await _request("lsp_read_type_definition", {
        "project_id": project_id, "file_path": file_path,
        "line": line, "character": character,
        "timeout": timeout,
    })
    return _to_location_result(result)


@mcp.tool()
@_tag_errors
async def lsp_find_references(project_id: str, file_path: str,
                              line: int, character: int,
                              include_declaration: bool = True,
                              timeout: int = 120) -> LocationResult:
    """Find all references to the symbol at the given position.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        line: 1-based line number.
        character: 1-based character offset.
        include_declaration: Include the declaration in results (default True).
        timeout: Maximum seconds to wait for LSP server readiness (default 120).
    """
    result = await _request("lsp_find_references", {
        "project_id": project_id, "file_path": file_path,
        "line": line, "character": character,
        "include_declaration": include_declaration,
        "timeout": timeout,
    })
    return _to_location_result(result)


@mcp.tool()
@_tag_errors
async def lsp_hover(project_id: str, file_path: str,
                    line: int, character: int,
                    timeout: int = 120) -> HoverResult:
    """Get hover information (type, documentation) for the symbol at the given position.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        line: 1-based line number.
        character: 1-based character offset.
        timeout: Maximum seconds to wait for LSP server readiness (default 120).
    """
    result = await _request("lsp_hover", {
        "project_id": project_id, "file_path": file_path,
        "line": line, "character": character,
        "timeout": timeout,
    })
    return _to_hover_result(result)


@mcp.tool()
@_tag_errors
async def lsp_document_symbols(project_id: str, file_path: str,
                               timeout: int = 120) -> DocumentSymbolsResult:
    """List all symbols (functions, classes, variables, etc.) in a file.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        timeout: Maximum seconds to wait for LSP server readiness (default 120).
    """
    result = await _request("lsp_document_symbols", {
        "project_id": project_id, "file_path": file_path,
        "timeout": timeout,
    })
    return _to_document_symbols_result(result)


@mcp.tool()
@_tag_errors
async def lsp_call_hierarchy_incoming(project_id: str, file_path: str,
                                      line: int, character: int,
                                      timeout: int = 120) -> CallHierarchyResult:
    """Find all callers of the function/method at the given position.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        line: 1-based line number.
        character: 1-based character offset.
        timeout: Maximum seconds to wait for LSP server readiness (default 120).
    """
    result = await _request("lsp_call_hierarchy_incoming", {
        "project_id": project_id, "file_path": file_path,
        "line": line, "character": character,
        "timeout": timeout,
    })
    return _to_call_hierarchy_result(result)


@mcp.tool()
@_tag_errors
async def lsp_call_hierarchy_outgoing(project_id: str, file_path: str,
                                      line: int, character: int,
                                      timeout: int = 120) -> CallHierarchyResult:
    """Find all functions/methods called by the function at the given position.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        line: 1-based line number.
        character: 1-based character offset.
        timeout: Maximum seconds to wait for LSP server readiness (default 120).
    """
    result = await _request("lsp_call_hierarchy_outgoing", {
        "project_id": project_id, "file_path": file_path,
        "line": line, "character": character,
        "timeout": timeout,
    })
    return _to_call_hierarchy_result(result)


@mcp.tool()
@_tag_errors
async def lsp_type_hierarchy_supertypes(project_id: str, file_path: str,
                                        line: int, character: int,
                                        timeout: int = 120) -> TypeHierarchyResult:
    """Find supertypes (base classes/interfaces) of the type at the given position.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        line: 1-based line number.
        character: 1-based character offset.
        timeout: Maximum seconds to wait for LSP server readiness (default 120).
    """
    result = await _request("lsp_type_hierarchy_supertypes", {
        "project_id": project_id, "file_path": file_path,
        "line": line, "character": character,
        "timeout": timeout,
    })
    return _to_type_hierarchy_result(result)


@mcp.tool()
@_tag_errors
async def lsp_type_hierarchy_subtypes(project_id: str, file_path: str,
                                      line: int, character: int,
                                      timeout: int = 120) -> TypeHierarchyResult:
    """Find subtypes (derived classes/implementations) of the type at the given position.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        line: 1-based line number.
        character: 1-based character offset.
        timeout: Maximum seconds to wait for LSP server readiness (default 120).
    """
    result = await _request("lsp_type_hierarchy_subtypes", {
        "project_id": project_id, "file_path": file_path,
        "line": line, "character": character,
        "timeout": timeout,
    })
    return _to_type_hierarchy_result(result)


@mcp.tool()
@_tag_errors
async def lsp_call_tree_incoming(project_id: str, file_path: str,
                                 line: int, character: int,
                                 max_depth: int = 3,
                                 timeout: int = 120) -> CallTreeResult:
    """Recursively find all callers of the function/method, returning a full tree.

    Walks the incoming call hierarchy up to max_depth levels, with cycle detection.
    Returns a tree rooted at the target function, where each node's children are
    its callers.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        line: 1-based line number.
        character: 1-based character offset.
        max_depth: Maximum depth of returned tree (default 3). Nodes at the
                   boundary have has_more=true if deeper levels exist.
                   Increase to explore further.
        timeout: Maximum seconds to wait for LSP server readiness (default 120).
    """
    result = await _request("lsp_call_tree_incoming", {
        "project_id": project_id, "file_path": file_path,
        "line": line, "character": character,
        "max_depth": max_depth,
        "timeout": timeout,
    })
    return _to_call_tree_result(result)


@mcp.tool()
@_tag_errors
async def lsp_call_tree_outgoing(project_id: str, file_path: str,
                                 line: int, character: int,
                                 max_depth: int = 3,
                                 timeout: int = 120) -> CallTreeResult:
    """Recursively find all functions called by the function, returning a full tree.

    Walks the outgoing call hierarchy up to max_depth levels, with cycle detection.
    Returns a tree rooted at the target function, where each node's children are
    the functions it calls.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        line: 1-based line number.
        character: 1-based character offset.
        max_depth: Maximum depth of returned tree (default 3). Nodes at the
                   boundary have has_more=true if deeper levels exist.
                   Increase to explore further.
        timeout: Maximum seconds to wait for LSP server readiness (default 120).
    """
    result = await _request("lsp_call_tree_outgoing", {
        "project_id": project_id, "file_path": file_path,
        "line": line, "character": character,
        "max_depth": max_depth,
        "timeout": timeout,
    })
    return _to_call_tree_result(result)


@mcp.tool()
@_tag_errors
async def lsp_type_tree_supertypes(project_id: str, file_path: str,
                                   line: int, character: int,
                                   max_depth: int = 3,
                                   timeout: int = 120) -> TypeTreeResult:
    """Recursively find all supertypes (base classes/interfaces), returning a full tree.

    Walks the type hierarchy upward up to max_depth levels, with cycle detection.
    Returns a tree rooted at the target type, where each node's children are
    its supertypes.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        line: 1-based line number.
        character: 1-based character offset.
        max_depth: Maximum depth of returned tree (default 3). Nodes at the
                   boundary have has_more=true if deeper levels exist.
                   Increase to explore further.
        timeout: Maximum seconds to wait for LSP server readiness (default 120).
    """
    result = await _request("lsp_type_tree_supertypes", {
        "project_id": project_id, "file_path": file_path,
        "line": line, "character": character,
        "max_depth": max_depth,
        "timeout": timeout,
    })
    return _to_type_tree_result(result)


@mcp.tool()
@_tag_errors
async def lsp_type_tree_subtypes(project_id: str, file_path: str,
                                 line: int, character: int,
                                 max_depth: int = 3,
                                 timeout: int = 120) -> TypeTreeResult:
    """Recursively find all subtypes (derived classes/implementations), returning a full tree.

    Walks the type hierarchy downward up to max_depth levels, with cycle detection.
    Returns a tree rooted at the target type, where each node's children are
    its subtypes.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        line: 1-based line number.
        character: 1-based character offset.
        max_depth: Maximum depth of returned tree (default 3). Nodes at the
                   boundary have has_more=true if deeper levels exist.
                   Increase to explore further.
        timeout: Maximum seconds to wait for LSP server readiness (default 120).
    """
    result = await _request("lsp_type_tree_subtypes", {
        "project_id": project_id, "file_path": file_path,
        "line": line, "character": character,
        "max_depth": max_depth,
        "timeout": timeout,
    })
    return _to_type_tree_result(result)


@mcp.tool()
@_tag_errors
async def lsp_diagnostics(project_id: str, file_path: str,
                          timeout: int = 120) -> DiagnosticsResult:
    """Get compiler diagnostics (errors, warnings) for a file.

    Args:
        project_id: Project identifier from lsp_register_project.
        file_path: Absolute path to the source file.
        timeout: Maximum seconds to wait for LSP server readiness (default 120).
    """
    result = await _request("lsp_diagnostics", {
        "project_id": project_id, "file_path": file_path,
        "timeout": timeout,
    })
    return _to_diagnostics_result(result)


@mcp.tool()
@_tag_errors
async def lsp_workspace_symbols(project_id: str, query: str,
                                timeout: int = 120) -> WorkspaceSymbolsResult:
    """Search for symbols across the entire project by name or pattern.

    Returns matching symbols from all files in the project. Useful for finding
    types, functions, and classes by name without knowing which file they are in.

    Args:
        project_id: Project identifier from lsp_register_project.
        query: Symbol name or pattern to search for.
        timeout: Maximum seconds to wait for LSP server readiness (default 120).
    """
    result = await _request("lsp_workspace_symbols", {
        "project_id": project_id, "query": query,
        "timeout": timeout,
    })
    return _to_workspace_symbols_result(result)


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
