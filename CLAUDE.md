# Project: karellen-lsp-mcp

MCP server bridging to LSP servers for structured code introspection.
See [README.md](README.md) for architecture, supported languages, configuration,
and tool documentation.

## Build & Test

PyBuilder project. Python >= 3.10.

```bash
pyb                          # default task: analyze + publish
pyb run_unit_tests           # unit tests only
pyb run_integration_tests    # integration tests (requires clangd on PATH)
```

Dependencies: `mcp`, `lsprotocol`, `filelock`, `platformdirs`

## Project Layout

```
src/main/python/karellen_lsp_mcp/
  server.py            # MCP stdio frontend (entry point: main())
  lsp_server.py        # LSP proxy frontend (entry point: main()) — polyglot LSP proxy
  daemon.py            # Persistent daemon, owns LSP servers and project registry
  daemon_client.py     # Async client connecting frontends to daemon via Unix socket
  lsp_client.py        # Async wrapper around a single LSP server subprocess (JSON-RPC 2.0)
  lsp_normalizer.py    # Pluggable adapter for LSP server quirks (clangd readiness, retries)
  lsp_adapter.py       # LSP adapters: bridge detection → LSP server config (per-language)
  detector.py          # Project autodetection: build systems, IDE metadata, source roots
  project_registry.py  # Refcounted registry: project_id → LspClient
  types.py             # Dataclass response types for MCP tools
.claude-plugin/
  plugin.json          # Plugin manifest (name, version, description)
.mcp.json              # MCP server configuration for the plugin
.lsp.json              # LSP server configuration: maps file extensions to languages for Claude Code native LSP
hooks/
  hooks.json           # SessionStart (prerequisites), PostToolUse (compiler error detection)
scripts/
  check-prerequisites.sh   # Checks karellen-lsp-mcp, clangd, jdtls availability
  detect-lsp-opportunity.sh # Detects compiler/build errors, suggests LSP tools
agents/
  lsp-investigator.md  # Autonomous agent for LSP-based code investigation
skills/
  lsp-register/SKILL.md    # Project registration skill
  lsp-investigate/SKILL.md # Code investigation skill
docs/
  c-cpp.md             # C/C++ integration: detection, clangd, compile_commands.json
  java-kotlin.md       # Java/Kotlin integration: detection, jdtls, Gradle/Maven
  python.md            # Python integration: detection, pyright, virtual environments
  rust.md              # Rust integration: detection, rust-analyzer, Cargo workspaces
src/unittest/python/       # Unit tests (mocked, no external deps)
src/integrationtest/python/ # Integration tests (need clangd)
```

Console scripts:
- `karellen-lsp-mcp` → `karellen_lsp_mcp.server:main` (MCP frontend)
- `karellen-lsp` → `karellen_lsp_mcp.lsp_server:main` (LSP proxy frontend)

## Key Design Details

- **Daemon IPC**: Custom length-prefixed binary protocol (4-byte big-endian + UTF-8 JSON)
  over Unix socket. Max message size: 10 MB. Request/response multiplexed via message IDs.
- **Two-phase readiness**: Single-file queries (definition, hover, symbols) wait only for
  LSP initialization. Cross-file queries (references, call/type hierarchy, diagnostics)
  wait for indexing with progress-driven dynamic timeout extension.
- **Refcounting**: Multiple sessions registering the same project (path + language) share
  one LSP server instance. Server stops when refcount reaches 0.
- **LSP proxy routing**: File-to-project routing lives in `ProjectRegistry.find_project_for_file()`
  (longest-prefix path matching with extension disambiguation for polyglot projects).
  Both MCP and LSP proxy frontends share the same daemon registry — MCP-registered projects
  are visible to native LSP queries and vice versa.
- **Normalizers**: Server-specific behavior abstracted via `LspNormalizer` subclasses.
  `ProgressNormalizer` is the base for servers using standard `$/progress` notifications
  (used directly for pyright, rust-analyzer). `ClangdNormalizer(ProgressNormalizer)` adds
  clangd-specific quirks (transient errors, version gating, position fallback).
  `JdtlsNormalizer` tracks jdtls readiness via three conditions: ServiceReady seen,
  Searching progress seen, and no active progress tokens — preventing premature query
  dispatch during cold/warm index builds. Normalizers also handle response URI normalization
  (`normalize_response`) and incoming param denormalization (`denormalize_params`) with
  per-instance reverse URI mapping for hierarchy item roundtripping.
- **Post-init configuration push**: After `initialized`, `LspClient` sends
  `workspace/didChangeConfiguration` if settings are present in `init_options`.
  Required by pyright which doesn't pull via `workspace/configuration`.
- **Staleness detection**: `compile_commands.json` is checked for freshness (build config
  mtime, >= 5% dead source references) before use. Stale files trigger regeneration for
  CMake/Meson. Use `lsp_regenerate_index` to force regeneration.
- **Per-tool timeout**: All tools accept an optional `timeout` parameter (seconds) that
  overrides the daemon's `LSP_MCP_READY_TIMEOUT` for that specific call. Passed in params
  to the daemon, read by `_handle_lsp_request` via `params.get("timeout")`.
- **Signal handling**: SIGTERM/SIGINT handled in both frontend and daemon. Frontend monitors
  parent PID death via background thread (2s polling) to avoid orphan processes.

## Change Checklist

When making any change to detection, adapters, normalizers, or supported languages:

1. Update the relevant `docs/*.md` integration article
2. Update `README.md` supported languages table if languages changed
3. Update `.lsp.json` extension mappings if file extensions changed
4. Update plugin manifests, skills, investigator configs, and hooks if applicable
5. Bump the version in `build.py` if the change affects the public API or tool behavior

## Debugging & Troubleshooting

- **LSP proxy logs**: `karellen-lsp` logs to stderr. When launched by Claude Code,
  use `claude --debug` to see LSP server output. For standalone testing:
  ```bash
  LSP_MCP_LOG_LEVEL=DEBUG karellen-lsp 2>lsp-debug.log
  ```
- **Daemon logs**: Written to `~/.local/state/karellen-lsp-mcp/daemon.log`.
  Set `LSP_MCP_LOG_LEVEL=DEBUG` before the daemon starts for verbose output.
  Kill the daemon first if it's already running (`pkill -f karellen_lsp_mcp.daemon`).
- **Log level**: `LSP_MCP_LOG_LEVEL` environment variable controls log verbosity
  for both the LSP proxy and the daemon. Values: `DEBUG`, `INFO` (default), `WARNING`, `ERROR`.
- **LSP server debug**: `LSP_MCP_SERVER_DEBUG=1` enables verbose/trace logging
  in LSP servers that support it (e.g., pyright `python.analysis.logLevel: trace`).
  Independent from `LSP_MCP_LOG_LEVEL`.

## Code Style

Flake8 rules are configured in `build.py` — check there for line length, ignores, etc.
