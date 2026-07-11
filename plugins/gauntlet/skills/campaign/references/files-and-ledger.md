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
| `.gauntlet/tmp/<run-id>/` | Ephemeral. Safe to wipe once a run reaches a terminal state. |
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

The file opens with a short run-config header (`run_id`, `base_branch`, `api_changes`, `reviewer` —
re-read every wake, see Constraints and "Run identity and concurrency"), then one row per adopted PR:

```
run_id: g260704-0915-a3f29c1b  # this run's identity — namespaces its dir/label/wakes (set once)
base_branch: main       # the adopted PRs' baseRefName — the branch they merge into & diffs measure against (set once; see "Base branch")
api_changes: ask        # ask | allowed (run-wide; set once from the invocation)
reviewer: default       # default (Claude subagents) | codex | <other> — the selected reviewer (set once; see "The reviewer")

id | slug | branch | worktree | worktree_owned | pr | head_sha | reviews_ok | ci | tier | attempts | started | api_approval | status
```

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
  `worktree` (see "PR adoption"); read by Stage 3 cleanup so it never deletes a checkout/branch the
  user owns.
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

---
