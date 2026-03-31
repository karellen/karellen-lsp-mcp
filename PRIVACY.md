# Privacy Policy

**karellen-lsp-mcp** — MCP Server for LSP Code Intelligence

*Last updated: 2026-03-31*

## Summary

karellen-lsp-mcp does not collect, transmit, or store any personal data. It runs
entirely on your local machine.

## Data Collection

This software does **not**:

- Collect or transmit any personal information
- Send telemetry, analytics, or usage data
- Make any network connections (other than local communication between the MCP/LSP
  frontends, the daemon, and backend LSP servers, all on localhost via Unix domain
  sockets and stdio)
- Store any data beyond LSP server indexes and logs in the platform-standard data
  directory on your local filesystem

## Data Processing

All code intelligence operations (definitions, references, call hierarchies, type
hierarchies, hover, symbols, diagnostics) are performed locally using LSP servers
such as clangd and jdtls. The daemon and frontends act as local bridges between the
MCP/LSP client (e.g. Claude Code) and these servers. No data leaves your machine
through this software.

## Third-Party Services

This software does not integrate with any third-party services or APIs.

## Changes to This Policy

If this policy changes, the updated version will be published in the project
repository at
[https://github.com/karellen/karellen-lsp-mcp](https://github.com/karellen/karellen-lsp-mcp).

## Contact

If you have questions about this privacy policy, please open an issue at
[https://github.com/karellen/karellen-lsp-mcp/issues](https://github.com/karellen/karellen-lsp-mcp/issues)
or contact [supervisor@karellen.co](mailto:supervisor@karellen.co).
