## Stage 3 — Merge (serialized, auto)

A PR is mergeable when it is **NOT parked** AND the **live PR head SHA** —
`gh pr view <pr> --json headRefOid --jq .headRefOid`, keyed by the PR number from the ledger row —
equals the ledger `head_sha` AND `reviews_ok >= required(tier)` AND `ci == green` — i.e.
`required(tier)` SATISFIED verdicts (1 if `tier == TRIVIAL`, else 2) and green CI all recorded
against the live tip. (An adopted PR may have no local worktree checked out, so use the PR's own head
via `gh`, never a local `git rev-parse HEAD`.)

**The parked-status guard binds the merge (`loop-control.md` step 3).** A PR whose `status` is
`awaiting-user` or `awaiting-api` is parked on a HUMAN: **NEVER merge it**, whatever `reviews_ok` /
`ci` / `mergeable` say. Merge eligibility is **not** derived from the gate counters alone — a park does
not lower `reviews_ok`, so a rule that reads only the counters would merge a PR whose disputed finding
or API change the user has not yet ruled on. Only the user's answer unparks it, and **to the `status` that
answer dictates** — `in_review` for a **resume** answer; terminal `aborted` for a **terminal** one (a
`declined` API change, a `blocker_ruling` of `abort`), which never returns to `in_review` and is never
merged (`loop-control.md` step 3, "Only the user's answer unparks a PR", owns the mapping). Until the
answer lands the PR is skipped, never merged.

### The merge precondition — TWO enums, and NEITHER of them is a CI signal

**`mergeStateStatus` NEVER feeds `ci`.** It is a **merge precondition**, read at Stage 3 and nowhere
else. Campaign's own SHA-pinned snapshot (`stage-2-ci.md`) is the **only** source of `ci`. Crossing these
two wires is what turns a blocked merge into an infinite CI watch.

`gh pr view <pr> --json mergeable,mergeStateStatus,isDraft` returns **two different enums** — say which
field a value came from, and map **every** value of each (introspected from the schema, not recalled):

```
MergeableState (.mergeable)       MERGEABLE CONFLICTING UNKNOWN
MergeStateStatus (.mergeStateStatus)  DIRTY UNKNOWN BLOCKED BEHIND UNSTABLE HAS_HOOKS CLEAN
```

**The two enums answer DIFFERENT questions, and `.mergeable` NEVER answers the merge question.**
`.mergeable` says whether the branches **can be combined at all**; `.mergeStateStatus` says whether the
merge is **permitted right now**. So `MERGEABLE` is **NECESSARY BUT NOT SUFFICIENT** — a `MERGEABLE` PR
is routinely `BLOCKED`, `BEHIND` or `UNSTABLE`, and merging on `.mergeable` alone would merge straight
over a blocked, stale, or non-passing PR. **`MERGEABLE` does NOT mean "merge it"; only the
`.mergeStateStatus` rows below decide that.** The two are read together, never one instead of the other.

The table maps **BOTH enums TOTALLY** — every value of each has its own row, so the catch-all fires
**only** on a value GitHub has genuinely added since. A value with no row is a **wedge**: it falls to the
catch-all and parks a PR that nothing was wrong with.

| Field / value | Meaning | Do |
|---|---|---|
| `.isDraft = true` | a **draft** PR — GitHub blocks the merge regardless of CI | **NEVER merge.** Park `awaiting-user`. |
| `.mergeable = MERGEABLE` | the branches **can** be combined — **the ONLY non-blocking `.mergeable` value, and the one EVERY healthy PR carries** | **NOT a licence to merge.** The `.mergeable` precondition is satisfied and **nothing else is decided**: fall through to the `.mergeStateStatus` rows below, which decide whether the merge may actually run. |
| `.mergeable = CONFLICTING` | conflicts with the base | refresh the PR per step 6 |
| `.mergeable = UNKNOWN` | **not computed yet** (GitHub computes mergeability lazily) | **re-poll with backoff**, bounded; **never** read as a verdict |
| `.mergeStateStatus = CLEAN` | mergeable, everything green | **merge** |
| `.mergeStateStatus = HAS_HOOKS` | mergeable; the repo has pre-receive hooks | **merge** |
| `.mergeStateStatus = BEHIND` | base has moved ahead | refresh the PR per step 6 → gate reset |
| `.mergeStateStatus = DIRTY` | conflicts | refresh the PR per step 6 |
| `.mergeStateStatus = UNSTABLE` | a check is **non-passing** — which **INCLUDES STILL RUNNING** | **do not merge.** Do **NOT** touch `ci` and do **NOT** dispatch a CI-fix — campaign's own snapshot decides that. |
| `.mergeStateStatus = BLOCKED` | the merge is blocked — **cause NOT enumerable** | **do not merge.** Park `awaiting-user`. **NEVER** map it to `ci = pending`. |
| `.mergeStateStatus = UNKNOWN` | not computed yet | re-poll with backoff, bounded |
| **any other value** | GitHub added one | **park `awaiting-user`**, naming the value. Never guess. |

**EVERY `awaiting-user` park in this table is a MACHINE-BLOCKER park, and it MUST declare its exit** — a
park whose exit event never comes is the same wedge it was meant to prevent. So, in the same step: write
`ci_reason` **naming the blocker** (the draft state, `BLOCKED`, or the unrecognized value verbatim),
**clear `blocker_ruling` to `-`** (park entry spends nothing and answers nothing — a ruling already on the
row belongs to a **previous** park; `stage-2-ci.md`, "THE RULING IS CONSUMED EXACTLY ONCE"), and
resolve it through `blocker_ruling` = `retry` / `abort` — the user marks the PR ready, clears the
protection, or gives up, and answers. The record and the unpark are defined once, in `files-and-ledger.md`
(`status`) and `loop-control.md` step 3, "Only the user's answer unparks a PR"; never invent a second
mechanism here.

