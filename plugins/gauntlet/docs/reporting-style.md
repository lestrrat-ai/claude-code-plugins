# A sample reporting style

Nothing here is loaded or enforced by the plugin — see the
[README](../README.md#optional--tell-it-how-to-report-to-you) for why it's a sample and not a setting.

One set of rules that works. Opinionated on purpose. Copy it into your `CLAUDE.md` and edit until it
sounds like what you want to read. It is a snapshot to copy and adapt, not a record of anyone's live
configuration, and it is not kept in sync with any other file.

## The sample

````markdown
# Reporting

## Word choice (ALL user-facing text)

- Plain, common English. NEVER use a rare/formal word when a common one works: "ratified" → "agreed",
  "ascertain" → "find out", "exposes a collision" → "creates a conflict".
- NEVER use math/CS-theory jargon for everyday ideas: "cut vertex", "in-edge", "unsatisfiable" → say it
  plainly ("the only gate", "extra path in", "impossible to meet").
- Metaphors/terms coined earlier in a task → restate plainly at each reuse, don't build on them.
- Test: a non-native technical reader gets it on first pass. Domain terms the code itself uses are fine.

## Interim updates (after tool batches, subagent returns, phase boundaries)

- State what was learned / what changed in plain language, then next step. NEVER narrate mechanics
  ("grepping now") or use labels/numbering invented mid-task.
- End EVERY interim update with either explicit user action item(s) or "Nothing needed from you."
- No new info → no update.
- Budget: 1–4 complete sentences. Full sentences, no fragments/arrow chains.
- One idea per sentence, ≤ ~20 words. NEVER semicolon-chain clauses or nest conditionals — split into
  separate sentences.
- Say what an identifier is on first mention per update: `#30 (retention rules)`, not bare `#30`.
  Applies to PR/issue numbers, branches, agent names.
- 2+ independent items → one short bullet per item, NOT packed into one paragraph. Sentence budget
  covers only the non-bullet part.
- No meta-commentary about own reporting ("because they're real risks, not mechanics"). Report the work
  only.

## Final report

- Summary of work + worktree name (`.worktrees/<branch>`) or directory.
- Action items for user listed explicitly, or state none.

## Answering direct questions

- First sentence = the answer. "Does X need action from me?" → "No." / "Yes: `<action>`." BEFORE any
  explanation.
- Explanation budget: 1–3 sentences, only what the asker needs to act. No root-cause essays unless asked.
- NEVER justify/defend past work unasked. Answer the question, don't defend yourself.
- Side-findings or plans that come up while answering → ≤1 sentence mention ("queuing a doc fix
  separately"). Details on request.
````

## Why each rule is there

Worth understanding before you adopt or drop any of them — several look like minor style choices and are
not.

**"Plain, common English."** The rule most likely to be dismissed as a minor style issue, and the one that
decides whether any of the others matter. A brief report you have to re-read twice saved you nothing.
Agents drift into jargon because it is *precise* — but precision the reader has to decode is not precision,
it is work you handed the reader. Watch for the specific failure: a term the agent made up earlier in the
task, reused later as if you already knew it. The agent knows what it means; you do not.

**"End every update with an action item or 'Nothing needed from you.'"** The single highest-value rule
for an unattended run. It makes *blocked-on-you* impossible to miss and impossible to fake — the agent
must pick one or the other, every time, and cannot leave it unclear.

**"No new info → no update."** Removes the empty "still working" updates that train you to stop reading.
If the agent writes something every time it wakes up, none of it means anything.

**"Never narrate mechanics."** "Grepping the tree now" is a fact about the agent, not about your code.
Across a hundred updates, lines like that push out the ones that mean something.

**"Say what an identifier is on first mention."** After two hours you will not remember what `#31` was.
`#31 (CI liveness)` costs three words and saves a lookup every single time.

**"One idea per sentence, ≤20 words."** The rule people are most tempted to drop, and it is doing real
work: it forbids the semicolon-chained mega-sentence that does contain the news but hides it inside itself.
Splitting forces the important part to stand alone.

**"First sentence = the answer."** When you ask a direct question mid-run, you want the answer, not how
the agent got there. The explanation can follow — it just cannot come first.

**"Never justify past work unasked."** An agent explaining why its earlier choice made sense is
spending your attention on its reputation instead of your problem.
