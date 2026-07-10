## Base branch

The run targets a **base branch** — the branch every PR merges into and every fix diff is measured
against. It is **not assumed to be `main`**: it defaults to the branch checked out at invocation
(`git rev-parse --abbrev-ref HEAD`; fall back to the repo's default branch if HEAD is detached), which
may be a release or integration branch. Resolve it **once** at the start of a run and record it in the
ledger header as `base_branch`; re-read it from the ledger every wake, never from memory (a run is
long and the checked-out branch can drift).

Throughout this doc, `<base>` means that branch and `origin/<base>` its remote-tracking branch.
Concretely: fix worktrees branch off `<base>`, PRs open with `--base <base>`, every review diffs
`<base>...HEAD`, carryover entries are re-judged against current `<base>`, and after each merge local
`<base>` is fast-forwarded to `origin/<base>`. Where examples below show `main`, read it as `<base>` —
`main` is only the common default.

## Run identity and concurrency

Multiple gauntlet runs can execute concurrently in one repo, and a new agent instance can pick
up a run a prior instance left mid-flight — but **never two agents driving the same ledger at once**
(that is the bug this guards against). Two mechanisms: a **run ID** that namespaces everything a run
owns, and a **run lease** that marks whether an agent is actively driving that run right now.

### Run ID — namespacing

Minted once at the start of a fresh run — compact, filesystem- and label-safe. Create the run dir
**atomically** (`mkdir` fails if it already exists) so a run-id collision can't silently share a dir;
retry with a fresh id on the rare clash:

```
run_id="g$(date +%y%m%d-%H%M)-$(openssl rand -hex 4)"   # e.g. g260704-0915-a3f29c1b (32 bits entropy)
mkdir -p "$PROJECT/.gauntlet/tmp"                    # parent only; -p is safe here
mkdir "$PROJECT/.gauntlet/tmp/$run_id" || run_id=…   # NO -p: must fail on collision
```

The run dir's `mkdir` carries **no `-p`** on purpose — it must fail if the id is already taken. Create
the `.gauntlet/tmp` parent in its own `-p` call, never by `-p`-ing the run dir itself.

Record it in the ledger header (`run_id:`) and re-read it every wake (like `base_branch`); never trust
in-context memory for it — a wake may be a fresh agent instance. It flows into:

| Owned by the run | Namespaced form |
|------------------|-----------------|
| tmp working dir  | `<rundir>` = `.gauntlet/tmp/<run-id>/` (all findings/verdicts/state/review/ci/abort/lease files) |
| ledger header    | `run_id: <run-id>` |
| fix branch       | `fix-<run-id>-<finding-id>-<slug>` (finding-id keeps same-slug findings from colliding) |
| worktree         | `$PROJECT/.worktrees/fix-<run-id>-<finding-id>-<slug>` |
| PR owner label   | `gauntlet-run-<run-id>` (every PR the run opens carries it; authoritative "mine" marker) |
| self-wake prompt | `/gauntlet:campaign --run <run-id> --token <agent-token> <args>` (carries the id **and** the driver token so a summarized wake re-proves ownership without guessing) |

**Isolation invariant — a run touches ONLY its own work.** It reads/writes only its `<rundir>`, only
its `state.md`, only PRs carrying its `gauntlet-run-<run-id>` label (equivalently on a `fix-<run-id>-`
branch), and only those worktrees/branches. It MUST NOT reconcile, relabel, review, fix, merge, or
clean up another run's PRs/branches — **every git/gh scan is filtered to this run's label or branch
prefix.** The status labels `gauntlet-reviewing` / `gauntlet-accepted` describe gate state and are
shared across runs; ownership is the per-run label, never a status label.

**Shared across runs:** the carryover ledger tree `.gauntlet/history/` (kept race-free by one
file per run — see "Fresh runs and carryover"), the two status labels, and the Copilot precondition's
scratch file `.gauntlet/tmp/copilot-review-items.json` (written by `/gauntlet:copilot-address-reviews`) — treat that
last one as ephemeral to a single fetch→address cycle and re-fetch rather than trusting a stale
snapshot another run may have overwritten.

### Run lease — one active driver at a time

Namespacing keeps two *runs* apart; the **lease** keeps two *agents* from driving the **same** run.
Each run has `<rundir>/lease.json`:

```
{ "agent": "<token>", "updated": <unix-ts> }   # token = the agent holding the run; ts = last heartbeat
```

- **Mint** an agent token (`openssl rand -hex 4`) when you first take a run — at fresh-run start or on
  adoption — keep it in context, **and put it in your self-wake prompt** (`--token <tok>`) so a
  summarized/amnesiac wake recovers it from the prompt instead of guessing. **You own the run iff the
  token you present (prompt `--token`, else in-context) equals the lease's `agent`.**
- **Claim atomically.** Taking or adopting a run is a check-and-set that MUST be serialized against
  other agents: acquire an atomic lock first — `mkdir <rundir>/claim.lock` (fails if held) — then read
  the lease, decide, write your token + fresh `updated`, and `rmdir` the lock. Two agents racing to
  adopt can't both win because only one `mkdir` succeeds. (A crashed claim leaves a stale
  `claim.lock`; treat one whose mtime is older than a few minutes as abandoned and clear it.)
