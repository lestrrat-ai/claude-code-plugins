## Stage 3 — Merge (serialized, auto)

A PR is mergeable when the **live PR head SHA** —
`gh pr view <pr> --json headRefOid --jq .headRefOid`, keyed by the PR number from the ledger row —
equals the ledger `head_sha` AND `reviews_ok >= required(tier)` AND `ci == green` — i.e.
`required(tier)` SATISFIED verdicts (1 if `tier == TRIVIAL`, else 2) and green CI all recorded
against the live tip. (An adopted PR may have no local worktree checked out, so use the PR's own head
via `gh`, never a local `git rev-parse HEAD`.)

1. **Serialize merge operations, not wakes.** A wake may merge multiple PRs, but only one at a time.
   Before each merge, re-confirm both gates still hold against the live PR head SHA
   (`gh pr view <pr> --json headRefOid --jq .headRefOid`, PR number from the ledger row) — a late push
   may have moved the tip past the recorded `head_sha` and reset the gates —
   **and re-fetch `origin/<base>` and re-check
   `gh pr view <pr> --json mergeable,mergeStateStatus`** — a concurrent run sharing this base may
   have advanced it since the PR was last reviewed. If it now reads `BEHIND`/`DIRTY`/`CONFLICTING`,
   refresh the PR per step 6 instead of merging it.
2. Push guard: `gh pr view <pr> --json state --jq .state` (PR number from the ledger row) must be
   `OPEN`.
3. Merge — the `--delete-branch` decision is gated on the run's `branch_ownership` header (read from
   the ledger; resolved once at adoption, see "PR adoption"):
   - **`branch_ownership = declined` (default)** → `gh pr merge <pr> --squash` (use the repo's prevailing
     merge method if not squash). **No `--delete-branch`** — the PR's remote head branch may be
     **user-owned** (an adopted PR keeps its own branch; campaign did not create it), so campaign must
     not delete it. Leave the remote branch in place; the repo's "automatically delete head branches"
     setting, or the user, handles it.
   - **`branch_ownership = granted`** → campaign owns the adopted branch, so tidy the remote too:
     `gh pr merge <pr> --squash --delete-branch` (prevailing merge method if not squash) — this deletes
     the remote head branch as part of the merge.
4. **Sync the local base branch with the remote.** The merge landed on `origin/<base>`, but local
   `<base>` is now behind. Fast-forward it so every subsequent rebase and `<base>...HEAD` diff is
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
   git -C $PROJECT fetch origin refs/heads/<base>:refs/remotes/origin/<base>   # explicit refspec: refresh origin/<base> (bare `fetch origin <base>` only writes FETCH_HEAD)
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
   ledger row (its `branch`/`worktree` columns) — there is no `fix-<run-id>-*` branch to clean up. **How
   much is torn down is gated on the run's `branch_ownership` header** (read from the ledger; resolved
   once at adoption). The two paths are explicit:

   **`branch_ownership = granted` — campaign owns adopted branches; full tidy.** Campaign may delete
   what it adopted, so clean up **regardless of `worktree_owned`/`branch_owned`**:
   - **The PR's remote head branch is already gone** — step 3 merged with `--delete-branch`.
   - **Remove the worktree and delete the local branch unconditionally.** Verify the merge with the
     `git-detect-merged` skill **against the run's `<base>`** (the ledger `base_branch` — NOT the
     helpers' default `main`), then `git worktree remove` the ledger-recorded worktree and delete the
     ledger-recorded local `branch` (e.g. `git-cleanup-merged` with that same `<base>`, or `git branch
     -d` after the worktree is gone) — even for a reused worktree or a pre-existing local branch. The
     user granted this; report what was removed.

   **`branch_ownership = declined` (default) — the safe behavior; tear down only what campaign
   created:**
   - **The PR's remote head branch is left in place** (step 3 dropped `--delete-branch`) — it may be
     user-owned, so campaign never deletes it; the repo's auto-delete setting or the user handles it.
   - **Worktree removal and local-branch deletion are gated SEPARATELY**, on `worktree_owned` and
     `branch_owned` respectively (adoption records the two independently — campaign can create a
     worktree over a **pre-existing** local branch, in which case `worktree_owned = yes` but
     `branch_owned = no`; see "PR adoption"). Never delete a worktree or branch campaign didn't create:
     - **`worktree_owned = yes`** → campaign created the worktree, so remove it: verify the merge with
       the `git-detect-merged` skill **against the run's `<base>`** (the ledger `base_branch` — NOT the
       helpers' default `main`, since the base may be a release/integration branch), then `git worktree
       remove` the **ledger-recorded worktree**. **`worktree_owned = no`** → campaign reused a
       pre-existing checkout (the user's root checkout or their own worktree), so **leave the worktree
       in place** — do NOT `git worktree remove` it.
     - **`branch_owned = yes`** → campaign created the local branch (the `-b` path), so delete it (e.g.
       via `git-cleanup-merged` **with that same `<base>`**, or `git branch -d` the ledger-recorded
       `branch` after the worktree is gone). **`branch_owned = no`** → campaign reused a pre-existing
       local branch (or checkout), so **leave the local branch in place** — do NOT delete it; that
       branch may be the user's.
     - **Report** any reused worktree and any reused local branch that were left in place (path +
       branch) in the final report, so the user knows their working tree/branches were untouched.

   In **both** paths, once cleanup is done set status `merged` and stop the PR's background tasks.

   This runs only after the merge is verified, and only ever touches PRs this run **owns** — those
   carrying its `gauntlet-run-<run-id>` label — never another run's. Leave the worktree in place if the
   merge cannot be confirmed — treat that as a bailout condition, not a cleanup.
6. After each merge+sync+cleanup, reconcile other open PRs. **Base advancement alone does NOT
   invalidate gauntlet reviews.** Rebase only if GitHub flags the PR behind/conflicting:
   - Clean rebase (no conflicts) → verify the PR's own diff/content is unchanged → keep `reviews_ok`,
     update `head_sha` to the new tip, set `ci = pending`, **and relaunch its CI watch in the same
     wake** — the rebased PR must not sit unwatched until the heartbeat; CI must return green before
     merging.
   - Rebase requiring conflict resolution → PR content changed → **reset `reviews_ok` to 0**, relaunch
     the CI watch, re-enter Stage 2.
   - Still open, mergeable, not behind/dirty/conflicting, same live `head_sha`,
     `reviews_ok >= required(tier)`, and `ci == green` → still immediately mergeable; return to step 1
     in the same wake.

Stop the merge loop only when no remaining PR is immediately mergeable after the latest base refresh.

---
