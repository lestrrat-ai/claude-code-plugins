## Rules

- Runs are isolated by `run_id`: a run touches ONLY its own `<rundir>`, its `state.md`, and PRs/branches
  carrying its `gauntlet-run-<run-id>` label / `fix-<run-id>-` prefix. NEVER reconcile, review, fix,
  merge, relabel, or clean up another run's work — scope every git/gh scan by that label or prefix.
- One active driver per run, enforced by `<rundir>/lease.json` under an atomic `mkdir <rundir>/claim.lock`:
  take/adopt a run only inside the claim lock, and adopt ONLY when its lease is absent or stale
  (`now - updated` > ~30 min); refresh the lease every wake AND around long foreground ops; on a
  self-wake whose lease is fresh but bears a different token, **stand down** — never double-drive a ledger.
- Every self-wake carries `--run <run-id> --token <agent-token>` (ScheduleWakeup + background
  completions); the token re-proves lease ownership so a summarized wake never mistakes its own run for
  another's. Re-read `run_id` from the ledger each wake, never from memory.
- Resume is intent-scoped: a fresh instance resumes via `--run <id>` or an **arg-less** bare invocation
  (adopts the sole orphaned run). A **scoped** bare invocation and `--new` start an independent new run
  and never pre-empt other live runs.
- Carryover is **one file per run** under `.gauntlet/history/<run-id>.md`: a run writes and
  prunes only its own file, so appends never contend and there's no shared-file rewrite to race.
- Run-owned git/GitHub operations are authorized by invocation: `add`, `commit`, `push`, PR
  create/update, labels/checks/comments, and merge. Ask only for public API changes, active-run
  takeover, uncertain carryover pruning, or out-of-scope/destructive work.
- NEVER commit the run's own bookkeeping — the whole `.gauntlet/**` tree: the `<rundir>` under
  `.gauntlet/tmp/**` (ledger, plans, progress, review/CI outputs, lease) and the carryover tree
  `.gauntlet/history/**`. A fix commit stages ONLY the specific source files it changes, by explicit
  path — never `git add -A` / `git add .`, which would sweep in run state. The tree must be
  git-ignored; add `.gauntlet/` to `.gitignore` if missing.
- NEVER `rm -rf .gauntlet/` — that destroys the durable carryover history. Only `.gauntlet/tmp/**` is
  disposable.
- NEVER pass destructive instructions (delete, force-push, reset) to `codex exec`.
- NEVER use `--dangerously-bypass-approvals-and-sandbox`; always `--sandbox workspace-write`.
- One finding = one tightly-scoped PR. Do not bundle unrelated fixes.
- PR-first loop is mandatory: implement → commit → push → create/update PR → watch CI + review PR
  HEAD. NEVER do local gauntlet reviews first and wait to push/open a PR until the end.
- Fan-out is a **rolling cap (~8 in flight), never a barrier wave**: backfill each freed slot with the
  next `pending` finding immediately. Never let a draining group of findings stall the backlog —
  Loop-control step 3 owns this refill for both initial fan-out and resume.
- Work-conserving dispatch is mandatory: every wake scans all findings/PRs and launches every due
  action that fits a free slot before returning. Waiting is allowed only when no useful action is
  launchable anywhere in the run.
- Stage 0 is **pipelined, never a blocking phase**: sweep shards and verification chunks run as
  background tasks; each verification chunk's confirmed findings are deduped incrementally against
  the run-wide survivor set and fanned out immediately. NEVER barrier on the full sweep or full
  verification before starting fixes, and NEVER run a sweep as a blocking foreground call.
- A pending-CI PR must ALWAYS have a live watch: if the CI snapshot reads pending and the watch task
  has exited (including after any rebase/push), relaunch the watch in the same wake — never wait for
  the heartbeat.
- Stop a PR's in-flight review before dispatching content-changing work on it (review fix, CI fix,
  copilot-address, conflict-resolving rebase): a verdict on a doomed SHA wastes tokens and a review
  slot. Refill the slot with the next due review.
- Reconcile from ONE batched `gh pr list --label gauntlet-run-<run-id> --json …` snapshot per wake
  (`<rundir>/prs.json`); per-PR `gh` calls only where the snapshot falls short. Merge-gate CI truth
  stays the re-polled `gh pr checks` snapshot.
- Carryover pruning NEVER blocks a fresh-run start: keep uncertain entries, launch Stage 0
  immediately, ask the user asynchronously, and fold the answer in as its own wake.
- Public API surface/behavior changes need user confirmation by default (see Constraints). The
  `api_changes` flag lives in the ledger header and is re-read every wake — never trust memory, never
  auto-merge an unapproved API break.
- Before queueing a review pass on a PR, clear its preconditions on the current tip: address any
  GitHub Copilot review items (`/gauntlet:copilot-address-reviews <pr>`), fix any CI failures (one at a time,
  prefer a scoped subagent), and rebase away any conflict with `<base>`. PR-content changes reset
  verdicts. Clean base-only rebase with unchanged PR diff keeps `reviews_ok` and sets `ci = pending`.
  Never spend a review over open Copilot items, a red check, or a conflicting PR (Stage 2a).
