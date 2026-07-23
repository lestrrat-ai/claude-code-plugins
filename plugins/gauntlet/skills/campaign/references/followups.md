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
claim needing corroboration exactly like a reviewer's (`AGENTS.md`/`CLAUDE.md`, "Your OWN diagnosis is a claim too";
`finding-audit.md`, "Audit every finding before you fix it").

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
(`AGENTS.md`/`CLAUDE.md`, "Your OWN diagnosis is a claim too"). An investigation that can only ever say *yes* is not
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
2. **`not-gate-machinery`** (`--act-not-gate`) — it does not decide whether a PR may merge. The active
   repository instruction file (`AGENTS.md` or `CLAUDE.md`) defines the gate; do not reconstruct that
   definition here. **WHEN IT IS UNCLEAR, IT IS GATE
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

## WORKING A FOLLOW-UP — the active loop, one entry at a time

The threshold above says what the driver **may** do. This is the **procedure** that does it — the loop
that turns an open follow-up into either a refutation or a merged PR. Run it when a heartbeat has **spare
capacity** and the gated PRs do not need the driver right now (the nudge's "start on follow-ups" reminder
is one prompt for it). It is **work-conserving, never blocking**: it runs *alongside* gating the campaign's
PRs, never instead of them, and it holds the run hostage on nothing.

**Scope: one run's driver, not cross-run coordination.** The follow-up store is shared across every
concurrent run, and this loop does not claim a follow-up against a *second run* — two runs active at once
could both take up one `self-accepted` entry and open duplicate PRs for it. Making dispatch idempotent
across runs (a run-owner claim field, or deterministic-branch reconciliation that adopts an existing
`gauntlet-authored` PR) needs a `followups.py` store transition, so it is a deliberate **non-goal** here
and is tracked as a follow-up, not solved by this documentation.

