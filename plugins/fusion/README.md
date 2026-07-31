# fusion

Look up the Autodesk Fusion 360 Python API (`adsk.core`, `adsk.fusion`, `adsk.cam`, …) without leaving
your session. A SQLite database compiled from the official
[FusionAPIReference](https://github.com/AutodeskFusion360/FusionAPIReference) intellisense stubs ships
with the plugin, so queries work offline out of the box.

## Skills

| Skill | Invocation | What it does |
|-------|------------|--------------|
| `query-api` | `/fusion:query-api` (Codex `$fusion:query-api`) | Answers questions about Fusion API symbols: full class detail, member signatures, inheritance trees, name and docstring search. Member lookups include members declared on base classes, walk them in the order Python itself resolves an attribute, and name the class each one comes from; `members <class> --own` narrows a listing to the class itself. |
| `compile-api` | `/fusion:compile-api` (Codex `$fusion:compile-api`) | Rebuilds the database from the reference repo's current `main` into `~/.cache/fusion-api-db/`, which `query-api` prefers over the bundled copy — upgrades need no plugin reinstall. |

## Examples

- "What members does `adsk.fusion.ExtrudeFeatures` have?"
- "Show me the signature and docs of `ExtrudeFeatures.addSimple`."
- "Which Fusion API docstrings mention sweep profiles?"
- "Recompile the Fusion API database."

## Prerequisites

- **Python 3 (`python3`)** — runs both bundled scripts. Standard library only.
- Network access to `api.github.com` (commit and file listing) and `raw.githubusercontent.com`
  (the stub files themselves), only when recompiling the database. Those two hosts are the whole
  egress: `github.com` appears in a recompiled database as the recorded source, never as a host
  anything is fetched from.
