## File locations

Everything below is under the run's own absolute `<rundir>`, derived from the invocation's typed
`RepositoryContext` by `runtime-adapter.md`'s run-directory operation (create at fresh-run start; on
resume, reuse the existing dir). Per-run dirs are what keep concurrent runs' files from colliding — see
"Run identity and concurrency".

| File (under `<rundir>`) | Contents |
|------|----------|
| `state.jsonl` | Live per-PR ledger — a **cache/hint**, not the source of truth (see below) |
| `pr-<pr>.json` | `gh pr view` snapshot captured at adoption (PR facts the ledger row is built from) |
| `prs.json` | Batched snapshot of this run's PRs — adoption/discovery + per-heartbeat reconcile input. **`scripts/reconcile.py fetch` is the ONE executable producer**: it owns query argv, schema validation, scope validation, truncation refusal, and atomic promotion. **`scripts/reconcile.py detect` is the ONE consumer**: it emits per-PR reconcile facts. Routing remains skill policy (`loop-control.md`, "Step 1 — reconcile from ground truth") |
| `lease.json` | This run's active-driver lease — read/written ONLY through `scripts/lease.py` (see "Run lease") |
| `review-<pr>-<n>.prompt.txt` | The reviewer prompt for pass `n`, launch attempt 1, with the verbatim intent and JSON-encoded typed transport record bound as data. `n` is the pass number, spent only by a landed verdict (`runtime-adapter.md`, "Review preparation mapping"). Written only by `scripts/review-dispatch.py prepare`, then passed through `dispatch_native` or `run_argv` — never embedded in shell source (`review-dispatch.md`) |
| `review-<pr>-<n>.txt` | The reviewer's PR review output, pass `n` (launch attempt 1), written by the sole producer assigned in `runtime-adapter.md`'s typed review record. `review-pass.py verify` derives this path from the active progress artifact and owns its strict terminal-result parse |
| `review-<pr>-<n>.plan.jsonl` | Orchestrator-authored review work units for pass `n` (per-pass — a relaunch reuses it). Written through `scripts/review-pass.py plan-add`, never a heredoc |
| `review-<pr>-<n>.progress.jsonl` | Reviewer progress events against the plan for pass `n` (launch attempt 1), opened by `review-dispatch.py prepare` with an orchestrator-owned `pass_identity` validated through `review-pass.py`. **Every line is READ and validated through `scripts/review-pass.py`, and every reviewer-written line goes through one of its doors.** Unit-progress events use `emit-progress.py`; a `plan_amendment_request` uses `emit-amendment.py`, which forwards to `review-pass.py amend`. `stage-2-review-gate.md` owns the artifact rule |
| `review-<pr>-<n>.findings.jsonl` | The FINDINGS of pass `n` (launch attempt 1) — one validated record per finding, written **only** through `scripts/emit-finding.py`. A finding used to be prose in the report, so nothing could check its citation, bound its writer, or ask what it DEFENDED — and therefore nothing could ever **decline** one. Each record ANCHORS to the PR's intent: a `## Purpose` line quoted verbatim, or the actor who can actually write the bad input. `stage-2-review-gate.md` owns the schema and the gating rule |
| `intent-<pr>.md` | **What this PR is FOR** — `## Purpose` / `## Non-goals` / `## Threat model`. Written at adoption (`pr-adoption.md`), from the PR body when it carries one and **authored by the driver** from the diff/title/body when it does not; re-read every heartbeat, never re-derived. It is passed to the reviewer **verbatim**, and it is what the **pass** is measured against: `review-pass.py verify` loads it for **every** pass it judges — including one that found nothing — so a PR with no usable block here earns **no verdicts at all** (`stage-2-review-gate.md`, "Does this pass COUNT?"). **LOCAL and git-ignored — campaign NEVER writes it back to the PR** |
| `review-<pr>-<n>.a<k>.prompt.txt` / `.a<k>.txt` / `.a<k>.progress.jsonl` / `.a<k>.findings.jsonl` | The same four per-attempt artifacts for **launch attempt `k ≥ 2`** — a relaunched pass writes here, never over attempt 1's files, so a killed-but-alive attempt can't corrupt the live one. `review-pass.py verify` derives `.a<k>.txt` from `.a<k>.progress.jsonl`; only that attempt is read or counted. The plan and intent remain per-pass/per-PR |
| `ci-<pr>-<head_sha>.txt` | Latest **SHA-pinned** CI snapshot for a PR — check runs **AND** commit statuses, written **BY THE HEARTBEAT** running **`scripts/ci-status.py derive`** after the watch completes (**the watch never writes it**), promoted atomically, and **stamped with the `head_sha` it describes** (verify the stamp before parsing). Carries a **`source` completion marker per mandatory source**, so a source that was **never queried** is `unusable`, not a silent green (`ci-derivation-spec.md`). Never the watch stream, and never `gh pr checks` — its output carries **no SHA** |
| `audit-<pr>-<n>.jsonl` | Schema-owned audit of the gating findings from the round pass `n`'s landed verdict produced, and any standoff ruling. Read/written only through `scripts/finding-audit.py`; `finding-audit.md`, **Executable audit artifact**, owns the complete procedure |
| `repair-<pr>-<k>.prompt.txt` / `.prompt.txt.manifest.json` | Deterministic reassessment prompt and hash manifest written only by `repair-pass.py bundle` (`repair-pass.md`, "Build the complete reassessment bundle"). The manifest is promoted last and is required by `decide`; a prompt without its valid sidecar cannot authorize a decision |
| `repair-<pr>-<k>.md` | The **reassessment pass**'s decision record for repair `k`: the ONE decision and why (`repair-pass.md`). Written before the decision is recorded, with the matching bundle hash as its first nonblank line and a machine-readable `DECISION: <enum>` line naming the chosen decision. `repair-pass.py decide` refuses a missing/empty record, one not bound to its exact prepared prompt, or one whose `DECISION:` line is absent/duplicated/not-permitted or disagrees with `--decision` |
| `abort-<id>.md` | Detailed log for an aborted PR-task |

**The canonical `prs.json` command — `scripts/reconcile.py fetch` is THE definition.** Every other site
defers here and NEVER reconstructs its internal GitHub query. Resolve script from active `SKILL.md`, then
launch this typed operation (`skill_dir` = absolute directory containing active `SKILL.md`):

```text
run_argv(
  argv: ["python3", path_join(skill_dir, "scripts", "reconcile.py"), "fetch",
         "--project-root", repository.project_root,
         "--run-id", run_id,
         "--output", path_join(<rundir>, "prs.json")],
  cwd: repository.project_root,
  stdin_file: null,
  stdout_file: null
)
```

`fetch` builds the fixed `gh pr list` argv internally. It captures stdout as bytes without shell source,
validates JSON shape + every required field + every row's run label, rejects a response at its result cap,
then promotes the exact bytes through a same-directory atomic rename. Any command, validation, or promotion
failure leaves the previous `prs.json` intact.

Every part is load-bearing:

- **Use the typed project root + output path.** `fetch` refuses relative paths and output paths outside
  project root before it launches GitHub CLI.
