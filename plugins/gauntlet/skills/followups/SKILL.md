---
name: followups
description: Show Gauntlet's durable follow-up queue through its existing read-only table command. Use when the user asks to list or inspect Gauntlet follow-ups, deferred work candidates, or hidden and rejected follow-up entries.
---

# Follow-ups

Invocation: Claude Code `/gauntlet:followups`; Codex `$gauntlet:followups`.

1. Resolve repository root to an absolute path from the user-supplied checkout or current repository.
2. Resolve `../campaign/scripts/followups.py` from this active `SKILL.md` to an absolute path. NEVER use a
   plugin-root environment variable.
3. Select `<repo>/.gauntlet/followups.jsonl`. Do not create it; the script handles a missing store.
4. Run this argument vector with absolute paths:

   ```text
   ["python3", "<absolute-followups.py>", "--file", "<absolute-followups.jsonl>", "table"]
   ```

5. Append `"--all"` only when user asks for hidden, rejected, closed, or all entries.
6. Print script stdout as-is. On failure, relay stderr as-is and stop.

Read-only. NEVER create, edit, parse, summarize, or reformat follow-up data or table output.