**`BLOCKED` does NOT mean "a required check is missing or failing."** It means the merge is blocked **for
any reason** — including a **draft** PR, or one **awaiting a human approving review**, or a ruleset
campaign cannot read. Verified: `cli/cli` PR #13856 reads `BLOCKED` with `mergeable = MERGEABLE` **and a
fully `SUCCESS` rollup**, purely because it is a draft. Mapping `BLOCKED` → `ci = pending` → "relaunch the
CI watch" therefore **LIVELOCKS**: the CI is already green, no CI event will ever fire, campaign never
approves PRs and never asks the user to — so it watches a settled PR forever. **Park it and name the
blocker instead.**

**`UNSTABLE` means non-*passing*, which includes *pending*.** Treating it as red would dispatch a CI-fix
subagent at a check that is merely **still running**.

1. **Serialize merge operations, not wakes.** A wake may merge multiple PRs, but only one at a time.
   Before each merge, re-confirm the PR is still **not parked** and that both gates still hold against the live PR head SHA
   (`gh pr view <pr> --json headRefOid --jq .headRefOid`, PR number from the ledger row) — a late push
   may have moved the tip past the recorded `head_sha` and reset the gates —
   **and re-fetch `origin/<base>` and re-check
   `gh pr view <pr> --json mergeable,mergeStateStatus,isDraft`** — a concurrent run sharing this base may
   have advanced it since the PR was last reviewed. Read the result through **"The merge precondition"**
   below, which maps **every** value of both enums.
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
   root checkout. A branch can be checked out in at most one working tree, so first locate that tree
   (`git worktree list` shows the branch per path; the root package counts as one), then fast-forward
   there. If `<base>` is checked out **nowhere**, update the ref directly instead — a plain `fetch`
   into the local branch (this form is refused while the branch is checked out, which is why it's the
   no-working-tree case):

   ```
   git -C $PROJECT fetch origin refs/heads/<base>:refs/remotes/origin/<base>   # explicit refspec: refresh origin/<base> even when <base> has no local branch and no upstream configured
   # case A — <base> is checked out in some working tree <dir> (root or a worktree):
   git -C <dir> merge --ff-only origin/<base>
   # case B — <base> is checked out in no working tree:
   git -C $PROJECT fetch origin <base>:<base>
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
     `.worktrees/<...>` worktree campaign itself created via `git worktree add`.
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

   **SKIP PARKED PRs FIRST — before any base refresh, rebase, or conflict handling.** A PR whose
   `status` is `awaiting-user` or `awaiting-api` is **FROZEN** (`loop-control.md` step 3,
   "parked-status guard"): this reconcile MUTATES a PR, so it is exactly what the guard forbids. A clean
   rebase would move its `head_sha`, set `ci = pending` and reset its liveness counters (`stage-2-ci.md`,
   "THE LIVENESS COUNTERS"); a conflict-resolving rebase would reset
   `reviews_ok`, relabel, and relaunch work — and would **change the PR's content**, which can invalidate
   the very refutation or API change the user was parked to adjudicate. **A parked PR that has fallen
   behind simply STAYS behind** until the user answers; it is re-reconciled normally on the wake after it
   unparks. **Do NOT drop its row** — it stays in the run, and the park **does not change its CI watch
   either way** (observation, not mutation): the watch follows the normal policy (`stage-2-ci.md`, "WATCH
   ONLY WHAT CAN MOVE") — relaunched while a row is still RUNNING, **not** relaunched once CI has settled.

   For each **non-parked** open PR: **base advancement alone does NOT
   invalidate gauntlet reviews.** Rebase only if GitHub flags the PR behind/conflicting:
   - Clean rebase (no conflicts) → verify the PR's own diff/content is unchanged → keep `reviews_ok`,
     **keep its status label as-is** (the gate did not reset, so an accepted PR stays
     `gauntlet-accepted`), update `head_sha` to the new tip, set `ci = pending`, **reset the liveness
     counters** (new commit, new evidence — `stage-2-ci.md`, "THE LIVENESS COUNTERS"), **and re-derive CI
     from a snapshot of the new tip in the same wake, launching a watch only if that snapshot holds a
     still-RUNNING row** ("WATCH ONLY WHAT CAN MOVE"). A rebased PR must not sit unwatched until the
     heartbeat while its checks are running — but it must not be watched when **nothing** is running
     either, which right after a push is the common case (no check has registered yet). CI must return
     green before merging.
   - Rebase requiring conflict resolution → PR content changed → **reset `reviews_ok` to 0 AND, in that
     same step, restore `gauntlet-reviewing` if the PR carries `gauntlet-accepted`** (`gh pr edit <pr>
     --remove-label gauntlet-accepted --add-label gauntlet-reviewing`) — the gate and its label move
     together (`stage-2-review-gate.md`, "Status labels mirror the review gate"). Update `head_sha` to the
     new tip and **reset the liveness counters** (a new head is new evidence — `stage-2-ci.md`, "THE
     LIVENESS COUNTERS"; the clean-rebase branch above does the same, and this branch is no different in
     that respect). Then re-derive CI for
     the new tip — watching it only if a row can still move ("WATCH ONLY WHAT CAN MOVE") — and re-enter
     Stage 2.
   - Still open, **not parked**, mergeable, not behind/dirty/conflicting, same live `head_sha`,
     `reviews_ok >= required(tier)`, and `ci == green` → still immediately mergeable; return to step 1
     in the same wake.

Stop the merge loop only when no remaining PR is immediately mergeable after the latest base refresh.

---
