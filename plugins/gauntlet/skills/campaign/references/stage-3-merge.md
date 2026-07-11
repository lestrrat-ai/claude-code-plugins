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
   - **`branch_ownership = granted`** → campaign may delete the adopted PR's **remote** head branch:
     `gh pr merge <pr> --squash --delete-branch` (prevailing merge method if not squash) — this deletes
     the remote head branch as part of the merge.

   This `--delete-branch` choice is the **ONLY** thing `branch_ownership` controls, and it touches the
   **remote** branch alone. Local cleanup (step 5) is identical in both modes — never keyed off
   `branch_ownership`.
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
   ledger row (its `branch`/`worktree` columns) — there is no `fix-<run-id>-*` branch to clean up.

   **The remote head branch was already handled by step 3** — deleted when `branch_ownership = granted`
   (the `--delete-branch` merge), left in place when `declined`. That is the **only** ref
   `branch_ownership` governs.

   **Local cleanup is gated on the per-PR `worktree_owned`/`branch_owned` flags — identically in both
   `granted` and `declined` modes, and never keyed off `branch_ownership`.** Adoption records the two
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

   So the **only** difference between `granted` and `declined` is step 3's remote `--delete-branch`;
   local ref safety (`worktree_owned`/`branch_owned`, never touching the root/main checkout or any
   reused ref) is identical and absolute in both. Once cleanup is done set status `merged` (via
   `scripts/ledger.py … set --pr <N> --status merged`, by field name — the schema-owning accessor,
   `files-and-ledger.md`; never hand-edit the row by column position) and stop the PR's background
   tasks.

   This runs only after the merge is verified, and only ever touches PRs this run **owns** — those
   carrying its `gauntlet-run-<run-id>` label — never another run's. Leave the worktree in place if the
   merge cannot be confirmed — treat that as a bailout condition, not a cleanup.
6. After each merge+sync+cleanup, reconcile other open PRs (write any `reviews_ok`/`head_sha`/`ci`
   change below through `scripts/ledger.py … set --pr <N> --<field> <val>` by field name, never by
   hand-editing the row by column position). **Base advancement alone does NOT
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
