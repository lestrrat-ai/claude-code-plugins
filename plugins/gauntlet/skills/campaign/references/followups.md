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

**THE DRIVER MUST NOT ACTION AN UNCORROBORATED CLAIM.** That is the whole guarantee, and everything below
is how it is kept. What the driver may do with a claim depends entirely on what it has DONE to corroborate
it — which is the threshold in the next section.

## THE AUTONOMY THRESHOLD — three tiers. READ THIS BEFORE TOUCHING A FOLLOW-UP.

**This is the rule that decides what the driver may do without asking.** It is owned here.

### Tier 1 — INVESTIGATE. Do it FREELY. No consent, no ceremony, no ask.

Read the code. Run the commands. **Reproduce the failure.** Walk the causal chain. Investigating a
follow-up needs no permission and never has: it is **STRICTLY READ-ONLY with respect to the repo** — no
tracked-file edits, no PRs, nothing published — and its only product is **EVIDENCE**.

**AN INVESTIGATION CAN REFUTE THE CLAIM AS EASILY AS CONFIRM IT — and refuting is its most valuable
outcome.** This repo has already burned four review rounds "fixing" an invented bug that was never real
(`CLAUDE.md`, "Your OWN diagnosis is a claim too"). An investigation that can only ever say *yes* is not
an investigation; it is a rubber stamp with a longer runtime.

So the outcome is **recorded**, either way, with the evidence that produced it:

```
followups.py --file <store> corroborate --id fuN --finding "reproduced: <command>, <what happened>"
followups.py --file <store> refute      --id fuN --finding "could not reproduce: <what I tried>"
```

**A REFUTED FOLLOW-UP IS NOT DELETED.** It stays in the store, with its evidence, and it stays **visible**
in the default view — the driver refuting its own claim is exactly the thing the user must be able to
audit. The user can overturn it (`accept`), and so can a **later, better investigation** (`corroborate`
again): the finding **APPENDS**, so the record of the driver changing its mind survives, and nobody has to
take the latest verdict on trust.

### Tier 2 — ACT (write a fix, open a PR). ALL FOUR conditions, or ASK.

The driver may take a follow-up up for work with no user ruling — **only** when **every one** of these
holds. They are not a checklist to feel good about; each is **EVIDENCED IN THE ENTRY**, and
`followups.py take-up` **REFUSES the step** if any is asserted without evidence.

1. **`corroborated`** — an independent reviewer confirmed it, **or** the driver reproduced the failure.
   This one is structural: `take-up` leaves **only** from the `corroborated` state, so a claim nobody
   investigated cannot be taken up at all, and the investigation's own `finding` is its evidence.
2. **`not-gate-machinery`** (`--act-not-gate`) — it does not decide whether a PR may merge. `CLAUDE.md`
   defines the gate; do not reconstruct that definition here. **WHEN IT IS UNCLEAR, IT IS GATE
   MACHINERY** — the ambiguous case resolves toward **ask**, never toward act.
3. **`behavior-preserved`** (`--act-behavior`) — it preserves user-facing behavior. If it **has** a
   behavioral surface, **name the test that proves it**. If it has **none**, say so and name why — *an
   assertion of no-behavioral-surface is itself a claim*, and it is recorded like any other. (A docs-only
   change has no behavioral surface. That is a legitimate answer, not a loophole — but it must be
   **stated**, because it is the thing that could be wrong.)
4. **`reversible`** (`--act-reversible`) — a revert restores the prior state. A schema migration is not
   reversible. Anything already published is not.

```
followups.py --file <store> take-up --id fuN \
  --act-not-gate "..." --act-behavior "..." --act-reversible "..."
```

It lands in **`self-accepted`** — deliberately **NOT** `accepted`. A follow-up the **user** agreed to and
one the **driver** took up on its own are different things, forever, and the table says which at a glance.
**The PR that comes out of an ACT is not self-approved**: it is gated by the review gauntlet like any
other, which is the independent authority the driver is not.

If any condition fails — or you are unsure whether it holds — **the follow-up is a candidate you surface
and ASK about.** That is not a failure state. It is the normal one.

### Tier 3 — PUBLISH (a GitHub issue, a release). ALWAYS the user's call. Never autonomous.

An issue is a **published claim**: it asserts to anyone reading the repo that the thing is real and worth
doing, **in the user's name**. A follow-up has not earned that, and filing one unilaterally launders an
unvalidated self-diagnosis into a public statement of fact.

