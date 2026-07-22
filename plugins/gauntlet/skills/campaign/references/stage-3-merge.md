## Stage 3 — Merge (serialized, auto)

A PR is mergeable when it is **NOT parked** AND the **live PR head SHA** —
`gh pr view <pr> --json headRefOid --jq .headRefOid`, keyed by the PR number from the ledger row —
equals the ledger `head_sha` AND `reviews_ok >= required(tier)` AND `ci == green` — i.e.
`required(tier)` SATISFIED verdicts (1 if `tier == TRIVIAL`, else 2) and green CI all recorded
against the live tip. (An adopted PR may have no local worktree checked out, so use the PR's own head
via `gh`, never a local `git rev-parse HEAD`.)

**The held-status guard binds the merge (`loop-control.md` step 3).** A **HELD** PR — `ledger.py …
dispatch-check --pr <N>` exits non-zero for it — is **NEVER merged**, whatever `reviews_ok` / `ci` /
`mergeable` say. That covers a PR **parked on a HUMAN** (`awaiting-user`, `awaiting-api`) and a PR that has
stopped converging and is being **`repairing`**-ed (`repair-pass.md`); `HELD_STATUSES` in
`scripts/ledger.py` is the one enumeration, so **do not retype it here**. Merge eligibility is **not**
derived from the gate counters alone — being held does not lower `reviews_ok`, so a rule that reads only
the counters would merge a PR whose disputed finding or API change the user has not yet ruled on, or one
whose diff the reassessment pass is in the middle of rescoping. For a park, only the user's answer unparks
it, and **to the `status` that
answer dictates** — `in_review` for a **resume** answer; terminal `aborted` for a **terminal** one (a
`declined` API change, a `blocker_ruling` of `abort`), which never returns to `in_review` and is never
merged (`loop-control.md` step 3, "Only the user's answer unparks a PR", owns the mapping). Until the
answer lands the PR is skipped, never merged.

### The merge precondition — TWO enums, and NEITHER of them is a CI signal

**`mergeStateStatus` NEVER feeds `ci`.** It is a **merge precondition**, read at Stage 3 and nowhere
else. Campaign's own SHA-pinned snapshot (`stage-2-ci.md`) is the **only** source of `ci`. Crossing these
two wires is what turns a blocked merge into an infinite CI watch.

**The merge-readiness decision is COMPUTED, never read by eye:**
`python3 <skill-dir>/scripts/merge-check.py check --pr <N> --file <state.jsonl>`. It reads the ledger
row + the live PR view (`gh pr view <pr> --json mergeable,mergeStateStatus,isDraft,state,headRefOid,baseRefName`),
resolves the row's **effective base** (its explicit `base_branch`, else the legacy header) and fetches THAT
base into the PR worktree, and prints `{"verdict":"merge"|"not-yet","reason":…}`.
It crosses — in ONE place — the held/open/draft/**base-retarget**/stale-head/ci/reviews preconditions and then **BOTH** GitHub enums (`.mergeable` first — `CONFLICTING`
and `UNKNOWN` decide on their own, `MERGEABLE` falls through — then `.mergeStateStatus`, which alone
yields `merge`), then confirms `origin/<effective-base>` is an ancestor of `HEAD`. Both enums are crossed
**TOTALLY**: a value GitHub's schema does not declare **parks**, never guesses. A live `baseRefName` that no
longer matches the row's recorded base is an unsupported retarget: it fails closed with the shared
machine-blocker reason (`base changed from <recorded> to <live>; not supported mid-run`), and the next
reconcile parks the row. Act on the verdict:

- `merge` → run the command in **"Resumable merge execution"** below.
- `not-yet <reason>` → do **NOT** merge; the reason names the block. Route on the reason's **action**
  (the phrase the tool emits), never on a hand-copied list of enum values — a value the tool newly parks
  then routes correctly with no edit here:
  - `rebase` reasons (base moved ahead / conflicts) → refresh the PR per step 6. A `BLOCKED` merge state
    that is merely **behind its base** emits a `rebase` reason here (the tool probes ancestry before
    parking), so it refreshes and re-gates rather than escalating to the user.
  - the tool's **`— park`** reasons (any `not-yet` reason that ends in `— park` / `park awaiting-user`)
    → park and name the blocker (below). This is the tool's catch-all for a merge GitHub blocks for a
    cause campaign cannot clear itself — a draft, a `BLOCKED` merge state that is **up to date** (a genuine
    human/ruleset block, not a stale base), **and any value neither enum recognizes** — so routing on the
    `— park` action, not a fixed value list, keeps this bucket total.
  - `re-poll` reasons (merge state / mergeability not computed yet — `UNKNOWN`) → the **UNKNOWN re-poll
    bound** (below).
  - the **`not supported mid-run`** reason (a live base retarget) → leave the PR; the next reconcile detects
    the base change and parks the row on the user (the merge door only refuses — reconcile owns the park).
  - Everything else (`ci is …`, `N of M approvals`, `held`, stale head) → leave the PR; the next
    heartbeat re-evaluates once that precondition changes.

