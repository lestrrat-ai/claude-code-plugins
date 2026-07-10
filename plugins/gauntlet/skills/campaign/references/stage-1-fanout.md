## Stage 1 — Fan out (one PR per finding)

**Ensure the labels exist** first — the two shared status labels plus this run's owner label
(idempotent — `--force` creates or updates, safe on every resume):

```
gh label create gauntlet-reviewing --color FBCA04 --description "gauntlet: under review" --force
gh label create gauntlet-accepted  --color 0E8A16 --description "gauntlet: passed two reviews" --force
gh label create gauntlet-run-<run-id> --color 5319E7 --description "gauntlet: run <run-id>" --force
```

Spawn one subagent per surviving finding, up to a **rolling cap of ~8 in flight** — NOT in barrier
waves. Launch the first ~8 immediately, then refill: the moment any fix subagent finishes (and its PR
enters the gates), pull the next `pending` finding into the freed slot. Never wait for a group of ~8
to fully drain before starting more — a small remainder of one group must not stall the rest of the
backlog. From the first wake onward this refilling is owned by Loop-control step 3 (the same dispatch
that launches reviews/CI fixes/merges), so initial fan-out and resume use one identical slot-driven
path; this Stage 1 description is just what each fix subagent does. Each subagent:

1. `git worktree add $PROJECT/.worktrees/<branch> -b <branch> <base>`, `<branch>` =
   `fix-<run-id>-<finding-id>-<short-slug>` (the `<run-id>` prefix scopes the branch to this run so
   concurrent runs never collide; the `<finding-id>` keeps two findings with the same slug apart;
   branch off `<base>`, not whatever HEAD happens to be).
2. Implement the fix for **that finding only**, diff tight and scoped. If it would modify the public
   API surface or behavior, follow **Constraints**: under `ask`, park the finding (`awaiting-api`)
   and confirm with the user before changing anything; under `allowed`, proceed.
3. Commit, push, open the PR off `<base>` **before any gauntlet review or CI gate**, tagged with this
   run's owner label **and**
   `gauntlet-reviewing`:
   `gh pr create --base <base> --label gauntlet-run-<run-id> --label gauntlet-reviewing --title ... --body ...`
   (no "Test plan" section).
4. Return: finding id, branch, worktree path, PR number, one-line fix summary.

For each PR: record it in `state.md` (status `in_review`, `started` = now, `attempts` += 1) and
launch its background CI watch. The `--watch` only **blocks** until the run settles; immediately
after, **re-poll a fresh snapshot** into the file — that snapshot, not the watch, is what you read:

```
# run in background. ';' (not '&&') so the re-poll ALWAYS runs, even when --watch exits non-zero on failure
gh pr checks <pr> --watch ; gh pr checks <pr> > <rundir>/ci-<pr>.txt
```

The fix subagents only produce the fix + PR. They do NOT run reviews, watch CI, or merge — those
are yours.

---
