---
name: campaign
description: >-
  Gates PRs to merge. A self-looping review-to-merge campaign: existing PRs are adopted into a run (pass PR numbers, or discover this run's labelled PRs), each PR is triaged to a review tier, and a per-PR review gauntlet (tier-dependent fresh, context-isolated SATISFIED verdicts on the whole PR diff, reviewed one at a time) plus event-driven CI monitoring gate an auto-merge. Multiple isolated runs (each keyed by a run-id, with a lease so only one agent drives each) can run concurrently in one repo. Drives its own loop through the active host's heartbeat or bounded-wait mechanism — invoke once. Campaign never writes fixes from scratch; to find issues first use gauntlet:review. Args (distinct modes): #PR... | --new #PR... | --run ID | no args
---

# Campaign

Self-looping, reactive PR-review-to-merge pipeline. The active host is orchestrator + gatekeeper:
reviews, CI watches, and fixes run as background tasks — and the heartbeat reconcile in a fresh
synchronous worker where the host provides one; gates and merges stay centralized. Campaign
gates **existing** PRs — adopted, never generated — and never writes a fix from scratch. To find issues
first, use `gauntlet:review`; after its report it can open one PR per confirmed fix and hand them here
(`/gauntlet:campaign #PRs` in Claude Code, `$gauntlet:campaign #PRs` in Codex).

Invoke once: the skill drives its own loop through the active host's heartbeat or bounded-wait
mechanism. In Claude Code, do not wrap it in `/loop`; in Codex, keep the invocation alive when no
heartbeat scheduler is available.

At every entry/resume, before any other work:

- Read `references/runtime-adapter.md`. Resolve the supplied checkout through its typed
  `RepositoryContext` owner exactly once per invocation/resume, and carry that record for every
  repository path and Git cwd. The adapter owns every host mapping — invocation forms, model classes
  (`session`/`economy`), the heartbeat mechanism — and the typed process/data boundary: dynamic values
  cross as argv, byte-file, or native-message data, and each review attempt's record assigns exactly
  one final-report producer.
- Read `references/run-identity-and-lease.md` and `references/files-and-ledger.md` before touching run
  state.

The **adversarial reviewer** is a selectable role: by default the cross-engine route (Claude Code
reviews with `codex exec`, Codex reviews with `claude -p`), launched at native-limitation level
whenever the paired CLI is present, falling back to a fresh native worker when it is absent or fails.
An explicit invocation or a TRUSTED saved preference (the orchestrator's own out-of-checkout user
memory / global user instructions — NEVER a file inside the candidate checkout, including
`.gauntlet/history/` carryover) overrides the default. `references/reviewer.md` owns selection and
fallback; `references/runtime-adapter.md` owns the isolation contract. Every route guarantees fresh
conversational context and launches on that alone; installed campaign rules remain the stage-0 gate
authority.

## Args

Claude Code: `/gauntlet:campaign #PR... | --new #PR... | --run <id> | (no args)`

Codex: `$gauntlet:campaign #PR... | --new #PR... | --run <id> | (no args)`

These are **distinct modes**, not freely-composable flags; `references/run-identity-and-lease.md`,
"Resolving a heartbeat", owns resolution.

- `#12` / `#12 #15` -> adopt those existing PRs into a run; gate + merge them.
- No argument -> discover this run's labelled PRs and continue. If none and nothing to do, show the
  idle prompt (`references/run-identity-and-lease.md`, "Resolving a heartbeat", owns its wording).
- Non-PR arg (e.g. `auth`) -> same prompt (the old area/topic sweep arg is REMOVED).
- `--run <id>` -> resume that run; takes no `#PR`/`--new`. Scheduled heartbeats carry internal
  `--token`.
- `--run <id> --token <tok> --watchdog` -> internal, session-scoped soundness-audit wake. It requires
  `--token`, rejects `#PR`/`--new`, and never adopts a run or recovers a dead session
  (`references/run-identity-and-lease.md`, "Resolving a heartbeat").
- `--new #PR...` -> force an independent new run-id for a new PR set (with carryover); requires PRs.

Invalid combinations (e.g. `--new` with no PRs, or `--run` with `#PR`) are rejected / fall through to
the idle prompt.

## Flow

The sequence of a campaign. Stage names match the reference filenames; each step states WHAT happens
and WHO owns the rules — read the owner before doing that step's work (see Load Discipline). A step
whose action IS a tool execution leads with the tool call; a `->` prefix is the trigger that gates
it — no trigger means the step runs unconditionally at that point in the sequence.

**Entry — run + lease** (`references/run-identity-and-lease.md`)

1. Resolve args to a run intent (`references/run-identity-and-lease.md`, "Resolving a heartbeat",
   owns resolution).
2. Fresh run -> `run-id.py new`: mint the run-id and ATOMICALLY create its run directory, then apply
   carryover from earlier runs (`references/carryover.md`).
3. `lease.py acquire` (`refresh` on resume): take or keep the run lease; stand down if a fresh lease
   names a different owner. One active driver per run — never double-drive. **Take a run in order**
   (`references/run-identity-and-lease.md`, "Take a run"): BEFORE arming, record the run intent
   (`ledger.py header set pending_adoption "<pr>…"`, cleared when adoption finishes) and — where the
   host supports it — ensure the session watchdog nudge
   (`references/runtime-adapter.md`, "Session watchdog nudge"). It audits a live session's campaign
   soundness; it does not recover a dead session.
4. Run start -> `ledger.py --file <state.jsonl> header set skill_version <version>`: record the
   `version` read from the **running plugin's** `plugin.json`. The harness loads this skill from the
   **installed plugin cache**, so a merged, version-bumped rule governs nothing until that cache
   refreshes; record what is actually running.
5. Run start -> set `reviewer` in the ledger header (`references/reviewer.md`) — once, never
   re-derived from memory. Header fields are DATA: re-read `reviewer` from the ledger every heartbeat,
   never trust memory. The base a PR merges into is **per-row** now (`effective_base` — the row's
   recorded base, else the header's legacy `base_branch` fallback), so **re-resolve each active PR's
   effective base every heartbeat**, never assume `main`. To rule a class out of scope for the WHOLE run
   in one place, set the run's default Non-goals once (`ledger.py header set default_non_goals '["<body>",
   …]'`, `references/files-and-ledger.md`); `pr-adopt.py intent-sync` folds them into every adopted PR's
   intent, so you never hand-edit each `intent-<pr>.md`.

**Adoption** (`references/pr-adoption.md`) — for each explicit `#PR` arg, and on every heartbeat for
every PR carrying this run's `gauntlet-run-<run-id>` label (from a batched snapshot):

6. Fetch the PR; REFUSE foreign-owned and cross-repo/fork PRs. REFUSE an existing terminal row before
   any refresh, label, worktree, or intent work. For new and existing non-terminal rows, register the
   ledger row (refresh in place on re-adoption, never duplicate), run label, status label, and worktree.
   THEN write (or preserve) the PR's **base** intent artifact (`intent-<pr>.md`: `## Purpose` /
   `## Non-goals` / `## Threat model`;
   local, git-ignored, never written back to the PR) and `pr-adopt.py intent-sync` to fold the run's
   default Non-goals into its managed block — the row must exist FIRST, because `intent-sync` REFUSES a PR
   with no ledger row (`pr-adoption.md`). Run `triage.py derive` on that resolved worktree for the
   mechanical floor + inventory, decide the SHA-pinned tier at or above that floor, and record it before
   gate work. Apply item 21, "CI watch action".
7. `review-pass.py intent-check --file <rundir>/intent-<pr>.md --ledger <rundir>/state.jsonl`: run
   immediately after writing an intent artifact and syncing it, before dispatching the PR's first review —
   the same parser every pass later loads, plus a check that the managed block is in sync with the run
   header's `default_non_goals`, so a malformed or stale intent fails before review work is spent.

**Heartbeat loop** (`references/loop-control.md` — read at each heartbeat before dispatch)

8. At heartbeat entry, once you own the run and load its ledger, run `nudge.py` and READ its advisory
   reminders — computed from durable state, decides nothing, always exits 0 (`references/loop-control.md`
   step 1). Then produce the run's validated PR snapshot through `reconcile.py fetch`, reconcile it
   through `reconcile.py detect` (treat `state.jsonl` as cache), and fold completed
   review / CI / fix tasks against the SHA each ran on. On a host with a fresh-worker mechanism, the
   Step 1 reconcile runs in ONE fresh reconcile worker per heartbeat and the driver folds, dispatches,
   and reschedules from its compact report (`references/runtime-adapter.md`, "Reconcile worker" — owns
   the contract and the inline fallback). When the run is **QUIET** (nudge, no meaningful
   ledger activity for its window) **OR** `ledger.py watchdog check` says the long-cadence deadline is
   `due`/`unset`/`invalid`, run the **health pass** (one pass, then one `ledger.py watchdog arm`)
   before rescheduling and lead the status with the diagnosis. A `--watchdog` wake runs the runtime
   adapter's advisory inspections (`references/runtime-adapter.md`, "Session watchdog nudge") and runs the
   health pass only when one of those normal triggers applies; it never decides the primary re-arm —
   `references/loop-control.md`, "Primary continuity", owns continuing the loop.
9. `ci-status.py required-set --ledger <rundir>/state.jsonl`: refresh the required set before any CI
   derivation this heartbeat.
10. Mutating action due on a PR -> `ledger.py … dispatch-check --pr <N>`: run before ANY action that
    mutates a PR — it exits non-zero for a HELD one.
11. **Due-work dispatch.** `triage.py derive`: re-derive each PR's tier from its `head_sha` — the tool emits the mechanical floor
    + inventory, the orchestrator decides the tier at or above that floor and never grants TRIVIAL to a
    diff with a non-prose file (`references/stage-2-review-gate.md`, "2a-triage"). Then launch ALL due
    mutating work up to caps — reviews, CI fixes, precondition clearing, base refresh — skipping HELD PRs,
    and stop in-flight reviews doomed by a content change. CI watches follow item 21's returned
    `watch_warranted` action, including for HELD PRs.
12. Before sleeping, audit: re-run the dispatch scan across both concurrency pools and confirm every
    due launch actually happened, every PR at a liveness cap was escalated rather than left spinning,
    and the loop continues per `references/loop-control.md`, "Primary continuity", whenever non-terminal
    work remains. NEVER sleep with due work un-launched or no path to the next reconcile. Then follow
    `references/loop-control.md`, "Reschedule or exit", exactly: a scheduled-heartbeat host renders the
    status — the `ledger.py table` output that block defines — and then schedules-or-replaces the primary
    wake as the turn's LAST action ("Primary continuity"; scheduling ends the turn on that host:
    `references/runtime-adapter.md`, "Scheduled-heartbeat host"); a scheduler-less host renders the same
    status, performs one bounded wait, and returns to the reconcile step while non-terminal work remains.

