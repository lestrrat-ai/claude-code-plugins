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

   Once bound and confirmed owner, decide on **liveness of THIS run**, not on whether some `state.md`
   exists — and scope **every** git/gh scan to this run's `gauntlet-run-<run-id>` label so another run's
   PRs are never mistaken for your own (adopted PRs keep their OWN head branch, so ownership is the
   LABEL only — never a branch prefix). Live work (this run) = any open PR carrying this run's label,
   **OR** any non-terminal row in this run's `state.md` (`in_review` / `mergeable` / `awaiting-api`).
   Three cases:

   - **This run has live work → resume.** **Reconcile against ground truth** (do NOT redo *completed*
     work — a review/CI task whose output file is missing may be re-launched, since in-flight tasks die
     with their session):
     for each of this run's branches/PRs read the live SHA, CI status, and verdict files, and refresh
     the ledger. Do the PR scan as **one batched snapshot per wake** —
     `gh pr list --label gauntlet-run-<run-id> --json number,headRefName,headRefOid,state,mergeable,mergeStateStatus,labels > <rundir>/prs.json`
     — and drive reconcile from that file; fall back to per-PR `gh pr view` only where the snapshot
     isn't enough (merge-gate CI truth stays the re-polled `gh pr checks` snapshot, Stage 2b). Wake
     turnaround is throughput: every serial `gh` call in reconcile delays every dispatch behind it. Re-read `run_id`, `base_branch`, `api_changes`, and `reviewer` from the ledger header — they
     govern namespacing, the merge/diff target, API-change handling, and which reviewer runs the
     review passes, and must be consulted fresh each wake, never from memory (a wake may be a
     fresh agent instance that just adopted the run, so an explicit/preferred reviewer would otherwise
     be lost and silently revert to the default; Constraints, Base branch, "The reviewer"). Refresh
     the lease. This is the path every `--run` self-wake takes.
   - **No run bound and none live (no `gauntlet-run-*` PR, no non-terminal `<rundir>`) → first run.**
     **Check there is something to adopt BEFORE creating any run state.** If the invocation carries no
     `#PR` args (a bare or non-`#PR` invocation that found no live run to resume — likewise `--new`
     with no `#PR` args), **create nothing** — no run-id, no `<rundir>`, no lease, no `state.md` — and
     **PROMPT** the user: "No PRs under a campaign. Run gauntlet:review to find issues, or pass PR
     numbers to gate." Creating `<rundir>`/lease/header before a PR is confirmed would leave an empty
     orphan run that later no-arg invocations rediscover as bogus state.

     When there **are** `#PR` args, **preflight the whole set FIRST — read-only, before creating any
     run state**: read every PR's metadata (`gh pr view`), run the refusal checks (foreign-owned,
     cross-repo/fork per `pr-adoption.md`), and verify all share a common `baseRefName`. This touches
     **no** run-id, `<rundir>`, lease, `state.md`, label, worktree, or CI watch. **If the bases
     disagree or any PR is refused, prompt and create nothing** — so a rejected set never leaves an
     empty orphan run behind. **Only once the full set passes preflight**: mint a run-id + agent token,
     atomically create `<rundir>`, and write the lease **and `state.md` header** — now with
     `base_branch` filled from the agreed `baseRefName` (known from preflight). Then **adopt** each PR
     (ledger row + labels + worktree + CI watch per `pr-adoption.md`); a death mid-adoption still leaves
     a discoverable, adoptable run. Then fall through to dispatch/reschedule.
   - **This run's `state.md` is fully terminal — every row `merged`/`aborted`, no open PR carrying this
     run's label → the run is finished.** Do **not** silently exit "all fixed" (the old bug) and do **not**
     silently restart. **Ask the user** whether to gate more PRs — e.g. "gauntlet run
     <run-id> finished (N merged, M aborted). Gate more PRs? Pass PR numbers (or run gauntlet:review
     first)." A new run needs a `#PR` set, so collect PR numbers (equivalently direct the user to
     `/gauntlet:campaign --new #PR...`); on a PR set, start a fresh run **with carryover** (see "Fresh
     runs and carryover") — **no run-id/lease/`state.md` is created until that set passes preflight**.
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
   it carries `gauntlet-run-<run-id>`, and set its status label to match its **live** gate state —
   `gauntlet-accepted` if its current HEAD holds `required(tier)` SATISFIED verdicts, else
   `gauntlet-reviewing`; add the status label if it has none. **Never touch another run's PRs.**
2. **Fold in completions.** For any background task that finished (CI watch → `ci-<pr>.txt`; review →
   `review-<pr>-<n>.txt`, with `review-<pr>-<n>.progress.jsonl` as its liveness evidence; CI/review
   fix), record the result against the SHA it ran on and act per Stage 2.
3. **Dispatch due work — non-blocking, idempotent, bounded, work-conserving.** Scan the whole run,
   not just the PR/job that woke you. Launch every due action that fits a free slot before returning.
   Launch only what is actually due *and not already in flight* (check ground truth first, never the
   ledger alone):
   - any newly-adopted PR whose ledger row lacks a `tier`, or any PR whose `head_sha` changed since it
     was last triaged → **re-triage its tier** (deterministic file-class classification of the changed
     files at that `head_sha`; agent-docs = code; default STANDARD on uncertainty — see the tiers
     spec). The tier is pinned to `head_sha` and sets `required(tier)` = **1 if TRIVIAL else 2**.
   - current tip has `reviews_ok < required(tier)`, its **review preconditions are clear** (no
     unaddressed Copilot review items, CI not red, no merge conflict with `<base>` — see Stage 2a
     preconditions), and no review running for that SHA → **first ensure the PR-head worktree exists**
     (the review runs `codex exec -C <worktree>` — the PR row's ledger `worktree` column value, the
     single source of truth for this PR's checkout path (created at adoption/pre-review per
     `pr-adoption.md`; the ledger-recorded `<worktree>` path — default `.worktrees/<headRefName>` when
     campaign creates it, else a reused existing checkout) — and diffs
     `<base>...HEAD`, so a real checkout must be present): if that `<worktree>` is missing, create it
     from the PR head **per `pr-adoption.md` step 5** — which reuses an existing checkout of that branch
     if one exists (root or another worktree), else adds a fresh worktree, since `git worktree add`
     refuses a branch checked out elsewhere — and record its path in the row's `worktree`. This is an
     explicit precondition of the review launch. Then launch **one** review pass as a **background**
     task (one at a time per PR — the second, when the tier requires two, only after the first is
     SATISFIED; Stage 2a). If a precondition is dirty, clear it first (address Copilot items / fix CI /
     rebase) instead of spending a review;
   - CI red and no CI-fix subagent is already in flight for that PR/SHA → dispatch a scoped fix
     subagent (Stage 2b); different PRs may fix CI concurrently within the cap.
   - CI snapshot reads `pending` for a PR whose watch task has already exited → **relaunch the watch
     in this same wake**. A pending PR must never sit unwatched until the heartbeat; the heartbeat is
     a fallback, not the mechanism.
   - about to dispatch content-changing work on a PR (review fix, CI fix, copilot-address,
     conflict-resolving rebase) while a review is in flight on that PR → **stop that review task
     first** (its verdict can only describe a SHA the fix is about to replace); the freed slot goes
     to the next due review.
   - mergeable → queue for serialized merge drain.
   Treat ~8 as a **rolling concurrency cap**, not a wave size: keep up to ~8 CI-fix subagents and ~8
   review processes in flight, refilling each free slot immediately; queue the rest. **Launch, do not
   wait — never barrier on a group of PRs before dispatching the next.**
   Allowed idle state is narrow and explicit: no PR can start a review, no CI/precondition fix is due,
   no exited watch needs relaunching, no PR is mergeable, and every remaining wait is external
   (background review/CI), user/API approval, or a genuinely full cap. If the run has **no PR at all**
   and none is in flight (no-arg idle), do not spin: **PROMPT** "No PRs under a campaign. Run
   gauntlet:review to find issues, or pass PR numbers to gate."
