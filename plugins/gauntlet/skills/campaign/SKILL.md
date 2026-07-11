---
name: campaign
description: >-
  Gates PRs to merge. A self-looping review-to-merge campaign: existing PRs are adopted into a run (pass PR numbers, or discover this run's labelled PRs), each PR is triaged to a review tier, and a per-PR review gauntlet (tier-dependent fresh, context-isolated SATISFIED verdicts on the whole PR diff, reviewed one at a time) plus event-driven CI monitoring gate an auto-merge. Multiple isolated runs (each keyed by a run-id, with a lease so only one agent drives each) can run concurrently in one repo. Drives its own loop via ScheduleWakeup — invoke once, no /loop wrapper. Campaign never writes fixes from scratch; to find issues first use gauntlet:review. Args: [--run id] [--new] [#PR ...]
---

# Campaign

Self-looping, reactive PR-review-to-merge pipeline.

Claude Code is orchestrator + gatekeeper. The **adversarial reviewer** is a selectable role: by
default Claude's own subagents (no external tool required); use the user's preferred reviewer when one
is set (explicit invocation, or a preference in memory/`CLAUDE.md`/carryover). A reviewer running a
different agent/model than the orchestrator (e.g. Codex CLI) is recommended for stronger reviewer
diversity but never required — see `references/reviewer.md`. Reviews and CI watches run as background
tasks; gates and merges stay centralized. Campaign gates **existing** PRs; it never writes fixes from
scratch — to find issues first, use `gauntlet:review`.

Invoke once. This skill drives its own loop via `ScheduleWakeup`; do NOT wrap it in `/loop`.

## Args

`/gauntlet:campaign [--run <id>] [--new] [#PR ...]`

- `#12` / `12` / `#12 #15` -> adopt those existing PRs into a run; gate + merge them.
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
events (see `references/stage-2-review-gate.md`).

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
- **Review gate is tier-dependent:** `required(tier)` fresh, context-isolated `SATISFIED` verdicts on
  same live PR content + green CI — **1 if TRIVIAL, else 2** (any code/agent-doc/sensitive change is 2).
- **Sequential same-PR reviews:** launch review 2 only after review 1 is `SATISFIED` (TRIVIAL needs
  only one).
- **Progress ledger:** reviewer progress means planned unit `done` or accepted amendment, not vague
  output.
- **No green by watch exit:** derive CI from re-polled `gh pr checks` snapshot.
- **Public API changes require user confirmation** unless ledger `api_changes: allowed`.

## Wake Skeleton

1. **Resolve run + lease;** adopt only absent/stale lease, stand down if fresh different owner.
2. **Adopt `#PR` args + reconcile run-labelled PRs.** For each explicit `#PR`, adopt it per
   `references/pr-adoption.md` (refresh existing row on re-adoption, never duplicate); then reconcile
   every PR carrying this run's `gauntlet-run-<run-id>` label from a batched snapshot. Treat `state.md`
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
  path; never `git add -A`/`git add .`. Ensure `.gauntlet/` is git-ignored (add it if missing).
- NEVER `rm -rf .gauntlet/`; only `.gauntlet/tmp/**` is disposable — the rest is carryover history.