**Review gate — stage 2a** (`references/stage-2-review-gate.md`)

13. Clear preconditions first — open Copilot items, red CI, base conflicts. Never spend a review over
    them.
14. Reviews are fresh, context-isolated, SHA-pinned, and sequential per PR (launch review 2 only
    after review 1 is SATISFIED). The gate: `required(tier)` SATISFIED verdicts on current content —
    **1 if TRIVIAL, else 2** (any code/agent-doc/sensitive change is 2) — plus green CI.
    Before each launch, `review-pass.py plan-check --file <plan> --tier <tier>` must pass (a refusal
    blocks the launch; `references/stage-2-review-gate.md`, "Review work-plan ledger", owns the rule),
    then run `review-dispatch.py prepare` to write the attempt identity and exact prompt
    and return the one typed transport; `references/review-dispatch.md` owns the invocation and handoff.
15. The review is measured against the PR's INTENT, never "is anything wrong with this code?". The
    reviewer receives `intent-<pr>.md` verbatim — its base Purpose/Non-goals/Threat model PLUS the run's
    default Non-goals, folded into the managed block by `intent-sync` — and answers ONE question: does
    this PR achieve its stated Purpose, without breaking anything reachable by an actor named in its
    Threat model? Non-goals BIND, the run defaults among them. A pass with no usable intent earns no verdicts at all. Every finding is a record
    (`emit-finding.py`) that must ANCHOR — a `## Purpose` line quoted verbatim, or the writer who can
    actually supply the bad input; a finding that anchors to neither is NON-GATING (a follow-up, never
    a `NOT SATISFIED`, never a fix). The rule is an if-and-only-if: `NOT SATISFIED` exactly when at
    least one GATING finding stands.
