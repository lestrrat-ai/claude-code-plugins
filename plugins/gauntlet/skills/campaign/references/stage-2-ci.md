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

  **Dispatch it on `sonnet`** (`model: "sonnet"`), not the session model — the one sanctioned
  downgrade in this skill (`SKILL.md`, "Subagent Dispatch"). It is safe because **both** of its
  failure modes are caught — but by two different mechanisms, not by CI alone:
  - *fix didn't work* → CI stays red → campaign re-dispatches, and a red check blocks the review pass
    regardless. CI catches this one.
  - *fix weakened the check* (gutted assertion, `skip`/`xfail`, disabled lint rule, raised timeout) →
    **CI turns green, so CI does NOT catch it.** It is caught by the **review gate**, which reads the
    whole `origin/<base>...HEAD` diff and sees the weakened check as the code change it is.

  Never claim "CI catches everything" — it does not. Both nets must stay in place.

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

Every CI failure must be handled; never merge over a red or pending check, and never infer green from
the watch's exit code alone — always confirm against the re-polled snapshot.

CI fixes serialize only within one PR/SHA. Different PRs with red CI may run scoped CI-fix subagents
concurrently within the dispatcher cap.

---
