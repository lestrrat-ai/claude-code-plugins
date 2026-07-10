## Bailout

- **1-hour cap per task** — one hour of wall-clock since `started` without merging. The cap catches a
  *stuck* task, not a slow external system, and the ledger records no separately-metered work time — so
  key it off recorded row state, not a running subtraction of durations nothing stores: **do not fire
  the cap on a wake where the row is blocked on an external wait** — `status == awaiting-api` (parked
  for user approval), or `ci == pending` for the current `head_sha` (CI still running). Only a wake
  where `started` is over an hour old *and* the row is agent-controlled (not in either wait) trips it.
  When it trips, abort cleanly and **retry once** from a fresh worktree (`attempts` += 1, reset
  `started`). **Supersede the prior attempt's PR** — close it noting the supersession — so a stale
  open PR doesn't linger; the retry opens its own.
- On the **second** stuck/failure, abort permanently: stop work on that finding, **close its open PR
  and remove its gate labels**, write `<rundir>/abort-<id>.md` with the full history (reviews, CI
  failures, diffs, what blocked it), set status `aborted`, and **continue the other findings**. Only
  ever touch this run's own PRs. **Terminal detection requires the closure**: loop control gates the
  finished-run branch on "no open `fix-<run-id>-*` PR", and a PR-first pipeline almost always left one
  open — an aborted row is terminal and lacks two SATISFIED verdicts, so reconcile will never merge or
  keep driving it, but a dangling open PR would block terminal exit and heartbeat forever.
- **Converging-but-expensively → escalate to the root-cause pass.** The bailouts above catch a *stuck*
  task; this catches one that's *progressing by whack-a-mole*. A targeted per-finding fix is right for
  the **first** `NOT SATISFIED` on a PR, or for genuinely independent findings. But on the **second**
  `NOT SATISFIED` on the same PR, **stop targeted patching and run the §2a-deep root-cause pass** — map
  the whole space with a dedicated read-only mapper and fix at one chokepoint. This is a hard backstop:
  even if the archetype wasn't obvious on finding 1, the 2nd sibling finding forces the pass no later.

Other stop conditions — escalate rather than loop: a worktree won't build, codex keeps returning the
same unactionable verdict, or CI fails identically after a fix attempt.

---

## Final report

When the loop exits, summarize:

- **Merged** — finding id, PR number, one-line fix.
- **Residual risk** — for each merged PR, both accepting SATISFIED passes' `RESIDUAL-RISK` lines (the
  least-certain area each named), and a flag when the two name the same area. This is non-actionable,
  non-gating calibration metadata — a place a human might look, never a reopened finding (Stage 2a).
- **Aborted** — finding id, why, pointer to `abort-<id>.md`.
- **Skipped** — REFUTED findings, UNCERTAIN ones the user should triage, and any API-changing fix the
  user was asked about and declined (with the change each would have needed).
- Any worktrees left for inspection.

The same outcomes are written to this run's durable carryover file
(`.gauntlet/history/<run-id>.md`) on exit (Loop control step 5), so the next fresh run inherits them.
