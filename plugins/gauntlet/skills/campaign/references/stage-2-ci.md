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

  **Classify the failure first** (see "Whitelist classification" below). Not whitelisted →
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

#### Whitelist classification — run the formatter TOOL, or the session model

Before dispatching anything, decide whether the failing check is **whitelisted**. A check is whitelisted
**only because its fixer is semantics-preserving BY CONSTRUCTION**: it rewrites LAYOUT and cannot change
program meaning at all (`SKILL.md`, "The only cheap path"). That guarantee belongs to **the tool's
output** and to nothing else — a model hand-editing the same file does NOT inherit it, however
formatting-like its diff looks. A pure-indentation edit can move behavior in a whitespace-significant
language and still be formatter-clean, so there is no diff-shape guard that makes a cheap model's edit
safe to accept. **NO SUBAGENT IS EVER RUN ON A DOWNGRADED MODEL.**

- **IN** — pure formatting / import ordering: `gofmt`, `gofumpt`, `goimports` (import block only), `gci`,
  `whitespace`, `prettier`, `ruff format`.
- **OUT** — every **semantic rewriter**, including `modernize` and any rule that rewrites logic. A
  `modernize` rewrite can PASS its own rule while CHANGING BEHAVIOR (e.g. `sort.Slice` → `slices.SortFunc`
  with a reversed or non-equivalent comparator): lint-clean, semantics changed.
- **OUT** — blanket `golangci-lint run --fix` and blanket `ruff --fix`: they apply semantic autofixes
  too. A whitelisted run MUST invoke **only the formatter**, NEVER a catch-all `--fix`.
- **NEVER whitelisted**: a failing product test (making a test pass is not the same as fixing the bug), a
  compile error, and any rule that rewrites logic.

Key it on the **check / fixer IDENTITY** from the failing check's output — NEVER on a judgment that the
failure "looks mechanical". **Default is NOT whitelisted. Unknown check → session model.**

Then, in order:

1. **Whitelisted formatter → run the TOOL, no model (prefer this always).** In `<worktree>`: `gofmt -w`,
   `gofumpt -w`, `goimports -w`, `gci write`, `prettier --write`, `ruff format`. NEVER a catch-all
   `--fix`. ACCEPT only if **both** hold: re-running the **exact** failing check now **passes**, AND the
   diff touches **no check definition, config, or test**. Then commit + push — **zero model spend**.
2. **Everything else → the scoped CI-fix subagent on the session model**, set explicitly, per the red-CI
   dispatch above. Covers: the tool did not fix it, the tool left residue, the check is not whitelisted,
   or the failure needs any judgment.

If the tool's run fails either acceptance point → **discard the work** (reset the worktree to the PR
head) and **re-dispatch the same failure on the session model**. NEVER patch a formatter run in place;
NEVER commit an unverified one; NEVER hand the failure to a cheap model instead.

**Residual risk, stated honestly:** it is small and specific — reordering imports could in principle
change init side-effect order (blank/`_` imports). That is the whole of it. Do NOT widen the whitelist
past it, and NEVER re-derive its safety from the review gate: the whitelist stands on the fixer being
incapable of changing semantics, or it does not stand at all.

Every CI failure must be handled; never merge over a red or pending check, and never infer green from
the watch's exit code alone — always confirm against the re-polled snapshot.

CI fixes serialize only within one PR/SHA. Different PRs with red CI may run scoped CI-fix subagents
concurrently within the dispatcher cap.

---
