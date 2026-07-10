## Stage 0 — Review and verify (pipelined, non-blocking)

Stage 0 is a **pipeline of background tasks**, not a serial phase the driver waits out. Sweep shards,
verification chunks, and fan-out overlap: each completion is a wake, and a confirmed survivor starts
its fix while later shards are still sweeping. The dispatcher (Loop control step 3) owns every launch
below — never barrier on the full sweep or full verification before starting downstream work.

1. **Launch the adversarial sweep as background task(s), using the selected reviewer** (see
   "The reviewer"; default = Claude subagents following `gauntlet:review`, else the user's preferred
   reviewer). Scope = the arg if given, else the whole repo. Each shard writes the standard finding
   shape — stable ID, severity, file:line, defect, reproduction trigger, impact, concrete fix — to
   `<rundir>/findings-raw-<shard>.md`.

   - **Default (Claude subagents)** → dispatch the sweep as background subagents per `gauntlet:review`.
   - **External reviewer (e.g. Codex)** → invoke it in background, output to the shard file, e.g.:

     ```
     # run in background — the driver never blocks on a sweep
     codex exec --sandbox workspace-write -c "sandbox_workspace_write.network_access=true" -o <rundir>/findings-raw-<shard>.md \
       "Perform an adversarial code review of <SHARD SCOPE>. For each finding give: a stable ID, \
        severity, file:line, the defect, a concrete reproduction trigger, the impact, and a \
        concrete fix. Be hostile — surface everything that could be wrong. Do not edit code."
     ```

   - **Small/scoped surface** → a single shard.
   - **Whole-repo or large surface** → split into shards by area, mirroring the tiering strategy in
     the `gauntlet:review` skill, and launch them concurrently within the dispatcher cap. Shards
     also keep each reviewer call short relative to the ~30-min lease-stale window (see "Run lease");
     the driver heartbeats on every wake between completions.

   Backgrounding, not sharding, is what keeps the driver free: even a single-shard sweep runs in
   background while the driver heartbeats, dispatches anything else due, reschedules, and folds the
   shard's findings when its completion wakes it.

   If a shard can't produce findings (an *external* reviewer hitting quota/rate-limit, auth, timeout,
   hang, or other system error — see "The reviewer"), retry that shard once, then run it with your own
   subagents into the same `findings-raw-<shard>.md` and continue.

   **On a fresh run, load carryover first** (all of `.gauntlet/history/`, pruned of stale
   entries per "Pruning the ledger" — pruning never blocks the sweep launch) and pass the prior
   unresolved items (aborted / declined-api / uncertain) to the reviewer as known areas of interest,
   so a re-find is recognized rather than treated as net-new. See "Fresh runs and carryover".

2. **Verify per shard, as findings land.** The moment a shard's `findings-raw-<shard>.md` is folded
   in, dispatch its neutral verification — do NOT wait for other shards. Audit each finding with the
   `gauntlet:review` Phase 2 scheme — `CONFIRMED` / `ADJUSTED` / `REFUTED` / `UNCERTAIN`, biased
   toward refuting — into `<rundir>/verdicts-<chunk>.md`. Scale by the shard's finding count:
   - **≤ 10 findings** → one fresh `Explore` subagent audits the shard's whole set.
   - **> 10 findings** → chunks of 5–8, one `Explore` subagent per chunk in parallel, each auditing
     only its assigned IDs.

   Only **CONFIRMED** and **ADJUSTED** survive as work items. Drop REFUTED; list UNCERTAIN in the
   final report for the user to triage. On a fresh run, hand every verifier the carryover sets
   (refuted to suppress, merged to dedup, unresolved to prioritize) per "Fresh runs and carryover".

3. **Incremental reconcile — dedup on arrival, no barrier.** Chunked verifiers are blind across
   chunks and shards, so the orchestrator keeps a run-wide **survivor set**. As each verification
   chunk lands, check its confirmed findings against that set AND against findings already fanned
   out: collapse same-underlying-bug duplicates into the existing work item; a causally-linked
   finding shares the existing PR or is sequenced behind it — never a parallel PR colliding on the
   same code. Checking each arrival against the accumulated set gives the same pairwise coverage as
   a batch dedup, so nothing is gained by waiting for all chunks. This is set-level dedup, NOT
   re-verification — the chunk already judged each finding.

4. **Append survivors and fan out immediately.** Each deduped survivor becomes a `pending` row in
   `state.md` (the run-config header — `run_id`, `base_branch`, `api_changes`, `reviewer` — was written
   at run start; Loop control step 1 / "Starting a fresh run"), eligible for a Stage 1 fix slot **on the
   same wake**. Flip `phase` from `reviewing` to `fanout` when the first row lands; sweep shards and
   verification chunks may still be running during fanout — `phase` is advisory, liveness comes from
   rows + PRs (Loop control).

If ALL shards and verification chunks complete with zero survivors, **release the run before
stopping** — write its (empty) carryover file `.gauntlet/history/<run-id>.md`, delete
`<rundir>/lease.json` and the `gauntlet-run-<run-id>` label *if it exists* (Stage 1 hasn't run yet on
a zero-survivor exit, so the owner label is usually not created — skip it then), mark `state.md`
terminal — then report and stop (no loop). Do not leave the lease dangling.

---
