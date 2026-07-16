# Runtime compatibility

Use this map when authoring shared Gauntlet behavior. Keep host-specific syntax inside an explicit
adapter; keep workflow rules shared.

| Surface | Claude Code | Codex | Shared rule |
|---|---|---|---|
| Repository instructions | `CLAUDE.md` | `AGENTS.md` | `CLAUDE.md` is a relative symlink to `AGENTS.md`; edit `AGENTS.md` only. |
| Plugin manifest | `.claude-plugin/plugin.json` | `.codex-plugin/plugin.json` | Keep `name` and `version` equal. |
| Marketplace | `.claude-plugin/marketplace.json` | `.agents/plugins/marketplace.json` | Keep the marketplace `name`, the plugin names, and their source directories equal. |
| Skill invocation | `/gauntlet:<skill>` | `$gauntlet:<skill>` | Show both forms when exact invocation matters. |
| Installed cache | `~/.claude/plugins/cache/...` | `~/.codex/plugins/cache/...` | Never assume working-tree skill files are active. |
| Skill resources | May expose `CLAUDE_PLUGIN_ROOT` | Active skill path is supplied to the agent | Resolve from the directory containing active `SKILL.md`; pass absolute paths to workers. |
| Agent dispatch | Agent tool and configured agent types | Available Codex multi-agent controls | Describe worker scope, permissions, model class, and output; do not require a host tool name. |
| Model selection | Claude model aliases may be available | Session model or configured Codex agents | State required capability; map named models only inside host adapter. |
| Wake/resume | `ScheduleWakeup` where available | Thread wake where available; bounded foreground wait otherwise | Persist state before waiting and provide an exact resume path. |
| Other-agent reviewer | Default: review with `codex exec` | Default: review with `claude -p` | Cross-engine is the default, launched at native-limitation level when the paired CLI is present. Fall back to a fresh native worker when it is absent or fails. Explicit or saved user choice overrides. |

Campaign's exact cross-agent command lines live in
[`plugins/gauntlet/skills/campaign/references/cross-agent-reviewers.md`](../plugins/gauntlet/skills/campaign/references/cross-agent-reviewers.md).

## Authoring checks

- Preserve workflow outcomes across hosts. Adapt execution only.
- NEVER claim a fallback preserves context isolation when it runs in the current context.
- Distinguish fresh conversational context from filesystem/security isolation. Native task APIs may
  share cwd, writable files, and repository startup instructions; disclose that limitation and keep the
  installed campaign rules as stage-0 authority. Claim instruction-neutral/read-only isolation only for
  a transport whose host or OS enforces it; `runtime-adapter.md` owns the complete rule.
- Resolve the supplied checkout once per workflow entry/resume through the runtime adapter's typed
  `RepositoryContext`; carry its absolute paths and every dynamic ref/payload through typed
  argv/byte/message fields, never an ambient project variable or shell splice. Its per-attempt record
  also assigns one final-report producer.
- Evaluate cross-engine reviewers through the runtime adapter's `ReviewIsolationCapability` transition.
  A cross-engine route launches at native-limitation level whenever the paired CLI is present; the three
  `os_filesystem_isolation` properties are an optional stronger-boundary claim that never blocks launch.
  When the paired CLI is absent, or the process fails after its retry, fall back to a fresh native worker.
- NEVER silently skip a required capability. Report the missing capability and use the documented fallback.
- Keep host-specific examples paired unless a section is explicitly host-only.
- Cross-engine review is the default per host, launched at native-limitation level; explicit or saved
  user selection overrides it.
- Run both plugin install validators after changing shared plugin content.
- Bump both plugin manifest versions after changing `plugins/**`.
