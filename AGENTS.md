# claude-code-plugins

This repo **authors** the agent instructions: `plugins/**` defines the skills that may be driving the
current session. The skills actually running in a session are the **installed** copies, not the working
tree. The consequences below are all non-obvious:

## Keep Claude Code and Codex compatible

This repository ships the same plugin skills to Claude Code and Codex. Treat every file under
`plugins/**/skills/**` as shared unless its path explicitly names one host.

Read `docs/runtime-compatibility.md` before changing manifests, marketplaces, skills, plugin docs, or
validation.

- NEVER require a host-specific tool name, agent type, model name, invocation form, environment variable,
  or wake mechanism without an explicit host adapter and a safe fallback.
- Resolve bundled resources from the directory containing the active `SKILL.md`. NEVER depend on
  `CLAUDE_PLUGIN_ROOT` or `PLUGIN_ROOT` being available to a worker.
- When invocation syntax matters, document both forms: Claude Code `/plugin:skill`; Codex `$plugin:skill`.
- Cross-agent review is opt-in: Claude Code may run `codex exec`, and Codex may run `claude -p`, only
  when the user selects it explicitly or has saved that preference.
- Keep `.claude-plugin/plugin.json` and `.codex-plugin/plugin.json` names and versions synchronized.
- Validate both marketplace formats and both plugin install paths before merging.

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

The hosts load skill content under `plugins/**` (`SKILL.md`, `references/`, `scripts/`) from the
**installed** plugin cache (`~/.claude/plugins/cache/...` or `~/.codex/plugins/cache/...`), not from the
working tree. So a branch's changes to *those* do **not** take effect just because they are checked out.

**This file is the exception.** Codex loads root `AGENTS.md`; Claude Code loads the `CLAUDE.md` symlink to
it. Each host reads the shared file from the checkout at session start, with no hot reload. A checked-out
edit is live for sessions **launched from that worktree** — including a session launched to review it.
Therefore a change to `AGENTS.md` or `CLAUDE.md` is **PR content, never gate authority**.
It MUST NOT be treated as the rule governing its own review; it is judged by the **installed** gate and
by the user, exactly like any other PR content. **NEVER put a gate-DECIDING rule into `AGENTS.md` (or its
`CLAUDE.md` symlink) and then rely on it while gating the PR that introduces it** — that is a branch
acting as its own gate authority, the exact hazard this section forbids.

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

## Your OWN diagnosis is a claim too — reproduce the failure before you "fix" it

The campaign gate already knows that **a reviewer's finding is a CLAIM, not a fact**, and must be audited
CONFIRMED / ADJUSTED / REFUTED before any fix is dispatched (`critical-rules.md`). **That rule applies to
your own diagnoses with exactly the same force, and it is the one place it kept getting skipped.**

**If you cannot demonstrate the failure, you do not get to change the code.**

Before you "fix" existing behavior, produce the reproduction: the command that fails, the input that
breaks it, the line that cannot be reached. Walk the causal chain of the bug you *believe* is there and
check every link. If you cannot make it fail, **it is not broken** — and what you are about to write is
not a fix, it is a regression with good intentions.

**Name the asymmetry, or you will misread this as contradicting the rule beside it.** `critical-rules.md`
also says **"unsure → CONFIRMED, never REFUTED"**: for a *reviewer's* finding, uncertainty means **fix
it**. Here, for your *own* diagnosis, inability to reproduce means **do NOT fix it**. Both are correct,
because the **evidence differs** — that, not the verdict, is what to reason from:

- A **reviewer's finding is INDEPENDENT EVIDENCE.** A separate, context-isolated observer looked at the
  code and saw something. Being unsure about it means *you* have not yet understood what *they* saw — so
  it stays **CONFIRMED** and gets fixed: wrongly refuting a real defect is far worse than wrongly fixing
  a phantom one.
- A **self-originated diagnosis has NO independent corroboration.** Nothing observed it but you. So it
  needs a **demonstrated failure or a verified causal chain** before any fix is dispatched — otherwise a
  fix subagent is dispatched at an **invented** bug and lands a regression that CI and the review gate
  will happily pass, because nothing downstream knows the bug was never real.

**Unsure that a REVIEWER is right → FIX. Unsure that YOU are right → STOP and reproduce it.** The two
rules never conflict once that is named — and they always appear to, as long as it is not.

**Why this rule exists:** a PR in this repo changed `--state open` to `--state all` to fix terminal-PR
reconciliation. The reconciliation was **never broken** — `main` already handled it via **absence** (with
`--state open` a merged PR simply drops out of the snapshot, and the existing rule reads that absence as
the signal). The "bug" was a misreading. The change then broke adoption (it resurrected merged PRs as
active work), and several subsequent review rounds were then spent patching the consequences of an
invented problem, round after round — the exact death-spiral this repo has already suffered before. It
took a human's call to stop it.

