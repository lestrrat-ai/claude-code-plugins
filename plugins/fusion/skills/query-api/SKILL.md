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
   | Provenance and counts for the database in use | `info` |
   | Find names matching a term | `search <term>` |
   | Full detail for a class, function, or one member | `show <name>` |
   | All members of a class | `members <class>` (`--own` drops inherited ones) |
   | Base chain + direct subclasses | `tree <class>` |
   | Which symbols' docs mention a term | `doc-search <term>` |

   `show` accepts qualnames (`adsk.fusion.ExtrudeFeatures`), bare class names, `Class.member`, and bare
   member names; ambiguous input returns the candidate list — pick one and re-run.

   Member lookups include inherited members by default, and every one is reported against the class
   that declares it: `show Class.member` resolves a member declared on any base, `show Class` lists
   inherited members after the class's own, and `members <class>` groups them by declaring class.
   The bases are walked in the order Python itself resolves an attribute, so the class reported is
   the class the member really comes from. `members` and `tree` take a class; given anything else
   they report no such class.
3. Database resolution order (explicit `--db`, then user cache, then the bundled copy) is owned by the
   script's module docstring; pass `--db` only when the user names a database, and pass it **before**
   the subcommand — after it, it is a usage error and exits 2:

   ```text
   ["python3", "<absolute-query_fusion_api.py>", "--db", "<path>", "<subcommand>", "<argument>"]
   ```

   Database missing at every location → tell user to run `/fusion:compile-api`
   (Codex `$fusion:compile-api`).
4. Answer from script output only. Exit code 1 with a candidate list or "no matches" on stdout is a
   lookup miss, not a failure — refine the name and re-run, or report the miss. Exit code 1 with an
   `error:` line on stderr is a real failure — relay it and stop. The causes are: no database, the
   file is not one, and a class whose recorded bases have no consistent resolution order, which is
   refused rather than answered from a guessed order. The last one names the class; report it as a
   defect in the database, and recompile with `/fusion:compile-api` (Codex `$fusion:compile-api`).

Read-only. The bundled database ships with the plugin; NEVER edit or regenerate it here — regeneration
is `compile-api`'s job.
