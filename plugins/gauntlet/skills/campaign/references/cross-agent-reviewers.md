# Cross-agent reviewer commands

The cross-engine reviewer is **the default per host, overridable**: Claude Code reviews with Codex, Codex
reviews with Claude Code, launched at native-limitation level whenever the paired CLI is present. An
explicit user selection or saved preference overrides the default (a native worker, or a specific engine).
This file defines the argv for that route, not review policy or the isolation rule.
Before preparing a record or prompt, evaluate `runtime-adapter.md`'s `ReviewIsolationCapability` and take
its transition. Only `launch-external` or `retry-external` uses the commands below; every other action
stays with the owner.
A capable adapter runs `review-dispatch.py prepare` through the exact invocation in `review-dispatch.md`,
then launches the process from its returned transport as a background task whose completion triggers a
reconcile. Prompt bytes — including verbatim GitHub-derived intent — and dynamic paths never enter shell
source.

`runtime-adapter.md`, **Review preparation mapping**, owns retry profile selection. A retry uses the same
canonical argv below in a fresh process; never resume a failed external session or select a profile by
matching provider output. The shipped argv has no model-selection member. `codex-recovery` changes only
the prepared prompt's opening framing and never the process command.

The commands assume a same-repository PR, as required by `pr-adoption.md`. Never add a permission-bypass
flag to make a failed launch work.

At the default native-limitation level, `transport.review_root` is the plain absolute run-artifact
directory (the same one native uses) and makes no isolation claim; the argv below launch there whenever
the paired CLI is present. `review_root` becomes an alias inside a proved view **only** for a future
adapter that returns the three `os_filesystem_isolation` properties true;
this record field does not materialize or test the view. If the paired CLI is absent,
follow the owned transition instead of constructing this record. Candidate `AGENTS.md`/`CLAUDE.md` files
remain diff content, never gate authority.

## Claude Code orchestrator → Codex reviewer (capability-gated)

The external-reviewer argv below is the canonical spelling; `review-dispatch.md` points here:

```text
run_argv(
  argv: ["env", concat("CODEX_HOME=", reviewer_home),
         "codex", "exec", "--sandbox", "workspace-write", "-c",
         "sandbox_workspace_write.network_access=true", "--skip-git-repo-check",
         "-C", transport.review_root, "-"],
  cwd: transport.review_root,
  stdin_file: transport.prompt_path,
  stdout_file: null
)
```

`reviewer_home` is the `prepare` result's own member, a sibling of `transport` and never a member of it
(`review-dispatch.md`, "Prepare the active attempt"). Pass it verbatim; never recompute the path.

This argv launches at the native-limitation level using the plain run-artifact working root; it makes no
isolation claim and does not create a stronger boundary:

- `-C`, followed by `transport.review_root` as its own argv element, selects the run-artifact working
  root;
  `--skip-git-repo-check` is required because that root is deliberately not the candidate repository.
- **`-C` MUST be `transport.review_root` because that is the reviewer's only WRITABLE root.** Every
  artifact the reviewer writes — progress, findings, amendments, and now the report itself — lives under
  `review_root`,
  and `--sandbox workspace-write` makes only the `-C` root (and its `writable_roots`) writable. A `-C`
  pointed anywhere else (for example at the candidate worktree) leaves the run directory READ-ONLY, so
  every `emit` fails with a read-only-filesystem error — **and the REPORT cannot land either**, because it
  is written through a door under that same root. So the reviewer cannot defer: a deferral is a report,
  and there is no report to write it in. It reports the write door's diagnostic as its FINAL OUTPUT and
  stops; the attempt has no report and is therefore `unusable`. That symptom is a DISPATCH fault, not a
  reviewer fault: relaunching the same argv fails identically, so the DRIVER corrects the launch before
  relaunching. It says nothing about the engine, so it is a `transient` failure and never grounds for the
  native fallback (`reviewer.md`, "External failure classes"). Do not widen the sandbox to "fix" it (a `writable_roots` entry for the worktree would make
  candidate content writable); point `-C` at `review_root`.
- `transport.worktree` is named only inside the bound prompt and is read through absolute paths (for
  example, the typed Git argv in the review prompt). Do not pass it through `-C` or `--add-dir`: either makes candidate
  content part of the writable workspace, and `-C` also enables candidate `AGENTS.md` discovery.
