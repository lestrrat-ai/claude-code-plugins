## Bailout

- **1-hour cap per task** — one hour of wall-clock since `started` without merging. The cap catches a
  *stuck* task, not a slow external system, and the ledger records no separately-metered work time — so
  key it off recorded row state, not a running subtraction of durations nothing stores: **do not fire
  the cap on a wake where the row is in a BOUNDED wait.** There are exactly two kinds, and **neither is
  a `ci` value**:
  - **a PARK** — `status == awaiting-api` (parked for user approval) or `status == awaiting-user` (parked
    for the user to adjudicate a **review standoff** or a **machine blocker** — `files-and-ledger.md`,
    `status`). Its exit is **declared**: the user's answer (`approved`/`declined`, a standoff ruling, a
    `blocker_ruling` of `retry`/`abort`).
  - **a LIVE Stage 2 CI LIVENESS BOUND, still below its cap.** **Read the bounds from their owner —
    `stage-2-ci.md`, "THE LIVENESS COUNTERS", which is the ONE enumeration of the bounded CI waits and
    names each one's cap. NEVER re-list them here, and NEVER key this exemption on a `ci` value.** A bound
    added to that set must exempt this cap **with no edit to this line**. (A re-listed enumeration goes
    stale the moment a bound is added — it already did: this line once enumerated the bounds itself and
    named only **some** of them, so a resumed run whose `started` was already over an hour old could trip
    this cap while a bound was still **below** its cap, **aborting the PR before the bound could park it**.)

  **LIVE = the bound is actually COUNTING for this PR at this `head_sha`.** Test it against that owner's
  table, whose middle column ("Bounds the wait where…") *is* the liveness test: the bound whose row
  describes this PR's current CI state is the one that applies, and it is live while it has **not** reached
  the cap that row names. Two consequences the table states and this line must not contradict:
  - **A bound the MACHINE ACTION gate has STOPped is NOT live** (`stage-2-ci.md`, "MACHINE ACTION"): while
    a fix is **due or in flight** for this PR at this `head_sha`, the bounds do not accrue at all.
  - **A bound with no cap is still an answer — motion is not a wait.** A PR whose check set is genuinely
    **CHANGING** is progressing on its own, and is exempt while it does. It cannot use motion to sit
    forever: the moment it stops changing, a **capped** bound takes over (`stage-2-ci.md`).

  **A LIVE BOUND and AN AGENT AT WORK are DIFFERENT THINGS, and only the first exempts.** They are
  mutually exclusive by that MACHINE ACTION gate, which is what keeps this exemption from disabling the
  cap:
  - **A machine action due or in flight → no bound is counting → NOT exempt.** That row is
    **agent-controlled**: campaign's own fix is what has to land, and a task stuck in campaign's own work
    is exactly what this cap is for. **A red PR the driver is actively repairing is the paradigm case**,
    and it has never been exempt.
  - **No machine action, a bound counting below its cap → EXEMPT.** **Nobody is coming**, and the bound
    itself is the exit.

  **The exemption is the BOUND, not the `ci` value.** Keying it to `ci == pending` was wrong in **both**
  directions, which is why **no** `ci` value can express it. It **over-exempted**: a pending PR that has
  SETTLED is waiting on **nothing**, and a still-`RUNNING` row is no evidence that anything is coming (**a
  hung runner keeps a row `RUNNING` forever**) — blessing `pending` as "an external wait on its own terms"
  is what let a PR sit **forever** with the cap disabled and no one told. And it **under-exempted**: **the
  bounds are NOT confined to `pending`** (`stage-2-ci.md`, "THE LIVENESS COUNTERS" — the owner's table says
  which CI state each bound covers, and "surfaces as `pending`" is **NOT** the test), so a PR whose CI-fix
  budget is **spent** can be counting down toward a cap on a snapshot that is **not** pending, with no
  machine action coming — and the cap would abort it before its bound could park it. Only the counters know
  whether **anything is counting down**; `ci` does not. **An external wait must be a BOUNDED wait, or it is
  a wedge with a nicer name.**
  **Firing this cap inside a live bound would PRE-EMPT the park the bound exists to reach.** Every bound
  ends at **ESCALATE** (`stage-2-ci.md`), which parks the PR `awaiting-user` for a human to adjudicate —
  and that park is exempt for its own reason (a **declared exit**, above). So a PR waiting on a live CI
  bound is never aborted out from under it: the bound either resolves, or it reaches the human. This cap's
  job is the **agent-controlled** row — the task stuck in campaign's own work (a hung review, a red PR
  whose fix never lands), where nothing else is counting. Only a wake where
  `started` is over an hour old *and* the row is agent-controlled (**NO** bound of the owner's set is live
  for it — never a count of them, which rots the moment the set gains a member) trips it.
  When it trips, abort cleanly and **retry once against the SAME adopted PR** (`attempts` += 1, reset
  `started`). The PR is user/externally owned — campaign never closes it and opens a replacement of
  its own. Instead, **rebuild the worktree from the PR's head branch** so the retry runs with fresh
  LOCAL state against that same PR / its head; the PR itself is left in place.
- **The user's `abort` ruling on a parked PR takes this SAME path.** A `blocker_ruling = abort@<iso>`
  (`loop-control.md` step 3, "Only the user's answer unparks a PR") is a permanent abort of that PR, not a
  new mechanism: run exactly the procedure below, with the park's `ci_reason` as the recorded cause.
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
- **What the base branch REQUIRED** — the ledger's `required_set` (`stage-2-ci.md` owns its states). Report
  **which state the run was in**, because it is what every `green` in this report rests on: `declared:…` —
  name the checks that had to pass; `none` — the base branch required nothing, **read and confirmed**, not
  merely unobserved. **NEVER report `unknown` as "no required checks"**: it means campaign **could not
  read** them, nothing merged on it, and any PR that reached it **escalated** — say which read failed, so
  the user can fix the access rather than wonder why the run stalled.
- **Aborted** — PR number + slug, why, pointer to `abort-<id>.md`.
- **Skipped (API-declined)** — any PR whose API-changing fix the user was asked about and declined,
  with the change each would have needed.
- Any worktrees left for inspection.

This run's durable carryover file (`.gauntlet/history/<run-id>.md`) is written on exit (Loop control
step 5), so the next fresh run inherits what this run's PRs came to. Its contents are **NOT** a copy of
the list above: the carryover file's schema is owned by `carryover.md` ("The carryover ledger") — write
the slots that owner defines, and take any change to them from there. The two lists overlap but are not
the same list, and the report must never be read as an enumeration of the carryover file.
