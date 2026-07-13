## The fix-subagent contract — SCOPE the reading, SWEEP the writing

**This file is the DEFINITION, and it is COMPLETE.** Every fix subagent campaign dispatches — the cheap
CI-fix, the session-model CI-fix, and the review-fix — is dispatched under it. `stage-2-ci.md` and
`stage-2-review-gate.md` name the inputs their path hands over and the model it runs on; **neither
restates the rules below, and neither may contradict them.** If you are dispatching a fixer, read this.

The contract has two halves that pull in opposite directions **on purpose**. Ship both, or the fixer
optimizes the one you gave it:

- **SCOPE** bounds what it READS, so it does not re-derive a problem you already solved for it.
- **SWEEP** bounds what it WRITES, so it does not leave the fix half-applied.

**Read narrowly to UNDERSTAND; grep widely to FINISH.** They are not in conflict: the scope rule is
about **not re-deriving the problem**, the sweep rule is about **not shipping half a fix**. Neither is
licence to ignore the other. A fixer that greps the tree to find the sites its own change invalidated is
**obeying** the scope rule, not breaking it — it is not re-deriving anything, it is finishing.

### SCOPE — bounds the reading

**Scope every fix subagent.** Hand it the worktree path and the **concrete issue list** (for a CI-fix:
the failing check's logs and the specific failing file(s)), and tell it **NOT** to re-derive the whole
diff or read the repo beyond what the named issues touch. An unscoped fixer re-reads everything it was
already told, and that is where the cost is — **not** in the model tier.

**Scope by defect, not by guess: name every file the defect actually touches**, or the fixer will
faithfully leave the sites you forgot to list.

### SWEEP — bounds the writing

**Scoping the fix is NOT licence to fix only the INSTANCE.** Every fix subagent — CI or review — gets
the block below in its prompt **verbatim**, because a scoped fixer is exactly the thing that will patch
the one line it was pointed at and leave the class intact:

> **When your fix changes a DEFINITION (a rule, a command, a schema, a format) or a FACT (a count, a
> name, an API behavior), you are NOT done until every place that RESTATES it is also correct.** `grep`
> for the old value, the old spelling, the old command, the old number — across the whole tree, not just
> the file you were sent to. Restatements hide in **summaries**, **quick-reference bullets**,
> **cross-references**, **table rows**, **worked examples**, and **other copies of the same command**. A
> summary that has drifted from its definition is **worse than no summary** — it is the version people
> actually read. **Report every site you found and its disposition, including the ones you deliberately
> left alone and why.** If your fix is genuinely local and restates nothing, say so explicitly.
>
> This widening is **bounded by the change you are making**: grep for what your own fix invalidated. It
> is **not** permission to re-derive the diff, re-review the PR, or fix defects nobody asked you to fix.

This is a **report** requirement, not just a search requirement: a sweep nobody can audit did not happen.

**Prefer ONE OWNER over another copy.** When the fixer finds a definition restated in N places, the best
repair is usually to leave the definition in one place and make the others point at it — a fifth copy of
a rule is a fifth thing that can go stale. A pointer is only valid if what it points at is **complete**;
pointing at a partial definition is itself the defect.
