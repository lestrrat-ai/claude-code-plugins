---
name: campaign
description: >-
  Gates PRs to merge. A self-looping review-to-merge campaign: existing PRs are adopted into a run (pass PR numbers, or discover this run's labelled PRs), each PR is triaged to a review tier, and a per-PR review gauntlet (tier-dependent fresh, context-isolated SATISFIED verdicts on the whole PR diff, reviewed one at a time) plus event-driven CI monitoring gate an auto-merge. Multiple isolated runs (each keyed by a run-id, with a lease so only one agent drives each) can run concurrently in one repo. Drives its own loop via ScheduleWakeup — invoke once, no /loop wrapper. Campaign never writes fixes from scratch; to find issues first use gauntlet:review. Args (distinct modes): #PR... | --new #PR... | --run <id> | no args
---

# Campaign

Self-looping, reactive PR-review-to-merge pipeline.

Claude Code is orchestrator + gatekeeper. The **adversarial reviewer** is a selectable role: by
default Claude's own subagents (no external tool required); use the user's preferred reviewer when one
is set (explicit invocation, or a preference in memory/`CLAUDE.md`/carryover). A reviewer running a
different agent/model than the orchestrator (e.g. Codex CLI) is recommended for stronger reviewer
diversity but never required — see `references/reviewer.md`. Reviews and CI watches run as background
tasks; gates and merges stay centralized. Campaign gates **existing** PRs; it never writes fixes from
scratch — to find issues first, use `gauntlet:review`, which after its report offers to open one PR per
confirmed fix and hand them straight here (`/gauntlet:campaign #PRs`). That opt-in handoff is a common
way PRs enter a campaign; you can also open them yourself.

Invoke once. This skill drives its own loop via `ScheduleWakeup`; do NOT wrap it in `/loop`.

## Args

`/gauntlet:campaign  #PR... | --new #PR... | --run <id> | (no args)`

These are **distinct modes**, not freely-composable flags: `--new` requires a `#PR` set; `--run <id>`
resumes and takes no `#PR`/`--new`; `#PR` args alone start/adopt into a run. Invalid combinations
(e.g. `--new` with no PRs, or `--run` with `#PR`) are rejected / fall through to the idle prompt.

- `#12` / `#12 #15` -> adopt those existing PRs into a run; gate + merge them.
- No argument -> discover this run's labelled PRs and continue. If none and nothing to do, PROMPT:
  "No PRs under a campaign. Run gauntlet:review to find issues, or pass PR numbers to gate."
- Non-PR arg (e.g. `auth`) -> same prompt (the old area/topic sweep arg is REMOVED).
- `--run <id>` -> resume specific run; self-wakes also carry internal `--token`.
- `--new` or "fresh run" -> force independent new run-id for a new PR set (with carryover).

## Bundled Scripts

Bundled scripts live in `scripts/`, resolved relative to the directory holding this `SKILL.md` (not
the current working directory) — installed as `${CLAUDE_PLUGIN_ROOT}/skills/campaign/scripts/`, or in
the repo at `plugins/gauntlet/skills/campaign/scripts/`. Pass their absolute paths to subtasks.
`scripts/review-pass.py` is the schema-owning accessor for a review pass's artifacts — the plan, the
`pass_identity`, the unit-progress events, **the findings**, and the READ that answers "does this pass
COUNT?", which
validates every line of those files, including the one event the emit-only rule exempts from tool-writing
(see `references/stage-2-review-gate.md`); never hand-parse one of those files, and never hand-write a
line the tool writes.
`scripts/emit-progress.py` is the reviewer's door into it — **its CLI is unchanged**, and it is the only
sanctioned way to record a unit-progress event. **`scripts/emit-finding.py` is the reviewer's other door,
and the only sanctioned way to report a FINDING**: every finding is a validated record that ANCHORS to the
PR's intent (a `## Purpose` line quoted verbatim, or the actor who can actually write the bad input), and a
finding that anchors to neither is **NON-GATING** — recorded as a follow-up, never a `NOT SATISFIED`, never
a fix. `scripts/ledger.py` is the schema-owning accessor for
`state.jsonl` — read/write the ledger header and per-PR rows **by field name** through it, never by column
position (see `references/files-and-ledger.md`); pass its absolute path to subtasks the same way. It also
owns the **review loop's memory and its caps**: `ledger.py verdict` is the **only** sanctioned way to
record a review verdict (it bumps `review_rounds`/`ns_streak`, applies the tally, and **at a cap holds the
PR `repairing` and exits non-zero**), and `ledger.py dispatch-check` is the guard you run before **any**
action that mutates a PR. `scripts/repair-pass.py` records the reassessment pass's decision for a PR that
has stopped converging — the closed enum, the ownership guardrail, and the repair cap
(`references/repair-pass.md`).

Each script's fixtures live in a **sibling `*-test.py`** (`review-pass-test.py`, `ledger-test.py`,
`repair-pass-test.py`); the `self-test` subcommand loads it and **fails loudly if it is missing**.

