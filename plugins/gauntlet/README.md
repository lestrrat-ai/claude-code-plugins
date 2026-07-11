# gauntlet

Part of the [claude-code-plugins](../../README.md) marketplace.

Adversarial code review that follows through to a merge.

The centerpiece is [`/gauntlet:campaign`](skills/campaign/README.md): hand it existing pull requests
(`/gauntlet:campaign #12 #15`) and it gates each one to merge — defending that PR through repeated
context-isolated review rounds until it passes a strict bar with green CI, fixing up whatever review
or CI turns up on the PR itself, and then merging. It doesn't hunt for problems or write fixes from
scratch; it drives PRs that already exist. Run it once — it schedules its own follow-ups and keeps
working unattended.

Where do those PRs come from? [`/gauntlet:review`](skills/review/SKILL.md) is the front half. By
default it runs a two-pass adversarial review and only reports — it makes no source/tracked-file or
GitHub changes (it may write ephemeral `.gauntlet/tmp` review scratch). But at the end
it can, opt-in, open one PR per confirmed fix and hand them straight to a campaign. So the
usual progression is **`gauntlet:review` to find and confirm the problems, then `gauntlet:campaign`
to gate and merge the fixes** — and you can always skip review and hand campaign PRs you opened
yourself.

## Install

```
/plugin marketplace add lestrrat-ai/claude-code-plugins
/plugin install gauntlet@lestrrat-ai
```

## Skills

| Skill | What it does |
|-------|--------------|
| [`/gauntlet:campaign`](skills/campaign/README.md) | The PR-gating pipeline. Adopts existing pull requests and drives each through review + CI to merge. |
| [`/gauntlet:review`](skills/review/SKILL.md) | A standalone two-pass hostile review: pass 1 surfaces everything, pass 2 neutrally confirms or refutes each finding. Reports only by default; can opt-in to open PRs and hand them to a campaign. |
| [`/gauntlet:copilot-address-reviews`](skills/copilot-address-reviews/SKILL.md) | Verify and address GitHub Copilot's PR review comments, one at a time. |
| [`/gauntlet:codex-exec`](skills/codex-exec/SKILL.md) | Delegate a lightweight task to Codex CLI via `codex exec`. |

## Requirements

- A GitHub remote — the pipeline works through PRs via the `gh` CLI.

That's it. By default the adversarial reviewer is Claude's own subagents, so nothing else is needed.

### Recommended — a second-opinion reviewer

The gate's strength comes from re-reviewing each change with a *fresh, independent* reviewer. Two
Claude subagents share the orchestrator's model, so for a tougher gauntlet point the pipeline at a
reviewer that runs a **different agent/model** — e.g. [Codex CLI](https://github.com/openai/codex)
(`codex exec`). A different engine catches defects a same-model re-roll can miss.

To use one, either name it when you invoke the campaign ("review with codex") or record it as your
preferred reviewer (in memory or `CLAUDE.md`) and the pipeline will pick it up. If an external
reviewer can't return a verdict because of a system problem (quota, auth, timeout), the pipeline
retries once and then falls back to its own subagents, so an outage slows a run rather than stalling
it.

## Scratch files

Both skills keep working state under `.gauntlet/` at the repo root, which is git-ignored. Run scratch
lives in `.gauntlet/tmp/` and is mostly safe to delete — a just-finished run's dir is kept so campaign
can still offer to gate more PRs, but deleting it only loses that prompt, not your history.
`.gauntlet/history/` holds the durable carryover between campaign runs, so don't remove `.gauntlet/`
wholesale.

## License

MIT
