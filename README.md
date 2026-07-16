# claude-code-plugins

Claude Code and Codex plugins by [lestrrat](https://github.com/lestrrat-ai), published as a plugin marketplace.

## Install

### Claude Code

Add the marketplace once:

```
/plugin marketplace add lestrrat-ai/claude-code-plugins
```

Then install whichever plugins you want:

```
/plugin install gauntlet@lestrrat-ai
```

### Codex

Add the marketplace once:

```
codex plugin marketplace add lestrrat-ai/claude-code-plugins
```

Then install whichever plugins you want:

```
codex plugin add gauntlet@lestrrat-ai
```

Start a new Codex session after installation so its bundled skills are loaded.

## Prerequisites

The plugins shell out to a few external tools. Have these available before installing.

Required:

- **git** — the skills use worktrees and branch operations.
- **GitHub CLI (`gh`)** — every GitHub interaction (PRs, reviews, labels, checks) goes through `gh`, so it must be authenticated (`gh auth login`) and the repo needs a GitHub remote.
- **Python 3 (`python3`)** — runs the bundled scripts (campaign ledger, progress emitter, review-item deduper). Standard library only, nothing to `pip install`.
- **`jq`** — parses `gh` JSON in the Copilot review-item fetcher.
- **`bash`** — runs the bundled shell scripts (standard on macOS/Linux).

Optional when Claude Code is the orchestrator:

- **Codex CLI (`codex`)** — an optional independent external reviewer for `gauntlet:campaign`. The
  default is a fresh native worker whether or not Codex is installed; Codex is used only when you select
  it for a run or have saved that reviewer preference. A different engine can catch defects a same-model
  re-roll misses, so that opt-in diversity is recommended. If a selected Codex reviewer cannot return a
  verdict, campaign retries it once and then uses a fresh native worker fallback when the host can
  isolate that worker from candidate instructions; otherwise it parks with a machine blocker.

## Plugins

| Plugin | What it is |
|--------|------------|
| [`gauntlet`](plugins/gauntlet/README.md) | Adversarial review that gates PRs to merge: `review` reports findings (report-only by default, can opt in to opening PRs); `campaign` adopts existing PRs and defends each through repeated context-isolated reviews and green CI, then merges. |

## License

MIT
