## File locations

Everything under the run's own dir `<rundir>` = `.gauntlet/tmp/<run-id>/` (create at the start
of a fresh run; on resume, reuse the run's existing dir). Per-run dirs are what keep concurrent runs'
files from colliding — see "Run identity and concurrency".

| File (under `<rundir>`) | Contents |
|------|----------|
| `state.jsonl` | Live per-PR ledger — a **cache/hint**, not the source of truth (see below) |
| `pr-<pr>.json` | `gh pr view` snapshot captured at adoption (PR facts the ledger row is built from) |
| `prs.json` | Batched `gh pr list` snapshot of this run's PRs — the per-wake reconcile input (Loop control) |
| `lease.json` | This run's active-driver lease (`{agent, updated}`; see "Run lease") |
| `review-<pr>-<n>.txt` | The reviewer's PR review output, round `n` (launch attempt 1) |
| `review-<pr>-<n>.plan.jsonl` | Orchestrator-authored review work units for round `n` (per-pass — a relaunch reuses it) |
| `review-<pr>-<n>.progress.jsonl` | Reviewer progress events against the plan for round `n` (launch attempt 1) |
| `review-<pr>-<n>.a<k>.txt` / `.a<k>.progress.jsonl` | Same two artifacts for **launch attempt `k ≥ 2`** — a relaunched pass writes here, never over attempt 1's files, so a killed-but-alive attempt can't corrupt the live one. Only the attempt named in the active `pass_identity` is read or counted (see `stage-2-review-gate.md`) |
| `ci-<pr>-<sha>.txt` | The PR's check state at **`<sha>`**, covering **BOTH check families** — check runs **and** legacy commit statuses (never the watch stream, never an unpinned `gh pr checks` snapshot, and **never `/check-runs` alone**, which cannot see a failing Jenkins/CircleCI **commit status**). **TWO fetches, and each family is judged ONLY from the one that carries both its IDENTITY and its RESULT** (`stage-2-ci.md`, "The same-observation rule"): (1) the **SHA-pinned `--paginate` REST check-runs** fetch → `checkrun\t<head_sha>\t<name>\t<app_id>\t<status>\t<conclusion>` (one row = the commit, the producer **and** the result — **the only source of a check-run verdict**; `-` app_id = unbound); (2) the **rollup** (`gh pr view <pr> --json headRefOid,statusCheckRollup`, re-polled after the watch) → `status\t<context>\t<state>` for the family (1) cannot see, plus `rollup-checkrun\t<name>` **WITNESS** rows used **only** for the cross-fetch agreement test, **never as a verdict**. **SHA-SCOPED in the FILENAME, stamped in its FIRST LINE** (`# sha: <full sha>`, from the same payload as the rollup rows) **and on every `checkrun` row** (stamped by GitHub per run). Written **ATOMICALLY**: both fetches go to a temp file **inside `<rundir>`** (same filesystem → `mv` is an atomic rename) and are promoted here only if **every** fetch exits 0, so this file is **always a COMPLETE snapshot**. **Before parsing, the consumer MUST verify that every SHA in it (the `# sha:` line AND every `checkrun` row's) equals the ledger's current `head_sha`, AND that the two fetches agree on the SET of check-run names** — the fetches are taken at **different times**, so a run present in one and absent from the other means the state was still **MOVING**. Absent, partial, mismatched, disagreeing, or a rollup at its context cap (truncated = partial) → `ci = pending`, relaunch the watch, **NEVER green**. A snapshot for a superseded SHA is expected (the head advances under a running watch) and is simply discarded. It lists only the checks that had **REGISTERED** when it was taken, so it is **NOT** self-evidently the whole set: every **declared** required check must be present in it — **matched on producer where the declaration binds an app, with presence and success read off the SAME row** — before it may be read as green (`stage-2-ci.md`) |
| `audit-<pr>-<n>.md` | The orchestrator's audit of round `n`'s findings — CONFIRMED / ADJUSTED / REFUTED, each with evidence. A REFUTED finding's reasoning is recorded here **and** written into the tree as an inline comment at the site, committed like any other change (`stage-2-review-gate.md`, "Audit every finding before you fix it") |
| `abort-<id>.md` | Detailed log for an aborted PR-task |

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
`headRefOid` from `gh` — keyed by PR number — for the live head SHA, **both check families** (check runs
**and** commit statuses) pinned to that SHA for live CI, and
the **active launch attempt's** review output files for which verdicts exist on which SHA —
`review-<pr>-<n>.txt` for attempt 1, `review-<pr>-<n>.a<k>.txt` after a relaunch, counting only the
attempt named in that pass's `pass_identity`, so a relaunch's verdict is never missed and a dead
attempt's is never counted). `git rev-parse HEAD` is used
ONLY to validate/read an existing worktree when one is checked out — never as the primary source of a
PR's live head (an adopted PR may have no local branch/worktree at all). Every wake re-derives what's
due from those, then refreshes this file. So a stale or half-written ledger is self-healing — never
act on it without reconciling against gh (and any existing worktree) first.

