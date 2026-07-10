# gauntlet

Adversarial code review that follows through to a merge.

The centerpiece is `/gauntlet:campaign`: point it at your code and it runs an adversarial review, files
each real finding as its own pull request, defends that PR through repeated context-isolated review
rounds until it passes a strict bar with green CI, and merges. Run it once — it schedules its own
follow-ups and keeps working unattended.

If you only want to know what's wrong, `/gauntlet:review` reports findings and changes nothing.

## Install

```
/plugin marketplace add lestrrat-ai/claude-code-plugins
/plugin install gauntlet@lestrrat-ai
```

## Skills

| Skill | What it does |
|-------|--------------|
| `/gauntlet:campaign` | The review-to-merge pipeline. Writes code and merges it. See [its README](skills/campaign/README.md). |
| `/gauntlet:review` | A standalone two-pass hostile review: pass 1 surfaces everything, pass 2 neutrally confirms or refutes each finding. Reports only. |
| `/gauntlet:copilot-address-reviews` | Verify and address GitHub Copilot's PR review comments, one at a time. |
| `/gauntlet:codex-exec` | Delegate a lightweight task to Codex CLI via `codex exec`. |

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
lives in `.gauntlet/tmp/` and is safe to delete; `.gauntlet/history/` holds durable carryover between
campaign runs, so don't remove `.gauntlet/` wholesale.

## License

MIT
