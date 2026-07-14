## File locations

Everything under the run's own dir `<rundir>` = `.gauntlet/tmp/<run-id>/` (create at the start
of a fresh run; on resume, reuse the run's existing dir). Per-run dirs are what keep concurrent runs'
files from colliding — see "Run identity and concurrency".

| File (under `<rundir>`) | Contents |
|------|----------|
| `state.jsonl` | Live per-PR ledger — a **cache/hint**, not the source of truth (see below) |
| `pr-<pr>.json` | `gh pr view` snapshot captured at adoption (PR facts the ledger row is built from) |
| `prs.json` | Batched `gh pr list` snapshot of this run's PRs — the per-wake reconcile input, and the adoption/discovery input. **ONE path, ONE schema, ONE command**: the canonical command is spelled in full in **"The canonical `prs.json` command"**, the command block directly below this table, and that block is its ONLY definition |
| `lease.json` | This run's active-driver lease (`{agent, updated}`; see "Run lease") |
| `review-<pr>-<n>.txt` | The reviewer's PR review output, round `n` (launch attempt 1) |
| `review-<pr>-<n>.plan.jsonl` | Orchestrator-authored review work units for round `n` (per-pass — a relaunch reuses it). Written through `scripts/review-pass.py plan-add`, never a heredoc |
| `review-<pr>-<n>.progress.jsonl` | Reviewer progress events against the plan for round `n` (launch attempt 1), opened by the orchestrator's `pass_identity` line. **Every line is READ and validated through `scripts/review-pass.py` — and not every line is WRITTEN by it.** The `pass_identity` (`review-pass.py identity`) and the unit-progress events (the reviewer's `emit-progress.py`) are; a `plan_amendment_request` the reviewer appends **directly** is not — it is the one event the emit-only rule exempts. `stage-2-review-gate.md` owns that rule; see "Review-pass artifacts" below |
| `review-<pr>-<n>.a<k>.txt` / `.a<k>.progress.jsonl` | Same two artifacts for **launch attempt `k ≥ 2`** — a relaunched pass writes here, never over attempt 1's files, so a killed-but-alive attempt can't corrupt the live one. Only the attempt named in the active `pass_identity` is read or counted (see `stage-2-review-gate.md`) |
| `ci-<pr>-<head_sha>.txt` | Latest **SHA-pinned** CI snapshot for a PR — check runs **AND** commit statuses, fetched **BY THE WAKE** after the watch completes (**the watch never writes it**), promoted atomically, and **stamped with the `head_sha` it describes** (verify the stamp before parsing). Carries a **`source` completion marker per mandatory source**, so a source that was **never queried** is `unusable`, not a silent green (`stage-2-ci.md`). Never the watch stream, and never `gh pr checks` — its output carries **no SHA** |
| `audit-<pr>-<n>.md` | The orchestrator's audit of round `n`'s findings — CONFIRMED / ADJUSTED / REFUTED, each with evidence. A REFUTED finding's reasoning is recorded here **and** written into the tree as an inline comment at the site, committed like any other change (`stage-2-review-gate.md`, "Audit every finding before you fix it") |
| `abort-<id>.md` | Detailed log for an aborted PR-task |

**The canonical `prs.json` command — this block is THE definition.** Every other site defers to it, and
**NO site may spell a variant of it** — differing spellings are how a reader of `prs.json` ends up with
a file that is scoped wrong or missing the fields it reads. Copy it whole, including the `--label`
filter and the output path:

```
gh pr list --label gauntlet-run-<run-id> --state open --limit 1000 \
  --json number,headRefName,headRefOid,title,baseRefName,state,mergeable,mergeStateStatus,labels \
  > <rundir>/prs.json
```

`pr-adoption.md` (discovery) and `loop-control.md`'s per-wake PR scan (the `prs.json` block in step 1)
each run this command
inline, **identically** — same label, same flags, same `--json` field set, same path. That is intended:
they are the same scan. What is forbidden is a **different** spelling anywhere.

Every part is load-bearing:

- **Without `--label gauntlet-run-<run-id>`** the snapshot escapes the run's scope: the listing returns
  **every PR in the repo** instead of this run's, and reconcile would then act on — adopt, relabel,
  even merge — **other runs' PRs**. That is a **run-isolation violation**, and run isolation is the
  property that lets concurrent runs coexist in one repo.