The store is **JSONL** — one JSON object per line, `cat`/`grep`/`jq`-able. The first line is the
run-config header record (`{"type": "header", …}` — `run_id`, `base_branch`, `api_changes`, `reviewer`,
re-read every wake, see Constraints and "Run identity and concurrency"); each
following line is one adopted PR's row record (`{"type": "row", …}`). Every record is **self-describing**
— fields are keyed by NAME, never by column position:

```
{"type": "header", "run_id": "g260704-0915-a3f29c1b", "base_branch": "main", "api_changes": "ask", "reviewer": "codex"}
{"type": "row", "id": "pr41", "slug": "fix-null-deref", "branch": "fix-null-deref", "worktree": ".worktrees/fix-null-deref", "worktree_owned": "yes", "branch_owned": "yes", "pr": "41", "head_sha": "a3f29c1b", "reviews_ok": "2", "ci": "green", "required_set": "declared", "tier": "STANDARD", "attempts": "1", "started": "2026-07-04T09:15:00Z", "api_approval": "-", "status": "mergeable"}
{"type": "row", "id": "pr52", "slug": "add-retry-flag", "branch": "add-retry-flag", "worktree": ".worktrees/add-retry-flag", "worktree_owned": "no", "branch_owned": "no", "pr": "52", "head_sha": "b1c2d3e4", "reviews_ok": "0", "ci": "pending", "required_set": "unknown", "tier": "HIGH", "attempts": "0", "started": "-", "api_approval": "-", "status": "in_review"}
```

