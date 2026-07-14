## Follow-ups — work the campaign FOUND and deliberately did not do

While gating PRs the driver discovers work it does not do: a defect **out of scope** for the PR in hand,
a **pre-existing** bug a fix subagent noticed and declined to touch, a refinement a review exposed. Left
in the driver's prose it **dies with the driver's context** — the same defect the CI-liveness work exists
to fix, one layer up: *a counter that dies with the context never reaches its cap, and a follow-up that
lives only in the driver's head is a follow-up that is lost.*

So a follow-up is **recorded, in a durable store, the moment it is noticed** — and never in the report,
never in a scratch note, never only in the reply to the user.

### EVERY ENTRY IS A CANDIDATE, NEVER AN ISSUE

**These are things the DRIVER noticed. They are CLAIMS, not facts** — and the driver's own diagnosis is a
claim needing corroboration exactly like a reviewer's (`CLAUDE.md`, "Your OWN diagnosis is a claim too";
`stage-2-review-gate.md`, "Audit every finding before you fix it").

**NOTHING in this store may be PUBLISHED — a GitHub issue, a PR — without the USER's agreement on that
SPECIFIC item.** An issue is a **published claim**: it asserts to anyone reading the repo that the thing
is real and worth doing. A follow-up has not earned that. Filing one unilaterally launders an unvalidated
self-diagnosis into a public statement of fact.

The promotion path is **raise → consensus with the user → publish/fix**. Nothing skips the middle step.

### The store — `.gauntlet/followups.jsonl`

Durable and **user-local**: git-ignored driver bookkeeping like the rest of `.gauntlet/**`, and **never
committed** (campaign has no committed file of its own — `files-and-ledger.md`). It sits at the top of
`.gauntlet/`, a **sibling** of `history/`, and **never under `.gauntlet/tmp/**`** — that tree is
disposable, and a follow-up must outlive the run that found it.

**It is NOT run-scoped, and that is the whole point.** A follow-up raised by run A is promoted by run C.
Two consequences follow, and both are why this is not just another `state.jsonl`:

- **It is the SOURCE OF TRUTH, not a cache.** `state.jsonl` is a hint reconciled against GitHub every
  wake, so a lost row heals itself. **Nothing can rebuild a lost follow-up** — it exists nowhere else, by
  design. A lost entry is lost forever.
- **It has MANY writers.** The lease makes `state.jsonl` single-writer; **every concurrent run** writes
  this one. Its accessor locks the read-modify-write for that reason — hand-editing the file races with a
  live run and silently drops entries.

### Editing it — use `scripts/followups.py`

`scripts/followups.py` is the **sanctioned way** to read and write the store, **by FIELD NAME**. It owns
the schema, the lifecycle graph and the store's invariants in ONE place; agents and subtasks **must not
hand-edit the JSONL**. Resolve its absolute path as `<skill-dir>/scripts/followups.py` and pass that path
to subtasks, exactly as with `ledger.py` and `emit-progress.py`.

```
followups.py --file <store> add --title T --evidence E --deferred-why W [--run <run-id>]  # raise a CANDIDATE
followups.py --file <store> accept  --id fuN        # THE USER AGREED — the only way out of `candidate`
followups.py --file <store> reject  --id fuN        # the user ruled against it
followups.py --file <store> publish --id fuN --ref <issue/PR>   # only AFTER accept
followups.py --file <store> done    --id fuN        # it shipped
followups.py --file <store> set --id fuN --<field> <value>      # edit the PROSE fields only
followups.py --file <store> get --id fuN [--field <f>]          # read one entry, or one field
followups.py --file <store> list [--where <field>=<value>]      # ids of matching entries
followups.py --file <store> table [--all] [--fields <f>,<f>,…]  # the open follow-ups (read-only)
```

The **fields**, the **states**, and which transition is legal **from** which state are owned by
`scripts/followups.py` and printed live by `followups.py --help` / `<cmd> --help`. **This page does not
retype them** — a list here would go stale the day one is added, and the stale copy is the one people
read.

