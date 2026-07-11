## PR adoption — take existing PRs into the run

Campaign does not sweep, invent findings, or write fixes from scratch. It **adopts existing PRs** and
drives each through the gates to merge. This file is the adoption procedure: given some PRs, register
them into the run and start their gate work.

Two entry paths feed it (see "Run identity and concurrency" for the full grammar):
- **explicit `#PR` args** (`/gauntlet:campaign #12 #15`) — adopt exactly those PRs.
- **no-arg discovery** (`/gauntlet:campaign`, resume) — reconcile the PRs already labelled for this run:

  ```
  gh pr list --label gauntlet-run-<run-id> --state open --json number,headRefName,headRefOid,title,baseRefName > <rundir>/prs.json
  ```

  Every open PR carrying this run's owner label is already ours — refresh its row from that snapshot.
  A PR with the label but no row is a re-adoption after an amnesiac wake; a row whose PR is gone
  (merged/closed) reconciles to its terminal status.

`base_branch` for the run = the adopted PR's `baseRefName`. When several PRs are adopted at once they
**must agree** on `baseRefName`; if they disagree, stop and prompt the user (one run targets one base).

**Ensure the labels exist** first — the two shared status labels plus this run's owner label
(idempotent — `--force` creates or updates, safe on every resume):

```
gh label create gauntlet-reviewing --color FBCA04 --description "gauntlet: under review" --force
gh label create gauntlet-accepted  --color 0E8A16 --description "gauntlet: passed its reviews" --force
gh label create gauntlet-run-<run-id> --color 5319E7 --description "gauntlet: run <run-id>" --force
```

### Adopt one PR

For each `#PR` to adopt:

1. **Read the PR** — one `gh pr view` for the facts the ledger row needs:

   ```
   gh pr view <pr> --json number,title,headRefName,headRefOid,baseRefName,labels,state > <rundir>/pr-<pr>.json
   ```

2. **Refuse a foreign-owned PR.** If `labels` already contains a `gauntlet-run-*` label that is **not**
   this run's `gauntlet-run-<run-id>`, another run owns it — **do NOT adopt, relabel, or touch it**.
   Tell the user that PR is owned by that other run and to let that run finish or release it first.
   Never steal or transfer another run's owner label (isolation invariant, "Run identity and
   concurrency"). A PR with **no** `gauntlet-run-*` label, or already carrying **ours**, is adoptable.

3. **Register the ledger row — refresh, never duplicate.** Look the PR up in `state.md` by `pr`/`id`
   first. If a row already exists (re-adoption / resume), **refresh it in place** — never append a
   second row for the same PR. Otherwise append a new row. Write the **full** row:

   - `id` = `pr<N>`; `slug` = slugified PR title; `branch` = the PR's **own** `headRefName` (adopted PRs
     keep their branch — do NOT mint a `fix-<run-id>-...` branch); `worktree` = `-` until the PR-head
     worktree is created in step 5 (before its first review pass); `pr` = `<N>`; `head_sha` = `headRefOid`.
   - `reviews_ok` = `0` on first adoption (no verdicts yet against our watch); `ci` = `pending`
     (unknown until the first `gh pr checks`); `tier` = triage per `head_sha` ("Adaptive review tiers");
     `attempts` = `1`; `started` = now; `api_approval` = `-`; `status` = `in_review`.

   The ownership marker for an adopted PR is the **label**, not the branch name (its branch won't match
   the `fix-<run-id>-` prefix) — so labelling in step 4 is what makes the PR ours.

4. **Label it ours + under review.** Add this run's owner label and the shared reviewing status label:

   ```
   gh pr edit <pr> --add-label gauntlet-run-<run-id> --add-label gauntlet-reviewing
   ```

5. **Create the PR-head worktree before the first review pass — off the PR's OWN head, never `<base>`.**
   The review itself needs a real checkout: the review command runs `codex exec -C
   $PROJECT/.worktrees/<branch>` and diffs `<base>...HEAD`, so the worktree MUST exist **before the PR's
   first review pass dispatches** — create it here as part of adoption, or as a guaranteed pre-review
   step (Loop control makes it a precondition of the review launch). It is NOT created lazily only on a
   fix; a review always needs it. Branch it from the **PR's head branch/SHA**, not `<base>` (branching
   off `<base>` would throw the PR's own commits away). Fetch the head branch to a named local ref
   first, then add the worktree on it:

   ```
   git fetch origin <headRefName>:<headRefName>          # bring the PR's branch down as a local branch
   git worktree add $PROJECT/.worktrees/<headRefName> <headRefName>
   ```

   Record the resulting path in the row's `worktree`. Reviews read/diff this checkout, and all fix
   commits for the PR also go here; stage only the specific source files changed (explicit paths, never
   `git add -A`).

6. **Ensure a live CI watch when `ci = pending`.** Every adopted PR whose CI state is unknown gets a
   background watch so a settling run wakes the driver. The `--watch` only **blocks** until the run
   settles; immediately after, **re-poll a fresh snapshot** — that snapshot, not the watch, is what you
   read:

   ```
   # run in background. ';' (not '&&') so the re-poll ALWAYS runs, even when --watch exits non-zero on failure
   gh pr checks <pr> --watch ; gh pr checks <pr> > <rundir>/ci-<pr>.txt
   ```

   Don't launch a duplicate watch for a PR that already has a live one (Loop control tracks in-flight
   watches).

Adoption produces only the registered, labelled row (and a CI watch when due). Reviews, CI fixes, and
merges are driven by Loop control on later wakes — this file just gets each PR **into** the run.

---
