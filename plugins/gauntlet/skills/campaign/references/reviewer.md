## The reviewer — selection, invocation, failure handling

The campaign needs an **adversarial reviewer** for two jobs: the Stage 0 sweep and each Stage 2a PR
review pass. The reviewer is a *role*, not a fixed tool. Claude Code is always the orchestrator +
implementer; the reviewer is chosen per run.

### Selecting the reviewer

Resolve once at run start (record the choice in the ledger header note / final report), in priority
order:

1. **Explicit invocation.** User named a reviewer for this run (e.g. "review with codex") → use it.
2. **User preference from memory.** A recorded preference (memory entry, `CLAUDE.md`, or a prior
   run's carryover) naming a preferred reviewer → use it. Do NOT invent a preference; use one only
   when it actually exists.
3. **Default — Claude subagents.** No preference → the reviewer is Claude's own subagents, run
   fresh and context-isolated. **No external tool is required for the campaign to run.**

**Reviewer diversity is a quality lever, not a requirement.** The gate's two passes are already
fresh, context-isolated re-rolls, but two Claude subagents share the orchestrator's model, so they
are less independent than a *different* engine would be. When available, a reviewer running a
**different agent/model than the orchestrator** (e.g. Codex CLI, `codex exec`) catches defects a
same-model re-roll can miss — recommend it to the user for a stronger gauntlet, and honor it when the
user has set it as their preference. It is never mandatory: the default Claude-subagent path is a
complete, valid reviewer.

### Running the default reviewer — Claude subagents

- **Stage 0 (adversarial sweep)** → run the sweep with your own subagents following the
  `gauntlet:review` skill over the scope (tier/shard it for a large surface, as Stage 0 describes),
  writing findings to that shard's `findings-raw-<shard>.md` in the standard finding shape. The
  streamed verification (Stage 0 step 2) is unchanged.
- **Stage 2a (per-PR review)** → spawn a **fresh** subagent to review the whole `<base>...HEAD` diff
  with an adversarial pass, using the same `review-<pr>-<n>.plan.jsonl` /
  `review-<pr>-<n>.progress.jsonl` protocol — planned units, then the unstructured adversarial sweep —
  and, on SATISFIED, the same `RESIDUAL-RISK: <area> — <why>` line immediately above exactly one
  final `VERDICT: SATISFIED` / `VERDICT: NOT SATISFIED` line. Each pass is a fresh, context-isolated
  subagent, so the two-SATISFIED-verdict gate holds: launch review 2 only after review 1 is
  SATISFIED, one at a time per PR.

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
default Claude subagents** (the two procedures above) rather than stalling, looping, or skipping the
gate — then note in the final report which passes ran on the subagent fallback. The gate is unchanged:
a subagent pass is a fresh, context-isolated re-roll that counts toward the two-SATISFIED-verdict gate
exactly like an external pass.