**At run startup, record which version of these rules is actually running:** read `version` from the
running plugin's `plugin.json` and write it to the ledger header —
`ledger.py --file <state.jsonl> header set skill_version <version>`. The harness loads this skill from the
**installed plugin cache**, so a merged, version-bumped rule governs **nothing** until that cache refreshes;
one already did not, for days, and no artifact of the run recorded it.

## Subagent Dispatch — model per class

Campaign spawns subagents for several jobs. **Set the model explicitly on every dispatch.** With no
model set, a subagent inherits the session model (often the most expensive one) — so an unset model is
a silent cost decision, taken by default, on every subagent this skill launches.

| Subagent | Model | Why |
|---|---|---|
| Review pass (default reviewer) | **session model** | It *is* the gate. A weaker verdict is a worse gate — the one thing never worth cheapening. |
| Fresh-subagent fallback review | **session model** | Same job as a review pass; counts toward the gate identically. |
| Review-fix (after `NOT SATISFIED`) | **session model** | Authors code from scratch, judged only by another full review pass. A cheap bad fix burns a whole review pass and a gate reset — it *costs* more than the tier saves. |
| Root-cause **mapper** | **session model** | Read-only, but NOT low-judgment: it enumerates a full matrix and confirms each gap with a repro. A weaker model **under-maps**, which is the exact failure the mapper exists to prevent (`root-cause-pass.md`). "Read-only" is not a licence to downgrade. |
| **Reassessment pass** (a PR at a review-loop cap) | **session model** | It reads a PR's ENTIRE history at once and decides the **acceptance path** — rescope, re-intent, demote, root-cause, or abort. It is gate machinery, and it is the one judgment no wake in the failed run was ever able to make. A weaker model here mis-diagnoses the loop and repairs the wrong thing (`repair-pass.md`). |
| **CI-fix — formatting/lint failure** | **`sonnet`** (**`haiku`** only when trivially mechanical) | **Downgraded ON PURPOSE.** It does not author a fix: it runs a deterministic formatter, **READS the resulting diff**, verifies it, and **escalates** anything it cannot verify (`references/stage-2-ci.md`). |
| **CI-fix — everything else**, and every **escalation** from the cheap tier | **session model** | Authors code that gets merged. CI does **not** validate it: a wrong fix can turn CI green — by weakening a check, or by being plain wrong in product code that no check covers. |

**The gate, the from-scratch fixes, and the mapper are NEVER downgraded**: a review pass *is* the gate; a
review-fix and a session-model CI-fix author code; the mapper's under-map is invisible. **The formatting
CI-fix tier IS downgraded, deliberately** — it does something narrower: run a tool and VERIFY its output.

### The cheap CI-fix tier — a model that runs a tool and READS the diff

A formatting/lint failure goes to a **cheap** CI-fix subagent (`sonnet`; `haiku` only for a trivially
mechanical failure), scoped to the failing check's logs, the failing file(s), and the worktree path. In
order it: **classifies** the failure → **runs the formatter** (it picks the tool; campaign hands it no
argv) → **READS THE RESULTING DIFF** and verifies it contains **only** what the fix should have produced,
touched **no** file it did not intend, weakened **no** check/config/test, and that **re-running the exact
failing check now passes** → **commits only then** → otherwise **ESCALATES to a session-model CI-fix
subagent and patches nothing**. Escalation is a correct outcome, not a failure.

