# MCP Server for LSP Code Intelligence (karellen-lsp-mcp)

[![Gitter](https://img.shields.io/gitter/room/karellen/lobby?logo=gitter)](https://gitter.im/karellen/Lobby)
[![Build Status](https://img.shields.io/github/actions/workflow/status/karellen/karellen-lsp-mcp/build.yml?branch=master)](https://github.com/karellen/karellen-lsp-mcp/actions/workflows/build.yml)
[![Coverage Status](https://img.shields.io/coveralls/github/karellen/karellen-lsp-mcp/master?logo=coveralls)](https://coveralls.io/r/karellen/karellen-lsp-mcp?branch=master)

[![karellen-lsp-mcp Version](https://img.shields.io/pypi/v/karellen-lsp-mcp?logo=pypi)](https://pypi.org/project/karellen-lsp-mcp/)
[![karellen-lsp-mcp Python Versions](https://img.shields.io/pypi/pyversions/karellen-lsp-mcp?logo=pypi)](https://pypi.org/project/karellen-lsp-mcp/)
[![karellen-lsp-mcp Downloads Per Day](https://img.shields.io/pypi/dd/karellen-lsp-mcp?logo=pypi)](https://pypi.org/project/karellen-lsp-mcp/)
[![karellen-lsp-mcp Downloads Per Week](https://img.shields.io/pypi/dw/karellen-lsp-mcp?logo=pypi)](https://pypi.org/project/karellen-lsp-mcp/)
[![karellen-lsp-mcp Downloads Per Month](https://img.shields.io/pypi/dm/karellen-lsp-mcp?logo=pypi)](https://pypi.org/project/karellen-lsp-mcp/)

## Overview

`karellen-lsp-mcp` is an [MCP](https://modelcontextprotocol.io/) (Model Context Protocol)
server that gives any MCP-compliant LLM client structured code intelligence via
[LSP](https://microsoft.github.io/language-server-protocol/) (Language Server Protocol)
servers. Instead of reading through entire codebases, the LLM can query definitions,
references, call hierarchies, type hierarchies, hover documentation, symbols, and
diagnostics — the same information a human developer gets from their IDE.

### Architecture

Multiple Claude sessions share a single daemon process and a single LSP server per project,
avoiding duplicate server instances and redundant indexing:

```
Claude Session 1          Claude Session 2
     │ (stdio)                  │ (stdio)
     ▼                          ▼
┌─────────────┐          ┌─────────────┐
│  MCP stdio  │          │  MCP stdio  │
│  frontend   │          │  frontend   │
└──────┬──────┘          └──────┬──────┘
       │ (Unix socket)          │ (Unix socket)
       ▼                        ▼
┌──────────────────────────────────────┐
│     karellen-lsp-mcp daemon          │
│                                      │
│  Project Registry (refcounted)       │
│    project-A  refcount=2  ──► clangd │
│    project-B  refcount=1  ──► clangd │
└──────────────────────────────────────┘
```

- **Daemon**: Persistent process, owns all LSP server subprocesses and the project
  registry. Listens on Unix domain socket. User-scoped. Auto-starts when the first
  MCP frontend connects, auto-exits after idle timeout with no connections.
- **MCP frontend**: Thin stdio process per Claude session. Connects to daemon, proxies
  MCP tool calls. Returns structured data (dataclasses) for fast LLM processing.

### Supported Languages

| Language | Default LSP Server | Autodetection | Details |
|----------|-------------------|---------------|---------|
| C / C++ | [clangd](https://clangd.llvm.org/) | CMake, Meson, autotools, Make, Bazel | [docs/c-cpp.md](docs/c-cpp.md) |
| Java / Kotlin | [jdtls](https://github.com/karellen/karellen-jdtls-kotlin) | Gradle, Maven, Ant | [docs/java-kotlin.md](docs/java-kotlin.md) |
| Any | Custom via `lsp_command` parameter | &mdash; | Provide the command in `lsp_register_project` |

Projects are autodetected when `language` is omitted from `lsp_register_project`, or
inspected explicitly via `lsp_detect_project`. Detection scans build system markers,
IDE metadata (JetBrains `.idea/`, Eclipse, VS Code), and source file conventions.

## Requirements

- **Python** >= 3.10
- **An LSP server** for your language, installed and on PATH
- **Linux** or **macOS** (uses Unix domain sockets; Windows 10 build 17063+ also works)

## Installation

```bash
pip install karellen-lsp-mcp
```

Or with pipx for an isolated environment:

```bash
pipx install karellen-lsp-mcp
```

## Claude Code Integration

### Plugin Installation (Recommended)

The plugin provides MCP tools, hooks (prerequisite checks, compiler error detection),
skills (`/karellen-lsp-mcp:lsp-register`, `/karellen-lsp-mcp:lsp-investigate`), and
an autonomous `lsp-investigator` agent.

**From Karellen marketplace:**

```bash
claude plugin marketplace add karellen/claude-plugins
claude plugin install karellen-lsp-mcp@karellen-plugins
```

**From local checkout:**

```bash
claude --plugin-dir /path/to/karellen-lsp-mcp
```

### Manual MCP Configuration (Alternative)

If you prefer not to use the plugin system, configure the MCP server directly:

```bash
claude mcp add --transport stdio karellen-lsp-mcp -- karellen-lsp-mcp
```

Or manually add to `~/.claude.json` (user scope) or `.mcp.json` in your project root
(project scope, shared via version control):

```json
{
  "mcpServers": {
    "karellen-lsp-mcp": {
      "type": "stdio",
      "command": "karellen-lsp-mcp"
    }
  }
}
```

If installed with pipx:

```bash
claude mcp add --transport stdio karellen-lsp-mcp -- pipx run karellen-lsp-mcp
```

or manually:

```json
{
  "mcpServers": {
    "karellen-lsp-mcp": {
      "type": "stdio",
      "command": "pipx",
      "args": ["run", "karellen-lsp-mcp"]
    }
  }
}
```

Note: manual MCP configuration provides tools only, without hooks, skills, or agents.

### Auto-approve LSP tools

By default Claude Code will prompt for confirmation before each `lsp_*` tool call. To
auto-approve all tools from this server, add a permission rule to your user settings
(`~/.claude/settings.json`):

```json
{
  "permissions": {
    "allow": [
      "mcp__karellen-lsp-mcp__*",
      "mcp__plugin_karellen-lsp-mcp_karellen-lsp-mcp__*"
    ]
  }
}
```

Or for a project-scoped setting, add the same rule to `.claude/settings.json` in your
project root (this file can be committed to version control so all team members get it).

### Teach Claude the LSP workflow

Claude will automatically discover all `lsp_*` tools, but to teach it **when and how** to
use them effectively, add the following to your project's `CLAUDE.md`:

````markdown
## LSP Code Intelligence

### When to Use LSP Tools

Use the `lsp_*` MCP tools for navigating and understanding codebases — especially when
you need to trace call chains, find all references to a symbol, understand type
hierarchies, or check compiler diagnostics without reading entire files.

### Setup

Register your project once at the start of a session. Do NOT ask the user for
configuration details. Do NOT narrate intermediate steps. Do the entire setup
(search, generate if needed, register) in one action sequence without pausing for
confirmation.

**C/C++ projects** use clangd as the default LSP server. clangd needs a
`compile_commands.json` for accurate results. Most projects don't have one pre-built.
Do all of this in one go:

1. Search for an existing `compile_commands.json` — use `Glob` to check the project
   tree. Also check if there's an existing build directory (look for `build/`,
   `cmake-build-*/`, `out/`, or a `compile_commands.json` symlink in the project root).
2. If not found, detect the build system and generate it:
   - `CMakeLists.txt` → check if there's already a build directory with cmake cache
     (in-tree or out-of-tree). If an existing build dir has `CMakeCache.txt`, re-run
     cmake there with `-DCMAKE_EXPORT_COMPILE_COMMANDS=ON`. Otherwise run
     `cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -B build`.
   - `meson.build` → `meson setup build` (generates it automatically)
   - `Makefile`/`configure`/autotools → use Bear if available (`bear -- make`), or
     run `make -n` to get compiler commands and write `compile_commands.json` yourself
   - No build system → register without `build_info` (clangd still works for basics)
3. Register immediately — don't ask for confirmation:

```
lsp_register_project(
    project_path="/path/to/project",
    language="cpp",  # or "c"
    build_info={"compile_commands_dir": "/path/to/dir/containing/compile_commands.json"}
)
```

**Other languages**: pass a custom `lsp_command`:

```
lsp_register_project(
    project_path="/path/to/project",
    language="rust",
    lsp_command=["rust-analyzer"]
)
```

### Key Rules

- **Use LSP instead of grepping**: `lsp_find_references` is semantically aware — it finds
  actual references, not string matches. It won't return comments, strings, or unrelated
  symbols with the same name
- **Hover before reading**: `lsp_hover` gives you the type signature and documentation
  for any symbol — often enough to understand usage without reading the full definition
- **Call hierarchy for impact analysis**: before changing a function, use
  `lsp_call_tree_incoming` to get the full recursive call tree in one shot, or
  `lsp_call_hierarchy_incoming` for a single level
- **Single-file queries work immediately**: `lsp_read_definition`, `lsp_hover`, and
  `lsp_document_symbols` don't wait for background indexing. Use these freely even on
  large codebases that are still indexing
- **Cross-file queries wait for indexing automatically**: `lsp_find_references`,
  call hierarchy, type hierarchy, and `lsp_diagnostics` wait for indexing to finish,
  with the timeout extending automatically as long as progress is being made. No need
  to poll or sleep. Use `lsp_indexing_status(project_id)` to check progress on large
  codebases
- **Regenerate compile_commands.json after adding C/C++ files**: it's a static snapshot
  of compiler commands. After adding `.c`/`.cpp` files, regenerate it (re-run cmake,
  bear, etc.) and clangd will pick up the changes automatically
- **Deregister when done**: `lsp_deregister_project` decrements the refcount; the LSP
  server shuts down when all sessions deregister
````

## Available Tools

### Project Lifecycle
| Tool | Description |
|------|-------------|
| `lsp_register_project` | Register a project for LSP analysis. Returns a project_id. Multiple sessions sharing the same project get the same LSP server. |
| `lsp_deregister_project` | Deregister a project. Decrements refcount; stops LSP server at 0. |
| `lsp_list_projects` | List all registered projects with status, refcounts, and paths. |
| `lsp_indexing_status` | Query indexing progress for a project: state, elapsed time, active tasks with percentages, completed task count. Returns immediately without waiting for readiness. |

### Code Navigation
| Tool | Description |
|------|-------------|
| `lsp_read_definition` | Go to definition of symbol at position. |
| `lsp_read_declaration` | Go to declaration (e.g., header in C/C++, interface in Java). |
| `lsp_read_type_definition` | Go to the type definition of a variable/expression. |
| `lsp_find_references` | Find all references to symbol at position. |
| `lsp_find_implementations` | Find all implementations of an interface/abstract method. |
| `lsp_hover` | Get type signature and documentation for symbol at position. |

### Symbols and Structure
| Tool | Description |
|------|-------------|
| `lsp_document_symbols` | List all symbols (functions, classes, variables, etc.) in a file. |
| `lsp_workspace_symbols` | Search for symbols across the entire project by name or pattern. |
| `lsp_call_hierarchy_incoming` | Find all callers of function/method at position (single level). |
| `lsp_call_hierarchy_outgoing` | Find all functions/methods called by function at position (single level). |
| `lsp_call_tree_incoming` | Recursively find all callers, returning a full tree (configurable depth, default 20). |
| `lsp_call_tree_outgoing` | Recursively find all callees, returning a full tree (configurable depth, default 20). |
| `lsp_type_hierarchy_supertypes` | Find base classes/interfaces of type at position (single level). |
| `lsp_type_hierarchy_subtypes` | Find derived classes/implementations of type at position (single level). |
| `lsp_type_tree_supertypes` | Recursively find all supertypes, returning a full tree (configurable depth, default 20). |
| `lsp_type_tree_subtypes` | Recursively find all subtypes, returning a full tree (configurable depth, default 20). |

### Diagnostics
| Tool | Description |
|------|-------------|
| `lsp_diagnostics` | Get compiler diagnostics (errors, warnings) for a file. |

## Structured Responses

All tools return structured data (dataclasses), not plain text. This enables fast LLM
processing without parsing. Examples:

**`lsp_read_definition`** returns `LocationResult`:
```json
{
  "locations": [
    {"file": "/path/to/impl.cpp", "line": 42, "character": 5}
  ]
}
```

**`lsp_document_symbols`** returns `DocumentSymbolsResult`:
```json
{
  "symbols": [
    {"name": "MyClass", "kind": "Class", "line": 10, "children": [
      {"name": "method1", "kind": "Method", "line": 12},
      {"name": "method2", "kind": "Method", "line": 18}
    ]}
  ]
}
```

**`lsp_call_hierarchy_incoming`** returns `CallHierarchyResult`:
```json
{
  "direction": "incoming",
  "items": [
    {"name": "main", "kind": "Function", "file": "/path/to/main.cpp", "line": 5, "call_sites": 1}
  ],
  "indexing": true
}
```

**`lsp_call_tree_incoming`** returns `CallTreeResult` (recursive):
```json
{
  "direction": "incoming",
  "root": {
    "name": "target_func", "kind": "Function", "file": "/path/to/file.cpp", "line": 42,
    "call_sites": 1,
    "children": [
      {"name": "caller_a", "kind": "Function", "file": "/path/to/a.cpp", "line": 10,
       "call_sites": 2, "children": [
        {"name": "main", "kind": "Function", "file": "/path/to/main.cpp", "line": 5,
         "call_sites": 1, "children": []}
      ]},
      {"name": "caller_b", "kind": "Function", "file": "/path/to/b.cpp", "line": 20,
       "call_sites": 1, "children": []}
    ]
  },
  "indexing": false
}
```

Cross-file queries include `"indexing": true` when the LSP server is still building its
index, signaling that results may be incomplete.

**`lsp_indexing_status`** returns `IndexingStatusResult`:
```json
{
  "state": "indexing",
  "elapsed_seconds": 45.2,
  "active_tasks": [
    {"title": "indexing", "message": "loading index shards", "percentage": 30}
  ],
  "completed_tasks": 2
}
```

## Configuration

### Timeouts

All timeouts are configurable via environment variables (in seconds). Set them in your
MCP server configuration:

```json
{
  "mcpServers": {
    "karellen-lsp-mcp": {
      "type": "stdio",
      "command": "karellen-lsp-mcp",
      "env": {
        "LSP_MCP_READY_TIMEOUT": "300",
        "LSP_MCP_REQUEST_TIMEOUT": "120"
      }
    }
  }
}
```

| Variable | Default | Description |
|----------|---------|-------------|
| `LSP_MCP_READY_TIMEOUT` | 120 | Base timeout (seconds) for cross-file queries to wait for indexing. Actual timeout extends dynamically based on indexing progress |
| `LSP_MCP_REQUEST_TIMEOUT` | 60 | Max seconds to wait for a single LSP JSON-RPC response |
| `LSP_MCP_CLIENT_TIMEOUT` | 180 | Max seconds for the MCP frontend to wait for a daemon response (must exceed ready + request timeouts) |
| `LSP_MCP_IDLE_TIMEOUT` | 300 | Seconds before the daemon auto-exits when idle (no connections, no projects) |

For large codebases (e.g. LLVM, Linux kernel), cross-file query timeouts extend
automatically as long as indexing is making progress. Single-file queries (definition,
hover, document symbols) only wait for the server to start, not for indexing. Ensure
`LSP_MCP_CLIENT_TIMEOUT` exceeds your expected maximum indexing time +
`LSP_MCP_REQUEST_TIMEOUT`.

## C/C++ Setup with clangd

### compile_commands.json

clangd needs a [compilation database](https://clang.llvm.org/docs/JSONCompilationDatabase.html)
to understand your project's build flags, include paths, and defines. Generate one with:

**CMake:**
```bash
cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -B build
```

**Bear (any build system):**
```bash
bear -- make
```

**Meson:**
```bash
meson setup build  # compile_commands.json is generated automatically
```

Then register with the directory containing `compile_commands.json`:

```python
lsp_register_project(
    project_path="/path/to/project",
    language="cpp",
    build_info={"compile_commands_dir": "/path/to/build"}
)
```

### Background indexing

For large projects, pass `--background-index` to clangd for cross-file features:

```python
lsp_register_project(
    project_path="/path/to/project",
    language="cpp",
    lsp_command=["clangd", "--background-index"],
    build_info={"compile_commands_dir": "/path/to/build"}
)
```

### Installing clangd

**Fedora / RHEL / CentOS:**
```bash
sudo dnf install clang-tools-extra
```

**Ubuntu / Debian:**
```bash
sudo apt install clangd
```

**Arch Linux:**
```bash
sudo pacman -S clang
```

**macOS:**
```bash
brew install llvm
```

## Troubleshooting

### Daemon files

The daemon stores its files in platform-standard directories (via
[platformdirs](https://pypi.org/project/platformdirs/)):

| Directory | Linux | macOS | Windows | Contents |
|-----------|-------|-------|---------|----------|
| Runtime | `~/.local/share/karellen-lsp-mcp/` | `~/Library/Caches/karellen-lsp-mcp/` | `%LOCALAPPDATA%/karellen-lsp-mcp/` | `daemon.sock`, `daemon.lock` |
| Logs | `~/.local/share/karellen-lsp-mcp/log/` | `~/Library/Logs/karellen-lsp-mcp/` | `%LOCALAPPDATA%/karellen-lsp-mcp/Logs/` | `daemon.log` |
| Data | `~/.local/share/karellen-lsp-mcp/` | `~/Library/Application Support/karellen-lsp-mcp/` | `%LOCALAPPDATA%/karellen-lsp-mcp/` | compile_commands copies, jdtls workspaces |

If the daemon gets into a bad state, remove the socket file and it will be restarted
automatically on the next MCP tool call. Check `daemon.log` to diagnose crashes or
unexpected behavior.

### LSP server not starting

If tools return errors about the LSP server, check:

1. The LSP server binary is on PATH (e.g. `which clangd`)
2. The project path is an absolute path
3. For C/C++, `compile_commands.json` exists in the specified `compile_commands_dir`

### Incomplete results during indexing

Cross-file queries (references, call hierarchy, type hierarchy, diagnostics) automatically
wait for indexing to finish, with the timeout extending dynamically based on progress.
If a query completes while indexing is still in progress (e.g. the server became "ready"
before full indexing finished), the response includes an `indexing: true` flag — results
may be incomplete. Use `lsp_indexing_status` to check progress, or re-query later.
Single-file queries (definition, hover, document symbols) always work immediately.

### Stale daemon

If you update `karellen-lsp-mcp` to a new version, the running daemon may still be the
old version. Kill the daemon to force a restart:

```bash
pkill -f "karellen_lsp_mcp.daemon"
```

The next MCP tool call will auto-start the new version.

## License

Apache-2.0
