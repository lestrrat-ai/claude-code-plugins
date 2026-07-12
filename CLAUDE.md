# claude-code-plugins

This repo's contents **are** the agent instructions the agent is running. Editing `plugins/**` edits
the skill that may be driving the current session. Two consequences, both non-obvious:

## Follow the procedure the running skill specifies

When a skill in use names a procedure — "dispatch a scoped fix subagent", "run the review as a
background task", "write the ledger through `scripts/ledger.py`" — **do that**. Do NOT silently
substitute inline work in the main loop for a dispatch the skill specifies.

- The substitution is invisible in the result (the fix still lands) but it means the specified code
  path is **never exercised** → its bugs stay undiscovered, and any claim made about it is untested.
- A deliberate deviation is allowed, but **say so out loud** and give the reason. Silent deviation is
  the failure mode.

## Dogfood the branch's behavior — but NEVER let it gate itself

The harness loads skills from the **installed** plugin cache
(`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`), not from the working tree. So a branch's
changes do **not** take effect just because they are checked out. Split the two roles deliberately:

| Role | Which copy | Why |
|------|-----------|-----|
| **Behavior under development** (a new rule, a new dispatch procedure, a model tier) | the **branch** — read it from the worktree and follow it | It is the thing being tested. Exercising it is the only way to learn whether it works. |
| **The gate** (verdict counting, `required(tier)`, merge preconditions, the review contract) | the **installed** known-good version | The check must be an independent authority. |

**NEVER use an in-development gate to approve the change that alters it.** A bug in the branch would
corrupt the very check meant to catch that bug — a branch that accidentally set `required(tier) = 1`
would, used as its own gate, merge itself on a single verdict. Keep a trusted stage-0.

## Version is the plugin cache key

`plugin.json`'s `version` decides whether an installed copy refreshes. Changing skill content **without**
bumping it means installed copies stay stale at the old content while claiming to be current — this has
already happened once across several releases. Bump the version in the same PR as any `plugins/**`
content change, or in a release PR that batches several.