Header-record fields: `run_id` (this run's identity — namespaces its dir/label/wakes; set once),
`base_branch` (the adopted PRs' baseRefName — the branch they merge into & diffs measure against; set
once, see "Base branch"), `api_changes` (`ask` | `allowed`, run-wide; set once from the invocation),
`reviewer` (`default` (Claude subagents) | `codex` | `<other>` — the selected reviewer; set once, see
"The reviewer").

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
  then carry `reviews_ok` forward to the new `head_sha` and set `ci = pending`.
- `reviews_ok` — number of fresh, context-isolated SATISFIED verdicts recorded against this PR's
  current content. Target = `required(tier)`: **1 if `tier == TRIVIAL`, else 2** ("Adaptive review
  tiers").
- `tier` — the adaptive review tier derived from `head_sha`: `TRIVIAL` | `STANDARD` | `HIGH`. Re-derived
  every wake and re-triaged on any content change; drives `required(tier)` and the review depth.
- `ci` — `green` / `red` / `pending` / `none` for `head_sha`. `green` requires a complete, SHA-verified
  snapshot **covering BOTH check families** (check runs **and** commit statuses — a check-runs-only
  snapshot cannot see a failing Jenkins status) with **≥1 row, every check run `COMPLETED`+`SUCCESS`, every
  commit status `SUCCESS`** (`ERROR` is a **failure**), **the two fetches agreeing on the head SHA and on
  the set of check runs** (they are taken at **different times**; a disagreement means the state was still
  moving → `pending`), **AND every DECLARED required check present in it and successful — matched on the
  check run's `app_id` wherever the declaration binds an app, with its PRESENCE and its SUCCESS read off
  the SAME row of the SAME fetch** (a same-named check from another producer does **not** satisfy it; where
  no app is bound, any producer of that name does). **NEVER join rows across fetches to reach a verdict**
  (`stage-2-ci.md`, "The same-observation rule"). A declared required check that has not registered →
  `pending`, never `green`. `ci = green` alone **never** authorizes a merge: Stage 3 also requires GitHub's
  `MERGEABLE` + `mergeStateStatus == CLEAN`.
- `required_set` — the outcome of the **required-check-set read** for this PR's base, **persisted** so the
  three states survive a context loss or a driver resume: `declared` (protection ∪ rulesets read
  **succeeded**, union **non-empty** — registration completeness is **PROVABLE**, and green requires every
  required check present-and-successful) | `none` (**BOTH** reads genuinely **succeeded and came back
  empty** — a classic 404 counts only when `.permissions.admin == true` proves the token may actually look;
  registration completeness is **unprovable**, and green means only *"every check that had registered by
  the time we looked had passed"*) | `unknown` (**CANNOT READ** — any unexplained error on either endpoint;
  the expected set is **UNKNOWN, which is NOT "none declared"**: never claim registration completeness,
  never imply no required checks exist). **`unknown` is the DEFAULT** — the fail-safe is uncertainty, never
  a claim of completeness. Written every wake the required set is read, through `scripts/ledger.py … set
  --pr <N> --required-set <state>`; read back by the final report (`bailout-and-final-report.md`), which
  states the CI verification gap **from this field, not from memory** (`stage-2-ci.md`, "Three states,
  never two" and "The registration gap").
- `attempts` — task attempts so far (for the retry-once bailout).
- `started` — wall-clock start of the current attempt (for the 1-hour cap).
- `api_approval` — durable record of the user's decision on this PR's API-changing fix: `-`
  (not an API change, or not yet decided) | `approved@<iso>` | `declined@<iso>`. Written the moment
  the user answers, so a later wake — or a fresh agent that adopted the run — reads it and never
  re-asks about a PR already decided. It records the decision (an input); `status` stays the
  live position, so the two never contradict: `approved` pairs with the PR back in normal
  gate flow, `declined` with a terminal `aborted`. A one-off approval lands here only; it never flips
  the run-wide `api_changes` header.
- `status` — `in_review` → `mergeable` → `merged`, or `aborted`; plus two **user-parked** (non-terminal)
  statuses. **BOTH parked statuses FREEZE that PR until the user answers**: while `status` is
  `awaiting-api` or `awaiting-user`, take **no action that MUTATES the PR** — never launch a review
  pass, a CI fix, a review fix, or a merge for it, and never rebase it, refresh its base, push to it, or
  relabel it (`loop-control.md` step 3, "parked-status guard" — the property, of which those are only
  examples; `stage-3-merge.md` binds both the merge and the post-merge reconcile). The park does
  not raise `reviews_ok`, so the guard reads **`status`** — never `reviews_ok`/`ci`/`mergeable` alone,
  which would re-review a parked PR and merge it without the ruling. The PR's **CI watch keeps running**
  (observing is not mutating). The other PRs keep being driven; the user's answer sets `status`
  back to `in_review` and normal dispatch resumes on the next wake.
  - `awaiting-api` — parked for the user to approve an API-changing fix. Resolves via `api_approval`:
    `approved` returns the PR to the normal flow, `declined` makes it `aborted` (terminal).
  - `awaiting-user` — **standoff only**: parked for the user to adjudicate a finding the orchestrator
    REFUTED in the tree and a **fresh reviewer re-raised anyway** (`stage-2-review-gate.md`, "Audit every
    finding before you fix it"). A REFUTED finding does **NOT** park by itself — it is committed as an
    inline refutation and the next reviewer judges it; only the re-raise parks. Same park mechanics as
    `awaiting-api`: `reviews_ok` stays 0, no review pass is launched for this PR, the other PRs keep
    being driven, and the answer folds in as its own wake. The user ruling the finding **invalid**
    returns the PR to the normal flow; ruling it **valid** returns it to the normal flow with that
    finding fixed like a CONFIRMED one. NEVER park without surfacing the question.

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
```

It rejects an unknown field name (listing the valid ones), refuses a duplicate `pr` on `add-row`,
errors on a missing row for `set`/`get`, and creates the file with the header if it is missing. It also
validates the store on every read and refuses a corrupt ledger — a malformed JSON line, a record that
is not a JSON object or has a missing/unknown `type`, a duplicate `pr` row, or a header that is missing,
not first, or repeated — reporting the offending line number rather than silently dropping records. On
read it also normalizes every field value to a string (so an on-disk numeric/boolean `pr` matches the
string key) and recomputes each row's derived `id` from its `pr`, never trusting the `id` on disk. A
non-zero exit with a clear stderr message means the input was rejected — fix it and re-run.

---
