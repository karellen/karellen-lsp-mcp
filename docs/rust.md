# Rust Integration

## Default LSP Server

[rust-analyzer](https://rust-analyzer.github.io/) via `rust-analyzer`.

Install: `rustup component add rust-analyzer`.

**Runtime prerequisites**: rust-analyzer requires `rustc`, `cargo`, and the
`rust-src` component at runtime. Install via: `rustup component add rust-src`.

## Build System Detection

The `RustDetector` checks for:

| Marker | Build System | Confidence | Notes |
|--------|-------------|------------|-------|
| `Cargo.toml` | cargo | high | Primary marker; parsed for workspace and edition info |
| `build.rs` | (supplementary) | &mdash; | Build script; presence recorded but not statically parseable |

## Workspace Detection

Rust projects can be organized as Cargo workspaces with multiple crates.
The detector handles this:

1. If the project's `Cargo.toml` contains a `[workspace]` section, the
   project itself is the workspace root
2. Otherwise, the detector walks up the directory tree (up to 5 levels)
   looking for a parent `Cargo.toml` with `[workspace]`
3. Workspace `members` glob patterns are expanded to find member crate
   directories

The `RustAnalyzerAdapter` uses the workspace root as `root_uri` so
rust-analyzer sees all workspace members.

## Detection Details

| Field | Source | Description |
|-------|--------|-------------|
| `build_system` | &mdash; | Always `"cargo"` |
| `workspace_root` | directory walk | Path to workspace root (if different from project path) |
| `workspace_members` | Cargo.toml | Resolved paths of workspace member crates |
| `edition` | Cargo.toml | Rust edition (e.g., `"2021"`) |
| `has_build_rs` | filesystem | True if `build.rs` exists |
| `rust_toolchain` | rust-toolchain.toml / rust-toolchain | Toolchain channel (e.g., `"stable"`, `"nightly"`) |

## TOML Parsing

`Cargo.toml` parsing requires a TOML library. Python 3.11+ has `tomllib` in
the standard library. For Python 3.10, install `tomli` (`pip install tomli`)
for full detection capabilities. Without a TOML parser, detection still works
(file existence) but workspace members and edition are not extracted.

## Rust Toolchain

The detector checks for toolchain configuration in:
1. `rust-toolchain.toml` — TOML format, reads `[toolchain].channel`
2. `rust-toolchain` — plain text, first line is the channel name

This information is recorded in detection details but does not affect
rust-analyzer configuration (it discovers the toolchain independently).
