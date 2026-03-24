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

"""Structured result types for MCP tool responses."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Location:
    file: str
    line: int
    character: int


@dataclass
class LocationResult:
    locations: list[Location]
    indexing: bool = False


@dataclass
class HoverResult:
    content: Optional[str] = None
    language: Optional[str] = None


@dataclass
class SymbolInfo:
    name: str
    kind: str
    line: int
    detail: Optional[str] = None
    children: list["SymbolInfo"] = field(default_factory=list)


@dataclass
class DocumentSymbolsResult:
    symbols: list[SymbolInfo]


@dataclass
class CallHierarchyItem:
    name: str
    kind: str
    file: str
    line: int
    call_sites: int = 1


@dataclass
class CallHierarchyResult:
    direction: str
    items: list[CallHierarchyItem]
    indexing: bool = False


@dataclass
class TypeHierarchyItem:
    name: str
    kind: str
    file: str
    line: int


@dataclass
class TypeHierarchyResult:
    direction: str
    items: list[TypeHierarchyItem]
    indexing: bool = False


@dataclass
class CallTreeNode:
    name: str
    kind: str
    file: str
    line: int
    call_sites: int = 1
    children: list["CallTreeNode"] = field(default_factory=list)


@dataclass
class CallTreeResult:
    direction: str
    root: Optional["CallTreeNode"] = None
    indexing: bool = False


@dataclass
class TypeTreeNode:
    name: str
    kind: str
    file: str
    line: int
    children: list["TypeTreeNode"] = field(default_factory=list)


@dataclass
class TypeTreeResult:
    direction: str
    root: Optional["TypeTreeNode"] = None
    indexing: bool = False


@dataclass
class Diagnostic:
    line: int
    character: int
    severity: str
    message: str
    source: Optional[str] = None


@dataclass
class DiagnosticsResult:
    diagnostics: list[Diagnostic]
    indexing: bool = False


@dataclass
class ProjectInfo:
    project_id: str
    path: str
    language: str
    refcount: int
    status: str


@dataclass
class RegisterResult:
    project_id: str


@dataclass
class IndexingTask:
    title: str
    message: Optional[str] = None
    percentage: Optional[int] = None


@dataclass
class IndexingStatusResult:
    state: str
    elapsed_seconds: float = 0.0
    active_tasks: list[IndexingTask] = field(default_factory=list)
    completed_tasks: int = 0


@dataclass
class StringResult:
    result: str


@dataclass
class DetectedLanguageInfo:
    language: str
    build_system: Optional[str] = None
    confidence: str = "high"
    lsp_command: Optional[list[str]] = None
    details: Optional[dict] = None
    server_available: bool = True
    install_hint: Optional[str] = None


@dataclass
class DetectResult:
    project_path: str
    languages: list[DetectedLanguageInfo] = field(default_factory=list)
