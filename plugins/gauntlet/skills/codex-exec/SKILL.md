---
name: codex-exec
description: Delegate a task to Codex CLI via `codex exec`. Use for lightweight tasks (exploration, simple searches, file reads) that don't require heavy reasoning. Only available from Claude Code sessions.
---

Delegate a task to `codex exec` for non-interactive execution.

## When to use

- Lightweight exploration, file searches, simple code reads
- Tasks where reasoning effort is low
- Parallelizable subtasks that don't need Claude Code's full context

## When NOT to use

- Tasks requiring edits to files already being worked on in this session
- Tasks needing heavy reasoning or multi-step planning
- Tasks requiring interactive user input

## Execution

1. Construct prompt from user's request. Be specific — include file paths, package names, or search terms when known.
2. `mkdir -p .gauntlet/tmp` (git-ignored; add `.gauntlet/` to `.gitignore` if missing), then run:

```
codex exec --sandbox workspace-write -c "sandbox_workspace_write.network_access=true" -o .gauntlet/tmp/codex-output.txt "<prompt>"
```

Flags:
- `--sandbox workspace-write` (short `-s workspace-write`) → grant write access to the workspace. `codex exec` is already non-interactive, so no approval prompts. (Replaces the removed `--full-auto`.)
- `-c "sandbox_workspace_write.network_access=true"` → allow network access from within the workspace-write sandbox
- `-o .gauntlet/tmp/codex-output.txt` → capture final agent message
- `-C <dir>` → set working directory if different from current

3. Read `.gauntlet/tmp/codex-output.txt` for results.
4. Present results to user concisely.

## Rules

- NEVER pass destructive instructions (delete, force-push, reset) to codex exec.
- NEVER use `--dangerously-bypass-approvals-and-sandbox`.
- Always use `--sandbox workspace-write -c "sandbox_workspace_write.network_access=true"` for sandboxed execution.
- Store output in `.gauntlet/tmp/` — NEVER `/tmp/`. Never `rm -rf .gauntlet/`; only `.gauntlet/tmp/**` is disposable.
- If codex exec fails or times out, fall back to handling the task directly.
