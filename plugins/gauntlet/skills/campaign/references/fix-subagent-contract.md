## The fix-subagent contract — SCOPE the reading, SWEEP the writing

**This file is the DEFINITION, and it is COMPLETE.** Every fix subagent campaign dispatches — the cheap
CI-fix, the session-model CI-fix, and the review-fix — is dispatched under it. If you are dispatching a
fixer, read this.

**Other files carry NON-AUTHORITATIVE summaries of the rules below**, so each dispatch site reads on its
own. Every one of them is a **pointer with a reminder attached, never a substitute**: **a summary may
never be relied on, extended, or treated as complete, and if any of them disagrees with this file, THIS
FILE WINS.** Never dispatch a fixer from a summary; never reconstruct the contract from one. Correcting
this file means sweeping those sites too — a summary that has drifted from its definition is worse than
no summary.

**When you correct this contract, SWEEP for the restatements — do NOT expect a list of them here.** There
is deliberately none, and that is not an omission: **a list of restatement sites is ITSELF a restatement,
and it rots exactly like every other one.** This file once enumerated four sites; six existed the day it
was written — **the list went stale inside the commit that created it**, and a reader trusting it would
have swept four and never looked for the rest. That is the whole reason you must search instead. Search
for the *shape* of the rules — a recipe does not go stale, a list of files does:

- the **scope** rule — `SCOPE`, "scope … fix subagent", "re-derive", "whole diff", "beyond the named"
- the **sweep** rule — `SWEEP`, "RESTATES", "sweep-and-report", "stale restatement", "fix only the instance"
- **pointers at this file** — `fix-subagent-contract`

Then read each hit and decide: it is a summary of this contract, or it is not. Do not stop when the count
matches something you were told. Whether you CHANGE a rule here or ADD one, **enumerate SEMANTICALLY first
and then search for the identifiers that enumeration names**, exactly as the SWEEP block below requires:
searching for the rule's own wording finds only the sites you already fixed, and searching for an old value
misses every site that paraphrased it.

The contract has two halves that pull in opposite directions **on purpose**. Ship both, or the fixer
optimizes the one you gave it:

- **SCOPE** bounds what it READS, so it does not re-derive a problem you already solved for it.
- **SWEEP** bounds what it WRITES, so it does not leave the fix half-applied.

