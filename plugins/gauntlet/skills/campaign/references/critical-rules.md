## Rules

- Runs are isolated by `run_id`: a run touches ONLY its own `<rundir>`, its `state.jsonl`, and the PRs
  carrying its `gauntlet-run-<run-id>` label. Adopted PRs keep their OWN head branch, so ownership is
  scoped by that LABEL only (never a branch prefix). NEVER reconcile, review, fix, merge, relabel, or
  clean up another run's work — scope every git/gh scan by that label.
- One active driver per run, enforced by `<rundir>/lease.json` under an atomic `mkdir <rundir>/claim.lock`:
  take/adopt a run only inside the claim lock, and adopt ONLY when its lease is absent or stale
  (`now - updated` > ~30 min); refresh the lease every wake AND around long foreground ops; on a
  self-wake whose lease is fresh but bears a different token, **stand down** — never double-drive a ledger.
- Every self-wake carries `--run <run-id> --token <agent-token>` (ScheduleWakeup + background
  completions); the token re-proves lease ownership so a summarized wake never mistakes its own run for
  another's. Re-read `run_id` from the ledger each wake, never from memory.
- Resume is intent-scoped: a fresh instance resumes via `--run <id>` or an **arg-less** bare invocation
  (adopts the sole orphaned run). A bare invocation **with `#PR` args** and `--new` start an independent
  new run (adopting those PRs) and never pre-empt other live runs; a **non-PR** arg starts nothing —
  it hits the idle prompt.
- Carryover is **one file per run** under `.gauntlet/history/<run-id>.md`. In normal operation a run
  WRITES only its OWN file, so concurrent runs never contend on a shared rewrite. A **fresh** run,
  while pruning history, MAY edit or remove OTHER runs' files — but only those of **finished** runs
  (no live writer/lease), never a file an actively-driven run owns — so there's still no write
  contention with a live writer.
- Run-owned git/GitHub operations are authorized by invocation: `add`, `commit`, `push`, and — on
  adopted PRs — PR update, labels/checks/comments, and merge. Campaign never opens its own PR (PR
  creation lives only in the `gauntlet:review` handoff); it ADOPTS existing PRs. Ask only for public
  API changes, active-run takeover, uncertain carryover pruning, or out-of-scope/destructive work.
- NEVER commit the run's own bookkeeping — the whole `.gauntlet/**` tree: the `<rundir>` under
  `.gauntlet/tmp/**` (ledger, plans, progress, review/CI outputs, lease) and the carryover tree
  `.gauntlet/history/**`. A fix commit stages ONLY the specific source files it changes, by explicit
  path — never `git add -A` / `git add .`, which would sweep in run state. The tree must be
  git-ignored; add `.gauntlet/` to `.gitignore` if missing.
- NEVER `rm -rf .gauntlet/` — that destroys the durable carryover history. Only `.gauntlet/tmp/**` is
  disposable.
- NEVER pass destructive instructions (delete, force-push, reset) to an external reviewer command
  (e.g. `codex exec`).
- NEVER use `--dangerously-bypass-approvals-and-sandbox` with an external reviewer; always
  `--sandbox workspace-write`.
- One PR = one unit. Campaign gates whole adopted PRs; do not split or bundle them.
- PR-gating loop is mandatory: adopt PR → triage tier → watch CI + review PR HEAD → merge. Campaign
  gates **existing** PRs; it NEVER writes fixes from scratch (only review/CI fixes on an adopted PR).
- Concurrency is a **rolling cap (~8 in flight), never a barrier wave**: keep up to ~8 review passes
  and ~8 CI-fix subagents in flight, backfilling each freed slot immediately. Never let a draining
  group of PRs stall the backlog — Loop-control step 3 owns this refill.
- Work-conserving dispatch is mandatory: every wake scans all PRs and launches every due
  action that fits a free slot before returning. Waiting is allowed only when no useful action is
  launchable anywhere in the run.
- A pending-CI PR must ALWAYS have a live watch: if the CI snapshot reads pending and the watch task
  has exited (including after any rebase/push), relaunch the watch in the same wake — never wait for
  the heartbeat.
- Stop a PR's in-flight review before dispatching content-changing work on it (review fix, CI fix,
  copilot-address, conflict-resolving rebase): a verdict on a doomed SHA wastes tokens and a review
  slot. Refill the slot with the next due review.
- Reconcile from ONE batched `gh pr list --label gauntlet-run-<run-id> --json …` snapshot per wake
  (`<rundir>/prs.json`); per-PR `gh` calls only where the snapshot falls short. Merge-gate CI truth
  stays the re-polled `gh pr checks` snapshot.