**One follow-up at a time. Never a grab-bag.** Pick a single open entry and resume it **by its lifecycle
state** (`followups.py` owns the transitions, and its `--help` subcommand listing prints each
subcommand's exact from-set→to edge). Release the slot once that entry reaches an
**actionable outcome** — refuted, taken up and opened as a PR now being gated (`in-pr`), or surfaced and
awaiting the user — never "a terminal state": only `rejected` is terminal, and it is the user's ruling, so
the loop never reaches it on its own. A PR that bundles several follow-ups is one no reviewer can reason
about and one whose partial rejection strands the rest.

1. **VERIFY the claim — the main line, and where a fresh `candidate` starts.** The common path is one line:
   a `candidate` → **INVESTIGATE** (dispatch a context-isolated Tier-1, read-only subagent) → `corroborate`
   or `refute` → if corroborated and every Tier-2 condition holds, `take-up` → a fix subagent opens a PR →
   `open-pr` → adopt into the run → `merged`. A follow-up is a CLAIM, and the driver's own diagnosis needs
   corroboration exactly like a reviewer's finding does (`AGENTS.md`/`CLAUDE.md`, "Your OWN diagnosis is a
   claim too"). So the driver does **not** verify inline and does **not** skip to a fix: the investigation
   subagent's sole job is to **reproduce the claim** — read the code, run the commands, walk the causal
   chain — and record the outcome with evidence through `followups.py corroborate` / `refute`. This is the
   same audit-before-fix discipline the review gate keeps (`stage-2-review-gate.md`); the investigation and
   any later fix are **two different subagents, in that order, always**.

   **An entry that is NOT a fresh candidate resumes at the step its lifecycle state has already reached — it
   does not restart.** `followups.py` owns the transitions, and its `--help` subcommand listing prints
   each subcommand's exact from-set→to edge — that is the authority on **which store edge** is legal from
   each state; **read the edges there (`followups.py --help` / `scripts/followups.py`), do not re-derive
   them here.** But a store edge
   is **not the whole resume step**: several states also need a **campaign action** — dispatch a subagent,
   reconcile a PR against this run, adopt it into the gauntlet — that **is not a store transition at all**,
   so it is invisible on the graph, and "defer to the graph" would silently drop it. The graph still owns
   every `state` move; what this list adds is the campaign action each move must **accompany**. Resume by
   non-terminal state (the two terminal states — `rejected`, the user's ruling; and a deleted `merged`
   entry — are never resumed, and the loop never reaches either on its own):

   - **`candidate`** (a fresh one) — INVESTIGATE: this is step 1, the common path above. No prior step to
     resume.
   - **`corroborated`** — skip investigation; resume at the `take-up` decision (step 3). No campaign action
     beyond what step 3 already does.
   - **`refuted`** — re-investigated **only** when new evidence may overturn it, and that re-investigation
     succeeds by `corroborate`, never `refute`.
   - **`self-accepted`** — the driver already took it up; the entry has **no PR yet** (a `self-accepted`
     entry stores no PR reference — `open-pr` is the step that first writes one). So resume at **step 3**:
     dispatch the scoped fix subagent, which authors the fix and **opens the PR**; `open-pr` then records it
     (→ `in-pr`) and step 4 adopts it into the run. Do **not** try to "look up" or reconcile an
     already-created PR first — there is **no durable fuN→PR key** to look one up by (no PR field before
     `open-pr`, no fuN→PR reverse index, the `gauntlet-authored` label is run-wide not per-fuN, and the fix
     contract mandates no fuN-keyed branch — `fix-subagent-contract.md`). See the **same-run idempotency**
     note below the list.
   - **`accepted`** — the **user ruled** on it (step 5 reached `accept`), so the driver **skips take-up**;
     the user already decided, so no autonomous ACT is needed. But `accepted` is the single gateway to
     **both** `open-pr` (a fix) **and** `publish` (a Tier-3 issue), and the entry records only **when** the
     user decided (`decided`) — **nothing stores which** they approved. So proceed with the action the
     ruling **authorized**: if the user approved a **FIX**, dispatch the scoped fix subagent under the
     approved scope, which opens the PR, then `open-pr` records it (→ `in-pr`) and step 4 adopts it — no
     reconciliation, exactly as `self-accepted` resumes; if the user approved **PUBLICATION**, that is the
     **publish** path (Tier 3), **not** a fix. If a fresh heartbeat **cannot tell which** the ruling was for,
     **SURFACE the entry to the user** rather than assume a fix. The same-run idempotency note below covers
     its interrupted-heartbeat gap.
   - **`in-pr`** — a PR is open and named in the entry, but an interrupted heartbeat may have recorded `open-pr`
     **without** finishing ADOPTION. Adoption is a campaign action, not a store edge — no `in-pr`
     transition performs it — so "defer to the graph" strands the PR. On resume, **reconcile the recorded
     PR against the current run.** If it has no ledger row, or its **non-terminal** row lacks the run label,
     ADOPT it through step 4. **If its existing row is terminal, NEVER refresh, re-adopt, or relabel it** —
     surface that terminal campaign result and leave the follow-up lifecycle unchanged. Choosing the next
     follow-up transition for an aborted-but-open PR is separate lifecycle work; this adoption guard does
     not invent one. An unadopted follow-up PR with no terminal row sits **outside the campaign gate** —
     the exact thing "fold that PR into the current campaign" exists to prevent. If the user rejects it,
     follow **Rejecting an `in-pr` follow-up** below.
   - **`reopened`** — its PR died and it already carries the decision it earned, so it does **not** re-decide:
     it resumes at opening the **replacement** PR. Dispatch the fixer, which opens the replacement PR, then
     `open-pr` records it (→ `in-pr`) and step 4 adopts it — no reconciliation, same as `self-accepted`. The
     same-run idempotency note below covers its interrupted-heartbeat gap.

   **Same-run idempotency is a deliberate non-goal — the interrupted-heartbeat gap for `self-accepted`,
   `accepted`, and `reopened`.** A heartbeat that dies **after** the fix subagent opens the PR but **before**
   `open-pr` records it leaves the entry in `self-accepted` (or `accepted`/`reopened`) with **no durable
   fuN→PR key** to reconcile against — so the next heartbeat re-dispatches and can open a **duplicate PR within
   one run**. This is the same-run analogue of the cross-run race scoped out under "Scope: one run's driver"
   above — a non-goal for the same reason (the fix needs a `followups.py` store change or a fix-contract
   change), tracked as **a follow-up**, not solved by this documentation.

2. **NOT APPLICABLE → `refute`.** If the investigation cannot reproduce the claim, or shows the mechanism
   cannot occur, it is **refuted** — and that is its **most valuable** outcome, not a failure. A refuted
   entry is **not deleted**: it stays visible with its evidence so the user (or a later, better
   investigation) can overturn it. The driver then moves on. **Refuting is not declining** — refute only on
   evidence the claim is false, never because a fix is inconvenient.

3. **APPLICABLE → `take-up`, then a FIX SUBAGENT that opens a PR.** If the investigation `corroborated` it
   **and every Tier-2 condition holds and is evidenced** (`corroborated`, `not-gate-machinery`,
   `behavior-preserved`, `reversible` — `take-up` refuses without them), the driver takes it up
   (→ `self-accepted`) and dispatches a **scoped fix subagent under the fix-subagent contract**
   (`fix-subagent-contract.md`) that authors the fix **and opens a PR** for it. The driver hands the fixer
   a **worktree branched from the base the follow-up targets**, and the fixer branches, commits, pushes,
   and opens the PR against that base — its worktree and scope requirements are owned by
   `fix-subagent-contract.md`, not restated here. **The target is per follow-up**: a follow-up **derived
   from one PR** takes **that PR's recorded base** (`effective_base` of its ledger row) as the proposed
   target; a **run-level** follow-up has **no implicit target**, so the **user chooses** it. The user may
   override the proposed target before the PR is opened. The resulting PR then enters through **normal
   adoption**, which records its live `baseRefName` once as its immutable row base (`pr-adoption.md`).
   **Adoption admits PRs on DIFFERENT bases into one run** (`pr-adoption.md`, "PR adoption"), so the
   follow-up folds into THIS run (step 4) **regardless of whether its target matches the existing rows** —
   a different-base target is adopted here, never diverted for base disagreement. That PR is opened
   **`gauntlet-authored`** and adopted into the current run so `pr-adoption.md` reads it as
   `pr_origin=gauntlet` — without the label it defaults to `external`, which then blocks campaign's own
   later autonomous repair of the very PR it authored. Record the PR with `followups.py open-pr --id fuN
   --pr <ref>`; the entry stays `in-pr` and names which PR is addressing it.

4. **FOLD THE PR INTO THE CURRENT CAMPAIGN.** The follow-up's PR — which step 3 admitted on its own
   recorded base — is **adopted into this run** like any other
   (`pr-adoption.md` — the `gauntlet-authored` label, ledger row, intent, CI) and **gated by the same
   review gauntlet**. This is the
   whole point of "self-accepted, not accepted": the driver may take a follow-up up on its own, but the PR
   it produces is **judged by the independent gate, not self-approved** — the driver is not its own gate
   authority. When that PR **merges**, run `followups.py merged --id fuN`: the merged PR is the durable
   record now, so the entry is deleted (`closed-unmerged` if the PR dies instead — back to open work).

5. **ANY TIER-2 CONDITION FAILS OR IS UNCLEAR → SURFACE AND ASK.** If the fix would touch gate machinery,
   **change user-facing behavior at all** (Tier-2 condition 3 requires it **preserved** — a named test is
   evidence the behavior is unchanged, never licence to change it), be irreversible — or you are simply
   unsure — it is **not** the driver's to take up. Surface it in the report and let the user rule (`accept` / `reject`). That is
   the normal case, not a failure. And **publishing is never on the autonomous path**: an issue or a
   release always waits for the user's agreement on that specific item (Tier 3).

#### Rejecting an `in-pr` follow-up

**Finish the recorded PR's campaign disposition BEFORE recording `reject`.** `rejected` is terminal, so
the active loop never resumes it. `reject` records the durable user ruling; it does not finish the
campaign ledger row or remove campaign labels. Resolve the recorded PR's live state, then use the matching
sequence:

**If this procedure is interrupted, resume here, not through the state-resume list.** Re-resolve the
recorded PR's live state and continue with the matching branch.

- **OPEN** — run the permanent-abort procedure in `bailout-and-final-report.md`, **1-hour cap per task**,
  to completion. Then run `followups.py --file <store> reject --id fuN`.
- **CLOSED WITHOUT MERGING** — run `merge.py run` through its existing terminal close-out
  (`loop-control.md`, "Step 4 — Merge queued PRs as a serialized drain"). Then run
  `followups.py --file <store> reject --id fuN`.
- **MERGED** — run `merge.py run` to finish the existing merge finalization, then run
  `followups.py --file <store> merged --id fuN`. Do not record `reject`: the merged PR is now the durable
  record and `merged` removes the queue entry.

The existing `reject` edge keeps the recorded `pr`; never clear that history.
Do not add a follow-up state for campaign disposition. The store graph and PR history stay unchanged;
ordering the existing campaign procedure before the existing terminal ruling closes the gap.

**The two subagents are the load-bearing part.** The investigation reproduces before anything is changed,
and the fix authors code that the gauntlet judges — never the same worker doing both, and never the driver
doing either inline. That is what keeps an *invented* follow-up from becoming a *merged* regression, which
is the exact death-spiral this repo has already suffered (`AGENTS.md`/`CLAUDE.md`, "when each fix creates
the next finding").

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

**Move it in the heartbeat that SAW the event** — the same rule as recording one the moment it is noticed, and
for the same reason: the driver's memory of it dies with the driver's context. The heartbeat that opens the PR
addressing a follow-up runs `open-pr`; the heartbeat that observes that PR **merged** or **closed** runs
`merged` / `closed-unmerged`. A follow-up whose PR landed three heartbeats ago and still sits in `in-pr` is a
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
  heartbeat, so a lost row heals itself. **Nothing can rebuild a lost follow-up** — it exists nowhere else, by
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
followups.py --file <store> reject  --id fuN        # user ruled against it; `in-pr` follows "Rejecting an `in-pr` follow-up"
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

**The rules on this page are EXECUTABLE.** The fixtures live beside the script, in
`scripts/followups-test.py`, and are run through it — `python3 <skill-dir>/scripts/followups.py self-test`,
which is what CI invokes. Each fixture pins one rule and goes red if that rule is weakened; the named
fixtures cited below (`user-step-unskippable`, `delete-needs-a-record`, …) are those checks, not prose.

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

**The required fields are required at EVERY door an entry can pass through.** `add` refuses to create a
follow-up without them, `set` refuses to **empty** one that has them, and the store refuses to **open** on a
line that carries none. A rule enforced only where an entry is CREATED is not enforced: `set --evidence
'   '` an hour later leaves the same rumor, except this one the store has already vouched for.

**A value that carries nothing is not a value**: whitespace of any kind is not, and `-` is not — it is what
an **unset** field holds, so a door that accepted it would write an entry that reads back empty. **Nor is a
field a required follow-up simply lacks: absence is not a value either**, so it is never defaulted into one.
Only a genuinely **optional** field backfills, which is what lets the schema grow without migrating a store
that cannot be rebuilt.

**Every value the CLI takes is checked — the timestamps too**, and by construction rather than by each door
remembering to ask: the flags a write door offers are generated from **one intake table**, and the accessor
validates whatever that table declares. Add a flag without registering it and the self-test goes red before
it can ever take a blank. A stamp (`--at`, `--found`) may be **omitted** — it then defaults to now — but a
stamp that is **supplied** and shows nothing is refused like anything else.

**And a flag exists on a subcommand only where that subcommand consumes it.** `--at` is offered by the steps
that **stamp** something and by no others; passing it elsewhere is an argparse **error**, not a value that
silently vanishes. `<cmd> --help` names every flag a command takes. Never "document" a dropped value: a
documented silent discard is still a silent discard.

**The claim's `evidence` and the investigation's `finding` are DIFFERENT FIELDS, and both matter.** One is
why the driver **raised** it; the other is what happened when somebody actually **looked**. A finding never
overwrites the claim, and a second investigation never overwrites the first — it **appends**. The driver
changing its mind is part of the record, not a thing to tidy away. The **ACT grounds** are separate again:
they are the driver's evidence for each tier-2 condition, and nothing can edit them after the fact, because
they are what made the self-acceptance legal.

**There is deliberately NO severity field.** Severity is the driver's judgment about a claim nobody has
corroborated, and a machine-readable rank is exactly what an autonomous driver would sort on and *act*
on — which is the prioritisation the user has not yet given. If an item is worse than it looks, **say so
in its prose**, where a human reads it and rules on it.

### The lifecycle — the THRESHOLD, enforced by the graph

Every entry enters as a **candidate**. The state moves **only** along the transition graph in
`followups.py` — `set` cannot write `state`, nor any evidence a transition left behind, and each
transition validates the state it is coming **from**. **The END of an entry is on that graph too:** a
deleting step is an edge like any other, and it is refused from any state it does not leave from.

Two structural facts carry the whole threshold, and both are **proved on the graph** by the self-test
(`user-step-unskippable`), not asserted in prose:

- **No sequence of driver-only steps reaches `accepted`, nor any state `publish` may leave from.**
  `accepted` has exactly one in-edge and it is the user's `accept`; `publish` leaves only from `accepted`.
  Tier 3 has no back door — not a missing check, an absent **edge**. (That `publish` **deletes** the
  entry rather than parking it changes nothing: the guarantee was never about the state it landed in, but
  about which states the step may leave **from**.)
- **The driver's own edge is evidence-bearing and lands somewhere else.** `take-up` leaves only from
  `corroborated` (tier 2, condition 1) and lands in `self-accepted`, which is never `accepted`.

And a third, which is what makes DELETION safe (`delete-needs-a-record`):

- **No deleting edge can be reached without a durable record in the entry.** Every legal history that
  arrives at one has already written the field that names where the record lives — the PR, or the issue —
  and the accessor **refuses** the step if it does not. An entry cannot be deleted with nothing, anywhere,
  left to remember it.

### What the store is — and what it is NOT hardened against

**The store is a DRIVER-OWNED LOCAL SCRATCH FILE.** Its only writer is the campaign driver, through
`followups.py`. It is git-ignored, it is never published, and nobody else can see it — so the accessor is
**not** built to defend against a hostile or hand-edited store, and it does not pretend to be. What it
defends against is the thing a driver can get wrong on its own: **taking a step it has not earned**, which
is the graph above.

Two properties are still load-bearing, and neither is a nicety:

- **The read-modify-write is LOCKED.** Every concurrent run writes this one file, and nothing can rebuild a
  lost follow-up. An unlocked race silently drops entries, with no error and no other copy anywhere.
- **A corrupt store is REFUSED, and the refusal names the LINE it is on** — the same contract `ledger.py`
  keeps. Malformed JSON, a record that is not a JSON object, an unknown record type, an unknown state, a
  duplicate id: each is reported, never silently repaired and never **skipped**. A skipped line is a
  follow-up nothing reads.

**Every value in a follow-up is a STRING**, because `argv` is the only thing that feeds a write door and it
has nothing else to hand one. A `null` or a number on a line is refused rather than **coerced**: coercion
invents a value — `str(None)` is `"None"` and `str(123)` is `"123"`, both non-blank — and this store's whole
job is to hold claims a human can audit.

**State the limit honestly: the script cannot verify that the user really agreed.** No local file can.
`accept` is a promise the driver makes, and what the graph buys is that **skipping the user is a
DELIBERATE act rather than an oversight**. It is a footgun guard, **NOT** a security boundary.

**The user's ruling is DURABLE DATA.** `accept`/`reject` stamp when it was made, for the same reason the
ledger's `api_approval` records `approved@<iso>` rather than living in the driver's head: **a later heartbeat
is a fresh agent that never saw the conversation**, and it must not re-ask a question the user already
answered. **Nothing the driver does alone stamps it** — not an investigation, not a `take-up`, not opening
a PR, not `publish`. A ruling written by anything but the user would launder the driver's action into the
user's consent (`ruling-recorded` proves the stamp belongs to `accept`/`reject` and to nothing else).

### WHEN TO RECORD ONE — the moment it is noticed, not at the end

**A rule nobody is told to follow is not a rule.** Record a follow-up at each of these moments, before
moving on:

- **An audit finds something real that is NOT the finding** (`stage-2-review-gate.md`, "Audit every
  finding before you fix it") — a pre-existing defect at the same site, or a wider class an **ADJUSTED**
  finding only clipped the edge of.
- **A fix subagent reports a site it deliberately LEFT ALONE.** The materialized prompt's report block
  requires it (`fix-subagent-contract.md`, "Materialize the exact prompt bundle"). That report is the
  follow-up's evidence, and the orchestrator records it — **the subagent's report dies with the subagent**.
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
report and fold any answer in as its own heartbeat. **A `candidate` is a question, not a task**: the driver has
not investigated it yet, so it has nothing to act on and nothing to say. The FIRST move on a fresh
candidate is to INVESTIGATE it — the active loop above, when there is spare capacity — because that is
how the question gets answered. Surfacing it to the user is the THRESHOLD fallback (when the driver
cannot act autonomously or a user ruling is required), never a substitute for investigating it.

---
