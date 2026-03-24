---
description: Investigate code using LSP tools. Register a project, then use definitions, references, call hierarchies, type hierarchies, hover, and diagnostics to understand code structure, trace bugs, and assess change impact.
---

# LSP Code Investigation

Use this skill when you need to understand code structure, trace bugs through call
chains, find all usages of a symbol, or check compiler diagnostics across a codebase.

## Prerequisites

- `karellen-lsp-mcp` must be installed and on PATH
- An LSP server for the target language on PATH:
  - **C/C++**: `clangd`
  - **Java/Kotlin**: `jdtls` (from `karellen-jdtls-kotlin`)

## Workflow

### 1. Register the Project

```
lsp_register_project(project_path="/path/to/project")
```

Auto-detection identifies the language, build system, and LSP server configuration.
See `/karellen-lsp-mcp:lsp-register` for detailed registration options.

### 2. Understand a Symbol

Start with hover to get the type signature without reading the file:

```
lsp_hover(project_id="<id>", file_path="/path/to/file.cpp", line=42, character=10)
```

Then jump to the definition if you need more context:

```
lsp_read_definition(project_id="<id>", file_path="/path/to/file.cpp", line=42, character=10)
```

### 3. Trace Usage and Impact

Find all references to understand where and how a symbol is used:

```
lsp_find_references(project_id="<id>", file_path="/path/to/file.cpp", line=42, character=10)
```

Trace call chains — use the recursive tree tools to get the full hierarchy in one shot:

```
lsp_call_tree_incoming(project_id="<id>", file_path="/path/to/file.cpp", line=42, character=10)
lsp_call_tree_outgoing(project_id="<id>", file_path="/path/to/file.cpp", line=42, character=10)
```

Or use the single-level versions if you only need immediate callers/callees:

```
lsp_call_hierarchy_incoming(project_id="<id>", file_path="/path/to/file.cpp", line=42, character=10)
lsp_call_hierarchy_outgoing(project_id="<id>", file_path="/path/to/file.cpp", line=42, character=10)
```

### 4. Understand Type Hierarchies

Get the full recursive type tree in one shot:

```
lsp_type_tree_supertypes(project_id="<id>", file_path="/path/to/file.java", line=10, character=14)
lsp_type_tree_subtypes(project_id="<id>", file_path="/path/to/file.java", line=10, character=14)
```

Or single-level:

```
lsp_type_hierarchy_supertypes(project_id="<id>", file_path="/path/to/file.java", line=10, character=14)
lsp_type_hierarchy_subtypes(project_id="<id>", file_path="/path/to/file.java", line=10, character=14)
```

### 5. Get File Overview

List all symbols in a file to understand its structure:

```
lsp_document_symbols(project_id="<id>", file_path="/path/to/file.cpp")
```

### 6. Check Diagnostics

Get compiler errors and warnings for a specific file:

```
lsp_diagnostics(project_id="<id>", file_path="/path/to/file.cpp")
```

### 7. Clean Up

```
lsp_deregister_project(project_id="<id>")
```

## Investigation Strategies

### Understanding an Unfamiliar Function

1. `lsp_hover` to get the signature and docs
2. `lsp_read_definition` to see the implementation
3. `lsp_call_tree_incoming` to get the full caller tree
4. `lsp_call_tree_outgoing` to get the full callee tree

### Assessing Change Impact

1. `lsp_find_references` to find all usages of the symbol being changed
2. `lsp_call_tree_incoming` to get the full caller tree
3. For each caller, `lsp_hover` to understand how it uses the symbol
4. `lsp_diagnostics` on affected files after making changes

### Tracing a Bug

1. Start at the file where the bug manifests
2. `lsp_document_symbols` to find relevant functions
3. `lsp_read_definition` to follow suspicious calls
4. `lsp_call_tree_incoming` to trace the data flow backwards
5. `lsp_hover` on variables to check types

### Understanding a Class Hierarchy

1. `lsp_type_tree_supertypes` to get the full supertype tree
2. `lsp_type_tree_subtypes` to get the full subtype tree
3. `lsp_document_symbols` on key classes to compare their structure
4. `lsp_find_references` on interface methods to see polymorphic usage

## Key Rules

- **Hover before reading.** `lsp_hover` is fast and often provides enough context
  (type signature, docs) without needing to read the full file.
- **Use LSP instead of grepping.** `lsp_find_references` is semantically aware; it
  finds actual references, not string matches.
- **Check indexing status on large codebases.** Cross-file queries wait for indexing
  automatically, but `lsp_indexing_status` shows progress.
- **All positions are 0-based** (LSP convention).
- **Always deregister when done** to release resources.