The tell is unmistakable in hindsight, and it generalises: **when each fix creates the next finding, you
are not converging on a bug — you are patching your own invention.** Stop and re-derive whether the
original thing was ever broken.

## Fix the CLASS, not the instance — a definition with one stale restatement is a definition that lies

When you change a **definition** (a rule, a command, a schema, a format) or a **fact** (a count, a name,
an API behavior), the change is not done when the thing you were looking at is correct. **It is done when
every place that RESTATES it is correct.**

**Sweep for restatements. Every time. Before you call it fixed.**

- **Enumerate SEMANTICALLY first, then search for the identifiers that enumeration names.** That is the
  method for **every** definition or fact change — one you EDITED and one you just INTRODUCED alike. List
  every site that **does or states the thing the rule governs**, search for those behavior identifiers (the
  field, the state, the command, the cap), and check every hit. Restatements hide in **summaries**,
  **quick-reference bullets**, **cross-references**, **table rows**, **worked examples**, and **other
  copies of the same command** — not just the file you edited.
- **`grep` for the old value, the old spelling, the old command, the old number is ONE INPUT to that
  sweep, never the sweep itself** — `grep` cannot tell you WHAT to search for. A restatement is greppable
  only if it COPIED the old value; one that **PARAPHRASES** the rule contains no old string, and an EDIT
  has just as many paraphrases as an introduction — the drifted "green" summary below paraphrased a rule
  that was rewritten three times, and no search for an old value would ever have reached it. An INTRODUCED
  rule is the same trap at its sharpest: there is no old string at all, and searching for the new rule's
  own WORDING or NAME matches only the sites you already fixed, so it reports success every time and is
  wrong every time. The shape of that miss (**INVENTED strings — they exist nowhere in this repo, see the
  bullet below**): you add *"a re-queue must reset the freshness counters"*, and a line elsewhere already
  reads *"it only bumps `retry_epoch`"* — a stale restatement of your brand-new rule that shares not one
  word with it. None of this is a licence to skip text search: searching for the **behavior identifiers the
  rule governs** (here, `retry_epoch` — which only the enumeration could have told you to look for) is HOW
  you execute the enumeration.
- **An illustration of a defect must never be a live string in the tree.** When you quote a bad line as an
  example, quote one that exists **nowhere** — invent it, and say you invented it. Quote a real one and the
  doc becomes a false-positive generator: the next sweeper searches for the example, lands on the live site,
  and condemns correct text. If a real quotation is unavoidable, mark it HISTORICAL unmistakably — but
  prefer the invented one: that marking has to say where the phrase is still live and why it is correct
  there, which is a fact about the tree, and it rots. **An example a reader can act on by mistake is worse
  than no example.**
- **A pointer with a gloss is still a restatement.** A site that points at the owner and then "just
  briefly" restates it — *"reset the liveness counters (`settled_strikes`, `unusable_refetches`)"* — has
  copied the definition into the gloss. The pointer stays right while the gloss silently goes wrong, and
  the gloss is the part people read. **Name the set; do not unpack it.** This survives every sweep that
  asks "does this site point at the owner?", because it does.
- **Prefer a mechanical check to an exhortation.** "Sweep thoroughly" cannot fail. "`rg -Un '<value>'`
  returns exactly one hit, and here is every hit accounted for" can — reconcile the output out loud.
- A **summary that has drifted from its definition is worse than no summary**: it is the version people
  actually read, and it will be believed. A one-line "green means zero failing and zero pending" outlived
  three rewrites of the rule it summarised, and silently discarded both of that rule's disclosed caveats.
- Prefer **one owner + pointers** over N copies. But then the owner must be **complete** — a pointer to a
  partial definition is how a reader reconstructs a command with the run-scoping `--label` missing. And a
  pointer must name the **block**, not the **step number**: numbers move.
- Distinguish **volatile facts** from **stable constants**. A live count from a real repo (`16 runs named
  X`) rots — cite the permanent **claim** and mark any number as a dated illustration. A documented API
  constant (a page size of 30, a 1000-item cap) is the **point** — keep it exact, never hedge it.

**Why this rule exists:** across one PR series, the review gate had to say *"you have two more"* about
missed propagation sites, a fourth copy of a canonical command, a stale summary, and volatile counts in
three separate places. Each time, the instance was fixed and the class survived. **The reviewer
generalised; the author did not.** That is a defect in how the work was done, not in any one line.

## Version is the plugin cache key

`plugin.json`'s `version` decides whether an installed copy refreshes. Changing skill content **without**
bumping it means installed copies stay stale at the old content while claiming to be current — this has
already happened once across several releases. Bump the version in the same PR as any `plugins/**`
content change, or in a release PR that batches several.