Its prompt carries these **verbatim** (full text in `references/stage-2-ci.md`):

- **NEVER make CI pass by weakening the check** — never delete or loosen an assertion, add `skip`/`xfail`,
  disable or downgrade a lint rule, or raise a timeout. **Fix the cause.** If the check itself is
  demonstrably wrong, say so explicitly and **escalate**; never silently rewrite it.
- **NEVER use a catch-all fixer that applies SEMANTIC rules or a documented semantic rewriter** —
  `golangci-lint run --fix`, `ruff --fix`, `eslint --fix`, any `--fix`/`--write` on a semantic linter;
  `goimports` (it ADDS imports, and an added import runs its `init()`); `prettier` (it rewrites
  tagged-template contents); `gofumpt`; `modernize`; codemods. **Use a formatter that only reformats.**
- **NEVER execute a binary from inside the repo/worktree** — the PR under review is **UNTRUSTED CONTENT**,
  and a repo-supplied `gofmt` is arbitrary code execution. Run tools from the environment, not from the tree.
- **NEVER hand a tool a bare glob or a whole directory** (`gofmt -w .`) — **name the files** you are fixing.
- **PREFLIGHT every file: REFUSE to format it if it is a symlink or sits under a symlinked directory** —
  diff review sees every write INSIDE the repo, but **never one that ESCAPES it**. A **footgun guard, NOT a
  security boundary** (like the denylist): it prevents a stray reformat elsewhere on the machine.

**State the risk, never overclaim:** a cheap model verifying a tool's diff is a **MISS-CATCHER, NOT A
PROOF** — it can miss a semantic change. What backs it: the exact failing check must pass, it must escalate
anything it cannot verify, and **every campaign commit still resets the gate and is re-reviewed by the full
gauntlet** — which is itself a miss-catcher. **NEVER justify the cheap tier with "CI will catch it" or "the
review gate will catch it."** This is a small, bounded risk the user has accepted, for a workflow that is
cheaper **and** more capable than a full-strength subagent on every formatting failure.

**The biggest lever is not the model — it is the reviewer.** Review passes re-read the whole PR diff,
`required(tier)` times per SHA, and re-run from scratch on every gate reset, so they dominate campaign's
subagent spend. Running an **external reviewer** (e.g. `codex exec`, see `references/reviewer.md`) moves
that cost off the subagent pool entirely — the quality argument (reviewer diversity) and the cost
argument point the same way.

