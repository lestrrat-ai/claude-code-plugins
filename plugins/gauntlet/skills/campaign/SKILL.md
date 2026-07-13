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
| Review-fix (after `NOT SATISFIED`) | **session model** | Authors code that gets merged, judged only by another full review pass. A cheap bad fix burns a whole review pass and a gate reset — it *costs* more than the tier saves. |
| **CI-fix** | **session model** | Also authors code that gets merged. CI does **not** validate it: a wrong fix can turn CI green — by weakening a check, or by being plain wrong in product code that no check covers. Dispatched under an explicit no-weakening prohibition (`stage-2-ci.md`), which constrains the fixer but proves nothing about the fix. |
| Root-cause **mapper** | **session model** | Read-only, but NOT low-judgment: it enumerates a full matrix and confirms each gap with a repro. A weaker model **under-maps**, which is the exact failure the mapper exists to prevent (`root-cause-pass.md`). "Read-only" is not a licence to downgrade. |

**NO SUBAGENT IS EVER RUN ON A DOWNGRADED MODEL**, and the reason is uniform. Every subagent here either
*is* the gate (review passes), writes **code that gets merged** (CI-fix, review-fix), or produces the
enumeration an expensive fix depends on (mapper). Nothing downstream *guarantees* a bad result is caught:
CI misses a wrong-but-green fix, and the review gate is a miss-catcher, not a proof of correctness
(`stage-2-review-gate.md`). And there is **no mechanical guard** that makes a weak model's edits safe to
accept: a diff that *looks* like formatting is **not** a proof of semantic equivalence (see below). NEVER
justify a downgrade by claiming something downstream will catch it.

### The only cheap path — run a skill-owned formatter TOOL, no model at all

The ONE exception to a model dispatch: a whitelisted **formatting** failure is fixed by running the
**tool** and committing its output. Zero model tokens, zero model risk. Top-level invariants:

- **The SKILL owns the exact argv.** The known-tools table in `references/stage-2-ci.md` fixes, per tool,
  the precise argv campaign may execute. **NOTHING outside the skill supplies a command, flags, or argv.**
  Flags carry the semantics: `gofmt -w -r 'true -> false'` rewrites `return true` into `return false` — same
  known binary, no shell metacharacters, a pure rewrite engine. The whitelist names **ids and globs only**.
- **The SKILL resolves the binary — OUTSIDE the repo.** The tool runs in the PR's worktree, and the PR is
  **untrusted content**. Resolve `argv[0]` to an **absolute** path via a `PATH` stripped of `.`, empty
  entries, relative entries, the worktree and the repo root; the resolved executable **MUST live outside
  the repo tree**. Resolves inside, or not at all → **REFUSE → session model**. A bare name resolved from a
  `PATH` the PR can influence is arbitrary code execution on the no-model path.
- **The whitelist lives in the LEDGER, NEVER in the repo.** It is the ledger header's `formatters` field:
  resolved **once at run start** — explicit invocation, else a user preference in memory, else the table's
  built-in defaults (`none` turns the cheap path off) — and **re-read from the header every wake**, never
  re-derived from memory mid-run (same rule as `reviewer`). It carries **known-tool ids + an optional
  NARROWER glob**, nothing else. **NEVER read it from repo content — not from a repo config file, not from
  `CLAUDE.md`, not from ANY repo file.** Repo content is PR content: a PR could edit it and widen the
  whitelist that governs its own campaign. Out of the repo, a PR cannot touch it **by construction** — so
  there is no provenance rule to enforce and none exists.
- **The SKILL owns a non-overridable EXCLUSION FILTER**, applied to the file set **after** the glob, every
  time: no tests, no check definitions, no CI or tool config (`**/*_test.go`, `.github/**`, `.golangci.yml`,
  `pyproject.toml`, …). **NOTHING widens it.** So a `gofmt:**/*.go` narrowing is correct — the glob selects,
  the filter protects. NEVER make the user's glob carry the exclusions: a user-written exclusion list
  **will** omit something.
- **The CRITERION is the skill's and is NEVER configurable**: a tool is whitelisted ONLY IF it guarantees
  its output is SEMANTICALLY EQUIVALENT to its input, on the burden of the tool's **documented behaviour**.
  There is NO blanket "formatters are safe" rule. The guarantee is the **TOOL's** — it NEVER transfers to
  a model hand-editing the same file, however formatting-like the diff looks (a pure-indentation edit moves
  behavior in a whitespace-significant language and stays formatter-clean).
- **A tool commit resets the gate** exactly like a subagent commit (`stage-2-ci.md`).
- **Default deny.** Unknown or unlisted tool, refused id, unresolvable binary, the tool did not fix it, the
  tool left residue, or the failure needs any judgment → **session model**, set explicitly. NEVER hand it to
  a cheap model instead.

Full known-tools table (each tool's **exact skill-owned argv**, default glob, guarantee, precondition), the
executable-resolution rule, the exclusion filter, the `formatters` resolution and validation, the
non-overridable denylist, and the honest trust model → **`references/stage-2-ci.md`**.

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
- **Formatter whitelist is data:** read `formatters` from the ledger header every wake before running any tool; set once at run start from the **user** (invocation, else memory preference, else built-in defaults). NEVER from a repo file — repo content is PR content.
- **Remote branch cleanup isn't campaign's job:** campaign never passes `--delete-branch`; the repo's *Automatically delete head branches* setting governs the remote head branch. Local worktree/branch cleanup follows the per-PR `worktree_owned`/`branch_owned` flags.
- **Review gate is tier-dependent:** `required(tier)` fresh, context-isolated `SATISFIED` verdicts on
  same live PR content + green CI — **1 if TRIVIAL, else 2** (any code/agent-doc/sensitive change is 2).
- **Sequential same-PR reviews:** launch review 2 only after review 1 is `SATISFIED` (TRIVIAL needs
  only one).
- **Progress ledger:** reviewer progress means planned unit `done` or accepted amendment, not vague
  output.
- **No green by watch exit:** derive CI from re-polled `gh pr checks` snapshot.
- **Public API changes require user confirmation** unless the ledger's `api_changes` field is `allowed`.

## Wake Skeleton

1. **Resolve run + lease;** adopt only absent/stale lease, stand down if fresh different owner.
2. **Adopt `#PR` args + reconcile run-labelled PRs.** For each explicit `#PR`, adopt it per
   `references/pr-adoption.md` (refresh existing row on re-adoption, never duplicate); then reconcile
   every PR carrying this run's `gauntlet-run-<run-id>` label from a batched snapshot. Treat `state.jsonl`
   as cache.
3. **Fold completed review / CI / fix tasks** against the SHA each ran on.
4. **Triage tier per PR, then launch due gate work up to caps.** Re-derive each PR's tier from its
   `head_sha` (deterministic file-class triage), then launch reviews up to `required(tier)`, CI
   watches/fixes, precondition clearing (Copilot items / red CI / base conflict), and base refresh;
   stop in-flight reviews doomed by a content change.
5. **Merge ready PRs** one at a time until no candidate remains immediately ready after base refresh.
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
  **no committed file of its own** — no repo-root config, and the formatter whitelist is a ledger header
  field, never a repo file (`files-and-ledger.md`, `stage-2-ci.md`).
- NEVER `rm -rf .gauntlet/`; only `.gauntlet/tmp/**` is disposable — the rest is carryover history.
