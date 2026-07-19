## Stage 3 — Merge (serialized, auto)

A PR is mergeable when it is **NOT parked** AND the **live PR head SHA** —
`gh pr view <pr> --json headRefOid --jq .headRefOid`, keyed by the PR number from the ledger row —
equals the ledger `head_sha` AND `reviews_ok >= required(tier)` AND `ci == green` — i.e.
`required(tier)` SATISFIED verdicts (1 if `tier == TRIVIAL`, else 2) and green CI all recorded
against the live tip. (An adopted PR may have no local worktree checked out, so use the PR's own head
via `gh`, never a local `git rev-parse HEAD`.)

**The held-status guard binds the merge (`loop-control.md` step 3).** A **HELD** PR — `ledger.py …
dispatch-check --pr <N>` exits non-zero for it — is **NEVER merged**, whatever `reviews_ok` / `ci` /
`mergeable` say. That covers a PR **parked on a HUMAN** (`awaiting-user`, `awaiting-api`) and a PR that has
stopped converging and is being **`repairing`**-ed (`repair-pass.md`); `HELD_STATUSES` in
`scripts/ledger.py` is the one enumeration, so **do not retype it here**. Merge eligibility is **not**
derived from the gate counters alone — being held does not lower `reviews_ok`, so a rule that reads only
the counters would merge a PR whose disputed finding or API change the user has not yet ruled on, or one
whose diff the reassessment pass is in the middle of rescoping. For a park, only the user's answer unparks
it, and **to the `status` that
answer dictates** — `in_review` for a **resume** answer; terminal `aborted` for a **terminal** one (a
`declined` API change, a `blocker_ruling` of `abort`), which never returns to `in_review` and is never
merged (`loop-control.md` step 3, "Only the user's answer unparks a PR", owns the mapping). Until the
answer lands the PR is skipped, never merged.

### The merge precondition — TWO enums, and NEITHER of them is a CI signal

**`mergeStateStatus` NEVER feeds `ci`.** It is a **merge precondition**, read at Stage 3 and nowhere
else. Campaign's own SHA-pinned snapshot (`stage-2-ci.md`) is the **only** source of `ci`. Crossing these
two wires is what turns a blocked merge into an infinite CI watch.

**The merge-readiness decision is COMPUTED, never read by eye:**
`python3 <skill-dir>/scripts/merge-check.py check --pr <N> --file <state.jsonl>`. It reads the ledger
row + the live PR view (`gh pr view <pr> --json mergeable,mergeStateStatus,isDraft,state,headRefOid`) and
prints `{"verdict":"merge"|"not-yet","reason":…}`, crossing — in ONE place — the held/open/draft/
stale-head/ci/reviews preconditions and then **BOTH** GitHub enums (`.mergeable` first — `CONFLICTING`
and `UNKNOWN` decide on their own, `MERGEABLE` falls through — then `.mergeStateStatus`, which alone
yields `merge`), so the miscross above cannot recur. Both enums are crossed **TOTALLY**: a value GitHub's
schema does not declare **parks**, never guesses. Act on the verdict:

- `merge` → proceed to the merge steps below (step 1).
- `not-yet <reason>` → do **NOT** merge; the reason names the block. Route on the reason's **action**
  (the phrase the tool emits), never on a hand-copied list of enum values — a value the tool newly parks
  then routes correctly with no edit here:
  - `rebase` reasons (base moved ahead / conflicts) → refresh the PR per step 6.
  - the tool's **`— park`** reasons (any `not-yet` reason that ends in `— park` / `park awaiting-user`)
    → park and name the blocker (below). This is the tool's catch-all for a merge GitHub blocks for a
    cause campaign cannot clear itself — a draft, a `BLOCKED` merge state, **and any value neither enum
    recognizes** — so routing on the `— park` action, not a fixed value list, keeps this bucket total.
  - `re-poll` reasons (merge state / mergeability not computed yet — `UNKNOWN`) → the **UNKNOWN re-poll
    bound** (below).
  - Everything else (`ci is …`, `N of M approvals`, `held`, stale head) → leave the PR; the next
    heartbeat re-evaluates once that precondition changes.

The mapping the tool crosses is **OWNED by `merge-check.py`** and pinned by its sibling fixtures
(`merge-check-test.py`), which assert every enum value's verdict — that is what proves the mapping, not a
table restated here for a reader to map by eye.

