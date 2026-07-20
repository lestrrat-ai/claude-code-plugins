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
  argv: ["codex", "exec", "--sandbox", "workspace-write", "-c",
         "sandbox_workspace_write.network_access=true", "--skip-git-repo-check",
         "-C", transport.review_root, "-o", transport.report.path, "-"],
  cwd: transport.review_root,
  stdin_file: transport.prompt_path,
  stdout_file: null
)
```

This argv launches at the native-limitation level using the plain run-artifact working root; it makes no
isolation claim and does not create a stronger boundary:

- `-C`, followed by `transport.review_root` as its own argv element, selects the run-artifact working
  root;
  `--skip-git-repo-check` is required because that root is deliberately not the candidate repository.
- **`-C` MUST be `transport.review_root` because that is the reviewer's only WRITABLE root.** Every
  artifact the reviewer writes — progress, findings, amendments, the report — lives under `review_root`,
  and `--sandbox workspace-write` makes only the `-C` root (and its `writable_roots`) writable. A `-C`
  pointed anywhere else (for example at the candidate worktree) leaves the run directory READ-ONLY, so
  every `emit` fails with a read-only-filesystem error and the reviewer defers with a read-only progress
  file. That symptom is a DISPATCH fault, not a reviewer fault: relaunching the same argv fails
  identically. Do not widen the sandbox to "fix" it (a `writable_roots` entry for the worktree would make
  candidate content writable); point `-C` at `review_root`.
- `transport.worktree` is named only inside the bound prompt and is read through absolute paths (for
  example, the typed Git argv in the review prompt). Do not pass it through `-C` or `--add-dir`: either makes candidate
  content part of the writable workspace, and `-C` also enables candidate `AGENTS.md` discovery.
- `-o` names `transport.report.path` as the external process's sole report producer.
- `stdin_file: transport.prompt_path` passes prompt bytes as data and supplies EOF; inherited
  interactive stdin is never left open.
- `--sandbox workspace-write` is mandatory. Never use
  `--dangerously-bypass-approvals-and-sandbox`.
- `--ignore-rules` is irrelevant here: it suppresses execpolicy `.rules`, not project agent
  instructions, and MUST NOT be used as the isolation control.

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
  stdout_file: transport.report.path
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
  for git inspection and the two artifact emitters.
- `--permission-mode dontAsk` makes an unapproved operation fail instead of opening an interactive
  prompt. A permission or sandbox denial is a reviewer system failure; retry or fall back under
  `reviewer.md`. Never switch to `--dangerously-skip-permissions`.
- Set `stdin_file` to `transport.prompt_path` and `stdout_file` to `transport.report.path`; the external
  process capture is the sole report producer. Prompt and path values remain data.

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
the reviewer and any pass that fell back to the active host's native worker.
