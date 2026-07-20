## The reassessment pass — when a PR stops converging, REPAIR IT

**This file owns the definition. `scripts/ledger.py` and `scripts/repair-pass.py` own the enforcement.**
Everything below is a command that exits non-zero, not a rule an attentive agent has to remember — which
is the entire point, and is explained by the section that ends this file.

### The trigger — the loop's memory, and the caps on it

`ledger.py` carries the memory. Two counters, and `ledger.py verdict` is the **ONLY** sanctioned way to
record a verdict — it bumps them, applies the tally, and evaluates the caps **atomically**:

```
ledger.py --file <state.jsonl> verdict --pr <N> --head-sha <the live head> --verdict satisfied|not-satisfied
```

- **`review_rounds`** — landed verdicts. **NEVER reset** — not by a fix, a rebase, or a content change.
  This is the loop's only memory. Reset it and every round looks like round 1 again, which is precisely how
  21 of them passed unnoticed.
- **`ns_streak`** — consecutive `NOT SATISFIED`. Reset **only** by a `SATISFIED`.

**Hand-setting `reviews_ok` for a verdict is FORBIDDEN** — it applies the tally and silently skips the
counters, restoring the amnesia. **But a gate RESET is not a verdict**: a content change (a fix commit, a
CI fix, a conflict-resolving rebase, a bot push) still writes `reviews_ok = 0` through `ledger.py … set`,
exactly as it always has. `verdict` records what a **reviewer decided**; `set` records what a **commit
did**. Do not convert the reset sites into `verdict` calls.

**When a cap is reached on a `NOT SATISFIED`, `ledger.py verdict` sets `status = repairing` and EXITS
NON-ZERO.** The driver **MUST NOT** dispatch another targeted fix and **MUST NOT** launch another review
pass for that PR. It runs the reassessment pass below.