**Read narrowly to UNDERSTAND; sweep widely to FINISH.** They are not in conflict: the scope rule is
about **not re-deriving the problem**, the sweep rule is about **not shipping half a fix**. Neither is
licence to ignore the other. A fixer that sweeps the tree to find the sites its own change invalidated is
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
> name, an API behavior), you are NOT done until every place that RESTATES it is also correct.** Sweep
> the whole tree, not just the file you were sent to. Restatements hide in **summaries**, **quick-reference
> bullets**, **cross-references**, **table rows**, **worked examples**, and **other copies of the same
> command**. A summary that has drifted from its definition is **worse than no summary** — it is the
> version people actually read. **Report every site you found and its disposition, including the ones you
> deliberately left alone and why.** If your fix is genuinely local and restates nothing, say so explicitly.
>
> This widening is **bounded by the change you are making**: sweep for what your own fix invalidated. It
> is **not** permission to re-derive the diff, re-review the PR, or fix defects nobody asked you to fix.
>
> **GREP CANNOT TELL YOU WHAT TO SEARCH FOR — whether you EDIT a rule or INTRODUCE one.** A restatement is
> greppable only if it COPIED the old value, the old spelling, the old number. One that **PARAPHRASES** the
> rule contains no old string, and an EDIT has just as many paraphrases as an introduction: the one-line
> summary *"green means zero failing and zero pending"* outlived THREE rewrites of the rule it summarised
> and silently dropped both of that rule's disclosed caveats — that rule was EDITED, and no search for an
> old value would ever have reached that sentence. So **searching for the old value/spelling/command/number
> is ONE INPUT to the sweep, never the sweep itself.**
>
> An INTRODUCED rule is the same trap at its sharpest: there is no old string to hunt at all, and
> **searching for the new rule's own WORDING or NAME matches only the sites you already fixed, so it
> reports success every time and is wrong every time.** The shape of that miss — **INVENTED strings, they
> exist nowhere in this repo, see the rule below**: you add *"a re-queue must reset the freshness
> counters"*, and somewhere else a line already reads *"it only bumps `retry_epoch`"*. That line is now a
> stale restatement of your brand-new rule, and it shares **not one word** with it — no search for the
> rule's wording, its name, or any old value will ever reach it.
>
> **This does NOT mean text search is useless** — believing that swaps one incomplete method for another,
> and makes you skip a search that would have worked. Searching for the **BEHAVIOR IDENTIFIERS the rule
> governs** — the field, the state, the command, the cap's name — is legitimate and necessary; it is HOW
> you execute the sweep. Continue the invented example: *"it only bumps `retry_epoch`"* names a field, and
> a search for `retry_epoch` WOULD have found it — **but only once you enumerated the sites that state what
> a re-queue does.** Nothing in the new rule's own wording ("reset the freshness counters") tells you to go
> looking for `retry_epoch`. That is the whole gap the enumeration closes.
>
> **AN ILLUSTRATION OF A DEFECT MUST NEVER BE A LIVE STRING IN THE TREE.** When you quote a bad line as an
> example — here or in any doc you fix — quote one that exists **nowhere**, invent it, and say you invented
> it. Quote a real one and the doc becomes a false-positive generator: the next sweeper searches for the
> example, lands on the live site, and condemns text that is correct. If a real quotation is truly
> unavoidable, mark it HISTORICAL unmistakably — but prefer the invented one, because that marking has to
> say **where** the phrase is still live and **why** it is correct there, which is a fact about the tree,
> and it rots. **An example a reader can act on by mistake is worse than no example.**
>
> So, for EVERY definition or fact change alike: **enumerate SEMANTICALLY first, then search for the
> identifiers the enumeration names.** List every site that **DOES OR STATES THE THING THE RULE GOVERNS** —
> writes the field, enters the state, performs the reset, states the cap — search for each of those
> identifiers, and check every hit against the definition.
> **The enumeration tells you WHAT to search for; the search is how you execute it, never a substitute
> for it.** Enumerate the BEHAVIOR, not the words. Report the site list.
>
> **A POINTER WITH A GLOSS IS STILL A RESTATEMENT.** A site that correctly points at the owner and then
> "just briefly" restates what it points at — *"reset the liveness counters (`settled_strikes`,
> `unusable_refetches`)"* — has **copied the definition into the gloss**. When the owner gains a member,
> the pointer stays right and **the gloss silently goes wrong**, and the gloss is the part people read.
> This shape survives every sweep that asks "does this site point at the owner?", because it does.
> **Name the set. Do not unpack it.** If a gloss is genuinely needed, it must be COMPLETE — and then it
> is a copy, and you own the cost of keeping it true.
>
> **MAKE THE CHECK MECHANICAL WHEREVER YOU CAN.** "Did you sweep thoroughly?" is not a check — it cannot
> fail. "`rg -Un '<the value>'` returns exactly one hit, and here is every hit accounted for" is a check.
> Prefer a command whose output you must reconcile out loud over a promise that you looked.

This is a **report** requirement, not just a search requirement: a sweep nobody can audit did not happen.

**Prefer ONE OWNER over another copy.** When the fixer finds a definition restated in N places, the best
repair is usually to leave the definition in one place and make the others point at it — a fifth copy of
a rule is a fifth thing that can go stale. A pointer is only valid if what it points at is **complete**;
pointing at a partial definition is itself the defect. And a pointer is only a pointer while it stays a
pointer: the moment it unpacks what it points at, it is a copy wearing a pointer's clothes.

**A value that appears twice has two sources of truth.** Give every constant (a cap, a limit, a count)
exactly ONE defining site that carries the literal; everywhere else names it. Then the sweep for it is a
command, not a judgment call.

**But an EXTERNAL constant that happens to equal yours is NOT a duplicate of it.** GitHub's documented 6h
per-job execution limit is not your 6h stall cap — they are two independent facts that currently agree.
"Unifying" them destroys a true external fact and couples you to a coincidence: change your cap to 4h and
GitHub's limit still reads 6 hours. Keep the external constant EXACT, and mark it as theirs, so the next
sweeper does not helpfully collapse it into yours.