- **Without `--limit`** `gh pr list` silently caps at **30** items, writing a truncated file that
  reconcile reads as the complete run snapshot.
- **Without `--json <the field set above>`** the reader finds no `labels`/`mergeable`/`headRefOid` —
  two writers with different field sets silently hand the reader a file missing the fields it reads.
- **Without `> <rundir>/prs.json`** the snapshot lands somewhere nobody reads, and reconcile reads a
  file nobody wrote.

**`prs.json` is a BOUNDED snapshot, not a proof of completeness.** `--limit 1000` defeats the default-30
truncation; it does **not** make the snapshot provably complete — a run with more than 1000 labelled PRs
would still truncate. That matters because **an absent PR is indistinguishable from a PR that was never
adopted**: a dropped row does not error, it just quietly stops being reconciled. Treat "every labelled PR
is in `prs.json`" as an assumption bounded by that cap, never as a guarantee.

Store ALL reviewer and `gh` output under `<rundir>` first, then Read/Grep it. NEVER `/tmp/`.

All of this is driver bookkeeping, **never repo content — do NOT commit it**: the whole `.gauntlet/`
tree stays git-ignored, and a fix commit stages only the specific source files it changes (explicit
paths, never `git add -A`/`.`).

**Durable cross-run knowledge lives outside `.gauntlet/tmp/`.** The plugin owns one directory at the
repo root, `.gauntlet/` (git-ignored; add `.gauntlet/` to `.gitignore` if missing), split by lifetime:

| Path | Lifetime |
|------|----------|
| `.gauntlet/tmp/<run-id>/` | Ephemeral scratch. A **terminal** run's dir is kept so a later bare invocation can detect the *finished* run and offer the finished-run prompt (Loop control step 1); it is otherwise disposable — wiping it only loses that prompt (discovery then falls back to the generic "pass PR numbers" prompt), never carryover, which lives in `history/`. Not wiped mid-run. |
| `.gauntlet/history/<run-id>.md` | Durable. The carryover ledger — the one thing a *new* run needs to remember from old ones. |

**Only `.gauntlet/tmp/` is disposable — never `rm -rf .gauntlet/` itself.** That would take the
carryover history with it. Scratch cleanup targets `.gauntlet/tmp/**` and nothing above it.

The history tree keeps **one file per run** (`<run-id>.md`) so concurrent runs never clobber a shared
file. Everything else stays ephemeral under the per-run `<rundir>`. See "Fresh runs and carryover".

### Campaign commits NO file of its own

**Campaign has NO committed file — no repo-root config, nothing.** The whole `.gauntlet/**` tree is
git-ignored driver bookkeeping, and that is the extent of campaign's on-disk footprint.

### The ledger — `state.jsonl`

