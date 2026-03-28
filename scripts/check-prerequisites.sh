#!/bin/bash
# Check that karellen-lsp-mcp is available.
# Runs once on SessionStart. Reports missing dependency via JSON output.
#
# Only checks for karellen-lsp-mcp itself. Language-specific LSP servers
# (clangd, jdtls) are checked at registration time, not at startup,
# since the user may not need all supported languages.

WARNINGS=""

check_command() {
  local cmd="$1"
  local install_hint="$2"
  local path
  path=$(command -v "$cmd" 2>/dev/null)
  if [ -z "$path" ]; then
    WARNINGS="${WARNINGS}- ${cmd} is not installed. ${install_hint}"$'\n'
  elif [ ! -x "$path" ]; then
    WARNINGS="${WARNINGS}- ${cmd} found at ${path} but is not executable. Run: chmod +x ${path}"$'\n'
  fi
}

check_command "karellen-lsp-mcp" "Run: pip install --user karellen-lsp-mcp[all]"

[ -z "$WARNINGS" ] && exit 0

jq -n --arg warnings "$WARNINGS" '{
  systemMessage: ("karellen-lsp-mcp plugin: missing prerequisites:\n" + $warnings),
  hookSpecificOutput: {
    hookEventName: "SessionStart",
    additionalContext: ("karellen-lsp-mcp plugin prerequisites are missing:\n" + $warnings + "The LSP code intelligence tools will not work until these are resolved.")
  }
}'