The mapping the tool crosses is **OWNED by `merge-check.py`** and pinned by its sibling fixtures
(`merge-check-test.py`), which assert every enum value's verdict — that is what proves the mapping, not a
table restated here for a reader to map by eye.

**The UNKNOWN re-poll bound.** A `not-yet` whose reason is a **`re-poll`** (`.mergeStateStatus` or
`.mergeable` = `UNKNOWN` — a value GitHub has **not computed yet**) is not a verdict, and it resolves
within seconds once GitHub finishes computing mergeability lazily. Re-poll it **in-heartbeat up
to 3 times**, with a short backoff between re-polls (a few seconds) — the initial Stage-3 fetch that
returned `UNKNOWN` is what triggers this loop and is **not** one of the three. If it is **still** `UNKNOWN`
after the third re-poll, do **NOT** merge on this heartbeat — leave the PR and let the **next heartbeat** re-evaluate it: **the
heartbeat is the backoff** (`stage-2-ci.md`, "The HEARTBEAT is the backoff — never tight-loop inside one"). A value
that stays `UNKNOWN` across heartbeats is bounded by the heartbeat cadence, so **no persisted counter is needed** —
the in-heartbeat cap is a fixed 3, and the coarse retry is the heartbeat loop itself. Never read `UNKNOWN` as
`MERGEABLE`, and never let a perpetually-`UNKNOWN` PR either merge or wedge.

**EVERY `awaiting-user` park a `not-yet` verdict names is a MACHINE-BLOCKER park, and it MUST declare its exit** — a
park whose exit event never comes is the same wedge it was meant to prevent. Run **`ledger.py … park --pr
<N> --reason <the blocker>`** — the sanctioned writer of a non-CI machine-blocker park (`stage-2-ci.md`,
"ESCALATE"). It sets `status = awaiting-user`, `ci_reason` = the blocker **named** (the draft state,
an up-to-date `BLOCKED` merge state, or the unrecognized value verbatim), and `blocker_ruling = -` in ONE atomic write (park entry
spends nothing and answers nothing — a ruling already on the row belongs to a **previous** park;
`stage-2-ci.md`, "THE RULING IS CONSUMED EXACTLY ONCE"), and it refuses a blank reason, a terminal row, and
a second park over an open question. It is then resolved through `blocker_ruling` = `retry` / `abort` — the
user marks the PR ready, clears the protection, or gives up, and answers. The record and the unpark are
defined once, in `files-and-ledger.md` (`status`) and `loop-control.md` step 3, "Only the user's answer
unparks a PR"; never invent a second mechanism here.

#### `BLOCKED` and `UNSTABLE` — what each merge state means

**`BLOCKED` does NOT mean "a required check is missing or failing."** It means the merge is blocked **for
any reason** — including a **draft** PR, or one **awaiting a human approving review**, or a ruleset
campaign cannot read, **or simply a branch that has fallen behind its base**. Verified: `cli/cli` PR #13856
reads `BLOCKED` with `mergeable = MERGEABLE` **and a fully `SUCCESS` rollup**, purely because it is a draft.
Mapping `BLOCKED` → `ci = pending` → "relaunch the CI watch" therefore **LIVELOCKS**: the CI is already
green, no CI event will ever fire, campaign never approves PRs and never asks the user to — so it watches a
settled PR forever. **Probe the base ancestry before parking: a `BLOCKED` PR that is only behind its base
rebases (`merge-check.py` emits the `rebase` reason, verdicts carried), and only a `BLOCKED` PR proven up to
date parks — name the blocker instead.** GitHub's merge-state enum is not a reliable "behind" signal (a
behind PR can read `CLEAN` or `BLOCKED` under the same ruleset), so the Git ancestry check is the sound one,
which is why `base-preflight.py` already runs it and the merge gate now matches.

**`UNSTABLE` means non-*passing*, which includes *pending*.** Treating it as red would dispatch a CI-fix
subagent at a check that is merely **still running**.

### Resumable merge execution

**Run the merge sequence through its command:**

