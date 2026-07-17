---
name: ledger
description: Show a Gauntlet campaign ledger through its existing read-only table command. Use when the user asks for campaign status, a campaign ledger, PR state for a named or current campaign run, or hidden and merged campaign entries.
---

# Ledger

Invocation: Claude Code `/gauntlet:ledger`; Codex `$gauntlet:ledger`.

1. Resolve repository root to an absolute path from the user-supplied checkout or current repository.
2. Resolve `../campaign/scripts/ledger.py` from this active `SKILL.md` to an absolute path. NEVER use a
   plugin-root environment variable.
3. Select state file:
   - User named a run, or current campaign context supplies one → use that run's
     `<repo>/.gauntlet/tmp/<run-id>/state.jsonl`.
   - No run is known → enumerate `<repo>/.gauntlet/tmp/*/state.jsonl` without reading the files.
   - Exactly one match → use it.
   - No matches → report that no campaign ledger exists.
   - Multiple matches → list run directory names and ask user to choose. NEVER guess by age or contents.
4. Run this argument vector with absolute paths:

   ```text
   ["python3", "<absolute-ledger.py>", "--file", "<absolute-state.jsonl>", "table"]
   ```

5. Append `"--all"` only when user asks for hidden, merged, closed, or all entries.
6. Print script stdout as-is. On failure, relay stderr as-is and stop.

Read-only. NEVER create, edit, parse, summarize, or reformat ledger data or table output.
