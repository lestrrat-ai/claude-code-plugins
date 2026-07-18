### 2a-deep. Root-cause pass — one decision made at N sites

**The archetype — recognize it on sight.** The most common root cause behind a run of review findings
is **one decision duplicated at N independent sites**: the same error-classification across every
loader + caller, the same notation/resolution across every code path, the same attribute-value
handling per read-site. Each site is a sibling in a finite space the code must cover **identically**.
The fix is almost never per-site — it is a **single chokepoint every site routes through**, and the
enumeration's whole job is to find **all N sites, including the ones no reviewer has hit yet**. Treat
each review finding as a *symptom of the shared decision*, not a new problem.

**Trigger on the finding's SHAPE, not on a round count.** Only an **audited** finding triggers it —
CONFIRMED or ADJUSTED (`finding-audit.md`, "Audit every finding before you fix it"); a REFUTED
claim describes no defect, so it maps no space. Fire this the moment the **first** audited finding
takes the form "this check / resolution / classification is missing (or wrong) at site X" — i.e. a
decision applied independently at more than one site. Do **NOT** wait for the reviewer to surface
sites 2..N over successive rounds — that is the reviewer mapping *your* space for you, one expensive
review at a time. One such finding is enough to suspect the archetype and map the whole space now. (A
string of `NOT SATISFIED` findings that are siblings in one structured space — same function/concept,
different instance — is the same signal arriving late; treat it identically.)

**The old "no later than the 2nd `NOT SATISFIED`" backstop is GONE — do not look for it, and do not
restore it.** It triggered on a fact about history that nothing recorded, evaluated by a heartbeat that
remembers nothing, and it **never fired once** across 35 review rounds on two PRs
(`bailout-and-final-report.md`). What backstops this pass now is a **counter with a cap**: at a review-loop
cap the PR goes `repairing` and a reassessment pass sees **every round at once** and returns one decision —
and **ROOT-CAUSE is one of the five** (`repair-pass.md`). So this pass is now reached by **two** doors: the
shape trigger above, which is still the fast one and still the one you should be using, and the
reassessment, which is the backstop that actually fires. **This file remains the definition of the pass
itself; the reassessment REUSES it and never reimplements it.**

**Enumeration is a SEPARATE read-only pass — NEVER folded into a fix.**

1. **Name the space and its axes.** What is the finite set every site must cover identically, and what
   are its axes? (The set of cases/inputs/states a function must handle; a `{variant} × {code-path}`
   grid; both directions of a round-trip or other symmetric relation; every call site of one operation
   that must behave the same.) The axes are domain-specific — derive them from the finding, don't
   assume a fixed shape.
2. **Dispatch a dedicated MAPPER subagent whose SOLE deliverable is the map** — read-only, in a
   *fresh* worktree at the PR tip (NOT the fix worktree). It enumerates every cell, marks each
   `HANDLED` / `GAP` / `N-A` with a one-line why, and returns **the complete cell table + a
   one-sentence root-cause statement** — each gap with `file:line` + a minimal repro confirmed by a
   throwaway test (deleted before it reports). Emit the table literally.

   **NEVER ask one subagent to both enumerate and fix.** A fixer under-maps toward what it can reach:
   it finds the sites next to its diff and misses the caller two layers up or the code path it has no
   handle on. A mapper carries no fix pressure, so it maps the space exhaustively — which is the point.
   Enumeration and fix are two subagents, in that order, always.

   **Run the mapper in the `session` class — do NOT downgrade it because it is "read-only."**
   Read-only is not low-judgment: this subagent derives the axes, enumerates every cell, and confirms
   each gap with a `file:line` and a working repro. A weaker model **under-maps** — it returns a
   plausible, tidy, *incomplete* table — which is precisely the failure this whole two-subagent split
   exists to prevent, and an under-map is invisible: the gaps it misses look exactly like cells that
   were never there. The cheap version of this subagent defeats its own purpose (`SKILL.md`,
   "Worker Dispatch").
3. **One batch-fix round** for all confirmed gaps. Route every site through **ONE shared
   chokepoint/helper** so the cells can't diverge again. Add a test per cell.
4. **Resume the gauntlet** on the batched result — the review gate's `required(tier)` fresh,
   context-isolated SATISFIED verdicts (two: a systemic / root-cause change is HIGH tier, never
   TRIVIAL).

Caveats:

- A cell's verdict can be **wrong** — the analysis is a hypothesis (e.g. "handled by X" when X doesn't
  actually cover it). The adversarial gauntlet still arbitrates its two fresh, context-isolated
  SATISFIED verdicts; the deep pass *accelerates* convergence, it does NOT replace the gate. Re-feed
  any reviewer-found gap into the enumeration.
- Completeness = no false-positive AND no false-negative across the space. The pass surfaces both: an
  over-strict path that rejects valid cells is as much a gap as a missing one.
- A whole AXIS can be missed. When a later finding reveals a dimension the enumeration didn't have,
  RE-RUN it on the expanded space rather than patching the single new cell.

This acceptance-path mapper remains the mandatory dedicated native session-class role above. It is
never filled by Stage 2 cross-agent reviewer transport and never renders or replaces a gate verdict.
