---
name: compile-api
description: Compile or upgrade the Autodesk Fusion 360 Python API reference database from the AutodeskFusion360/FusionAPIReference GitHub repository. Use when the user asks to build, refresh, upgrade, or recompile the Fusion API database, or when query-api reports the database is missing.
---

# Compile Fusion API Database

Invocation: Claude Code `/fusion:compile-api`; Codex `$fusion:compile-api`.

1. Resolve `scripts/compile_fusion_api.py` from this active `SKILL.md` to an absolute path. NEVER use a
   plugin-root environment variable.
2. Run:

   ```text
   ["python3", "<absolute-compile_fusion_api.py>"]
   ```

   Needs network access to github.com. The default output is the user cache
   (`~/.cache/fusion-api-db/fusion-api.db`), which `query-api` prefers over the database bundled with
   the plugin — a recompile takes effect immediately, no plugin reinstall.
3. Options, only when the user asks for them:
   - `--ref <git-ref>` compiles a specific ref of the reference repo (default `main`).
   - `--source <dir>` parses a local directory of `adsk/*.py` stubs instead of downloading.
   - `--output <path>` writes elsewhere. Refreshing the plugin's bundled copy (authoring checkout of
     this plugin only) → `--output <plugin>/skills/query-api/data/fusion-api.db`, then commit the result.
4. Report the script's final line (path, symbol count, member count) and the `compiling <repo>@<sha>`
   line as the new source commit. Non-zero exit → relay stderr as-is and stop.

The script deletes and rebuilds its output database from scratch; there is no incremental mode, and a
rebuild is always safe.