- **Heartbeat.** Rewrite the lease with `updated = $(date +%s)` every wake once you're the confirmed
  owner, **and** immediately before and after any long *foreground* step, should one be unavoidable,
  so a busy turn still looks alive. All long work — sweep shards, verification chunks, reviews, CI —
  is backgrounded, so turns stay short and the per-wake refresh normally suffices. A lease is
  **stale** only once `now - updated` exceeds **~30 min** — comfortably longer than any single
  foreground operation, so liveness flags a *dead* driver, not a busy one.
- **Never hold the run hostage on a user prompt.** Do NOT block the loop waiting on a user answer —
  that freezes the heartbeat and could let the run be declared stale mid-drive. Park the finding
  `awaiting-api`, surface the question, keep driving the other findings, reschedule, and fold the
  answer in when it lands as its own wake (Constraints).
- **Adopt only an orphaned run.** Safe to take over only when the lease is **absent or stale** (under
  the claim lock). After writing your token, re-read: if it isn't yours, you lost the race — stand down.
- **Stand down if superseded.** On a self-wake, present your `--token`: if the lease is **fresh** and
  its `agent` is a **different** token, you were superseded (a takeover while you were hung) — do NOT
  drive; report and stop. Never overwrite a fresh lease you don't own. (Carrying the token in the
  prompt removes any amnesia ambiguity — a self-wake always knows its own token.)
- **Release** on normal exit: delete `lease.json` (with the owner label) so the finished run shows no
  active driver.

### Resolving a wake (Loop control step 1 applies this)

1. **`--run <id>` given** (every self-wake; also a manual targeted resume). Load `<rundir>/state.md`.
   Under the claim lock, compare the token you present to the lease: **matches** (self-wake with
   `--token`) → refresh lease, reconcile, continue; **lease absent/stale** → adopt (write token, fresh
   ts, read-back); **lease fresh but a different token** → for a self-wake, stand down (superseded);
   for a **manual** `--run` with no matching token, another agent appears active, so **confirm takeover
   with the user** before adopting.
2. **Bare invocation** → the arg decides intent:
   - **A scope arg is given** (`/gauntlet:campaign <area>`, no `--run`) → **start a NEW run** on that
     scope. A scope is an explicit "sweep this now", so it never silently adopts an existing run — this
     is how you launch a second concurrent run (`auth` alongside `storage`). To resume a specific run
     instead, pass `--run <id>`.
   - **No arg at all** (`/gauntlet:campaign`) → resume-oriented: **discover runs** and bucket by lease —
     distinct `gauntlet-run-*` labels on open `fix-*` PRs ∪ run-ids with a `<rundir>/` (its `state.md`
     or `lease.json`; a run mid-Stage-0 has a dir before any PR), each **actively-driven** (fresh
     lease), **orphaned** (non-terminal, lease absent/stale), or **finished** (terminal, no open PR):
     - exactly one **orphaned** → adopt and resume it ("pick up where the previous instance left off");
     - several orphaned → list them (id, scope, #open PRs) and **ask which to resume, or start new**;
     - only **actively-driven** → each has a live driver; do NOT hijack — offer to start a **new** run;
     - only **finished** → the finished-run prompt (Loop control step 1), per run;
     - none at all → first run: mint a run-id, Stage 0 → Stage 1.
3. **`--new`** (or "fresh run" / "start over") → always mint a NEW run-id + token and start fresh with
   carryover; it creates an independent run and does **not** pre-empt other runs (they keep their own
   drivers). Scope is the arg, if any.