- **Use the exact run ID.** `fetch` forms the owner label as an argv value and verifies it on every row.
- **Treat exit 0 as promotion success.** A refusal emits no replacement artifact.

**`fetch` refuses the query's result-cap boundary.** At that exact row count, completeness is unknown and
absence cannot be evidence. Split campaign into smaller runs; NEVER detect against the refused response.

Store ALL reviewer and `gh` output under `<rundir>` first, then Read/Grep it. NEVER `/tmp/`.

All of this is driver bookkeeping, **never repo content — do NOT commit it**: the whole `.gauntlet/`
tree stays git-ignored, and a fix commit stages only the specific source files it changes (explicit
paths, never `git add -A`/`.`).

**Durable cross-run knowledge lives outside `.gauntlet/tmp/`.** The plugin owns one directory at the
repo root, `.gauntlet/` (git-ignored; add `.gauntlet/` to `.gitignore` if missing), split by lifetime:

| Path | Lifetime |
|------|----------|
| `<rundir>/` | Ephemeral scratch, derived by the runtime adapter's owned operation. A **terminal** run's dir is kept so a later bare invocation can detect the *finished* run and offer the finished-run prompt (Loop control step 1); it is otherwise disposable — wiping it only loses that prompt (discovery then falls back to the generic "pass PR numbers" prompt), never carryover, which lives in `history/`. Not wiped mid-run. |
| `.gauntlet/history/<run-id>.md` | Durable. The carryover ledger — the one thing a *new* run needs to remember from old ones. **Written by `scripts/carryover.py distill`** (Loop control step 5, on normal exit), a mechanical projection of the terminal `state.jsonl`; owned by `carryover.md`. |
| `.gauntlet/followups.jsonl` | Durable, and **not run-scoped** — one store, shared by every run. The **follow-up ledger**: work the campaign FOUND and deliberately did not do. Unlike `state.jsonl` it is a **source of truth, not a cache** (nothing can rebuild a lost entry), and it has **many writers** (every concurrent run), so its accessor locks the read-modify-write. Every entry is a **CANDIDATE, never an issue** — **nothing in it may be published without the user's agreement on that specific item**. It is a **WORK QUEUE, not an archive**: an entry is **deleted** once a durable record of it exists elsewhere, and kept when nothing else would remember it (`followups.md` owns when). Owned by `scripts/followups.py`; see `followups.md`. |
| `.gauntlet/review-learnings.jsonl` | Durable, and **not run-scoped** — one store, shared by every run. The **review-learnings store**: refuted/demoted finding CLASSES the driver consults when authoring a PR's intent, so a settled question is not re-litigated (`review-learnings.md`). Like `followups.jsonl` it is a **source of truth, not a cache** (nothing can rebuild a lost entry) with **many writers**, so its accessor locks the read-modify-write. Unlike the follow-up **queue**, it **ACCUMULATES and never auto-deletes** — an entry leaves the consulted set only when the user REVOKES it or the anchored code goes STALE, never by removal, and is KEPT for audit either way. Owned by `scripts/review-learnings.py`; see `review-learnings.md`. |

**Only `.gauntlet/tmp/` is disposable — never `rm -rf .gauntlet/` itself.** That would take **every
durable store in the table above** with it — the carryover history, the follow-up ledger, and the
review-learnings store, which (unlike `state.jsonl`, a cache that re-derives itself from `gh` every
heartbeat) **nothing can rebuild**. Scratch cleanup targets `.gauntlet/tmp/**` and nothing above it.

The history tree keeps **one file per run** (`<run-id>.md`) so concurrent runs never clobber a shared
file. Those durable stores aside — the carryover history, the follow-up ledger, and the review-learnings
store — everything else stays ephemeral under the per-run `<rundir>`. See "Fresh runs and carryover".

### Campaign commits NO file of its own

**Campaign has NO committed file — no repo-root config, nothing.** The whole `.gauntlet/**` tree is
git-ignored driver bookkeeping, and that is the extent of campaign's on-disk footprint.

### The ledger — `state.jsonl`

