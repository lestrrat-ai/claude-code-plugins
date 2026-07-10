### 2b. CI (event-driven)

Each PR has a background task that waits on `gh pr checks --watch`, then **re-polls** `gh pr checks
<pr>` into `ci-<pr>.txt`. The watch only blocks; the re-polled snapshot is the source of truth. When
the task completes, a wake reads the file and decides `ci` **from the file's contents — never from
the watch exit code**:

- **green** → ONLY if the snapshot shows **zero failing lines AND zero pending lines** and the
  expected checks are actually present. `gh pr checks --watch` can exit 0 while checks are still
  pending or have not yet registered, so a clean exit is not evidence of green.
- **pending** → any line still pending, or the expected checks haven't appeared yet → not green;
  leave `ci = pending` and, if the watch task has exited, **relaunch it in this same wake** — a
  pending PR must never sit unwatched waiting for the heartbeat.
- **red** → any failing line → **stop any review pass in flight on that PR first** (Loop control
  step 3 — the fix will replace its SHA, so the verdict is already void; free the slot), then
  diagnose from the check logs and dispatch a scoped CI-fix subagent into that worktree. Its fix
  commits + pushes → code changed → **reset `reviews_ok` to 0**, relaunch the watch immediately,
  re-enter 2a.

Every CI failure must be handled; never merge over a red or pending check, and never infer green from
the watch's exit code alone — always confirm against the re-polled snapshot.

CI fixes serialize only within one PR/SHA. Different PRs with red CI may run scoped CI-fix subagents
concurrently within the dispatcher cap.

---