16. Review verdict returned -> `ledger.py verdict`: the ONLY way to record it — NEVER hand-set
    `reviews_ok`. It bumps `review_rounds` (monotone, never reset — the loop's only memory across
    fresh-context heartbeats) and `ns_streak` atomically, and at a cap holds the PR `repairing` and
    exits non-zero. It also **refuses a verdict — satisfied or not — unless a fresh base-preflight `proceed`
    is on record for the head** (`base_ok_sha == head_sha`, written by `base-preflight.py … --file` on
    `proceed`), so a review can never be counted over a base no pre-flight cleared.
17. On `NOT SATISFIED`, findings are claims, not facts: a dispatched context-isolated audit subagent
    (never the orchestrator inline) verdicts each gating finding — CONFIRMED / ADJUSTED / REFUTED,
    with evidence — through `finding-audit.py`, BEFORE any fix is dispatched; **unsure -> CONFIRMED,
    never REFUTED**. Build post-audit work only from its verified `fix-list --json` output. A refutation NEVER clears
    the gate: it is committed into the tree as a falsifiable inline claim (never an instruction to the
    reviewer), once per finding; if a fresh reviewer re-raises it, that is a standoff -> park
    `awaiting-user`.
18. Any campaign commit is PR content: it resets the gate, re-triages the tier, and is re-reviewed on
    the new SHA.
