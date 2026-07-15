# Runtime adapter — Claude Code and Codex

Campaign describes logical operations such as “dispatch a fresh worker” and “schedule a heartbeat.”
Map those operations to the active host here. Read this file before the first dispatch and before the
first wait or heartbeat. The gate rules do not change between hosts.

## Skill invocation

| Host | Start or resume syntax |
|---|---|
| Claude Code | `/gauntlet:campaign ...` |
| Codex | `$gauntlet:campaign ...` |

`<campaign-invocation>` elsewhere in this skill means the active row above. A self-resume always adds
exactly `--run <run-id> --token <agent-token>`; it never repeats `--new` or the original PR arguments.

Do not put either invocation in a shell command. These are host UI forms, not shell syntax.

## Bundled resources

Resolve `<skill-dir>` from the actual path of the active `SKILL.md`, then resolve `scripts/` and
`references/` relative to it. Never depend on `CLAUDE_PLUGIN_ROOT`, `CODEX_HOME`, the current working
directory, or a repository-relative path. Installed plugin caches differ between hosts and can differ
between installations of the same host.

## Fresh workers

A **worker** is a fresh, context-isolated execution created through the active host's native agent/task
mechanism. Claude Code may call this a subagent; Codex may expose an agent or task. Give it only the
contract, paths, and evidence its role needs. Never let a gate review inherit the campaign driver's
conversation.

- Use a background or otherwise asynchronous worker whenever the host supports one.
- Preserve each role's read/write limits and output artifact paths exactly.
- If the host cannot create a fresh worker, an explicitly configured external reviewer may fill a
  review role. It does not fill audit, mapper, reassessment, or fix roles.
- If neither a native fresh worker nor the required role's allowed fallback exists, park the PR as a
  machine blocker. Never run a context-isolation gate inline merely to keep moving.

## Model classes

Campaign chooses a **logical model class** on every worker dispatch:

| Logical class | Claude Code | Codex |
|---|---|---|
| `session` | Explicitly select the session model when the dispatch API permits it. | Explicitly select the session model when the dispatch API permits it. |
| `economy` | `sonnet`; `haiku` only for a trivially mechanical formatting failure. | Use a user- or repository-configured cheaper model when the dispatch API permits it; otherwise use `session`. |

Selecting the logical class is mandatory. When a host does not expose per-worker model selection, the
native worker's inherited session model is the implementation of `session`; record that limitation in
the final report. Never guess a model name from the other host. An unavailable `economy` mapping raises
cost but does not lower the gate.

## Background work and wakeups

Reviews, fixes, audits, reassessments, and CI watches remain asynchronous logical tasks. Fold a
completion into the same reconcile loop regardless of the host mechanism that reports it.

For the heartbeat fallback:

1. If the host exposes a wake scheduler, schedule `<campaign-invocation> --run <run-id> --token
   <agent-token>` at the delay selected by `loop-control.md`.
2. Otherwise keep the current campaign invocation alive and use the host's bounded wait/poll mechanism.
   Reconcile after each wait and at the same 5-minute or 15-minute deadline the heartbeat would protect.
   Do not return while non-terminal work remains merely because one background task is still running.

The second path is how Codex CLI sessions operate when no scheduled-wakeup capability is available. If
the process is killed, durable run state and the lease takeover rules allow a later invocation to resume;
the skill does not pretend a wake was scheduled when none was.

## Reviewer selection and diversity

The default reviewer is a fresh native worker on the active host. No external command is required.
Using the other agent is an opt-in user choice; never launch it solely because its CLI is installed.

- When Claude Code orchestrates, a user may select `codex exec` for model diversity.
- When Codex orchestrates, a user may select `claude -p` for model diversity.
- Another process from the active host provides context isolation but not engine diversity. Use one
  only when the user selected it or when isolation, rather than diversity, is the stated reason.
- An explicitly selected or saved user preference wins. Record the exact selection in the ledger and
  final report.

Exact other-agent commands live in `cross-agent-reviewers.md`. External-reviewer retry and fallback
rules remain those in `reviewer.md` and `stage-2-review-gate.md`. “Fallback to the default reviewer”
always means a fresh native worker on the active host.
