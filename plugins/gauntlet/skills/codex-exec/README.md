# Codex Exec

Part of the [gauntlet](../../README.md) plugin.

It hands a self-contained task off to the **Codex CLI** (`codex exec`) to run non-interactively,
then reads the result back and reports it to you. Think of it as a way to offload lightweight
work — exploration, file reads, simple searches, a subtask that can run on its own — to a separate engine,
so the main session doesn't have to spend its own reasoning on it.

It's **only usable from within Claude Code sessions**, and it **requires the Codex CLI (`codex`) to be
installed** — the one genuinely optional external tool in this plugin. See the prerequisites in the
[root README](../../README.md).

## What it's good for

- Lightweight exploration, file searches, and simple code reads.
- Tasks where the reasoning effort is low.
- Parallelizable subtasks that don't need the full context of your main session.

## When not to use it

- Tasks that edit files you're actively changing in this session.
- Work that needs heavy reasoning or multi-step planning.
- Anything that needs interactive input while it runs.

## What to expect

It runs codex sandboxed — write access to the workspace, network allowed — and non-interactively, so there
are no approval prompts. It captures codex's final output to `.gauntlet/tmp/` (git-ignored, at the repo
root — never `/tmp`), reads it back, and presents the result to you concisely.

It stays inside the guardrails: it never passes destructive instructions (delete, force-push, reset) to
codex and never bypasses the sandbox. If codex fails or times out, it falls back to handling the task
directly, so a hiccup on the codex side doesn't leave you stuck.

Good to know: it always feeds codex EOF on stdin (`< /dev/null`), so a backgrounded `codex exec` can't hang
waiting for input that never comes.

Full mechanics live in [`SKILL.md`](./SKILL.md).
