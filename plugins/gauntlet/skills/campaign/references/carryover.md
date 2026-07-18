## Fresh runs and carryover

A **fresh run** gates a **new PR set** under a new run-id — triggered either by the user answering
"yes" to the finished-run prompt (Loop control step 1) or by an explicit `--new`. It is *not* a
resume of the prior run's PRs: `--new` mints a **fresh run-id** for a new set of PRs to adopt (see the
args grammar), leaving any already-live run untouched. There is **no adversarial sweep to seed** — the
campaign only gates+merges the PRs it is handed — so carryover is a lightweight **historical record**,
not sweep input.

### The carryover ledger — `.gauntlet/history/`

A durable, git-ignored store (a sibling of `.gauntlet/tmp/`, NOT under it — scratch gets wiped, this
must not). To stay concurrency-safe it is **one file per run**, `.gauntlet/history/<run-id>.md` —
never a single shared file two runs could clobber. Each finished run writes **its own** file exactly
once. A per-run file records what that run's PRs came to:

- **merged** — PR number + slug + one-line description, per PR that shipped.
- **aborted** — PR number + slug + why it couldn't clear the bar (pointer to its `abort-<id>.md` if
  still present).
- **skipped (API-declined)** — a PR whose API-changing fix the user was asked about and declined, with
  the change it would have needed.

If `.gauntlet/history/` doesn't exist, create it (and add `.gauntlet/` to the repo's `.gitignore` if
it's not already ignored). When the directory is empty, a fresh run is just a normal first run.

**Why per-run files.** In normal operation each run WRITES only its **own** file, so concurrent runs
never contend on a shared rewrite. Pruning is the one exception: a **fresh** run may edit or remove
OTHER runs' files, but only those of **finished** runs (no live writer/lease) — never a file an
actively-driven run owns — so there is still no write contention with a live writer. (A legacy single
`history.md`, if present from before this split, is still read as read-only history; leave it in
place.)

### Pruning the ledger

The store grows one file per run, so **prune it regularly** — early in every fresh run, once that
run's PRs are adopted and its `base_branch` is resolved (pruning keys off the base), and any time the
user asks. The goal is to drop entries that **no longer apply to the current code**:

- **aborted** whose cited `file:line` no longer exists, or whose PR has since merged/closed by other
  means — the recorded blocker can't still hold.
- **skipped (API-declined)** whose referenced surface no longer exists, or that has since shipped —
  moot.
- **merged** entries are historical record and cheap; keep them unless the user wants them condensed.

**Confirm before deleting when unsure — this is the load-bearing rule.** Delete outright *only*
entries that are unambiguously moot: the exact cited site is gone. For anything you're not certain
about — an aborted PR you can't confirm was resolved, a declined API change you're unsure shipped —
**do NOT delete it. List those candidates with why each looks stale and ask the user** which to
remove. Never silently drop an entry you're uncertain about.

**The question must not stall the run.** Keep every uncertain entry in place and start the run's work
immediately — surface the candidate list to the user in the same message and fold the answer in when
it lands as its own heartbeat (prune then). Same principle as "never hold the run hostage on a user prompt"
(Run lease). Note what was pruned (and what the user kept) so the decision is auditable next run.

A run is distilled into the ledger **exactly once**, on its **normal exit** (all its PRs terminal) —
Loop control step 5 writes that run's own `.gauntlet/history/<run-id>.md`. The finished-run
"ask the user → yes" path reuses *that* file; it does not re-distill. `--new` never pre-empts other
runs — each run is isolated and always distills itself on its own exit — so there is no mid-flight
snapshot path.

### Starting a fresh run

1. **Preflight the `#PR` set FIRST — read-only, before any run state.** Read every PR's metadata
   (`gh pr view`), run the refusal checks (foreign-owned, cross-repo/fork per `pr-adoption.md`), and
   verify all share a common `baseRefName`. This touches **no** run-id, `<rundir>`, lease, or
   `state.jsonl`. If any PR is refused or the bases disagree, **prompt and create nothing** — a rejected
   set must never leave an orphan run behind.
2. **Only once preflight passes: create the run directory first, then record the run.** Call
   `runtime-adapter.md`'s `create_run_directory(repository)` **first** — it mints the run-id and
   atomically creates the clean `<rundir>` (it owns path derivation and collision behavior); derive
   `run_id` from the returned directory's final path component. **Then** mint the agent token (separate,
   run-id-independent). Write the lease and the `state.jsonl` header — with `run_id` set and `base_branch`
   filled from the agreed `baseRefName` (known from preflight) — then adopt each PR
   (ledger row + labels + worktree, and a CI watch **only when one is due** — `pr-adoption.md` owns what
   adoption produces and when the watch is warranted); a death mid-adoption still leaves a discoverable run.
   Any already-live run keeps its own dir, lease, and heartbeat; a fresh run never closes, merges, or
   stops driving another run's PRs.
3. **Read every file in `.gauntlet/history/`, then prune against the resolved `base_branch`** (drop
   entries no longer applicable to that base; uncertain deletions are asked about **without blocking**
   — keep the entries and proceed, see "Pruning the ledger"). Pruning only ever edits **finished**
   runs' own files (no live writer), so there's nothing to race.
4. Enter the loop as normal, on the clean `<rundir>`. Carryover is advisory historical context only —
   it de-dups against already-merged/aborted work and reminds the user of parked API-declined changes;
   it never auto-adopts or auto-skips a PR.

---
