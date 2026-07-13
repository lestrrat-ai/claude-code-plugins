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
the repo at `plugins/gauntlet/skills/campaign/scripts/`. Pass their absolute paths to subtasks. The
review gauntlet's `scripts/emit-progress.py` (already present there) emits canonical reviewer progress
events (see `references/stage-2-review-gate.md`). `scripts/ledger.py` is the schema-owning accessor for
`state.jsonl` — read/write the ledger header and per-PR rows **by field name** through it, never by column
position (see `references/files-and-ledger.md`); pass its absolute path to subtasks the same way.

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

**Scope every fix subagent.** Give it the worktree path and the specific issue list, and tell it **not**
to re-derive the whole diff or re-read the repo beyond the named files. An unscoped fixer re-reads
everything it was already told.

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
| Repeated sibling findings / shared root cause | `references/root-cause-pass.md` |
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
  A pending-CI PR always has a live watch; a review doomed by a pending content change is stopped,
  not awaited.
- **Run isolation:** touch only this run's `<rundir>`, ledger, labels, branches, PRs, and worktrees.
- **One active driver:** lease controls ownership; never double-drive one run.
- **Base branch is data:** read `base_branch` from ledger every wake; never assume `main`.
- **Reviewer is data:** read `reviewer` from ledger every wake before dispatching any review; set once at run start, never re-derived from memory (else an explicit/preferred reviewer silently reverts to default on a self-wake or adoption).
- **Model is set explicitly on every subagent dispatch:** never let a dispatch inherit the session model by default. CI-fix on a formatting failure is cheap **by design**; review passes, review-fixes, and the mapper are never downgraded ("Subagent Dispatch").
- **Remote branch cleanup isn't campaign's job:** campaign never passes `--delete-branch`; the repo's *Automatically delete head branches* setting governs the remote head branch. Local worktree/branch cleanup follows the per-PR `worktree_owned`/`branch_owned` flags.
- **Review gate is tier-dependent:** `required(tier)` fresh, context-isolated `SATISFIED` verdicts on
  same live PR content + green CI — **1 if TRIVIAL, else 2** (any code/agent-doc/sensitive change is 2).
- **Sequential same-PR reviews:** launch review 2 only after review 1 is `SATISFIED` (TRIVIAL needs
  only one).
- **Progress ledger:** reviewer progress means planned unit `done` or accepted amendment, not vague
  output.
- **Findings are claims, not facts:** on every `NOT SATISFIED`, audit each finding (CONFIRMED /
  ADJUSTED / REFUTED, with evidence, into `audit-<pr>-<n>.md`) BEFORE dispatching a fix; only
  CONFIRMED + ADJUSTED get fixed. The reachability test asks whether the **mechanism can occur**, not
  where the trigger comes from; **unsure → CONFIRMED, never REFUTED**. A refutation NEVER clears the
  gate — `reviews_ok` stays 0 — and is **written into the tree** as an inline comment at the site and
  **committed**: a commit is PR content, so it resets the gate and the next reviewer REVIEWS the
  argument. The comment MUST be a falsifiable claim with evidence, NEVER an instruction to the reviewer;
  reviewers verify such comments, and a wrong claim is a finding. Refute a finding **once** — if the
  fresh reviewer re-raises it, that is a standoff → park `awaiting-user` for the USER to adjudicate
  (`references/stage-2-review-gate.md`).
- **A parked PR is FROZEN — take no action that MUTATES it.** `status = awaiting-user` / `awaiting-api`
  waits on a HUMAN. The test is "does this mutate the PR?", **not** "is it on a list": never review,
  CI-fix, review-fix, merge, rebase, base-refresh, push to, or relabel it — nor anything else that
  changes it (a park does NOT lower `reviews_ok`, so guard on `status` at every dispatch AND mutation
  site). Sole exception: its CI watch keeps running — observing is not mutating. Keep driving the other
  PRs; unpark only on the user's answer (`references/loop-control.md`, "parked-status guard").
- **No green by watch exit:** derive CI from re-polled `gh pr checks` snapshot.
- **No green from ONE CHECK FAMILY, no green by watch exit, no green from an unpinned snapshot, no green
  from a PARTIAL one, no green from a SUPERSEDED one, no green while a DECLARED REQUIRED CHECK is
  missing:** GitHub has **two** check families — **Checks** (check runs) and **legacy commit statuses**
  (Jenkins, CircleCI, many bots) — and **a failing commit status is INVISIBLE to `/check-runs`**. Derive CI
  from a source covering **both**, pinned to the current `head_sha`:
  `gh pr view <pr> --json headRefOid,statusCheckRollup` returns both families **and** the head SHA in one
  payload (`__typename` `CheckRun` → `.name`/`.status`/`.conclusion`; `StatusContext` → `.context`/`.state`
  — no `.conclusion`, and `ERROR` is a failure). Never read the *combined* status `.state` (it is `pending`
  on **zero** statuses). `gh pr checks` is not pinned to a commit and will report the PREVIOUS commit's
  passing checks right after a push. Fetch **ATOMICALLY** — temp file **inside `<rundir>`** (`mv` is atomic
  only within a filesystem), promoted onto the **SHA-scoped** `ci-<pr>-<head_sha>.txt` (first line
  `# sha: <head_sha>`, emitted by the same query as the rows) only if **every** fetch exits 0;
  a fetch redirected straight onto the snapshot leaves the rows that arrived on disk when it dies. **Prove
  the SOURCE is complete (both families), prove the snapshot is COMPLETE (`--paginate` on every REST fetch;
  a rollup at its ~100-context cap is truncated → pending), prove its `# sha:` matches the ledger's current
  `head_sha`, and prove EVERYTHING YOU EXPECT TO SEE IS THERE, before parsing it** — a watch launched for
  an older SHA can finish after the head advanced; absent, partial, or mismatched → `ci = pending`,
  relaunch, NEVER green (`references/stage-2-ci.md`).
