## File locations

Everything under the run's own dir `<rundir>` = `.gauntlet/tmp/<run-id>/` (create at the start
of a fresh run; on resume, reuse the run's existing dir). Per-run dirs are what keep concurrent runs'
files from colliding — see "Run identity and concurrency".

| File (under `<rundir>`) | Contents |
|------|----------|
| `state.md` | Live per-PR ledger — a **cache/hint**, not the source of truth (see below) |
| `pr-<pr>.json` | `gh pr view` snapshot captured at adoption (PR facts the ledger row is built from) |
| `prs.json` | Batched `gh pr list` snapshot of this run's PRs — the per-wake reconcile input (Loop control) |
| `lease.json` | This run's active-driver lease (`{agent, updated}`; see "Run lease") |
| `review-<pr>-<n>.txt` | The reviewer's PR review output, round `n` |
| `review-<pr>-<n>.plan.jsonl` | Orchestrator-authored review work units for round `n` |
| `review-<pr>-<n>.progress.jsonl` | Reviewer progress events against the plan for round `n` |
| `ci-<pr>.txt` | Latest `gh pr checks` snapshot for a PR (re-polled after the watch, not the watch stream) |
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

### The ledger — `state.md`

One row per adopted PR. It is a **cache**, not the authoritative state — **ground truth is
GitHub via `gh`, plus local worktrees** (`gh pr list/view` for PRs and merged/open state, each PR's
`headRefOid` from `gh` — keyed by PR number — for the live head SHA, `gh pr checks` for live CI, and
the `review-<pr>-<n>.txt` files for which verdicts exist on which SHA). `git rev-parse HEAD` is used
ONLY to validate/read an existing worktree when one is checked out — never as the primary source of a
PR's live head (an adopted PR may have no local branch/worktree at all). Every wake re-derives what's
due from those, then refreshes this file. So a stale or half-written ledger is self-healing — never
act on it without reconciling against gh (and any existing worktree) first.

The file opens with a short run-config header (`run_id`, `base_branch`, `api_changes`, `reviewer`,
`branch_ownership` — re-read every wake, see Constraints and "Run identity and concurrency"), then one
row per adopted PR:

```
run_id: g260704-0915-a3f29c1b  # this run's identity — namespaces its dir/label/wakes (set once)
base_branch: main       # the adopted PRs' baseRefName — the branch they merge into & diffs measure against (set once; see "Base branch")
api_changes: ask        # ask | allowed (run-wide; set once from the invocation)
reviewer: default       # default (Claude subagents) | codex | <other> — the selected reviewer (set once; see "The reviewer")
branch_ownership: declined  # declined | granted — may campaign delete the adopted PR's REMOTE head branch on merge (local worktree/branch cleanup is ALWAYS per-PR worktree_owned/branch_owned; set once; see "PR adoption")

id | slug | branch | worktree | worktree_owned | branch_owned | pr | head_sha | reviews_ok | ci | tier | attempts | started | api_approval | status
```

Header field notes (the header fields above; per-row fields follow):

- `branch_ownership` — run-wide consent to delete the adopted PR's **remote** head branch on merge:
  `declined` (default) | `granted`. **Resolved once at run start** by the same explicit > preference >
  default precedence as `reviewer` (an explicit user instruction in the invocation — natural language,
  like naming the reviewer, NOT a CLI flag — > a stored user preference — memory entry / `CLAUDE.md` /
  config — that grants branch ownership > default `declined`; see "PR adoption"), then re-read every
  wake like the other header fields — never re-derived mid-run. It governs **only** the remote branch.
  `declined` is the safe floor: campaign never deletes the remote head branch. `granted` is opt-in and
  lets the merge delete the remote head branch (`--delete-branch`). **Local cleanup is identical in both
  modes** and never keyed off `branch_ownership`: the worktree is removed only when `worktree_owned =
  yes` and the local branch deleted only when `branch_owned = yes`; a reused worktree, the root/main
  checkout, and a reused local branch are **never** removed regardless of `branch_ownership` (see
  "Stage 3 — Merge"). An unattended run with no stored grant stays `declined`.

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
- `ci` — `green` / `red` / `pending` / `none` for `head_sha`.
- `attempts` — task attempts so far (for the retry-once bailout).
- `started` — wall-clock start of the current attempt (for the 1-hour cap).
- `api_approval` — durable record of the user's decision on this PR's API-changing fix: `-`
  (not an API change, or not yet decided) | `approved@<iso>` | `declined@<iso>`. Written the moment
  the user answers, so a later wake — or a fresh agent that adopted the run — reads it and never
  re-asks about a PR already decided. It records the decision (an input); `status` stays the
  live position, so the two never contradict: `approved` pairs with the PR back in normal
  gate flow, `declined` with a terminal `aborted`. A one-off approval lands here only; it never flips
  the run-wide `api_changes` header.
- `status` — `in_review` → `mergeable` → `merged`, or `aborted`; plus `awaiting-api`
  while parked for the user to approve an API-changing fix. That park resolves via `api_approval`:
  `approved` returns the PR to the normal flow, `declined` makes it `aborted` (terminal).

### Editing the ledger — use `scripts/ledger.py`

`scripts/ledger.py` is the **sanctioned way** to read and write `state.md` (both the header block and
the per-PR rows) **by FIELD NAME**. The script owns the schema (the header fields and the row column
order above) in ONE place, so agents and subtasks **must not hand-edit rows by column position** —
adding a column shifts every positional offset and silently corrupts hand-edited rows. Address fields
by name and the script keeps the layout correct.

This mirrors how `stage-2-review-gate.md` treats `emit-progress.py`: the file stays **plaintext and
human-readable** (`cat`/grep-able), the accessor just owns the schema and writes the canonical layout.
`state.md` is still a cache reconciled against ground truth every wake (above) — the accessor changes
*how* rows are written, not what the ledger means.

Resolve its absolute path as `<skill-dir>/scripts/ledger.py` (skill dir = the directory holding the
campaign `SKILL.md`) and pass that path to subtasks, exactly as with `emit-progress.py`. Subcommands
(`<state.md>` = this run's `<rundir>/state.md`):

```
ledger.py --file <state.md> header get <field>                 # read a run-config header field
ledger.py --file <state.md> header set <field> <value>         # set a run-config header field
ledger.py --file <state.md> add-row --pr N [--<field> <val> …] # register a row (refuses a duplicate pr; unset fields default)
ledger.py --file <state.md> set --pr N --<field> <val> [--<field> <val> …]  # update named fields on the row for PR N
ledger.py --file <state.md> get --pr N [--field <f>]           # print the row as JSON, or one field
ledger.py --file <state.md> list [--where <field>=<val>]       # print matching rows' pr numbers (all if no filter)
```

It rejects an unknown field name (listing the valid ones), refuses a duplicate `pr` on `add-row`,
errors on a missing row for `set`/`get`, and creates the file with the header if it is missing. A
non-zero exit with a clear stderr message means the input was rejected — fix it and re-run.

---
