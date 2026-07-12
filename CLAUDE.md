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

The harness loads skill content under `plugins/**` (SKILL.md, `references/`, `scripts/`) from the
**installed** plugin cache (`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`), not from the
working tree. So a branch's changes to *those* do **not** take effect just because they are checked out.

**This file is the exception.** Root `CLAUDE.md` is **worktree-loaded**: it is read from the checkout
into every session in this repo. An edit to it is **live in the very session that makes it** — and in
the session that reviews it. Therefore a change to `CLAUDE.md` is **PR content, never gate authority**.
It MUST NOT be treated as the rule governing its own review; it is judged by the **installed** gate and
by the user, exactly like any other PR content. **NEVER put a gate-DECIDING rule into `CLAUDE.md` and
then rely on it while gating the PR that introduces it** — that is a branch acting as its own gate
authority, the exact hazard this section forbids.

Split the two roles deliberately:

| Role | Which copy | Why |
|------|-----------|-----|
| **Non-gate behavior under development** — it touches nothing that decides whether a PR may merge (e.g. a new worktree/commit convention, a fix subagent's dispatch procedure or model choice, report formatting) | the **branch** — read it from the worktree and follow it | It is the thing being tested. Exercising it is the only way to learn whether it works. |
| **The gate** — *every surface that decides whether a PR may merge*, i.e. the acceptance machinery as a whole | the **installed** known-good version | The check must be an independent authority. |

The gate is the machinery that **decides** acceptance: whatever renders the verdict, counts it, or
enforces it. It is defined by that property, **not** by a list of files or steps. Non-exhaustive
examples: the acceptance decision and the gate contract (risk-tier triage, `required(tier)`, the review
contract); verdict and progress accounting (emit/tally, gate resets); CI status derivation, watch
handling and the status labels; reviewer selection and isolation (which reviewer, which model, how it is
context-isolated); API approval; merge preconditions and merge execution.

Everything else only **feeds** the gate, and stays **branch**-owned and dogfoodable: fix generation (a
fix subagent's dispatch, scoping, model choice), report formatting, worktree and commit conventions. A
better or worse fix obviously changes whether the PR eventually passes — that does **not** make it gate
machinery. Producing something the gate judges is **NOT** the same as being the gate.

The first row **NEVER** overrides the second. If a branch change **decides** acceptance it is gate
machinery and the **installed** copy wins — being "the behavior under development" is not an exemption,
and a change can be both.

**When it is unclear whether something decides acceptance, treat it as gate machinery — use the
installed copy.** The ambiguous case MUST resolve toward the installed copy, never toward the branch:
guessing wrong in the other direction lets a branch approve itself. This fail-safe covers **decides**;
it MUST NEVER be stretched to "anything that influences the result", which would swallow the first row.

**NEVER use an in-development gate to approve the change that alters it.** A bug in the branch would
corrupt the very check meant to catch that bug — a branch that accidentally set `required(tier) = 1`
would, used as its own gate, merge itself on a single verdict; a branch that weakened CI status
derivation would feed itself a false `ci=green`. Keep a trusted stage-0.

## Version is the plugin cache key

`plugin.json`'s `version` decides whether an installed copy refreshes. Changing skill content **without**
bumping it means installed copies stay stale at the old content while claiming to be current — this has
already happened once across several releases. Bump the version in the same PR as any `plugins/**`
content change, or in a release PR that batches several.
