## The reviewer — selection, invocation, failure handling

The campaign needs an **adversarial reviewer** for one job: each Stage 2a per-PR review pass. The
reviewer is a *role*, not a fixed tool. The active host is always the orchestrator + implementer; the
reviewer is chosen per run. Map host operations through `runtime-adapter.md`.

### Selecting the reviewer

Resolve once at run start and record the choice as the ledger `reviewer` header field (see
`files-and-ledger.md`) so every later wake — including a self-wake or a fresh agent that adopted the
run — re-reads it before launching any review pass and never silently reverts to the default; also
note it in the final report. Resolve in priority order:

1. **Explicit invocation.** User named a reviewer for this run (e.g. "review with codex") → use it.
2. **User preference from memory.** A recorded preference (memory entry, `AGENTS.md`, `CLAUDE.md`, or a prior
   run's carryover) naming a preferred reviewer → use it. Do NOT invent a preference; use one only
   when it actually exists.
3. **Default — native workers.** No preference → the reviewer is the active host's native workers, run
   fresh and context-isolated. **No external tool is required for the campaign to run.**

**Reviewer diversity is a user option, not a requirement.** The gate's two passes are already
fresh, context-isolated re-rolls, but two native workers share the orchestrator's model, so they
are less independent than a *different* engine would be. When available, a reviewer running a
**different agent/model than the orchestrator** catches defects a
same-model re-roll can miss. Use it only when the user selects it explicitly or has saved it as a
preference. Claude Code can use Codex CLI (`codex exec`) for that diversity;
Codex must not claim diversity from another Codex process. It is never mandatory: the default
native-worker path is a complete, valid reviewer.

**A user-selected external reviewer can also reduce native-worker cost.**
Review passes dominate campaign's native-worker spend: each one re-reads the **whole** `origin/<base>...HEAD`
diff, runs `required(tier)` times per SHA, and re-runs **from scratch** on every gate reset (a content
change voids the tally). A PR that takes several fix rounds can therefore spend many full-diff passes.
An external reviewer moves all of that off the native-worker pool. When describing this user option,
note that it can reduce native-worker token use more than changing one worker's model tier.

**A REVIEW PASS IS NEVER RUN ON A DOWNGRADED MODEL.** Whether the reviewer is a native worker or the
worker fallback for a failed external reviewer, the pass runs in the **`session` class** — it *is* the
gate, and a weaker verdict is simply a worse gate (`SKILL.md`, "Worker Dispatch"). The **one** deliberate
downgrade in this skill is the CI-fix subagent for a **formatting/lint** failure (`stage-2-ci.md`), which
runs a formatter and **verifies its diff** rather than authoring a fix — never a review pass. If the user
selects an external reviewer, it can reduce native-worker token use; it never changes the required model
class or review contract.

### Running the default reviewer — native workers

**THE DEFAULT REVIEWER RUNS THE SAME REVIEW PASS AS EVERY OTHER REVIEWER — the one `stage-2-review-gate.md`
defines, whole.** “Native workers” names **who executes it**, and nothing else. It does not name a
lighter contract, a shorter prompt, or an older protocol, and there is no such thing to name.

It is also a verdict renderer, so use `runtime-adapter.md`'s **native-worker** isolation contract, not the
stronger external-process contract. The worker MUST be a fresh conversational context, but the native API
may share the candidate cwd and writable filesystem and may inherit repository startup instructions.
Those facts are disclosed limitations, not automatic machine blockers and not an OS security boundary.
The worker treats candidate instruction/gate files as review evidence, while the orchestrator applies
the installed campaign rules as stage-0 authority and rejects any pass whose observed worktree mutation
or invalid artifacts show that the contract was not followed.

**The contract is NOT restated here, and it must not be.** It has one owner
(`stage-2-review-gate.md` — "The review gauntlet", "What the review is MEASURED AGAINST", "Findings are
RECORDS, not prose", "Does this pass COUNT?"), and the one time this section carried its own summary of it,
the summary went stale: it still described a plan/progress/verdict protocol with **no intent, no findings
artifact and no gating rule**, months after those became the contract. Following it recreated exactly the
open-ended review — *"is anything wrong with this code?"* — that the intent block exists to kill, **on the
DEFAULT path**, which is the one that runs whenever no external reviewer is configured. A stale summary is
worse than no summary: it is the version people actually read, and it is believed.

**Dispatch it by taking the review prompt from `stage-2-review-gate.md` and calling
`bind_review_prompt` for its two data bindings**: `<INTENT>` receives the intent block **verbatim**, while `<TRANSPORT-RECORD>` receives the
JSON encoding of `runtime-adapter.md`'s typed record for the active attempt. That one record carries the
worktree, base, emitter paths, attempt identity, progress/findings paths, and report ownership; no field
is interpolated into shell source. **The prompt IS the
contract**: whatever it
requires of a `codex exec` reviewer it requires of a native worker — the same question ("does this PR achieve its
stated Purpose…"), the same emit-only rule, the same anchored findings, the same `RESIDUAL-RISK` +
single-`VERDICT:` ending. Its verdict is read and its artifacts verified by the same `review-pass.py verify`
(Stage 2a, "Does this pass COUNT?"), so a pass dispatched without those inputs is not a lighter pass — it is
an `unusable` one.

Only the **transport** differs from the external-reviewer form: it is a **background native-worker task**
rather than a process. Set `report.producer` to `native-worker-write`, materialize the attempt-scoped
prompt at `transport.prompt_path` with `write_bytes`, and pass those exact bytes through
`dispatch_native`. The prompt explicitly
requires the worker to write the complete report to the record's `report.path` through the host file API
before returning the same text; the orchestrator does not persist the returned task message. This exact
producer rule applies to initial launch, relaunch, and native fallback. Run it in the **`session` class**
(above) and give each pass a **fresh, context-isolated** worker, so the gate holds: for a two-pass tier,
launch review 2 only after review 1 is SATISFIED, one at a time per PR (see Stage 2a-triage for the per-tier
pass count).

### Running an external reviewer (e.g. Codex CLI)

For the exact Claude Code → Codex and Codex → Claude Code transports, read
`cross-agent-reviewers.md`. The stage review contract remains the prompt authority.

When the selected reviewer is an external process like `codex exec`, invoke it with
`runtime-adapter.md`'s typed `run_argv` operation and the complete argv in the stage refs; set
`report.producer` to `external-process-capture`. NEVER pass destructive
instructions (delete, force-push, reset) to an external reviewer command, and NEVER use
`--dangerously-bypass-approvals-and-sandbox`.

**ALWAYS give `codex exec` prompt stdin an immediate EOF by setting `stdin_file` to the complete
attempt-scoped prompt artifact.** Never pass the prompt as a command argument and never inherit stdin:
the former puts untrusted bytes at the wrong boundary, while the latter can stay open forever.

An external reviewer can fail in a way that yields **no usable verdict**: quota/rate-limit
exhaustion, auth failures, timeouts, or other system errors. Distinguish this from a real review — a
run that returns an actual finding list or a `VERDICT: …` line is a *result*, act on it. A *failure*
is the absence of a verdict.

**On external-reviewer failure, retry once. If it still can't deliver a verdict, fall back to the
default native workers** (the per-PR procedure above) rather than stalling, looping, or skipping the
gate — then note in the final report which passes ran on the worker fallback. The gate is unchanged:
a worker pass is a fresh, context-isolated re-roll that counts toward the review gate exactly like an
external pass. The fallback uses the native-worker isolation contract in `runtime-adapter.md`; absence of
native cwd/mount/sandbox controls is disclosed, not mistaken for an external-process boundary.

A reviewer that **never starts** is a distinct failure — it produces not even a partial result — and
has its own guard: the Stage 2a **launch check** kills any pass that has written **no launch evidence**
within ~5 min of dispatch (launch evidence = any reviewer-written line after `pass_identity`, including
a `plan_amendment_request`, not just a `progress` event), re-dispatches it once into attempt-scoped
artifacts, and falls back to a fresh native worker if the relaunch is also dead on arrival. A missing or
wrong prompt-file stdin redirect is a common cause, so re-check the command before relaunching: an
identical relaunch hangs identically.
