# Cross-agent reviewer commands

Using the other agent is a **user option, never a campaign rule**. Use this file only when the user
selected that reviewer explicitly or saved it as their preference. The presence of either CLI does not
select it automatically. This file defines transport, not review policy. In both directions,
substitute the complete prompt from `stage-2-review-gate.md`, preserve every attempt-scoped artifact
path, and launch the process as a background task whose completion triggers a reconcile.

The commands assume a same-repository PR, as required by `pr-adoption.md`. Never add a permission-bypass
flag to make a failed launch work.

## Claude Code orchestrator → Codex reviewer

Use the external-reviewer command in `stage-2-review-gate.md`:

```sh
codex exec --sandbox workspace-write -c "sandbox_workspace_write.network_access=true" -C <worktree> \
  --add-dir $PROJECT/<rundir> \
  -o $PROJECT/<rundir>/<review-output> \
  "<complete substituted review prompt>" < /dev/null
```

Required transport properties:

- `-C <worktree>` makes the PR checkout the working root.
- `--add-dir` permits the reviewer to write only its run artifacts outside that root.
- `-o` writes the final report to the active attempt's output file.
- `< /dev/null` prevents a non-interactive `codex exec` from waiting on inherited stdin.
- `--sandbox workspace-write` is mandatory. Never use
  `--dangerously-bypass-approvals-and-sandbox`.

## Codex orchestrator → Claude Code reviewer

Start the process with its **working directory set to `<worktree>`** through the host's process API,
then run:

```sh
claude -p --no-session-persistence --output-format text \
  --permission-mode dontAsk \
  --tools "Read,Bash" --allowedTools "Read,Bash" \
  --add-dir $PROJECT/<rundir> \
  "<complete substituted review prompt>" \
  < /dev/null > $PROJECT/<rundir>/<review-output>
```

Required transport properties:

- `-p` is Claude Code's non-interactive mode, and `--no-session-persistence` makes each pass fresh.
- Set the process working directory externally; Claude Code has no `-C` equivalent.
- Limit built-in tools to `Read` and `Bash`. The review prompt forbids source changes; Bash is needed
  for git inspection and the two artifact emitters.
- `--permission-mode dontAsk` makes an unapproved operation fail instead of opening an interactive
  prompt. A permission or sandbox denial is a reviewer system failure; retry or fall back under
  `reviewer.md`. Never switch to `--dangerously-skip-permissions`.
- Redirect stdout to the active attempt's output file and stdin from `/dev/null`.

The user's Claude Code settings still control sandboxing and policy. Do not widen them from campaign.
If the command cannot run the required read-only review and artifact writes under those settings, use
the normal retry and native-worker fallback.

## Diversity rule

When the user selects one of these directions, report its diversity accurately:

- Claude Code → Codex uses a different engine and provides reviewer diversity.
- Codex → Claude Code uses a different engine and provides reviewer diversity.
- Codex → another `codex exec`, or Claude Code → another `claude -p`, provides fresh context only.
  It is valid when explicitly selected, but it must not be reported as engine diversity.

Record `codex`, `claude`, or the exact configured reviewer in the ledger header. The final report names
the reviewer and any pass that fell back to the active host's native worker.
