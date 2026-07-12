# claude-code-plugins

This repo **authors** the agent instructions: `plugins/**` defines the skills that may be driving the
current session. The skills actually running in a session are the **installed** copies, not the working
tree. The consequences below are all non-obvious:

## Follow the procedure the running skill specifies

When a skill in use names a procedure — "dispatch a scoped fix subagent", "run the review as a
background task", "write the ledger through `scripts/ledger.py`" — **do that**. Do NOT silently
substitute inline work in the main loop for a dispatch the skill specifies.

- The substitution is invisible in the result (the fix still lands) but it means the specified code
  path is **never exercised** → its bugs stay undiscovered, and any claim made about it is untested.
- Deviation is permitted **only** when the specified procedure is one of: **impossible** in the current
  environment (the tool or harness cannot do it); **unsafe** (it would cause damage or violate a hard
  rule); or in **direct conflict** with a higher-priority instruction from the user. Nothing else
  qualifies — "it is faster", "it is simpler", "I can just do it inline" are **NOT** valid grounds and
  MUST NEVER be used to skip a specified procedure.
- When deviating you MUST say so out loud and state (a) which of the three grounds applies, and (b) that
  the specified path was therefore **not exercised**. The coverage gap has to be visible, not silently
  absorbed. Silent deviation is the failure mode.

## Dogfood the branch's behavior — but NEVER let it gate itself

The harness loads skills from the **installed** plugin cache
(`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`), not from the working tree. So a branch's
changes do **not** take effect just because they are checked out. Split the two roles deliberately:

| Role | Which copy | Why |
|------|-----------|-----|
| **Non-gate behavior under development** — it touches nothing that decides whether a PR may merge (e.g. a new worktree/commit convention, a fix subagent's dispatch procedure or model choice, report formatting) | the **branch** — read it from the worktree and follow it | It is the thing being tested. Exercising it is the only way to learn whether it works. |
| **The gate** — *every surface that decides whether a PR may merge*, i.e. the acceptance machinery as a whole | the **installed** known-good version | The check must be an independent authority. |

The gate is defined by that property, **not** by a list of files or steps. Non-exhaustive examples:
risk-tier triage and `required(tier)`; review dispatch and reviewer selection (which reviewer, which
model, how it is isolated); the review contract and verdict counting; reviewer progress and emit
accounting; CI status derivation and watch handling; gate resets and the status labels; API approval;
merge preconditions and merge execution.

The first row **NEVER** overrides the second. If a branch change affects acceptance in **any** way it is
gate machinery and the **installed** copy wins — being "the behavior under development" is not an
exemption, and a change can be both.

**When it is unclear whether something is gate machinery, it IS gate machinery — use the installed
copy.** The ambiguous case MUST resolve toward the installed copy, never toward the branch: guessing
wrong in the other direction lets a branch approve itself.

**NEVER use an in-development gate to approve the change that alters it.** A bug in the branch would
corrupt the very check meant to catch that bug — a branch that accidentally set `required(tier) = 1`
would, used as its own gate, merge itself on a single verdict; a branch that weakened CI status
derivation would feed itself a false `ci=green`. Keep a trusted stage-0.

## Version is the plugin cache key

`plugin.json`'s `version` decides whether an installed copy refreshes. Changing skill content **without**
bumping it means installed copies stay stale at the old content while claiming to be current — this has
already happened once across several releases. Bump the version in the same PR as any `plugins/**`
content change, or in a release PR that batches several.