- **Registered ≠ expected, and the right NAME is not the right PRODUCER.** Checks register
  **asynchronously**, so an all-success snapshot can simply predate the check that would have failed. Read
  the declared required checks from **BOTH** classic branch protection **and rulesets** (a repo can use
  either or both) and take the **UNION**: **green requires every required check PRESENT and successful** —
  one unregistered → `ci = pending`, NEVER green. **Where the declaration binds an app** (`app_id` /
  `integration_id`), match the **producer** too — the check run's `.app.id`, from the SHA-pinned check-runs
  fetch — or a same-named check from **another** app satisfies the test while the required one has not run.
  **Where it binds no app, any producer of that name satisfies it.**
- **THREE STATES, NEVER TWO — DECLARED / NONE DECLARED / CANNOT READ. "I cannot see any" is NOT "there are
  none."** `branches/<base>/protection/required_status_checks` **404s both** when the branch is unprotected
  **and** when the token lacks **Administration: read**. **NONE DECLARED** is provable **only** when the
  required-set read **SUCCEEDED and came back EMPTY** — registration completeness then **CANNOT be proven**,
  and green means only *"every check registered by the time we looked had passed"*: **state that residual
  risk; never claim it away.** **CANNOT READ** (404/403 without admin, any error on either endpoint) is
  **UNKNOWN — not "none declared"**: never fall through to the weaker rule, record the uncertainty, and
  never claim registration completeness. **Prefer the rulesets endpoint — it needs no admin, and the
  classic endpoint cannot see rulesets at all.** **NEVER infer the ABSENCE of a requirement from an ERROR
  that also means "you may not look"** (`references/stage-2-ci.md`, "The registration gap").
- **NEVER merge without GitHub's own verdict: `MERGEABLE` + `mergeStateStatus == CLEAN`.** GitHub computes
  it knowing the required-check set; campaign's snapshot does not. `BLOCKED` (required check missing/failing)
  and `UNSTABLE` (non-required check failing) both mean **do not merge**. It does not prove unregistered
  checks are absent — require it anyway, never overclaim it (`references/stage-3-merge.md`).
- **Public API changes require user confirmation** unless the ledger's `api_changes` field is `allowed`.

## Wake Skeleton

1. **Resolve run + lease;** adopt only absent/stale lease, stand down if fresh different owner.
2. **Adopt `#PR` args + reconcile run-labelled PRs.** For each explicit `#PR`, adopt it per
   `references/pr-adoption.md` (refresh existing row on re-adoption, never duplicate); then reconcile
   every PR carrying this run's `gauntlet-run-<run-id>` label from a batched snapshot. Treat `state.jsonl`
   as cache.
3. **Fold completed review / CI / fix tasks** against the SHA each ran on.
4. **Triage tier per PR, then launch due gate work up to caps — skipping PARKED PRs entirely** (`status`
   `awaiting-user` / `awaiting-api`: FROZEN, no action that mutates the PR — no review, CI fix, review
   fix, merge, rebase, base refresh, or relabel, and nothing else that changes it; CI watch stays).
   Re-derive each non-parked PR's tier from its
   `head_sha` (deterministic file-class triage), then launch reviews up to `required(tier)`, CI
   watches/fixes, precondition clearing (Copilot items / red CI / base conflict), and base refresh;
   stop in-flight reviews doomed by a content change.
5. **Merge ready PRs** (never a parked one) one at a time until no candidate remains immediately ready
   after base refresh.
6. **Launch audit + heartbeat — before sleeping, verify every due launch actually happened.** Re-run
   step 4's dispatch scan across both concurrency pools (CI-fix subagents and review passes each have
   their own cap): confirm every due review pass was launched, a CI watch is live for every pending-CI
   PR, and — whenever any non-terminal work remains — a `ScheduleWakeup` heartbeat is actually
   scheduled. If any due launch or the heartbeat is missing, launch it and re-audit. NEVER sleep with
   due work un-launched or the heartbeat unscheduled.
7. **Terminal -> carryover/report;** otherwise refresh lease and return (step 6 has already ensured the
   heartbeat is scheduled).

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