**What every field is for** (the schema owns the list; this owns the *why*): an entry carries a stable
id, a one-line title, the **evidence** (which PR, which review pass, which `file:line`), **why it was
deferred** rather than folded in, its lifecycle state, which run found it and when, and — once ruled on —
when the user decided and where it was published. **A follow-up with no evidence is a RUMOR** and `add`
refuses it: nobody can audit an entry that says only *"the merge logic looks wrong"*. **Why it was
deferred** is required on the same terms — without it the next run cannot see why the finding was not
simply folded into the PR that found it, and re-litigates the decision.

**There is deliberately NO severity field.** Severity is the driver's judgment about a claim nobody has
corroborated, and a machine-readable rank is exactly what an autonomous driver would sort on and *act*
on — which is the prioritisation the user has not yet given. If an item is worse than it looks, **say so
in its prose**, where a human reads it and rules on it. (The store's own `fu3` does precisely that.)

### The lifecycle — the user's agreement is UNSKIPPABLE BY CONSTRUCTION

Every entry enters as a **candidate**. The state moves **only** along the transition graph in
`followups.py` — `set` cannot write `state` at all, and each transition validates the state it is coming
**from**.

**`accepted` is a cut vertex: every path to `published` or `done` runs through it, and the only edge into
it is `accept`** — the transition whose entire purpose is to record that the user agreed to **this item**.
So there is **no route from `candidate` to `published`**. An autonomous driver cannot file an issue for an
unvalidated self-diagnosis by accident, because the graph has no edge that would let it.

**State the limit honestly: the script cannot verify that the user really agreed.** No local file can.
`accept` is a promise the driver makes, and what the graph buys is that **skipping the user is a
DELIBERATE LIE rather than an oversight**. It is a footgun guard, **NOT** a security boundary — the same
class of guarantee as the CI-fix symlink preflight (`stage-2-ci.md`).

**The user's ruling is DURABLE DATA.** `accept`/`reject` stamp when it was made, for the same reason the
ledger's `api_approval` records `approved@<iso>` rather than living in the driver's head: **a later wake
is a fresh agent that never saw the conversation**, and it must not re-ask a question the user already
answered. The driver's own steps (`publish`, `done`) never stamp it — a ruling written by anything but the
user would launder the driver's action into the user's consent.

### WHEN TO RECORD ONE — the moment it is noticed, not at the end

**A rule nobody is told to follow is not a rule.** Record a follow-up at each of these moments, before
moving on:

- **An audit finds something real that is NOT the finding** (`stage-2-review-gate.md`, "Audit every
  finding before you fix it") — a pre-existing defect at the same site, or a wider class an **ADJUSTED**
  finding only clipped the edge of.
- **A fix subagent reports a site it deliberately LEFT ALONE.** The sweep block already requires it to
  report those (`fix-subagent-contract.md`, "SWEEP — bounds the writing"). That report is the follow-up's
  evidence, and the orchestrator records it — **the subagent's report dies with the subagent**.
- **The user defers something** — anything the user says is real but "not now", or descopes from a PR.
  Record it with the user's own words as the evidence; it is already `accept`-able, because the user just
  agreed it is real.
- **A precondition or a bailout exposes a defect the run will not fix** (`bailout-and-final-report.md`).

**RECORDING A FOLLOW-UP NEVER DISCHARGES A FINDING, AND IT IS NOT A VERDICT.** It is **not** a fourth
audit disposition beside CONFIRMED / ADJUSTED / REFUTED, and it **never** subtracts from a fix list: a
CONFIRMED finding gets **fixed**, always. *"I'll file a follow-up instead"* is **REFUTING BY DEFERRAL** —
the exact thing "Refuting is NOT declining" forbids, wearing a different hat. A follow-up records what the
audit **discovered beside** the finding, never the finding it declined to fix.

### Surfacing them

Render `followups.py … table` in the **final report** of a run (`bailout-and-final-report.md`) whenever
the store holds an open entry, and whenever the user asks. It is a **PROJECTION**, on the same terms as
the ledger's (`files-and-ledger.md`, "`table` is a PROJECTION"): it shows only the **open** entries and
only some fields, it **escapes** every cell, and **the omission is never silent** — it states how many it
hid and the flag that reveals them. Read a value back with `get --field`, **never** by parsing the table.

**Ask about candidates; never act on them unasked.** Surfacing a candidate to the user *is* how consensus
gets reached — but it must never hold the run hostage (`run-identity-and-lease.md`, "Never hold the run
hostage on a user prompt"): raise them alongside the report and fold any answer in as its own wake.

---
