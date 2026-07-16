# gauntlet

Part of the [claude-code-plugins](../../README.md) marketplace for Claude Code and Codex.

Adversarial code review that follows through to a merge.

The centerpiece is [`gauntlet:campaign`](skills/campaign/README.md): hand it existing pull requests
(`/gauntlet:campaign #12 #15` in Claude Code or `$gauntlet:campaign #12 #15` in Codex) and it gates each one to merge — defending that PR through repeated
context-isolated review rounds until it passes a strict bar with green CI, fixing up whatever review
or CI turns up on the PR itself, and then merging. It doesn't hunt for problems or write fixes from
scratch; it drives PRs that already exist. Run it once — it uses scheduled wakes where available and
bounded waits otherwise, then keeps working unattended.

Where do those PRs come from? [`gauntlet:review`](skills/review/README.md) is the front half. By
default it runs a two-pass adversarial review and only reports — it makes no source/tracked-file or
GitHub changes (it may write ephemeral `.gauntlet/tmp` review scratch). But at the end
it can, opt-in, open one PR per confirmed fix and hand them straight to a campaign. So the
usual progression is **`gauntlet:review` to find and confirm the problems, then `gauntlet:campaign`
to gate and merge the fixes** — and you can always skip review and hand campaign PRs you opened
yourself.

## Install

### Claude Code

```
/plugin marketplace add lestrrat-ai/claude-code-plugins
/plugin install gauntlet@lestrrat-ai
```

### Codex

```
codex plugin marketplace add lestrrat-ai/claude-code-plugins
codex plugin add gauntlet@lestrrat-ai
```

Start a new Codex session after installation.

## Skills

| Skill | What it does |
|-------|--------------|
| [`gauntlet:campaign`](skills/campaign/README.md) | The PR-gating pipeline. Adopts existing pull requests and drives each through review + CI to merge. |
| [`gauntlet:review`](skills/review/README.md) | A standalone two-pass hostile review: pass 1 surfaces everything, pass 2 neutrally confirms or refutes each finding. Reports only by default; can opt-in to open PRs and hand them to a campaign. |
| [`gauntlet:copilot-address-reviews`](skills/copilot-address-reviews/README.md) | Verify and address GitHub Copilot's PR review comments, one at a time. |

## Requirements

- A GitHub remote — the pipeline works through PRs via the `gh` CLI.

That's it. By default the adversarial reviewer is a fresh native worker, so nothing else is needed.
Fresh means a separate conversational context. Native task APIs may still share the repository cwd and
writable filesystem and inherit repository startup instructions; campaign does not call that an OS or
security boundary. The installed campaign rules remain the stage-0 acceptance authority, and candidate
copies of gate or instruction files remain review evidence.

### Optional — use the other agent as reviewer

The gate's strength comes from re-reviewing each change with a *fresh, independent* reviewer. Two
native workers share the orchestrator's model. If you want engine diversity, Claude Code can launch
Codex with `codex exec`, and Codex can launch Claude Code with `claude -p`.

This is a user option, not a campaign rule. Name the reviewer when you invoke the campaign, or record it
as your preference in memory, `AGENTS.md`, or `CLAUDE.md`. The campaign never launches the other agent merely
because its CLI is installed. If an external
reviewer can't return a verdict because of a system problem (quota, auth, timeout), the pipeline
retries once and then falls back to a fresh native worker. An external transport may claim an
instruction-neutral cwd and read-only candidate checkout only when the host or OS enforces both; an
unavailable external boundary falls back rather than silently weakening that claim. The native fallback
keeps fresh conversational context and discloses the host's filesystem/startup-instruction limitations.

### Optional — tell it how to report to you

The plugin deliberately does **not** set how the host talks to you. That is your environment's business
(`AGENTS.md`, `CLAUDE.md`, or an output style), not a plugin's, and a skill that dictated tone would just fight whatever
you had already configured.

It's still worth setting something. A campaign reports on every wake, for hours, and those updates are all
you see of it. Without a contract, the update that needs your decision reads much like the twenty that
don't.

If you don't already have a style you like, [`docs/reporting-style.md`](docs/reporting-style.md) is a
sample to copy into your `AGENTS.md` or `CLAUDE.md` and edit.

## Scratch files

Both skills keep working state under `.gauntlet/` at the repo root, which is git-ignored. Run scratch
lives in `.gauntlet/tmp/` and is mostly safe to delete — a just-finished run's dir is kept so campaign
can still offer to gate more PRs, but deleting it only loses that prompt, not your history.
Everything else under `.gauntlet/` is durable, so don't remove `.gauntlet/` wholesale: `history/` holds
the carryover between campaign runs, and `followups.jsonl` is campaign's local ledger of work it found
but deliberately did not do — candidates it will never publish without your say-so.

## License

MIT
