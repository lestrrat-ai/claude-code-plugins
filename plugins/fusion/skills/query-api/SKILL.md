---
name: query-api
description: Look up Autodesk Fusion 360 Python API symbols — classes, methods, properties, functions, enum values — from a pre-compiled reference database. Use when the user asks what a Fusion API class/method/property does, what members or signature a Fusion type has, or where a Fusion API name is defined.
---

# Query Fusion API

Invocation: Claude Code `/fusion:query-api`; Codex `$fusion:query-api`.

1. Resolve `scripts/query_fusion_api.py` from this active `SKILL.md` to an absolute path. NEVER use a
   plugin-root environment variable.
2. Run one subcommand per question:

   ```text
   ["python3", "<absolute-query_fusion_api.py>", "<subcommand>", "<argument>"]
   ```

   | Question | Subcommand |
   |----------|------------|
   | Provenance / source commit of the data | `info` |
   | Find names matching a term | `search <term>` |
   | Full detail for a class, function, or one member | `show <name>` |
   | All members of a class | `members <class>` (`--inherited` walks bases) |
   | Base chain + direct subclasses | `tree <class>` |
   | Which symbols' docs mention a term | `doc-search <term>` |

   `show` accepts qualnames (`adsk.fusion.ExtrudeFeatures`), bare class names, `Class.member`, and bare
   member names; ambiguous input returns the candidate list — pick one and re-run.
3. Database resolution order (explicit `--db`, then user cache, then the bundled copy) is owned by the
   script's module docstring; pass `--db` only when the user names a database. Database missing at every
   location → tell user to run `/fusion:compile-api` (Codex `$fusion:compile-api`).
4. Answer from script output only. Exit code 1 with a candidate list or "no matches" is a lookup miss,
   not a failure — refine the name and re-run, or report the miss.

Read-only. The bundled database ships with the plugin; NEVER edit or regenerate it here — regeneration
is `compile-api`'s job.
