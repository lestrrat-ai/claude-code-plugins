### 2b. CI (event-driven)

Each PR has a background task that waits on `gh pr checks --watch`. **The watch only BLOCKS — it is
never evidence.** When the task completes, a heartbeat **fetches a fresh snapshot pinned to the PR's current
`head_sha`**, verifies it, and decides `ci` **from the snapshot's contents — NEVER from the watch's exit
code** — and records the result by handing `derive`'s JSON to `ci-status.py liveness` ("THE BOOKKEEPING
IS A COMMAND", below), which writes `ci` and the liveness counters through the ledger accessor **by field
name** (`files-and-ledger.md`), never by hand-editing the row by column position. (`reviews_ok` is a
different write: its `0`-reset belongs **only** to a campaign commit landing on the PR head — "Any
campaign commit to the PR head resets the gate", below, through `scripts/ledger.py … set --pr <N>
--reviews_ok 0`. An ordinary derivation is observation, not a content change, and never touches it.)

> **Jump by question (navigation, not authority — the sections below govern):**
> - `derive` returned a verdict → "ACT ON THE VERDICT"
> - a required-check-set question → "WHAT WERE WE EXPECTING TO SEE?"
> - a PR stuck or parked → SETTLED / "ESCALATE" / "THE PARK MUST DECLARE ITS OWN EXIT" (all under "`pending` MUST NOT BE AN ABSORBING STATE")
> - a watch question → "WATCH ONLY WHAT CAN MOVE"
> - a CI-fix dispatch → "Classify, then set the model class"

#### THE DERIVATION IS A COMMAND — RUN IT. NEVER DERIVE `ci` BY READING TERMINAL OUTPUT.

**The heartbeat derives `ci` by RUNNING `scripts/ci-status.py`, and by nothing else:**

```sh
python3 <skill>/scripts/ci-status.py derive --pr <N> --head-sha <the LEDGER's head_sha> --rundir <rundir> \
  --required-set "$(python3 <skill>/scripts/ledger.py --file <rundir>/state.jsonl header get required_set)"
```

It performs every step of the spec — FETCH (SHA-pinned, paginated, **both** families), PROMOTE (atomic),
VERIFY (via `scripts/ci-snapshot.py`, which it calls), and DECIDE, all defined in
`ci-derivation-spec.md` — and prints **JSON**: the `verdict`, the `ci`
value to write to the ledger, the `reason` (**which rule fired, and which row made it fire** — this is what
`ci_reason` is built from), the evidence counts, `head_moved` + `head_sha_now`, the `required_set` state the
verdict was decided under, the `fingerprint` of the verified snapshot's evidence rows, the `buckets`
CLASSIFY tally (`PASS`/`RUNNING`/`FAIL`/`UNKNOWN_VALUE`; both `null` when the snapshot never verified —
`liveness` reduces `buckets.RUNNING` to the `watch_warranted` fact the watch policy acts on, and reads it
directly for its SETTLED/RUNNING-STALL split), and the path to the snapshot it left
behind. It exits `0` **only** on green.
**Write `ci` from that JSON; never from an impression of some command's output** — and then hand that
same JSON to `ci-status.py liveness` ("THE BOOKKEEPING IS A COMMAND", below), which records it and does
the counter arithmetic.

**`--required-set` IS MANDATORY, AND IT HAS NO DEFAULT.** It is the ledger header's `required_set`, passed
straight through (`declared:<json>` | `none` | `unknown` — "WHAT WERE WE EXPECTING TO SEE?", below). The
evidence can only ever say what **showed up**; what was **supposed** to show up is a property of the base
branch, and **a required check that never registered is NO ROW AT ALL** — invisible to the counts, to
containment, and to the rollup cross-check alike. So the tool refuses to guess it: a caller who omits the
flag gets an error, and a spec it cannot parse is **exit 2**, never a quiet `none`. `unknown` is a legal
value and **can never go green** — which is what makes a run that never performed the read merge **nothing**
rather than merge everything with a footnote.

**A MOVED HEAD FAILS CLOSED — `head_moved: true` is NEVER a green, and never a red either.** The fetch is
pinned to the `head_sha` **the ledger holds**, and a push can land at any moment — including *while the tool
is fetching*, which is why it reads the PR's current head **LAST**, after both evidence families. If that
head differs from the one it was pinned to, the snapshot is a **true report about a commit that is no longer
this PR's head** — so it is not evidence about this PR at all: `verdict = unusable`, **`ci = pending`**, and
the `reason` **names the new head** (`head_sha_now`). Green would merge a PR on checks that never ran
against its code; red would blame the new head for the old one's failure. **Do NOT read this as "checks have
not started"** — that is `verdict = pending` (zero evidence rows), and it means *wait*. This means
**re-derive**: refresh the PR's `head_sha` into the ledger (`pr-adoption.md`) and derive again, pinned to it.

**EVIDENCE THE TOOL KNOWS IS INCOMPLETE FAILS CLOSED THE SAME WAY, AND FOR THE SAME REASON.** A moved head
is one way to hold evidence that cannot answer the question; a **short read** and a **rollup entry the REST
families cannot see** are the others. The FETCH rules (`ci-derivation-spec.md`) define them — each is `verdict = unusable`,
**`ci = pending`**, **refetch**, and **no snapshot is promoted**. There is deliberately **no `notes` field
in the output**: the tool used to disclose an incomplete read *beside a green verdict*, and a caveat printed
next to the answer it contradicts is a trapdoor, not a disclosure. **Anything the tool knows is missing is a
REFUSAL now** — so `ci = green` from this command means the evidence was complete, not merely annotated.

**WHY THIS IS A COMMAND AND NOT A PROCEDURE YOU FOLLOW.** Every rule in this section was already correct,
and a driver still wrote **`ci = green`** into the ledger for a PR whose checks had **not registered** —
having run `gh pr checks <pr>`, read a line saying no checks were reported for the branch, and judged it
green **by eye**. **ZERO EVIDENCE IS NOT GREEN.** The rules did not fail; the one step that was still a
model **reading output and forming an impression** did. A program cannot get tired, cannot skim, and cannot
decide that "no checks" is close enough to "passing" — so that step is now a program. **The shell
commands the tool implements are documented in `ci-derivation-spec.md` — a SPEC, NOT a second procedure
to hand-run.**

#### WHO DOES WHAT — the background task ONLY WATCHES; the HEARTBEAT fetches. This section is the DEFINITION.

**This split is normative, and every other file defers to it** (`pr-adoption.md`, `loop-control.md`):

| Actor | Does | Does NOT |
|---|---|---|
| **The background task** | **BLOCKS on `gh pr checks <pr> --watch`, and NOTHING else.** Its **ONLY** job is to block, so that **its completion becomes a heartbeat**. | It **NEVER** fetches, **NEVER** writes `ci-<pr>-<head_sha>.txt`, and **NEVER** produces evidence of any kind. |
| **The heartbeat** | **RUNS `scripts/ci-status.py derive`** (above), which **FETCHES** (SHA-pinned, both families), **PROMOTES** atomically, **VERIFIES** the stamp, **PARSES**, and **DECIDES** `ci` — then **RUNS `scripts/ci-status.py liveness`** on that JSON, which **RECORDS** `ci` and the counters and **PARKS at any cap** ("THE BOOKKEEPING IS A COMMAND", below). | It **NEVER** derives `ci` by READING the output of `gh pr checks` — or of anything else — and **NEVER** applies the counter arithmetic by hand. |

**WHY the fetch cannot live in the background task:** the fetch must be pinned to the `head_sha` **the
LEDGER currently holds**, and **only the heartbeat knows that**. A background task that fetched at its own
completion time would pin to whatever SHA *it* saw and could **promote an artifact for a SHA the ledger has
already moved past** — the exact false-green this section exists to prevent, smuggled back in through the
producer. **A watch completion yields a HEARTBEAT, never an artifact.**

**NEVER derive CI from `gh pr checks`.** Its output **carries no SHA at all** (`--json headSha` →
*Unknown JSON field*), so you can never prove which commit it describes — right after a push it can
report the **previous** commit's passing checks, and the ledger records a **green for code the PR no
longer contains**. This produced a false green on a live run in this repo, found by dogfooding rather
than by review. Use `gh pr checks --watch` to **wait**; never to decide.


#### The derivation's internals are `ci-derivation-spec.md` — a spec, never a procedure

FETCH (SHA-pinned, paginated, both families, completion markers), PROMOTE, VERIFY (the artifact's exact
shape and every fail-closed rule), CROSS-FETCH AGREEMENT, CLASSIFY (the enums and the buckets) and DECIDE
(the outcome bullets, first match wins) live in **`ci-derivation-spec.md`** — the specification
`ci-status.py derive` and `ci-snapshot.py` implement, held to the code by `doc-check`, executed fixtures,
and the mutation harness. **Read it to review or change the tools; never to derive `ci` by hand.** The
driver acts on `derive`'s JSON alone:

#### ACT ON THE VERDICT — the driver's move for each outcome

`derive`'s `verdict` names the DECIDE outcome, and by the time you read it `liveness` has already
recorded `ci`, the counters, and any cap park. The rightmost column is the **only** driver-owned work a
verdict carries; the decision logic itself is the spec's (`ci-derivation-spec.md`, "DECIDE — first match
wins") and is never re-evaluated by hand.

| `verdict` | `ci` | The driver's move |
|---|---|---|
| `green` | `green` | Nothing here — Stage 3's merge preconditions take over. No watch. |
| `red` | `red` | **If the row is HELD (`liveness` reports it), dispatch nothing** — a held PR dispatches nothing until its question is answered (`loop-control.md` step 3, "held-status guard"); the watch still follows "WATCH ONLY WHAT CAN MOVE" below. Otherwise: **stop any review pass in flight on this PR** (the fix will replace its SHA — `loop-control.md` step 3; free the slot), **CLASSIFY the failure from the check logs** ("Classify, then set the model class", below) **before dispatching anything**, and dispatch a **scoped CI-fix subagent** into `<worktree>` — the row's ledger `worktree` value, the single source of truth for this PR's checkout path (`pr-adoption.md`). Its fix commits + pushes to the PR's **own head branch** → apply the gate reset ("Any campaign commit to the PR head resets the gate", below). Watch only while `liveness` reports `watch_warranted` (a still-RUNNING row — "WATCH ONLY WHAT CAN MOVE", below). |
| `unclassified` | `pending` | `liveness` has parked the PR (`status = awaiting-user`). **Prompt the user** per ESCALATE ("THE PARK MUST DECLARE ITS OWN EXIT", below), naming the offending value from `reason`. Never guess a bucket for it. No watch — the park is the resolution. |
| `pending` | `pending` | `liveness` reports `watch_warranted` → ensure a live watch ("WATCH ONLY WHAT CAN MOVE", below). Otherwise nothing can move — no watch; the liveness bounds own the wait and `liveness` escalates at a cap. The `pending` sub-cases that waiting can never green (`nothing registered`, `required set unreadable`, `required check missing` — each named in `reason`) resolve through those same bounds; while `required_set` is `unknown`, keep running `required-set` each heartbeat ("WHAT WERE WE EXPECTING TO SEE?", below). |
| `unusable` / `unverifiable` | `pending` | **Refetch on the next heartbeat** — the heartbeat is the backoff ("UNUSABLE — the refetch is BOUNDED", below); `liveness` counted the attempt and escalates at the REFETCH CAP. `head_moved: true` → refresh the row's `head_sha` (`pr-adoption.md`) and re-derive pinned to it. No watch. |

#### WHAT WERE WE EXPECTING TO SEE? — the required-check set

`green` above says *"every evidence row passes"*. That is only worth something if the rows are **the rows
that were supposed to be there**. A required check that has **not registered yet** is not a failing row —
it is **no row at all** — so a snapshot of the checks that *did* register can be nonempty, entirely
passing, and **still not the truth about this commit**. This section is what makes `green` mean the
required set passed. **It is not a disclosure of that hole; it is the closing of it.**

So: **read what the base branch REQUIRES, and prove every required check is PRESENT and PASSING.**

**PROBE THE DATA, NEVER THE PERMISSIONS.** Do **not** ask "may I read this?" and infer the answer —
**ask for the thing, and see whether you got it.** A permissions probe is not evidence about a token:
`GET /repos/{o}/{r}` needs only **Metadata**, and its `.permissions` reports **the USER's role, not the
TOKEN's grants** — so a fine-grained token owned by an admin reads `admin: true` while lacking
`Administration: read`. **A rule keyed on that probe declares "proven unprotected" on a branch it simply
cannot see.**

Run the required-set command before CI derivation on every heartbeat:

```sh
python3 <skill>/scripts/ci-status.py required-set --ledger <rundir>/state.jsonl [--repo <owner>/<repo>]
```

The command reads `base_branch` through `ledger.py`, URL-encodes it as an API path segment, scopes every
GitHub call to one repository, reads both declaration sources, validates every response field, unions and
sorts the declarations, validates the result through `ci-snapshot.py`'s strict parser, and writes the
canonical value through `ledger.py`'s atomic store. It exits 0 for a settled `declared:…` or `none`, 1 for
`unknown`, and 2 for a caller or ledger error. A settled value is returned without another GitHub read, so
the same command is safe to run on every heartbeat and retries only an `unknown` value.

The command owns two mandatory reads. They do **not** need the same permission: **`GET
/repos/{o}/{r}/branches/{b}` needs `Contents: read`**, while **`GET
/repos/{o}/{r}/rules/branches/{b}` needs `Metadata: read`** (GitHub REST docs, "Get a branch" / "Get rules
for a branch"). A token provisioned for only one endpoint leaves the complete set unknown.

The classic branch read uses `.protection.enabled` to distinguish disabled classic protection, then reads
`.protection.required_status_checks.checks`; `.contexts` drops app bindings and is not used. The ruleset
read uses `--paginate --slurp` because the endpoint returns a paged list and a required-status-check rule
may be on a later page. Nullable or absent app bindings become `"-"` before conversion; missing required
response fields make the entire result `unknown`.

**Read (a) is NOT paginated, and that is not an oversight** — `GET /repos/{o}/{r}/branches/{b}` returns a
**single branch object**, not a list: it takes no `page`/`per_page`, and its
`.protection.required_status_checks.checks` array arrives whole. `--paginate` belongs on **every read that
returns a LIST** and on no other; read (b) is one, read (a) is not.

**A 404 from `/branches/<b>/protection` means THREE different things** — genuinely unprotected, *you may
not look*, **or protected by a RULESET the classic endpoint cannot see**. Reproduced on **this repo** (a
dated illustration — the repo's settings are a live fact and will drift; the API behavior is the permanent
point): `.permissions.admin` reads `true`, `.protection.enabled` reads `false`, and yet
`branches/main.protected` reads `true`, because a **ruleset** protects it. **A classic 404, or a
`classic_enabled: false`, NEVER establishes "nothing is required."** That is why read (b) exists and why
it is not optional.

##### THREE states, NEVER two — "I CANNOT SEE ANY" IS NOT "THERE ARE NONE"

The command persists the outcome in the ledger header as `required_set` — **a state it cannot persist is a
state it does not have** (`files-and-ledger.md` owns the field; this block owns its meaning and format).

**WHEN: run the COMMAND every heartbeat, and let IT decide whether GitHub is read** — that split is the
whole policy, and it is why the two sentences that follow do not conflict. The GitHub **read** happens once
per run, before the first CI derivation — the set is a property of `base_branch`, which is itself set once
(`files-and-ledger.md`, "Base branch"), so one read serves every PR in the run — **and is retried, every
heartbeat, for as long as the value is `unknown`**: that is the whole of its retry policy, and it is what
keeps a transient failure (a network blip, a rate limit) from parking PRs that were never really blocked —
a read that recovers before the STRIKE CAP costs the run nothing at all. **Once it is `declared:…` or
`none`, it is SETTLED — the command returns the ledger's value without touching GitHub**, and never
overwrites a successful read with a later failure. So there is no "should I run it this time?" question:
always run it; a settled value makes it a cheap local no-op.

| State | `required_set` | When | What `green` may claim |
|---|---|---|---|
| **DECLARED** | `declared:<json>` | **BOTH** reads succeeded **and** their union is non-empty | every declared check is **present AND passing** — the required set passed |
| **NONE DECLARED** | `none` | **BOTH** reads succeeded **and** their union is empty | the base branch **requires nothing**, so nothing required can be missing — **the required set passed, vacuously and completely** |
| **CANNOT READ** | `unknown` | **EITHER** read errored, **or** the field it needed was **absent** from the response | **NOTHING. `green` is UNREACHABLE** — the PR goes `pending (required set unreadable)` and escalates. |

**`unknown` CANNOT GO GREEN, and that is the entire point of separating it from `none`.** A `green`
printed next to *"…but a required check may exist, be missing, and campaign cannot tell"* is **not a
disclosure, it is a trapdoor with a sign on it**: the merge still happens, and the sign is read by nobody.
The three states are not three flavours of green — they are **two that can prove the claim and one that
must not make it**. That is also what makes `unknown` the **safe default** (`ledger.py`): a run that never
performed the read merges **nothing**, rather than merging everything with a footnote.

**`DECLARED` requires that BOTH reads SUCCEEDED.** If one read was denied and the other returned a
non-empty set, you know **SOME** required checks — not that you know them **ALL**. That is **CANNOT
READ** (`unknown`). A **permissive** endpoint answering never vouches for a **restricted** one erroring.

**`declared:` carries a JSON array, NOT a comma-separated list.** Each element is
`{"context": "<name>", "app": "<app_id>" | "-"}`, where `<app_id>` is the app id **as decimal digits** and
`"-"` means the declaration **binds no app** (any producer satisfies it). **Those two are the ONLY shapes
an `app` may take**: `ci-snapshot.py --required-set` rejects every other one **loudly** — above all the
string `"null"`, which is what a `//` default written **after** a `tostring` yields from a null binding
(`ci-derivation-spec.md`, FETCH, owns that rule). Bound to an app that **does not exist**, a check can never be matched by any
row, so **accepting it would WEDGE the PR** — and **normalising it to `"-"` would be a GUESS** about a
value we could not read. The payload is JSON because **a required check's name may CONTAIN A COMMA** — a
matrix job's name is `job (a, b)`, and 40 of the 100 check runs on `vercel/next.js`'s default-branch head
carried one when this was written (a dated illustration; the **claim** — commas are legal and common in
check names — is the permanent point). A comma-separated list of those names is **ambiguous at the
separator**, and a required set you cannot parse back is a required set you do not have.
`ci-snapshot.py --required-set` is the **one parser**; it rejects anything malformed **loudly** and
**NEVER degrades a value it cannot read into `none`** — quietly reading a broken spec as "nothing is
required" would rebuild the exact false green this section removes.

**MATCH ON PRODUCER, not just on name — a right-named check from the WRONG app is not the required one.**
A declared check is **satisfied** only by an evidence row that carries its `context` **and** its producer:

- a `checkrun` row whose `name` equals the context, and whose `app_id` equals the declared `app`
  (any `app_id` satisfies a declaration whose `app` is `"-"`). **`"-"` is a wildcard on the
  DECLARATION and NOT on the ROW**: on a declaration it means *any producer may satisfy this*, while
  on a row it means *this run came from no app* — so a producer-less row satisfies an **unbound**
  declaration and **never** an app-bound one, for the same reason a `status` row cannot;
- a `status` row whose `context` equals it — **and only where the declaration binds NO app.** The
  commit-status rows carry **no producer field at all** (`ci-derivation-spec.md`, FETCH: the status response has none to
  give), so an app-bound declaration **cannot be proven** by one. It stays unsatisfied → `pending
  (required check missing)` → SETTLED escalates it, naming the check. **That is fail-closed on purpose**:
  the alternative is to accept a status from an app we never identified as proof of a check that named
  one, which is the false green with an extra step.

#### `pending` MUST NOT BE AN ABSORBING STATE — SETTLED, then ESCALATE

**THE INVARIANT — every non-green state MUST declare (a) what event would leave it, and (b) what happens
if that event NEVER COMES. A rule with no answer to (b) is FORBIDDEN. Apply this to every rule you add
to this file, before you write it down.**

The failure this prevents is subtle and it has already happened: hardening a rule set against a false
*green* — one "→ pending, NEVER green" clause at a time, each one correct on its own — produces a
machine that in common configurations can **never go green at all**. Many rules enter `pending`; nothing
leaves it except CI itself changing; and the 1-hour task cap **was disabled for the whole of `pending`** —
its exemption was keyed to the **`ci` VALUE**, so it exempted a wait that **nothing was counting down**. So
the PR sat there forever and **no one was ever told**. A wedge is not safer than a false green — it is just
a failure that never files a report. (That cap's exemption is now keyed to a **LIVE liveness bound**, never
to `ci` — `bailout-and-final-report.md`. The rest of this section is what makes such a bound exist.)

The missing concept is **"CI has STOPPED MOVING and the rule is STILL unsatisfied."** `ci = pending`
cannot express it, because `pending` conflates *still running* with *stuck*.

**The FINGERPRINT COMES OUT OF `derive` — the `fingerprint` field of its JSON. NEVER hash by hand.** It
is computed (`ci-snapshot.py`'s `fingerprint()`) over the VERIFIED snapshot's EVIDENCE ROWS — the JSONL
the FETCH (`ci-derivation-spec.md`) emits, nothing else — and it exists for the same reason the derivation is a command: a
hash a driver reassembles from this spec is a hash that drifts, and **every drifted byte reads as "CI
moved"**, which resets the very counters the fingerprint feeds. The block below is the **SPEC the tool
implements** (`doc-check` holds its line formats to the code; `ci-status-test.py`'s `[fp]` cases pin the
exact bytes): each `checkrun` and `status` row becomes one canonical line, the lines are sorted bytewise,
the ledger's `head_sha` is prefixed, and the whole is hashed:

```
checkrun  ->  "checkrun\t<name>\t<app_id>\t<status>\t<conclusion>"
status    ->  "status\t<context>\t<state>"

fingerprint = sha256( head_sha + "\n" + <those lines, sorted bytewise, one per line> )
```

- **Only the VERDICT-BEARING fields go in.** These are exactly the fields CLASSIFY reads (`.status` +
  `.conclusion` on a `checkrun`, `.state` on a `status`) plus the identity that says **which** row they
  belong to. A fingerprint built from anything CLASSIFY does not read would call a PR "moving" when
  nothing that decides `ci` had changed.
- **`header`, `source` and `witness` rows are EXCLUDED** — they carry **no verdict** (they are never
  classified — CLASSIFY, `ci-derivation-spec.md`), so they can never be the thing that is or is not moving. A `source`
  marker's `count` is a **restatement** of the evidence rows it counts (VERIFY, `ci-derivation-spec.md`, enforces exactly that), so
  including it would add nothing and could only double-count.
- **The row's `sha` is EXCLUDED** — VERIFY (`ci-derivation-spec.md`) has already proved every evidence row's `sha` equals the
  ledger's `head_sha`, and `head_sha` is hashed in **once**, at the front. **The `id`/`details_url` is
  EXCLUDED too**: it is a cross-source **identity** for containment, not a verdict, and a re-run that
  produces the same result under a new job id is **not** CI moving toward green.
- **IDENTICAL lines are KEPT, and every line — the last included — ends with `\n`, in UTF-8.** Two matrix
  legs at the same verdict are two identical lines, and a third leg arriving at that same verdict **IS**
  motion — dedup would erase it and let the stall clock run through a check set that is visibly changing.
- **A snapshot that is not VERIFIED has NO fingerprint** — `derive` prints `fingerprint: null` for it.
  `UNUSABLE` never yields one — its rows were
  never trusted, so **nothing rejected is ever hashed** and an `UNUSABLE` derivation **NEVER touches
  `settled_strikes`**: a strike is a claim that *trusted* evidence did not move, and `UNUSABLE` is the
  **absence** of trusted evidence — the two cannot be counted on the same dial. It gets its **own**
  persisted counter and its **own** bound: "UNUSABLE — the refetch is BOUNDED" below.

```
SETTLED        ==  NO evidence row classifies RUNNING     # nothing left that could move on its own
               AND fingerprint == ledger.ci_fingerprint   # and it did not move since the last derivation

RUNNING-STALL  ==  ≥1 evidence row classifies RUNNING     # something still CLAIMS it can move
               AND fingerprint == ledger.ci_fingerprint   # but NOTHING in the check set actually moved
```

**These two are EXHAUSTIVE over an unchanged fingerprint, and that is the whole point of stating the
second one.** A derivation whose `fp` equals the ledger's is **either** SETTLED **or** RUNNING-STALL —
never neither. `SETTLED` alone left a hole exactly the size of the wedge this section exists to close: a
check that **registers and then runs forever** (a hung runner, a job whose reporter died, a required check
that queues and never starts) keeps a `RUNNING` row forever, so it is **never SETTLED**, never accrues a
strike, and never escalates — `pending` is **absorbing** for it, and half (b) of the invariant above is
**unanswered**. `RUNNING-STALL` is the rule that answers it. It is bounded in **TIME** — see "RUNNING-STALL
— a row that never finishes is bounded in TIME" below, which is where the bound and its rationale live.

`RUNNING` in both definitions is **the CLASSIFY bucket (`ci-derivation-spec.md`), verbatim**, and `derive` emits its tally:
**`buckets.RUNNING > 0` is the whole test** — never a hand classification of snapshot rows. Do **not**
re-derive it from `ci` either: `red` outranks `pending` in DECIDE, so a snapshot recorded `red` can still
hold a **RUNNING** row, and that PR is still moving.

##### THE BOOKKEEPING IS A COMMAND — RUN IT. NEVER APPLY THE DERIVATION BLOCK BY HAND.

```sh
python3 <skill>/scripts/ci-status.py liveness --ledger <rundir>/state.jsonl --pr <N> \
  --derive-json <the JSON derive printed, saved to a file — or - for stdin> \
  --machine-action <due | in-flight | none>
```

It applies **every line of the block below** to the PR's row and writes the result through the ledger
accessor in **ONE atomic row update**: `ci`, the fingerprint comparison, the strike, the stall clock, the
refetch counter, and — at any cap — **the ESCALATE park itself** (`status = awaiting-user`, `ci_reason`
naming the blocker, `blocker_ruling = -`, one write). It exits `0` when it recorded and nothing parked,
**`3` when it parked the PR on the user** (tell them — that half of ESCALATE is still yours), `2` when
its input was refused. **`--machine-action` is the ONE judgment it asks of the caller** — *"is work that
can move this PR's `head_sha` due or in flight?"* (the MACHINE ACTION property, below); everything
downstream of that answer is arithmetic, and hand-run arithmetic is what this command exists to remove.
The same reason the derivation is a command: a driver that reassembles the counters from this spec
reassembles them slightly differently every time, and every difference silently resets a bound.

Two rules the tool enforces that the block's lines cannot show:

- **A STALE derivation is REFUSED, never recorded** — `derive` pinned to a `head_sha` the row no longer
  holds exits `2` and writes **nothing**: the site that moved the head already reset the counters
  ("THE LIVENESS COUNTERS", below), and recording the old head's evidence would spend the new head's
  budget. Re-derive against the ledger's head.
- **A HELD row is OBSERVED, never struck** — `ci` is still recorded (observation is not mutation,
  `ledger.py`'s HELD_STATUSES), but on a held row the bounds neither accrue nor fire: a parked row's
  `ci_reason` is the **open question** a human is being asked, and no second park may overwrite it.

Per derivation **on a VERIFIED snapshot** — `fp` below is **the `fingerprint` field of `derive`'s JSON**
(an `UNUSABLE` one prints `fingerprint: null`, has no `fp` at all, is handled entirely by "UNUSABLE — the
refetch is BOUNDED" below, and touches **no liveness counter but its own**) — in this order:

```
fp != ci_fingerprint      -> ci_fingerprint = fp ; settled_strikes = 0 ; ci_stalled_since = -
                                                                          # still MOVING — be patient
# --- Everything below this line runs ONLY when fp == ci_fingerprint: NOTHING in the check set moved. ---
MACHINE ACTION due or in flight
  for this PR at this head_sha      -> ci_stalled_since = - ; STOP        # a new head IS coming: neither
                                                                          # bound applies this derivation
                                                                          # (settled_strikes: unchanged).
                                                                          # STOP = evaluate NOTHING below.
SETTLED and ci in {red, pending}    -> settled_strikes += 1                # nobody is coming — count it
settled_strikes >= 2                -> ESCALATE                           # 2 == THE STRIKE CAP. This line
                                                                          # is its ONE defining site; every
                                                                          # other rule says "the STRIKE CAP".
RUNNING-STALL and ci_stalled_since == "-"
                                    -> ci_stalled_since = <now, UTC ISO-8601>   # start the clock
RUNNING-STALL and now - ci_stalled_since >= the CI STALL CAP
                                    -> ESCALATE                           # not SLOW — DEAD. The CI STALL
                                                                          # CAP's value is defined in
                                                                          # "RUNNING-STALL" below.
```

**`STOP` on the MACHINE ACTION line is load-bearing, and it is the only early exit here.** Without it a
reader falls through to the strike rule and counts a strike against the very PR a fix is about to move —
the failure the gate exists to prevent, reintroduced by reading the gate as a note instead of a branch.
The remaining lines are evaluated **in order and all of them**: SETTLED and RUNNING-STALL are mutually
exclusive (above), so at most one bound can fire on any derivation.

**The MACHINE ACTION gate is hoisted, and it now gates BOTH bounds** — it used to sit inside the strike
rule alone. Same fact, same definition (below), one site: a PR the driver is actively repairing is neither
struck **nor** timed, because a fix that pushes will replace this `head_sha` and every row on it. **`ci in
{red, pending}` is an explicit membership test, not `ci != green`** — `ci` is `green`/`red`/`pending` and
nothing else (`files-and-ledger.md`), and a negated test over an enum is the catch-all-in-disguise this
file kills on sight (CLASSIFY, `ci-derivation-spec.md`).

**A STRIKE — AND A STALL CLOCK — MEAN "NOBODY IS COMING". NEVER PARK A PR THE DRIVER IS ALREADY
REPAIRING.** `SETTLED` and `RUNNING-STALL` are about **CI**, not about **campaign**: a **red** PR whose
rows are all terminal is SETTLED on the **first** derivation, and a CI-fix subagent in flight does **not**
change `head_sha` until it pushes — so an ungated strike rule would park, within the **STRIKE CAP**'s worth
of heartbeats, the exact PR the driver is actively fixing, and an ungated stall clock would start timing a
`RUNNING` row the driver is about to make irrelevant.

##### MACHINE ACTION — any work that can produce a new `head_sha`

**MACHINE ACTION** = any work campaign dispatches that can produce a new `head_sha` on this PR. That
PROPERTY is the definition, and it is the whole of it. **APPLY THE PROPERTY — NEVER CONSULT A LIST.**
Of any work in question, ask: *when it completes, can it put a new commit on this PR's head?* If yes it is
a MACHINE ACTION, whether or not it appears in any enumeration anywhere in this repo — the same idiom
as the parked-PR guard's "does this MUTATE the PR?, not is it on a list" (`SKILL.md`). A set defined
by a property but applied through its examples is a set that silently shrinks every time a member is
added somewhere else — which is exactly how the list below went stale once already.

- **NON-NORMATIVE EXAMPLES. Illustrative only; they DO NOT BOUND THE SET:** a CI-fix subagent (either
  tier, including an escalation from the cheap one), a review-fix subagent, a copilot-address fix, a
  refutation commit, and every rebase and base refresh — the conflict-resolving rebase and the CLEAN
  BASE-ONLY one alike (Stage 2a's precondition rebase, `stage-2-review-gate.md`; the post-merge
  reconcile, `stage-3-merge.md` step 6). Work that has the property but is missing from this list is
  still a machine action.
- **The CLEAN BASE-ONLY REBASE IS ONE — it is the member this list omitted, and the omission parked
  healthy PRs.** It qualifies for exactly the reason every other member does: it MOVES `head_sha`
  ("THE LIVENESS COUNTERS" below, which resets the counters at it for that same reason). Whether it also
  resets the gate is a DIFFERENT QUESTION — it does not — and it is not this one: the property
  here is `head_sha`, not `reviews_ok`. A PR merely DUE for a clean rebase would otherwise keep
  striking (or keep its stall clock running) against a head the rebase is about to replace, and park —
  a spurious park, with the work already on its way.
- **The CI watch is NOT a machine action, and neither is a review pass.** They fail the property: neither
  pushes a commit, so neither can move CI. A settled PR under review is still settled, a stalled one is
  still stalled, and suppressing either bound for them would wedge the PR exactly as before.

**In flight** = dispatched for this PR at this `head_sha` and not yet completed — the same in-flight
test `loop-control.md` step 3 already applies to suppress a duplicate dispatch ("CI red and no fix is
already in flight for that PR/SHA"). The strike rule and the stall clock read that same fact; they do not
invent a second one.

**Due** = this heartbeat would launch it — it is not in flight, and nothing but a free concurrency slot is
missing. That, too, is a property, not a fixed list of scans: whichever rule OWNS that dispatch is the
one that says so. For a CI fix it is `loop-control.md` step 3's dispatch scan; for a rebase it is the rule
that owns the rebase (Stage 2a's preconditions, `stage-2-review-gate.md`; the step-6 reconcile,
`stage-3-merge.md`) finding the PR behind/conflicting on this heartbeat. A PR frozen by a park has no
machine action due — the held-status guard forbids every one of them (`loop-control.md`), so nothing is
coming, which is why the park is the terminus and not another wait.

**IT STILL TERMINATES — a liveness rule that can be suppressed forever is not a liveness rule.** The STOP
is keyed to a machine action being DUE or IN FLIGHT, and every machine action ENDS, so the STOP
ends with it — the broader definition above does not widen it into a wedge:

- **A fix subagent is bounded**: a stuck task is retried once and then aborted permanently, and "CI
  fails identically after a fix attempt" is itself a stop condition (`bailout-and-final-report.md`).
- **A rebase is bounded by construction**: it either lands a new head — which resets the counters at the
  site that moved it, the correct outcome, not a suppression — or it fails; either way it is afterwards
  neither due nor in flight.
- **DUE cannot persist**: the only thing a due action waits on is a free concurrency slot, and slots
  free as work completes.

So a PR whose machine actions are all exhausted — a red PR whose CI-fix budget is spent, not
behind or conflicting, so no rebase is owed — has none due and none in flight: the STOP does not fire,
strikes accrue (or the stall clock runs) on the next derivations, and it reaches its cap and parks,
like any other settled or stalled PR. The gate suppresses a bound only while work that can move
`head_sha` is actually coming, never merely because the PR is red.

##### ESCALATE — park the PR and tell the user

**ESCALATE** = park the PR (`status = awaiting-user`, `ci_reason` = the blocker **named**: which check
never registered, **which check has been `RUNNING` since when without the check set moving**, which value
was unrecognized, which read was denied), **and `blocker_ruling` = `-` in
that same row write** ("THE RULING IS CONSUMED EXACTLY ONCE" below — a ruling already on the
row answers the **previous** park, never this one), and tell the user. **The three-field row write is a
COMMAND, split by who owns the park: for the three liveness bounds `ci-status.py liveness` performs it
itself** (exit `3` — "THE BOOKKEEPING IS A COMMAND", above); **every OTHER machine-blocker park runs
`ledger.py … park --pr <N> --reason <the blocker>`**, which writes `status = awaiting-user`, `ci_reason`,
and `blocker_ruling = -` in one atomic write (`files-and-ledger.md`, "Editing the ledger" — it refuses a
blank reason, a terminal row, and a second park over an open question). The non-CI sites are
`stage-3-merge.md`'s merge-precondition park. **NEVER hand-assemble the three fields through `set`** — a
park missing its `ci_reason` is a question nobody can read. Telling the user is the half that stays with
the driver. It does **not**
abort the run or close the PR — the run's other PRs keep going. At **this** park — a CI one — `ci_reason` is
**the DECIDE reason for this snapshot**: the bullet that matched and the row that made it match, never a
bare restatement of `ci`. (The field itself is **wider than CI**: it is the durable machine-blocker reason,
and `stage-3-merge.md`'s merge-precondition parks write it with `ci` **green**. `files-and-ledger.md` owns
that definition.) A park that cannot name its blocker is not actionable.

##### THE PARK MUST DECLARE ITS OWN EXIT

**THE PARK MUST DECLARE ITS OWN EXIT — the invariant at the top of this section binds `awaiting-user`
too.** A park whose exit event never comes is the same wedge, one level up. So the escalation:

- **PROMPTS with the blocker and what campaign already spent on it** — the PR, its `ci_reason`, the
  evidence (the fingerprint that did not change; **how long it has not changed** (`ci_stalled_since` →
  now) when a `RUNNING` row is what stalled; the row, value, or VERIFY rule that made the bullet match),
  and what was already tried (CI-fix attempts, strikes, refetches). **Never a bare "CI is stuck".**
- **Asks for exactly ONE of two answers, and names them:**
  - **`retry`** — "I changed something **outside** the PR (re-ran the workflow, registered the missing
    check, fixed the repo/branch setting); derive again."
  - **`abort`** — "stop work on this PR" (terminal `aborted`, PR left open — `bailout-and-final-report.md`).
- **Records that answer DURABLY in the ledger's `blocker_ruling`** (`files-and-ledger.md`) the moment it
  lands, and unparks per `loop-control.md` step 3, "Only the user's answer unparks a PR" — which also
  **clears the liveness counters**, so the retry gets a fresh budget instead of re-escalating on its first
  derivation. A heartbeat may be a fresh agent instance, so the answer must be durable ("HOW state dies
  with the context is a CLASS" below).

**NOTHING THIS SECTION RELIES ON LIVES IN THE DRIVER'S HEAD — and that is a PROPERTY, not the list that
used to stand here.** A heartbeat may be a fresh agent instance, so: **if a LATER derivation, the escalation
prompt, or the unpark has to read it, it is a LEDGER FIELD.** The durable set is therefore **the ledger row
schema itself** — `files-and-ledger.md`'s row-field definitions, and the `ROW_FIELDS`/`ROW_DEFAULTS` in
`scripts/ledger.py` that own it — and a field added there is durable **with no edit to this section**. A
list retyped here rots the next time one is added, and the one that stood here rotted **twice**: it first
dropped `ci_reason`, the very thing the park asks the user about, and its replacement dropped
`ci_fingerprint`, whose loss silently reopens the wedge ("HOW state dies with the context is a CLASS" below).
There is no third attempt: **the members are not retyped here, in any form, marked or not.** Every write
goes through the owning tool, **by field name**, never by hand-editing the row: the derivation-driven
fields are `ci-status.py liveness`'s ("THE BOOKKEEPING IS A COMMAND", above), and everything else —
the unpark, the reset sites, the non-CI parks — is `scripts/ledger.py … set --pr <N>`
(`files-and-ledger.md`).

**HOW state dies with the context is a CLASS, and it is stated as one — never per field**, so a field
added to the schema tomorrow is already covered here:

- **A COUNTER that dies never reaches its cap.** A fresh instance restarts the count from zero, so the
  bound never fires.
- **A CLOCK is worse**: it does not merely lose the elapsed time, it silently restarts it. Every clock
  is therefore a timestamp on disk, so that any heartbeat computes the elapsed time from the ledger
  alone, remembering nothing.
- **EVIDENCE OF WHAT CI LOOKED LIKE LAST TIME is worse still, because losing it looks like SUCCESS.** The
  derivation decides that CI moved by comparing this snapshot against what the row says it saw before;
  with that gone, every heartbeat sees motion, every heartbeat resets the counters and the clock, and the
  bounds never fire — the wedge this whole section exists to close, reopened silently and with no error.
- **A REASON that dies leaves the park UNANSWERABLE.** The escalation prompt above is built from the
  blocker the human is being asked to rule on; a fresh agent that lost it cannot even ask the question, so
  the park has no exit.
- **A RULING that dies gets re-asked** ("THE RULING IS CONSUMED EXACTLY ONCE" below).

##### THE RULING IS CONSUMED EXACTLY ONCE

**THE RULING IS CONSUMED EXACTLY ONCE — a durable answer that is never spent is a park that unparks
itself.** `blocker_ruling` must be **DURABLE** (it survives a context loss) **AND spent EXACTLY ONCE** (it
answers the park it was written for, and no other). Both halves, or neither holds:

- **ENTERING a machine-blocker park sets `blocker_ruling` = `-`** (ESCALATE above), in the **same** write
  that sets `status = awaiting-user` — `ledger.py … park` for a non-CI park, `ci-status.py liveness` for a
  CI one — one atomic row write, so no heartbeat can ever observe a freshly parked row still carrying the
  previous park's answer.
- **CONSUMING a `retry` sets it back to `-`** in the **same** `ledger.py … unpark --pr <N>` call that
  unparks the PR (`loop-control.md` step 3): the tool spends the ruling and resets the liveness counters in
  one write. It refuses a `blocker_ruling` that is not a well-formed `retry@<iso>` — a bare `retry` or an
  `abort` never reaches this consume path.
- **`abort@<iso>` is NEVER cleared.** It unparks into terminal `aborted`; a terminal row is never re-parked
  and never re-consulted, so the ruling stays as the durable record of **why** the PR stopped
  (`bailout-and-final-report.md`).

**This is what SCOPES the ruling to its park**, and it is why the clear at park **entry** is the
load-bearing one: a ruling present on a parked row can only have been written **while THAT park was
open**. The consume-clear alone would not hold — a crash between the unpark and the next park would leave
a spent `retry` on the row, and the next park would read it as its own answer. (A ruling keyed to
`head_sha` instead would **not** work: a `retry` that fails to move CI re-parks the PR at the **same**
`head_sha` (`loop-control.md` step 3), so a `head_sha`-scoped ruling would satisfy that re-park with no
fresh user answer — the exact failure this rule exists to prevent.)

##### THE LIVENESS COUNTERS

**THE LIVENESS COUNTERS — one name for the set, so a new counter never leaves a stale restatement.** They
are `ci_fingerprint`, `settled_strikes`, `unusable_refetches`, and `ci_stalled_since`.

**This set is also the ENUMERATION OF THE BOUNDED CI WAITS — every way a non-green `ci` can be waiting,
and what ends each.** It is the ONE place that enumeration lives: any rule that must reason about "is
this wait bounded, and by what?" (`bailout-and-final-report.md`'s 1-hour task cap is the one that does)
reads **this set**, and a counter added here is inherited by it with **no edit to it**. Each bound's
VALUE lives at its own single defining site, named here and never retyped:

| Counter | Bounds the wait where… | Its cap | Cap's ONE defining site |
|---|---|---|---|
| `ci_fingerprint` | CI is genuinely **MOVING** (the fingerprint CHANGED since the last derivation) | *none — motion is not a wait* | — |
| `settled_strikes` | CI has **SETTLED** and is still not green | **the STRIKE CAP** | "SETTLED", the derivation block above |
| `ci_stalled_since` | a row still says **RUNNING** but nothing in the check set moves | **the CI STALL CAP** | "RUNNING-STALL", below |
| `unusable_refetches` | the snapshot never **VERIFIES** | **the REFETCH CAP** | "UNUSABLE — the refetch is BOUNDED", below |

Each ends by itself — in a bounded number of derivations, or a bounded amount of time — and each ends in
the **same** place: **ESCALATE** (above), the park a human answers. That is what makes every one of them a
BOUNDED wait rather than a wedge.

**Reset them TOGETHER — EVERY member of the set, each back to its `ROW_DEFAULTS` value** (`scripts/ledger.py`
owns those defaults; **never retype them here**, or the reset rots the next time the set gains a member) — at
exactly two kinds of site, and the two RESET THEMSELVES DIFFERENTLY:

1. **Any `head_sha` change — whether or not the gate resets with it — and THE ACCESSOR DOES IT FOR YOU.**
   The new head is new evidence, so the old head's liveness says nothing about it. **THE TRIGGER IS THE
   PROPERTY — "this site wrote a new `head_sha`" — and NOT membership of a list.** This reset is enforced
   **at the write door, not by prose at each site**: `scripts/ledger.py … set --head-sha <new>` (and
   `pr-adopt`, which routes through it) resets every member of the set to its default in the SAME row write,
   on any genuine head move — ledger.py's `apply_head_sha`. **So a site that writes a new `head_sha` through
   the accessor NEED NOT and MUST NOT hand-reset the counters**: write the new head through the accessor and
   the door resets them. A `--settled-strikes 0` typed beside the head write is redundant, and a hand-reset
   that lists the members is exactly the stale restatement this door removes. Every site that writes a head
   through the accessor gets the reset, including one added after this paragraph, with **no edit here**.
   Today's sites, **illustratively and NON-EXHAUSTIVELY**: **"Any campaign commit to the PR head resets the
   gate"** (below — CI-fix, review-fix, copilot-item fix, refutation commit); **Stage 2a's precondition
   rebase** (`stage-2-review-gate.md`) and **`stage-3-merge.md`, step 6's reconcile** — for each, BOTH
   branches: a clean base-only rebase, which does **not** reset the gate, and a conflict-resolving rebase,
   which does; **`loop-control.md` step 1's ledger refresh** and **`pr-adoption.md` step 3's row refresh** (a
   formatter/bot commit, a manual push, any content change this run did not dispatch). Each writes the new
   `head_sha` through the accessor, so each gets the reset from the door — none hand-resets.
2. **An unpark by `retry`** (`loop-control.md` step 3) — no new head, but the user changed something
   **outside** the PR (they re-ran the workflow, killed the hung runner, registered the missing check), so
   the strikes and the stall clock that measured the old attempt are void. **This reset stays EXPLICIT** —
   there is NO `head_sha` change for the door to fire on, so the unpark writes the counters back to their
   defaults itself, in the same `ledger.py … set` call that spends the ruling.

**`liveness`'s stale-derivation refusal ("THE BOOKKEEPING IS A COMMAND", above) is NOT a substitute for
the head write doing it — it is the seam that makes that reset SAFE.** The sites above **write the new
`head_sha` to the row** through the accessor, so by the time CI is re-derived the ledger already reads the
new head, the counters are already reset, and there is nothing left to detect. The reset belongs **with the
`head_sha` write itself** — the accessor performs it when the site moves the head; what `liveness` adds is
the other half: a derivation still pinned to the OLD head is refused outright, so stale evidence can never
spend the budget the head write just reset.

**A gate reset is NOT the trigger — a `head_sha` change is.** The two are not the same set and never map
onto each other: a `NOT SATISFIED` verdict resets the gate with **no** new head (the counters stay — CI
did not move), and a clean base-only rebase moves the head **without** resetting the gate (the counters
reset — CI must be re-derived). Anything that reads "reset on every gate reset" is wrong in both
directions.

Any rule that says "reset the liveness counters" means **the set NAMED above** — never a number, and never
a list retyped somewhere else. That is what the name is **for**: a counter added to the set (as
`ci_stalled_since` was) is inherited by **every** one of those sites with **no edit to them**, and the
sites are unchanged by its addition. `ci_stalled_since` is a **clock, not a tally**, and it belongs to the
set anyway — it measures the same thing the strikes do (**how much budget this head has spent without CI
moving**), and it is void for exactly the same reasons, at exactly the same sites. `ci_reason` is a
**record, not a counter** — it is overwritten by the next derivation, never blanket-reset. `blocker_ruling`
is a record too, but it is **not** free-floating: it is cleared at its own park boundaries ("THE RULING IS
CONSUMED EXACTLY ONCE" above), never by a counter reset.

#### RUNNING-STALL — a row that never finishes is bounded in TIME: `ci_stalled_since`, the CI STALL CAP

`RUNNING-STALL` (defined above) is the other half of `SETTLED`, and it is the harder half, because the
rule must tell **NOT MOVING** apart from **SLOW** — and on a fingerprint they look **identical**. A
legitimately slow build (a 40-minute integration suite) has an **unchanged fingerprint across derivations**
in exactly the way a hung runner does. **So a naive "fingerprint unchanged for K derivations → park" parks
the healthy build**, and a rule that parks healthy PRs gets turned off, which leaves the wedge.

**THE BOUND IS A DURATION, NOT A DERIVATION COUNT. This is a deliberate choice and it is the crux.**

- **A derivation count measures the RUN'S LOAD, not this PR's CI.** Derivations are driven by **heartbeats**,
  and a heartbeat is the fallback lifecycle (**a ~5–15 min scheduled heartbeat or bounded wait returning**,
  `loop-control.md` step 5) **or any background task, on ANY PR, completing**. So on a busy run three
  derivations can land within seconds of one another — a
  derivation bound would park a 40-minute build that had barely started, for no reason but that **other**
  PRs were finishing work. On a quiet one-PR run the same bound is worth an hour or more. **The same
  number means a different amount of waiting on every run**, and none of it is a property of the check
  that is or is not moving. `unusable_refetches` can be a count precisely because it counts **its own
  attempts**; nothing here is counting attempts.
- **SLOW and DEAD differ in exactly one observable: how long the check set has gone without ANY motion.**
  So that is what the bound measures. `settled_strikes` gets away with counting derivations only because a
  SETTLED PR has **nothing that could move** — there is no slow-vs-dead question to answer.
- **It is computed FROM DISK, never from the driver's memory of when it last looked.** `ci_stalled_since`
  is a UTC ISO-8601 timestamp in the **ledger**; `now - ci_stalled_since` is a subtraction any fresh agent
  instance can do on its first heartbeat (why a clock lives on disk, not in context: "HOW state dies with
  the context is a CLASS" above).

**WHY IT DOES NOT PARK A HEALTHY SLOW CHECK.** The clock is **not** "how long the build has been running".
It is **"how long NOT ONE row in the whole check set has changed state"** — the fingerprint covers every
evidence row's verdict fields, so **any** motion **anywhere** resets it: a queued job starting
(`QUEUED`→`IN_PROGRESS`), any other check completing, a matrix leg finishing, a new check registering (a
new row), the slow suite itself finally concluding. A 40-minute suite therefore has to be the **only**
thing in the check set, and emit **no** transition, for the **whole CI STALL CAP** before it is timed out.
The cap (defined below) is set **far above** the longest stretch a legitimately slow check goes without
emitting *any* state transition, and a 40-minute suite is nowhere near it — a healthy check does not go
silent for hours. The clock also starts at the **first derivation that observes no motion**, not at the
moment the check began, so it errs **later** still.

**THE HONEST LIMIT — the cap is a DECLARED CONSTANT, not a proof.** A **GitHub-hosted** job cannot sit
`IN_PROGRESS` past **6 hours**: that is **GITHUB'S documented per-job execution limit — an EXTERNAL API
constant, NOT this cap** (see the note under the cap's definition below), and when it trips GitHub
**terminates the job**, which writes a `COMPLETED` row — motion, and therefore a reset, not a park.
A **self-hosted** runner or an **external** CI posting commit statuses (Jenkins, CircleCI) has **no such
limit**, so a repo *can* have a check that legitimately sits unchanged past the cap. If one does, this
rule **parks it** — and that is survivable **only** because ESCALATE is a **park that ASKS THE USER**
(`retry` / `abort`), never an abort: a false park costs **one prompt**, and `retry` resumes with a fresh
budget. A wedge costs the run, silently, forever. **NEVER claim the cap proves the check is dead** — it
proves campaign has waited the **full CI STALL CAP** for a check set that did not move once, and is now
telling a human instead of waiting forever.

**THE CI STALL CAP = 6h. This line is its ONE defining site.** A repo whose legitimate checks can outlast
it raises it **here**, and every rule that reads "the CI STALL CAP" follows. **Never restate the number
elsewhere** — refer to the cap **by name**.

> **The `6 hours` in "THE HONEST LIMIT" above is NOT a second copy of this cap.** It is **GitHub's**
> per-job execution limit — an external API constant, which this repo's rules require to be kept
> **exact** and never swapped for a symbolic reference. That it happens to equal the CI STALL CAP **today
> is a coincidence**: change the cap to 4h and GitHub's limit still reads **6 hours**. Do not "unify"
> them, and do not read that literal as a restatement to be swept.

**Where the heartbeat comes from while a stalled row is watched.** A hung `RUNNING` row keeps `gh pr checks
--watch` **blocked forever**, so the watch never completes and never wakes anyone — the escalation is
therefore evaluated by the fallback lifecycle like any other derivation. A scheduled-heartbeat host uses a
heartbeat; a scheduler-less host keeps the invocation alive and loops after each bounded wait
(`loop-control.md` step 5). **A bound that could only be reached by the event it is waiting for would not
be a bound at all.**

#### UNUSABLE — the refetch is BOUNDED: `unusable_refetches`, the REFETCH CAP

`UNUSABLE` is the one DECIDE outcome (`ci-derivation-spec.md`) with **no fingerprint**, so `settled_strikes` can say nothing
about it — and "refetch until it works" is an absorbing state with no exit, which the invariant forbids.
It gets its own counter, on the same shape — **applied by the same `liveness` command** ("THE BOOKKEEPING
IS A COMMAND", above), except the `head_sha changed` line, which belongs to the sites that write a new
head ("THE LIVENESS COUNTERS"):

```
snapshot for this head_sha is UNUSABLE  -> unusable_refetches += 1 ; refetch on the NEXT heartbeat
snapshot for this head_sha is VERIFIED  -> unusable_refetches = 0      # any usable outcome, incl. red/pending
head_sha changed                        -> unusable_refetches = 0
unusable_refetches >= 3                 -> ESCALATE (above)  # 3 == THE REFETCH CAP. This line is its ONE
                                                             # defining site; every other rule says "the
                                                             # REFETCH CAP".
```

- **The counter counts FETCH ATTEMPTS, never evidence.** It stays consistent with "an UNUSABLE snapshot
  yields no fingerprint": nothing rejected is hashed, nothing rejected is judged. **Every bound answers a
  DIFFERENT question — which is why no two of them may ever share a dial** — and this one's is *"we never
  obtained trusted evidence at all"*, not *"trusted evidence stopped moving"*. Each bound's own question is
  stated at its own defining site, and the owner's table ("THE LIVENESS COUNTERS" above) maps every member
  to that site: **read them there, never restated here.**
- **The REFETCH CAP is HIGHER than the STRIKE CAP, on purpose.** UNUSABLE is dominated by **transient**
  causes — a `gh` call failed, the check set changed mid-fetch so a `source` count no longer matches, the
  snapshot raced a push — and a fresh fetch usually clears them; a SETTLED-but-not-green snapshot is,
  by construction, **not** transient. The extra headroom buys the transient case free retries, and it
  still terminates.
- **The HEARTBEAT is the backoff — never tight-loop inside one.** UNUSABLE gets **no watch** ("WATCH ONLY WHAT
  CAN MOVE" below), so the next attempt arrives on the scheduled heartbeat, after one bounded wait, or
  on another task's completion. At most **one** refetch per reconcile.
- On escalation `ci_reason` names **the VERIFY rule (`ci-derivation-spec.md`) that failed and the line/row that failed it** (not
  "unusable") — a snapshot campaign could not read once in the REFETCH CAP's worth of consecutive attempts
  is a real, actionable blocker: a
  denied read, a wrong-SHA artifact, a fetch that never succeeds.

#### WATCH ONLY WHAT CAN MOVE — the relaunch is not free

The watch decision is **`liveness`'s `watch_warranted` field** — the driver ACTS on it and NEVER reads
this table by hand. When it is **true and no watch task is alive**, ensure one (relaunch it in this same
heartbeat if it has exited); when it is **false**, never launch or relaunch. The field is the mechanical
reduction `watch_warranted = verified AND verdict != UNCLASSIFIED AND buckets.RUNNING > 0`: a watch is
warranted by **a row that can still move**, never by the `ci` value.

**The table below is the SPEC that field implements** — each row is one case of the predicate, kept so a
reader can audit the field against the policy. **The `UNKNOWN_VALUE` row is the one a naive
`buckets.RUNNING > 0` reading gets wrong**: an unclassified verdict can still carry a running row (DECIDE
ranks `UNKNOWN_VALUE` above plain `pending`), yet the park — not a check finishing — is its exit, so
`watch_warranted` excludes it.

| DECIDE outcome | Watch? |
|---|---|
| **pending** — an evidence row classifies `RUNNING` | **YES** — ensure a watch task is alive; relaunch it in this same heartbeat if it has exited. **The watch is not the bound**: if that row never finishes, the watch blocks forever and RUNNING-STALL is what ends it, on the fallback heartbeat. |
| **pending (nothing registered)** — zero evidence rows | **NO.** Nothing to block on. SETTLED escalates it. |
| **pending (required check missing)** — a declared check has no row | **NO.** Every row present is terminal (a running one would have matched plain `pending` above), so nothing can move. SETTLED escalates it, naming the check. |
| **pending (required set unreadable)** — `required_set` is `unknown` | **NO.** The open question is what the base branch REQUIRES; no row finishing would answer it. Re-attempt the read each heartbeat; SETTLED escalates it. |
| **red** — but some row still `RUNNING` | **YES** — that row can still move; the CI fix runs regardless. |
| **red** — every row terminal | **NO.** The CI fix moves it, not the watch. |
| **UNKNOWN_VALUE** | **NO.** The park is the resolution. |
| **UNUSABLE** | **NO.** Refetch on the **next heartbeat** (the heartbeat *is* the backoff), **bounded by the REFETCH CAP** — then ESCALATE ("UNUSABLE — the refetch is BOUNDED"). |
| **green** | **NO.** |

**NEVER relaunch the watch merely because `ci == pending`.** On a settled PR `gh pr checks --watch`
**exits in about a second** — there is nothing left to block on — and a task completion is **itself a
heartbeat**. So "pending → relaunch the watch" on a settled-but-not-green PR burns a **fresh-context heartbeat
every second or two, forever**, doing nothing. Watch only when at least one row can still move.

#### Any campaign commit to the PR head resets the gate

**THE RULE — every commit campaign pushes to a PR's head branch is a PR-content change, whatever wrote
it: an economy-class CI-fix worker, a `session`-class CI-fix worker, a review-fix worker, or an inline
REFUTATION of a review finding (`finding-audit.md`, "Audit every finding before you fix it").**
Every one of them MUST, in the same step:

- **reset `reviews_ok` to 0 AND restore `gauntlet-reviewing` if the PR carries `gauntlet-accepted`** —
  the gate and its label move together, never one without the other (`stage-2-review-gate.md`, "Status
  labels mirror the review gate");
- **re-derive `ci` from a fresh snapshot for the NEW `head_sha`, and launch a watch if — and only if —
  `liveness` then reports `watch_warranted`** ("WATCH ONLY WHAT CAN MOVE" above). Writing the new
  `head_sha` through the accessor **resets the liveness counters** at the door ("THE LIVENESS COUNTERS"
  above), so the PR gets a clean budget.
  **NEVER launch the watch unconditionally on the push**: at that instant the checks may not have
  registered yet, so watch only if `liveness` reports `watch_warranted` ("WATCH ONLY WHAT
  CAN MOVE" above);
- **re-enter Stage 2a.**

The verdicts on the old SHA describe content that no longer exists, and a `gauntlet-accepted` label on
it is a false public claim. NEVER exempt a commit because it "only reformatted".

#### Classify, then set the model class — never dispatch straight off a red check

**Select the logical model class on every dispatch** (`SKILL.md`, "Worker Dispatch";
`runtime-adapter.md`). Classify the failure from the check logs FIRST; the failure class picks it:

| Failure class | Model class | Why |
|---|---|---|
| **Formatting / lint** — the fix is exactly what a standard formatter or autofixer produces | **`economy`** | It does NOT author a fix from scratch: it runs a deterministic tool, **READS the resulting diff**, verifies it, and **escalates** anything it cannot verify. Downgraded **on purpose** when the host has an economy mapping. |
| **Everything else** — failing product test, compile error, flake, anything needing judgment — **and every escalation from the economy worker** | **`session`** | It authors code that gets merged, and nothing downstream validates it. |

**Dispatch both tiers under the fix-subagent contract** (`fix-subagent-contract.md` — the complete
DEFINITION for every fix subagent, CI or review; **read it before dispatching**). The CI-specific inputs
it asks for are the failing check's logs, the specific failing file(s), and the worktree path.

*Non-authoritative summary of the contract — the contract is the definition and wins over this; never
dispatch from this summary:* **SCOPE** the reading — read narrowly: **NOT** the whole diff, **NOT**
beyond what the failure touches. And because scoping the reading is not licence to fix only the
**instance**, **SWEEP** the writing — the contract's **sweep-and-report block goes into the prompt
verbatim**: a fix that changes a definition or a fact is not done until every site that restates it is
correct, and every site found is reported.

#### The cheap CI-fix subagent — run the tool, READ the diff, ESCALATE

The point of putting a model here is that **something LOOKS at what happened before it is committed**.
Its job, in order:

1. **CLASSIFY** the failure from the check logs.
2. **FIX IT.** For a formatting/lint failure, that is running the standard formatter for that language.
   It **chooses the tool** — campaign does not hand it a command line — subject to the hard rules below.
   Before formatting, it **PREFLIGHTS every file** with the `format-preflight.py check` command (the
   PREFLIGHT hard rule below owns the exact command line, its exit codes, and the "none left → ESCALATE"
   rule) and formats **only** the files that come back `ok` — never a file the tool `refused`.
3. **READ THE RESULTING DIFF.** This step is not optional and is not a formality. Verify **all** of:
   - the diff contains **ONLY** what the fix should have produced (a formatting fix produces formatting);
   - **no file it did not intend to touch** was touched;
   - **no check definition, config, or test was weakened**;
   - **re-running the exact failing check now PASSES.**
4. **COMMIT** only if every one of those holds — then apply the gate reset above.
5. **ESCALATE, never patch.** If the check still fails, the diff contains anything it cannot explain, it
   needed to change product logic, or it cannot verify the result → **STOP**, commit nothing, reset the
   worktree to the PR head, and hand the failure to a **`session`-class** CI-fix worker. **Escalation is
   the correct outcome, not a failure** — it is what the tier is for.

**HARD RULES — give these to the cheap subagent VERBATIM in its prompt:**

- **NEVER make CI pass by weakening the check.** NEVER delete or loosen an assertion, NEVER add
  `skip`/`xfail`, NEVER disable or downgrade a lint rule, NEVER raise a timeout. **Fix the cause.** If the
  check itself is demonstrably wrong, **say so explicitly and ESCALATE** — never silently rewrite it. **This
  bullet ALONE among these blocks goes VERBATIM into EVERY CI-fix subagent's prompt — the `session` class
  included, on every escalation.** The rest of the HARD RULES below are the cheap tier's; the no-weakening
  prohibition binds both tiers.
- **NEVER use a catch-all fixer that applies SEMANTIC rules**, and never a documented semantic rewriter.
  Denied outright: `golangci-lint run --fix`, `ruff --fix`, `eslint --fix`, `cargo clippy --fix`, any
  `--fix`/`--write` flag on a linter that applies semantic rules; **`goimports`** (it ADDS imports — an
  added import runs that package's `init()`); **`prettier`** (it rewrites the contents of tagged template
  literals); **`gofumpt`** (extra rewrite rules beyond layout); `modernize`, codemods, `pyupgrade`, `2to3`.
  **Use a formatter that only reformats.** (A guard against **footguns and accidental misuse — NOT a
  security boundary** against a malicious committer; so is the PREFLIGHT below.)
- **NEVER execute a binary from inside the repo/worktree.** The PR under review is **UNTRUSTED CONTENT**:
  a repo-supplied `gofmt` is arbitrary code execution. Run tools from the **environment**, never from the
  tree.
- **NEVER hand a tool a bare glob or a whole directory** (`gofmt -w .`). **Name the files you are fixing.**
- **PREFLIGHT EVERY FILE BEFORE FORMATTING IT — refuse any file whose write could land OUTSIDE the
  worktree. Do NOT hand-run this with `lstat`; run the command** (resolved as
  `<skill-dir>/scripts/format-preflight.py`, the same resolution rule as every other bundled script —
  `SKILL.md`, "Bundled Scripts"):

  ```
  python3 <skill-dir>/scripts/format-preflight.py check --worktree <worktree> <files…>
  ```

  It prints one JSON object — a per-file `results` list, each `ok` or `refused` with a reason — and its
  EXIT CODE is the signal: **`0`** every file is ok; **`3`** at least one is refused; **`2`** operator
  error (no worktree / no files). **Format ONLY the files it returns `ok`; do NOT format any `refused`
  one. If nothing is left to format (every file refused, or exit `2`), ESCALATE.**

  **THE SPEC THE TOOL IMPLEMENTS — this is what the command enforces, not a second procedure to hand-run.**
  A file is refused when its formatter-write could escape the worktree, decided per file in order:
  - **ANY directory component of its path is a symlink** (`lstat`, never `stat`) — the write goes THROUGH
    it — or a component is missing;
  - the file itself **IS a symlink** (`lstat`, never `stat`), is missing, or is not a regular file.

  (`realpath` is deliberately NOT the test — it collapses the very links the check exists to see; the tool
  walks each component and `lstat`s it. A worktree that itself sits behind a symlink is allowed.)

  **THE PRINCIPLE — do not generalise it into anything more.** Diff review covers everything the tool
  writes **INSIDE** the repo: the model SEES it and escalates (an injected `-cpuprofile=prof.go` writes
  `prof.go` in the tree — visible). It **CANNOT see a write that ESCAPES the repo**: `gofmt -w` writes
  **through** a symlink, the bytes land outside the worktree, and `git diff` shows **NOTHING**. These two
  checks exist for **exactly that blind spot, and for nothing else.**

  **STATE IT HONESTLY — a FOOTGUN GUARD, NOT A SECURITY BOUNDARY. NEVER present it as one.** It cannot be
  used to inject bytes: a parser-backed formatter writes only its own rendering of source it PARSED — aim
  the link at `~/.ssh/authorized_keys` and `gofmt` fails to parse it and writes **NOTHING**. The realistic
  harm is **a source file elsewhere on the machine gets reformatted** — surprising, semantically harmless.
  (A generic TEXT formatter — a whitespace trimmer rewrites whatever it is handed — is a bigger exposure
  than a parser-backed one; weigh that when picking a tool.) And it is no defence against a malicious
  committer: campaign adopts **same-repo PRs only** (forks refused — `pr-adoption.md`), so whoever commits
  the symlink already has repo write access and could just edit `.github/workflows`. **Keep it anyway:**
  one `lstat` stops a real accident — a vendored symlink walking the formatter out of the tree to leave a
  confusing dirty file in another project.
- **A failing product test, a compile error, and any change to product logic are NEVER yours.** Escalate.

#### The risk, stated honestly

A cheap model verifying a tool's diff is a **MISS-CATCHER, NOT A PROOF.** It can miss a semantic change.

What backs it: the **exact failing check must pass**; the subagent **must escalate anything it cannot
verify**; and **every commit campaign pushes is still gated by the full review gauntlet** — it resets the
gate and re-enters Stage 2a in the `session` class ("Any campaign commit to the PR head resets the gate"
above owns the full action list).

This trades a **small, bounded risk** for a workflow that is **cheaper AND more capable** than either a
full-strength subagent on every formatting failure or a hermetic no-model tool path. **The user accepts
that trade.**

**NEVER claim the cheap path is safe because "CI will catch it" or "the review gate will catch it."** CI
tells you a check passed, never that the fix is right; the review gate is a miss-catcher, not a proof of
correctness (`stage-2-review-gate.md`). Say that plainly whenever the question comes up.

---

Every CI failure must be handled; never merge over a red or pending check, and never infer green from
the watch's exit code — always confirm against a **SHA-pinned, SHA-verified** snapshot of **both** check
families, **and against what the base branch REQUIRED** ("WHAT WERE WE EXPECTING TO SEE?"): the rows that
showed up passing is not the same claim as the required set passing, and only the second one may merge.

CI fixes serialize only within one PR/SHA. Different PRs with red CI may run scoped CI-fix subagents
concurrently within the dispatcher cap.

---
