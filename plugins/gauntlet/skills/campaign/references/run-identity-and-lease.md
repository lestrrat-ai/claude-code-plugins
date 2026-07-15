## Base branch

The run targets a **base branch** — the branch every adopted PR merges into and every review diff is
measured against. It is **not assumed to be `main`**: it is the **adopted PRs' `baseRefName`** (from
`gh pr view`), which may be a release or integration branch. When several PRs are adopted at once they
must **agree** on `baseRefName`; if they disagree, stop and prompt (one run targets one base). Resolve
it **once** at the start of a run and record it in the ledger header as `base_branch`; re-read it from
the ledger every wake, never from memory.

Throughout this doc, `<base>` means that branch and `origin/<base>` its remote-tracking branch.
Concretely: adopted PRs already target `<base>` (their `baseRefName`), every review diffs
`origin/<base>...HEAD`, and after each merge local `<base>` is fast-forwarded to `origin/<base>`. **Fix
worktrees do NOT branch off `<base>`** — they branch off the PR's **own head** (see "PR adoption"),
since the PR's commits live there. Where examples below show `main`, read it as `<base>` — `main` is
only the common default.

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

Record it in the ledger header field `run_id` (`ledger.py --file <state.jsonl> header set run_id
<run-id>`) and re-read it every wake (`ledger.py … header get run_id`, like `base_branch`); never trust
in-context memory for it — a wake may be a fresh agent instance. It flows into:

| Owned by the run | Namespaced form |
|------------------|-----------------|
| tmp working dir  | `<rundir>` = `.gauntlet/tmp/<run-id>/` (all state/pr/review/ci/abort/lease files) |
| ledger header    | the `run_id` header field (set/read via `ledger.py … header set/get run_id`) |
| PR owner label   | `gauntlet-run-<run-id>` — the **authoritative "mine" marker**. Every adopted PR is tagged with it; it, not any branch name, is what makes a PR this run's. |
| branch           | the **adopted PR's own `headRefName`** — campaign reuses the PR's existing branch and does NOT mint a `fix-<run-id>-...` branch, so ownership can't be read off the branch name (that's the label's job). |
| worktree         | the ledger-recorded `worktree` path — the created default `$PROJECT/.worktrees/<headRefName>` when campaign runs `git worktree add`, or a reused existing checkout when the branch was already checked out elsewhere — resolved from the PR's head branch during adoption / before its first review, and reused for review/CI fixes. Only a campaign-created worktree (`worktree_owned = yes`) is ever removed; a reused checkout (the root/main checkout or a user worktree) and a reused local branch are **always** left in place (see "PR adoption" / Stage 3). |
| self-wake prompt | `/gauntlet:campaign --run <run-id> --token <agent-token>` — **only** these two flags (carries the id **and** the driver token so a summarized wake re-proves ownership without guessing). It **never** carries `--new` or the original `#PR` adoption args: those are **start-time-only** (they *create/adopt*), whereas `--run` **resumes** an existing run — replaying `--new` on a self-wake would mint a fresh run every heartbeat. |

**Isolation invariant — a run touches ONLY its own work.** It reads/writes only its `<rundir>`, only
its `state.jsonl`, and only PRs carrying its `gauntlet-run-<run-id>` label (adopted PRs keep their own
branch names, so the **label alone** — not any branch prefix — scopes ownership), and only those
PRs' branches/worktrees. It MUST NOT reconcile, relabel, review, fix, merge, or clean up another run's
PRs/branches — **every git/gh scan is filtered to this run's owner label.** The status labels
`gauntlet-reviewing` / `gauntlet-accepted` describe gate state and are shared across runs; ownership is
the per-run label, never a status label. Refuse to adopt a PR already carrying a **different**
`gauntlet-run-*` label — never steal or transfer another run's marker (see "PR adoption").

**Shared across runs:** the carryover ledger tree `.gauntlet/history/` (kept race-free by one
file per run — see "Fresh runs and carryover"), the follow-up store `.gauntlet/followups.jsonl` (**one
file, many writers** — kept race-free by a lock inside `scripts/followups.py`, which is why it is never
hand-edited; see `followups.md`), the two status labels, and the Copilot precondition's
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
  so a busy turn still looks alive. All long work — reviews, CI watches, and fix subagents —
  is backgrounded, so turns stay short and the per-wake refresh normally suffices. A lease is
  **stale** only once `now - updated` exceeds **~30 min** — comfortably longer than any single
  foreground operation, so liveness flags a *dead* driver, not a busy one.