**The UNKNOWN re-poll bound.** A `not-yet` whose reason is a **`re-poll`** (`.mergeStateStatus` or
`.mergeable` = `UNKNOWN` — a value GitHub has **not computed yet**) is not a verdict, and it resolves
within seconds once GitHub finishes computing mergeability lazily. Re-poll it **in-heartbeat up
to 3 times**, with a short backoff between re-polls (a few seconds) — the initial Stage-3 fetch that
returned `UNKNOWN` is what triggers this loop and is **not** one of the three. If it is **still** `UNKNOWN`
after the third re-poll, do **NOT** merge on this heartbeat — leave the PR and let the **next heartbeat** re-evaluate it: **the
heartbeat is the backoff** (`stage-2-ci.md`, "The HEARTBEAT is the backoff — never tight-loop inside one"). A value
that stays `UNKNOWN` across heartbeats is bounded by the heartbeat cadence, so **no persisted counter is needed** —
the in-heartbeat cap is a fixed 3, and the coarse retry is the heartbeat loop itself. Never read `UNKNOWN` as
`MERGEABLE`, and never let a perpetually-`UNKNOWN` PR either merge or wedge.

**EVERY `awaiting-user` park a `not-yet` verdict names is a MACHINE-BLOCKER park, and it MUST declare its exit** — a
park whose exit event never comes is the same wedge it was meant to prevent. Run **`ledger.py … park --pr
<N> --reason <the blocker>`** — the sanctioned writer of a non-CI machine-blocker park (`stage-2-ci.md`,
"ESCALATE"). It sets `status = awaiting-user`, `ci_reason` = the blocker **named** (the draft state,
`BLOCKED`, or the unrecognized value verbatim), and `blocker_ruling = -` in ONE atomic write (park entry
spends nothing and answers nothing — a ruling already on the row belongs to a **previous** park;
`stage-2-ci.md`, "THE RULING IS CONSUMED EXACTLY ONCE"), and it refuses a blank reason, a terminal row, and
a second park over an open question. It is then resolved through `blocker_ruling` = `retry` / `abort` — the
user marks the PR ready, clears the protection, or gives up, and answers. The record and the unpark are
defined once, in `files-and-ledger.md` (`status`) and `loop-control.md` step 3, "Only the user's answer
unparks a PR"; never invent a second mechanism here.

#### `BLOCKED` and `UNSTABLE` — what each merge state means

**`BLOCKED` does NOT mean "a required check is missing or failing."** It means the merge is blocked **for
any reason** — including a **draft** PR, or one **awaiting a human approving review**, or a ruleset
campaign cannot read. Verified: `cli/cli` PR #13856 reads `BLOCKED` with `mergeable = MERGEABLE` **and a
fully `SUCCESS` rollup**, purely because it is a draft. Mapping `BLOCKED` → `ci = pending` → "relaunch the
CI watch" therefore **LIVELOCKS**: the CI is already green, no CI event will ever fire, campaign never
approves PRs and never asks the user to — so it watches a settled PR forever. **Park it and name the
blocker instead.**

**`UNSTABLE` means non-*passing*, which includes *pending*.** Treating it as red would dispatch a CI-fix
subagent at a check that is merely **still running**.

1. **Serialize merge operations, not heartbeats.** A heartbeat may merge multiple PRs, but only one at a time.
   Before each merge, re-confirm the PR is still **not parked** and that both gates still hold against the live PR head SHA
   (`gh pr view <pr> --json headRefOid --jq .headRefOid`, PR number from the ledger row) — a late push
   may have moved the tip past the recorded `head_sha` and reset the gates —
   **and re-fetch `origin/<base>` and re-check
   `gh pr view <pr> --json mergeable,mergeStateStatus,isDraft`** — a concurrent run sharing this base may
   have advanced it since the PR was last reviewed. Feed the ledger row and that live view to
   **`merge-check.py check`** (**"The merge precondition"** above), which crosses **every** value of both
   enums and returns the verdict.
2. Push guard: `gh pr view <pr> --json state --jq .state` (PR number from the ledger row) must be
   `OPEN`.