- **There is NO `-o`, and its absence is the producer swap.** The reviewer writes its report by running
  `transport.emit_report_path`, which is the sole producer on every route (`runtime-adapter.md`, "Review
  transport record and report ownership"). `-o transport.report.path` would be a SECOND writer on that
  artifact — and `codex exec` writes the last agent message to that file AFTER the run, so it would not
  race the record, it would replace it. Never add `-o` back, and never point it at the report path under
  another name. The process's final message goes to captured stdout and is diagnostic only.
- `stdin_file: transport.prompt_path` passes prompt bytes as data and supplies EOF; inherited
  interactive stdin is never left open.
- `--sandbox workspace-write` is mandatory. Never use
  `--dangerously-bypass-approvals-and-sandbox`.
- **`CODEX_HOME=reviewer_home` is the ONE control over the operator's ambient instructions, and it is not
  optional.** See "The reviewer's codex home" below.
- `--ignore-rules` is irrelevant here: it suppresses execpolicy `.rules`, not agent instructions of any
  kind, and MUST NOT be used as the isolation control.

### The reviewer's codex home

**Launch every `external-codex` reviewer with `CODEX_HOME` set to the prepared `reviewer_home`.** Codex
loads `$CODEX_HOME/AGENTS.md` into every `codex exec`, a reviewer included. That file is the OPERATOR's
own standing instructions, written for their interactive work, and it can end a reviewer's turn before it
reviews anything. `review-dispatch.py prepare` therefore farms a codex home that simply lacks it, and
owns the directory's whole definition — this file only says to use it.

Two things it does NOT do, both of which must stay disclosed:

- It creates **no OS boundary**. It removes instruction files and catalogs; it is not a sandbox, and it
  does not change the native-limitation level this section already claims.
- It leaves auth, `config.toml`, sessions, and history **shared with the operator's real codex home**, by
  symlink. A reviewer writes the same session and history stores an interactive codex writes.

**Codex ships no flag that does this.** Do not "simplify" the launch to one. `--ignore-user-config` drops
`config.toml` and leaves the instruction file loaded, and `project_doc_max_bytes` governs the CANDIDATE
tree, not the home. Both were measured against a live reviewer, and both left the operator's instructions
in context.

The paired route already had this control and this one did not, which is how the gap survived: Claude
Code's `--safe-mode` (see the Codex → Claude Code argv below) suppresses its own equivalent discovery.

## Codex orchestrator → Claude Code reviewer (capability-gated)

Only after the adapter returns an available capability, start the process with its working directory set
to `transport.review_root` through the host's process API and run:

```text
run_argv(
  argv: ["claude", "-p", "--safe-mode", "--no-session-persistence",
         "--output-format", "text", "--permission-mode", "dontAsk",
         "--tools", "Read,Bash", "--allowedTools", "Read,Bash",
         "--add-dir", transport.worktree],
  cwd: transport.review_root,
  stdin_file: transport.prompt_path,
  stdout_file: null
)
```

This argv launches at the native-limitation level; it does not create a stronger boundary:

- `-p` is Claude Code's non-interactive mode, `--no-session-persistence` makes each pass fresh, and
  `--safe-mode` disables `CLAUDE.md` auto-discovery and other candidate-provided customizations.
- Set `cwd` to `transport.review_root`; Claude Code has no `-C` equivalent.
- `--add-dir`, followed by `transport.worktree` as its own argv element, supplies the candidate
  explicitly. At native-limitation level this shares the worktree on the same writable filesystem — the
  same disclosed limitation the native worker carries — so the prompt's do-not-modify rule is behavioral,
  not an OS read-only boundary; `--permission-mode dontAsk` and a prompt prohibition do not create that
  boundary. A future adapter that proves `os_filesystem_isolation` exposes the directory read-only instead.
- Limit built-in tools to `Read` and `Bash`. The review prompt forbids source changes; Bash is needed
  for git inspection and the bundled artifact tools — including `emit-report.py`, which is how this route
  delivers its report.
- `--permission-mode dontAsk` makes an unapproved operation fail instead of opening an interactive
  prompt. A permission or sandbox denial is a reviewer system failure; classify it and take the transition
  it selects under `reviewer.md`, "External failure classes". Never switch to
  `--dangerously-skip-permissions`.
- Set `stdin_file` to `transport.prompt_path` and leave `stdout_file` null. The reviewer writes its
  report by running `transport.emit_report_path`, which is the sole producer on every route
  (`runtime-adapter.md`, "Review transport record and report ownership"); capturing stdout at
  `transport.report.path` would be a SECOND writer on that artifact. Captured stdout is diagnostic only.
  Prompt and path values remain data.

The user's Claude Code settings still control sandboxing and policy. Do not widen them from campaign.
Take every unavailable/failure transition through `runtime-adapter.md`'s capability owner; do not
restate its fallback/park conditions here.

## Diversity rule

For each direction, report its diversity accurately:

- Claude Code → Codex (the default under Claude Code) uses a different engine and provides reviewer diversity.
- Codex → Claude Code (the default under Codex) uses a different engine and provides reviewer diversity.
- Codex → another `codex exec`, or Claude Code → another `claude -p`, provides fresh context only.
  It is valid when explicitly selected, but it must not be reported as engine diversity.

Record `codex`, `claude`, or the exact configured reviewer in the ledger header. The final report names
reviewer routing and retry-profile use through `bailout-and-final-report.md`, "Final report".
