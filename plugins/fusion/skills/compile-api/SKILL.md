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

   Needs network access to `api.github.com` (commit and file listing) and `raw.githubusercontent.com`
   (the stub files themselves), and to no other host: `github.com` is only the source string recorded
   in the database. The default output is the user cache
   (`~/.cache/fusion-api-db/fusion-api.db`), which `query-api` prefers over the database bundled with
   the plugin — a recompile takes effect immediately, no plugin reinstall.
3. Options, only when the user asks for them:
   - `--ref <git-ref>` compiles a specific ref of the reference repo (default `main`).
   - `--source <dir>` parses a local directory of `adsk/*.py` stubs instead of downloading. It
     resolves no ref, so the two options are refused together (usage error, exit 2) rather than
     `--ref` being ignored; a user asking for both wants a download of that ref.
   - `--output <path>` writes elsewhere. Refreshing the plugin's bundled copy (authoring checkout of
     this plugin only) → `--output <plugin>/skills/query-api/data/fusion-api.db`, then commit the result.
   - `--self-test` compiles a built-in stub, asserts the stub-only member drop below, and exits
     without touching any database. Use it to verify the script after changing it; it takes no
     network and ignores `--output`.
4. Report the script's final line on stdout (path, symbol count, member count) and the
   `compiling <source>@<commit>` line stderr opens with, as the source record. Every path emits that
   line: a download names the reference repo, the resolved commit, and the ref that was requested
   (`compiling <repo>@<sha> (requested ref '<ref>')`), a `--source` compile names the local directory
   and `local` in place of a commit. Report the requested ref as well as the commit — the resolved
   commit alone cannot be checked against the ref the user named. Relay every `warning:` line on
   stderr too: one kind reports how many statements used a construct the compiler does not index,
   one kind reports a warning Python raised while parsing a named stub, and one kind reports a
   `STUB_ONLY_MEMBERS` entry (below) that matched nothing. The last means the compiler is still
   suppressing a member the stubs no longer declare — report it and say the entry needs review, but
   the database it wrote is usable. Non-zero exit → relay stderr as-is and stop.

The database tracks the SHIPPED RUNTIME, not the stubs, wherever the two disagree. The stubs are
auto-generated documentation and declare a few members the module Fusion imports does not define, so
the compiler drops those rather than indexing them — otherwise the database answers "yes, that
exists" about a call that raises `AttributeError`. `STUB_ONLY_MEMBERS` in the script names every
dropped member and owns the reasoning; `info` in `query-api` reports what a given database dropped
(`stub_only_members_dropped`). Adding an entry needs a diff against the runtime modules of the SAME
Fusion build (`API/Python/packages/adsk/*.py` in the install), never a different build: the reference
repo tracks a newer API than an installed Fusion, and that skew is a version gap, not a defect.

The script rebuilds the whole database from scratch; there is no incremental mode. It compiles into a
staging file next to the output and replaces the output only after every stub has parsed and the result
has been checked to be non-empty, so a compile that fails partway leaves the existing database in place.
A compile that succeeds still discards the previous contents — anything hand-edited into that database is
lost.
