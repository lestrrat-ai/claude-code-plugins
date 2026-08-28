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
         "--allocation-purpose", allocation_purpose,
         "--worktree", worktree, "--base", base, "--file", ledger,
         "--review-action", review_action,
         "--route", route, "--prompt-profile", prompt_profile,
         "--report-producer", report_producer,
         "--head-sha", head_sha, "--dispatched-at", dispatched_at,
         "--default-non-goals", ledger_default_non_goals,
         "--intent-file", intent_file],
  cwd: repository.project_root,
  stdin_file: null,
  stdout_file: null
)
prepared = JSON_DECODE(result.stdout)
transport = prepared.transport
```

`prepared` carries `route`, `transport`, and — on the `external-codex` route only — `reviewer_home`. The
home is a SIBLING of `transport`, never a member of it, because only `transport` is bound into the prompt
and the launch environment must stay out of the bytes the reviewer reads. See **The reviewer's codex
home** below.

Inputs have these owners:

- `review_action`, `route`, `prompt_profile`, and `report_producer` come from `runtime-adapter.md`,
  **Review preparation mapping**. `prepare` never selects, probes, or changes them. It refuses an unknown
  profile and every action/route/profile combination outside that mapping before writing launch artifacts.
- `launch_attempt` and `allocation_purpose` come only from `runtime-adapter.md`, **Review allocation
  journal**, read through `review-dispatch.py allocation-status` after the prior attempt settles. The
  journal supplies the next monotonic attempt and due allocation; **Review preparation mapping** selects
  neither.
- `review_root`, `worktree`, and `base` come from the invocation's typed `RepositoryContext` and ledger.
  `base` is **this PR row's effective base** — its explicit `base_branch`, else the legacy header fallback
  (`ledger.py`'s `effective_base`), never the one header base. It rides the typed transport as data (the
  reviewer diffs `origin/<base>...HEAD`); `--file <review_root>/state.jsonl` makes `--base` an **assertion**
  that must equal the selected `--pr` row's effective base, and `prepare` refuses a disagreement — the row is
  the source of truth, the flag is not. `review_root` and `worktree` must be different directories with
  neither nested inside the other; the command refuses an identical or either-way-nested pair before staging
  any artifact, so preparation never writes launch files into the candidate worktree. This is a fail-closed
  input check, not an OS boundary.
- `pr`, `review_pass`, `head_sha`, and `dispatched_at` name this launch attempt. With `--file`,
  `head_sha` is an assertion against the selected row's live `head_sha`; a mismatch refuses before
  preparation writes an allocation or reviewer artifact. Refresh the PR state and preconditions, then
  retry with the live head. The ledger row is the source of truth; `--head-sha` is not a review-head source.
- `ledger_default_non_goals` is the run header's `default_non_goals` value (its canonical JSON array, `[]`
  when the run declares none), read from `<review_root>/state.jsonl`. `prepare` BINDS it into the
  `pass_identity` as the immutable dispatch-time review scope this pass's verdict is measured against — the
  tally (`verify --ledger`) compares this binding to the header's live defaults, never the mutable intent
  block (`stage-2-review-gate.md`, "Does this pass COUNT?"). A malformed value is a controlled refusal.
- `intent_file` is the absolute derived `<rundir>/intent-<pr>.md` path. The command refuses another path.

The command validates the existing per-pass plan and per-PR intent through `review-pass.py`. It then runs
`stage-2-review-gate.md`, **"Review-history integrity guard"**, before it creates any launch artifact and
derives the prompt/progress/findings/report paths from one attempt identity, resolves every bundled emitter the
transport names from its installed script directory, writes the exact bound prompt, and writes the
validated `pass_identity` as the progress file's first line. The emitter set is the code's, not this
document's: `review-dispatch.py` owns which `emit_*_path` fields the transport carries, and it refuses to
prepare an attempt when any one of them is missing on disk.
Every transport text value must encode as UTF-8; a path containing other filesystem bytes is a
controlled refusal before either launch artifact exists.

**Recover any inert residue of a preparation that never launched a reviewer.** A reviewer starts only
after `prepare` returns, so until then no findings or report exist and the progress file holds at most this
attempt's single `pass_identity` line. Residue that carries only this invocation's own inert bytes — a
prompt whose bytes exactly match, and a progress file that is exactly this attempt's lone, **well-formed**
`pass_identity` line (validated through `review-pass.py`'s progress-file schema — the same read door the
tool itself passes an identity through before it writes one), in whichever combination the interruption
left (prompt alone, identity alone, or both) — is removed so the pair can be recreated. A findings file, a
report, or any further progress line is real reviewer evidence; a lone identity that fails that schema
(bad `head_sha`, missing `dispatched_at`, a duplicate key) is not this tool's own residue but a foreign
writer's — refuse either, along with any prompt or progress file that does not match this attempt, and
never delete it. A non-zero exit prepares nothing usable; do not launch.

`review-<pr>-<n>.plan.jsonl` remains per-pass and `intent-<pr>.md` remains per-PR. Every other path in
`transport` is per-attempt: attempt 1 uses `review-<pr>-<n>.*`; attempt `k >= 2` uses
`review-<pr>-<n>.a<k>.*`. The command derives the complete set once, so a relaunch cannot mix a dead
attempt's progress/findings/report paths with the active prompt.

### The reviewer's codex home

**This section owns `reviewer_home`; `cross-agent-reviewers.md` owns how the argv passes it.** On the
`external-codex` route, and only there, `prepare` materializes `<rundir>/codex-home` and returns its
absolute path as `prepared.reviewer_home`. Launch with `CODEX_HOME` set to it; every other route omits the
key entirely, and its presence is the signal.

The directory is a farm of symlinks back into the operator's real codex home (`$CODEX_HOME`, else
`~/.codex`) with two deliberate holes. `review-dispatch.py` owns the exact contents — the omitted
instruction filenames and the emptied catalog directories are named by its own constants, not restated
here — and the two properties that hold are these. The operator's standing instruction files are absent,
so codex cannot load them into a reviewer. The skill and plugin catalogs are empty real directories, so
the reviewer is offered no catalog and cannot invoke the campaign skill that dispatched it. Everything
else, auth and `config.toml` included, is symlinked, so no credential is copied into the run directory and
the reviewer runs under the operator's configured model and account.

It is built **once per run and reused**, staged and renamed so a concurrently preparing attempt either
wins the rename or finds the finished farm. A machine with no codex home is a refusal before any attempt
artifact or allocation is written, because the host adapter only selects this route when the paired CLI is
present, so a missing home means the launch could not have worked.

**NEVER site a run directory under the system temp dir.** Codex refuses to create its helper binaries
beneath one and runs degraded, printing `Refusing to create helper binaries under temporary dir`. A
campaign run directory is a `<project>/.gauntlet/tmp/<run-id>` repository path and does not trip that
check; the rule is written down so a later "simplification" to a `mktemp -d` home cannot silently degrade
every reviewer launch.

### Record allocation outcomes

**Settle the prepared launch through `review-dispatch.py result` before preparing another.** Pass the same
`--run-dir`, `--pr`, `--pass`, and `--launch-attempt`, plus the result the heartbeat observed:

```text
python3 <skill-dir>/scripts/review-dispatch.py result \
  --run-dir <review_root> --pr <pr> --pass <review_pass> --launch-attempt <launch_attempt> \
  --result provider-failure|transport-failure|malformed-output|incomplete-plan|amended|reviewed|head-invalidated|scope-invalidated
