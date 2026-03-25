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
src/unittest/python/       # Unit tests (mocked, no external deps)
src/integrationtest/python/ # Integration tests (need clangd)
```

Console script: `karellen-lsp-mcp` → `karellen_lsp_mcp.server:main`

## Key Design Details

- **Daemon IPC**: Custom length-prefixed binary protocol (4-byte big-endian + UTF-8 JSON)
  over Unix socket. Max message size: 10 MB. Request/response multiplexed via message IDs.
- **Two-phase readiness**: Single-file queries (definition, hover, symbols) wait only for
  LSP initialization. Cross-file queries (references, call/type hierarchy, diagnostics)
  wait for indexing with progress-driven dynamic timeout extension.
- **Refcounting**: Multiple sessions registering the same project (path + language) share
  one LSP server instance. Server stops when refcount reaches 0.
- **Normalizers**: Server-specific behavior abstracted via `LspNormalizer` subclasses.
  `ClangdNormalizer` tracks clangd progress and version-based feature support.
  `JdtlsNormalizer` tracks jdtls readiness via three conditions: ServiceReady seen,
  Searching progress seen, and no active progress tokens — preventing premature query
  dispatch during cold/warm index builds.
- **Staleness detection**: `compile_commands.json` is checked for freshness (build config
  mtime, dead source references) before use. Stale files trigger regeneration for CMake/Meson.
- **Signal handling**: SIGTERM/SIGINT handled in both frontend and daemon. Frontend monitors
  parent PID death via background thread (2s polling) to avoid orphan processes.

## Change Checklist

When making any change to detection, adapters, normalizers, or supported languages:

1. Update the relevant `docs/*.md` integration article
2. Update `README.md` supported languages table if languages changed
3. Update plugin manifests, skills, investigator configs, and hooks if applicable
4. Bump the version in `build.py` if the change affects the public API or tool behavior

## Code Style

Flake8 rules are configured in `build.py` — check there for line length, ignores, etc.
