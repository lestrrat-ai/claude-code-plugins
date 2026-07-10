## Fresh runs and carryover

A **fresh run** is a new Stage 0/1 cycle started when a prior run already happened — triggered either
by the user answering "yes" to the finished-run prompt (Loop control step 1) or by an explicit
`--new`. It is *not* a resume: it does a brand-new codex sweep. What makes it more than a blind
re-run is **carryover** — it inherits what earlier runs already learned.

### The carryover ledger — `.gauntlet/history/`

A durable, git-ignored store (a sibling of `.gauntlet/tmp/`, NOT under it — scratch gets wiped, this
must not). To stay
concurrency-safe it is **one file per run**, `.gauntlet/history/<run-id>.md` — never a single
shared file two runs could clobber. Each finished run writes **its own** file exactly once; a fresh
run reads **every file in the directory** (concatenated) so knowledge compounds across runs. A per-run
file records:

- **merged** — finding slug + one-line fix, per PR that shipped.
- **aborted** — finding slug + why it couldn't clear the bar (pointer to its `abort-<id>.md` if still
  present).
- **declined-api** — findings parked under `ask` that the user declined, with the change they'd have
  needed.
- **refuted** — findings the verification pass rejected as non-issues, with a one-line reason.
- **uncertain** — findings left for the user to triage.

If `.gauntlet/history/` doesn't exist, create it (and add `.gauntlet/` to the repo's
`.gitignore` if it's not already ignored). When the directory is empty, a fresh run is just a normal
first run.

**Why per-run files.** Because each run only ever writes and prunes its **own** file, appends never
contend and there is no shared-file rewrite to race — the append/prune hazard of a single `history.md`
is gone. (A legacy single `history.md`, if present from before this split, is still read for carryover;
leave it in place as read-only history.)

### Pruning the ledger

The ledger grows append-only during runs, so **prune it regularly** — at the start of every fresh
run (right before feeding carryover into Stage 0), and any time the user asks. The goal is to drop
entries that **no longer apply to the current code**, so stale context can't mislead the new sweep.
Check each entry against current `<base>`:

- **refuted / aborted / uncertain** whose cited `file:line` no longer exists, or whose code has
  materially changed since — the finding as recorded can't still hold. A changed refuted/aborted site
  should be *re-judged fresh* by the new sweep, not carried as a settled verdict, so drop the stale
  entry.
- **declined-api** whose referenced surface no longer exists, or that has since shipped — moot.
- **merged** entries are historical record and cheap; keep them unless the user wants them condensed.

**Confirm before deleting when unsure — this is the load-bearing rule.** Delete outright *only*
entries that are unambiguously moot: the exact cited site is gone and there's nothing to re-judge.
For anything you're not certain about — the site moved but the concern might still stand, an aborted
finding you can't confirm was resolved, a declined-api you're unsure shipped — **do NOT delete it.
List those candidates with why each looks stale and ask the user** which to remove. Never silently
drop an entry you're uncertain about; a wrongly-pruned `refuted` re-opens a settled non-issue, and a
wrongly-pruned `aborted` loses a real unfinished thread.

**The question must not stall the run.** Keep every uncertain entry in place, feed carryover as-is,
and launch Stage 0 immediately — surface the candidate list to the user in the same message and fold
the answer in when it lands as its own wake (prune then; kept-by-default entries are only advisory
verifier context, so starting without the answer costs nothing). Same principle as "never hold the
run hostage on a user prompt" (Run lease).

Note what was pruned (and what the user kept) so the decision is auditable on the next run.

A run is distilled into the ledger **exactly once**, on its **normal exit** (all its PRs terminal) —
Loop control step 5 writes that run's own `.gauntlet/history/<run-id>.md`. The finished-run
"ask the user → yes" path reuses *that* file; it does not re-distill. (`--new` no longer pre-empts
other runs — each run is isolated and always distills itself on its own exit — so there is no
mid-flight snapshot path.)

### Starting a fresh run

1. **Mint the new run-id + agent token; atomically create its clean `<rundir>`.** Per-run dirs make a
   fresh run isolated by construction — a bare `mkdir` (no `-p`) of `.gauntlet/tmp/<new-run-id>/`
   starts empty and fails loudly on the rare id clash, so retry with a fresh id; there is nothing to
   archive and no prior scratch to wipe. Write the
   lease and a minimal `state.md` header immediately (so the run is discoverable before Stage 0
   finishes). Any already-live run keeps its own dir, lease, and heartbeat; a fresh run never closes,
   merges, or stops driving another run's PRs (abandoning a specific run is a separate explicit ask).
2. **Read every file in `.gauntlet/history/`, then prune** (drop entries no longer applicable
   to current `<base>`; uncertain deletions are asked about **without blocking** — keep the entries
   and launch Stage 0 while the question is pending, see "Pruning the ledger"). Pruning only ever
   edits **finished** runs' own files (no live writer), so there's nothing to race. Feed the pruned
   carryover into Stage 0 (below).
3. Proceed through Stage 0 → Stage 1 → the loop as normal, on the clean `<rundir>`.

### How carryover shapes Stage 0

- **Re-surface unresolved items.** Seed the verification pass with the prior **aborted**,
  **declined-api**, and **uncertain** findings as priority candidates: if the new codex sweep
  re-finds them (or they're still live in the code), they survive into the ledger ahead of net-new
  findings rather than being silently forgotten. A declined-api finding stays parked under `ask`
  unless the user has since OK'd it.
- **Suppress known false-positives.** Give the neutral verifier the prior **refuted** set so it
  doesn't re-litigate the same non-issues — a finding matching a prior refutation is dropped unless
  the code at that site has since changed (in which case re-judge it fresh).
- **Dedup already-merged fixes.** Give the verifier the prior **merged** set so it doesn't re-propose
  work already shipped. (Usually moot — the merged fix changed the code — but it catches a sweep that
  re-flags the same site from a different angle.)

Carryover is advisory context for the verifier, never an auto-accept or auto-reject: every finding
still goes through normal CONFIRMED/ADJUSTED/REFUTED/UNCERTAIN judgement.

---