```

`runtime-adapter.md`, **Review allocation journal**, owns which allocation is due and which outcomes leave
the final review reserved. The journal is driver state, not reviewer evidence: do not add allocation lines
to a progress or report artifact. `result --result reviewed` derives that allocated attempt's artifacts and
requires its allocated head and same-run `<rundir>/state.jsonl` scope check to pass, like
`review-pass.py verify --ledger`. It refuses a missing, deferred, malformed, stale-head, stale-scope, or
otherwise unusable report. Settle a stale head as `head-invalidated` and a stale scope as
`scope-invalidated`; both require the matching live-ledger difference and preserve the final reserve.
`allocation-status` renders its durable history after a dead attempt settles, for a held PR, or for a final
report.

For a journal-less run created before allocation journaling, `result` first validates the named active
attempt against its derived plan and records its `legacy` allocation from immutable identity data. It then
records the requested outcome. `runtime-adapter.md`, **Review allocation journal**, owns the migration
conditions and the due-allocation rule; `legacy` is not accepted by `prepare`.

### Prompt bytes have one owner

**Use only the prompt written at `transport.prompt_path`.** The exact reviewer contract lives in the
bundled `scripts/review-prompt.txt`; `review-dispatch.py` is its only binder. It JSON-encodes
`ReviewTransport`, inserts the intent bytes verbatim, validates the template's closed slot set before
binding, and never rescans inserted bytes. Do not copy the template into prose, build it with a heredoc,
or substitute record fields into shell source.

**Keep one review contract/template for both prompt profiles.** `standard` binds the template directly.
`codex-recovery` adds the binder-owned repository-maintenance preamble before the same complete template;
it is valid only for `retry-external` on `external-codex`; every other `ReviewAction` uses `standard`.
The preamble states the concrete local goal through the bound Intent and asks for proof from the local diff,
repository tests, and fixtures. It never changes,
shortens, or duplicates the template, and it never asks the reviewer to contact a third-party system.

The shared prompt tells every route to review the whole `origin/<base>...HEAD` diff against the intent and plan,
record progress/findings/amendments only through the bundled tools, perform the adversarial sweep, obey
the finding-anchor rule, and deliver the verdict and its optional residual-risk records by running the
bundled report tool. These are prompt contents,
not a second dispatch procedure; edit and test the bundled template when that contract changes.

### Launch the prepared attempt

**Launch only the route named by `prepared.route`, using the returned `transport` without reconstructing
paths or prompt bytes.** Route selection and availability were decided before preparation:

- `native` → pass the complete bytes at `transport.prompt_path` through `dispatch_native` in a fresh
  `session`-class worker.
- `external-codex` / `external-claude` → use the canonical `run_argv` block in
  `cross-agent-reviewers.md`, "Claude Code orchestrator → Codex reviewer" or "Codex orchestrator →
  Claude Code reviewer". The codex block also consumes `prepared.reviewer_home` (**The reviewer's codex
  home**, above).

Every route assigns the same sole report producer, `reviewer-tool-write`: the reviewer writes its report
by running `transport.emit_report_path`, and neither transport captures a final-output channel at
`transport.report.path` (`runtime-adapter.md`, "Review transport record and report ownership").

Never embed the prompt in an argument or shell source. External prompt stdin is the prepared prompt file,
which supplies immediate EOF. Launch under runtime-adapter.md, **Direct asynchronous process launch**;
completion triggers reconcile.

Never pass destructive instructions to an external reviewer. Keep Codex on `--sandbox workspace-write`;
never use `--dangerously-bypass-approvals-and-sandbox`. At native-limitation level, `transport.review_root`
is the plain run-artifact root and makes no isolation claim. `--ignore-rules`, a cwd, and prompt
prohibitions do not create an OS boundary; only the capability owner may claim one.