- Carryover pruning NEVER blocks a fresh-run start: keep uncertain entries, adopt the run's PRs
  immediately, ask the user asynchronously, and fold the answer in as its own wake.
- Public API surface/behavior changes need user confirmation by default (see Constraints). The
  `api_changes` flag lives in the ledger header and is re-read every wake — never trust memory, never
  auto-merge an unapproved API break.
- Before queueing a review pass on a PR, clear its preconditions on the current tip: address any
  GitHub Copilot review items (`/gauntlet:copilot-address-reviews <pr>`), fix any CI failures (one at a time,
  prefer a scoped subagent), and rebase away any conflict with `<base>`. PR-content changes reset
  verdicts. Clean base-only rebase with unchanged PR diff keeps `reviews_ok` and sets `ci = pending`.
  Never spend a review over open Copilot items, a red check, or a conflicting PR (Stage 2a).
- The review gate is **tier-dependent**: `required(tier)` fresh, context-isolated `SATISFIED` verdicts
  on the same live PR content — **one if TRIVIAL, two otherwise** (any code / agent-doc / sensitive
  change always requires two). Re-derive the tier from `head_sha` each wake.
- **NEVER leave `gauntlet-accepted` on a PR whose live content no longer holds `required(tier)`
  SATISFIED verdicts.** The label is a projection of `reviews_ok`, and it is the only run state a human
  sees on GitHub — a stale `gauntlet-accepted` publicly claims a PR passed a gauntlet it did not. So the
  **gate and the label move together, in the same step**: every action that drops `reviews_ok` to 0 (a
  `NOT SATISFIED` verdict, a review/CI/copilot fix commit, a conflict-resolving rebase, any other
  content change on the head branch) MUST also run `gh pr edit <pr> --remove-label gauntlet-accepted
  --add-label gauntlet-reviewing`. Never defer the swap to the next wake — that leaves the label lying
  until reconcile, and lying forever if the session dies first. A **clean base-only rebase** with an
  unchanged PR diff does NOT reset the gate, so it correctly KEEPS `gauntlet-accepted` (it only sets
  `ci = pending`). Per-wake label reconcile is the self-healing backstop, never the mechanism
  (`stage-2-review-gate.md`, "Status labels mirror the review gate").
