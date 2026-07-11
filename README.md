# claude-code-plugins

Claude Code plugins by [lestrrat](https://github.com/lestrrat-ai), published as a plugin marketplace.

## Install

Add the marketplace once:

```
/plugin marketplace add lestrrat-ai/claude-code-plugins
```

Then install whichever plugins you want:

```
/plugin install gauntlet@lestrrat-ai
```

## Plugins

| Plugin | What it is |
|--------|------------|
| [`gauntlet`](plugins/gauntlet/README.md) | Adversarial review that gates PRs to merge: `review` reports findings (report-only by default, can opt in to opening PRs); `campaign` adopts existing PRs and defends each through repeated context-isolated reviews and green CI, then merges. |

## License

MIT