- Reviews are fresh, context-isolated re-rolls: separate `codex exec` each pass, no shared context.
  Two passes re-roll a stochastic reviewer to catch a missed defect — they are NOT statistically
  independent (the same diff, task, and protocol correlate them; the normal codex path also shares
  model/prompt), so the gate is a miss-catcher, not a proof of correctness.
- Before each review, write an orchestrator-owned `review-<pr>-<n>.plan.jsonl`; reviewers append
  `review-<pr>-<n>.progress.jsonl` events against planned units. Meaningful progress = planned unit
  `done` or accepted plan amendment, not vague "still working" output. Stale meaningful progress →
  suspicious review → retry/fallback per Stage 2a.
- Reviewers do not own the plan but must not treat it as presumptively complete: critically evaluate
  its coverage first, and raise any omitted dimension or materially wrong unit via a
  `plan_amendment_request` event rather than silently reviewing only the listed units. Never rewrite
  the plan or self-grant units (Stage 2a).
- After finishing every planned unit, a pass runs a brief UNSTRUCTURED ADVERSARIAL SWEEP for defects
  outside the plan's decomposition (cross-unit interactions, unstated assumptions, edge cases,
  unenumerated categories). It complements — never replaces — the plan, reports only concrete
  `file:line` defects at the normal finding bar (a real one → NOT SATISFIED), and treats "nothing
  found" as a fine result; no speculative "might be fragile" notes (Stage 2a).
- A SATISFIED verdict carries one `RESIDUAL-RISK: <area> — <why>` line (the least-certain part of the
  diff). It is calibration metadata, never a finding: it never withholds the gate, never enters the fix
  loop, and is never fed into the corroborating review. Do not manufacture a concern to fill it; a real
  defect found while identifying it is a normal finding → NOT SATISFIED (Stage 2a).
- One decision at N sites is the most common root cause. Trigger the §2a-deep root-cause pass on the
  **first** "missing/wrong at site X" finding (its shape, not a round count), map the whole space with
  a dedicated **read-only mapper** subagent — never one that also fixes, which under-maps toward what
  it can reach — and fix at a **single chokepoint**. Hard backstop: a 2nd `NOT SATISFIED` on one PR
  forces the pass (Bailout).
- The two reviews on a PR run **sequentially, never queued together**: launch the first, wait for its
  verdict, and launch the second **only if the first came back SATISFIED**. A NOT-SATISFIED first
  review means a fix lands and the SHA changes, so a concurrently-queued second review would be burned
  on a commit that's about to be replaced — wasted tokens. (Reviews for *different* PRs still run
  concurrently; only the two for the same PR serialize. See Stage 2a.)
- Verdicts are pinned to reviewed PR content: any PR-content change (review fix / CI fix /
  conflict-resolving rebase / bot or manual PR-branch commit) makes prior verdicts stale. Base
  advancement with no conflict and unchanged PR diff does NOT invalidate verdicts; carry `reviews_ok`
  forward, update `head_sha`, and require fresh CI.
- Resume vs. fresh run is decided by **liveness**, not by `state.md` existing: live work → resume
  (no Stage 0/1); a finished prior run → ask the user before a fresh run; `--new` → fresh run with
  carryover (Loop control step 1). A finished run must never silently exit "all fixed" or silently
  restart.
- A fresh run carries over prior knowledge from `.gauntlet/history/` (refuted to suppress,
  unresolved to re-surface, merged to dedup) but still judges every finding fresh — carryover is
  advisory, never auto-accept/reject.
- Prune `.gauntlet/history/` at every fresh run: drop only entries unambiguously moot against
  current `<base>`; for anything uncertain, list it and ask the user before deleting. Never silently
  prune an entry you're unsure about.
- If `codex exec` can't deliver a verdict (quota/rate-limit, auth, timeout, or other system error —
  *not* a real finding list / `VERDICT:` line), retry once, then do the equivalent work with your own
  subagents: the Stage 0 adversarial sweep, or a fresh, context-isolated subagent pass in Stage 2a. The
  gate is unchanged — note any fallback pass in the report. See "Codex fallback".
- CI status comes from a re-polled `gh pr checks` snapshot with **zero fail AND zero pending lines** —
  never from the `--watch` exit code (it can exit 0 on pending/unregistered checks). No green, no merge.
- The run targets a **base branch** (`base_branch` in the ledger header), which is **not assumed to
  be `main`** — it defaults to the branch checked out at invocation. PRs open with `--base <base>`,
  worktrees branch off `<base>`, and reviews diff `<base>...HEAD`. Re-read it each wake (see "Base
  branch").
- After every merge, fast-forward local `<base>` to `origin/<base>` (Stage 3 step 4) so subsequent
  fan-out worktrees and `<base>...HEAD` diffs branch off the just-merged tip, not a stale base. If the
  fast-forward fails, bail out — never force it.
- No "Test plan" section in PR bodies.