**There is no autonomous path to `publish`.** Not from a candidate, not from a corroborated one, not from
one the driver took up and shipped. `publish` leaves **only** from `accepted`, `accepted` has exactly one
in-edge, and that edge is the user's `accept` — the graph has no other way there. See the lifecycle below.

The promotion path for publication is **raise → consensus with the user → publish**. Nothing skips the
middle step.

## THE LIFETIME OF AN ENTRY — delete once a durable record exists ELSEWHERE; KEEP what prevents repeated work

**This store is a WORK QUEUE, not an archive.** It is **local** and **git-ignored**: it does not survive a
fresh clone and nobody else can see it. That makes it a poor archive and a fine queue — *and an archive
nobody reads is just a file that grows.*

So a finished follow-up is **DELETED**, not parked in a "done" state and hidden. But **when** it is deleted
is the whole safety of it, and there is exactly one test:

**Is there a record ELSEWHERE?**

- **A PR that MERGED** — it is on GitHub, it is reviewable, and it is where anyone actually looks for *"why
  did we do this"*. The entry can go; the fact did not.
- **PUBLISHED as an issue** — the issue is then the record.

**DELETION NEVER HAPPENS WHEN THE WORK IS MERELY TAKEN UP.** An entry deleted at `take-up` is one a PR that
is **closed, abandoned, or rejected in review** takes down with it — the work still undone, and *nothing
left to remember it*. That is the exact permanent loss this store exists to prevent, just moved later in
time. So:

- While the PR is **open**, the entry **STAYS** (`in-pr`) and records **which PR** is addressing it.
- The PR **merges** → `merged` deletes the entry. The PR is the record now.
- The PR is **closed WITHOUT merging** → `closed-unmerged` returns it to **open work** (`reopened`), with
  its history intact — the finding, the ACT grounds or the user's ruling, and the PR that died. It never
  vanishes with the PR, and it is never stuck in "being worked on" forever.

**Move it in the wake that SAW the event** — the same rule as recording one the moment it is noticed, and
for the same reason: the driver's memory of it dies with the driver's context. The wake that opens the PR
addressing a follow-up runs `open-pr`; the wake that observes that PR **merged** or **closed** runs
`merged` / `closed-unmerged`. A follow-up whose PR landed three wakes ago and still sits in `in-pr` is a
queue nobody can trust to say what is left to do.

**AND REJECTIONS ARE KEPT.** A `rejected` entry stays in the store — hidden from the default view (nobody
has anything left to do about it), **never deleted**. This is not an exception to the rule above; it is
that rule, applied: ask a rejection the same question. *Is there a record elsewhere?* **No** — nothing was
filed and nothing merged, and this store is the only place the user's *no* exists. Delete it and the next
run rediscovers the same thing, records it again, and **asks the user the same question** — forever. **A
rejection is worth remembering precisely so it is not re-raised.**

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
hand-edit the JSONL**. Resolve its absolute path as `<skill-dir>/scripts/followups.py`, run it as
**`python3 <that path>`** (`SKILL.md`, "Bundled Scripts" — never bare), and pass that path to subtasks,
exactly as with `ledger.py` and `emit-progress.py`.

```
# Run: python3 <skill-dir>/scripts/followups.py --file <store> <subcommand> …
# The synopsis abbreviates that `python3 <skill-dir>/scripts/followups.py` prefix to `followups.py`.
followups.py --file <store> add --title T --evidence E --deferred-why W [--run <run-id>]  # raise a CANDIDATE
followups.py --file <store> corroborate --id fuN --finding F   # TIER 1 — free. An investigation confirmed it
followups.py --file <store> refute      --id fuN --finding F   # TIER 1 — free. And it stays in the store
followups.py --file <store> take-up     --id fuN --act-...     # TIER 2 — only with EVERY condition evidenced
followups.py --file <store> accept  --id fuN        # THE USER AGREED — the only edge into `accepted`
followups.py --file <store> reject  --id fuN        # the user ruled against it — and the entry is KEPT
followups.py --file <store> open-pr --id fuN --pr <ref>    # a PR is addressing it — the entry STAYS
followups.py --file <store> merged  --id fuN        # that PR LANDED — it is the record now, so the entry is DELETED
followups.py --file <store> closed-unmerged --id fuN       # that PR died — back to OPEN WORK, nothing recorded it
followups.py --file <store> publish --id fuN --ref <issue> # TIER 3 — only AFTER the user's accept. The ISSUE
                                                           # is the record now, so the entry is DELETED
followups.py --file <store> set --id fuN --<field> <value>      # edit the PROSE of the claim — never EMPTY it
followups.py --file <store> get --id fuN [--field <f>]          # read one entry, or one field
followups.py --file <store> list [--where <field>=<value>]      # ids of matching entries
followups.py --file <store> table [--all] [--fields <f>,<f>,…]  # the open follow-ups (read-only)
```