One row per adopted PR. It is a **cache**, not the authoritative state — **ground truth is
GitHub via `gh`, plus local worktrees** (`reconcile.py fetch` + per-PR `gh pr view` for PRs and state, each PR's
`headRefOid` from `gh` — keyed by PR number — for the live head SHA, a **SHA-pinned** `check-runs` +
commit-`status` fetch for live CI, and
the **active launch attempt's** review output parsed by `review-pass.py verify` —
`review-<pr>-<n>.txt` for attempt 1, `review-<pr>-<n>.a<k>.txt` after a relaunch, counting only the
attempt named in that pass's `pass_identity`, so a relaunch's verdict is never missed and a dead
attempt's is never counted). `git rev-parse HEAD` is used
ONLY to validate/read an existing worktree when one is checked out — never as the primary source of a
PR's live head (an adopted PR may have no local branch/worktree at all). Every heartbeat re-derives what's
due from those, then refreshes this file. So a stale or half-written ledger is self-healing — never
act on it without reconciling against gh (and any existing worktree) first.

The store is **JSONL** — one JSON object per line, `cat`/`grep`/`jq`-able. The first line is the
run-config header record (`{"type": "header", …}` — the run's config, **every field of it** re-read each
heartbeat, never from memory; the fields are the ones `ledger.py`'s `HEADER_FIELDS` declares and "Header-record
fields" below defines, and no reader may keep its own copy of that list); each
following line is one adopted PR's row record (`{"type": "row", …}`). Every record is **self-describing**
— fields are keyed by NAME, never by column position:

```
{"type": "header", "run_id": "g260704-0915-a3f29c1b", "base_branch": "main", "api_changes": "ask", "reviewer": "codex", "required_set": "declared:[{\"context\": \"build\", \"app\": \"-\"}, {\"context\": \"test (3.12, ubuntu)\", \"app\": \"15368\"}]", "skill_version": "0.1.4", "last_activity": "2026-07-04T09:40:00Z", "watchdog_due": "2026-07-04T10:25:00Z", "pending_adoption": "-"}
{"type": "row", "id": "pr41", "slug": "fix-null-deref", "branch": "fix-null-deref", "worktree": "/srv/example-repo/.worktrees/fix-null-deref", "worktree_owned": "yes", "branch_owned": "yes", "pr": "41", "head_sha": "a3f29c1b7d4e6f8091a2b3c4d5e6f708192a3b4c", "reviews_ok": "2", "ci": "green", "tier": "STANDARD", "attempts": "1", "started": "2026-07-04T09:15:00Z", "api_approval": "-", "status": "in_review", "ci_fingerprint": "sha256:9f2c\u2026", "settled_strikes": "0", "unusable_refetches": "0", "ci_stalled_since": "-", "ci_reason": "-", "blocker_ruling": "-", "review_rounds": "3", "ns_streak": "0", "intent": "stated@2026-07-04T09:15:00Z", "pr_origin": "gauntlet", "repair_count": "0", "repair_decision": "-"}
{"type": "row", "id": "pr52", "slug": "add-retry-flag", "branch": "add-retry-flag", "worktree": "/home/example/checkouts/add-retry-flag", "worktree_owned": "no", "branch_owned": "no", "pr": "52", "head_sha": "b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7089a1b", "reviews_ok": "0", "ci": "pending", "tier": "HIGH", "attempts": "0", "started": "-", "api_approval": "-", "status": "in_review", "ci_fingerprint": "sha256:4a71\u2026", "settled_strikes": "1", "unusable_refetches": "0", "ci_stalled_since": "-", "ci_reason": "required check absent: integration-tests", "blocker_ruling": "-", "review_rounds": "5", "ns_streak": "2", "intent": "authored@2026-07-04T09:20:00Z", "pr_origin": "external", "repair_count": "0", "repair_decision": "-"}
```

Read those two rows together and the sensor's whole point is visible: **`pr41` and `pr52` both read
`reviews_ok = 0`-or-`2` and say nothing about how hard they were to get there** \u2014 but `pr52` has taken **5
review rounds** and is **2 NOT SATISFIED deep**, and `pr41` has taken 3 and is converging. Before
`review_rounds` existed, a PR twenty-one rounds into a spiral rendered **identically** to one on its first
round, and the only reader who could tell them apart was a human holding every round in one context.

**`head_sha` is ALWAYS the full 40-char `headRefOid` — never an abbreviation.** The examples above spell
it in full because they are what gets copied. A SHA in the ledger is compared for equality against `gh`'s
live `headRefOid` and pasted into commands; an abbreviated one silently fails both. **NEVER write a
shortened SHA into the store, and never reconstruct one from a display.** The ONLY place a SHA is ever
shortened is `table`'s rendering (below) — that is display-only and does not exist on disk.

Header-record fields: `run_id` (this run's identity — namespaces its dir/label/heartbeats; set once),
`base_branch` (**the LEGACY base fallback** — the base a PR merges into is now **per-row** state, and this
header field is only what a row carrying no explicit base inherits through `effective_base`; see "Base
branch" and the row `base_branch` field below), `api_changes` (`ask` | `allowed`, run-wide; set once from
the invocation), `reviewer` (`default` (per-host cross-engine route with native fallback — see "The
reviewer") | `codex` | `claude` | `<other>` — the selected reviewer; set once, see "The reviewer"),
`required_set` (**the LEGACY required-set fallback** — the required set is now **per-row** state too, and
this header field is only what a row carrying no explicit set inherits through `effective_required_set`;
`stage-2-ci.md`, "What were we expecting to see?", owns the three states and the format; see the row
`required_set` field below), `skill_version` (**which copy of the rules actually governed this run**),
`last_activity` (**when this run last did something MEANINGFUL** — a UTC ISO-8601 stamp the write doors
maintain; the quiet-run sensor `nudge.py` reads, defined below), `watchdog_due` (**the durable
health-pass deadline** — a UTC ISO-8601 stamp `ledger.py watchdog` stamps and reads, defined below),
`pending_adoption` (**the run-intent checkpoint** — the requested PR list recorded at setup, defined
below).

`skill_version` is read at startup from the **running plugin's** `plugin.json` (`SKILL.md`) and stated in
the final report. **It is not cosmetic.** The harness loads this skill from the **installed plugin cache**,
and a merged, version-bumped rule governs **nothing** until that cache refreshes — which is not a
hypothetical: the rule that audits a finding before fixing it was written, reviewed, merged and bumped to
`0.1.3`, and then **did not run**, for days, because the installed copy was still `0.1.2`. **No artifact of
the run recorded which version it was**, so no report could say so, and the failure was invisible until a
human went looking. It defaults to `unknown` for the same reason `required_set` does: *"I did not look"* is
a different fact from any version number, and must never be spelled as one.

The header `required_set` is a property of the **base branch** — and the base is now **per-row** state, so
the canonical required set is a **row field** too (below). This header field is the **legacy fallback**
`effective_required_set` reads for a row that carries no explicit set; an old single-base ledger keeps its
header value and every one of its rows inherits it. It defaults to **`unknown`**, and that default is
**load-bearing, not a placeholder**: `unknown` **cannot go green** (`stage-2-ci.md`), so a run that never
performed the read merges **nothing**. **"I have not looked" and "I looked and there are none" are
different facts**, and the default is the one that claims nothing — a `none` that really meant "I could not
see" is how a green gets recorded for a commit whose required check never registered.

`last_activity` records **when this run last did something MEANINGFUL**, so a fresh-context heartbeat can
tell **how long nothing has moved** without holding the history in its head — the same lesson as
`review_rounds`. It is **maintained internally by the write doors**: any `ledger.py` mutation that changes a
**non-exempt** field to a **new** value stamps it (UTC ISO-8601, second precision) in the **same atomic
write**. What does **not** stamp: a **liveness-counter-only** write (a write touching only THE LIVENESS
COUNTERS — CI-polling bookkeeping that by definition observed no PR change, which is the very "nothing
moved" this field exists to expose), and a **no-op** (a field set to the value it already holds). **No door
writes it directly** — `header set last_activity` is **refused**: it is a **sensor**, the same stance
`review_rounds` takes (a door that can write a sensor is a door that can reset the clock it keeps). It
**defaults to `-`** — the schema's "not set yet" spelling — so an old ledger written before this field
existed reads back `-`, and every reader tolerates that: the quiet-run rule simply does not fire on it.

**Boundary — `ci-status.py` writes bypass the stamp.** `ci-status.py` writes the ledger through
`ledger.py`'s raw `dump`, not the stamping `save`, so **its writes never touch `last_activity`** — a CI
derivation, a liveness-counter update, or a CI-park it performs leaves the field standing. This is benign:
those CI writes are exactly the liveness-counter bookkeeping the stamp already exempts, so the only effect
is that a run whose *only* recent motion was CI polling reads as quiet **sooner** — the quiet-run check
fires **earlier**, which surfaces a sweep earlier and never suppresses one. It never reads falsely *fresh*.

`watchdog_due` records **the durable HEALTH-PASS DEADLINE** — a UTC ISO-8601 stamp (second precision)
naming the instant by which the run owes its next deep **health pass** (`loop-control.md`, "Reschedule or
exit", owns the pass). It is stamped **only** by `ledger.py watchdog arm` (`now + WATCHDOG_INTERVAL`);
`ledger.py watchdog check` reads it back — read-only, always exit 0 — printing exactly one of `unset` /
`ok <remaining>` / `due <age>` / `invalid — re-arm`, and **`ledger.py` OWNS that parse** (nudge reuses it,
never a second copy): a malformed or naive stamp reads as `invalid`, whose advisory fix is a re-arm, never
a crash — the same treatment the quiet-run rule gives an unreadable `last_activity`. It is a
**TOOL-STAMPED** field, the exact stance `last_activity` takes: **`header set watchdog_due` is refused**
(there is no door to hand-write it — a typed-in deadline would be a **forged** one), and its writes are
**activity-EXEMPT** (it joins `ACTIVITY_EXEMPT`) — re-arming the watchdog is not "the run did something
meaningful", so a bare `watchdog arm` must **never** stamp `last_activity`, else the watchdog would defeat
the very quiet sensor it backs. It **defaults to `-`** — the schema's "not set yet" spelling — and
`watchdog check` reads `-` as `unset`; an old ledger predating the field reads back `-` and every reader
tolerates that.

`pending_adoption` records **the RUN-INTENT CHECKPOINT** — a space-separated list of PR numbers written at
setup, **BEFORE `acquire`**, so a death mid-setup does not lose the requested PR list (which otherwise
lived only in the invocation args — `run-identity-and-lease.md`, "Take a run", owns the sequence). Unlike
`watchdog_due` it is an **ORDINARY, hand-settable config field** (`header set pending_adoption "89 90"`):
it has a real door, and **writing it IS meaningful activity** (no exemption — it is **not** a sensor and
carries no liveness meaning). Adoption clears it back to `-` as its final step, and any later entry — the
armed wake, a watchdog nudge after it refreshes this owner, or a manual resume — that finds it set resumes
setup idempotently from exactly those PRs (`loop-control.md` step 1). It **defaults to `-`**: nothing is
pending.

Header field notes (the header fields above; per-row fields follow):

- Campaign **never** deletes the adopted PR's **remote** head branch — it never passes `--delete-branch`
  on merge. The repo's "Automatically delete head branches" setting governs remote-branch cleanup: if
  enabled, GitHub deletes the head branch on merge; otherwise it stays. Either way it is not campaign's
  action. **Local cleanup** is governed solely by the per-PR flags: the worktree is removed only when
  `worktree_owned = yes` and the local branch deleted only when `branch_owned = yes`; a reused worktree,
  the root/main checkout, and a reused local branch are **never** removed. `merge.py run` owns the
  resumable enforcement; see Stage 3, **"Resumable merge execution"**.

- `id` — `pr<N>` (the adopted PR number). `slug` — slugified PR title. Together they identify the row;
  re-adoption looks up by `pr`/`id` and refreshes in place, never appends a duplicate.
- `branch` — the PR's **own** `headRefName`. Adopted PRs keep their branch — campaign does NOT mint a
  `fix-<run-id>-...` branch, so the branch name won't carry the run id. **The `gauntlet-run-<run-id>`
  label is the ownership marker**, not the branch prefix.
- `worktree` — the **actual** checkout path for this PR, resolved off the PR's head branch during
  adoption (or as a guaranteed pre-review step, before its first review pass) and reused for any
  review/CI fix — not created lazily only on fix (see "PR adoption"). The repository-context-aware
  adoption operation owns created-default derivation and existing-checkout discovery; this field records
  whichever absolute path that operation returned.
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
  **moved** — the ledger
  accessor performs the head-move reset at the `set --head-sha` write itself (`ledger.py`'s
  `apply_head_sha`).

  **What a genuine head move resets — THE CANONICAL SET; every other site POINTS here and re-lists
  nothing:** the liveness counters (`stage-2-ci.md`, "THE LIVENESS COUNTERS", owns that set and every
  member) **and** the base-preflight stamp `base_ok_sha` (the `base_ok_sha` field below owns it) — both
  read from `ROW_DEFAULTS` by `apply_head_sha`, never a list retyped anywhere. The review **gate** is a
  SEPARATE decision and is NOT in this set: `reviews_ok` carries forward on a clean base-only rebase, and
  every other reset of the gate tally is owned by `stage-2-review-gate.md`, "Status labels mirror the
  review gate" — its conditions are not restated here.
  **Every** `head_sha` change fires this reset, gate reset or not: the old head's liveness evidence no
  longer describes the tip, and a base-preflight `proceed` decided for the old head no longer
  describes the content — so the next verdict must wait on a fresh `proceed` for the new tip.
- `reviews_ok` — number of fresh, context-isolated SATISFIED verdicts recorded against this PR's
  current content. Target = `required(tier)`: **1 if `tier == TRIVIAL`, else 2** (Stage **2a-triage**).
  **Only `ledger.py verdict` may RAISE it** — `set --reviews-ok <n>` refuses any value above the current
  one, because a hand-raised tally is indistinguishable from an earned one and `reviews_ok >=
  required(tier)` is a merge precondition. `set --reviews-ok 0` stays available and correct: **voiding** the
  tally on a PR-content change — or on a depth-raising tier escalation, which voids it on unchanged content
  (`stage-2-review-gate.md`, "Status labels mirror the review gate") — is not a verdict
  (`stage-2-review-gate.md`, "Recording a verdict").
- `review_rounds` — **landed verdicts, ever, for this PR — and it is NEVER RESET.** Not by a fix, not by a
  rebase, not by a content change, not by a re-triage. Written **only** by `ledger.py verdict`, which is why
  there is **no `--review-rounds` flag at any door**: a door that can write a counter is a door that can
  zero it, and the rule is enforced by the flag's ABSENCE rather than by a promise.

  **It is the review loop's only memory across fresh-context heartbeats.** `reviews_ok` is a gate tally and is
  correctly zeroed on every NOT SATISFIED — which means that without this field, **the ledger after 21
  review rounds is indistinguishable from the ledger after one**, and every stopping rule of the form "on
  the second NOT SATISFIED…" is a backstop with **no sensor**. That is not a hypothetical either: one PR ran
  **21 rounds** with such a backstop sitting unfired in this very skill, and its final row read
  `reviews_ok=0 attempts=0`. Note the asymmetry it closes: the **CI** path already carries three persisted
  counters with caps (`settled_strikes`, `unusable_refetches`, `ci_stalled_since`) and has never spiralled;
  the **review** path carried none.
- `ns_streak` — consecutive NOT SATISFIED verdicts. Cleared **only** by a SATISFIED — never by a fix, a
  rebase or a content change. Same owner, same absent flag, same reason.
- `base_ok_sha` — the head a base-preflight **`proceed`** was last decided for: the **MECHANICAL** form of the
  rebase-before-review precondition. **`ledger.py verdict` refuses unless `base_ok_sha == head_sha`** — for a
  SATISFIED **or** a NOT SATISFIED, since a counted NOT SATISFIED spends `review_rounds`/`ns_streak` toward the
  caps just the same — so a review verdict can never be recorded over a base no fresh `proceed` cleared.
  **Written by exactly one thing: `base-preflight.py check`**, through `ledger.py base-ok`, when — and only
  when — it decides `proceed` for the live head. It has **no `set`/`add-row` flag** (the same absent-door
  mechanism as `review_rounds`): a hand-written stamp would forge a `proceed` no preflight ever decided,
  recording a verdict over a conflicting or stale base, which is the exact waste this guard exists to stop. It
  is SHA-bound and voided on a head move exactly like the liveness counters (`head_sha` above, the reset
  family), and stamping it is **not activity** (it records a precondition, like re-arming the watchdog). The
  default is `-`, which no 40-char head equals, so `verdict` **fails closed** until base-preflight runs.

  **What READS these counters is `ledger.py verdict` itself, and at a cap it STOPS THE LOOP.** They are
  sensors, and the reader is fused into the one door that cannot be skipped — deliberately: a cap
  evaluated by a *separate* command is a cap a driver can forget to run, which is exactly how a "hard
  backstop" sat unfired through 35 review rounds. The hazard that argues for keeping a reader out of a
  sensor — that the reader comes to reset what it consumes — is structurally absent here: the cap path
  writes `status` and **nothing else**, so `review_rounds` stays monotone whatever it decides. At a cap the
  row goes **`repairing`** and the command **exits non-zero** (`repair-pass.md` owns the caps, the
  reassessment, and the repair). **This paragraph is the owner of the sensors-and-fused-reader design
  rationale** — the procedure docs point here rather than restate it.
- `base_branch` — **the base this PR merges into**, the target `baseRefName` from live GitHub, written
  once at row creation (`add-row --base-branch`). A run may hold rows on **different** bases, so the base
  is **per-row**, not per-run; the header `base_branch` is only the **legacy fallback** a row with none
  inherits. `ledger.py`'s `effective_base(header, row)` — an explicit row value, else the header — is the
  one place the fallback lives, and it is the **sanctioned door** for resolving a row's base. **This is
  stage 1 of 3: the field and the resolver exist, and consumers are deliberately unchanged** — today only
  `table`'s computed, display-only `base` column resolves per row; adoption does not yet record an explicit
  row base, and every other consumer still reads the header field directly. Stages 2-3 move them through
  the resolver. It is **TOOL-OWNED and
  IMMUTABLE after creation** (`CREATE_ONLY` in `scripts/ledger.py`): `add-row` writes it and **`set` has no
  `--base-branch` flag**, so the recorded base can never be rewritten — the campaign does not migrate a row
  to a new base. The default is **`-`**, which is both the schema's "not set" spelling **and** its "inherit
  the legacy header" signal, and is **DISTINCT from any explicit base name**: a `-` row inherits, an
  explicit-base row does not — which is what lets one run mix legacy inheriting rows with new explicit-base
  rows. An old ledger's rows read back `-` and resolve exactly as they always did, with no migration.
- `required_set` — **the canonical required-check set for this row's effective base**. Required checks are a
  property of the base, and the base is row state, so this is too; the header `required_set` is the **legacy
  fallback**. `effective_required_set(header, row)` — an explicit row value, else the header — is the
  resolver, the same sanctioned door as `effective_base` and **staged the same way: the resolver exists
  today, and consumers still read the header field directly until stages 2-3 move them.** Its three states
  (`declared:<json>` / `none` / `unknown`) are owned by `stage-2-ci.md` exactly as the header field's are.
  Unlike `base_branch` it is **an ordinary settable field** — the stage-2 grouped required-set refresh will
  write the canonical value through `set`, so that door stays open. The default is **`-`**, which — like
  `base_branch` — means **inherit the header**, and is **DISTINCT from `unknown`**: `-` says "this row owns
  no set; read the header", while `unknown` is an **explicit** row value meaning a read for THIS base was
  attempted and failed, so it **fails closed and cannot go green** and must **never** be silently replaced
  by the header. An old row reads back `-` and inherits; once stages 2-3 land, a new run will write
  `unknown` per row until a grouped read succeeds.
- `intent` — the PROVENANCE of `<rundir>/intent-<pr>.md` (the file itself is markdown, so it lives in the
  run dir, not in this one-object-per-line store): `-` (not adopted yet) | `stated@<iso>` (the PR body
  already carried a usable intent block, copied verbatim) | `authored@<iso>` (the driver **inferred** it
  from the PR's title, body and diff). Set at adoption (`pr-adoption.md` step 3a) and **preserved** — never
  re-derived — by every refresh (a heartbeat is a fresh agent instance; `blocker_ruling` below owns the
  durability rule).

  The distinction is the honest one and the final report states it: an `authored` intent is **the driver's
  CLAIM about what the PR is for**, not the author's, and a wrong intent block silently **narrows** a
  review.
- `pr_origin` — who authored this PR: `gauntlet` (this pipeline opened it — it carries the
  `gauntlet-authored` label) | `external` (the user's, a teammate's, or any PR adopted by number). Set at
  adoption (`pr-adoption.md`). It decides **which autonomous repairs are permitted**: an `external` PR may
  never have its branch content rewritten by a repair (`repair-pass.md`, "The ownership guardrail").
  **The default is `external` and it is LOAD-BEARING, not a placeholder** — a row whose origin was never
  established can never have its owner's work reshaped. This is **separate from `worktree_owned` /
  `branch_owned`**, which say whether campaign created the local checkout and branch for cleanup purposes;
  a PR can have a campaign-created worktree and still belong to someone else.
- `repair_count` — reassessment decisions taken for this PR (`repair-pass.md`). At **`REPAIR_CAP`** the
  only permitted decision is **ABORT**: a second failed repair leaves the PR open for a human rather than
  looping. The mechanism that fixes non-convergence must not itself fail to converge. Like `review_rounds`,
  it has **no flag at any door** — a budget you can zero is not a bound.
- `repair_decision` — `-`, or the last reassessment decision + when: `<decision>@<iso>`. Durable, because
  a heartbeat may be a fresh agent instance (`blocker_ruling` below owns why) — and
  a repair may not be dispatched at all until this field is set (`ledger.py dispatch-check --action repair`).
  **DURABLE *and* SPENT EXACTLY ONCE per cap** — it is reset to `-` when the row **re-enters `repairing`**
  (`ledger.py verdict` at a cap), so a decision answers exactly the cap it was recorded for and a PR that
  reaches a cap **again** must earn a fresh `decide` (which spends `repair_count`, so the bound holds).
  `abort@…` is terminal and is never cleared.
- `tier` — the adaptive review tier for `head_sha`: `TRIVIAL` | `STANDARD` | `HIGH`. Every heartbeat and on
  any content change, `triage.py derive` supplies the mechanical `floor` + inventory and the orchestrator
  decides the tier at or above it; `stage-2-review-gate.md`, "2a-triage", owns the complete invocation and
  policy. This field records the orchestrator's decided tier (never below the tool's floor; `TRIVIAL` only
  as the orchestrator's all-prose call); it drives `required(tier)` and review depth.
- `ci` — `green` / `red` / `pending` for `head_sha`. Recorded by `ci-status.py liveness` from `derive`'s
  JSON (`stage-2-ci.md`, "THE BOOKKEEPING IS A COMMAND"). (**There is no `none`.** It was documented but
  no procedure could ever write it.)
- `ci_fingerprint` — digest of the last **verified** CI snapshot, written by `ci-status.py liveness`
  **verbatim from the `fingerprint` field of `derive`'s JSON** (`null` there → the derivation was not
  verified and this field is not written). **What it covers and exactly how it is serialized is DEFINED in
  `stage-2-ci.md`, "SETTLED" — and is NEVER restated here**, because a fingerprint reconstructed from a
  paraphrase is a different fingerprint. **UNCHANGED + nothing RUNNING == SETTLED.**
- `settled_strikes` — consecutive derivations seen **SETTLED but not green** *while no machine action was
  due or in flight* for the PR at this `head_sha` (`stage-2-ci.md`, "SETTLED", owns the gate — a PR the
  driver is actively repairing is never struck). Counted by `ci-status.py liveness`, never by hand. At
  the **STRIKE CAP**, escalate: park `awaiting-user`
  naming the blocker. Reset to `0` on any `head_sha` change (the ledger `set --head-sha` accessor does this
  itself — ledger.py's `apply_head_sha`) or fingerprint change (by `ci-status.py liveness`).
- `unusable_refetches` — consecutive derivations whose snapshot was **UNUSABLE** at this `head_sha`. An
  UNUSABLE snapshot has **no fingerprint** (its rows were never trusted), so it can never be a
  `settled_strike`: it gets its own counter, counted by `ci-status.py liveness`. At the **REFETCH CAP**,
  escalate the same way. Reset to `0`
  on any `head_sha` change (the ledger `set --head-sha` accessor does this itself) and on any **VERIFIED**
  snapshot (by `ci-status.py liveness`) (`stage-2-ci.md`, "UNUSABLE — the refetch is BOUNDED").
- `ci_stalled_since` — `-`, or the **UTC ISO-8601 timestamp** of the first derivation that saw the check
  set **RUNNING-STALLED** at this fingerprint: an evidence row still classifies `RUNNING` **and** the
  fingerprint did **not** change (`stage-2-ci.md`, "RUNNING-STALL" — the definition; the cap lives there
  and nowhere else). Started and cleared by `ci-status.py liveness`. A **clock, not a tally**, and that is deliberate: a `RUNNING` row that is merely
  **SLOW** and one that is **DEAD** are indistinguishable on a fingerprint, and derivations are driven by
  heartbeats whose cadence depends on the run's load — so only elapsed **TIME** separates them. It is on disk
  precisely so `now - ci_stalled_since` is computable by a fresh agent instance that remembers nothing.
  Cleared (`-`) on any fingerprint change (by `ci-status.py liveness`), on any `head_sha` change (the ledger
  `set --head-sha` accessor does this itself), and whenever a **machine action** is
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
    **`ci = green`**): any value `merge-check.py` parks (its `MERGEABLE` / `MERGE_STATE_STATUS`
    catch-alls — any `— park` reason it emits) — the offending value named **verbatim**.

  Those two are the write sites that exist today, **not a bound on the set**: the class is the
  **property** — *campaign cannot move this PR without a human* (`status`, below, `awaiting-user` class
  2) — so **any** future park with that property writes its reason here, with no edit to this bullet.
  Written by the park **tool**, never by hand: `ci-status.py liveness` for a CI park, `ledger.py … park
  --pr <N> --reason <blocker>` for a non-CI one (both set it in the same atomic write as `status =
  awaiting-user`).
- `blocker_ruling` — durable record of the user's answer to a **machine-blocker park** (the `status`
  taxonomy below): `-` (none yet) | `retry@<iso>` | `abort@<iso>`. It is the **answer** to the question
  `ci_reason` **asks**, and it exists for the same reason `api_approval` does: a heartbeat may be a fresh
  agent instance, so an answer held only in context is an answer the user is asked for twice. `retry`
  unparks with the liveness counters cleared; `abort` goes terminal `aborted`. The unpark is
  `loop-control.md` step 3, "Only the user's answer unparks a PR".

  **Who writes it:** the user's **answer** is recorded through `set --pr <N> --blocker-ruling retry@<iso>`
  (or `abort@<iso>`); the park-ENTRY clear to `-` is the park writer's (`ledger.py … park` / `ci-status.py
  liveness`); and the retry SPEND back to `-` is **`ledger.py … unpark`**'s, in the same write that flips
  the status and resets the counters. `unpark` validates the shape — a bare `retry` or a malformed stamp is
  refused, and an `abort` is routed to the terminal flow rather than consumed.

  **DURABLE *and* SPENT EXACTLY ONCE — one ruling answers exactly ONE park** (`stage-2-ci.md`, "THE RULING
  IS CONSUMED EXACTLY ONCE", is the owning definition: the clears at park **entry** and `retry` **consume**,
  the scoping that keeps a stale `retry` off a later park, and why terminal `abort@<iso>` is never cleared).
  A **counter reset never touches it**: it is not one of the liveness counters.

  These live **on disk, not in the driver's head**: a heartbeat may be a fresh agent instance, and a counter —
  or a ruling — that dies with the context never reaches its cap.
- `attempts` — task attempts so far (for the retry-once bailout).
- `started` — wall-clock start of the current attempt (for the 1-hour cap).
- `api_approval` — durable record of the user's decision on this PR's API-changing fix: `-`
  (not an API change, or not yet decided) | `approved@<iso>` | `declined@<iso>`. Written the moment
  the user answers, so a later heartbeat — or a fresh agent that adopted the run — reads it and never
  re-asks about a PR already decided. It records the decision (an input); `status` stays the
  live position, so the two never contradict: `approved` pairs with the PR back in normal
  gate flow, `declined` with a terminal `aborted`. A one-off approval lands here only; it never flips
  the run-wide `api_changes` header.
- `status` — `in_review` → `merged`, or `aborted`; plus the **HELD** (non-terminal) statuses below.

### `status` held and parked taxonomy

  **HELD is the PROPERTY the dispatch guard is keyed on: campaign takes NO action that MUTATES a held
  PR.** Never launch a review pass, a CI fix, a review fix, or a merge for it, and never rebase it, refresh
  its base, push to it, or relabel it (`loop-control.md` step 3, "held-status guard" — the property, of
  which those are only examples; `stage-3-merge.md` binds both the merge and the post-merge reconcile).
  Being held does not raise `reviews_ok`, so the guard reads **`status`** — never `reviews_ok`/`ci`/
  `mergeable` alone, which would re-review a held PR and merge it without its question ever being answered.
  **It is a command, not a memory exercise**: `ledger.py … dispatch-check --pr <N>` exits non-zero on
  every held status, and the members are `HELD_STATUSES` in `scripts/ledger.py` — **the one place they are
  enumerated. Never retype that list; ask the tool.** A status added to it is enforced everywhere with no
  edit to any of the sites that consult it.

  **Holding does not change the watch policy either way** (observing is not mutating): the watch follows
  `stage-2-ci.md`, "WATCH ONLY WHAT CAN MOVE" — alive while a row is still `RUNNING`, **not** relaunched
  once CI has settled. Nor does it stop **reconcile** from reading the PR and recording what it read. The
  other PRs keep being driven: **a held PR never blocks the loop.**

  Held statuses come in **two kinds, and they are cleared by DIFFERENT events — do not collapse them:**

  1. **PARKED — waiting on a HUMAN** (`awaiting-api`, `awaiting-user`). No amount of machine work can
     resolve it. The user's answer unparks the PR **to the `status` that answer dictates** — a **RESUME**
     answer (`approved`, a standoff ruling, `retry`) to `in_review`, with normal dispatch resuming on the
     next heartbeat; a **TERMINAL** answer (`declined`, `abort`) to `aborted`, which never resumes. Per class,
     below — and `loop-control.md` step 3, "Only the user's answer unparks a PR", owns the mapping.
  2. **`repairing` — waiting on the REASSESSMENT PASS, not on a human.** The PR reached a review-loop cap
     (`review_rounds` or `ns_streak`), so it has stopped converging and **must not take another targeted
     fix or another review pass**. `ledger.py verdict` sets it, and it is **NOT a park**: campaign clears it
     itself, by reassessing the PR and executing the one decision that comes back (`repair-pass.md` — the
     owner). **A cap is a MODE SWITCH, not a doorbell**: the driver repairs the PR autonomously and asks the
     user nothing. Only the ABORT decision involves a human at all, and it is the last resort of five.
  - `awaiting-api` — parked for the user to approve an API-changing fix. Resolves via `api_approval`:
    `approved` returns the PR to the normal flow, `declined` makes it `aborted` (terminal).
  - `awaiting-user` — parked for the user to adjudicate. **Two CLASSES, each with its OWN durable answer
    record** (the class is a **property**, not a list of sites — any future park where campaign cannot
    make progress without a human is a machine blocker and inherits class 2's exit):
    1. **A REVIEW STANDOFF** — a finding the orchestrator REFUTED in the tree and a **fresh reviewer
       re-raised anyway** (`finding-audit.md`, "Audit every finding before you fix it"). A REFUTED
       finding does **NOT** park by itself — it is committed as an inline refutation and the next
       reviewer judges it; only the re-raise parks. **Answered through** `finding-audit.py rule-standoff`
       in `<rundir>/audit-<pr>-<n>.jsonl`; follow `finding-audit.md`, **Executable audit artifact**.
    2. **A MACHINE BLOCKER — campaign cannot move this PR without a human.** `ci_reason` **names** it.
       Non-exhaustively: **CI has SETTLED and is still not green** (`settled_strikes` at its cap), a check
       that **never stopped `RUNNING`** while nothing in the check set moved (`ci_stalled_since` at the CI
       STALL CAP — a hung runner, a dead reporter, a required check that queues and never starts), a
       snapshot that stayed **UNUSABLE** (`unusable_refetches` at its cap), a check carrying an
       **unrecognized enum value**, or **any merge precondition `merge-check.py` parks** (any `— park`
       reason it emits) (`stage-2-ci.md`, "SETTLED", "RUNNING-STALL"
       and "UNUSABLE — the refetch is BOUNDED"; `stage-3-merge.md`, "The merge precondition"). **This is
       the exit from `pending` — in BOTH of its shapes**, the settled one and the forever-`RUNNING` one;
       without it, a stuck PR spins forever and no one is ever told. **Answered into**
       `blocker_ruling`: `retry@<iso>` → **`ledger.py … unpark --pr <N>`**, which returns the row to
       `in_review` **with the liveness counters cleared** (else it re-escalates on its first derivation)
       **and the ruling itself SPENT back to `-`** (a ruling is consumed exactly once — `stage-2-ci.md`,
       "THE RULING IS CONSUMED EXACTLY ONCE"; entering this park clears it too, so it can never be answered
       by a **previous** park's ruling), all in one write; `abort@<iso>` → terminal `aborted` via the abort
       procedure (`unpark` refuses it — not cleared, terminal rows are never re-parked).

    Same park mechanics as
    `awaiting-api` for both: `reviews_ok` stays 0, no review pass is launched for this PR, the other PRs
    keep being driven, and the answer folds in as its own heartbeat (`loop-control.md` step 3, "Only the
    user's answer unparks a PR" — the owning definition of the record + unpark for **every** park class).
    NEVER park without surfacing the question, and NEVER park into a state whose exit is undefined.

### Review-pass artifacts — use `scripts/review-pass.py`

The plan, the `pass_identity`, the progress events, **the findings** and the read that decides **whether a
pass counts** are one artifact set with one schema owner. `scripts/review-pass.py` READS every line of it —
whatever wrote that line — and WRITES every line the reviewer records; the reviewer's three
doors, `emit-progress.py` (CLI unchanged), `emit-finding.py`, and `emit-amendment.py`, are doors into the same owner. **Never
hand-parse one of those files, and never hand-write a
line the tool writes for you** — `review-dispatch.py prepare` writes the validated `pass_identity`; the
old hand-written identity is how a truncated SHA reached real
state, and the hand-rolled tally is the same "read it by eye and write down the answer" that produced a
false `ci = green`. `stage-2-review-gate.md` owns the rules — including the emit-only rule (now with no exempt event: a
`plan_amendment_request` goes through `emit-amendment.py`), the intent a finding must ANCHOR to, and the gating rule that decides whether a finding
may block the PR — the subcommands, and the four verdicts `verify`
returns; resolve the script at `<skill-dir>/scripts/review-pass.py` and pass that path to subtasks, exactly
as with `ledger.py` below.

`review-pass.py`'s `self-test` loads a sibling fixture suite, following the **sibling-suite convention that
`SKILL.md` owns** — that page defines the rule and names which scripts do and do not follow it. This page
deliberately does not restate or enumerate it; a list here goes stale the moment a suite is added.

### Editing the ledger — use `scripts/ledger.py`

`scripts/ledger.py` is the **sanctioned way** to read and write `state.jsonl` (both the header record
and the per-PR row records) **by FIELD NAME**. The script owns the schema (the header fields and the
row fields above) in ONE place, so agents and subtasks **must not hand-edit the JSONL**. Address fields
by name and the script keeps the store canonical.

This mirrors how `stage-2-review-gate.md` treats `emit-progress.py`: the file stays **plaintext and
human-readable** JSONL (`cat`/`grep`/`jq`-able), the accessor just owns the schema and writes the
canonical layout — the store is now JSONL owned by the script. `state.jsonl` is still a cache reconciled
against ground truth every heartbeat (above) — the accessor changes *how* records are written, not what the
ledger means.

Resolve its absolute path as `<skill-dir>/scripts/ledger.py` (skill dir = the directory holding the
campaign `SKILL.md`), run it as **`python3 <that path>`** (`SKILL.md`, "Bundled Scripts" — never bare),
and pass that path to subtasks, exactly as with `emit-progress.py`. Subcommands
(`<state.jsonl>` = this run's `<rundir>/state.jsonl`):

```
# Run: python3 <skill-dir>/scripts/ledger.py --file <state.jsonl> <subcommand> …
# The synopsis abbreviates that `python3 <skill-dir>/scripts/ledger.py` prefix to `ledger.py`.
ledger.py --file <state.jsonl> header get <field>                 # read a run-config header field
ledger.py --file <state.jsonl> header set <field> <value>         # set a run-config header field (refused for a TOOL-STAMPED field: last_activity, watchdog_due)
ledger.py --file <state.jsonl> watchdog arm                       # stamp watchdog_due = now + WATCHDOG_INTERVAL (activity-EXEMPT)
ledger.py --file <state.jsonl> watchdog check                     # print unset|ok <rem>|due <age>|invalid — re-arm (read-only, always exit 0)
ledger.py watchdog interval                                       # print the interval in minutes (reads NO ledger; scheduler adapter consumes THIS)
ledger.py --file <state.jsonl> add-row --pr N [--<field> <val> …] # register a row (refuses a duplicate pr; unset fields default)
ledger.py --file <state.jsonl> set --pr N --<field> <val> [--<field> <val> …]  # update named fields on the row for PR N
ledger.py --file <state.jsonl> verdict --pr N --head-sha <sha> --verdict satisfied|not-satisfied
                                                                  # record ONE landed review verdict — the ONLY sanctioned path (refused unless base_ok_sha == head_sha)
ledger.py --file <state.jsonl> base-ok --pr N --head-sha <sha>    # record a base-preflight `proceed` for the head (base_ok_sha) — written only by base-preflight.py, never hand-set
ledger.py --file <state.jsonl> get --pr N [--field <f>]           # print the row as JSON, or one field
ledger.py --file <state.jsonl> list [--where <field>=<val>]       # print matching rows' pr numbers (all if no filter)
ledger.py --file <state.jsonl> table [--all] [--fields <f>,<f>,…] # print run header + the live rows as an aligned table (read-only)
ledger.py --file <state.jsonl> dispatch-check --pr N [--action ordinary|repair]
ledger.py --file <state.jsonl> park --pr N --reason <blocker>     # MACHINE-BLOCKER park: status=awaiting-user, ci_reason=<blocker>, blocker_ruling=- — one write
ledger.py --file <state.jsonl> unpark --pr N                      # retry unpark: status=in_review, ruling spent, liveness counters reset — one write
```

**`verdict` is the ONLY sanctioned way to record a review verdict**, and it is not a convenience: it bumps
the loop's memory (`review_rounds`, `ns_streak`), applies the tally (`reviews_ok`), and evaluates the
review-loop caps — **atomically**. Hand-setting `reviews_ok` for a verdict does the tally and silently
skips the rest, which is exactly how a PR ran 21 review rounds while its row still read `reviews_ok=0`,
indistinguishable from a PR on its first. At a cap it sets `status = repairing` and **exits non-zero**
(`repair-pass.md`). **A gate RESET is not a verdict**: a content change still writes `reviews_ok = 0`
through `set`, exactly as it always has — `verdict` records what a *reviewer decided*, `set` records what
a *commit did*.

**`dispatch-check` is the guard, and it is a COMMAND, not a rule to remember.** Run it before **any**
action that MUTATES a PR; it exits non-zero when the row is HELD (`status`, above). `--action repair` is
the one kind of work a `repairing` row accepts, and it is refused until the reassessment's decision is on
the row — otherwise a driver could call its next targeted fix "the repair" and go on whacking moles under
a new name.

**`park`/`unpark` are the sanctioned writers of the machine-blocker park/unpark TRANSITIONS**, the same
way `verdict` owns the review counters: a park (`status = awaiting-user`, `ci_reason` = the blocker,
`blocker_ruling = -`) and a retry unpark (`status = in_review`, ruling spent, the four liveness counters
reset) are each MULTI-FIELD writes coherent only together, so each is ONE atomic call rather than a
hand-assembled string of `set`s. `park` is for every **non-CI** machine-blocker park (the merge-precondition
park, `stage-3-merge.md`); the **CI** parks stay `ci-status.py liveness`'s, which writes the same three
fields itself (`stage-2-ci.md`, "ESCALATE"). `unpark` consumes a `retry@<iso>` ruling only — it refuses a
bare `retry`, an unanswered park, an `abort` (that goes terminal through the abort procedure), and a row
that is not parked. **`set` still writes `status` and `blocker_ruling` and is NOT gated:** the
review-standoff park/unpark (`finding-audit.md`) is answered through `finding-audit.py rule-standoff`, not
`blocker_ruling`, so park/unpark cannot serve it and it stays on `set`; and `blocker_ruling` is where the user's **answer** is
recorded (`set --blocker-ruling retry@<iso>`), which `unpark` then consumes.

`table` is the user-facing status view: the end-of-heartbeat report renders it whenever the run goes back
to waiting (`loop-control.md`, "Reschedule or exit"). It renders state and decides nothing — no gate
logic, no derived values.

**Read the ledger by FIELD NAME through `ledger.py get`** (or `list`) — **never by parsing the table**.
A SHA (or any value) recovered from `table`'s grid is a truncated, escaped rendering, and feeding one back
into a command or writing it to the store is a bug: this repo has already had a fabricated 8-char SHA
written into a real ledger, and a truncated SHA escape into a command.

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

  One default column is **not a stored field at all**: `base` is **computed** — `effective_base(header,
  row)`, the row's resolved base (`TABLE_BASE_COLUMN` in `scripts/ledger.py`) — so a `-` row shows the
  header base it inherits rather than a bare `-`. It is **display-only and decides nothing**; the raw stored
  value is still `ledger.py … get --pr N --field base_branch`, and `--fields base_branch` prints it raw.
- **It shortens the SHA.** `table` prints `head_sha` truncated to its first **8 characters**. This is a
  **display-only** truncation and applies to **`table` alone** — nothing else in campaign ever shortens a
  SHA. The stored value, and the one every other subcommand returns, stays the full 40-char `headRefOid`:
  `ledger.py … get --pr N --field head_sha` prints all 40.
- **It escapes cell values.** Rendered raw, a value could forge the very layout it is printed into — an
  extra column, an extra row, a run-config line, the empty-ledger marker — or, carrying whitespace at its
  edges, be eaten by the column padding and come out looking like a DIFFERENT value. So `table`
  backslash-escapes before printing: the escaped text is what you see, the raw value is what is stored.
  **`escape_cell()` in `scripts/_gauntlet/table.py` owns which characters are escaped and how, and the
  `ledger.py self-test` fixtures pin it** — that function is the definition, and this page deliberately
  does not restate it (a list here goes stale the moment one is added). What you may rely on: the rendering
  is **injective** — two different values NEVER print as the same cell, so one row can never be read as
  another — and it reserves
  the leading-`#` namespace for **every** out-of-band line the table prints (the `# <field>: …` run-config
  block, the empty-grid markers, the hidden-count line), so **no row can ever forge one**. That is what
  makes the omission notice trustworthy, and it is why an empty grid always **says which empty it is** —
  a ledger that holds nothing and a ledger whose every row is hidden print **different** markers, so an
  end-of-run table where everything merged can never be misread as a campaign that adopted no PRs.

It rejects an unknown field name (listing the valid ones), refuses a duplicate `pr` on `add-row`,
errors on a missing row for `set`/`get`, and creates the file with the header if it is missing. It also
validates the store on every read and refuses a corrupt ledger — a malformed JSON line, a record that
is not a JSON object or has a missing/unknown `type`, a duplicate `pr` row, or a header that is missing,
not first, or repeated — reporting the offending line number rather than silently dropping records. On
read it also normalizes every field value to a string (so an on-disk numeric/boolean `pr` matches the
string key) and recomputes each row's derived `id` from its `pr`, never trusting the `id` on disk. A
non-zero exit with a clear stderr message means the input was rejected — fix it and re-run.

---
