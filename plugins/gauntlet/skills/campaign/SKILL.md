---
name: campaign
description: >-
  Writes code and merges it. A self-looping adversarial-review-to-merge campaign: an adversarial reviewer sweeps a given area/topic (else the whole repo), findings are neutrally verified, each survivor becomes its own PR, and a per-PR review gauntlet (two fresh, context-isolated SATISFIED verdicts on the same PR content, reviewed one at a time over the whole diff) plus event-driven CI monitoring gate the merge. Multiple isolated runs (each keyed by a run-id, with a lease so only one agent drives each) can run concurrently in one repo. Drives its own loop via ScheduleWakeup — invoke once, no /loop wrapper. For a report with no changes, use gauntlet:review instead. Args: [--run id] [area or topic]
---

# Campaign

Self-looping, reactive adversarial-review-to-merge pipeline.

Codex is adversarial reviewer. Claude Code is orchestrator + implementer. Fixes fan out in
parallel, but gates, CI watching, and merges stay centralized.

Invoke once. This skill drives its own loop via `ScheduleWakeup`; do NOT wrap it in `/loop`.

## Args

`/gauntlet:campaign [--run <id>] [--new] [area or topic]`

- Argument -> Codex reviews that area/topic.
- No argument -> whole-repo adversarial sweep, or resume sole orphaned run.
- `--run <id>` -> resume specific run; self-wakes also carry internal `--token`.
- `--new` or "fresh run" -> force independent new run with carryover.
- Do NOT ask user to confirm no-arg scope; no arg intentionally means whole repo.

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
| Fresh run, carryover, pruning old findings | `references/carryover.md` |
| Codex quota/auth/timeout/system error | `references/codex-fallback.md` |
| Stage 0 sweep / neutral verification | `references/stage-0-review-and-verify.md` |
| Implementing survivor fixes / opening PRs | `references/stage-1-fanout.md` |
| PR review gauntlet / progress ledger | `references/stage-2-review-gate.md` |
| Repeated sibling findings / shared root cause | `references/root-cause-pass.md` |
| CI watch, check polling, CI fix | `references/stage-2-ci.md` |
| Merge candidate / base refresh / cleanup | `references/stage-3-merge.md` |
| Stuck task, abort, final report | `references/bailout-and-final-report.md` |
| Rule lookup / uncertainty | `references/critical-rules.md` |

## Core Invariants

- **PR-first:** implement -> commit -> push -> open/update PR -> watch CI + review PR HEAD.
- **Work-conserving:** every wake reconciles, folds completions, launches all due work up to caps,
  drains still-ready PRs serially, then reschedules only when no useful action remains launchable.
- **Driver never blocks:** sweeps, verifications, reviews, and CI watches run as background tasks —
  completions are wakes. Stage 0 pipelines into fan-out (survivors fix while later shards sweep).
  A pending-CI PR always has a live watch; a review doomed by a pending content change is stopped,
  not awaited.
- **Run isolation:** touch only this run's `<rundir>`, ledger, labels, branches, PRs, and worktrees.
- **One active driver:** lease controls ownership; never double-drive one run.
- **Base branch is data:** read `base_branch` from ledger every wake; never assume `main`.
- **Two-review gate:** two fresh, context-isolated `SATISFIED` verdicts on same live PR content + green CI.
- **Sequential same-PR reviews:** launch review 2 only after review 1 is `SATISFIED`.
- **Progress ledger:** reviewer progress means planned unit `done` or accepted amendment, not vague
  output.
- **No green by watch exit:** derive CI from re-polled `gh pr checks` snapshot.
- **Public API changes require user confirmation** unless ledger `api_changes: allowed`.

## Wake Skeleton

1. Resolve run + lease; adopt only absent/stale lease, stand down if fresh different owner.
2. Reconcile state from git + GitHub; treat `state.md` as cache.
3. Fold completed sweep/verification/review/CI/fix tasks.
4. Launch all due work up to caps — sweep shards, verification chunks, fix fan-out, reviews, CI
   watches/fixes, base refresh; stop in-flight reviews doomed by a content change.
5. Merge ready PRs one at a time until no candidate remains immediately ready after base refresh.
6. **Launch audit — before sleeping, verify every due launch actually happened.** Re-run step 4's dispatch scan across both concurrency pools (fix subagents and review passes each have their own cap): confirm every ready sweep shard, verification chunk, fix, and review pass was launched, a CI watch is live for every pending-CI PR, and — whenever any non-terminal work remains — a `ScheduleWakeup` heartbeat is actually scheduled. If any due launch or the heartbeat is missing, launch it and re-audit. NEVER sleep with due work un-launched or the heartbeat unscheduled.
7. Update carryover/final state if terminal; otherwise refresh lease and return (step 6 has already ensured the heartbeat is scheduled).

## Critical Rules

- NEVER ask scope confirmation for no-arg invocation.
- NEVER review unpublished local work.
- NEVER spend review over open Copilot items, red checks, or conflicts.
- NEVER pass destructive instructions to `codex exec`.
- NEVER use `--dangerously-bypass-approvals-and-sandbox`; use `--sandbox workspace-write`.
- NEVER force-push/reset/delete outside explicit stage procedure and run scope.
- NEVER touch another run's PR/branch/worktree.
- NEVER merge over red/pending CI or stale review verdicts.
- NEVER add "Test plan" section to PR bodies.
- NEVER commit run/scratch files (the whole `.gauntlet/**` tree) — they are
  driver bookkeeping, not repo content. Stage only the specific source files a fix touches, by explicit
  path; never `git add -A`/`git add .`. Ensure `.gauntlet/` is git-ignored (add it if missing).
- NEVER `rm -rf .gauntlet/`; only `.gauntlet/tmp/**` is disposable — the rest is carryover history.
