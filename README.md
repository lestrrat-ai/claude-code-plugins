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

What you need depends on which plugins you install. Only `gauntlet` shells out to external tools.

Required by every plugin:

- **Python 3 (`python3`)** — runs the bundled scripts. Standard library only, nothing to `pip install`.

Required by `gauntlet` as well:

- **git** — the skills use worktrees and branch operations.
- **GitHub CLI (`gh`)** — every GitHub interaction (PRs, reviews, labels, checks) goes through `gh`, so it must be authenticated (`gh auth login`) and the repo needs a GitHub remote.
- **`jq`** — parses `gh` JSON in the Copilot review-item fetcher.
- **`bash`** — runs the bundled shell scripts (standard on macOS/Linux).

Required by `fusion` as well: nothing. Its scripts invoke `python3` and shell out to no external tool, and its bundled database answers queries offline. Rebuilding that database needs network access to the hosts [`plugins/fusion/README.md`](plugins/fusion/README.md) names.

Optional when Claude Code is the orchestrator, and used by `gauntlet` only:

- **Codex CLI (`codex`)** — the default independent reviewer for `gauntlet:campaign` under Claude Code.
  When Codex is installed, campaign reviews with it (`codex exec`) for engine diversity — a different
  engine catches defects a same-model re-roll misses. It launches at native-limitation level; engine
  diversity needs no OS sandbox. Campaign falls back to a fresh native worker under the documented native
  limitations only when Codex is genuinely unusable — it is absent, it is out of quota or rejects your
  credentials, it cannot run at all, or retries of a transient failure ran out — so the campaign runs with
  or without Codex. A reviewer that refuses to review a diff's content parks the PR for you instead, since
  the fallback would hide the refusal. An explicit selection or saved preference overrides the default (you can
  force a native reviewer). Missing native filesystem/startup controls alone never park a pass.

## Plugins

| Plugin | What it is |
|--------|------------|
| [`gauntlet`](plugins/gauntlet/README.md) | Adversarial review that gates PRs to merge: `review` reports findings (report-only by default, can opt in to opening PRs); `campaign` adopts existing PRs and defends each through repeated context-isolated reviews and green CI, then merges. |
| [`fusion`](plugins/fusion/README.md) | Autodesk Fusion 360 Python API lookups: `query-api` answers symbol questions (classes, methods, properties, enum values) from a bundled reference database; `compile-api` rebuilds that database from AutodeskFusion360/FusionAPIReference. |

## License

MIT
