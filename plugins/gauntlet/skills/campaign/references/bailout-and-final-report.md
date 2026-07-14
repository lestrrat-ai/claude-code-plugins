## Bailout

- **1-hour cap per task** — one hour of wall-clock since `started` without merging. The cap catches a
  *stuck* task, not a slow external system, and the ledger records no separately-metered work time — so
  key it off recorded row state, not a running subtraction of durations nothing stores: **do not fire
  the cap on a wake where the row is blocked on an external wait** — `status == awaiting-api` (parked
  for user approval) or `status == awaiting-user` (parked for the user to adjudicate a review-finding
  standoff), or `ci == pending` for the current `head_sha` (CI still running). Only a wake
  where `started` is over an hour old *and* the row is agent-controlled (in none of those waits) trips it.
  When it trips, abort cleanly and **retry once against the SAME adopted PR** (`attempts` += 1, reset
  `started`). The PR is user/externally owned — campaign never closes it and opens a replacement of
  its own. Instead, **rebuild the worktree from the PR's head branch** so the retry runs with fresh
  LOCAL state against that same PR / its head; the PR itself is left in place.
- On the **second** stuck/failure, abort permanently: stop work on that PR but **leave the PR OPEN** —
  the adopted PR may be user/externally owned, so closing it is destructive and contradicts "set aside
  and move on." Instead, **remove THIS run's owner label (`gauntlet-run-<run-id>`) and its status
  label** from the PR, write `<rundir>/abort-<id>.md` with the full history (reviews, CI failures,
  diffs, what blocked it), set the ledger row `status = aborted`, and **continue the other PRs**. Only
  ever touch this run's own PRs (ownership = the `gauntlet-run-<run-id>` label). **Terminal detection
  relies on label removal, not closure**: loop control gates the finished-run branch on "no open PR
  still carrying this run's label", so once this run's owner label is removed the still-open PR no
  longer carries it and can no longer block terminal exit — an aborted row is terminal and lacks its
  `required(tier)` SATISFIED verdicts, so reconcile will never merge or keep driving it, and the
  un-labelled open PR is simply left for its owner.
- **Converging-but-expensively → escalate to the root-cause pass.** The bailouts above catch a *stuck*
  task; this catches one that's *progressing by whack-a-mole*. A targeted per-finding fix is right for
  the **first** `NOT SATISFIED` on a PR, or for genuinely independent findings. But on the **second**
  `NOT SATISFIED` on the same PR, **stop targeted patching and run the §2a-deep root-cause pass** — map
  the whole space with a dedicated read-only mapper and fix at one chokepoint. This is a hard backstop:
  even if the archetype wasn't obvious on finding 1, the 2nd sibling finding forces the pass no later.

Other stop conditions — escalate rather than loop: a worktree won't build, the reviewer keeps
returning the same unactionable verdict, or CI fails identically after a fix attempt.

---

## Final report

When the loop exits, summarize:

- **Reviewer** — the ledger `reviewer` value the run used, plus any review pass where an external
  reviewer failed and fell back to Claude subagents (see "The reviewer").
- **Merged** — PR number + slug, one-line description, and tier.
- **Residual risk** — for each merged PR, each accepting SATISFIED pass's `RESIDUAL-RISK` line (the
  least-certain area it named — `required(tier)` lines, so two for a STANDARD/HIGH PR and one for a
  TRIVIAL PR), and a flag when two accepting passes name the same area. This is non-actionable,
  non-gating calibration metadata — a place a human might look, never a reopened finding (Stage 2a).
- **Aborted** — PR number + slug, why, pointer to `abort-<id>.md`.
- **Skipped (API-declined)** — any PR whose API-changing fix the user was asked about and declined,
  with the change each would have needed.
- Any worktrees left for inspection.

This run's durable carryover file (`.gauntlet/history/<run-id>.md`) is written on exit (Loop control
step 5), so the next fresh run inherits what this run's PRs came to. Its contents are **NOT** a copy of
the list above: the carryover file's schema is owned by `carryover.md` ("The carryover ledger") — write
the slots that owner defines, and take any change to them from there. The two lists overlap but are not
the same list, and the report must never be read as an enumeration of the carryover file.