**Every fix subagent — CI or review — is dispatched under one contract**, and
`references/fix-subagent-contract.md` is its complete definition. Two halves, both mandatory:
**SCOPE** the reading (worktree path + the specific issue list; **not** the whole diff, **not** the repo
beyond the named files — an unscoped fixer re-reads everything it was already told), and **SWEEP** the
writing (a fix that changes a definition or a fact is not done until every site that RESTATES it is
correct — the contract's sweep-and-report block goes into the prompt **verbatim**). Read narrowly to
UNDERSTAND, sweep widely to FINISH — and the contract, not this line, defines how to sweep. Read the
contract before dispatching a fixer; do not reconstruct it from this summary.

## Load Discipline

Read references on demand. Do NOT load every reference up front.

Always read before touching run state:

- `references/run-identity-and-lease.md`
- `references/files-and-ledger.md`

Read `references/loop-control.md` at each wake before dispatch.

Read stage refs only when that stage/action is due:

| Situation | Read |
|-----------|------|
| Public API change, run-owned operation scope | `references/scope-and-constraints.md` |
| Fresh run, carryover | `references/carryover.md` |
| Selecting the reviewer; external-reviewer failure/fallback | `references/reviewer.md` |
| Adopting PRs into a run (worktree/labels/ledger row) | `references/pr-adoption.md` |
| PR review gauntlet / progress ledger | `references/stage-2-review-gate.md` |
| Dispatching ANY fix subagent (CI-fix or review-fix) | `references/fix-subagent-contract.md` |
| Repeated sibling findings / shared root cause | `references/root-cause-pass.md` |
| A PR at a review-loop cap (`status = repairing`) / a verdict that exits non-zero | `references/repair-pass.md` |
| CI watch, check polling, CI fix | `references/stage-2-ci.md` |
| Merge candidate / base refresh / cleanup | `references/stage-3-merge.md` |
| Stuck task, abort, final report | `references/bailout-and-final-report.md` |
| Rule lookup / uncertainty | `references/critical-rules.md` |

## Core Invariants

- **PR-gating:** adopt PR -> triage tier -> watch CI + review PR HEAD -> merge. PRs are **adopted, not
  generated** — campaign gates existing PRs and never writes fixes from scratch.
- **Work-conserving:** every wake reconciles, folds completions, launches all due work up to caps,
  drains still-ready PRs serially, then reschedules only when no useful action remains launchable.
- **Driver never blocks:** reviews and CI watches run as background tasks — completions are wakes.
  A PR with a **still-RUNNING** check always has a live watch — but a PR whose CI has **SETTLED** does
  **not** (watching it burns a wake per second and observes nothing: `references/stage-2-ci.md`, "WATCH
  ONLY WHAT CAN MOVE"). A review doomed by a pending content change is stopped, not awaited.
- **Run isolation:** touch only this run's `<rundir>`, ledger, labels, branches, PRs, and worktrees.
- **One active driver:** lease controls ownership; never double-drive one run.
- **Base branch is data:** read `base_branch` from ledger every wake; never assume `main`.
- **Reviewer is data:** read `reviewer` from ledger every wake before dispatching any review; set once at run start, never re-derived from memory (else an explicit/preferred reviewer silently reverts to default on a self-wake or adoption).
- **Model is set explicitly on every subagent dispatch:** never let a dispatch inherit the session model by default. CI-fix on a formatting failure is cheap **by design**; review passes, review-fixes, and the mapper are never downgraded ("Subagent Dispatch").
- **Remote branch cleanup isn't campaign's job:** campaign never passes `--delete-branch`; the repo's *Automatically delete head branches* setting governs the remote head branch. Local worktree/branch cleanup follows the per-PR `worktree_owned`/`branch_owned` flags.
- **Review gate is tier-dependent:** `required(tier)` fresh, context-isolated `SATISFIED` verdicts on
  same live PR content + green CI — **1 if TRIVIAL, else 2** (any code/agent-doc/sensitive change is 2).
- **The review loop has MEMORY, and it is capped:** record every verdict with `ledger.py verdict` (never
  hand-set `reviews_ok` for one) — it bumps `review_rounds` (**NEVER reset**) and `ns_streak`. At a cap it
  holds the PR `repairing` and **exits non-zero**: stop dispatching targeted fixes, hand the PR's **whole
  history at once** to a context-isolated reassessment pass, and execute the ONE decision it returns —
  **RESCOPE / REPAIR-INTENT / DEMOTE / ROOT-CAUSE / ABORT** — **without asking the user**. A cap is a
  **mode switch, not a doorbell** (`references/repair-pass.md`). **Autonomous repair NEVER rewrites a PR
  campaign does not own**: on `pr_origin = external` (the default) only DEMOTE / REPAIR-INTENT / ABORT are
  permitted, and a second failed repair **aborts** rather than looping.
- **Sequential same-PR reviews:** launch review 2 only after review 1 is `SATISFIED` (TRIVIAL needs
  only one).
- **Progress ledger:** reviewer progress means planned unit `done` or accepted amendment, not vague
  output.
- **The review is measured against the PR's INTENT, never against "is anything wrong with this code?"**
  The reviewer is handed `<rundir>/intent-<pr>.md` **verbatim** (`## Purpose` / `## Non-goals` /
  `## Threat model`; written at adoption, local and git-ignored — **never** written back to the PR) and
  answers ONE question: **does this PR achieve its stated Purpose, without breaking anything reachable by an
  actor named in its Threat model?** The open-ended question has **no fixed point** — it ran one PR through
  21 rounds of true, reproduced, irrelevant findings. **Non-goals BIND** the reviewer. The adversarial sweep
  **stays**, bounded by the threat model rather than by nothing (`references/stage-2-review-gate.md`).
  **It is the PASS that is measured against it, not merely its findings:** `review-pass.py verify` loads
  the intent for **every** pass — including one that found nothing, which is the case that merges a PR — so
  a PR with no usable intent block earns **no verdicts at all**.
- **A finding must ANCHOR, or it does not gate:** every finding is a record written by
  `scripts/emit-finding.py`, naming **either** the `## Purpose` line it defends (quoted verbatim) **or** the
  `writer` who can actually supply the bad input (a CLOSED enum). **A finding whose `purpose` is `-` AND
  whose `writer` is `driver-only`/`hand-edit`/`dev-time` is NON-GATING** — recorded as a follow-up, never a
  `NOT SATISFIED`, never a fix. Enforced in `review-pass.py`, not by good intentions. **Not every true
  statement about the code is a reason to block it**; a guard being incomplete is not, by itself, a defect.
  **And the rule runs BOTH ways — it is an if and only if: `NOT SATISFIED` exactly when at least one GATING
  finding stands.** A pass that records a gating finding and returns `SATISFIED` anyway is `unusable`, the
  same as one that blocks with nothing to point at. A finding cannot be blocking in the artifact and
  ignorable in the verdict. **The verdict is a REQUIRED input to `verify`** (`--verdict`, what the report's
  `VERDICT:` line says): a COMPLETE pass verified without one is `unusable`, because a guard whose input
  can be ABSENT never fires.
- **Verdicts go through `ledger.py verdict` — NEVER set `reviews_ok` by hand.** It bumps `review_rounds`
  (**monotone, never reset — the loop's only memory across fresh-context wakes**), applies the tally, and
  moves `ns_streak`, atomically. `set` cannot RAISE the tally and no door can write the counters at all.
- **Findings are claims, not facts:** on every `NOT SATISFIED`, audit each **gating** finding (CONFIRMED /
  ADJUSTED / REFUTED, with evidence, into `audit-<pr>-<n>.md`) BEFORE dispatching a fix; only
  CONFIRMED + ADJUSTED get fixed. That asks **is it TRUE?**; the gating rule above asks **does it MATTER?**
  — both must pass, and a finding must matter before anyone spends an audit on whether it is true. The
  reachability test asks whether the **mechanism can occur**, not
  where the trigger comes from; **unsure → CONFIRMED, never REFUTED**. A refutation NEVER clears the
  gate — `reviews_ok` stays 0 — and is **written into the tree** as an inline comment at the site and
  **committed**: a commit is PR content, so it resets the gate and the next reviewer REVIEWS the
  argument. The comment MUST be a falsifiable claim with evidence, NEVER an instruction to the reviewer;
  reviewers verify such comments, and a wrong claim is a finding. Refute a finding **once** — if the
  fresh reviewer re-raises it, that is a standoff → park `awaiting-user` for the USER to adjudicate
  (`references/stage-2-review-gate.md`).
- **A HELD PR is FROZEN — take no action that MUTATES it. `ledger.py … dispatch-check --pr <N>` exits
  non-zero for one.** Two kinds: **parked** (`awaiting-user` / `awaiting-api` — waits on a HUMAN) and
  **`repairing`** (at a review-loop cap; waits on the reassessment pass, **not** on a human). The test is
  "does this mutate the PR?", **not** "is it on a list": never review,
  CI-fix, review-fix, merge, rebase, base-refresh, push to, or relabel it — nor anything else that
  changes it (being held does NOT lower `reviews_ok`, so guard on `status` at every dispatch AND mutation
  site). `HELD_STATUSES` in `scripts/ledger.py` is the one enumeration — never retype it. Sole exception: the park does not change the CI watch either way — observing is not mutating, so
  the watch follows the normal policy (alive only while a check can still move). Keep driving the other
  PRs; unpark only on the user's answer — **recorded DURABLY, per park class** (`api_approval`; the
  standoff's audit record; `blocker_ruling` = `retry`/`abort` for a machine blocker, where `retry` clears
  the liveness counters and is **SPENT** — a ruling is durable **and consumed exactly once**, cleared on
  park entry and on consumption, so a previous park's answer can never unpark a later one;
  `references/stage-2-ci.md`, "THE RULING IS CONSUMED EXACTLY ONCE"). **Every park names the event that
  leaves it** (`references/loop-control.md`,
  "held-status guard" and "Only the user's answer unparks a PR").
- **No green by watch exit:** derive CI from a **SHA-pinned** snapshot of **both** check families
  (`check-runs` **and** commit `status`), verified against `head_sha` before parsing. **NEVER from `gh pr
  checks`** — its output carries **no SHA** (`references/stage-2-ci.md`).
- **Public API changes require user confirmation** unless the ledger's `api_changes` field is `allowed`.

## Wake Skeleton

1. **Resolve run + lease;** adopt only absent/stale lease, stand down if fresh different owner.
2. **Adopt `#PR` args + reconcile run-labelled PRs.** For each explicit `#PR`, adopt it per
   `references/pr-adoption.md` (refresh existing row on re-adoption, never duplicate); then reconcile
   every PR carrying this run's `gauntlet-run-<run-id>` label from a batched snapshot. Treat `state.jsonl`
   as cache.
3. **Fold completed review / CI / fix tasks** against the SHA each ran on.
4. **Triage tier per PR, then launch due gate work up to caps — skipping HELD PRs** (`ledger.py …
   dispatch-check --pr <N>` exits non-zero: FROZEN, no action that mutates the PR — no review, CI fix,
   review fix, merge, rebase, base refresh, or relabel, and nothing else that changes it; the CI watch is
   unaffected and follows the normal policy). **A `repairing` PR is the one held PR that still has machine
   work due**: run its reassessment pass, then dispatch the decision it returns — and never a plain fix
   (`references/repair-pass.md`).
   Re-derive each non-held PR's tier from its
   `head_sha` (deterministic file-class triage), then launch reviews up to `required(tier)`, CI
   watches/fixes, precondition clearing (Copilot items / red CI / base conflict), and base refresh;
   stop in-flight reviews doomed by a content change.
5. **Merge ready PRs** (never a parked one) one at a time until no candidate remains immediately ready
   after base refresh.
6. **Launch audit + heartbeat — before sleeping, verify every due launch actually happened.** Re-run
   step 4's dispatch scan across both concurrency pools (CI-fix subagents and review passes each have
   their own cap): confirm every due review pass was launched, a CI watch is live for every PR with a
   **still-RUNNING** check (**not** for one whose CI has settled — that is the hot-spin bug), that every
   PR at **any liveness cap** was **escalated** rather than left spinning — the caps are named in ONE
   place (`references/stage-2-ci.md`, "THE LIVENESS COUNTERS"; a PR whose check has been `RUNNING` with an
   unchanged fingerprint past the CI STALL CAP is at a cap too, and its watch will never wake anyone) —
   and — whenever any
   non-terminal work remains — a `ScheduleWakeup` heartbeat is actually
   scheduled. If any due launch or the heartbeat is missing, launch it and re-audit. NEVER sleep with
   due work un-launched or the heartbeat unscheduled.
7. **Terminal -> carryover/report;** otherwise refresh lease, show the user where the run stands
   (`ledger.py … table` output plus what each wait is on — `references/loop-control.md`, "Reschedule
   or exit"), and return (step 6 has already ensured the heartbeat is scheduled).

## Critical Rules

- NEVER review unpublished local work.
- NEVER spend review over open Copilot items, red checks, or conflicts.
- NEVER pass destructive instructions to an external reviewer command (e.g. `codex exec`).
- NEVER use `--dangerously-bypass-approvals-and-sandbox`; use `--sandbox workspace-write`.
- NEVER force-push/reset/delete outside explicit stage procedure and run scope.
- NEVER touch another run's PR/branch/worktree.
- NEVER merge over red/pending CI or stale review verdicts.
- NEVER add "Test plan" section to PR bodies.
- NEVER commit run/scratch files (the whole `.gauntlet/**` tree) — they are
  driver bookkeeping, not repo content. Stage only the specific source files a fix touches, by explicit
  path; never `git add -A`/`git add .`. Ensure `.gauntlet/` is git-ignored (add it if missing). Campaign has
  **no committed file of its own** — no repo-root config (`files-and-ledger.md`).
- NEVER `rm -rf .gauntlet/`; only `.gauntlet/tmp/**` is disposable — the rest is carryover history.
