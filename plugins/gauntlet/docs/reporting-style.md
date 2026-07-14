# A sample reporting style

Nothing here is loaded or enforced by the plugin — see the
[README](../README.md#optional--tell-it-how-to-report-to-you) for why it's a sample and not a setting.

One set of rules that works. Opinionated on purpose. Copy it into your `CLAUDE.md` and edit until it
sounds like what you want to read.

## The sample

---

### Reporting

#### Interim updates (after tool batches, subagent returns, phase boundaries)

- State what was learned / what changed in plain language, then next step. NEVER narrate mechanics
  ("grepping now") or use labels/numbering invented mid-task.
- End EVERY interim update with either explicit user action item(s) or "Nothing needed from you."
- No new info → no update.
- Budget: 1–4 complete sentences. Full sentences, no fragments/arrow chains.
- One idea per sentence, ≤ ~20 words. NEVER semicolon-chain clauses or nest conditionals — split into
  separate sentences.
- Anchor identifiers on first mention per update: `#30 (retention rules)`, not bare `#30`. Applies to
  PR/issue numbers, branches, agent names.
- 2+ independent items → one short bullet per item, NOT packed prose. Sentence budget covers only the
  non-bullet portion.
- No meta-commentary about own reporting ("because they're real risks, not mechanics"). Report the work
  only.

#### Final report

- Summary of work + worktree name (`.worktrees/<branch>`) or directory.
- Action items for user listed explicitly, or state none.

#### Answering direct questions

- First sentence = the answer. "Does X need action from me?" → "No." / "Yes: `<action>`." BEFORE any
  explanation.
- Explanation budget: 1–3 sentences, only what the asker needs to act. No root-cause essays unless asked.
- NEVER justify/defend past work unasked. Answer the question, don't litigate.
- Side-findings/plans surfaced while answering → ≤1 sentence mention ("queuing a doc fix separately").
  Details on request.

---

## What each rule is actually buying you

Worth understanding before you adopt or drop any of them — several look like style nits and are not.

**"End every update with an action item or 'Nothing needed from you.'"** The single highest-value rule
for an unattended run. It makes *blocked-on-you* impossible to miss and impossible to fake. Without it,
a run that needs your decision reads exactly like a run that is making progress.

**"No new info → no update."** Removes the heartbeat noise that trains you to stop reading. If every
wake produces prose, none of it is a signal.

**"Never narrate mechanics."** "Grepping the tree now" is a fact about the agent, not about your code.
Over a hundred updates it crowds out the ones that mean something.

**"Anchor identifiers on first mention."** After two hours you will not remember what `#31` was. `#31
(CI liveness)` costs three words and saves a lookup every single time.

**"One idea per sentence, ≤20 words."** The rule people are most tempted to drop, and it is doing real
work: it forbids the semicolon-chained mega-sentence that technically contains the news but buries it.
Splitting forces the important clause to stand alone.

**"First sentence = the answer."** When you ask a direct question mid-run, you want the answer, not the
derivation. The explanation can follow — it just cannot come first.

**"Never justify past work unasked."** An agent explaining why its earlier choice was defensible is
spending your attention on its reputation instead of your problem.