- Reviews are fresh, context-isolated re-rolls: a separate reviewer invocation each pass (Claude
  subagent by default, or the user's preferred reviewer), no shared context. A second pass re-rolls a
  stochastic reviewer to catch a missed defect — the two are NOT statistically independent (the same
  diff, task, and protocol correlate them; same-reviewer passes also share model/prompt), so the gate
  is a miss-catcher, not a proof of correctness.
- Before each review, write an orchestrator-owned `review-<pr>-<n>.plan.jsonl` (per-pass — a relaunch
  reuses it); reviewers append progress events against planned units to the **active launch attempt's**
  progress file (`review-<pr>-<n>.progress.jsonl` for attempt 1, `review-<pr>-<n>.a<k>.progress.jsonl`
  for a relaunch — only the attempt named in the active `pass_identity` is read or counted). Meaningful
  progress = planned unit `done` or accepted plan amendment, not vague "still working" output. Two
  distinct bars, never collapsed: **launch evidence** = ANY reviewer-written line after `pass_identity`
  (a `started`/`done` `progress` event *or* a `plan_amendment_request`) — none within ~5 min of dispatch
  → the pass never started → kill + relaunch into attempt-scoped artifacts per the Stage 2a launch
  check. **Meaningful progress** is the stronger bar (`done`/accepted amendment) — stale for ~15 min →
  suspicious review → retry/fallback per Stage 2a. Both bars judge a **live** process. A pass whose
  task is **dead** with no verdict (killed session) ignores launch evidence entirely and dispatches on
  `launch_attempt` alone: `1` → relaunch once; `2` → fresh-subagent fallback. Never leave a dead pass
  on neither branch.
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
- When a PR's tier requires two reviews they run **sequentially, never queued together**: launch the
  first, wait for its verdict, and launch the second **only if the first came back SATISFIED**. A
  NOT-SATISFIED first review means a fix lands and the SHA changes, so a concurrently-queued second
  review would be burned on a commit that's about to be replaced — wasted tokens. A **TRIVIAL** PR
  needs only one SATISFIED pass, so there is no second review to sequence. (Reviews for *different* PRs
  still run concurrently; only the two for the same PR serialize. See Stage 2a.)
- Verdicts are pinned to reviewed PR content: any PR-content change (review fix / CI fix /
  conflict-resolving rebase / bot or manual PR-branch commit) makes prior verdicts stale. Base
  advancement with no conflict and unchanged PR diff does NOT invalidate verdicts; carry `reviews_ok`
  forward, update `head_sha`, and require fresh CI.
- Resume vs. fresh run is decided by **liveness**, not by `state.jsonl` existing: live work → resume;
  a finished prior run → ask the user before a fresh run; `--new` → fresh run with
  carryover (Loop control step 1). A finished run must never silently exit "all done" or silently
  restart.
- A fresh run carries over prior knowledge from `.gauntlet/history/` (merged/aborted PR record, to
  dedup and inform) but still judges every adopted PR fresh — carryover is advisory, never
  auto-accept/reject.
- Prune `.gauntlet/history/` at every fresh run: drop only entries unambiguously moot against
  current `<base>`; for anything uncertain, list it and ask the user before deleting. Never silently
  prune an entry you're unsure about.
- **Set the model explicitly on EVERY subagent dispatch** (`SKILL.md`, "Subagent Dispatch"). An unset
  model silently inherits the session model — a cost decision taken by default. **NO class is downgraded
  BY DEFAULT**: review passes and the subagent-fallback review *are* the gate; CI-fix and review-fix write
  **code that gets merged**; the root-cause **mapper** feeds an expensive fix. Nothing downstream
  *guarantees* a bad result is caught — CI misses a wrong-but-green fix, and the review gate is a
  miss-catcher, not a proof of correctness — so no class's mistakes are reliably absorbed. NEVER claim CI
  catches a bad fix. NEVER downgrade the mapper on the grounds that it is "read-only": read-only is not
  low-judgment, and an under-map is invisible.
- **The only sanctioned downgrade: a total-oracle CI check** (`SKILL.md`, "The one exception";
  `stage-2-ci.md`). Allowed IFF (a) re-running the failing check **fully verifies** its own fix
  (`gofmt`, `goimports`, `golangci-lint` rules), AND (b) the fix changes **no product behavior**, AND (c)
  the diff touches **no check definition, config, or test**. **Tier 0 — run the autofixer tool, no
  subagent at all** (`golangci-lint run --fix`, `gofmt -w`, `prettier --write`, …); **Tier 1** — no
  autofixer → cheap model (`haiku`/`sonnet`), ACCEPT only if the exact failing check now passes and the
  diff touches no check/config/test, else **discard and re-dispatch on the session model**. Keyed on
  **check/linter rule IDENTITY**, NEVER on a failure "looking mechanical". Default is NOT whitelisted;
  unknown check → session model. Failing product tests are NEVER whitelisted. The review gate still reads
  the whole diff — the whitelist lowers cost, not the gate.
- A CI-fix subagent MUST be dispatched under the no-weakening prohibition (`stage-2-ci.md`): fix the
  cause, NEVER gut an assertion, add `skip`/`xfail`, disable a lint rule, or raise a timeout to force
  green.
- Scope every fix subagent to its worktree + concrete issue list; tell it NOT to re-derive the whole diff.
  That, not the model tier, is where fix-subagent savings live.
- Default reviewer is Claude's own subagents; no external tool is required. Use the user's preferred
  reviewer when one is set (explicit invocation, or a preference in memory/`CLAUDE.md`/carryover). A
  different-model reviewer (e.g. Codex) is recommended for diversity but never required. See
  "The reviewer".
- If an *external* reviewer can't deliver a verdict (quota/rate-limit, auth, timeout, or other system
  error — *not* a real finding list / `VERDICT:` line), retry once, then do the equivalent work with
  your own subagents: a fresh, context-isolated subagent review pass in
  Stage 2a. The gate is unchanged — note any fallback pass in the report. See "The reviewer".
- CI status comes from a re-polled `gh pr checks` snapshot with **zero fail AND zero pending lines** —
  never from the `--watch` exit code (it can exit 0 on pending/unregistered checks). No green, no merge.
- The run targets a **base branch** (`base_branch` in the ledger header), which is **not assumed to
  be `main`** — it is the `baseRefName` of the adopted PRs (must agree across them, else prompt).
  Reviews diff `origin/<base>...HEAD` and PRs merge into `<base>`; a fix worktree branches off the PR's OWN
  head branch/SHA, never off `<base>` (see `pr-adoption.md`). Re-read it each wake (see "Base branch").
- After every merge, fast-forward local `<base>` to `origin/<base>` (Stage 3 step 4) so subsequent
  `origin/<base>...HEAD` diffs and rebases branch off the just-merged tip, not a stale base. If the
  fast-forward fails, bail out — never force it.
- No "Test plan" section in PR bodies.
