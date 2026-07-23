## Loop control

> **Read when:** every heartbeat executes these steps in order; jump to the step you are in.

The skill is **event-driven**. Read `runtime-adapter.md` before waiting. Reconciles come from the first
invocation, a scheduled heartbeat when the host provides one, a **background task completing**, or the
bounded-wait fallback returning. A completion may be a CI watch, a review, or a CI/review fix.

**Who executes Step 1:** on a host with a fresh-worker mechanism, a heartbeat that resumes an
already-bound run executes Step 1 through ONE fresh reconcile worker, and the driver executes Steps 2–5
from its compact report; inline Step 1 is the fallback (no worker mechanism, or a dead/unusable
worker). `runtime-adapter.md`, "Reconcile worker", owns the contract — what the driver passes in, what
the worker returns, and what never moves into it. The steps below are unchanged whoever executes them.

**Every heartbeat — reconcile, dispatch, reschedule:**

### Step 1 — Resolve repository context, then the run + lease, then init / resume / start fresh

1. **Resolve repository context, then the run + lease, then init / resume / start fresh.** Call
   `runtime-adapter.md`'s repository-context resolver exactly once with the supplied checkout and carry
   that record for every path and Git cwd on this heartbeat. Then bind **which run this heartbeat is
   for** and confirm you may drive it — `run-identity-and-lease.md`, "Resolving a heartbeat", owns the
   whole resolution: which arg mode does what, and which `lease.py` call to present a token to. Drive
   only on an `owned`/`adopted` verdict; on `superseded` or any refusal, **stand down**.
   This lease gate is what guarantees **no two agents drive one ledger**.

   Once you own the run and have loaded its ledger, run
   `scripts/nudge.py --file <rundir>/state.jsonl --followups .gauntlet/followups.jsonl --rundir <rundir>`
   and **READ its output** before reconciling. It is the **advisory reminder list** — computed from durable
   state so an amnesiac fresh-context heartbeat is HANDED its obligations rather than told to remember
   them. It **decides nothing and always exits 0**; each reminder just points at the owner of the actual
   check (labels, caps, the health pass below — including its `watchdog due` reminder). Act on the
   reminders through those owners; ignore none silently.

   Once bound and confirmed owner, decide on **liveness of THIS run**, not on whether some `state.jsonl`
   exists — and scope **every** git/gh scan to this run's `gauntlet-run-<run-id>` label so another run's
   PRs are never mistaken for your own (adopted PRs keep their OWN head branch, so ownership is the
   LABEL only — never a branch prefix). Live work (this run) = any open PR carrying this run's label,
   **OR** any non-terminal row in this run's `state.jsonl` (any status that is not terminal `merged`/`aborted`).
   Three cases:

   - **This run has live work → resume.** Resolve a dead review pass — no verdict, no live task — from its
     highest-numbered `launch_attempt` through `runtime-adapter.md`, **Review preparation mapping**, **NOT
     by a blind re-launch**. Why launch evidence is irrelevant on this path lives in **"Resume after a
     killed session"** below (Stage 2a).
     **Reconcile against ground truth** — do NOT redo *completed* work; a CI task whose output file is
     missing may be re-launched, since in-flight tasks die with their session — then, for each of this
     run's branches/PRs read the live SHA, CI status, and verdict files, and refresh the ledger: write
     every ledger update through `scripts/ledger.py … set/header set` **by field name**
     (`files-and-ledger.md`), never by hand-editing rows by column position.

     **This refresh is itself a gate-reset site — relabel here, in this step.** When it detects that a
     PR's live `head_sha` has moved with the PR diff changed (a formatter/bot commit, a manual push,
     any content change this run did not dispatch), it resets `reviews_ok` to 0 — and MUST, in the same
     step, reconcile the label by running `label-mirror.py mirror` for that PR, which restores
     `gauntlet-reviewing` on a PR carrying `gauntlet-accepted`
     (`stage-2-review-gate.md`, "Status labels mirror the review gate", owns the swap and the tool).
     Do NOT defer this to the label-reconcile pass below — that pass is only the **backstop** ("This
     reconcile is a backstop, not the mechanism", below). (A clean base-only advance with the PR
     diff unchanged does not reset the gate, so it keeps `gauntlet-accepted`.)

     **And whenever this refresh writes a NEW `head_sha` — gate reset or not — the ledger accessor FIRES
     THE HEAD-MOVE RESET** (`files-and-ledger.md`, the `head_sha` field, "What a genuine head move resets"):
     write the new `head_sha` through `ledger.py … set --head-sha` and its door performs the reset in the
     same row write. This covers **both** cases above: the content change that resets the
     gate, **and** the clean base-only advance that does not. **Do NOT hand-reset any field here** — the
     door owns it.

     Produce **one batched snapshot per heartbeat** through **"The canonical `prs.json` command"** in
     `files-and-ledger.md`. Its executable owner is `scripts/reconcile.py fetch`; NEVER reconstruct its
     GitHub query. A refusal leaves the prior file intact, but that prior file is not current evidence →
     stop this reconcile and handle the reported blocker.

     — then **compare that snapshot against the ledger by running
     `scripts/reconcile.py detect --ledger <rundir>/state.jsonl --prs <rundir>/prs.json --run-id
     <run-id>`. The comparison is MECHANICAL and the TOOL does it — do NOT hand-walk the snapshot row by
     row.** It emits **FACTS ONLY** (one object per ledger row, plus an `unadopted` list) and **names no
     action**: a terminal row reports only `{"terminal": <status>}` (it is not compared), and every live
     row reports `absent_from_snapshot` and, when present, `head_moved` / `base_changed` /
     `branch_mismatch` / `state` / `mergeable` / `mergeStateStatus` / `label_facts` (see its `--help`).
     **Routing each fact is THIS skill's policy — the tool observes, you route** (a moved head is a FACT,
     not a tool failure, so `detect` exits 0 on it; it fails closed only on a snapshot that is not
     evidence). Route each fact to the rule that already governs it — the rules are unchanged, this only
     names which fact triggers which:
     - **`absent_from_snapshot`** — a live row whose PR dropped out of the validated canonical snapshot:
       it merged or closed, and **that absence IS the signal** (`scripts/reconcile.py fetch` owns the
       query contract). **NEVER read absence as an error, and NEVER fetch anything to "resolve" it or widen
       the snapshot (`--state all`) to re-open whether the row is really terminal** — a past change broke
       adoption by "fixing" absence with `--state all` (repo `CLAUDE.md`). ROUTE it to the **Stage 3 drain**
       (Step 4), which FINALIZES it through `merge.py run`: that single per-row live view — NOT a snapshot
       re-widening — distinguishes **MERGED** (resume the owed base-sync/cleanup/terminal-write phases) from
       **CLOSED without merging** (the terminal close-out, which records `aborted` and touches no local refs).
     - **`head_moved`** — the live head differs from the row's `head_sha`: this is the **gate-reset and
       head-move-reset** site — the two paragraphs directly above the command block own it. The tool
       reports only THAT the head moved; deciding whether the PR **diff** changed (reset `reviews_ok`,
       relabel) or it was a **clean base-only advance** — which fires the head-move reset at the door
       (`files-and-ledger.md`, the `head_sha` field, "What a genuine head move resets"), gate kept — stays
       your judgement.
     - **`base_changed`** — the snapshot `baseRefName` differs from this row's **`effective_base`** (its
       explicit row `base_branch`, else the legacy header fallback; `detect` resolves it per row through
       `ledger.py`'s `effective_base`, never against the one header base). The PR's TARGET moved, and the
       campaign does **not** migrate a row to a new base — **PARK the row** on the user through the existing
       machine-blocker path and do **NOT** act on `head_moved`/dispatch/CI/merge for it this pass (base
       currency is decided FIRST): run **`ledger.py … park --pr <N> --reason "base changed from <ledger> to
       <snapshot>; not supported mid-run"`** with the `base_changed` fact's own `ledger` (recorded) and
       `snapshot` (live) values — the EXACT durable reason, recorded in `ci_reason`; the park sets
       `status = awaiting-user` and clears `blocker_ruling`, and the label reconcile below mirrors it. An
       **already-held** row keeps its open question — `park` returns `EXIT_STOP` and leaves the existing
       `ci_reason` intact; report the mismatch, do not overwrite. The user resolves it through the ordinary
       machine-blocker ruling path (`retry` after restoring the base / `abort`) at "PARKED" below. (This is
       the PR being **retargeted** — a different base branch NAME. A base branch that merely ADVANCED, same
       name, is not this fact; it is caught by base-preflight ancestry — `stage-2-review-gate.md`, "Base
       currency with `<base>`".)
     - **`branch_mismatch`** — the snapshot `headRefName` differs from the row's recorded `branch`: reconcile
       the row's `branch` (adopted PRs keep their **own** head branch — the `gauntlet-run-<run-id>` label is
       the ownership marker, never a branch prefix; `files-and-ledger.md`, `branch`).
     - **`state` / `mergeable` / `mergeStateStatus`** — verbatim GitHub fields, unjudged: they feed CI and
       merge derivation (Stage 2b; `merge-check.py`, Stage 3).
     - **`label_facts`** — which status labels the snapshot shows, reported not judged: they feed the **label
       reconcile pass** below ("Reconcile labels too" / "Always apply one status label AND remove the other").
     - **`unadopted`** — snapshot PRs with **no ledger row**: discovery candidates (`pr-adoption.md`).

     Fall back to per-PR `gh pr view` only where the snapshot
     isn't enough (merge-gate CI truth stays the SHA-pinned, SHA-verified snapshot of **both** check
     families, Stage 2b). Heartbeat
     turnaround is throughput: every serial `gh` call in reconcile delays every dispatch behind it. Re-read
     **the ledger header's run config — EVERY field of it, whatever they are** (`files-and-ledger.md` owns
     that set; do NOT keep a copy of it here, a list beside a property goes stale the heartbeat a field is
     added). It governs the run, and must be
     consulted fresh each heartbeat, never from memory (a heartbeat may be a fresh agent instance that just
     adopted the run, so an explicit/preferred reviewer would otherwise
     be lost and silently revert to the default; Constraints, Base branch, "The reviewer",
     "PR adoption"). Refresh
     the lease. This is the path every `--run` scheduled heartbeat takes.
   - **No run bound and none live (no `gauntlet-run-*` PR, no non-terminal `<rundir>`) → first run.**
     **Check there is something to adopt BEFORE creating any run state.** If the invocation carries no
     `#PR` args (a bare or non-`#PR` invocation that found no live run to resume — likewise `--new`
     with no `#PR` args), **create nothing** — no run-id, no `<rundir>`, no lease, no `state.jsonl` — and
     show the **idle prompt** (`run-identity-and-lease.md`, "Resolving a heartbeat", owns its wording).
     Creating `<rundir>`/lease/header before a PR is confirmed would leave an empty
     orphan run that later no-arg invocations rediscover as bogus state.

     When there **are** `#PR` args, **preflight the whole set FIRST — read-only, before creating any
     run state**: read every PR's metadata (`gh pr view`, including each PR's `baseRefName`) and run the
     refusal checks (foreign-owned, cross-repo/fork per `pr-adoption.md`). **The PRs need NOT share a
     base** — one run may hold PRs targeting different bases, each driven against its own recorded base
     (`run-identity-and-lease.md`, "Base branch"). Preflight imposes no cross-row base agreement; every
     PR must still pass all `pr-adoption.md` refusal checks (foreign-owner, cross-repo/fork).
     This touches **no** run-id, `<rundir>`, lease, `state.jsonl`, label, worktree, or CI watch. **If any
     PR is refused, prompt and create nothing** — so a rejected set never leaves an
     empty orphan run behind. **Only once the full set passes preflight**: call `create_run_directory`
     **first** — it mints the run-id and atomically creates `<rundir>` — and derive `run_id` from the
     returned directory's final path component; **then** take the run per `run-identity-and-lease.md`,
     "Take a run" (which, BEFORE arming, records `pending_adoption` = this set's PR list; then token,
     heartbeat arming, `lease.py acquire` — in that order),
     and write the `state.jsonl` header with `run_id` set. **The header `base_branch` stays its `-`
     default** — the base is per-row now, recorded on each row at adoption from that PR's live
     `baseRefName`, never a run-wide header value (`files-and-ledger.md`, the row `base_branch` field).
     Then **adopt** each PR
     (ledger row + labels + worktree, and a CI watch **only when one is due** — `pr-adoption.md` owns what
     adoption produces and when the watch is warranted; adoption fetches **each PR's own base ref**, so a
     set spanning several bases fetches each of them once at its adoption). A death mid-adoption still
     leaves a discoverable, adoptable run. **When the whole requested set is adopted, clear the checkpoint —
     `ledger.py … header set pending_adoption -`** (this is adoption's final step; a later entry that
     still sees it set knows setup did not finish and resumes it). Then fall through to dispatch/reschedule.
   - **This run's `state.jsonl` is fully terminal — every row `merged`/`aborted`, no open PR carrying this
     run's label → the run is finished.** Do **not** silently exit "all fixed" (the old bug) and do **not**
     silently restart. **Ask the user** whether to gate more PRs — e.g. "gauntlet run
     <run-id> finished (N merged, M aborted). Gate more PRs? Pass PR numbers (or run gauntlet:review
     first)." A new run needs a `#PR` set, so collect PR numbers (equivalently direct the user to
     `<campaign-invocation> --new #PR...`); on a PR set, start a fresh run **with carryover** (see "Fresh
     runs and carryover") — **no run-id/lease/`state.jsonl` is created until that set passes preflight**.
     With no PR numbers (or "no"), emit that run's final report and stop. This prompt is the *only* heartbeat
     that asks the user about scope.

   **The `--new` fresh-run signal short-circuits the above — but only WITH `#PR` args:** `--new #PR...`
   (or "fresh run" / "start over" with PR numbers) mints a NEW run-id + token and starts a fresh run
   adopting those PRs immediately, regardless of any run's liveness — no prompt, and **other live runs
   are left untouched** (they keep running under
   their own drivers). **`--new` with no `#PR` args creates nothing** — it falls through to the idle
   prompt (run `gauntlet:review`, or pass PR numbers), exactly like a bare no-arg first run, and mints
   no run-id/`<rundir>`/lease.

   **Reconcile labels too** (idempotent, retroactive, **scoped to this run**). Ensure the labels exist
   (`gh label create … --force`, including this run's `gauntlet-run-<run-id>`), then for every PR **of
   this run** (its `gauntlet-run-<run-id>` label — the only ownership marker for adopted PRs): ensure
   it carries `gauntlet-run-<run-id>`, and **reconcile its status label by running `label-mirror.py
   mirror` for it** — the tool overwrites the status label to match the PR's **live** gate state. (The
   `gauntlet-run-<run-id>` ownership label is adoption's, and the tool never touches it — ensure it
   separately, as above.) **Never touch another run's PRs.**

   **The tool applies one status label AND removes the other — never merely add.** The two status labels
   are mutually exclusive, so a purely additive reconcile cannot repair the one state it most needs to: a
   PR wearing **both** labels (what a missed swap at a reset site actually produces) would keep the
   contradictory label forever, because adding a label it already carries changes nothing. The swap it
   applies — shown as the SPEC it implements, one call per PR, direction picked from the live gate, NOT a
   command to paste by hand:

   ```
   # current HEAD holds required(tier) SATISFIED verdicts:
   gh pr edit <pr> --add-label gauntlet-accepted  --remove-label gauntlet-reviewing
   # otherwise:
   gh pr edit <pr> --add-label gauntlet-reviewing --remove-label gauntlet-accepted
   ```

   Both forms are idempotent (`--add-label` of a label already present and `--remove-label` of one that
   is absent are both no-ops), so this converges from **any** starting state — neither label, one label,
   the wrong label, or both.

   This reconcile is a **backstop, not the mechanism**. The relabel is owed at the moment the gate
   projection changes, in the same step that writes `reviews_ok = 0` (which includes a depth-raising tier
   escalation) or decides a de-escalation that lowers `required(tier)`
   (`stage-2-review-gate.md`, "Status labels mirror the review gate", owns the two-direction split); this pass only
   *self-heals* a swap that was somehow missed. A PR that
   reaches this point wearing a stale `gauntlet-accepted` — or both labels at once — means some reset
   site skipped its relabel: fix the label here, and treat it as a bug in that site, not as normal
   operation.

   **Settle the base branch's required-check set before any CI derivation:** run `scripts/ci-status.py
   required-set --ledger <rundir>/state.jsonl`. Run it every heartbeat; the command reuses a settled value and
   only retries `unknown`. `stage-2-ci.md`, "WHAT WERE WE EXPECTING TO SEE?", owns its states and behavior.
### Step 2 — Fold in completions

2. **Fold in completions.** For any background task that finished (CI watch → **a HEARTBEAT, not an artifact**:
   the watch only blocks and produces nothing, so this heartbeat performs the SHA-pinned fetch of
   both check families, promotes it atomically to `ci-<pr>-<head_sha>.txt` and verifies its stamp
   against the ledger's current `head_sha` before parsing a single line of it — the CI state is
   never decided from the watch's exit code (Stage 2b, "WHO DOES WHAT" and "VERIFY THE STAMP BEFORE
   PARSING"); review →
   the active launch attempt's output file, with its progress file as liveness evidence — attempt 1
   uses `.prompt.txt` and writes `review-<pr>-<n>.txt` / `.progress.jsonl` / `.findings.jsonl`; a
   relaunch uses and writes `review-<pr>-<n>.a<k>.*`, and
   only the attempt named in the current `pass_identity` is read or counted (Stage 2a). **Before a
   review verdict is counted, the pass's artifacts must verify** — run `scripts/review-pass.py verify
   --ledger <rundir>/state.jsonl` against the active attempt's progress file, never parse the report by
   hand. The tool derives the attempt-scoped report path and prints its strict result; anything but `ok`
   is not tallied (Stage 2a, "Does this pass COUNT?"). **`--ledger` is a TALLY PRECONDITION, not an
   option:** it compares the pass's DISPATCH-TIME `pass_identity.default_non_goals` binding to the run
   header's current `default_non_goals`, so a verdict a reviewer earned under a scope the operator has since
   changed is refused as `unusable` rather than counted (Stage 2a, "Does this pass COUNT?" owns the rule).
   The scope is bound at dispatch, not read from the mutable intent, so the step-3 re-sync of the intent
   cannot let a stale SATISFIED merge an area now in scope but never reviewed. The coherence rule is an if and only if: `not-satisfied` exactly when at least one GATING
   finding stands — a verdict that blocks a PR
   must name what blocks it, and a finding that blocks a PR cannot be waved through by the verdict. Either
   way round is `unusable` (Stage 2a, "Does this pass COUNT?").
   **A pass that raised a separate request ends with the exact DEFERRED-with-reason result.** DEFERRED is
   not a verdict, so it is never tallied — the tool routes on
   the progress file and returns `amended` (fold the amendment, re-run the pass) or `incomplete`
   (relaunch), and only a binary `satisfied`/`not-satisfied` ever reaches the ledger below.
   **Then record the verdict with `scripts/ledger.py verdict --pr <N> --head-sha <sha> --verdict …`** — the
   ONLY sanctioned path, and the only thing that bumps `review_rounds` (Stage 2a, "Recording a verdict");
   never set `reviews_ok` by hand. It refuses unless the base-preflight `proceed` above stamped `base_ok_sha`
   for this head (the `--file` on the precondition run is what records it).
   For any completed task (review, CI watch,
   CI/review fix), record the result against the SHA it ran on and act per Stage 2.
### Step 3 — Dispatch due work

3. **Dispatch due work — non-blocking, idempotent, bounded, work-conserving.** Scan the whole run,
   not just the PR/job that woke you. Launch every due action that fits a free slot before returning.
   Launch only what is actually due *and not already in flight* (check ground truth first, never the
   ledger alone).

   #### HELD-STATUS GUARD — a PROPERTY, not a list, and now a COMMAND

   **HELD-STATUS GUARD — a PROPERTY, not a list, and now a COMMAND. Apply it BEFORE every bullet below,
   and before every other action this skill takes on a PR.** While a PR's `status` is **HELD** the PR is
   **FROZEN: take NO action that MUTATES it.** **Skip it and keep driving the run's other PRs** — the run
   stays live and a held PR NEVER blocks the loop (`run-identity-and-lease.md`, "Never hold the run hostage
   on a user prompt").

   **Ask the tool, never a memorized list:**

   ```
   ledger.py --file <state.jsonl> dispatch-check --pr <N>     # non-zero => do NOT act on this PR
   ```

   `HELD_STATUSES` in `scripts/ledger.py` is the **one place** the members are enumerated, and
   `files-and-ledger.md`, `status`, is their definition. **Never retype that list here or anywhere else** —
   a status added to it must be enforced at every site with no edit to any of them. Today it holds two
   kinds, held for **different reasons** and cleared by **different events**:

   - **PARKED** (`awaiting-user`, `awaiting-api`) — waiting on a **HUMAN**. No amount of machine work can
     resolve it; only the user's answer unparks it (below).
   - **`repairing`** — the PR reached a **review-loop cap** and has stopped converging. It is **NOT waiting
     on a human**: campaign clears it **itself**, by running the reassessment pass and executing the one
     decision that comes back (`repair-pass.md`). **A cap is a mode switch, not a doorbell — do NOT prompt
     the user.** Ordinary gate work is refused for it; the **decided repair** is the one thing that may be
     dispatched, and only once the decision is recorded (`dispatch-check --action repair`).

   **The test is "does this MUTATE the PR?" — NOT "is this action named in a list?"** *Mutate* = change
   the PR or dispatch work that will: its content, its head commit, its base, its labels, its
   open/merged state. Non-exhaustively: no review pass, no CI fix, no review fix, no copilot-address, no
   precondition fix, no merge, no base refresh, no rebase (clean **or** conflict-resolving), no push, no
   relabel, no gate reset, no content change of any kind — **and nothing absent from this list either.**
   The list is **illustrative; the property governs.** An enumeration of dispatch sites WILL miss one —
   it already did: this guard once listed four (review, CI fix, review fix, merge) and missed
   `stage-3-merge.md` step 6, whose post-merge rebase of PRs that fell behind would have moved a held
   PR's `head_sha`, reset its gate, and **changed the very PR content the user was parked to
   adjudicate**. Any site the skill grows later is covered the moment it would mutate a held PR, with
   no edit to this list. When unsure whether an action mutates, treat it as mutating and skip it.
   - **The ONE exception is the CI watch: OBSERVING a PR is not mutating it.** The park **does not change
     the watch either way** — it follows the normal policy, `stage-2-ci.md`, "WATCH ONLY WHAT CAN MOVE":
     alive while an evidence row can still `RUN`, **not** relaunched once CI has SETTLED (relaunching a
     settled PR's watch burns a heartbeat per second and observes nothing). Parking never stops a warranted
     watch and never starts an unwarranted one. But do **NOT** dispatch a CI *fix*.
   - **Recording ground truth is not mutating either.** Reconcile still READS a parked PR (live SHA, CI,
     labels) and writes what it read to the ledger — including a `reviews_ok` reset, and its label
     mirror, when **someone else** pushed to the PR (step 1). Recording a change campaign did not make is
     not making one. What is frozen is **campaign's own action on the PR**; a park never licenses a
     lying label or a stale row.

   #### Only the user's answer unparks a PR

   - **Only the user's answer unparks a PR — and EVERY park class names the durable record it is
     answered into.** An answer that lives only in this session is an answer a fresh agent re-asks. On
     the answer: **record it**, then unpark to the `status` **THAT ANSWER** dictates — the table below is
     the authority, and it is **NOT always `in_review`**:
     - a **RESUME** answer returns the PR to `in_review` and resumes normal dispatch — including any
       rebase or base refresh the PR has been owed while frozen — from the next heartbeat. **The write
       depends on the park class:** an `api_approval` = `approved` or a standoff ruling → `ledger.py … set
       --pr <N> --status in_review` (that class carries no liveness budget and no `retry` ruling to spend);
       a **machine-blocker** `blocker_ruling` = `retry` → **`ledger.py … unpark --pr <N>`**, which flips the
       status, spends the ruling, and resets the liveness counters in ONE write (below — a plain `set` would
       leave the counters standing and re-escalate). A parked PR that has fallen **behind** its base simply
       **stays behind** until then; it is not dropped from the run, just frozen.
     - a **TERMINAL** answer (`api_approval` = `declined`, `blocker_ruling` = `abort`) → `--status aborted`
       (terminal), via `bailout-and-final-report.md`'s abort procedure. It **never** returns to
       `in_review`, and nothing is dispatched for it again.

     The record and the unpark, per cause (`files-and-ledger.md`, `status`):

     | Park cause | Durable record | Unpark |
     |---|---|---|
     | **`awaiting-api`** — an API-changing fix | `api_approval` = `approved@<iso>` / `declined@<iso>` | `approved` → `in_review`; `declined` → terminal `aborted` |
     | **`awaiting-user`, review standoff** — a REFUTED finding the fresh reviewer re-raised | `finding-audit.py rule-standoff` in `<rundir>/audit-<pr>-<n>.jsonl` (`finding-audit.md`, **Executable audit artifact**) | `in_review`; follow the accessor's derived fix scope |
     | **`awaiting-user`, machine blocker** — campaign cannot move this PR without a human; that **property** IS the class, **never a list of cases** (one illustration: CI has SETTLED and is still not green). Do not enumerate the members here — `files-and-ledger.md`, `status`, `awaiting-user` class 2, **owns** the class, and `ci_reason` names the blocker at every one of them, present or future | `blocker_ruling` = `retry@<iso>` / `abort@<iso>` | `retry` → **`ledger.py … unpark --pr <N>`** — one write that sets `status = in_review`, SPENDS the ruling to `-`, and RESETS the liveness counters (`stage-2-ci.md`, "THE LIVENESS COUNTERS" / "THE RULING IS CONSUMED EXACTLY ONCE"), then re-derive CI on the next heartbeat; `abort` → terminal `aborted` via the abort procedure (`unpark` refuses it — the ruling **stays** as the record of why) |

     **The counter reset is part of the unpark, not an optimization**: a `retry` that clears nothing
     re-escalates on its first derivation (`stage-2-ci.md`, "THE LIVENESS COUNTERS" / "SETTLED" /
     "NOT VERIFIED — the refetch is BOUNDED", own why). The loop is bounded by the **human**: campaign never
     re-asks unprompted, and every park is a fresh question backed by a fresh snapshot.

     **A `retry` is SPENT when it is consumed — one ruling answers exactly ONE park.** `ledger.py … unpark
     --pr <N>` writes `status = in_review`, the counter reset, and `blocker_ruling` = `-` in **one** call —
     the three fields are coherent only together, so the tool does them as one write rather than leaving a
     driver to hand-assemble them. That re-park above is precisely the case that proves it: the PR comes back to the **same**
     machine blocker, and a ruling left on the row would answer the new park with the **old** answer —
     the blocker would silently self-clear with no fresh user answer, which is the one thing the durable
     record exists to prevent. Park **entry** clears it too (`stage-2-ci.md`, ESCALATE), so a crash
     between the unpark and the next park cannot resurrect a spent ruling; the two clears are belt and
     braces on the same invariant, and the entry clear is the one that survives a lost context.
     **`abort` is different and is NOT cleared:** it goes **terminal** (`aborted`), and a terminal row is
     never re-parked, so nothing can ever re-consume it.
   - **Why the guard must live HERE, at the dispatch site:** `reviews_ok < required(tier)` is TRUE for a
     parked PR (the park does not raise it), so a dispatch rule that looks only at `reviews_ok` will
     happily re-review a PR that is waiting on a human — and a `SATISFIED` verdict would then make it
     eligible to merge and **merge it WITHOUT the user's ruling**, which is exactly the hole the standoff
     park exists to close. **The park MUST be enforced wherever the PR is ACTED ON — every dispatch site
     and every mutation site — not merely recorded in the ledger.**

   **A `repairing` PR is the ONE held PR that still has machine work due — and it is NOT the work the
   other bullets describe.** Do not skip it and do not prompt the user; drive its repair to completion:

   - **no `repair_decision` yet** → run `repair-pass.md`, **"Build the complete reassessment bundle"**.
     Dispatch its exact prompt to one context-isolated **`session`** worker. Record the returned decision
     through that section's bundle-bound `repair-pass.py decide` command.
   - **a `repair_decision` is recorded** → `ledger.py dispatch-check --pr <N> --action repair`, then
     execute **that** decision and no other work. When the repair has landed, return the row to the gate
     (`ledger.py … set --pr <N> --status in_review`). `review_rounds` is **not** reset — it never is.
   - **`repair_decision` is `abort@…`** → the row is already terminal (`aborted`): run the abort procedure
     (`bailout-and-final-report.md`) — leave the PR **OPEN**, drop this run's labels, write `abort-<id>.md`.

   Then, for each PR that is **not held at all**:
   - any newly-adopted PR whose ledger row lacks a `tier`, and every PR on every heartbeat → **run
     `triage.py derive --worktree <worktree> --base origin/<base> --head-sha <head_sha>`** for the
     mechanical inventory and `floor`. `stage-2-review-gate.md`, "2a-triage", owns the complete invocation
     and policy. Never classify files or modes here. On success, require output `head_sha` to equal the
     row, **decide the tier at or above `floor`** (`TRIVIAL` only when `floor` is `null` and you judge it
     truly human prose — the tool never grants it). **VETO FIRST — BEFORE any ledger write:** re-run
     `derive --tier <decided>` with the IDENTICAL `--worktree`/`--base`/`--head-sha` inputs so the tool
     vetoes a below-floor mistake; require its success and an output `head_sha` still equal to the row, and
     BLOCK gate dispatch on refusal (exit 2, no JSON). Only THEN write the tier, with **EXACTLY ONE
     directional `ledger.py … set`** — never a preliminary generic tier write followed by a second. **A
     same-SHA tier change is TWO events by direction, and the one write differs by direction**
     (`stage-2-review-gate.md`, "Status labels mirror the review gate", owns the split; depth order
     TRIVIAL < STANDARD < HIGH):
     - **Depth-raising escalation** (the decision raises the tier to a strictly deeper one — TRIVIAL→STANDARD,
       TRIVIAL→HIGH, STANDARD→HIGH; STANDARD→HIGH raises depth even though `required` stays 2): the standing
       verdicts were earned at a shallower depth and do NOT satisfy the new tier, so write the deeper tier and
       the voided tally in ONE atomic ledger write — `ledger.py … set --pr <N> --tier <deeper> --reviews-ok 0`
       (`ledger.py set` applies every field flag in a single atomic write, so tier and reset land together and
       no driver death can leave the deeper tier standing beside a stale tally that the next heartbeat would
       read as no escalation). **Also stop any review pass in flight on that PR first** (the same stop the
       content-change rule below makes): the SHA is unchanged, so an in-flight shallow-depth pass keeps a
       matching `head_sha`, and a late SATISFIED verdict would refill the just-voided tally against the
       deeper tier. Then require a fresh tier-sized plan before the next dispatch (the plan-copy
       rule in `stage-2-review-gate.md` must NOT reuse the shallower plan). Do this even though the SHA is
       unchanged.
     - **De-escalation, unchanged, or fresh adoption** (the decision lowers the tier — STANDARD→TRIVIAL,
       HIGH→STANDARD, HIGH→TRIVIAL — or holds it, or first-sets it on a row that lacked one): any standing
       verdicts were earned at a DEEPER-or-equal depth (a superset), so write the tier ALONE —
       `ledger.py … set --pr <N> --tier <decided>` — which KEEPS `reviews_ok` and the plan; only
       `required(tier)` moves.
     **Then, in that SAME step, run `label-mirror.py mirror` for that PR** — idempotent, a no-op when the
     label already matches — so the tier, `required(tier)`, and the public status label move together: it
     restores `gauntlet-reviewing` on a depth-raising escalation (the voided tally no longer meets
     `required`) and swaps a standing tally to `gauntlet-accepted` on a de-escalation that now meets the
     lowered `required` (`stage-2-review-gate.md`, "Status labels mirror the review gate", owns the swap and
     the tool). On refusal,
     refresh the moving or mismatched input and retry; never carry a tier across content the command did
     not classify.
   - current tip has `reviews_ok < required(tier)`, has no unaddressed Copilot review items, CI is not red,
     and no review is running for that SHA → **first ensure the PR's INTENT
     (`<rundir>/intent-<pr>.md`) and PR-head worktree exist.** The dispatch substitutes the intent block
     into the review prompt **verbatim** (`review-dispatch.md`), and a reviewer with no intent is a
     reviewer asked "is anything wrong with this code?" — the question with no fixed point. **It is not an
     exhortation: `review-pass.py verify` loads `intent-<pr>.md` for EVERY pass, so a pass dispatched
     without one is `unusable` and its verdict cannot be tallied** — the review would be spent for nothing.
     If the file is
     missing (a wiped `<rundir>`), write it **per `pr-adoption.md` step 3a** and record its provenance in
     the row's `intent`. Whether it was just re-authored or already present from adoption, **run
     `pr-adopt.py intent-sync --file <state.jsonl> --pr <N>` then `review-pass.py intent-check --file
     <rundir>/intent-<pr>.md --ledger <state.jsonl>` before dispatch** — the sync folds the run's current
     default Non-goals into the managed block, and the check refuses a stale or malformed one, so the
     reviewer is never launched against defaults the operator has since changed. Then **ensure the PR-head
     worktree exists**
     (the reviewer receives `<worktree>` as explicit review input under the transport-specific isolation
     contract in `runtime-adapter.md` — the
     PR row's ledger `worktree` column value, the
     single source of truth for this PR's checkout path (created at adoption/pre-review per
     `pr-adoption.md`; the ledger-recorded `<worktree>` path from that repository-context-aware
     operation) — and diffs
     `origin/<base>...HEAD`, so a real checkout must be present): if that `<worktree>` is missing, create it
     from the PR head **per `pr-adoption.md` step 5** — which reuses an existing checkout of that branch
     if one exists (root or another worktree), else adds a fresh worktree, since `git worktree add`
     refuses a branch checked out elsewhere — and record its path in the row's `worktree`. This is an
     explicit precondition of the review launch. **Also fetch `origin/<base>` fresh before the first
     review dispatch through the typed Stage 2a pre-review operation** — the review
     diffs `origin/<base>...HEAD`, a remote-tracking ref that always exists, since adoption fetches only
     the PR head and a local `<base>` may be absent or stale (see `pr-adoption.md` / Stage 2a). Then run
     `base-preflight.py check --pr <N> --worktree <worktree> --base <base> --file <state.jsonl>`; only
     `proceed` clears the review launch, and — because a verdict follows — the `--file` is REQUIRED: on
     `proceed` it records `base_ok_sha` for the head, without which `ledger.py verdict` refuses the verdict
     it later records (Stage 2a, "Recording a verdict"). Rebase on `rebase-first`, re-poll on `recheck`, or
     leave the ledger-held candidate alone on `park` per Stage 2a, then restart this precondition sequence.
     With `proceed`, evaluate the verdict transport through
     `runtime-adapter.md`'s capability/transition owner before
     building its record and take only the action it returns;
     missing native cwd/mount/sandbox controls alone are not a machine blocker. Then **ensure the pass's
     plan exists, sized to the tier, and passes `review-pass.py plan-check --file <plan> --tier <tier>`**
     — a refusal blocks the launch; repair the plan per `stage-2-review-gate.md` ("Review work-plan
     ledger" owns the plan and the default-dimensions rule this command enforces). Then launch **one**
     review pass as a **background**
     task (one at a time per PR — the second, when the tier requires two, only after the first is
     SATISFIED; Stage 2a). If a precondition is dirty, clear it first (address Copilot items / fix CI /
     rebase) instead of spending a review;
   - a review pass is in flight but the **active attempt's** progress file holds **no launch evidence**
     — no reviewer-written line of ANY kind after `pass_identity` (a `progress` `started`/`done` event
     *or* a `plan_amendment_request` all count) — past its **~5-min launch deadline** (measured from
     that file's `pass_identity.dispatched_at`) →
     it **never started** (Stage 2a launch check — a reviewer hung on stdin, a bad path, a sandbox
     denial). Kill the task, re-check the command for the known launch faults (above all the quoted
     prompt-file stdin route), then take the next action and fresh attempt from `runtime-adapter.md`,
     **Review preparation mapping**. `review-dispatch.py prepare` creates coherent attempt-scoped
     artifacts, never the dead attempt's files, which a surviving process could still write to. A failed
     launch yields no verdict: it never touches `reviews_ok` and never bumps the row's `attempts`;
   - CI red and no fix is already in flight for that PR/SHA → **CLASSIFY the failure from the check logs
     first (Stage 2b, "Classify, then set the model class") — never dispatch a worker straight off a red
     check.** The class picks the logical model class: a **formatting/lint** failure → a scoped CI-fix
     worker in the runtime adapter's **`economy`** class, which runs a
     formatter, **reads the resulting diff**, and **escalates** anything it cannot verify; **everything
     else** — and every **escalation** — → a scoped CI-fix worker in the **`session`** class. Either way the
     resulting commit **resets the gate** (Stage 2b, "Any campaign commit to the PR head resets the gate").
     Materialize the selected role through `worker-prompt.py fix` (`fix-subagent-contract.md`); its
     template owns the job order and role rules. Different PRs may fix CI concurrently within the cap.
   - `liveness` reports **`watch_warranted`** (its reduction of "WATCH ONLY WHAT CAN MOVE" — a verified
     snapshot with a still-`RUNNING` evidence row that is not an `UNKNOWN_VALUE` park; the `RUNNING` is an
     evidence row that classifies `RUNNING` under Stage 2b CLASSIFY, never "a row that is not terminal") for
     a PR whose watch task has already exited →
     **relaunch the watch in this same heartbeat**. A PR whose watch is warranted must never sit
     unwatched until the fallback heartbeat; the fallback lifecycle is not the mechanism. **But NEVER relaunch
     it merely because `ci == pending`** — once CI has SETTLED nothing can move, `gh pr checks --watch`
     exits in about a second, and its completion is itself a heartbeat: that is a heartbeat per second, forever
     (Stage 2b, "WATCH ONLY WHAT CAN MOVE"). A settled PR is resolved by the `settled_strikes`
     escalation, not by watching it harder. **And a row that never leaves `RUNNING` is resolved by
     RUNNING-STALL** (Stage 2b): its watch blocks forever and completes never, so the escalation lands on
     **this fallback heartbeat**, once `ci_stalled_since` has stood at the same fingerprint for the CI STALL
     CAP. **A watch is never a bound.**
   - about to dispatch content-changing work on a PR (review fix, CI fix, copilot-address,
     conflict-resolving rebase) while a review is in flight on that PR → **stop that review task
     first** (its verdict can only describe a SHA the fix is about to replace); the freed slot goes
     to the next due review.
   - mergeable **and not parked** → queue for serialized merge drain.
   Treat ~8 as a **rolling concurrency cap**, not a wave size: keep up to ~8 CI-fix subagents and ~8
   review processes in flight, refilling each free slot immediately; queue the rest. **Launch, do not
   wait — never barrier on a group of PRs before dispatching the next.**
   Allowed idle state is narrow and explicit: no PR can start a review, no CI/precondition fix is due,
   no exited watch needs relaunching, no PR is mergeable, and every remaining wait is external
   (background review/CI), a **parked** PR awaiting the user (`awaiting-user` / `awaiting-api` — idle on
   that PR is the CORRECT state, never a stall to "fix" by dispatching work), or a genuinely full cap. If the run has **no PR at all**
   and none is in flight (no-arg idle), do not spin: show the **idle prompt**
   (`run-identity-and-lease.md`, "Resolving a heartbeat", owns its wording).
### Step 4 — Merge queued PRs as a serialized drain

4. **Merge** queued PRs as a serialized drain: re-confirm one candidate against the live SHA **and
  re-check it is not parked** (the held-status guard binds the merge too — Stage 3), then run
  `merge.py run` for that PR. It revalidates the fetched base, merges, syncs `<base>`, cleans owned local
  resources, and records the terminal row as one resumable sequence. Reconcile remaining candidates and
  repeat while another PR is immediately mergeable (Stage 3 owns the check).

  **An `absent_from_snapshot` row (Step 1 routes it here) whose ledger status is not yet terminal is
  FINALIZED by this drain through `merge.py run`.** Such a row is the resume case: the process died after
  `gh pr merge` landed `MERGED` but before base-sync/cleanup/terminal write, so the PR left the
  `--state open` snapshot while its later phases stay pending. `merge.py run` re-reads the live PR ONCE and,
  on `MERGED`, skips the merge and resumes exactly those remaining phases. When it instead finds the PR
  **CLOSED without merging**, the SAME command performs the terminal close-out: it records the terminal
  `aborted` status and does **no merge and no cleanup** — the branch content never reached `<base>`, so its
  owned worktree/branch are left untouched for the user. Either way `merge.py run` is the single finalizer;
  Step 1 only ROUTES the absent fact here, it does not finalize it itself.
### Step 5 — Reschedule or exit

#### Primary continuity

**Every turn with non-terminal work continues the fallback lifecycle — no matter how the turn was
entered:** a scheduled wake, a session-watchdog wake, a background-task completion, or a manual `--run`
resume. This block OWNS when the loop continues; every other site points here, never restates it.

- **Scheduled-heartbeat host: schedule-or-replace the primary wake UNCONDITIONALLY, as the turn's last
  action.** The host has one replaceable pending-wake slot (`runtime-adapter.md`, "Background work and
  heartbeats"), so the schedule call arms it when empty or replaces whatever occupies it. **NEVER gate
  that call on remembered or inspected scheduler state** — not on a belief that a wake is "still armed",
  not on a `primary inspect` result. Inspection is advisory only (`runtime-adapter.md`, "Session watchdog
  nudge"); it never decides whether this re-arm happens.
- **Completion-driven turns re-arm too.** A turn entered by a background completion (CI watch, review, or
  fix) that still finds work remaining schedules-or-replaces exactly like a scheduled wake — the entry
  path changes nothing.
- **Scheduler-less host: start the bounded wait instead** (`runtime-adapter.md`, "Scheduler-less host").
- **Recalculate the delay from current state every turn** — sized by the tiers below and capped at the
  time `ledger.py watchdog check` says remains (the `watchdog_due` cap below). Never carry a delay
  forward.

5. **Reschedule or exit.**
   - Any non-terminal PR remains (in review, pending CI, or awaiting a user ruling on a review-finding
     standoff / API approval / precondition) →
     refresh this run's lease, then choose the runtime adapter's scheduled-heartbeat or bounded-wait
     branch. A scheduled heartbeat does not hand-assemble its wake: the **Scheduled-heartbeat host**
     step in `runtime-adapter.md` ("Background work and heartbeats") owns building it — it runs
     `heartbeat.py callback` and schedules that tool's stdout (a lean same-session wake prompt, never a
     leading skill re-invocation), which is why the wake carries exactly
     `--run` and `--token` and never `--new`/`#PR` or `--heartbeat-id`. A scheduler-less bounded
     wait retains the current invocation and token instead of constructing a scheduled heartbeat. Both are a
     **fallback lifecycle, not a tight poll**: background completions are the primary heartbeat. A scheduled
     heartbeat resumes another turn only while its session remains live; a dead session needs the explicit
     resume path (see "Resume after a killed session"). **Size the scheduled delay or bounded wait to the
     nearest stall it guards:**
     - **Run setup still owed — the lease not yet acquired, or adoption / first dispatches not yet
       run (a fresh run's arm-ended setup turn, `run-identity-and-lease.md` "Take a run") → ~60 s:**
       on a turn-ending scheduler the wakeup IS the continuation of setup, so any longer delay is
       dead time the user reads as a hung run.
     - **Any dispatched review pass still awaiting its first line of launch evidence → ~5 min:** its
       Stage 2a launch deadline is then the soonest thing that can fire, and a hung launch must not
       sit undetected for a full normal interval.
     - **A background-task review streaming its stdout → poll toward the quiet window:** it can be
       declared hung before the ~15-min cap once the stream falls quiet (`stage-2-review-gate.md`, the
       stdout-stream liveness signal), so while such a review is streaming, a shorter poll toward that
       quiet window is a real check, not a bare re-reconcile.
     - **Otherwise → ~15 min:** matching the Stage 2a meaningful-progress
       threshold — with no launch deadline pending, nothing can declare a review stalled before then, so a
       shorter interval only re-reconciles git/gh with no new signal (and pays a full reconcile — plus
       added driver-session context — per heartbeat).

     **And whatever tier you picked, NEVER schedule the primary heartbeat past the watchdog deadline** —
     cap the delay at the time `ledger.py watchdog check` says remains. A host with the session-watchdog
     capability also delivers its separate audit nudge at that deadline; this cap is the matching fallback
     on a bounded-wait host and the guard against a later primary tier longer than 45 minutes. (The current
     tiers all sit inside the interval, so the cap binds only if a tier grows.)

     **The health pass — when the run owes a deep look, LOOK before you reschedule.** A run can
     heartbeat forever while nothing moves: every PR parked on a forgotten question, a review silently
     stuck — and each heartbeat looks locally fine, because "nothing moved" is invisible to an agent that
     remembers nothing. Two sensors trigger the same deep look, and **you compute the trigger AFTER
     ownership is established (step 1's lease verdict) and AFTER the nudge read**:

     ```text
     need_health = run is QUIET (nudge, step 1 — no meaningful ledger activity for QUIET_AFTER)
                   OR  ledger.py watchdog check says `due` / `unset` / `invalid`
     ```

     The first is #112's quiet sensor (the durable `last_activity` stamp); the second is the durable
     long-cadence deadline (`ledger.py watchdog`, `files-and-ledger.md`). The separate session watchdog
     nudge (`runtime-adapter.md`, "Session watchdog nudge") may REPORT scheduler state during run
     resolution, but it never decides whether the final re-arm happens — "Primary continuity" above owns
     that, and the re-arm is unconditional. It
     does NOT add a deep-health trigger: a watchdog and primary heartbeat can arrive together, and the first
     health pass re-arms the deadline for the second. The primary wake is continued per "Primary
     continuity" (this turn's final scheduling action); re-ensure a missing watchdog nudge before that
     action. A bounded-wait host reports that it has no separate nudge.
     **When `need_health` — and open PRs remain — this heartbeat runs exactly ONE health pass and then exactly ONE
     `ledger.py --file <state.jsonl> watchdog arm`, and
     completes BOTH before the status render and the turn's final scheduling action** (the last-action rule
     below). **Never two passes for two triggers**: quiet and watchdog-due firing together is still one
     pass, one arm. The pass **relies on this heartbeat's successful `refresh` verdict** (step 1) and adds
     **no lease machinery** of its own. Its contents are nothing new — it re-runs the checks their existing
     owners already define, on the rows that have gone still:
     - **any in-flight review pass** → `review-pass.py status --run <rundir> --verify`, then apply the
       **Stage 2a launch-deadline and meaningful-progress rules** to it (`stage-2-review-gate.md`) — a
       review that died mid-flight is exactly what a quiet streak hides.
     - **any row that can still move** → **re-derive CI** (Stage 2b) and apply the CI watch / RUNNING-STALL
       rules — a watch that wedged wakes no one, and only a swept heartbeat notices.
     - **every parked PR** → **re-surface its question to the user, WITH how long it has waited.** The park
       is not a stall to "fix" (the held-status guard still binds — step 3), but a forgotten question is
       the single most likely reason a run went quiet, so it is what the user needs put back in front of
       them.

     **When the health pass ran, the status this heartbeat renders LEADS with the diagnosis — BEFORE the
     ledger table:** every parked question restated with its waiting age, and every stalled-review finding,
     first; then the mandatory table below. **When every open PR is parked, the run is not stalled — it is
     idle BECAUSE it waits on the user**, and the status says so plainly, leading with the unanswered
     question rather than presenting the idleness as a fault to chase. Then continue the loop per "Primary
     continuity" above (this turn's final scheduling action), exactly as always.

     **The honest boundary: the session watchdog checks a live session, not a dead one.** If the primary
     heartbeat is missing while the session remains live, the watchdog nudge fires, audits it, and the final
     scheduling action restores it. If the session itself dies, both in-session wakes disappear. Recovery is
     a **MANUAL RESUME on every host**: the next `--run <id>` finds a stale lease and adoption reports the
     run as **orphaned**, not merely resumed (`run-identity-and-lease.md`, "Adopt only an orphaned run").

     Continue the loop per "Primary continuity" above whenever non-terminal work remains — skipping the
     re-arm means a hung or orphaned run wakes no one. **Now render the status — this happens on EVERY
     heartbeat that reschedules, never skipped. Its first and mandatory element is the ledger table
     itself** (a health-pass diagnosis, when one fired above, leads *in front of* the table — it never
     replaces or reorders it). Run
     `ledger.py --file <state.jsonl> table` and include its output verbatim, fenced, in the status
     message. **Verbatim means WHOLE** — including every `#` line it prints below the grid. The default
     view is a **filtered** one and those lines are what disclose the filtering
     (`files-and-ledger.md`, "`table` is a PROJECTION"); drop them and the user is shown a subset
     presented as the whole ledger. Never re-type, trim, or re-align it. Then one line per remaining
     wait naming what it waits on (review in flight, CI watch, parked on the user's answer). Render it
     after every ledger write during this heartbeat — the ledger was reconciled this heartbeat, so the table is the
     state the next reconcile resumes from. State whether the session watchdog is armed or unavailable. For
     the primary wake, report the active host's `primary inspect` result — `runtime-adapter.md`, "Session
     watchdog nudge", owns that result set and each host's value — never a claim that the primary wake is
     already armed: nothing arms it before the turn's final action, and the status names that final action
     as whichever runtime branch below the active host takes ("Primary continuity" above). Never imply
     dead-session recovery. Then take
     exactly one runtime branch:
     - **Scheduled-heartbeat host:** with the status above already rendered, schedule-or-replace the
       primary wake as the turn's LAST action ("Primary continuity" above) — scheduling ends the turn on
       this host (`runtime-adapter.md`, "Scheduled-heartbeat host"), so nothing runs after it. The
       scheduled wake begins again at step 1.
     - **Scheduler-less bounded-wait host:** render the same status, wait only until the first task
       completion or the nearest protected deadline, then go directly back to step 1 and reconcile.
       Repeat this status/wait/reconcile cycle while non-terminal work remains. Do **not** execute the
       scheduled-host return after the bounded wait.
   - All this run's PRs `merged` or `aborted` → **distill the run into the carryover ledger** by running
     **`carryover.py distill`** (it projects this run's own file `.gauntlet/history/<run-id>.md` from the
     terminal ledger — merged PRs, aborted PRs + why, and declined-API facts — and REFUSES a run with any
     non-terminal row or an existing file, so it distills exactly once; per-run files never
     contend, see "Fresh runs and carryover"), **release the run** (delete this run's
     `gauntlet-run-<run-id>` owner label via `gh label delete gauntlet-run-<run-id> --yes`, release
     the lease — `lease.py … release --token <tok>`; then remove the session watchdog nudge
     (`runtime-adapter.md`, "Session watchdog nudge"); the shared status labels stay), emit the final
     report, and **do not
     reschedule**. This run's loop ends. **"Rows all terminal" alone is NOT "finished"** — finalization
     (carryover distilled, label deleted, lease released, watchdog removed) may still be **owed**, so
     do these in order. **Leave
     `<rundir>` in place** (do NOT delete it here) — its terminal `state.jsonl` is what lets a later bare
     invocation detect *this* *finished* run and take the "ask the user" branch in step 1 instead of a
     silent exit. (A stale heartbeat firing after exit harmlessly re-hits the finished-run branch via
     its `--run <run-id>`; with the lease released it reads as an un-driven finished run.)

**Idempotency is the load-bearing property.** Because every heartbeat re-derives from git/gh and launches
only work not already in flight, a relaunch after a killed session — or two completions landing close
together — cannot corrupt state or act on a stale verdict (PR-content pinning rejects stale verdicts
at the gate). The worst case is a wasted duplicate review, which is harmless: it's just another fresh,
context-isolated re-roll anyway. The agent is also single-threaded per turn, so heartbeat *decisions* never truly race — only
in-flight tasks do.

#### Resume after a killed session

**Resume after a killed session — including by a different agent instance:** in-flight background
tasks die with the session, but nothing authoritative is lost. A new invocation reconciles against
git/gh and continues — completed work is never redone (existing PRs, landed verdict files); a CI task
whose output file is missing re-launches, and a **review** with no verdict and no live task goes through
**Stage 2a active-attempt resolution** rather than a blind re-launch: read the highest-numbered launch
attempt's `pass_identity` and take the exact branch in `runtime-adapter.md`, **Review preparation
mapping**. **The relaunch budget lives on disk, not in the session**, so it survives
the death of the agent that spent it — otherwise each new instance would rediscover a missing output
file, relaunch the same hung reviewer, die, and repeat forever.

**Every dead pass must land on exactly one branch in Review preparation mapping.** Do NOT gate the resume
path on launch evidence: a dead attempt that wrote a `started` line still has no process capable of
finishing, so launch evidence cannot select or suppress its recovery branch. Launch
evidence answers "is this **live** process working?" and is meaningful **only** for the in-flight
~5-min launch check; once the task is gone, the only question is how much relaunch budget remains. It
binds to the run via
`--run <id>` (which a scheduled heartbeat carries only while its session remains live; after a dead
session, use a manual `--run <id>` resume) or, for a bare re-invocation, by discovering live runs and
adopting the sole **orphaned** one (asking among several). Adoption is gated on the **run lease**: an agent takes
over only a run whose lease is absent or stale, so it can always tell whether another agent is still driving that
ledger and never double-drives an actively-held run (see "Run identity and concurrency" and Loop
control step 1). This is how a later agent picks up exactly where a previous instance left off.

---