**A DELETING step still PRINTS the entry it removed, in full** — that record is the driver's handoff, and it
names where the follow-up now lives (the merged PR; the issue). Put it in the report; the store no longer
has it.

The **fields**, the **states**, which transition is legal **from** which state, and **what each transition
must record** are owned by `scripts/followups.py` and printed live by `followups.py --help` / `<cmd>
--help`. **This page does not retype them** — a list here would go stale the day one is added, and the
stale copy is the one people read. The ACT conditions above are the **one** thing stated in both places,
because the driver must read the rule before it can obey it — so the self-test **executes their
agreement**: add or drop a condition in the script and the mismatch with this page turns CI red
(`doc-and-code-agree`).

**What every field is for** (the schema owns the list; this owns the *why*): an entry carries a stable
id, a one-line title, the **evidence** (which PR, which review pass, which `file:line`), **why it was
deferred** rather than folded in, its lifecycle state, which run found it and when, **which PR is
addressing it**, and — once ruled on — when the user decided. **A follow-up with no evidence is a RUMOR**:
nobody can audit an entry that says only *"the merge logic looks wrong"*. **Why it was deferred** is
required on the same terms — without it the next run cannot see why the finding was not simply folded into
the PR that found it, and re-litigates the decision.

**An `id` is never reused, not even after the entry that held it was DELETED** — a reused one would
silently re-point every reference to the old follow-up (the merged PR that closed it, the user's own note)
at a different one. The store therefore keeps the high-water mark of every id it has ever handed out, so
the deleted entry is really gone and its id is still spent forever. That mark is the **one** record in the
store that is not a follow-up; it is the accessor's, and like everything else there it is never
hand-edited.

**The required fields are required at EVERY door an entry can pass through** — `add` refuses to create a
follow-up without them, and `set` refuses to **empty** one that has them. A rule enforced only where an
entry is CREATED is not enforced: `set --evidence '   '` an hour later leaves the same rumor, except this
one the store has already vouched for. **A value that SHOWS nothing is not a value**: whitespace of any
kind is not, `-` is not (it is what an **unset** field holds), and neither is a character that RENDERS as
nothing — a zero-width space, a soft hyphen, a BOM. The accessor asks the Unicode category, not a list of
codepoints, so evidence nobody can see is refused at every door, including the ACT conditions.

**Every value the CLI takes is that value — the timestamps too.** The rule is not "evidence is checked";
it is that **nothing** enters the store without passing the one blank predicate, and that is enforced by
construction rather than by each door remembering to ask: the flags a write door offers are generated from
one intake table, and the accessor validates whatever that table declares. A stamp (`--at`, `--found`) may
be **omitted** — it then defaults to now — but a stamp that is **supplied** and shows nothing is refused
like anything else. It has to be: a blank `--at` on the user's ruling writes an `accepted` entry that was
never ruled on, which is a history `load()` calls illegal, and **the whole store then stops opening** — and
these follow-ups have no other copy anywhere. A write that would leave the store unreadable is refused at
the write, with the file on disk untouched.

**And nothing the CLI accepts is thrown away.** Validating a value you then discard fixes nothing, so the
rule has a second half: **anything this tool accepts, it must either USE or REFUSE** — the mirror of the one
it already keeps on the way out (it will not write a store it could not read back). So a flag exists on a
subcommand **only where that subcommand consumes it**: `--at` is offered by the steps that **stamp**
something and by no others, and passing it anywhere else is an argparse **error**, not a value that
vanishes. `<cmd> --help` names every flag that command takes — and the self-test
(`nothing-accepted-is-dropped`) exercises **every flag of every subcommand** with two values and requires
the tool to behave differently on them, so a flag wired to nothing turns CI red the day it is added. Never
"document" a dropped value: a documented silent discard is still a silent discard.

**The claim's `evidence` and the investigation's `finding` are DIFFERENT FIELDS, and both matter.** One is
why the driver **raised** it; the other is what happened when somebody actually **looked**. A finding never
overwrites the claim, and a second investigation never overwrites the first — it **appends**. The driver
changing its mind is part of the record, not a thing to tidy away. The **ACT grounds** are separate again:
they are the driver's evidence for each tier-2 condition, and nothing can edit them after the fact, because
they are what made the self-acceptance legal.

**There is deliberately NO severity field.** Severity is the driver's judgment about a claim nobody has
corroborated, and a machine-readable rank is exactly what an autonomous driver would sort on and *act*
on — which is the prioritisation the user has not yet given. If an item is worse than it looks, **say so
in its prose**, where a human reads it and rules on it. (The store's own `fu3` does precisely that.)

### The lifecycle — the THRESHOLD, enforced by the graph

Every entry enters as a **candidate**. The state moves **only** along the transition graph in
`followups.py` — `set` cannot write `state`, nor any evidence a transition left behind, and each
transition validates the state it is coming **from**. **The END of an entry is on that graph too:** a
deleting step is an edge like any other, and it is refused from any state it does not leave from.

Two structural facts carry the whole threshold, and both are **proved on the graph** by the self-test
(`user-step-unskippable`), not asserted in prose:

- **No sequence of driver-only steps reaches `accepted`, nor any state `publish` may leave from.**
  `accepted` has exactly one in-edge and it is the user's `accept`; `publish` leaves only from `accepted`.
  Tier 3 has no back door — not a missing check, an absent **edge**. (That `publish` now **deletes** the
  entry rather than parking it changes nothing: the guarantee was never about the state it landed in, but
  about which states the step may leave **from**.)
- **The driver's own edge is evidence-bearing and lands somewhere else.** `take-up` leaves only from
  `corroborated` (tier 2, condition 1) and lands in `self-accepted`, which is never `accepted`.

And a third, which is what makes DELETION safe (`delete-needs-a-record`):

- **No deleting edge can be reached without a durable record in the entry.** Every legal history that
  arrives at one has already written the field that names where the record lives — the PR, or the issue —
  and the accessor **refuses** the step if it does not. An entry cannot be deleted with nothing, anywhere,
  left to remember it.

**AND THE INVARIANT IS ENFORCED WHERE THE DATA ENTERS, NOT ONLY WHERE THE COMMANDS DO.** A transition
checking the state it comes **from** guards nothing against a driver that hand-writes `"state":
"accepted"` into the JSONL — **and that is the driver this store defends against**. So `load()` refuses any
entry no legal history could have produced: an `accepted` with no user ruling stamped, an `in-pr` naming no
PR, a `self-accepted` missing any ACT condition's evidence. Such an entry is not argued with; **it does not
load at all**. This is also why the store is **never hand-edited** — a hand-written entry is, at best, one
the accessor will reject.

**State the limit honestly: the script cannot verify that the user really agreed.** No local file can.
`accept` is a promise the driver makes, and what the graph buys is that **skipping the user is a
DELIBERATE LIE rather than an oversight**. It is a footgun guard, **NOT** a security boundary — the same
class of guarantee as the CI-fix symlink preflight (`stage-2-ci.md`).

**The user's ruling is DURABLE DATA.** `accept`/`reject` stamp when it was made, for the same reason the
ledger's `api_approval` records `approved@<iso>` rather than living in the driver's head: **a later wake
is a fresh agent that never saw the conversation**, and it must not re-ask a question the user already
answered. **Nothing the driver does alone stamps it** — not an investigation, not a `take-up`, not opening
a PR, not `publish`. A ruling written by anything but the user would launder the driver's action into the
user's consent, and it is exactly what `load()` demands of an `accepted` entry.

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

**What the driver may do with what it surfaces is the THRESHOLD above — do not re-derive it here.**
Surfacing an entry to the user *is* how consensus gets reached, and it must never hold the run hostage
(`run-identity-and-lease.md`, "Never hold the run hostage on a user prompt"): raise them alongside the
report and fold any answer in as its own wake. **A `candidate` is a question, not a task**: the driver has
not investigated it yet, so it has nothing to act on and nothing to say — surface it and ask.

---