**The caps are evaluated ONLY on a `NOT SATISFIED`** — a `SATISFIED` is the gate *moving*, and a PR one
corroborating pass from merging must never be torn up. (On the real record this is not hypothetical: PR
#42's 10th landed verdict was a `SATISFIED`.)

**The cap values live in `ledger.py` and are named, never numbered, anywhere else** — `ROUND_CAP`,
`NS_STREAK_CAP`, `REPAIR_CAP`. A value retyped in prose is a value that goes stale the day it is tuned,
and the prose is the copy people read. `ledger.py`'s comment defends each number against the real record,
and `ledger-test.py` **replays that record** so a re-tuned cap states, in its own failure message, what the
new number would have done to two PRs that really ran.

### Why this exists

The review gate had **no memory and could not see its own non-convergence.** Every heartbeat is a fresh agent
instance; `reviews_ok` is zeroed on every `NOT SATISFIED`; and nothing counted rounds. So the ledger after
21 review rounds was **indistinguishable from the ledger after 1**, and the skill's stopping rules — "run
the root-cause pass on the 2nd `NOT SATISFIED`", the 1-hour cap — were rules with **no sensor**. They sat
in the skill, unfired, for 8.5 hours.

Two PRs ran **21** and **14** adversarial review rounds. Nearly every round produced a **true** finding, a
fix was dispatched, the fix added code, and the next round found more — in the late rounds, more *in the
guards the loop itself had just added*. Neither PR converged. A human stopped it, and could only do so by
holding all 21 rounds in mind **at once**. That view is what no heartbeat had, and it is what this pass restores.

**The reviewer was not malfunctioning.** It was doing exactly what it was asked, and what it was asked has
**no fixed point**: an open-ended adversarial mandate over a growing surface always has one more true thing
to say. Do not look for a bug in the reviewer. The bug is that nothing could **count**.

### A cap is a MODE SWITCH, not a doorbell

**It does not stop and ask the user.** The driver stops dispatching targeted fixes and **repairs the PR
itself, autonomously.** Asking a human is one of the five outcomes, and it is the last resort — not the
first.

### The reassessment pass — give the loop the memory it never had

Dispatch **one context-isolated worker** in the **`session` class** (it decides the acceptance path — never
downgrade it; `SKILL.md`, "Worker Dispatch"), and hand it **THE WHOLE HISTORY AT ONCE**. This is the
crux: **no heartbeat has ever had this view**, which is exactly why 21 rounds could pass unnoticed.

### Build the complete reassessment bundle

**Build the worker prompt only through `repair-pass.py bundle`:**

```text
repair-pass.py --file <state.jsonl> bundle --pr <N> --run-dir <rundir> \
  --worktree <pr-worktree> --output <rundir>/repair-<pr>-<k>.prompt.txt
```

The command selects rounds numerically, selects each round's active launch attempt through
`review-pass.py`'s identity rules, and validates the complete artifact set before writing anything. It
includes the active reports/findings, each round's `finding-audit.py`-owned audit with explicit absence
markers (the complete audit results and standoff rulings the accessor returns, never a summary), intent,
cumulative per-commit file measurements, current three-dot diff, and the ledger-derived `permitted` result
as JSON data. Dynamic bytes never become shell source.

The command writes the prompt and `<output>.manifest.json`, then prints the manifest location and hashes.
It refuses missing or duplicate active artifacts, an incomplete pass, a report whose framing `review-pass.py`
rejects (no terminal `VERDICT:` line, or a verdict incoherent with the round's findings), a stale
latest-review/ledger/worktree SHA, or a failed Git read. It resolves `origin/<base>` to one immutable commit
SHA before any read and binds it, so every diff is measured against a single base and `decide` can detect a
base that moved. **Re-running it while the decision is still unrecorded is safe and idempotent:**
because the bundle is deterministic, an existing prompt/manifest pair whose bytes match the freshly rebuilt
bundle is REUSED — so a heartbeat that built the bundle and died before `decide` simply resumes — a partial
pair left by a crash mid-write is regenerated, and a non-matching or symlinked output is refused rather than
overwritten. Dispatch the exact prompt file to the reassessment worker; NEVER rebuild, reorder, summarize,
or splice its inputs by hand.

It returns **exactly ONE decision from a CLOSED enum**, and the driver executes it **without asking the
user**:

| Decision | When | What the driver does |
|---|---|---|
| **RESCOPE** | the diff has **outgrown its stated purpose** — the findings may all be true, and most of the lines now defend the guards the loop itself added | dispatch a shrink back to intent, then re-gate |
| **REPAIR-INTENT** | the intent artifact is **missing, vague or wrong**, so the reviewer has nothing to measure against and **nothing can be out of scope** | re-author it (Purpose / Non-goals / Threat model), then re-gate |
| **DEMOTE** | the findings **anchor to no Purpose line and no Threat-model actor** — true, and not reasons to block this PR | record them as follow-ups, **do NOT fix them**, re-gate |
| **ROOT-CAUSE** | the findings **share one cause** | run the root-cause pass — **`root-cause-pass.md` already defines it; REUSE it, do not reinvent it** — and fix at the chokepoint |
| **ABORT** | unsalvageable | the **existing** bailout procedure (`bailout-and-final-report.md`): **leave the PR OPEN**, drop this run's labels, write `abort-<id>.md` |

The decision is recorded through the tool, and **only** through the tool:

```
repair-pass.py --file <state.jsonl> decide --pr <N> --decision <one of the five> \
  --record <rundir>/repair-<pr>-<k>.md \
  --bundle-manifest <rundir>/repair-<pr>-<k>.prompt.txt.manifest.json
```

`--record` is **refused if it does not exist or is empty**. The reasoning — the history the pass saw, the
decision, and why — must be **on disk**: every heartbeat is a fresh agent instance, and a justification that
lives only in the context of an agent that has already exited is one nobody can audit. Its first nonblank
line must copy the prompt's exact `BUNDLE-SHA256: <hash>` marker, and it must carry, on its own line, a
machine-readable `DECISION: <enum>` field naming the chosen decision. `decide` re-hashes the prompt payload
and refuses a record or manifest for different bytes, PR, decision-determining ledger fields (the
`DECISION_FIELDS` projection in `repair-pass.py`, not the full row — its liveness fields keep moving under
the CI-observation exception, so they are excluded from the bundle bytes), head SHA, or a base ref that has
moved since the bundle was built. It also refuses a record whose `DECISION:` line is **absent, duplicated,
not permitted, or disagrees with `--decision`** — the record is the sole carrier of the decision across the
fresh-heartbeat boundary, so the ledger can never record a decision the audit artifact does not name.

### The repair is dispatched only after its decision is recorded

While `status = repairing`, **ordinary gate work on that PR is refused** — no review pass, no review fix,
no CI fix, no merge, no rebase, no relabel. The repair itself is the one thing that may run, and it is
gated on the decision existing:

```
ledger.py --file <state.jsonl> dispatch-check --pr <N>                    # before ANY action that mutates the PR
ledger.py --file <state.jsonl> dispatch-check --pr <N> --action repair    # before dispatching the decided repair
```

Without that second gate the guard would have a hole exactly where it matters: a driver could call its next
targeted fix "the repair", dispatch it, and **go on whacking moles under a new name**. `--action repair`
refuses until a decision is on the row, and prints **which** decision, so the work that follows is the work
that was decided.

When the repair has landed, return the row to the gate (`ledger.py … set --pr <N> --status in_review`) and
let the review gauntlet run again from the top. **`review_rounds` is not reset** — it never is. A PR that
comes back to a cap has spent another repair.

### Bound the repair itself — it must not become the new spiral

**`repair_count`, capped at `REPAIR_CAP`.** At the cap, the **only** permitted decision is **ABORT** —
`repair-pass.py` refuses every other one, and `permitted` says so before the agent is even asked. **A
second failed repair leaves the PR OPEN for a human rather than looping.** The irony would be fatal: a
mechanism that fixes non-convergence must not itself fail to converge.

### The ownership guardrail — autonomous repair NEVER rewrites a PR it does not own

Campaign **adopts** PRs. They may be the user's, or a third party's. **RESCOPE** and **ROOT-CAUSE** reshape
branch content wholesale, and doing that to someone else's work uninvited is not a repair — it is a hijack.

`pr_origin` is the ledger's answer, set at adoption (`pr-adoption.md`):

| `pr_origin` | Meaning | Permitted repairs |
|---|---|---|
| `gauntlet` | this pipeline opened the PR — it carries the `gauntlet-authored` label, applied by `gauntlet:review`'s handoff when it opens a PR | **all five** |
| `external` | anything else: the user's PR, a teammate's, a PR adopted by number — **and the DEFAULT** | **DEMOTE / REPAIR-INTENT / ABORT only** |

**The default is `external`, and it is load-bearing.** A row whose origin was never established can never
have its owner's branch reshaped. *"I do not know who wrote this"* must never resolve to *"I wrote this"*.

**This does NOT restrict targeted per-finding fixes.** Campaign has always pushed those to adopted PRs, and
that is the workflow the user asked for. What is forbidden on an `external` PR is the **wholesale rewrite**,
never the ordinary fix.

### Why every part of this is a command and not a paragraph

The rules that failed on 2026-07-14 did not fail by being ignored. **They failed by being unevaluable.**
The 2nd-`NOT SATISFIED` backstop triggers on a fact about history that nothing recorded, read by a heartbeat
that remembers nothing. The 1-hour cap was never computed by anything, and its own word — *"stuck"* — did
not describe a loop that produced a real finding and a real fix every single round. It **looked like
progress**, one round at a time, all night.

So: the counters are **on disk**, the caps are **evaluated by the tool that records the verdict**, the
dispatch guard is **a command that exits non-zero**, and the decision enum is **closed and enforced**. A
driver that ignores this mechanism has a **failed command in its transcript**, not a defensible judgment
call. That is the only kind of rule that was ever going to work here.
