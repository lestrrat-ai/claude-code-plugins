# Runtime compatibility

Use this map when authoring shared Gauntlet behavior. Keep host-specific syntax inside an explicit
adapter; keep workflow rules shared.

| Surface | Claude Code | Codex | Shared rule |
|---|---|---|---|
| Repository instructions | `CLAUDE.md` | `AGENTS.md` | `CLAUDE.md` is a relative symlink to `AGENTS.md`; edit `AGENTS.md` only. |
| Plugin manifest | `.claude-plugin/plugin.json` | `.codex-plugin/plugin.json` | Keep `name` and `version` equal. |
| Marketplace | `.claude-plugin/marketplace.json` | `.agents/plugins/marketplace.json` | Keep plugin names and source directories equal. |
| Skill invocation | `/gauntlet:<skill>` | `$gauntlet:<skill>` | Show both forms when exact invocation matters. |
| Installed cache | `~/.claude/plugins/cache/...` | `~/.codex/plugins/cache/...` | Never assume working-tree skill files are active. |
| Skill resources | May expose `CLAUDE_PLUGIN_ROOT` | Active skill path is supplied to the agent | Resolve from the directory containing active `SKILL.md`; pass absolute paths to workers. |
| Agent dispatch | Agent tool and configured agent types | Available Codex multi-agent controls | Describe worker scope, permissions, model class, and output; do not require a host tool name. |
| Model selection | Claude model aliases may be available | Session model or configured Codex agents | State required capability; map named models only inside host adapter. |
| Wake/resume | `ScheduleWakeup` where available | Thread wake where available; bounded foreground wait otherwise | Persist state before waiting and provide an exact resume path. |
| Other-agent reviewer | A user may select `codex exec` | A user may select `claude -p` | This is opt-in. Honor explicit or saved user choice; otherwise choose a fresh host worker. |

Campaign's exact cross-agent command lines live in
[`plugins/gauntlet/skills/campaign/references/cross-agent-reviewers.md`](../plugins/gauntlet/skills/campaign/references/cross-agent-reviewers.md).

## Authoring checks

- Preserve workflow outcomes across hosts. Adapt execution only.
- NEVER claim a fallback preserves context isolation when it runs in the current context.
- NEVER silently skip a required capability. Report the missing capability and use the documented fallback.
- Keep host-specific examples paired unless a section is explicitly host-only.
- Treat cross-agent review as a user option, never an automatic default.
- Run both plugin install validators after changing shared plugin content.
- Bump both plugin manifest versions after changing `plugins/**`.
