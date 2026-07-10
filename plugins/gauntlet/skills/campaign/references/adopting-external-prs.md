## Adopting an existing PR

A campaign normally opens its own `fix-<run-id>-*` PRs from Stage 0 findings. But an **existing repo
PR** — one a human or another tool already opened — can be **folded into a run** and driven through
the same Stage 2 review gate and Stage 3 merge. Use this to put a hand-authored PR through the review
gauntlet without reopening it.

To adopt PR `<pr>` into run `<run-id>`:

1. **Own it — but refuse a PR already owned by another run.** Before adding any label, inspect the
   PR's current labels: `gh pr view <pr> --json labels --jq '.labels[].name'`. If it already carries a
   `gauntlet-run-<other>` owner label for a *different* run, do NOT adopt it — refuse the adoption and
   tell the user to let that other run finish, or to release the PR (drop its `gauntlet-run-<other>`
   label) first. Never remove or transfer another run's owner label yourself: that would orphan the
   other run's ledger row. Only a PR with no existing `gauntlet-run-*` owner label, or one already
   owned by THIS run, may be adopted. Once you have confirmed no other run owns it, add this run's
   owner label and the reviewing status label:
   `gh api -X POST repos/<owner>/<repo>/issues/<pr>/labels -f 'labels[]=gauntlet-run-<run-id>' -f 'labels[]=gauntlet-reviewing'`
   (use the REST API if `gh pr edit --add-label` fails). The owner label is what makes the PR "this
   run's" for every scoped git/gh scan — an adopted PR keeps its original branch name, so the label,
   not a `fix-<run-id>-` prefix, is what ties it to the run.
2. **Add a ledger row.** First get the PR's head branch onto disk and into a worktree that checks out
   THAT branch (not `<base>`): `git fetch origin <pr-branch>` then
   `git worktree add $PROJECT/.worktrees/<pr-branch> <pr-branch>`. An adopted PR already has its own
   branch at its head, so the worktree must check out that branch — never create it off `<base>`, which
   would give you the base branch instead of the PR's changes. Then append a complete `state.md` row
   with every column in order — `id | slug | branch | worktree | pr | head_sha | reviews_ok | ci |
   attempts | started | api_approval | status`. Because an adopted PR has no Stage 0 finding, derive
   `id` and `slug` deterministically so two agents adopting the same PR pick identical values: set
   `id = pr-<pr-number>` (e.g. `pr-6`), and set `slug` to the PR's head branch name (fall back to its
   title) sanitized to a short kebab-case token — lowercase, non-alphanumerics collapsed to single
   hyphens, trimmed of leading/trailing hyphens, truncated to ~40 chars (e.g. head branch
   `Feat/New Auth` → `feat-new-auth`). Fill the rest from the PR: the PR's existing `branch` and its
   `worktree`, `pr`, current `head_sha`, `reviews_ok=0`, live `ci`, `attempts=1`,
   `started=<timestamp>`, `api_approval=-`, and `status: in_review`. It is NOT a Stage 0 finding, so it
   has no `findings-raw`/verdict entry — it enters the pipeline at Stage 2.
3. **Gate and merge as usual.** From here the normal loop applies with no special-casing: clear the
   Stage 2a preconditions (Copilot / CI / conflicts), run the two-review gauntlet over `<base>...HEAD`,
   and merge on two admissible SATISFIED verdicts + green CI (Stage 2, Stage 3).

Constraints:

- The PR must target this run's `base_branch`; if it targets a different branch, retarget it or run a
  campaign whose base matches.
- Merging an adopted PR is still a merge, so the run-wide `api_changes` gate applies — an API-changing
  adopted PR needs user confirmation unless `api_changes: allowed`.
- Cleanup at merge: squash-merge and delete the remote branch as usual, but the Stage 3 worktree/local
  branch cleanup only sweeps `fix-<run-id>-*` names, so an adopted PR's differently-named branch is
  SKIPPED by that rule. You must clean up its worktree and local branch explicitly, by the exact
  `branch`/`worktree` names recorded in the ledger row — and only ever for this run's own labelled PR.
