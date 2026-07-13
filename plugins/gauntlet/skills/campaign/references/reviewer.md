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

**Reviewer diversity is a quality lever, not a requirement.** The gate's two passes are already
fresh, context-isolated re-rolls, but two Claude subagents share the orchestrator's model, so they
are less independent than a *different* engine would be. When available, a reviewer running a
**different agent/model than the orchestrator** (e.g. Codex CLI, `codex exec`) catches defects a
same-model re-roll can miss — recommend it to the user for a stronger gauntlet, and honor it when the
user has set it as their preference. It is never mandatory: the default Claude-subagent path is a
complete, valid reviewer.

**An external reviewer is also the single biggest cost lever — the quality and cost arguments agree.**
Review passes dominate campaign's subagent spend: each one re-reads the **whole** `origin/<base>...HEAD`
diff, runs `required(tier)` times per SHA, and re-runs **from scratch** on every gate reset (a content
change voids the tally). A PR that takes several fix rounds can therefore spend many full-diff passes.
An external reviewer moves all of that off the subagent pool. When recommending Codex for diversity,
say so: it is the change that most reduces token spend, not the model tier of any individual subagent.

**A REVIEW PASS IS NEVER RUN ON A DOWNGRADED MODEL.** Whether the reviewer is a Claude subagent or the
subagent fallback for a failed external reviewer, the pass runs on the **session model** — it *is* the
gate, and a weaker verdict is simply a worse gate (`SKILL.md`, "Subagent Dispatch"). The **one** deliberate
downgrade in this skill is the CI-fix subagent for a **formatting/lint** failure (`stage-2-ci.md`), which
runs a formatter and **verifies its diff** rather than authoring a fix — never a review pass. Save tokens on
review by moving passes to an external reviewer and by scoping every fix subagent — never by cheapening a
verdict.

### Running the default reviewer — Claude subagents

The reviewer's only job is the **Stage 2a per-PR review pass**: spawn a **fresh** subagent to review
the whole `origin/<base>...HEAD` diff with an adversarial pass, using the same plan / progress protocol
— the per-pass `review-<pr>-<n>.plan.jsonl` and the **active launch attempt's** progress file
(`review-<pr>-<n>.progress.jsonl`, or `review-<pr>-<n>.a<k>.progress.jsonl` after a relaunch;
`stage-2-review-gate.md`) — planned units, then the unstructured adversarial sweep —
and, on SATISFIED, the same `RESIDUAL-RISK: <area> — <why>` line immediately above exactly one final
`VERDICT: SATISFIED` / `VERDICT: NOT SATISFIED` line. Each pass is a fresh, context-isolated subagent,
so the review gate holds: for a two-pass tier, launch review 2 only after review 1 is SATISFIED, one
at a time per PR (see Stage 2a-triage for the per-tier pass count).

### Running an external reviewer (e.g. Codex CLI)

When the selected reviewer is an external command like `codex exec`, invoke it as the stage refs show
(`codex exec --sandbox workspace-write … < /dev/null`, output to the run's file — the full command is
in `stage-2-review-gate.md`; build it from there, never from this abbreviation). NEVER pass destructive
instructions (delete, force-push, reset) to an external reviewer command, and NEVER use
`--dangerously-bypass-approvals-and-sandbox`.

**ALWAYS redirect stdin from `/dev/null` (`< /dev/null`) on every `codex exec` dispatch.** `codex exec`
reads stdin and appends it as a `<stdin>` block when a prompt is also passed as an argument; in a
background / non-interactive context stdin never reaches EOF, so codex **blocks forever waiting for
input** and the pass emits nothing at all. `< /dev/null` gives it an immediate EOF. (Omit it only when
deliberately piping the prompt in on stdin.)

An external reviewer can fail in a way that yields **no usable verdict**: quota/rate-limit
exhaustion, auth failures, timeouts, or other system errors. Distinguish this from a real review — a
run that returns an actual finding list or a `VERDICT: …` line is a *result*, act on it. A *failure*
is the absence of a verdict.

**On external-reviewer failure, retry once. If it still can't deliver a verdict, fall back to the
default Claude subagents** (the per-PR procedure above) rather than stalling, looping, or skipping the
gate — then note in the final report which passes ran on the subagent fallback. The gate is unchanged:
a subagent pass is a fresh, context-isolated re-roll that counts toward the review gate exactly like an
external pass.

A reviewer that **never starts** is a distinct failure — it produces not even a partial result — and
has its own guard: the Stage 2a **launch check** kills any pass that has written **no launch evidence**
within ~5 min of dispatch (launch evidence = any reviewer-written line after `pass_identity`, including
a `plan_amendment_request`, not just a `progress` event), re-dispatches it once into attempt-scoped
artifacts, and falls back to a fresh subagent if the relaunch is also dead on arrival. A dropped
`< /dev/null` is the most common cause, so re-check the command before relaunching: an identical
relaunch hangs identically.