4. **Merge** queued PRs as a serialized drain: re-confirm one candidate against the live SHA, merge
   it, sync `<base>`, reconcile remaining candidates, and repeat while another PR is immediately
   mergeable (Stage 3).
5. **Reschedule or exit.**
   - Any non-terminal PR remains (in review, pending CI, or awaiting API/precondition) →
     refresh this run's lease, then set a `ScheduleWakeup` heartbeat
     (`prompt: "/gauntlet:campaign --run <run-id> --token <agent-token>"` — exactly those two flags:
     `--run` rebinds the wake to this run and `--token` re-proves ownership of its lease). A self-wake
     **never replays `--new` or the original `#PR` adoption args** — the run is *resumed* by `--run`,
     not re-created, and carrying `--new` would mint a brand-new run every heartbeat. This heartbeat is a
     **fallback wake, not a poll**: background completions are the primary wake and normally fire
     first, so the heartbeat forces a wake in the cases **no completion ever arrives** — a background
     task that **hangs** (e.g. a reviewer stuck on input) and never completes, or a **killed/orphaned
     session** whose in-flight tasks died with it, so a later self-wake reconciles and resumes/adopts
     the run (see "Resume after a killed session"). Size the delay to the stall it guards, **~15 min**,
     matching the Stage 2a meaningful-progress threshold: nothing can declare a review stalled before
     then, so a shorter interval only re-reconciles git/gh with no new signal (and pays a fresh-context
     cost per wake). ALWAYS schedule it whenever non-terminal work remains — skipping it means a hung
     or orphaned run wakes no one. Return.
   - All this run's PRs `merged` or `aborted` → **distill the run into the carryover ledger** (write
     this run's block to its own file `.gauntlet/history/<run-id>.md` — merged PRs, aborted
     PRs + why, and declined-API PRs; per-run files never
     contend, see "Fresh runs and carryover"), **release the run** (delete this run's
     `gauntlet-run-<run-id>` owner label via `gh label delete gauntlet-run-<run-id> --yes`, and delete
     `<rundir>/lease.json`; the shared status labels stay), emit the final report, and **do not
     reschedule**. This run's loop ends. **Leave
     `<rundir>` in place** (do NOT delete it here) — its terminal `state.md` is what lets a later bare
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
git/gh and continues — completed work is never redone (existing PRs, landed verdict files); only a
review/CI task whose output file is missing re-launches. It binds to the run via
`--run <id>` (what every self-wake carries, so a fresh instance adopting an orphaned run's heartbeat
just works) or, for a bare re-invocation, by discovering live runs and adopting the sole **orphaned**
one (asking among several). Adoption is gated on the **run lease**: an agent takes over only a run
whose lease is absent or stale, so it can always tell whether another agent is still driving that
ledger and never double-drives an actively-held run (see "Run identity and concurrency" and Loop
control step 1). This is how a later agent picks up exactly where a previous instance left off.

---
