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

- **Codex CLI (`codex`)** — the default independent reviewer for `gauntlet:campaign` under Claude Code.
  When Codex is installed, campaign reviews with it (`codex exec`) for engine diversity — a different
  engine catches defects a same-model re-roll misses. It launches at native-limitation level; engine
  diversity needs no OS sandbox. When Codex is absent, or a cross-engine process fails after one retry,
  campaign falls back to a fresh native worker under the documented native limitations, so the campaign
  runs with or without Codex. An explicit selection or saved preference overrides the default (you can
  force a native reviewer). Missing native filesystem/startup controls alone never park a pass.

## Plugins

| Plugin | What it is |
|--------|------------|
| [`gauntlet`](plugins/gauntlet/README.md) | Adversarial review that gates PRs to merge: `review` reports findings (report-only by default, can opt in to opening PRs); `campaign` adopts existing PRs and defends each through repeated context-isolated reviews and green CI, then merges. |

## License

MIT
