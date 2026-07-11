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

**Why per-run files.** Because each run only ever writes and prunes its **own** file, appends never
contend and there is no shared-file rewrite to race. (A legacy single `history.md`, if present from
before this split, is still read as read-only history; leave it in place.)

### Pruning the ledger

The store grows one file per run, so **prune it regularly** — at the start of every fresh run, and any
time the user asks. The goal is to drop entries that **no longer apply to the current code**:

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
it lands as its own wake (prune then). Same principle as "never hold the run hostage on a user prompt"
(Run lease). Note what was pruned (and what the user kept) so the decision is auditable next run.

A run is distilled into the ledger **exactly once**, on its **normal exit** (all its PRs terminal) —
Loop control step 5 writes that run's own `.gauntlet/history/<run-id>.md`. The finished-run
"ask the user → yes" path reuses *that* file; it does not re-distill. `--new` never pre-empts other
runs — each run is isolated and always distills itself on its own exit — so there is no mid-flight
snapshot path.

### Starting a fresh run

1. **Mint the new run-id + agent token; atomically create its clean `<rundir>`.** Per-run dirs make a
   fresh run isolated by construction — a bare `mkdir` (no `-p`) of `.gauntlet/tmp/<new-run-id>/`
   starts empty and fails loudly on the rare id clash, so retry with a fresh id. Write the lease and a
   minimal `state.md` header immediately (so the run is discoverable before its first PR is adopted).
   Any already-live run keeps its own dir, lease, and heartbeat; a fresh run never closes, merges, or
   stops driving another run's PRs.
2. **Read every file in `.gauntlet/history/`, then prune** (drop entries no longer applicable to
   current `<base>`; uncertain deletions are asked about **without blocking** — keep the entries and
   proceed, see "Pruning the ledger"). Pruning only ever edits **finished** runs' own files (no live
   writer), so there's nothing to race.
3. Proceed to adopt the run's PR set and enter the loop as normal, on the clean `<rundir>`. Carryover
   is advisory historical context only — it de-dups against already-merged/aborted work and reminds the
   user of parked API-declined changes; it never auto-adopts or auto-skips a PR.

---
