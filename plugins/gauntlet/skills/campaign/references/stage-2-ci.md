### 2b. CI (event-driven)

Each PR has a background task that waits on `gh pr checks --watch`, then **re-polls** `gh pr checks
<pr>` into `ci-<pr>.txt`. The watch only blocks; the re-polled snapshot is the source of truth. When
the task completes, a wake reads the file and decides `ci` **from the file's contents — never from
the watch exit code** — and writes the `ci`/`reviews_ok` result through `scripts/ledger.py … set --pr
<N> --ci <state> [--reviews_ok 0]` **by field name** (`files-and-ledger.md`), never by hand-editing the
row by column position:

- **green** → ONLY if the snapshot shows **zero failing lines AND zero pending lines** and the
  expected checks are actually present. `gh pr checks --watch` can exit 0 while checks are still
  pending or have not yet registered, so a clean exit is not evidence of green.
- **pending** → any line still pending, or the expected checks haven't appeared yet → not green;
  leave `ci = pending` and, if the watch task has exited, **relaunch it in this same wake** — a
  pending PR must never sit unwatched waiting for the heartbeat.
- **red** → any failing line → **stop any review pass in flight on that PR first** (Loop control
  step 3 — the fix will replace its SHA, so the verdict is already void; free the slot), then
  diagnose from the check logs and dispatch a scoped CI-fix subagent into `<worktree>` — the PR row's
  ledger `worktree` column value, the single source of truth for this PR's checkout path (created at
  adoption/pre-review per `pr-adoption.md`; the ledger-recorded `<worktree>` path — default
  `.worktrees/<headRefName>` when campaign creates it, else a reused existing checkout).

  **Classify the failure first** (see "Total-oracle classification" below). Not whitelisted →
  **dispatch on the session model** — set the model explicitly, and do NOT downgrade it (`SKILL.md`,
  "Subagent Dispatch"). Its output is **code that gets merged**, and nothing downstream validates it: a
  wrong fix can turn CI **green** — by weakening the check, or by being plain wrong in product code no
  check covers — and the review gate is a miss-catcher, not a proof of correctness. A green check means
  the check passed, never that the fix is right. NEVER claim CI catches a bad fix.

  **Scope it**: give it the failing check's logs, the specific failing file(s), and the worktree path,
  and tell it NOT to re-derive the whole diff or read beyond what the failure touches.

  **Tell it, verbatim in its prompt**: it MUST fix the *cause* of the failure. It MUST NEVER make CI
  pass by weakening the check — NEVER delete or loosen an assertion, NEVER add `skip`/`xfail`, NEVER
  disable or downgrade a lint rule, NEVER raise a timeout — UNLESS the check itself is demonstrably
  wrong, in which case it MUST say so explicitly and name the change in its output so the review gate
  can judge it.

  Its fix commits + pushes to the PR's **own head branch**
  → code changed → **reset `reviews_ok` to 0 AND, in that same step, restore `gauntlet-reviewing` if
  the PR carries `gauntlet-accepted`** (`gh pr edit <pr> --remove-label gauntlet-accepted --add-label
  gauntlet-reviewing`) — the gate and its label move together, never one without the other
  (`stage-2-review-gate.md`, "Status labels mirror the review gate"). Then relaunch the watch
  immediately and re-enter 2a.

#### Total-oracle classification

Before dispatching anything, decide whether the failing check is a **total oracle** for its fix:
re-running that same check *fully decides* correctness, leaving no judgment over (`SKILL.md`, "The one
exception"). All three MUST hold:

- (a) the failing check fully verifies its own fix on re-run; AND
- (b) the fix is confined to what the check itself defines — **no product behavior changes**; AND
- (c) the resulting diff touches **no check definition, config, or test** (the no-weakening prohibition
  above, now doing double duty as a guard).

Key it on the **check / linter rule IDENTITY** from the failing check's output — `gofmt`, `goimports`,
`golangci-lint` rules (`modernize`, `gci`, `whitespace`, …) — NEVER on a judgment that the failure
"looks mechanical". **Default is NOT whitelisted. Unknown check → session model.** A failing product
test is NEVER whitelisted: making a test pass is not the same as fixing the bug. Compile errors in real
logic are NEVER whitelisted.

Then, in order:

1. **Tier 0 — no subagent (prefer this always).** Whitelisted check with a deterministic autofixer →
   run the **tool** in `<worktree>`: `golangci-lint run --fix`, `gofmt -w`, `goimports -w`,
   `ruff --fix`, `prettier --write`, etc. Re-run the exact failing check. Passes → commit + push, no
   model spend at all. Dispatching a model to hand-edit a formatting violation is both the most
   expensive and the LEAST reliable way to do it.
2. **Tier 1 — cheap model, verified.** Whitelisted check with no autofixer, or an autofixer that ran but
   left residue → dispatch the scoped CI-fix subagent on `haiku` (pure mechanical rewrite) or `sonnet`
   (needs any reasoning). Set the model explicitly. **Verify before accepting**: re-run the exact
   failing check in `<worktree>` AND inspect the diff. ACCEPT only if the check now **passes** AND the
   diff touches **no check definition, config, or test** and changes no product behavior.
3. **Discard and escalate.** Verification fails on any point (check still red, diff touches a check/
   config/test, behavior moved) → **discard the work** (`git checkout -- .` / reset the worktree to the
   PR head) and re-dispatch the same failure on the **session model**. NEVER patch up a Tier-1 fix in
   place; NEVER commit an unverified Tier-1 diff.
4. **Everything else → session model**, per the red-CI dispatch above.

The review gate still reads the whole diff for every commit a tier produces. The whitelist lowers
**cost**, never the gate. An autofixer or `modernize`-class rewrite CAN in rare cases change behavior —
the gate is what catches that; this is not a guarantee of correctness.

Every CI failure must be handled; never merge over a red or pending check, and never infer green from
the watch's exit code alone — always confirm against the re-polled snapshot.

CI fixes serialize only within one PR/SHA. Different PRs with red CI may run scoped CI-fix subagents
concurrently within the dispatcher cap.

---