One row per adopted PR. It is a **cache**, not the authoritative state — **ground truth is
GitHub via `gh`, plus local worktrees** (`gh pr list/view` for PRs and merged/open state, each PR's
`headRefOid` from `gh` — keyed by PR number — for the live head SHA, a **SHA-pinned** `check-runs` +
commit-`status` fetch for live CI, and
the **active launch attempt's** review output files for which verdicts exist on which SHA —
`review-<pr>-<n>.txt` for attempt 1, `review-<pr>-<n>.a<k>.txt` after a relaunch, counting only the
attempt named in that pass's `pass_identity`, so a relaunch's verdict is never missed and a dead
attempt's is never counted). `git rev-parse HEAD` is used
ONLY to validate/read an existing worktree when one is checked out — never as the primary source of a
PR's live head (an adopted PR may have no local branch/worktree at all). Every wake re-derives what's
due from those, then refreshes this file. So a stale or half-written ledger is self-healing — never
act on it without reconciling against gh (and any existing worktree) first.

The store is **JSONL** — one JSON object per line, `cat`/`grep`/`jq`-able. The first line is the
run-config header record (`{"type": "header", …}` — the run's config, **every field of it** re-read each
wake, never from memory; the fields are the ones `ledger.py`'s `HEADER_FIELDS` declares and "Header-record
fields" below defines, and no reader may keep its own copy of that list); each
following line is one adopted PR's row record (`{"type": "row", …}`). Every record is **self-describing**
— fields are keyed by NAME, never by column position:

```
{"type": "header", "run_id": "g260704-0915-a3f29c1b", "base_branch": "main", "api_changes": "ask", "reviewer": "codex", "required_set": "declared:[{\"context\": \"build\", \"app\": \"-\"}, {\"context\": \"test (3.12, ubuntu)\", \"app\": \"15368\"}]"}
{"type": "row", "id": "pr41", "slug": "fix-null-deref", "branch": "fix-null-deref", "worktree": ".worktrees/fix-null-deref", "worktree_owned": "yes", "branch_owned": "yes", "pr": "41", "head_sha": "a3f29c1b7d4e6f8091a2b3c4d5e6f708192a3b4c", "reviews_ok": "2", "ci": "green", "tier": "STANDARD", "attempts": "1", "started": "2026-07-04T09:15:00Z", "api_approval": "-", "status": "in_review", "ci_fingerprint": "sha256:9f2c\u2026", "settled_strikes": "0", "unusable_refetches": "0", "ci_stalled_since": "-", "ci_reason": "-", "blocker_ruling": "-"}
{"type": "row", "id": "pr52", "slug": "add-retry-flag", "branch": "add-retry-flag", "worktree": ".worktrees/add-retry-flag", "worktree_owned": "no", "branch_owned": "no", "pr": "52", "head_sha": "b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7089a1b", "reviews_ok": "0", "ci": "pending", "tier": "HIGH", "attempts": "0", "started": "-", "api_approval": "-", "status": "in_review", "ci_fingerprint": "sha256:4a71\u2026", "settled_strikes": "1", "unusable_refetches": "0", "ci_stalled_since": "-", "ci_reason": "required check absent: integration-tests", "blocker_ruling": "-"}
```

**`head_sha` is ALWAYS the full 40-char `headRefOid` — never an abbreviation.** The examples above spell
it in full because they are what gets copied. A SHA in the ledger is compared for equality against `gh`'s
live `headRefOid` and pasted into commands; an abbreviated one silently fails both. **NEVER write a
shortened SHA into the store, and never reconstruct one from a display.** The ONLY place a SHA is ever
shortened is `table`'s rendering (below) — that is display-only and does not exist on disk.

Header-record fields: `run_id` (this run's identity — namespaces its dir/label/wakes; set once),
`base_branch` (the adopted PRs' baseRefName — the branch they merge into & diffs measure against; set
once, see "Base branch"), `api_changes` (`ask` | `allowed`, run-wide; set once from the invocation),
`reviewer` (`default` (Claude subagents) | `codex` | `<other>` — the selected reviewer; set once, see
"The reviewer"), `required_set` (what `base_branch` **requires** — `stage-2-ci.md`, "What were we
expecting to see?", owns the three states, the format, and the reads that produce them; re-read every
wake while it is `unknown`).

`required_set` is a property of the **base branch**, not of a PR, which is why it lives in the header. It
defaults to **`unknown`**, and that default is **load-bearing, not a placeholder**: `unknown` **cannot go
green** (`stage-2-ci.md`), so a run that never performed the read merges **nothing**. **"I have not
looked" and "I looked and there are none" are different facts**, and the default is the one that claims
nothing — a `none` that really meant "I could not see" is how a green gets recorded for a commit whose
required check never registered.

Header field notes (the header fields above; per-row fields follow):

- Campaign **never** deletes the adopted PR's **remote** head branch — it never passes `--delete-branch`
  on merge. The repo's "Automatically delete head branches" setting governs remote-branch cleanup: if
  enabled, GitHub deletes the head branch on merge; otherwise it stays. Either way it is not campaign's
  action. **Local cleanup** is governed solely by the per-PR flags: the worktree is removed only when
  `worktree_owned = yes` and the local branch deleted only when `branch_owned = yes`; a reused worktree,
  the root/main checkout, and a reused local branch are **never** removed (see "Stage 3 — Merge").

- `id` — `pr<N>` (the adopted PR number). `slug` — slugified PR title. Together they identify the row;
  re-adoption looks up by `pr`/`id` and refreshes in place, never appends a duplicate.
- `branch` — the PR's **own** `headRefName`. Adopted PRs keep their branch — campaign does NOT mint a
  `fix-<run-id>-...` branch, so the branch name won't carry the run id. **The `gauntlet-run-<run-id>`
  label is the ownership marker**, not the branch prefix.
- `worktree` — the **actual** checkout path for this PR, resolved off the PR's head branch during
  adoption (or as a guaranteed pre-review step, before its first review pass) and reused for any
  review/CI fix — not created lazily only on fix (see "PR adoption"). This is the **created default**
  `$PROJECT/.worktrees/<headRefName>` when campaign runs `git worktree add`, **or** a **reused existing
  checkout** (the root checkout or a prior worktree) when the branch was already checked out elsewhere
  — in which case the path is wherever that checkout already lives, not `.worktrees/<headRefName>`.
- `worktree_owned` — whether **campaign created** this worktree: `yes` (campaign ran `git worktree
  add`, so cleanup may remove it) | `no` (campaign **reused** a pre-existing checkout it did not
  create, so Stage 3 leaves it in place) | `-` (not yet resolved). Set at adoption alongside
  `worktree` (see "PR adoption"); read by Stage 3 cleanup so it never deletes a checkout the
  user owns.
- `branch_owned` — whether **campaign created** the local branch, tracked **separately** from
  `worktree_owned`: `yes` (campaign created the branch on the `git worktree add -b <headRefName> ...
  origin/<headRefName>` path) | `no` (campaign **reused** a pre-existing local branch — the `git
  worktree add <path> <branch>` path — or a pre-existing checkout it did not create) | `-` (not yet
  resolved). Set at adoption alongside `worktree_owned` (see "PR adoption"). Stage 3 deletes the local
  branch **only when `branch_owned = yes`**, so campaign never deletes a branch the user owns even
  when it created the worktree.
- `head_sha` — the PR's live head (`headRefOid` from `gh`, keyed by PR number) that `reviews_ok`, `ci`,
  and `tier` describe. `ci`
  and `tier` are pinned to this exact SHA (re-triage on any content change). `reviews_ok` is pinned to
  this SHA **unless** the only change is a clean base-only rebase/merge with the PR diff unchanged;
  then carry `reviews_ok` forward to the new `head_sha`, set `ci = pending`, and — because the head
  **moved** — **reset the liveness counters** (`stage-2-ci.md`, "THE LIVENESS COUNTERS"). A clean rebase
  does not reset the *gate*, but it **is** a `head_sha` change, and **every** `head_sha` change resets
  those counters: the old head's strikes and stall clock measured evidence that no longer exists.
- `reviews_ok` — number of fresh, context-isolated SATISFIED verdicts recorded against this PR's
  current content. Target = `required(tier)`: **1 if `tier == TRIVIAL`, else 2** (Stage **2a-triage**).
- `tier` — the adaptive review tier derived from `head_sha`: `TRIVIAL` | `STANDARD` | `HIGH`. Re-derived
  every wake and re-triaged on any content change; drives `required(tier)` and the review depth.
- `ci` — `green` / `red` / `pending` for `head_sha`. (**There is no `none`.** It was documented but no
  procedure could ever write it.)
- `ci_fingerprint` — digest of the last **verified** CI snapshot. **What it covers and exactly how it is
  serialized is DEFINED in `stage-2-ci.md`, "SETTLED" — and is NEVER restated here**, because a
  fingerprint reconstructed from a paraphrase is a different fingerprint. **UNCHANGED + nothing RUNNING
  == SETTLED.**
- `settled_strikes` — consecutive derivations seen **SETTLED but not green** *while no machine action was
  due or in flight* for the PR at this `head_sha` (`stage-2-ci.md`, "SETTLED", owns the gate — a PR the
  driver is actively repairing is never struck). At the **STRIKE CAP**, escalate: park `awaiting-user`
  naming the blocker. Reset to `0` on any `head_sha` change or fingerprint change.
- `unusable_refetches` — consecutive derivations whose snapshot was **UNUSABLE** at this `head_sha`. An
  UNUSABLE snapshot has **no fingerprint** (its rows were never trusted), so it can never be a
  `settled_strike`: it gets its own counter. At the **REFETCH CAP**, escalate the same way. Reset to `0`
  on any `head_sha` change and on any **VERIFIED** snapshot (`stage-2-ci.md`, "UNUSABLE — the refetch is
  BOUNDED").
- `ci_stalled_since` — `-`, or the **UTC ISO-8601 timestamp** of the first derivation that saw the check
  set **RUNNING-STALLED** at this fingerprint: an evidence row still classifies `RUNNING` **and** the
  fingerprint did **not** change (`stage-2-ci.md`, "RUNNING-STALL" — the definition; the cap lives there
  and nowhere else). A **clock, not a tally**, and that is deliberate: a `RUNNING` row that is merely
  **SLOW** and one that is **DEAD** are indistinguishable on a fingerprint, and derivations are driven by
  wakes whose cadence depends on the run's load — so only elapsed **TIME** separates them. It is on disk
  precisely so `now - ci_stalled_since` is computable by a fresh agent instance that remembers nothing.
  Cleared (`-`) on any fingerprint change, on any `head_sha` change, and whenever a **machine action** is
  due or in flight for this PR at this `head_sha` (a fix that pushes will replace these rows). At the cap,
  escalate: park `awaiting-user`, `ci_reason` naming the check that never finished and how long the check
  set sat unchanged.

  **The three caps above are NAMED here, never numbered.** The **STRIKE CAP**, the **CI STALL CAP** and the
  **REFETCH CAP** each carry their value at exactly ONE defining site, and `stage-2-ci.md`, "THE LIVENESS
  COUNTERS", is the one table that maps each counter to its cap and to that site. Never retype a value here.
- `ci_reason` — the durable **MACHINE-BLOCKER REASON**: what campaign cannot get past without a human, in
  a form that human can act on. It is the **question** `blocker_ruling` **answers**, and the escalation
  prompt is built from it — so, like every park field, it lives on disk: a fresh agent instance that lost
  it cannot even ask. A park that cannot name its blocker is not actionable.

  **"`ci` is not green because X" is ONE CLASS of it, not the whole of it.** The `ci_` prefix is
  historical and **understates** the field: it is also written at machine-blocker parks where **`ci` is
  `green`**. The name is kept — renaming it would churn the schema and every write site for cosmetics —
  so the definition, not the name, is what binds. Its write sites, both classes:
  - **CI blockers** (`stage-2-ci.md`, "ESCALATE" — `ci` is `red` or `pending`): the DECIDE bullet that
    matched and the row that made it match — which required check never registered, which check has been
    `RUNNING` since when without the check set moving, which enum value was unrecognized, which VERIFY
    rule the snapshot failed, which read was denied.
  - **MERGE-PRECONDITION blockers** (`stage-3-merge.md`, "The merge precondition" — reached only with
    **`ci = green`**): the PR is a **draft**, `mergeStateStatus` = `BLOCKED`, or an **unrecognized**
    `mergeStateStatus` — the offending value named **verbatim**.

  Those two are the write sites that exist today, **not a bound on the set**: the class is the
  **property** — *campaign cannot move this PR without a human* (`status`, below, `awaiting-user` class
  2) — so **any** future park with that property writes its reason here, with no edit to this bullet.
- `blocker_ruling` — durable record of the user's answer to a **machine-blocker park** (the `status`
  taxonomy below): `-` (none yet) | `retry@<iso>` | `abort@<iso>`. It is the **answer** to the question
  `ci_reason` **asks**, and it exists for the same reason `api_approval` does: a wake may be a fresh
  agent instance, so an answer held only in context is an answer the user is asked for twice. `retry`
  unparks with the liveness counters cleared; `abort` goes terminal `aborted`. The unpark is
  `loop-control.md` step 3, "Only the user's answer unparks a PR".

  **DURABLE *and* SPENT EXACTLY ONCE — one ruling answers exactly ONE park.** It is set back to `-` when a
  machine-blocker park is **ENTERED** and when a `retry` is **CONSUMED** (`stage-2-ci.md`, "THE RULING IS
  CONSUMED EXACTLY ONCE" — that is the owning definition). That is what **scopes** a ruling to its park: a
  ruling sitting on a **parked** row can only have been written while **that** park was open, so a stale
  `retry` can never unpark a **later** blocker with no fresh user answer. `abort@<iso>` is **never**
  cleared — it goes terminal, and a terminal row is never re-parked, so it stays as the record of why.
  A **counter reset never touches it**: it is not one of the liveness counters.

  These live **on disk, not in the driver's head**: a wake may be a fresh agent instance, and a counter —
  or a ruling — that dies with the context never reaches its cap.
- `attempts` — task attempts so far (for the retry-once bailout).
- `started` — wall-clock start of the current attempt (for the 1-hour cap).
- `api_approval` — durable record of the user's decision on this PR's API-changing fix: `-`
  (not an API change, or not yet decided) | `approved@<iso>` | `declined@<iso>`. Written the moment
  the user answers, so a later wake — or a fresh agent that adopted the run — reads it and never
  re-asks about a PR already decided. It records the decision (an input); `status` stays the
  live position, so the two never contradict: `approved` pairs with the PR back in normal
  gate flow, `declined` with a terminal `aborted`. A one-off approval lands here only; it never flips
  the run-wide `api_changes` header.
- `status` — `in_review` → `merged`, or `aborted`; plus two **user-parked** (non-terminal)
  statuses. **BOTH parked statuses FREEZE that PR until the user answers**: while `status` is
  `awaiting-api` or `awaiting-user`, take **no action that MUTATES the PR** — never launch a review
  pass, a CI fix, a review fix, or a merge for it, and never rebase it, refresh its base, push to it, or
  relabel it (`loop-control.md` step 3, "parked-status guard" — the property, of which those are only
  examples; `stage-3-merge.md` binds both the merge and the post-merge reconcile). The park does
  not raise `reviews_ok`, so the guard reads **`status`** — never `reviews_ok`/`ci`/`mergeable` alone,
  which would re-review a parked PR and merge it without the ruling. **The park does not change the watch
  policy either way** (observing is not mutating): the watch follows `stage-2-ci.md`, "WATCH ONLY WHAT CAN
  MOVE" — alive while a row is still `RUNNING`, **not** relaunched once CI has settled. Parking never
  stops a warranted watch, and never starts an unwarranted one. The other PRs keep being driven; the
  user's answer unparks the PR **to the `status` that answer dictates** — a **RESUME** answer (`approved`,
  a standoff ruling, `retry`) to `in_review`, with normal dispatch resuming on the next wake; a
  **TERMINAL** answer (`declined`, `abort`) to `aborted`, which never resumes. Per class, below —
  and `loop-control.md` step 3, "Only the user's answer unparks a PR", owns the mapping.
  - `awaiting-api` — parked for the user to approve an API-changing fix. Resolves via `api_approval`:
    `approved` returns the PR to the normal flow, `declined` makes it `aborted` (terminal).
  - `awaiting-user` — parked for the user to adjudicate. **Two CLASSES, each with its OWN durable answer
    record** (the class is a **property**, not a list of sites — any future park where campaign cannot
    make progress without a human is a machine blocker and inherits class 2's exit):
    1. **A REVIEW STANDOFF** — a finding the orchestrator REFUTED in the tree and a **fresh reviewer
       re-raised anyway** (`stage-2-review-gate.md`, "Audit every finding before you fix it"). A REFUTED
       finding does **NOT** park by itself — it is committed as an inline refutation and the next
       reviewer judges it; only the re-raise parks. **Answered into** `<rundir>/audit-<pr>-<n>.md`: ruled
       **invalid** → back to the normal flow; ruled **valid** → back to the normal flow with that finding
       fixed like a CONFIRMED one.
    2. **A MACHINE BLOCKER — campaign cannot move this PR without a human.** `ci_reason` **names** it.
       Non-exhaustively: **CI has SETTLED and is still not green** (`settled_strikes` at its cap), a check
       that **never stopped `RUNNING`** while nothing in the check set moved (`ci_stalled_since` at the CI
       STALL CAP — a hung runner, a dead reporter, a required check that queues and never starts), a
       snapshot that stayed **UNUSABLE** (`unusable_refetches` at its cap), a check carrying an
       **unrecognized enum value**, a merge `BLOCKED` for a cause campaign cannot enumerate, an
       **unrecognized `mergeStateStatus`**, or a **draft** PR (`stage-2-ci.md`, "SETTLED", "RUNNING-STALL"
       and "UNUSABLE — the refetch is BOUNDED"; `stage-3-merge.md`, "The merge precondition"). **This is
       the exit from `pending` — in BOTH of its shapes**, the settled one and the forever-`RUNNING` one;
       without it, a stuck PR spins forever and no one is ever told. **Answered into**
       `blocker_ruling`: `retry@<iso>` → back to `in_review` **with the liveness counters cleared** (else
       it re-escalates on its first derivation) **and the ruling itself SPENT back to `-`** (a ruling is
       consumed exactly once — `stage-2-ci.md`, "THE RULING IS CONSUMED EXACTLY ONCE"; entering this park
       clears it too, so it can never be answered by a **previous** park's ruling), `abort@<iso>` →
       terminal `aborted` (not cleared — terminal rows are never re-parked).

    Same park mechanics as
    `awaiting-api` for both: `reviews_ok` stays 0, no review pass is launched for this PR, the other PRs
    keep being driven, and the answer folds in as its own wake (`loop-control.md` step 3, "Only the
    user's answer unparks a PR" — the owning definition of the record + unpark for **every** park class).
    NEVER park without surfacing the question, and NEVER park into a state whose exit is undefined.

### Review-pass artifacts — use `scripts/review-pass.py`

The plan, the `pass_identity`, the progress events and the read that decides **whether a pass counts** are
one artifact set with one owner. `scripts/review-pass.py` READS every line of it — whatever wrote that
line — and WRITES every line the emit-only rule does not exempt; the reviewer's `emit-progress.py` (CLI
unchanged) is a door into the same owner. **Never hand-parse one of those files, and never hand-write a
line the tool writes for you** — the hand-written `pass_identity` is how a truncated SHA reached real
state, and the hand-rolled tally is the same "read it by eye and write down the answer" that produced a
false `ci = green`. `stage-2-review-gate.md` owns the rules — including the emit-only rule and the ONE
event it exempts — the subcommands, and the four verdicts `verify`
returns; resolve the script at `<skill-dir>/scripts/review-pass.py` and pass that path to subtasks, exactly
as with `ledger.py` below.

### Editing the ledger — use `scripts/ledger.py`

`scripts/ledger.py` is the **sanctioned way** to read and write `state.jsonl` (both the header record
and the per-PR row records) **by FIELD NAME**. The script owns the schema (the header fields and the
row fields above) in ONE place, so agents and subtasks **must not hand-edit the JSONL**. Address fields
by name and the script keeps the store canonical.

This mirrors how `stage-2-review-gate.md` treats `emit-progress.py`: the file stays **plaintext and
human-readable** JSONL (`cat`/`grep`/`jq`-able), the accessor just owns the schema and writes the
canonical layout — the store is now JSONL owned by the script. `state.jsonl` is still a cache reconciled
against ground truth every wake (above) — the accessor changes *how* records are written, not what the
ledger means.

Resolve its absolute path as `<skill-dir>/scripts/ledger.py` (skill dir = the directory holding the
campaign `SKILL.md`) and pass that path to subtasks, exactly as with `emit-progress.py`. Subcommands
(`<state.jsonl>` = this run's `<rundir>/state.jsonl`):

```
ledger.py --file <state.jsonl> header get <field>                 # read a run-config header field
ledger.py --file <state.jsonl> header set <field> <value>         # set a run-config header field
ledger.py --file <state.jsonl> add-row --pr N [--<field> <val> …] # register a row (refuses a duplicate pr; unset fields default)
ledger.py --file <state.jsonl> set --pr N --<field> <val> [--<field> <val> …]  # update named fields on the row for PR N
ledger.py --file <state.jsonl> get --pr N [--field <f>]           # print the row as JSON, or one field
ledger.py --file <state.jsonl> list [--where <field>=<val>]       # print matching rows' pr numbers (all if no filter)
ledger.py --file <state.jsonl> table [--all] [--fields <f>,<f>,…] # print run header + the live rows as an aligned table (read-only)
```

`table` is the user-facing status view: the end-of-wake report renders it whenever the run goes back
to waiting (`loop-control.md`, "Reschedule or exit"). It renders state and decides nothing — no gate
logic, no derived values.

**`table` is a PROJECTION — NEVER a source to read a value back out of.** Its output is *formatted for a
human*, and the formatting is lossy in four ways:

- **It shows only SOME ROWS.** The default view **hides finished work** — a row that reached the run's
  *successful* terminal state and needs nothing further from anyone. It is **not** "terminal rows": an
  `aborted` PR is terminal too and it **stays visible**, because it is the run's *unfinished business* —
  left open for its owner, with an `abort-<id>.md` a human is meant to read. Everything still in play,
  parked included, always shows. **The omission is NEVER silent**: whenever the view drops a row, `table`
  prints an out-of-band line stating **how many** rows it hid and the flag that reveals them, and **`--all`
  shows every row** (it composes with `--fields` — one picks the rows, the other the columns). So **a
  missing ROW is not a missing PR**, exactly as a missing column is not a missing value. Which statuses the
  default hides is owned by `TABLE_HIDDEN_STATUSES` in `scripts/ledger.py` and named **live** in `ledger.py
  table --help`; when this paragraph and that output disagree, **the script is right**.
- **It shows only SOME fields.** The default view is a **SUBSET** of the row fields, and **NEITHER the
  shown nor the hidden set is enumerated here** — both are **DERIVED from the live schema**, never retyped
  on this page. The shown set is `TABLE_DEFAULT_FIELDS` in `scripts/ledger.py`, printed **live** by
  `ledger.py table --help` (it names the defaults). The **hidden set is everything else** — `ROW_FIELDS`
  minus that projection — and every row field is printed by `ledger.py … get --pr N`, which projects onto
  the full `ROW_FIELDS`. Anything hidden is **shown on request** with `--fields <f>,<f>,…`. So **a missing
  COLUMN is not a missing VALUE** — the field is in the store, the default projection just does not print
  it. **The script is the owner; when this page and its output disagree, the script is right.** A
  hand-typed list of hidden fields would be **stale the next time a row field is added — by a change its
  author never sees**, and that is exactly how this paragraph broke before: it named seven hidden fields,
  a later PR added six more row fields, and neither author touched the other's work.
- **It shortens the SHA.** `table` prints `head_sha` truncated to its first **8 characters**. This is a
  **display-only** truncation and applies to **`table` alone** — nothing else in campaign ever shortens a
  SHA. The stored value, and the one every other subcommand returns, stays the full 40-char `headRefOid`:
  `ledger.py … get --pr N --field head_sha` prints all 40.
- **It escapes cell values.** Rendered raw, a value could forge the very layout it is printed into — an
  extra column, an extra row, a run-config line, the empty-ledger marker — or, carrying whitespace at its
  edges, be eaten by the column padding and come out looking like a DIFFERENT value. So `table`
  backslash-escapes before printing: the escaped text is what you see, the raw value is what is stored.
  **`escape_cell()` in `scripts/ledger.py` owns which characters are escaped and how, and its `self-test`
  fixtures pin it** — that function is the definition, and this page deliberately does not restate it (a
  list here goes stale the moment one is added). What you may rely on: the rendering is **injective** — two
  different values NEVER print as the same cell, so one row can never be read as another — and it reserves
  the leading-`#` namespace for **every** out-of-band line the table prints (the `# <field>: …` run-config
  block, the empty-grid markers, the hidden-count line), so **no row can ever forge one**. That is what
  makes the omission notice trustworthy, and it is why an empty grid always **says which empty it is** —
  a ledger that holds nothing and a ledger whose every row is hidden print **different** markers, so an
  end-of-run table where everything merged can never be misread as a campaign that adopted no PRs.

So **read the ledger by FIELD NAME through `ledger.py get`** (or `list`) — **never by parsing the table**.
A SHA (or any value) recovered from `table`'s grid is a truncated, escaped rendering, and feeding one back
into a command or writing it to the store is a bug: this repo has already had a fabricated 8-char SHA
written into a real ledger, and a truncated SHA escape into a command.

It rejects an unknown field name (listing the valid ones), refuses a duplicate `pr` on `add-row`,
errors on a missing row for `set`/`get`, and creates the file with the header if it is missing. It also
validates the store on every read and refuses a corrupt ledger — a malformed JSON line, a record that
is not a JSON object or has a missing/unknown `type`, a duplicate `pr` row, or a header that is missing,
not first, or repeated — reporting the offending line number rather than silently dropping records. On
read it also normalizes every field value to a string (so an on-disk numeric/boolean `pr` matches the
string key) and recomputes each row's derived `id` from its `pr`, never trusting the `id` on disk. A
non-zero exit with a clear stderr message means the input was rejected — fix it and re-run.

---