19. At a review-loop cap -> `repair-pass.py bundle` / `decide`: STOP dispatching targeted fixes — a cap
    is a **mode switch, not a doorbell**. Build and dispatch the exact reassessment bundle, then execute
    its bundle-bound decision without asking the user (`references/repair-pass.md`). The tool derives and
    enforces the permitted decision set from PR ownership and remaining repair budget.

**CI — stage 2b** (`references/stage-2-ci.md`)

20. `ci-status.py derive`: how `ci` is DERIVED — always, the only way ("THE DERIVATION IS A COMMAND"
    owns the exact invocation): a SHA-pinned snapshot of BOTH check families, verified before parsing,
    decided against the selected row's `effective_required_set` that `--ledger` resolves ("THE REQUIRED
    SET IS NAMED, AND IT HAS NO DEFAULT" owns it). NEVER from `gh pr checks` (its output carries no SHA),
    NEVER by reading command output and judging it by eye. `ci-status.py liveness` then RECORDS that
    JSON — `ci`, the fingerprint, the strike/stall/refetch counters, and any cap park ("THE BOOKKEEPING
    IS A COMMAND" owns the invocation): the arithmetic is never applied by hand.
21. **CI watch action.** Run `liveness`, then ensure or relaunch a watch only when returned
    `watch_warranted` is `true` (`stage-2-ci.md`, "WATCH ONLY WHAT CAN MOVE"). Parked status does not
    override that result. Completions are heartbeats — the driver never blocks. The bounded
    CI waits and their caps are named in ONE place ("THE LIVENESS COUNTERS"); at any cap, escalate or
    park — never leave a PR spinning on a watch that will never wake anyone.
22. CI fixes: a formatting/lint failure goes to the `economy` tier (Worker Dispatch below); everything
    else, and every escalation from the cheap tier, goes to `session`.

**Parks — cross-cutting** (`references/loop-control.md`, "held-status guard")

23. A HELD PR is FROZEN — take no action that MUTATES it. Two kinds: **parked** (`awaiting-user` /
    `awaiting-api` — waits on a HUMAN) and **`repairing`** (waits on the reassessment pass, which is
    machine work due NOW). The test is "does this mutate the PR?", **not** "is it on a list";
    `HELD_STATUSES` in `scripts/ledger.py` is the one enumeration — never retype it. Sole exception:
    the CI watch — observing is not mutating, so parked status does not override item 21's returned
    `watch_warranted` action. Keep driving the other PRs. Unpark only on the user's answer, recorded
    DURABLY per park class; a ruling is durable and
    consumed exactly once (`references/stage-2-ci.md`, "THE RULING IS CONSUMED EXACTLY ONCE").

**Merge — stage 3** (`references/stage-3-merge.md`)

24. A merge candidate is not held, on a live SHA, with `reviews_ok >= required(tier)` and
    `ci == green`. Merge one at a time until no candidate remains immediately ready after base
    refresh. Campaign never passes `--delete-branch` — the repo's *Automatically delete head branches*
    setting governs the remote branch; local cleanup follows the per-PR `worktree_owned` /
    `branch_owned` flags. Execute each candidate through `merge.py run`; re-run that command after any
    partial failure instead of hand-running its later phases.

**Terminal** (`references/bailout-and-final-report.md`, `references/carryover.md`)

25. Work FOUND and deliberately not done — out of scope, pre-existing, a site a fixer left alone ->
    `followups.py add`: record it the moment it is noticed, never in driver prose, which dies with the
    driver's context; recording one never discharges a finding (`references/followups.md`).
26. Every PR merged or set aside -> run `carryover.py distill` to write the carryover ledger (it projects
    the terminal ledger, exactly once), then the final report. Otherwise refresh the lease and reschedule.

## Bundled Scripts

Bundled scripts live in `scripts/`, resolved relative to the actual path of this active `SKILL.md`
(not the current working directory or a host-specific root variable). Pass their absolute paths to
workers.

**HOW TO RUN ONE — always through the interpreter, with the absolute path:**

```
python3 <skill-dir>/scripts/<name>.py …      # every bundled Python script
bash    <skill-dir>/scripts/<name>.sh …      # every bundled shell script
```

**NEVER invoke a bundled script bare** — no `followups.py …`, no `./scripts/ledger.py`. A bare
invocation needs the file to be **executable** *and* its shebang to resolve, and needs that to survive
every checkout, archive, copy and install path between this repo and the machine running it — most of
these scripts are not even committed executable, so it fails outright with `Permission denied`. The
interpreter form needs **neither the executable bit nor the shebang** and behaves identically
everywhere. It is the only sanctioned form; a new script added here inherits it with no further note.

Prose and synopsis blocks throughout these docs write `ledger.py … set --pr <N>`, `followups.py …
table` and the like. **That is SHORTHAND naming the tool — it always means the full form above**,
never a literal command line to paste.

**The inventory below is COMPLETE by contract, and the contract is checked** — one row per script in
`scripts/` (sibling `*-test.py` suites and internals aside); a new script gets a row here in the same
change that adds it, and `script-table-test.py` fails CI on a missing, stale, or duplicate row. The
rules owner, not this table, defines each tool's behavior. A schema-owning accessor is the ONLY door to its
file: read and write **by field name** through it — never hand-parse, never hand-edit, never hand-write
a line the tool writes.

| Script | Job | Rules owner |
|---|---|---|
| `run-id.py` | Mint a run-id and ATOMICALLY create its run directory (`new`) | `references/run-identity-and-lease.md` |
| `lease.py` | Run-lease accessor: `mint` / `acquire` / `refresh` / `release` / `read` | `references/run-identity-and-lease.md` |
| `pr-adopt.py` | `plan` / `adopt` — mechanically adopt an existing first-party PR into a run: refuse fork/foreign/non-open, register the ledger row + ownership/status labels, discover-or-create the PR-head worktree | `references/pr-adoption.md` |
| `triage.py` | `derive` — classify one stable, SHA-pinned PR diff and emit the per-file inventory + reasons and a mechanical FLOOR tier (SENSITIVE→HIGH, any non-prose→STANDARD, all-prose→no floor; never TRIVIAL — the orchestrator decides the tier); optional `--tier` vetoes a below-floor tier | `references/stage-2-review-gate.md` |
| `heartbeat.py` | Emit the lean same-session wake prompts (scheduled heartbeat and session watchdog) the driver arms for its next wake | `references/runtime-adapter.md` |
| `ledger.py` | Schema-owning accessor for `state.jsonl` — plus `verdict`, the ONLY verdict recorder (tally, caps, `repairing` hold), and `dispatch-check`, the held-PR guard run before any mutating action | `references/files-and-ledger.md` |
| `review-pass.py` | Executable contract for a review pass's artifacts — plan (`plan-add`/`plan-waive`, with `plan-check` gating dispatch on the default dimensions), `pass_identity`, progress, findings, active-attempt report result, `intent-check`, and the `verify` that answers "does this pass COUNT?" | `references/stage-2-review-gate.md` |
| `review-dispatch.py` | `prepare` — validate one fresh review attempt, derive every artifact path, write `pass_identity` + exact bound prompt, and return the host-neutral typed transport record; never selects or launches a route | `references/review-dispatch.md` |
| `finding-audit.py` | Schema-owning accessor for complete gating-finding audits, mechanically derived review-fix scope, and durable standoff rulings | `references/finding-audit.md` |
| `emit-progress.py` | Reviewer's door: append one unit-progress event (the only sanctioned way) | `references/stage-2-review-gate.md` |
| `emit-finding.py` | Reviewer's door: record one FINDING (the only sanctioned way; findings must anchor or they do not gate) | `references/stage-2-review-gate.md` |
| `emit-amendment.py` | Reviewer's door: raise one plan amendment (the only sanctioned way; `ts` is tool-stamped, the proposed unit is validated like a plan unit) | `references/stage-2-review-gate.md` |
| `reviewer-liveness.py` | Probe whether a dispatched reviewer's output stream is still moving; decides nothing, always exits 0 | `references/stage-2-review-gate.md` |
| `base-preflight.py` | Decide proceed / rebase-first / recheck / park from live merge-state plus fetched base ancestry before review or fix; performs no rebase — with `--file`, `proceed` records `base_ok_sha` and `park` records the ledger-owned machine blocker | `references/stage-2-review-gate.md` |
| `format-preflight.py` | `check` — refuse to format any file whose formatter-write could escape the worktree (the file, or any path component, is a symlink); reads only, formats nothing | `references/stage-2-ci.md` |
| `worker-prompt.py` | `fix` — bind one complete review/CI fix prompt and logical model class, then atomically publish its exact bytes + metadata | `references/fix-subagent-contract.md` |
| `clean-rebase.py` | `run` — EXECUTE the clean base-only rebase (fetch/rebase/force-with-lease push + the ledger write that **carries `reviews_ok` and the status label forward** on a shape-preserving rebase, resetting only `ci = pending` and firing the head-move reset (`files-and-ledger.md`, the `head_sha` field, "What a genuine head move resets")) and REFUSE everything else: a conflict or a diff-changing rebase is aborted/reset and handed back (exit 3), never resolved | `references/stage-2-review-gate.md` |
| `ci-status.py` | `derive` — how `ci` is DERIVED, always, the only way — `liveness` — the recorder: writes `ci` + the liveness counters, parks at any cap — and `required-set` | `references/stage-2-ci.md` |
| `ci-snapshot.py` | Executable contract for the SHA-pinned CI snapshot artifact (used by `derive`) | `references/ci-derivation-spec.md` |
| `mutate-ci-snapshot.py` | Mutation harness proving `ci-snapshot.py`'s rules are fixture-pinned; run by validation/CI, not the driver | `references/ci-derivation-spec.md` |
| `merge-check.py` | `check` — decide merge-readiness (`merge` / `not-yet <reason>`) from the ledger row, live PR view, and fetched base ancestry | `references/stage-3-merge.md` |
| `merge.py` | `run` — resumably execute one merge-check-approved PR merge, base sync, owned local cleanup, and terminal ledger write | `references/stage-3-merge.md` |
| `label-mirror.py` | `mirror` — reconcile a PR's status label with its review gate (the canonical idempotent `gauntlet-accepted`/`gauntlet-reviewing` swap), computed from the ledger row; touches only the two status labels | `references/stage-2-review-gate.md` |
| `reconcile.py` | `fetch` — construct, validate, and atomically promote the canonical run-scoped PR snapshot; `detect` — compare it against the ledger and emit per-PR FACTS. Names no action; routing is skill policy | `references/files-and-ledger.md`, `references/loop-control.md` |
| `repair-pass.py` | Reassessment pass's door: `permitted` / `bundle` / `decide` — deterministic complete-history prompt, bundle hash binding, closed decision enum, ownership guardrail, repair cap | `references/repair-pass.md` |
| `followups.py` | Schema-owning accessor for the follow-up store (`.gauntlet/followups.jsonl`) — a durable work QUEUE, not an archive: entries are deleted once recorded elsewhere, kept when nothing else would remember | `references/followups.md` |
| `review-learnings.py` | Schema-owning accessor for the durable review-learnings store (`.gauntlet/review-learnings.jsonl`) — refuted/demoted finding CLASSES the DRIVER consults when authoring intent; ACCUMULATES and never auto-deletes, expires to `stale`, promotion beyond gauntlet-local is the user's | `references/review-learnings.md` |
| `carryover.py` | `distill` — project a run's TERMINAL ledger into `.gauntlet/history/<run-id>.md` on normal exit: merged/aborted/API-declined facts, exactly once (refuses a live run and refuses to overwrite) | `references/carryover.md` |
| `nudge.py` | Advisory reminder printer for heartbeat start; always exits 0 | its own module docstring |
| `transport-contract-test.py` | Standalone suite the plugin validator runs directly to pin the typed review/adoption boundary; owns no run state | `references/runtime-adapter.md` |
| `script-table-test.py` | Standalone suite CI runs directly to prove this table and `scripts/` agree; owns no run state | this section |

Each schema-owning accessor that carries a sibling suite keeps its fixtures in a **sibling
`*-test.py`** — the accessor's own filename with `-test` appended, in the same directory; its
`self-test` subcommand loads that file by path and **fails loudly if it is missing**. That is the rule,
and it is deliberately **not** an enumeration: a list of the suites that exist today is a restatement,
and it goes stale the next time one is added — by an author who never reads this line. Not every script
follows it, so do not assume a sibling for one you have not checked: `ci-snapshot.py` has a `self-test`
whose fixtures are in-file plus golden files under `scripts/fixtures/`, not a sibling module; and the
standalone suites above (`transport-contract-test.py`, `script-table-test.py`) are run directly, with
no accessor `self-test` subcommand.

## Worker Dispatch — logical model class

Campaign creates fresh workers for several jobs. **Select a logical model class on every dispatch.**
`references/runtime-adapter.md` maps `session` and `economy` to the active host. Never copy a model
name from one host into another.

| Worker | Model class | Why |
|---|---|---|
| **Reconcile worker** (heartbeat Step 1) | **`session`** | It routes snapshot facts and judges head moves and liveness for the whole run, and the driver dispatches from its report — a weaker model mis-routes a fact and the driver acts on a wrong picture (`references/runtime-adapter.md`, "Reconcile worker"). |
| Review pass (default reviewer) | **`session`** | It *is* the gate. A weaker verdict is a worse gate — the one thing never worth cheapening. |
| Fresh-worker fallback review | **`session`** | Same job as a review pass; counts toward the gate identically. |
| Review-fix (after `NOT SATISFIED`) | **`session`** | Authors code from scratch, judged only by another full review pass. A cheap bad fix burns a whole review pass and a gate reset — it *costs* more than the tier saves. |
| Root-cause **mapper** | **`session`** | Read-only, but NOT low-judgment: it enumerates a full matrix and confirms each gap with a repro. A weaker model **under-maps**, which is the exact failure the mapper exists to prevent (`root-cause-pass.md`). "Read-only" is not a licence to downgrade. |
| **Reassessment pass** (a PR at a review-loop cap) | **`session`** | It reads a PR's ENTIRE history at once and decides the **acceptance path**. It is gate machinery; a weaker model mis-diagnoses the loop and repairs the wrong thing (`repair-pass.md`). |
| **CI-fix — formatting/lint failure** | **`economy`** | **Downgraded ON PURPOSE when the host has a configured economy mapping.** It does not author a fix: it runs a deterministic formatter, **READS the resulting diff**, verifies it, and **escalates** anything it cannot verify (`references/stage-2-ci.md`). |
| **CI-fix — everything else**, and every **escalation** from the cheap tier | **`session`** | Authors code that gets merged. CI does **not** validate it: a wrong fix can turn CI green — by weakening a check, or by being plain wrong in product code no check covers. |
| **Finding-audit worker** (verdicts each gating finding) | **`session`** | Gate-adjacent: its CONFIRMED / ADJUSTED / REFUTED verdict decides **whether and what** gets fixed (`references/finding-audit.md` owns the disposition→fix rule). A weaker model mis-verdicts a finding and the wrong thing, or nothing, gets fixed. Never downgraded. |
| **Follow-up investigator** (Tier-1, read-only) | **`session`** | Read-only but NOT low-judgment, exactly like the mapper: it must **reproduce or refute** a claim, and a weaker model rubber-stamps instead of refuting (`references/followups.md`). "Read-only" is not a licence to downgrade. |
| **Follow-up fixer** (opens a new PR) | **`session`** | Authors code from scratch that the gauntlet then judges — the review-fix reasoning, in the separate follow-up workflow (`references/followups.md`, `references/fix-subagent-contract.md`). |

**Only the formatting CI-fix tier is downgraded** — for its narrower formatter-and-verification job.
**Every other worker in this table is `session` and is NEVER downgraded.** `worker-prompt.py fix` builds the prompt for
each of the three fix-worker roles (review-fix and both CI tiers); its template owns the complete shared
and role-specific wording. Read `references/fix-subagent-contract.md`, materialize the selected role, and
dispatch only the exact `prompt.txt` bytes with the logical model class from `metadata.json`. The
follow-up fixer that opens a new PR is a separate workflow, outside these roles (`fix-subagent-contract.md`).

## Load Discipline

Read references on demand. Do NOT load every reference up front.

Always read before touching run state:

- `references/run-identity-and-lease.md`
- `references/files-and-ledger.md`

Read `references/loop-control.md` at each heartbeat before dispatch.

Read stage refs only when that stage/action is due:

| Situation | Read |
|-----------|------|
| Public API change, run-owned operation scope | `references/scope-and-constraints.md` |
| Fresh run, carryover | `references/carryover.md` |
| Selecting the reviewer; external-reviewer failure/fallback | `references/reviewer.md` |
| Adopting PRs into a run (worktree/labels/ledger row) | `references/pr-adoption.md` |
| PR review gauntlet / progress ledger | `references/stage-2-review-gate.md` |
| Dispatching a review pass (transport record, prompt, launch) | `references/review-dispatch.md` |
| Auditing gating findings after a NOT SATISFIED (CONFIRMED/ADJUSTED/REFUTED) | `references/finding-audit.md` |
| Dispatching ANY fix subagent (CI-fix or review-fix) | `references/fix-subagent-contract.md` |
| Repeated sibling findings / shared root cause | `references/root-cause-pass.md` |
| A PR at a review-loop cap (`status = repairing`) / a verdict that exits non-zero | `references/repair-pass.md` |
| CI watch, check polling, CI fix | `references/stage-2-ci.md` |
| Reviewing or changing the CI derivation tools themselves (`ci-status.py`, `ci-snapshot.py`) — never needed to drive | `references/ci-derivation-spec.md` |
| Merge candidate / base refresh / cleanup | `references/stage-3-merge.md` |
| Recording / investigating / acting on a follow-up (work found but not done) | `references/followups.md` |
| Stuck task, abort, final report | `references/bailout-and-final-report.md` |
| Rule lookup / uncertainty | `references/critical-rules.md` |

## Critical Rules

`references/critical-rules.md` is the consolidated lookup — read it on any uncertainty.

- NEVER review unpublished local work.
- NEVER spend review over open Copilot items, red checks, or conflicts.
- NEVER pass destructive instructions to an external reviewer command (e.g. `codex exec`).
- NEVER use `--dangerously-bypass-approvals-and-sandbox`; use `--sandbox workspace-write`.
- NEVER force-push/reset/delete outside explicit stage procedure and run scope.
- NEVER touch another run's PR/branch/worktree.
- NEVER merge over red/pending CI or stale review verdicts.
- NEVER add a "Test plan" section to PR bodies.
- NEVER commit run/scratch files (the whole `.gauntlet/**` tree) — driver bookkeeping, not repo
  content. Stage only the specific source files a fix touches, by explicit path; never `git add -A` /
  `git add .`. Ensure `.gauntlet/` is git-ignored (add it if missing). Campaign has **no committed
  file of its own** — no repo-root config (`references/files-and-ledger.md`).
- NEVER `rm -rf .gauntlet/`; only `.gauntlet/tmp/**` is disposable — **everything else under
  `.gauntlet/` is durable**, and some of it nothing can rebuild (`references/files-and-ledger.md`).
- Public API changes require user confirmation unless the ledger's `api_changes` field is `allowed`
  (`references/scope-and-constraints.md`).
- **What the driver may do with a follow-up WITHOUT ASKING is the THREE-TIER AUTONOMY THRESHOLD**,
  owned by `references/followups.md` — investigate freely, ACT only on a corroborated claim that meets
  every one of its conditions, and **NEVER PUBLISH** without the USER's agreement on that specific
  item. Read the threshold before touching one; do not reconstruct it from here.
