## Adopting an existing PR

A campaign normally opens its own `fix-<run-id>-*` PRs from Stage 0 findings. But an **existing repo
PR** — one a human or another tool already opened — can be **folded into a run** and driven through
the same Stage 2 review gate and Stage 3 merge. Use this to put a hand-authored PR through the review
gauntlet without reopening it.

To adopt PR `<pr>` into run `<run-id>`:

1. **Own it.** Add the run's owner label and the reviewing status label:
   `gh api -X POST repos/<owner>/<repo>/issues/<pr>/labels -f 'labels[]=gauntlet-run-<run-id>' -f 'labels[]=gauntlet-reviewing'`
   (use the REST API if `gh pr edit --add-label` fails). The owner label is what makes the PR "this
   run's" for every scoped git/gh scan — an adopted PR keeps its original branch name, so the label,
   not a `fix-<run-id>-` prefix, is what ties it to the run.
2. **Add a ledger row.** Append a `state.md` row: `id`, a slug, the PR's existing `branch` and a
   `worktree` for it (create one off `<base>` if none exists), `pr`, current `head_sha`,
   `reviews_ok=0`, live `ci`, and `status: in_review`. It is NOT a Stage 0 finding, so it has no
   `findings-raw`/verdict entry — it enters the pipeline at Stage 2.
3. **Gate and merge as usual.** From here the normal loop applies with no special-casing: clear the
   Stage 2a preconditions (Copilot / CI / conflicts), run the two-review gauntlet over `<base>...HEAD`,
   and merge on two admissible SATISFIED verdicts + green CI (Stage 2, Stage 3).

Constraints:

- The PR must target this run's `base_branch`; if it targets a different branch, retarget it or run a
  campaign whose base matches.
- Merging an adopted PR is still a merge, so the run-wide `api_changes` gate applies — an API-changing
  adopted PR needs user confirmation unless `api_changes: allowed`.
- Cleanup at merge treats the adopted PR like any other (squash-merge, delete the remote branch,
  remove its worktree/local branch) — but only ever this run's own labelled PR.
