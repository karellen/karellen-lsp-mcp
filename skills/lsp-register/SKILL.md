---
description: Register a project for LSP code intelligence. Auto-detects language and build system, or accepts explicit configuration. Handles C/C++ (clangd), Java/Kotlin (jdtls), and custom LSP servers.
---

# LSP Project Registration

Use this skill to register a project for LSP-backed code intelligence. Once registered,
all `lsp_*` query tools become available for that project.

## Prerequisites

- `karellen-lsp-mcp` must be installed and on PATH
- An LSP server for the target language:
  - **C/C++**: `clangd` (install via package manager)
  - **Java/Kotlin**: `jdtls` (install via `pip install karellen-jdtls-kotlin`)
  - **Other languages**: any LSP server, specified via `lsp_command`

## Workflow

### 1. Scan or Detect the Project (Optional)

Quick scan — count file extensions and see what languages are present:

```
lsp_scan_languages(project_path="/path/to/project")
```

Full detection — analyze build systems, IDE metadata, and configuration:

```
lsp_detect_project(project_path="/path/to/project")
```

Both return language recommendations without registering anything.
Use scan for a quick overview, detect for build system details and configuration.

### 2. Register the Project

#### Auto-Detection (Recommended)

Omit the `language` parameter to let the server auto-detect:

```
lsp_register_project(project_path="/path/to/project")
```

Auto-detection scans for build system markers (CMakeLists.txt, build.gradle, pom.xml,
etc.), IDE metadata (.idea/, .classpath, .vscode/), and source file conventions.

#### Explicit Language

Specify the language when auto-detection isn't sufficient:

```
lsp_register_project(project_path="/path/to/project", language="cpp")
```

#### C/C++ with compile_commands.json

For best results with clangd, provide the compile database location:

```
lsp_register_project(
    project_path="/path/to/project",
    language="cpp",
    build_info={"compile_commands_dir": "/path/to/build"}
)
```

If not provided, the adapter searches for `compile_commands.json` in common locations
and generates one for CMake/Meson projects automatically. Generated files go to
a platform-specific data directory (never pollutes the project tree).

**Generating compile_commands.json manually:**

- **CMake**: `cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -B build`
- **Meson**: `meson setup build` (generates automatically)
- **Bear (any build system)**: `bear -- make`

#### Java/Kotlin with jdtls

jdtls handles both Java and Kotlin via karellen-jdtls-kotlin:

```
lsp_register_project(project_path="/path/to/project", language="java")
```

For multi-module Gradle projects, register the root (where settings.gradle is):

```
lsp_register_project(project_path="/path/to/gradle-root", language="java")
```

#### Custom LSP Server

For languages without a built-in adapter:

```
lsp_register_project(
    project_path="/path/to/project",
    language="rust",
    lsp_command=["rust-analyzer"]
)
```

### 3. Wait for Indexing (Large Codebases)

Single-file queries (definition, hover, symbols) work immediately. Cross-file queries
(references, call hierarchy, diagnostics) wait for indexing automatically with
progress-driven timeout extension.

Check indexing progress on large codebases:

```
lsp_indexing_status(project_id="<id>")
```

### 4. Use LSP Query Tools

All queries use the `project_id` returned by registration:

- `lsp_read_definition` — go to definition
- `lsp_read_declaration` — go to declaration (header/interface)
- `lsp_read_type_definition` — go to the type definition of a variable
- `lsp_find_references` — find all references
- `lsp_find_implementations` — find all implementations of interface/abstract
- `lsp_hover` — type signature and documentation
- `lsp_document_symbols` — list symbols in a file
- `lsp_workspace_symbols` — search symbols across the project by name
- `lsp_call_tree_incoming` / `lsp_call_tree_outgoing` — recursive call trees (preferred)
- `lsp_call_hierarchy_incoming` / `lsp_call_hierarchy_outgoing` — single-level call chains
- `lsp_type_tree_supertypes` / `lsp_type_tree_subtypes` — recursive type trees (preferred)
- `lsp_type_hierarchy_supertypes` / `lsp_type_hierarchy_subtypes` — single-level type info
- `lsp_diagnostics` — compiler errors and warnings

### 5. Deregister When Done

```
lsp_deregister_project(project_id="<id>")
```

This decrements the refcount. The LSP server stops when all sessions deregister.

## Key Rules

- **Register once per session.** Multiple registrations of the same project share one
  LSP server instance (refcounted).
- **Use `force=True` to restart.** If the LSP server gets into a bad state, re-register
  with `force=True` to kill and restart it.
- **All positions are 0-based.** Line and character offsets follow LSP convention.
- **Cross-file queries include indexing status.** Check the `indexing` field in results;
  if `true`, results may be incomplete.