3. Merge — always `gh pr merge <pr> --squash` (use the repo's prevailing merge method if not squash),
   with **NO `--delete-branch`**. Campaign never deletes the adopted PR's **remote** head branch: an
   adopted PR keeps its own branch (campaign did not create it), so campaign leaves the remote branch
   alone. If the repo has "Automatically delete head branches" enabled, GitHub deletes it on merge;
   otherwise it remains — either way that is the repo setting's doing, not campaign's action. Local
   cleanup (step 5) is a separate concern, keyed only off the per-PR `worktree_owned`/`branch_owned`
   flags.
4. **Sync the local base branch with the remote.** The merge landed on `origin/<base>`, but local
   `<base>` is now behind. Fast-forward it so every subsequent rebase and `origin/<base>...HEAD` diff is
   measured against the just-merged tip, not a stale one (`<base>` = the adopted PRs' `baseRefName`,
   not assumed `main`).
   Local `<base>` is **shared** with any concurrent run on the same base; the fast-forward is
   idempotent, so if another run already advanced it, a no-op "already up to date" is fine — just never
   force it.

   **Run the fast-forward from wherever `<base>` is actually checked out** — don't assume it's the
   root checkout. Use the invocation's single `RepositoryContext` and the typed process boundary from
   `runtime-adapter.md`. A branch can be checked out in at most one working tree, so first locate that
   tree (`git worktree list` shows the branch per path; the root package counts as one), then
   fast-forward there. If `<base>` is checked out **nowhere**, update the ref directly instead — a plain
   `fetch` into the local branch (this form is refused while the branch is checked out, which is why
   it's the no-working-tree case):

   ```text
   # Always refresh origin/<base>, even with no local branch or configured upstream.
   run_argv(
     argv: ["git", "fetch", "origin",
            concat("refs/heads/", base, ":refs/remotes/origin/", base)],
     cwd: repository.project_root, stdin_file: null, stdout_file: null
   )

   # case A — <base> is checked out in discovered absolute worktree <dir>:
   run_argv(
     argv: ["git", "merge", "--ff-only", concat("origin/", base)],
     cwd: dir, stdin_file: null, stdout_file: null
   )

   # case B — <base> is checked out in no working tree:
   run_argv(
     argv: ["git", "fetch", "origin", concat(base, ":", base)],
     cwd: repository.project_root, stdin_file: null, stdout_file: null
   )
   ```

   Fast-forward only — never a merge commit or reset. If the fast-forward fails (local `<base>` somehow
   diverged), do NOT force it: that's a bailout condition (stop and surface it), since rebasing PRs
   onto a wrong base would corrupt every downstream diff.
5. **Clean up on successful merge.** Once the merge is confirmed (`gh pr view <pr> --json state
   --jq .state` → `MERGED`, PR number from the ledger row), tear down the local footprint. `<branch>`
   and its worktree are the adopted PR's **own head branch** and the worktree recorded in that PR's
   ledger row (its `branch`/`worktree` columns) — there is no `fix-<run-id>-*` branch to clean up.

   **The remote head branch is not campaign's concern** — step 3 never deletes it; the repo's
   "Automatically delete head branches" setting governs whether GitHub removes it on merge.

   **Local cleanup is gated on the per-PR `worktree_owned`/`branch_owned` flags.** Adoption records the two
   independently (campaign can create a worktree over a **pre-existing** local branch, in which case
   `worktree_owned = yes` but `branch_owned = no`; see "PR adoption"). **Never remove a worktree or
   delete a branch campaign didn't create:**
   - **`worktree_owned = yes`** → campaign created the worktree, so remove it: verify the merge with
     the `git-detect-merged` skill **against the run's `<base>`** (the ledger `base_branch` — NOT the
     helpers' default `main`, since the base may be a release/integration branch), then `git worktree
     remove` the **ledger-recorded worktree**. **`worktree_owned = no`** → campaign reused a
     pre-existing checkout (the user's root/main checkout or their own worktree), so **leave the
     worktree in place** — do NOT `git worktree remove` it.
   - **NEVER `git worktree remove` the root/main checkout** (the repo's primary working tree) under any
     circumstances — even were it somehow flagged owned. Removing/replacing the main checkout is
     destructive and, for the primary working tree, impossible; `worktree_owned = yes` only ever names a
     repository-context-derived worktree campaign itself created via `git worktree add`.
   - **`branch_owned = yes`** → campaign created the local branch (the `-b` path), so delete it (e.g.
     via `git-cleanup-merged` **with that same `<base>`**, or `git branch -d` the ledger-recorded
     `branch` after the worktree is gone). **`branch_owned = no`** → campaign reused a pre-existing
     local branch (or checkout), so **leave the local branch in place** — do NOT delete it; that branch
     may be the user's.
   - **Report** any reused worktree and any reused local branch that were left in place (path + branch)
     in the final report, so the user knows their working tree/branches were untouched.

   Local ref safety (`worktree_owned`/`branch_owned`, never touching the root/main checkout or any
   reused ref) is absolute. Once cleanup is done set status `merged` (via
   `scripts/ledger.py … set --pr <N> --status merged`, by field name — the schema-owning accessor,
   `files-and-ledger.md`; never hand-edit the row by column position) and stop the PR's background
   tasks.

   This runs only after the merge is verified, and only ever touches PRs this run **owns** — those
   carrying its `gauntlet-run-<run-id>` label — never another run's. Leave the worktree in place if the
   merge cannot be confirmed — treat that as a bailout condition, not a cleanup.
6. After each merge+sync+cleanup, reconcile other open PRs (write any `reviews_ok`/`head_sha`/`ci`
   change below through `scripts/ledger.py … set --pr <N> --<field> <val>` by field name, never by
   hand-editing the row by column position).

   **SKIP HELD PRs FIRST — before any base refresh, rebase, or conflict handling.** A **HELD** PR
   (`ledger.py … dispatch-check --pr <N>` — parked on a human, or `repairing`) is **FROZEN**
   (`loop-control.md` step 3,
   "held-status guard"): this reconcile MUTATES a PR, so it is exactly what the guard forbids. A clean
   rebase would move its `head_sha`, set `ci = pending` and — at that head write — the accessor would reset
   its liveness counters (`stage-2-ci.md`, "THE LIVENESS COUNTERS"); a conflict-resolving rebase would reset
   `reviews_ok`, relabel, and relaunch work — and would **change the PR's content**, which can invalidate
   the very refutation or API change the user was parked to adjudicate. **A parked PR that has fallen
   behind simply STAYS behind** until the user answers; it is re-reconciled normally on the heartbeat after it
   unparks. **Do NOT drop its row** — it stays in the run, and the park **does not change its CI watch
   either way** (observation, not mutation): the watch follows the normal policy (`stage-2-ci.md`, "WATCH
   ONLY WHAT CAN MOVE") — relaunched while a row is still RUNNING, **not** relaunched once CI has settled.

   For each **non-parked** open PR: **base advancement alone does NOT
   invalidate gauntlet reviews.** Rebase only if GitHub flags the PR behind/conflicting:
   - Clean rebase (no conflicts, PR diff unchanged) → **EXECUTED — not hand-run — by `python3
     scripts/clean-rebase.py run --ledger <state.jsonl> --pr <N> --worktree <worktree> --base <base>`**: it
     does the fetch/rebase/`--force-with-lease` push, verifies the PR's own diff is unchanged, and writes the
     one ledger reset — keep `reviews_ok`, **keep its status label as-is** (the gate did not reset, so an
     accepted PR stays `gauntlet-accepted`), new `head_sha` written through the accessor (which **resets the
     liveness counters** at the door — new commit, new evidence — `stage-2-ci.md`, "THE LIVENESS
     COUNTERS"), `ci = pending`. **Exit 3 means it was NOT
     clean** — a conflict, or a rebase that changed the PR's own diff — and it has already aborted/reset to
     the original head; the conflict-resolution bullet below then owns it. On a clean (exit 0) rebase,
     **re-derive CI from a snapshot of the new tip in the same heartbeat, launching a watch only if `liveness`
     then reports `watch_warranted`** ("WATCH ONLY WHAT CAN MOVE"). A rebased PR must not sit unwatched
     until the heartbeat while its checks are running — but it must not be watched when **nothing** is
     running either, which right after a push is the common case (no check has registered yet). CI must
     return green before merging.
   - Rebase requiring conflict resolution → PR content changed → **reset `reviews_ok` to 0 AND, in that
     same step, restore `gauntlet-reviewing` if the PR carries `gauntlet-accepted`** — the gate and its
     label move together (`stage-2-review-gate.md`, "Status labels mirror the review gate"). Update
     `head_sha` to the
     new tip through the accessor, which **resets the liveness counters** (a new head is new evidence — `stage-2-ci.md`, "THE
     LIVENESS COUNTERS"; the clean-rebase branch above does the same, and this branch is no different in
     that respect). Then re-derive CI for
     the new tip — watching it only if `liveness` reports `watch_warranted` ("WATCH ONLY WHAT CAN MOVE") — and re-enter
     Stage 2.
   - Still open, **not parked**, mergeable, not behind/dirty/conflicting, same live `head_sha`,
     `reviews_ok >= required(tier)`, and `ci == green` → still immediately mergeable; return to step 1
     in the same heartbeat.

Stop the merge loop only when no remaining PR is immediately mergeable after the latest base refresh.

---
