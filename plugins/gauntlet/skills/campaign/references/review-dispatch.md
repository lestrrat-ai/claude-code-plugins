## Review dispatch — prepare once, then launch

This file owns review-attempt preparation and launch handoff. Stage 2 owns review policy and artifact
acceptance. `runtime-adapter.md` owns route selection, isolation claims, and typed host operations.

### Prepare the active attempt

**Run `review-dispatch.py prepare` after `review_transition` selects a launch action and before any reviewer
starts.** Resolve `review_dispatch_script` from the directory containing the active campaign `SKILL.md`.
Pass every dynamic value as one argv member:

```text
result = run_argv(
  argv: ["python3", review_dispatch_script, "prepare",
         "--run-dir", review_root,
         "--pr", pr, "--pass", review_pass, "--launch-attempt", launch_attempt,
         "--worktree", worktree, "--base", base,
         "--route", route, "--report-producer", report_producer,
         "--head-sha", head_sha, "--dispatched-at", dispatched_at,
         "--intent-file", intent_file],
  cwd: repository.project_root,
  stdin_file: null,
  stdout_file: null
)
prepared = JSON_DECODE(result.stdout)
transport = prepared.transport
```

Inputs have these owners:

- `route` comes from `runtime-adapter.md`'s `review_transition`; `prepare` never selects or probes it.
- `report_producer` comes from `runtime-adapter.md`, "Review transport record and report ownership";
  `prepare` refuses a route/producer mismatch.
- `review_root`, `worktree`, and `base` come from the invocation's typed `RepositoryContext` and ledger.
- `pr`, `review_pass`, `launch_attempt`, `head_sha`, and `dispatched_at` name this launch attempt.
- `intent_file` is the absolute derived `<rundir>/intent-<pr>.md` path. The command refuses another path.

The command validates the existing per-pass plan and per-PR intent through `review-pass.py`, derives the
prompt/progress/findings/report paths from one attempt identity, resolves all three emitters from its
installed script directory, writes the exact bound prompt, and writes the validated `pass_identity` as
the progress file's first line. It refuses any existing prompt, progress, findings, or report artifact for
that attempt. A non-zero exit prepares nothing usable; do not launch.

`review-<pr>-<n>.plan.jsonl` remains per-pass and `intent-<pr>.md` remains per-PR. Every other path in
`transport` is per-attempt: attempt 1 uses `review-<pr>-<n>.*`; attempt `k >= 2` uses
`review-<pr>-<n>.a<k>.*`. The command derives the complete set once, so a relaunch cannot mix a dead
attempt's progress/findings/report paths with the active prompt.

### Prompt bytes have one owner

**Use only the prompt written at `transport.prompt_path`.** The exact reviewer contract lives in the
bundled `scripts/review-prompt.txt`; `review-dispatch.py` is its only binder. It JSON-encodes
`ReviewTransport`, inserts the intent bytes verbatim, validates the template's closed slot set before
binding, and never rescans inserted bytes. Do not copy the template into prose, build it with a heredoc,
or substitute record fields into shell source.

The prompt tells every route to review the whole `origin/<base>...HEAD` diff against the intent and plan,
record progress/findings/amendments only through the bundled tools, perform the adversarial sweep, obey
the finding-anchor rule, and return the exact residual-risk/verdict ending. These are prompt contents,
not a second dispatch procedure; edit and test the bundled template when that contract changes.

### Launch the prepared attempt

**Launch only the route named by `prepared.route`, using the returned `transport` without reconstructing
paths or prompt bytes.** Route selection and availability were decided before preparation:

- `native` → pass the complete bytes at `transport.prompt_path` through `dispatch_native` in a fresh
  `session`-class worker. The prompt assigns `native-worker-write` as sole report producer.
- `external-codex` / `external-claude` → use the canonical `run_argv` block in
  `cross-agent-reviewers.md`, "Claude Code orchestrator → Codex reviewer" or "Codex orchestrator →
  Claude Code reviewer". The process transport assigns `external-process-capture` as sole report
  producer.

Never embed the prompt in an argument or shell source. External prompt stdin is the prepared prompt file,
which supplies immediate EOF. Launch in the background so completion triggers reconcile.

Never pass destructive instructions to an external reviewer. Keep Codex on `--sandbox workspace-write`;
never use `--dangerously-bypass-approvals-and-sandbox`. At native-limitation level, `transport.review_root`
is the plain run-artifact root and makes no isolation claim. `--ignore-rules`, a cwd, and prompt
prohibitions do not create an OS boundary; only the capability owner may claim one.
