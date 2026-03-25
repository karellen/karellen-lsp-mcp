#!/bin/bash
# Detect compiler errors and build failures in Bash tool output and suggest LSP tools.
# Runs on PostToolUse and PostToolUseFailure for Bash commands.
# Reads the hook input JSON from stdin.

# Bail early if dependencies missing or karellen-lsp-mcp not installed
command -v jq &>/dev/null || exit 0
command -v karellen-lsp-mcp &>/dev/null || exit 0

INPUT=$(cat)

EVENT=$(echo "$INPUT" | jq -r '.hook_event_name // empty' 2>/dev/null)

# Get text to check from tool_response or error
TEXT=""
if [ "$EVENT" = "PostToolUse" ]; then
  TEXT=$(echo "$INPUT" | jq -r '.tool_response // empty' 2>/dev/null)
elif [ "$EVENT" = "PostToolUseFailure" ]; then
  TEXT=$(echo "$INPUT" | jq -r '.error // empty' 2>/dev/null)
fi

[ -n "$TEXT" ] || exit 0

# Check for compiler error patterns from various toolchains
MATCH=$(echo "$TEXT" | grep -oE \
  'error: |: error:|fatal error:|undefined reference to|cannot find symbol|package .+ does not exist|cannot resolve symbol|error\[E[0-9]+\]:|error CS[0-9]+:|TS[0-9]+:|compilation failed|BUILD FAILED|COMPILATION ERROR' \
  2>/dev/null | head -1)

[ -n "$MATCH" ] || exit 0

# Emit suggestion
jq -n --arg match "$MATCH" --arg event "$EVENT" '{
  hookSpecificOutput: {
    hookEventName: $event,
    additionalContext: ("Compiler/build error detected: " + $match + " Consider using LSP tools for code intelligence. Use the lsp-investigator agent or /karellen-lsp-mcp:lsp-investigate to register the project and navigate definitions, references, and diagnostics to understand the error.")
  }
}'