- **Never hold the run hostage on a user prompt.** Do NOT block the loop waiting on a user answer —
  that freezes the heartbeat and could let the run be declared stale mid-drive. Park the PR
  (`awaiting-api` for an API-changing fix; `awaiting-user` for a **review standoff** — a refutation the
  fresh reviewer re-raised — **or a machine blocker**, which is a **property**, not a list of cases:
  *campaign cannot move this PR without a human*, with `ci_reason` naming whatever it was. This file
  deliberately enumerates **no** blocker cases — `files-and-ledger.md`, `status`, `awaiting-user` class
  2, owns the class), surface the question, keep driving the other PRs, reschedule, and fold
  the answer in when it lands as its own wake. Each class names the **durable record** it is answered into
  and the **unpark** it triggers (`files-and-ledger.md`, `status`; `loop-control.md` step 3, "Only the
  user's answer unparks a PR") — a park with no defined exit is the wedge one level up (Constraints;
  `stage-2-review-gate.md`).
- **Adopt only an orphaned run.** Safe to take over only when the lease is **absent or stale** (under
  the claim lock). After writing your token, re-read: if it isn't yours, you lost the race — stand down.
- **Stand down if superseded.** On a self-wake, present your `--token`: if the lease is **fresh** and
  its `agent` is a **different** token, you were superseded (a takeover while you were hung) — do NOT
  drive; report and stop. Never overwrite a fresh lease you don't own. (Carrying the token in the
  prompt removes any amnesia ambiguity — a self-wake always knows its own token.)
- **Release** on normal exit: delete `lease.json` (with the owner label) so the finished run shows no
  active driver.

### Resolving a wake (Loop control step 1 applies this)

1. **`--run <id>` given** (every self-wake; also a manual targeted resume). Load `<rundir>/state.jsonl`.
   Under the claim lock, compare the token you present to the lease: **matches** (self-wake with
   `--token`) → refresh lease, reconcile, continue; **lease absent/stale** → adopt (write token, fresh
   ts, read-back); **lease fresh but a different token** → for a self-wake, stand down (superseded);
   for a **manual** `--run` with no matching token, another agent appears active, so **confirm takeover
   with the user** before adopting.
2. **Bare invocation** → the arg decides intent:
   - **`#PR` args are given** (`/gauntlet:campaign #12 #15`, no `--run`) → **start a NEW run** that
     **adopts those PRs** (see "PR adoption"). Passing PRs is an explicit "gate these now", so it never
     silently resumes an existing run — this is how you launch a second concurrent run (one PR set
     alongside another). To resume a specific run instead, pass `--run <id>`. A **non-PR** arg (e.g.
     `auth`) is not a scope any more — treat it like the no-arg idle case below and prompt.
   - **No arg at all** (`/gauntlet:campaign`) → resume-oriented: **discover runs** and bucket by lease —
     the distinct `gauntlet-run-*` ids present on open PRs — list PRs **with their labels** and extract
     the ids, since no id is known yet to query by (`gh pr list --state open --limit 1000 --json
     number,labels`, then pick labels matching `gauntlet-run-*`; **`--limit` is mandatory** — `gh pr list`
     silently caps at **30** without it, and a run missed by the scan reads exactly like a run that does
     not exist, so the driver would start a duplicate or fail to resume an orphan. Like `prs.json`, the
     cap **bounds** the scan rather than proving it complete) ∪ run-ids with a `<rundir>/` (its `state.jsonl` or
     `lease.json`), each **actively-driven** (fresh lease),
     **orphaned** (non-terminal, lease absent/stale), or **finished** (terminal, no open PR):
     - exactly one **orphaned** → adopt and resume it ("pick up where the previous instance left off"),
       reconciling its run-labelled PRs (see "PR adoption");
     - several orphaned → list them (id, #open PRs) and **ask which to resume, or start new**;
     - only **actively-driven** → each has a live driver; do NOT hijack — offer to start a **new** run;
     - only **finished** → the finished-run prompt (Loop control step 1), per run;
     - **none at all** (idle — nothing to drive) → **prompt**: "No PRs under a campaign. Run
       `gauntlet:review` to find issues, or pass PR numbers to gate." Campaign never sweeps or mints
       PRs itself, so with no run and no `#PR` args there is nothing to do.
3. **`--new #PR...`** (or "fresh run" / "start over" with PR numbers) → mint a NEW run-id + token and
   start a fresh run adopting those PRs; it creates an independent run and does **not** pre-empt other
   runs (they keep their own drivers). **`--new` with no `#PR` args mints nothing** — it falls through
   to the idle prompt (create no run-id/`<rundir>`/lease), exactly like a bare no-arg first run.