```text
python3 <skill-dir>/scripts/merge.py run \
  --ledger <state.jsonl> --pr <N> --project-root <repository.project_root> --repo <owner/name> \
  [--merge-method squash|merge|rebase]
```

`merge.py` imports `merge-check.py`; readiness policy stays owned by **"The merge precondition"** above.
The command re-reads the live PR and exact head, checks this run's owner label, executes the established
`gh pr merge <N> --<merge-method> --match-head-commit <head_sha>` call without `--delete-branch`, confirms
`MERGED`, updates the local **row base** (`effective_base` — after a `v3` PR, local `v3`; after a `main` PR,
local `main`), cleans only ledger-owned local resources, then records `status = merged`
through the ledger accessor. Every base door in the command — the pre-merge validation, the merge-check
ancestry, and this post-merge sync — targets the row's base, and refuses a live retarget with the shared
`base changed … not supported mid-run` reason.

`--match-head-commit` pins the merge to the exact reviewed head SHA: a push that advanced the live tip
between the readiness view and the merge call makes GitHub refuse fail-closed, rather than squashing the
unreviewed head. `--merge-method` defaults to `squash`; **use the repo's prevailing merge method if squash
is disabled** (a squash-only-disabled repository otherwise fails loudly on every restart). The
no-`--delete-branch` rule holds for every method: campaign never deletes the adopted PR's remote head
branch — the repo's "Automatically delete head branches" setting alone governs that.

Each phase leaves a durable or safely repeatable checkpoint. Re-run the same command after any failure:
GitHub `MERGED` skips the merge call; base updates are fast-forward-only; absent owned worktrees/branches
count as completed cleanup; a terminal ledger row is a no-op.

When the checked-out local base is what git fast-forwards and an unrelated actor's **uncommitted** edits in
that checkout block it, the command **refuses and lists the blocking paths it detected** (each staged, unstaged,
or untracked path the incoming fast-forward would overwrite — a change to an *unrelated* path, or a path
already staged to exactly the incoming content, does not block a fast-forward and is not listed), then
proposes the graph-safe fix:
**stash the listed work (`git stash -u` to include untracked files), or commit it on a SEPARATE branch and
switch back to the base — then re-run the same command to resume the owed base-sync.** It **never** tells you
to commit on the checked-out base itself, which would create a diverged sibling commit the re-run's
fast-forward would refuse; and it **never** commits, stashes, resets, restores, checks out, or cleans those
paths — the campaign does not own them. Because the base-sync runs before cleanup and the terminal write,
those phases stay pending until the re-run. The tailored path-list + stash guidance is emitted **only** when
git itself refused the fast-forward because uncommitted work would be overwritten (its `overwritten by merge`
diagnostic, matched under a forced C locale) **and** paths were detected; every other
fast-forward failure — a genuine divergence, an **unmerged/conflicted index** (which git will not let you
stash or commit away, so the tailored recovery advice would be wrong), a stale `.git/index.lock`, or a
diagnostic probe that could not run — falls back to git's original raw error
unchanged, even if the read-only probe still named candidate paths. **This tailored path-list is best-effort:
a read-only probe that cannot reproduce git's full fast-forward-blocking decision computes it, so in unusual
working-tree states (submodule gitlinks, nested untracked repositories, assume-unchanged / skip-worktree /
sparse-checkout entries) it MAY over- or under-name paths — it is a convenience, never the authority. Every
refused fast-forward, INCLUDING this tailored refusal, appends git's own diagnostic verbatim as
`Original Git diagnostic:`, and THAT raw diagnostic — not the tailored list — is the authoritative account of
what actually blocks the fast-forward.** The command refuses held rows whose live PR is
OPEN (a CLOSED held row is closed out to `aborted` — `loop-control.md` Step 4 — and a `MERGED` held row is an
external merge, resumed to finalize base-sync/owned-cleanup/terminal write; neither is refused), a `--repo`
that does not name the checkout's own repository, stale gates, uncertain GitHub facts, another run's PR,
foreign refs, root/reused cleanup targets, and cleanup before confirmed `MERGED`. Report `reused-left` cleanup results in the final report.

