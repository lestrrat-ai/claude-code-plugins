## The reviewer — selection, invocation, failure handling

The campaign needs an **adversarial reviewer** for one job: each Stage 2a per-PR review pass. The
reviewer is a *role*, not a fixed tool. Claude Code is always the orchestrator + implementer; the
reviewer is chosen per run.

### Selecting the reviewer

Resolve once at run start and record the choice as the ledger `reviewer` header field (see
`files-and-ledger.md`) so every later wake — including a self-wake or a fresh agent that adopted the
run — re-reads it before launching any review pass and never silently reverts to the default; also
note it in the final report. Resolve in priority order:

1. **Explicit invocation.** User named a reviewer for this run (e.g. "review with codex") → use it.
2. **User preference from memory.** A recorded preference (memory entry, `CLAUDE.md`, or a prior
   run's carryover) naming a preferred reviewer → use it. Do NOT invent a preference; use one only
   when it actually exists.
3. **Default — Claude subagents.** No preference → the reviewer is Claude's own subagents, run
   fresh and context-isolated. **No external tool is required for the campaign to run.**

The run's `branch_ownership` header follows this **same** explicit > preference > default selection
shape — explicit invocation/flag > stored user preference > safe default (`declined`) — resolved once
at run start and re-read every wake (see "PR adoption" and `files-and-ledger.md`).

**Reviewer diversity is a quality lever, not a requirement.** The gate's two passes are already
fresh, context-isolated re-rolls, but two Claude subagents share the orchestrator's model, so they
are less independent than a *different* engine would be. When available, a reviewer running a
**different agent/model than the orchestrator** (e.g. Codex CLI, `codex exec`) catches defects a
same-model re-roll can miss — recommend it to the user for a stronger gauntlet, and honor it when the
user has set it as their preference. It is never mandatory: the default Claude-subagent path is a
complete, valid reviewer.

### Running the default reviewer — Claude subagents

The reviewer's only job is the **Stage 2a per-PR review pass**: spawn a **fresh** subagent to review
the whole `<base>...HEAD` diff with an adversarial pass, using the same `review-<pr>-<n>.plan.jsonl` /
`review-<pr>-<n>.progress.jsonl` protocol — planned units, then the unstructured adversarial sweep —
and, on SATISFIED, the same `RESIDUAL-RISK: <area> — <why>` line immediately above exactly one final
`VERDICT: SATISFIED` / `VERDICT: NOT SATISFIED` line. Each pass is a fresh, context-isolated subagent,
so the review gate holds: for a two-pass tier, launch review 2 only after review 1 is SATISFIED, one
at a time per PR (see Stage 2a-triage for the per-tier pass count).

### Running an external reviewer (e.g. Codex CLI)

When the selected reviewer is an external command like `codex exec`, invoke it as the stage refs show
(`codex exec --sandbox workspace-write …`, output to the run's file). NEVER pass destructive
instructions (delete, force-push, reset) to an external reviewer command, and NEVER use
`--dangerously-bypass-approvals-and-sandbox`.

An external reviewer can fail in a way that yields **no usable verdict**: quota/rate-limit
exhaustion, auth failures, timeouts, or other system errors. Distinguish this from a real review — a
run that returns an actual finding list or a `VERDICT: …` line is a *result*, act on it. A *failure*
is the absence of a verdict.

**On external-reviewer failure, retry once. If it still can't deliver a verdict, fall back to the
default Claude subagents** (the per-PR procedure above) rather than stalling, looping, or skipping the
gate — then note in the final report which passes ran on the subagent fallback. The gate is unchanged:
a subagent pass is a fresh, context-isolated re-roll that counts toward the review gate exactly like an
external pass.
