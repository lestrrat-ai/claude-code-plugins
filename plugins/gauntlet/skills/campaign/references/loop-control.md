## Loop control

The skill is **event-driven**. Wakes come from three sources, all handled identically: the first
invocation, a `ScheduleWakeup` firing (heartbeat fallback), and a **background task completing** — a
CI watch, a review, *or* a CI/review fix. All long work runs as background tasks, so the driver never
blocks; each completion is its own wake.

**Every wake — reconcile, dispatch, reschedule:**

1. **Resolve the run + lease, then init / resume / start fresh.** First bind **which run this wake is
   for** and confirm you may drive it, per "Run identity and concurrency": a `--run <id>` self-wake
   presents its `--token` and, under the run's claim lock, continues if the token matches the lease,
   adopts if the lease is absent/stale, or **stands down** if a fresh lease bears a different token; a
   bare invocation **with `#PR` args** starts a NEW run adopting those PRs, while an **arg-less** bare
   invocation discovers runs and adopts the sole **orphaned** one (asks among several, refuses to
   hijack an actively-driven one); a bare invocation with a **non-PR** arg starts nothing — it hits
   the idle prompt (run `gauntlet:review`, or pass PR numbers).
   This claim-locked lease check is what guarantees **no two agents drive one ledger**.

   Once bound and confirmed owner, decide on **liveness of THIS run**, not on whether some `state.jsonl`
   exists — and scope **every** git/gh scan to this run's `gauntlet-run-<run-id>` label so another run's
   PRs are never mistaken for your own (adopted PRs keep their OWN head branch, so ownership is the
   LABEL only — never a branch prefix). Live work (this run) = any open PR carrying this run's label,
   **OR** any non-terminal row in this run's `state.jsonl` (`in_review` / `mergeable` / `awaiting-api` /
   `awaiting-user`).
   Three cases:

   - **This run has live work → resume.** **Reconcile against ground truth** (do NOT redo *completed*
     work — a CI task whose output file is missing may be re-launched, since in-flight tasks die with
     their session. A **review** whose output file is missing is NOT simply re-launched: resolve its
     **active launch attempt** first (Stage 2a) — read the highest-numbered attempt's `pass_identity`
     and dispatch on `launch_attempt` **alone**: `1` → relaunch once (as attempt `2`); `2` → the
     relaunch is spent, so take the **fresh-subagent fallback**. **Launch evidence is irrelevant on
     this path** — the task is already dead, so whether it managed to write a `started` line before
     dying says nothing about whether it will ever produce a verdict. A missing output file must never
     re-arm the relaunch budget, and a dead attempt `2` must never be left un-dispatched):
     for each of this run's branches/PRs read the live SHA, CI status, and verdict files, and refresh
     the ledger — write every ledger update through `scripts/ledger.py … set/header set` **by field
     name** (`files-and-ledger.md`), never by hand-editing rows by column position.

     **This refresh is itself a gate-reset site — relabel here, in this step.** When it detects that a
     PR's live `head_sha` has moved with the PR diff changed (a formatter/bot commit, a manual push,
     any content change this run did not dispatch), it resets `reviews_ok` to 0 — and MUST, in the same
     step, run `gh pr edit <pr> --remove-label gauntlet-accepted --add-label gauntlet-reviewing` on a PR
     carrying `gauntlet-accepted` (`stage-2-review-gate.md`, "Status labels mirror the review gate").
     Do NOT leave this to the label-reconcile pass below: that pass is the **backstop**, and a reset
     site that defers to it is the exact bug this rule forbids. (A clean base-only advance with the PR
     diff unchanged does not reset the gate, so it keeps `gauntlet-accepted`.)

     Do the PR scan as
     **one batched snapshot per wake** — the **same canonical command** `pr-adoption.md` runs, writing the
     **same path with the same schema** (they are the same scan; two spellings of it is how a reader of
     `prs.json` ends up with fields that are not there):

     ```
     gh pr list --label gauntlet-run-<run-id> --state all \
       --json number,headRefName,headRefOid,title,baseRefName,state,mergeable,mergeStateStatus,labels \
       > <rundir>/prs.json
     ```

     — and drive reconcile from that file; fall back to per-PR `gh pr view` only where the snapshot
     isn't enough (merge-gate CI truth stays the re-polled `gh pr checks` snapshot, Stage 2b). Wake
     turnaround is throughput: every serial `gh` call in reconcile delays every dispatch behind it. Re-read `run_id`, `base_branch`, `api_changes`, and `reviewer` from the ledger
     header — they govern namespacing, the merge/diff target, API-change handling, and which reviewer runs
     the review passes, and must be
     consulted fresh each wake, never from memory (a wake may be a fresh agent instance that just
     adopted the run, so an explicit/preferred reviewer would otherwise
     be lost and silently revert to the default; Constraints, Base branch, "The reviewer",
     "PR adoption"). Refresh
     the lease. This is the path every `--run` self-wake takes.
   - **No run bound and none live (no `gauntlet-run-*` PR, no non-terminal `<rundir>`) → first run.**
     **Check there is something to adopt BEFORE creating any run state.** If the invocation carries no
     `#PR` args (a bare or non-`#PR` invocation that found no live run to resume — likewise `--new`
     with no `#PR` args), **create nothing** — no run-id, no `<rundir>`, no lease, no `state.jsonl` — and
     **PROMPT** the user: "No PRs under a campaign. Run gauntlet:review to find issues, or pass PR
     numbers to gate." Creating `<rundir>`/lease/header before a PR is confirmed would leave an empty
     orphan run that later no-arg invocations rediscover as bogus state.

     When there **are** `#PR` args, **preflight the whole set FIRST — read-only, before creating any
     run state**: read every PR's metadata (`gh pr view`), run the refusal checks (foreign-owned,
     cross-repo/fork per `pr-adoption.md`), and verify all share a common `baseRefName`. This touches
     **no** run-id, `<rundir>`, lease, `state.jsonl`, label, worktree, or CI watch. **If the bases
     disagree or any PR is refused, prompt and create nothing** — so a rejected set never leaves an
     empty orphan run behind. **Only once the full set passes preflight**: mint a run-id + agent token,
     atomically create `<rundir>`, and write the lease **and `state.jsonl` header** — now with
     `base_branch` filled from the agreed `baseRefName` (known from preflight). Then **adopt** each PR
     (ledger row + labels + worktree + CI watch per `pr-adoption.md`); a death mid-adoption still leaves
     a discoverable, adoptable run. Then fall through to dispatch/reschedule.
   - **This run's `state.jsonl` is fully terminal — every row `merged`/`aborted`, no open PR carrying this
     run's label → the run is finished.** Do **not** silently exit "all fixed" (the old bug) and do **not**
     silently restart. **Ask the user** whether to gate more PRs — e.g. "gauntlet run
     <run-id> finished (N merged, M aborted). Gate more PRs? Pass PR numbers (or run gauntlet:review
     first)." A new run needs a `#PR` set, so collect PR numbers (equivalently direct the user to
     `/gauntlet:campaign --new #PR...`); on a PR set, start a fresh run **with carryover** (see "Fresh
     runs and carryover") — **no run-id/lease/`state.jsonl` is created until that set passes preflight**.
     With no PR numbers (or "no"), emit that run's final report and stop. This prompt is the *only* wake
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
   it carries `gauntlet-run-<run-id>`, and **overwrite** its status label to match its **live** gate
   state. **Never touch another run's PRs.**

   **Always apply one status label AND remove the other — never merely add.** The two status labels are
   mutually exclusive, so a purely additive reconcile cannot repair the one state it most needs to: a
   PR wearing **both** labels (what a missed swap at a reset site actually produces) would keep the
   contradictory label forever, because adding a label it already carries changes nothing. Write it
   unconditionally, in one call per PR, so the outcome does not depend on which labels are already there:

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
   resets, in the same step that writes `reviews_ok = 0` (`stage-2-review-gate.md`, "Status labels
   mirror the review gate"); this pass only *self-heals* a swap that was somehow missed. A PR that
   reaches this point wearing a stale `gauntlet-accepted` — or both labels at once — means some reset
   site skipped its relabel: fix the label here, and treat it as a bug in that site, not as normal
   operation.
2. **Fold in completions.** For any background task that finished (CI watch → `ci-<pr>.txt`; review →
   the **active launch attempt's** output file, with its progress file as liveness evidence — attempt 1
   writes `review-<pr>-<n>.txt` / `.progress.jsonl`, a relaunch writes `review-<pr>-<n>.a<k>.*`, and
   only the attempt named in the current `pass_identity` is read or counted (Stage 2a); CI/review fix),
   record the result against the SHA it ran on and act per Stage 2.
3. **Dispatch due work — non-blocking, idempotent, bounded, work-conserving.** Scan the whole run,
   not just the PR/job that woke you. Launch every due action that fits a free slot before returning.
   Launch only what is actually due *and not already in flight* (check ground truth first, never the
   ledger alone).

   **PARKED-STATUS GUARD — a PROPERTY, not a list. Apply it BEFORE every bullet below, and before
   every other action this skill takes on a PR.** While a PR's `status` is **`awaiting-user`** or
   **`awaiting-api`** the PR is **FROZEN: take NO action that MUTATES it.** It is waiting on a
   **HUMAN**, and no amount of machine work can resolve it. **Skip it and keep driving the run's other
   PRs** — the run stays live and the park NEVER blocks the loop (`run-identity-and-lease.md`, "Never
   hold the run hostage on a user prompt").

   **The test is "does this MUTATE the PR?" — NOT "is this action named in a list?"** *Mutate* = change
   the PR or dispatch work that will: its content, its head commit, its base, its labels, its
   open/merged state. Non-exhaustively: no review pass, no CI fix, no review fix, no copilot-address, no
   precondition fix, no merge, no base refresh, no rebase (clean **or** conflict-resolving), no push, no
   relabel, no gate reset, no content change of any kind — **and nothing absent from this list either.**
   The list is **illustrative; the property governs.** An enumeration of dispatch sites WILL miss one —
   it already did: this guard once listed four (review, CI fix, review fix, merge) and missed
   `stage-3-merge.md` step 6, whose post-merge rebase of PRs that fell behind would have moved a parked
   PR's `head_sha`, reset its gate, and **changed the very PR content the user was parked to
   adjudicate**. Any site the skill grows later is covered the moment it would mutate a parked PR, with
   no edit to this list. When unsure whether an action mutates, treat it as mutating and skip it.
   - **The ONE exception is the CI watch: OBSERVING a PR is not mutating it.** A parked PR **keeps its
     watch**, and an exited watch on a parked PR whose CI reads pending is **relaunched as usual**, so
     its CI state is current the moment the user answers. But do **NOT** dispatch a CI *fix*.
   - **Recording ground truth is not mutating either.** Reconcile still READS a parked PR (live SHA, CI,
     labels) and writes what it read to the ledger — including a `reviews_ok` reset, and its label
     mirror, when **someone else** pushed to the PR (step 1). Recording a change campaign did not make is
     not making one. What is frozen is **campaign's own action on the PR**; a park never licenses a
     lying label or a stale row.
   - **Only the user's answer unparks a PR.** On the answer: record it (`api_approval` for the API
     park; the audit record for the standoff ruling), set `status` back to `in_review` via
     `ledger.py … set --pr <N> --status in_review`, and resume normal dispatch — including any rebase or
     base refresh the PR has been owed while frozen — from the next wake. (A declined API change goes
     terminal `aborted` instead.) A parked PR that has fallen **behind** its base simply **stays
     behind** until then; it is not dropped from the run, just frozen.
   - **Why the guard must live HERE, at the dispatch site:** `reviews_ok < required(tier)` is TRUE for a
     parked PR (the park does not raise it), so a dispatch rule that looks only at `reviews_ok` will
     happily re-review a PR that is waiting on a human — and a `SATISFIED` verdict would then carry it
     to `mergeable` and **merge it WITHOUT the user's ruling**, which is exactly the hole the standoff
     park exists to close. **The park MUST be enforced wherever the PR is ACTED ON — every dispatch site
     and every mutation site — not merely recorded in the ledger.**

   Then, for each **non-parked** PR:
   - any newly-adopted PR whose ledger row lacks a `tier`, or any PR whose `head_sha` changed since it
     was last triaged → **re-triage its tier** (deterministic file-class classification of the changed
     files at that `head_sha`; agent-docs = code; default STANDARD on uncertainty — see the tiers
     spec) and write it back with `ledger.py … set --pr <N> --tier <tier>`. The tier is pinned to
     `head_sha` and sets `required(tier)` = **1 if TRIVIAL else 2**.
   - current tip has `reviews_ok < required(tier)`, its **review preconditions are clear** (no
     unaddressed Copilot review items, CI not red, no merge conflict with `<base>` — see Stage 2a
     preconditions), and no review running for that SHA → **first ensure the PR-head worktree exists**
     (the review runs `codex exec -C <worktree>` — the PR row's ledger `worktree` column value, the
     single source of truth for this PR's checkout path (created at adoption/pre-review per
     `pr-adoption.md`; the ledger-recorded `<worktree>` path — default `.worktrees/<headRefName>` when
     campaign creates it, else a reused existing checkout) — and diffs
     `origin/<base>...HEAD`, so a real checkout must be present): if that `<worktree>` is missing, create it
     from the PR head **per `pr-adoption.md` step 5** — which reuses an existing checkout of that branch
     if one exists (root or another worktree), else adds a fresh worktree, since `git worktree add`
     refuses a branch checked out elsewhere — and record its path in the row's `worktree`. This is an
     explicit precondition of the review launch. **Also fetch `origin/<base>` fresh before the first
     review dispatch** (`git fetch origin refs/heads/<base>:refs/remotes/origin/<base>`) — the review
     diffs `origin/<base>...HEAD`, a remote-tracking ref that always exists, since adoption fetches only
     the PR head and a local `<base>` may be absent or stale (see `pr-adoption.md` / Stage 2a). Then
     launch **one** review pass as a **background**
     task (one at a time per PR — the second, when the tier requires two, only after the first is
     SATISFIED; Stage 2a). If a precondition is dirty, clear it first (address Copilot items / fix CI /
     rebase) instead of spending a review;
   - a review pass is in flight but the **active attempt's** progress file holds **no launch evidence**
     — no reviewer-written line of ANY kind after `pass_identity` (a `progress` `started`/`done` event
     *or* a `plan_amendment_request` all count) — past its **~5-min launch deadline** (measured from
     that file's `pass_identity.dispatched_at`) →
     it **never started** (Stage 2a launch check — a reviewer hung on stdin, a bad path, a sandbox
     denial). Kill the task, re-check the command for the known launch faults (above all `< /dev/null`
     on `codex exec`), and re-dispatch the pass once into **attempt-scoped artifacts**
     (`review-<pr>-<n>.a2.*`, fresh `pass_identity` with `launch_attempt: 2` — never the dead attempt's
     files, which a surviving process could still write to); a dead `launch_attempt: 2` →
     fresh-subagent fallback. A failed launch yields no verdict: it never touches `reviews_ok` and
     never bumps the row's `attempts`;
   - CI red and no fix is already in flight for that PR/SHA → **CLASSIFY the failure from the check logs
     first (Stage 2b, "Classify, then set the model") — never dispatch a subagent straight off a red
     check.** The class picks the model, set **explicitly**: a **formatting/lint** failure → a scoped CI-fix
     subagent on a **cheap** model (`sonnet`; `haiku` only when trivially mechanical), which runs a
     formatter, **reads the resulting diff**, and **escalates** anything it cannot verify; **everything
     else** — and every **escalation** — → a scoped CI-fix subagent on the **session model**. Either way the
     resulting commit **resets the gate** (Stage 2b, "Any campaign commit to the PR head resets the gate").
     The subagent's job order, the no-weakening prohibition, and the denylist live in `stage-2-ci.md` —
     follow them there; do NOT restate them here. Different PRs may fix CI concurrently within the cap.
   - CI snapshot reads `pending` for a PR whose watch task has already exited → **relaunch the watch
     in this same wake**. A pending PR must never sit unwatched until the heartbeat; the heartbeat is
     a fallback, not the mechanism.
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
   and none is in flight (no-arg idle), do not spin: **PROMPT** "No PRs under a campaign. Run
   gauntlet:review to find issues, or pass PR numbers to gate."
4. **Merge** queued PRs as a serialized drain: re-confirm one candidate against the live SHA **and
   re-check it is not parked** (the parked-status guard binds the merge too — Stage 3), merge
   it, sync `<base>`, reconcile remaining candidates, and repeat while another PR is immediately
   mergeable (Stage 3).
5. **Reschedule or exit.**
   - Any non-terminal PR remains (in review, pending CI, or awaiting a user ruling on a review-finding
     standoff / API approval / precondition) →
     refresh this run's lease, then set a `ScheduleWakeup` heartbeat
     (`prompt: "/gauntlet:campaign --run <run-id> --token <agent-token>"` — exactly those two flags:
     `--run` rebinds the wake to this run and `--token` re-proves ownership of its lease). A self-wake
     **never replays `--new` or the original `#PR` adoption args** — the run is *resumed* by `--run`,
     not re-created, and carrying `--new` would mint a brand-new run every heartbeat. This heartbeat is a
     **fallback wake, not a poll**: background completions are the primary wake and normally fire
     first, so the heartbeat forces a wake in the cases **no completion ever arrives** — a background
     task that **hangs** (e.g. a reviewer stuck on input) and never completes, or a **killed/orphaned
     session** whose in-flight tasks died with it, so a later self-wake reconciles and resumes/adopts
     the run (see "Resume after a killed session"). **Size the delay to the nearest stall it guards:**
     **~5 min** while any dispatched review pass is still awaiting its first line of **launch evidence**
     — its Stage 2a launch deadline is then the soonest thing that can fire, and a hung launch must not
     sit undetected for a full heartbeat — otherwise **~15 min**, matching the Stage 2a meaningful-progress
     threshold: with no launch deadline pending, nothing can declare a review stalled before then, so a
     shorter interval only re-reconciles git/gh with no new signal (and pays a fresh-context cost per
     wake). ALWAYS schedule a heartbeat whenever non-terminal work remains — skipping it means a hung
     or orphaned run wakes no one. Return.
   - All this run's PRs `merged` or `aborted` → **distill the run into the carryover ledger** (write
     this run's block to its own file `.gauntlet/history/<run-id>.md` — merged PRs, aborted
     PRs + why, and declined-API PRs; per-run files never
     contend, see "Fresh runs and carryover"), **release the run** (delete this run's
     `gauntlet-run-<run-id>` owner label via `gh label delete gauntlet-run-<run-id> --yes`, and delete
     `<rundir>/lease.json`; the shared status labels stay), emit the final report, and **do not
     reschedule**. This run's loop ends. **Leave
     `<rundir>` in place** (do NOT delete it here) — its terminal `state.jsonl` is what lets a later bare
     invocation detect *this* *finished* run and take the "ask the user" branch in step 1 instead of a
     silent exit. (A stale heartbeat firing after exit harmlessly re-hits the finished-run branch via
     its `--run <run-id>`; with the lease released it reads as an un-driven finished run.)

**Idempotency is the load-bearing property.** Because every wake re-derives from git/gh and launches
only work not already in flight, a relaunch after a killed session — or two completions landing close
together — cannot corrupt state or act on a stale verdict (PR-content pinning rejects stale verdicts
at the gate). The worst case is a wasted duplicate review, which is harmless: it's just another fresh,
context-isolated re-roll anyway. The agent is also single-threaded per turn, so wake *decisions* never truly race — only
in-flight tasks do.

**Resume after a killed session — including by a different agent instance:** in-flight background
tasks die with the session, but nothing authoritative is lost. A new invocation reconciles against
git/gh and continues — completed work is never redone (existing PRs, landed verdict files); a CI task
whose output file is missing re-launches, and a **review** with no verdict and no live task goes through
**Stage 2a active-attempt resolution** rather than a blind re-launch: read the highest-numbered launch
attempt's `pass_identity` and dispatch on `launch_attempt` alone — `1` → relaunch once as attempt `2`;
`2` → fresh-subagent fallback. **The relaunch budget lives on disk, not in the session**, so it survives
the death of the agent that spent it — otherwise each new instance would rediscover a missing output
file, relaunch the same hung reviewer, die, and repeat forever.

**Every dead pass must land on exactly one of those two branches.** Do NOT gate the resume path on
launch evidence: a dead attempt `2` that *did* write a `started` line before its session died would
then satisfy neither "relaunch" (budget spent) nor "fall back" (evidence present) — no rule would fire
and the PR would stall forever, which is the very failure this feature exists to prevent. Launch
evidence answers "is this **live** process working?" and is meaningful **only** for the in-flight
~5-min launch check; once the task is gone, the only question is how much relaunch budget remains. It
binds to the run via
`--run <id>` (what every self-wake carries, so a fresh instance adopting an orphaned run's heartbeat
just works) or, for a bare re-invocation, by discovering live runs and adopting the sole **orphaned**
one (asking among several). Adoption is gated on the **run lease**: an agent takes over only a run
whose lease is absent or stale, so it can always tell whether another agent is still driving that
ledger and never double-drives an actively-held run (see "Run identity and concurrency" and Loop
control step 1). This is how a later agent picks up exactly where a previous instance left off.

---