Never hand-run a later phase after this command fails. Fix the named cause, then re-run the command so its
ownership checks and phase order remain in force.
6. After each merge+sync+cleanup, reconcile other open PRs (write any `reviews_ok`/`head_sha`/`ci`
   change below through `scripts/ledger.py … set --pr <N> --<field> <val>` by field name, never by
   hand-editing the row by column position).

   **SKIP HELD PRs FIRST — before any base refresh, rebase, or conflict handling.** A **HELD** PR
   (`ledger.py … dispatch-check --pr <N>` — parked on a human, or `repairing`) is **FROZEN**
   (`loop-control.md` step 3,
   "held-status guard"): this reconcile MUTATES a PR, so it is exactly what the guard forbids. A clean
   rebase would move its `head_sha`, set `ci = pending` and — at that head write — the accessor would fire
   the head-move reset (`files-and-ledger.md`, the `head_sha` field, "What a genuine head move resets"); a judgment-path rebase (conflict-resolving
   or diff-changed) would reset
   `reviews_ok`, relabel, and relaunch work — and would **change the PR's content**, which can invalidate
   the very refutation or API change the user was parked to adjudicate. **A parked PR that has fallen
   behind simply STAYS behind** until the user answers; it is re-reconciled normally on the heartbeat after it
   unparks. **Do NOT drop its row** — it stays in the run, and the park **does not change its CI watch
   either way** (observation, not mutation): the watch follows the normal policy (`stage-2-ci.md`, "WATCH
   ONLY WHAT CAN MOVE") — relaunched while a row is still RUNNING, **not** relaunched once CI has settled.

   For each **non-parked** open PR, run `python3 scripts/base-preflight.py check --pr <pr> --worktree
   <worktree> --base <base> --file <state.jsonl>`, where `<base>` is that row's **effective base** (its
   explicit `base_branch`, else the legacy header — a run may hold PRs on different bases, so it is resolved
   per PR, and `--base` is asserted against it). It fetches `origin/<base>` and requires it to be an ancestor
   of `HEAD` even when GitHub still reports CLEAN. On `recheck`, re-poll and leave the candidate alone. On
   `rebase-first`, rebase before considering the candidate for another review or merge:
   - Clean rebase (no conflicts, PR diff unchanged) → **EXECUTED — not hand-run — by `python3
     scripts/clean-rebase.py run --ledger <state.jsonl> --pr <N> --worktree <worktree> --base <base>`**: it
     does the fetch/rebase/`--force-with-lease` push, verifies the PR's own diff is unchanged, and writes the
     one ledger reset — keep `reviews_ok`, **keep its status label as-is** (the gate did not reset, so an
     accepted PR stays `gauntlet-accepted`), new `head_sha` written through the accessor (which fires the
     head-move reset at the door — new commit, new evidence — `files-and-ledger.md`, the `head_sha` field,
     "What a genuine head move resets"), `ci = pending`. **Exit 3 means it was NOT
     clean** — a conflict, or a rebase that changed the PR's own diff — and it has already aborted/reset to
     the original head; the judgment-path bullet below then owns **both** exit-3 subcases. On a clean (exit 0) rebase,
     **re-derive CI from a snapshot of the new tip in the same heartbeat, launching a watch only if `liveness`
     then reports `watch_warranted`** ("WATCH ONLY WHAT CAN MOVE"). A rebased PR must not sit unwatched
     until the heartbeat while its checks are running — but it must not be watched when **nothing** is
     running either, which right after a push is the common case (no check has registered yet). CI must
     return green before merging.
   - Judgment-path rebase — a conflict resolved by hand, OR a no-conflict rebase that reshaped the PR's own
     diff (both `clean-rebase.py` exit-3 subcases) → PR content changed → **reset `reviews_ok` to 0 AND, in that
     same step, reconcile the label by running `label-mirror.py mirror` for the PR** (it restores
     `gauntlet-reviewing` on a PR carrying `gauntlet-accepted`) — the gate and its
     label move together (`stage-2-review-gate.md`, "Status labels mirror the review gate", owns the swap
     and the tool). Update
     `head_sha` to the
     new tip through the accessor, which fires the head-move reset (a new head is new evidence — `files-and-ledger.md`,
     the `head_sha` field, "What a genuine head move resets"; the clean-rebase branch above does the same, and this branch is no different in
     that respect). Then re-derive CI for
     the new tip — watching it only if `liveness` reports `watch_warranted` ("WATCH ONLY WHAT CAN MOVE") — and re-enter
     Stage 2.
   - `proceed` with the same live `head_sha`, `reviews_ok >= required(tier)`, and `ci == green` → return
     to **"Resumable merge execution"** in the same heartbeat; `merge.py` imports `merge-check.py` and
     repeats the ancestry check before merging.

The drain stays **serialized — one PR at a time across the whole run**, even when PRs target different bases
(`v3`, `main`). There is no run-wide checkout pinned to one branch: each merge resolves, validates, and syncs
its own row's base, so a mixed-base run drains in one serial order and each merge updates only its base's
local ref. Stop the merge loop only when no remaining PR is immediately mergeable after the latest base refresh.

---
