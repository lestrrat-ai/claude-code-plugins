## Durable review-learnings — a settled question, remembered across runs

The campaign's reviewer is deliberately hostile and **stochastic**: a fresh, context-isolated pass may
raise a finding an earlier run already settled. When that settlement is durable and code-adjacent — a
REFUTED finding's inline comment in the tree (`finding-audit.md`, "A REFUTED finding is WRITTEN INTO THE
TREE") — the next reviewer on **that PR** reads it and moves on. But the settlement does **not** travel:
a *different* PR that touches the same class carries no such comment, so the same true-but-immaterial
edge case, or the same accepted single-user residual, is re-raised PR after PR. That re-litigation is the
churn this store exists to end.

This store holds the **CLASS-level learnings** those settlements produced, so the driver can consult them
when it prepares a new PR — and it is designed around one hard boundary: **it feeds the DRIVER, never the
reviewer.** See "Gate safety" below; that boundary is the whole of its safety.

### Why this is NOT the follow-up store

`followups.jsonl` is a work **QUEUE**: `followups.md` says it is "a durable work QUEUE, not an archive",
whose entries are **DELETED the moment the work lands elsewhere** ("delete once a durable record exists
ELSEWHERE"). A learning has no "elsewhere" and no completion — it is a standing fact about a **class** of
finding — so a queue would discharge and lose it. The two stores are siblings with opposite lifetimes:

| | `followups.jsonl` (`followups.md`) | `review-learnings.jsonl` (this) |
|---|---|---|
| what it holds | work the driver found and deferred | a settled refuted/demoted **class** |
| lifetime | a QUEUE — entries DELETED once recorded elsewhere | ACCUMULATES — **never auto-deleted** |
| leaves the set by | completion (merged PR / issue) → deletion | the user **revoking** it, or going **stale** — always KEPT |
| consulted by | the driver, as a work list | the driver, when authoring intent / auditing |

They never overlap and neither is changed by the other.

### The store — `.gauntlet/review-learnings.jsonl`

**Home, and why it is durable in the long run.** It sits at the top of `.gauntlet/`, a **sibling** of
`followups.jsonl` and `history/`, and **never under `.gauntlet/tmp/**`** — that tree is wiped, and a
learning must outlive it. Like the rest of `.gauntlet/**` it is **git-ignored**, which fixes its tier:

- **Not `.gauntlet/tmp/**`** — wiped between runs. A learning there would not survive its own run.
- **Not run-scoped** (unlike `history/<run-id>.md`) — a learning raised by run A must bind run C. One
  file, all runs, so it is **not** conceptually tied to a single campaign.
- **Git-ignored, so LOCAL-PER-MACHINE** — it survives every run on this machine, but **not a fresh
  clone**. That is the deliberate boundary: it is the **gauntlet tier**, tied to the campaign yet separate
  from any single run, durable across runs on the machine that drives them.
- **Not committed** — committing a learning is a *policy* choice, and that is exactly what the **repo
  tier** (below) is for, reached only with the user's consent.

**Many writers, no other copy.** Every concurrent run writes this one file, and **nothing can rebuild a
lost learning**. So the accessor **locks** the read-modify-write and **refuses** a corrupt line naming the
line it is on — the same contract `followups.py`/`ledger.py` keep, and for the same reason.

### Editing it — use `scripts/review-learnings.py`

`scripts/review-learnings.py` is the schema-owning accessor and the **only** door: read and write **by
field name**, never hand-edit the JSONL. Resolve its absolute path as `<skill-dir>/scripts/
review-learnings.py` and run it as **`python3 <that path>`** (`SKILL.md`, "Bundled Scripts").

```
# Run: python3 <skill-dir>/scripts/review-learnings.py --file <store> <subcommand> …
review-learnings.py --file <store> record --claim C --justification J --anchor A \
                    --falsifiability F --provenance P     # record a settled refutation/demotion (ACTIVE)
review-learnings.py --file <store> stale    --id rlN --reason R    # anchor changed — set aside, re-evaluate
review-learnings.py --file <store> reaffirm --id rlN --finding F   # a fresh investigation: it still holds
review-learnings.py --file <store> revoke   --id rlN --reason R    # THE USER overturned it — KEPT for audit
review-learnings.py --file <store> promote  --id rlN --tier repo|account   # THE USER consented to promote
review-learnings.py --file <store> set   --id rlN --<field> V   # edit the claim's PROSE (never blank it)
review-learnings.py --file <store> get   --id rlN [--field f]   # read one entry, or one field
review-learnings.py --file <store> list  [--where f=v]          # ids of matching entries
review-learnings.py --file <store> table [--all] [--fields …]   # active + stale (revoked hidden); only ACTIVE are consulted (read-only)
```

The **fields**, the **states**, and which transition is legal **from** which state are owned by the script
and printed live by `--help`; this page states the *why*, not a second copy of the schema (a copy would go
stale the day one changes — `AGENTS.md`/`CLAUDE.md`, "Fix the CLASS"). The rules here are **executable**:
`python3 <skill-dir>/scripts/review-learnings.py self-test` runs the fixtures in
`review-learnings-test.py`, which is what CI invokes.

**What each field is for** (the schema owns the list; this owns the *why*): a stable `id` (never reused,
even after `revoke`); the **claim** — the settled finding CLASS a fresh reviewer must not re-litigate;
the **justification** — why it is not a real defect / is an accepted residual under the calibration; the
**anchor** — the files/area/finding-class it applies to, which is what a consulting driver **matches** on;
the **falsifiability** condition — *the code change that would make it a REAL defect again*; the
**provenance** — which run / PR / audit settled it, so it can be audited and revoked. All five are
**required** at every door: a learning missing any of them is a **rumor** a future reviewer cannot audit.

### The lifecycle — falsifiable and expiring, never "never raise this again"

A learning is **never** an unconditional "do not raise X." It carries the condition under which it stops
holding, and the graph makes expiry a first-class state:

- **`active`** — recorded and currently holding; **consulted**.
- **`stale`** — the anchored code changed materially, so the falsifiability condition **may** now be met.
  The learning is **set aside pending re-evaluation and is NOT consulted** while stale. (Mirrors the
  memory-staleness discipline: an anchored fact whose anchor moved is re-judged, not trusted.)
- **`reaffirm`** (stale → active) — a **fresh investigation** confirmed it still holds; the evidence
  **appends**, so the record of it going stale and being reaffirmed is the audit trail.
- **`revoke`** (active/stale → revoked) — **the user** overturned it (or a later investigation showed the
  harm is now real). **KEPT** for audit — never consulted again, never silently re-recorded.

The class-protection rules hold at **every write door**, not only `record`. The store keeps **one live
record per class** (exact `claim`+`anchor`): `record` refuses a second twin in any state (a `revoked` twin
because only the user un-retires a class; an `active`/`stale` twin because the class is already on file). And
`set` enforces the SAME one-live invariant: it refuses to edit an entry **onto** ANY other entry's
`claim`+`anchor` — active, stale, or revoked — so no `set` leaves two records of one class (editing onto a
`revoked` class's pair is the sharper case: it would resurrect what the user retired). And a durable **user
ruling FREEZES ALL of a ruled entry's content** (`claim`, `anchor`, `justification`, `falsifiability`): on a
`revoked` or a `promote`d/`decided` entry, `set` refuses to edit ANY of them — the tier the user consented
to rested on the `justification`, and a revoked record is KEPT for audit — so a driver `set` can neither
split a class nor rewrite what the user ruled on. Only a fresh **user ruling** changes a ruled learning;
`claim`+`anchor` additionally define the class the twin guard matches on.

Recording a learning **NEVER discharges a finding.** It is written only **after** a finding is already
settled — refuted-and-dropped, or demoted at a review-loop cap — and its `provenance` names that settled
event. This store is a **memory of decisions the gate already made**, never a shortcut around one; it is
**not** a fourth audit disposition beside CONFIRMED / ADJUSTED / REFUTED.

### Gate safety — DRIVER-consulted, never reviewer-injected

A store that could tell a reviewer "do NOT raise class X" would be a way to **BLIND THE GATE**: it could
suppress a real defect in a *different* PR's code the reviewer never independently examined. So:

- **Driver-facing consultation ONLY.** A learning is consulted at exactly two driver decision points:
  1. **Authoring a new PR's intent Non-goals** (`pr-adoption.md` step 3a) — the authored Non-goal is
     git-ignored intent that is passed to the reviewer VERBATIM and **BINDS** it: a finding that attacks a
     declared Non-goal **cannot gate** (`stage-2-review-gate.md`, "NON-GOALS BIND THE REVIEWER"). The
     reviewer therefore does **NOT** re-judge a Non-goal — so a wrong one is **not caught** by it, it
     **silently NARROWS** that review. This holds for the run's **default Non-goals** too: those are
     **OPERATOR-DECLARED** in the ledger header and folded mechanically into every PR (`files-and-ledger.md`,
     `pr-adoption.md`). A consulted learning INFORMS the driver's judgment about what to declare; it is
     **never auto-promoted** into `default_non_goals`, because a store that could silently inject a Non-goal
     across **every** PR of a run is exactly the run-wide gate-blinding this section forbids.
  2. **The finding audit's non-dispositional precedent** (`finding-audit.md`) — a matching learning is
     prior art the audit may **cite**; it does **not** discharge a CONFIRMED finding.
- **The reviewer is NOT the backstop — the DRIVER and the USER are.** Because a Non-goal BINDS the reviewer
  rather than being re-judged by it, the safety cannot rest on a fresh reviewer catching a wrong demotion.
  It rests entirely on: the driver's **relevance** check (the learning must actually apply to THIS PR's
  anchor), the **never-suppress-a-real-guarantee** rule and the **falsifiability** condition (both below),
  and the user-facing **`authored`-intent disclosure** in the final report — the driver names an `authored`
  intent block as such (`stage-2-review-gate.md`), so the USER sees it and can overrule a wrong Non-goal. A
  learning is **never** injected into a review pass's prompt to make a reviewer stand down; the point is
  that even the Non-goal it produces is bound, not re-reviewed, so the demotion must be made safe
  DRIVER-side, before it is ever authored.
- **Never-suppress classes.** A learning may record only an **accepted residual**. It may **never** be
  recorded for a real-guarantee class — fail-closed on malformed input, a **non-owner's** destructive op,
  a false public `gauntlet-accepted` / an under-reviewed stricter tier, or **inaccurate agent-consumed
  docs**. A learning can waive a residual, never a real guarantee (`AGENTS.md`, "This does NOT lower the
  bar on REAL guarantees"). Recording one of those is a driver error, not a store feature.
- **Scoped and falsifiable.** A learning bears only on its own settled claim (its `anchor`), and it
  carries the `falsifiability` condition that makes it **stale** the moment its anchor changes materially.
- **Provenance + auditability.** Every learning names the run/PR/audit that produced it and the
  justification, so the user can review it and `revoke` it.
- **No auto-promotion.** The gauntlet-local learning is the **only** autonomous tier; every promotion
  beyond it is the user's explicit consent (below).

Because a learning only ever feeds the driver's own decision — one whose backstop is the DRIVER's
never-suppress discipline plus the USER, not a reviewer re-judging it — this store is **not gate
machinery**. It cannot raise a verdict, count one, or merge anything.

### Tiering by NATURE, and the promotion path — always the user's consent

The tier a settlement belongs to is decided by **what its justification depends on**:

- **PR-specific** (the refutation depends on THIS PR's code — "this exact mechanism cannot occur here") →
  it stays the **inline REFUTED comment** in the tree (`finding-audit.md`); it is not a class learning and
  need not be recorded here at all.
- **Repo-class residual** (a KIND of finding that is an accepted residual for THIS repo/artifact) → record
  it here (gauntlet tier, autonomous). It is a **candidate** for the **repo tier** — the
  `AGENTS.md`/`CLAUDE.md` "SINGLE-USER, advisory workflow" calibration, which is the existing repo-wide
  reviewing precedent, **committed** so it survives a clone. Promotion there is the user's call:
  `review-learnings.py promote --id rlN --tier repo` records the consent durably (so a fresh agent does not
  re-ask), and then the driver edits `AGENTS.md` under that consent.
- **Account principle** (a general reviewing principle across repos) → the user's out-of-checkout **global
  memory / instructions** (`reviewer.md`'s trusted source). Promotion there is **only on explicit user
  request**: `promote --tier account` records the consent; the user's global state is theirs to edit.

`promote` is a **user ruling** (`revoke` is the other): the graph has **no driver-only path** to it, and
it **stamps `decided`**, exactly as the follow-up store's `accept` does and for the same reason — a
promotion recorded by anything but the user would launder the driver's judgment into the user's consent.
This is the same [[issues-are-published-claims]] discipline: nothing crosses a tier boundary without the
user's agreement on that specific item. A promoted learning stays `active` and locally consulted; the
promotion only **widens** its reach.

### Relation to the materiality decision

`finding-audit.md` (and the materiality investigation it records) decides, **within a PR**, that a finding
is an immaterial accepted residual — by binding it to a declared Non-goal or demoting it at the cap. **This
store makes that decision durable ACROSS PRs and runs.** The two are complementary and share the same
guardrails: neither discharges a CONFIRMED finding on the driver's authority, neither is reviewer-facing
suppression, and both rest on the driver's own never-suppress-a-real-guarantee discipline plus the user —
not on a reviewer re-judging the demotion, which a bound Non-goal is not. Do not duplicate or contradict
that owner; a learning is only ever the **memory** of a settlement it already made.

---
