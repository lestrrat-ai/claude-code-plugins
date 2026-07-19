## Bailout

### 1-hour cap per task

When it trips, abort cleanly and **retry once against the SAME adopted PR** (`attempts` += 1, reset
`started`). The PR is user/externally owned — campaign never closes it and opens a replacement of
its own. Instead, **rebuild the worktree from the PR's head branch** so the retry runs with fresh
LOCAL state against that same PR / its head; the PR itself is left in place.

- **1-hour cap per task** — one hour of wall-clock since `started` without merging. The cap catches a
  *stuck* task, not a slow external system, and the ledger records no separately-metered work time — so
  key it off recorded row state, not a running subtraction of durations nothing stores: **do not fire
  the cap on a heartbeat where the row is in a BOUNDED wait.** There are exactly three kinds, and **none is
  a `ci` value**:
  - **a PARK** — `status == awaiting-api` (parked for user approval) or `status == awaiting-user` (parked
    for the user to adjudicate a **review standoff** or a **machine blocker** — `files-and-ledger.md`,
    `status`). Its exit is **declared**: the user's answer (`approved`/`declined`, a standoff ruling, a
    `blocker_ruling` of `retry`/`abort`).
  - **a REPAIR — `status == repairing`** (`repair-pass.md`). The PR has reached a review-loop cap and is
    being reassessed and repaired. It is **bounded by `repair_count`/`REPAIR_CAP`**, and its exit is
    **declared**: the reassessment's decision, and — at the cap — **ABORT**, which lands on this very
    procedure. **Firing the 1-hour cap here would PRE-EMPT the repair the cap exists to reach**, aborting a
    PR that the mechanism was in the middle of saving — the same mistake as firing it inside a live CI
    liveness bound, for the same reason. A repairing PR always terminates: it repairs, or it aborts.
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
  whose fix never lands), where nothing else is counting. Only a heartbeat where
  `started` is over an hour old *and* the row is agent-controlled — **it is in NONE of the three bounded
  waits above**: not parked, not `repairing`, and **NO** bound of the CI owner's set live for it (never a
  count of them, which rots the moment a set gains a member) — trips it.
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
- **Not converging → the REASSESSMENT PASS takes over. `repair-pass.md` owns this, and it SUPERSEDES the
  rule that used to live here.** The bailouts above catch a *stuck* task; this catches one that is
  *progressing by whack-a-mole* — a loop that produces a real finding and a real fix every single round and
  never finishes.

  **The rule this replaces was "stop targeted patching on the 2nd `NOT SATISFIED` and run the root-cause
  pass", and it NEVER FIRED — not once, across 35 review rounds on two PRs.** It was not skipped in bad
  faith: it was **unevaluable**. Its trigger is a fact about *history* ("the second `NOT SATISFIED` on this
  PR"), the ledger recorded no history, and every heartbeat is a fresh agent instance holding exactly one
  finding. It called itself a hard backstop; it was a hard backstop **with no sensor**. Do not restore it.

  What replaces it is a **counter with a cap**, on disk, evaluated by the tool that records the verdict:
  `ledger.py verdict` bumps `review_rounds` / `ns_streak`, and at a cap it sets `status = repairing` and
  **exits non-zero**. The driver then hands the PR's **whole history at once** to a context-isolated
  reassessment pass, which returns ONE decision — **RESCOPE / REPAIR-INTENT / DEMOTE / ROOT-CAUSE /
  ABORT** — and the driver executes it **without asking the user**. **A cap is a MODE SWITCH, not a
  doorbell.** The root-cause pass is still exactly the right answer when the findings share one cause —
  it is now **one of five decisions an agent that can see all the rounds gets to choose between**, rather
  than a rule nothing could trigger.

  **ABORT is the only decision that ends the PR, and it lands on the procedure below** (leave the PR OPEN,
  drop this run's labels, write `abort-<id>.md`) — reused, not reinvented. A **second failed repair**
  aborts rather than looping (`repair_count`, capped): the mechanism that fixes non-convergence must not
  itself fail to converge.

  **THIS BACKSTOP HAD NO SENSOR — that is why it never fired** (the full account is above: its trigger was
  a fact about *history* the ledger did not record, and each fresh-agent heartbeat held a single round, so
  one PR ran **21 review rounds** underneath it).

  The ledger now records that history in durable state — `ns_streak` and `review_rounds` — evaluated by
  `ledger.py verdict` itself as it records each verdict, and at a cap it holds the PR `repairing` and exits
  non-zero (`files-and-ledger.md` for the counters and the design rationale).

Other stop conditions — escalate rather than loop: a worktree won't build, the reviewer keeps
returning the same unactionable verdict, or CI fails identically after a fix attempt. **Note what is NOT
on that list: a reviewer that is RIGHT every time and still never lets the PR through.** Those conditions
all describe a **broken** reviewer, and the reviewer that ran 21 rounds was **working perfectly** — that is
exactly what made it lethal. That case is the reassessment pass's, above, and no rule here catches it.

---

## Final report

When the loop exits, summarize:

- **Reviewer** — the ledger `reviewer` value the run used, plus any review pass where an external
  reviewer failed and fell back to the active host's native workers (see "The reviewer").
- **Which rules actually ran** — the ledger header's `skill_version`. **State it every time.** The harness
  loads this skill from the **installed plugin cache**, so a merged, version-bumped rule governs **nothing**
  until that cache refreshes — and one did not, for days, while every report in that window said "reviewer:
  codex" and could not say "rules: v0.1.2, which is two commits behind the rule you think is protecting
  you". If it is `unknown`, say so plainly: the run did not record which copy of the gate judged it.
- **Review rounds** — for each PR, its `review_rounds` (and `ns_streak` if non-zero). A PR that took many
  rounds to pass is not the same as one that passed cleanly, and until this counter existed the report
  could not tell them apart — the difference was visible only to a human holding every round in one context,
  which is what it took to stop a 21-round loop.
- **Whose intent the review was measured against** — name every PR whose `intent` is `authored@…` rather
  than `stated@…`. **An `authored` intent is the DRIVER'S CLAIM about what the PR is for**, not the
  author's, and a wrong one silently **narrows** a review. It is a real cost and it is disclosed here rather
  than buried; a `stated@…` intent came from the PR body and needs no flag.
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
- **Repaired** — every PR that reached a review-loop cap: its `review_rounds`, the decision the
  reassessment pass returned, and a pointer to `repair-<pr>-<k>.md` (`repair-pass.md`). **Report a DEMOTE's
  demoted findings explicitly** — they are true findings that were deliberately **not fixed**, and burying
  that is how a report becomes a false claim of cleanliness. This is the run telling the user, in the one
  place they will read, that it stopped whacking moles and what it did instead.
- **Aborted** — PR number + slug, why, pointer to `abort-<id>.md`.
- **Skipped (API-declined)** — any PR whose API-changing fix the user was asked about and declined,
  with the change each would have needed.
- **Follow-ups** — whenever the store holds an open entry, the `followups.py … table` output
  (`followups.md`): the work this run FOUND and deliberately did not do. A `candidate` is a claim the user
  has not agreed to and the driver has not investigated — so **surface it and ASK**. What the run may do
  with one instead of asking is the **autonomy threshold** (`followups.md`), and **PUBLISHING is never on
  that list**: no issue, no release, without the user's agreement on that specific item. A defect a bailout
  exposed and this run will not fix is recorded in the store **before** the report is written, not
  announced only in it — a follow-up that exists only in this report dies with it.
- Any worktrees left for inspection.

This run's durable carryover file (`.gauntlet/history/<run-id>.md`) is written on exit by
`carryover.py distill` (Loop control step 5), so the next fresh run inherits what this run's PRs came to.
Its contents are **NOT** a copy of the list above: the carryover file is a mechanical projection of the
terminal ledger, owned by `carryover.md` ("The carryover ledger") — take any change to its slots from
there. The two lists overlap but are not the same list, and the report must never be read as an
enumeration of the carryover file.
