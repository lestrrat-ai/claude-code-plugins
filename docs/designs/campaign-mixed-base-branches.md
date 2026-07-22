# Mixed base branches in one gauntlet campaign

Status: proposed design. This document does not change campaign behavior.

## Problem and motivation

One campaign run currently assumes that every adopted pull request targets the same base branch. That
prevents a maintainer from putting related maintenance work into one run when, for example, some PRs
target `v3` and others target `v4` or `main`.

The campaign already tracks review, CI, repair, and merge state per PR. The base branch is the main
exception: it is stored once in the ledger header and reused for every row. The required-check set is
also stored once because it is derived from that base. Admission therefore rejects a mixed set before
the otherwise per-PR pipeline can run.

This feature supports static mixed bases only. A new row records the PR's live `baseRefName` once during
adoption. The campaign does not change that recorded base later. If the live target changes during the
run, the campaign parks the row for the user instead of updating it or automatically running the gate
again.

The design preserves these guarantees:

- Every diff, rebase, review, CI decision, and merge uses that PR row's recorded base.
- Reconciliation, base preflight, merge checks, and merge execution fail closed when the live base no
  longer matches the recorded base.
- Existing single-base ledgers continue without migration.
- The run keeps one run ID, lease, heartbeat, owner label, and serialized merge drain.
- The design uses the existing typed helpers and host adapters on both Claude Code and Codex.

## Current state

### The run contract admits only one base

The single-base rule is explicit in the current instructions:

- `plugins/gauntlet/skills/campaign/references/run-identity-and-lease.md:1-15` defines a run as targeting
  one base, requires all adopted PRs to agree, and says that `<base>` supplies review diffs and local
  synchronization.
- `plugins/gauntlet/skills/campaign/references/loop-control.md:124-140` fetches every explicitly named PR's
  `baseRefName`, refuses disagreement before creating run state, and writes the agreed value to the
  header.
- `plugins/gauntlet/skills/campaign/references/pr-adoption.md:20-21` repeats the agreement requirement for
  later adoption.
- `plugins/gauntlet/skills/campaign/SKILL.md:87-89` tells each heartbeat to reread the header's
  `base_branch` as run configuration.

Run and status labels do not encode a base. The owner label is the run boundary
(`run-identity-and-lease.md:50-66`), and the status labels describe the review gate
(`loop-control.md:159-192`). That part already fits a mixed-base run.

### The ledger puts base-owned state in the header

`plugins/gauntlet/skills/campaign/scripts/ledger.py:40-72` declares `base_branch` and `required_set` as
header fields. Its comments state that the required set is a property of the base and therefore belongs
in the header. `ledger.py:106-205` has no corresponding row fields. A row records only `base_ok_sha`.

`plugins/gauntlet/skills/campaign/references/files-and-ledger.md:110-150` shows the same model: one header
contains `base_branch` and `required_set`, while PR rows contain neither. It describes `base_branch` as
set once and `required_set` as the requirements of that one branch.

The loader already supports additive schema changes. `ledger.py:404-458` fills missing declared fields
from defaults, so an old JSONL file loads without migration. The write side is whole-store: `dump()`
writes every declared field of every row, and every mutating door routes through it, so the first write
after a schema addition stores the new fields at their defaults on every legacy row. A row addition
therefore needs no migration step, provided the default value itself carries legacy header inheritance
rather than relying on field absence.

### Adoption sees each base but discards it from row state

`plugins/gauntlet/skills/campaign/scripts/pr-adopt.py:147-193` includes `baseRefName` in the read-only plan
as `base`, but lines 173-176 say the base is header state rather than row state. The executor fetches the
base (`pr-adopt.py:229-245` and `301-306`) and then writes a row without it (`pr-adopt.py:276-290`).

Discovery is already run-scoped rather than base-scoped. `scripts/reconcile.py:74-81` fetches PRs by the
run label and includes `baseRefName` in the snapshot without filtering it. The conflict appears later:
`reconcile.py:308-318` and `347-372` compare every live PR base with the one header base.

No label schema change is needed. `scripts/label-mirror.py:162-203` chooses between the two status
labels from the review tally and tier alone; it reads row `status` only to skip terminal rows and
projects no held state into any label. The mirror is a PR mutation, so `ledger.py dispatch-check`
already freezes it for a held row along with every other mutating action (`ledger.py:1032-1033` names
relabel in the held-row refusal); a held row's labels therefore stay as they are until the hold clears.
The existing `awaiting-user` ledger status can represent an unsupported base change — it is ledger
state, visible in `status` and `ci_reason`, not in a label — and none of that projection depends on
the base.

### Rebase, review, and repair receive the run base

The orchestration docs pass the same `<base>` into triage, preflight, rebase, and review
(`references/loop-control.md:343-410` and `references/stage-2-review-gate.md:18-35,126-158`). The helpers
mostly operate on an explicit branch, but the caller supplies the header value:

- `scripts/triage.py:19,157,620` accepts `--base`. Its analysis is not inherently run-wide.
- `scripts/base-preflight.py:82-103,236-270` accepts `--base`, checks and fetches it, and stamps
  `base_ok_sha` after `proceed`.
- `scripts/clean-rebase.py:139-143,224-315` accepts `--base` and uses it for fetch, rebase, diff comparison,
  and push. It does not confirm that the argument is the adopted PR's live target.
- `scripts/review-dispatch.py:349-417,477-493` puts the supplied base into the typed review transport but
  cannot compare it with per-PR ledger state because that state does not exist.
- `scripts/review-pass.py:160,1222-1313` binds a pass to PR, pass, launch attempt, and head SHA. It does not
  bind the pass to a base.
- `scripts/worker-prompt.py:315-358` puts base text and head content into a fix prompt.
- `scripts/repair-pass.py:748-830,896-941` reads the header base. Its repair bundle already records and
  revalidates a base ref and base SHA; it needs to choose that base from the row.

A PR target can change without changing its head SHA. Current head-only checks do not detect that event.
This design does not make work portable across target changes. It prevents further campaign action by
comparing the fixed row base with live GitHub state.

### Required CI is run-wide

`plugins/gauntlet/skills/campaign/references/stage-2-ci.md:24-50` takes the required set from the header.
Its required-set procedure at lines 125-199 reads one base and stores one result for the run.

`scripts/ci-status.py:455-513` queries the requirements of one base and reads or writes the header field.
The CLI at `ci-status.py:2301-2320` describes `derive --required-set` as a header value, and its
`required-set` operation has no PR selector. CI liveness checks at `ci-status.py:1494-1552` are already
head-specific.

The check-run and commit-status snapshot is also head-specific. `scripts/ci-snapshot.py` need not know the
base as long as `ci-status.py derive` supplies the selected row's required set. Its CLI wording at
`ci-snapshot.py:1390` nevertheless calls that value the ledger header's required set and must change with
ownership.

### Merge and local synchronization use the header base

`scripts/merge-check.py:256-297` uses the header base for its ancestry check. `scripts/merge.py:140-204`
compares live `baseRefName` with that value, lines 232-265 use it for readiness, and lines 293-336 and
420-549 synchronize that branch locally. `references/stage-3-merge.md:30-58,100-185` describes the merge
drain in terms of one `<base>`.

The drain is already serialized per PR and can remain so. `merge.py:515-520` also records an intentional
single-user residual: the GitHub merge operation has no expected-base compare-and-swap, so a base change
between the last check and the merge can only be detected afterward.

### Carryover records one base

`references/carryover.md:33-54,71-82` waits for one resolved base and prunes history against it.
`scripts/carryover.py:103-155,168-184` writes format v1 with one top-level `base_branch`, refuses to distill
a header without it, and omits the base from each projected PR object. That loses which release line an
individual merged or aborted PR belonged to.

### Stacked PRs are not currently modeled

The campaign references and helpers contain no explicit stack relation. The common-base admission rule
also rejects a normal stack because a child PR targets its parent PR's head branch rather than the
parent's eventual release branch.

Without an ordering rule, merging a child into its parent's branch advances the parent's head and voids
work already completed for that parent. Existing head reconciliation makes this safe, but it can waste
reviews and CI work.

### Other run-wide wording

The same assumption appears in operational summaries:

- `scripts/nudge.py:96-118` tells a recovery pass to reread one header base and required set.
- `references/bailout-and-final-report.md:151-156` reports one base required set.
- `references/critical-rules.md:488-494` restates that a run has one base.
- `references/followups.md:193-207` tells a follow-up fixer to use the current run's base, which is
  ambiguous in a mixed run.
- `references/reviewer.md:46-50` describes every pass as reading `origin/<base>...HEAD`; `<base>` must be
  the selected row's effective base.

The runtime adapter is not a blocker. `references/runtime-adapter.md:198-232` already carries `base` in
each `ReviewTransport`. The implementation only changes how that existing value is resolved. It requires
no host-specific command, environment variable, typed transport revision, or heartbeat mechanism.

## Proposed design

### Core invariants

1. Every nonterminal PR row has one effective base branch.
2. A new row stores the live `baseRefName` observed during adoption. That stored value never changes.
3. Reconciliation compares the stored value with live `baseRefName` before dispatching work. A mismatch
   parks the row for the user; a row already held parks when its hold clears through its own exit.
4. The campaign never updates a row to a new base and never automatically runs the gate again because a
   live base changed.
5. Required checks are stored per row. The grouped refresh keeps the existing settle-once CI
   procedure per distinct effective base: at most one successful GitHub read per base per run.
6. One run may contain any number of distinct bases, but merging remains serialized.
7. Run identity, lease, heartbeat scheduling, owner labels, and status-label names remain run-wide.

### Resolve row-owned state with legacy fallback

Add two schema-owned accessors. Every consumer uses them:

```text
effective_base(header, row):
    row.base_branch, if it is explicit
    otherwise header.base_branch

effective_required_set(header, row):
    row.required_set, if it is explicit
    otherwise header.required_set
```

The fallback exists only for old rows. Consumers must not duplicate it. A helper with a ledger and PR
number resolves values through these accessors. If an existing CLI keeps `--base` for clarity or tests,
the argument becomes an assertion and must equal `effective_base`; it is not another source of truth.

New runs write `base_branch: "-"` and `required_set: "unknown"` in the header. Every new row writes an
explicit `base_branch`, even in a single-base run. Its row `required_set` starts as `unknown` until the
grouped refresh supplies the canonical value.

Old rows load with `base_branch: "-"` and `required_set: "-"` and resolve the old header values through
the accessors. Inheritance lives in the `-` sentinel and the accessors, not in field absence: every
ledger write serializes the whole store with the full declared schema, so the first write after the
schema addition stores the `-` defaults explicitly on every legacy row. That rewrite is expected and
lossless — a stored `-` resolves exactly as a missing field does.

### Adoption and discovery

Explicit-run preflight still verifies that each requested PR exists and is open. It no longer requires
their `baseRefName` values to agree. It records the PR-to-base mapping in the pending adoption plan,
creates one run, and fetches each distinct base ref once.

`pr-adopt.py` writes the plan's live base into `row.base_branch` when it creates the row. The field is
tool-owned after creation. Ordinary ledger writes, reconciliation, refresh, and re-adoption cannot change
it.

Label-based discovery remains run-scoped. The owner label selects membership, and a newly discovered PR
contributes its live base during adoption. For an already-adopted PR, discovery compares live state with
`effective_base` and follows the mismatch rule below.

An old ledger's header base does not constrain new adoption. Legacy rows may inherit that header base,
while newly adopted rows store different explicit bases in the same run.

### Unsupported base changes park the row

On every reconciliation snapshot, compare each open row's `effective_base` with live `baseRefName` before
head reconciliation, dispatch, CI derivation, or merge scheduling.

When the values differ on a row that is not already held, run the existing machine-blocker park
transition for that row; an already-held row follows the held-row rule later in this section. The
durable reason is:

```text
base changed from <recorded> to <live>; not supported mid-run
```

The transition sets `status: awaiting-user`, records the reason in `ci_reason`, and clears
`blocker_ruling` as the existing park contract requires — one atomic ledger write and nothing else.
The park performs no label work: once the row is held, `dispatch-check` forbids every action that
mutates the PR, relabel included (`ledger.py:1032-1033`), so the row's labels are left as they are
until unpark, when the normal status-label mirror covers the row again. No label work is owed at park
time anyway: the status labels project only the review tally against the tier, which the park does
not change, and no label shows the hold (see Labels, lease, and host compatibility). The row remains
in the run, but held-status guards prevent review, repair, CI action, rebase, and merge. A held row is
also ineligible for the grouped required-set refresh, though it still counts as settlement evidence for
its base (see Required checks and CI).

Do not update `base_branch`, `required_set`, review tallies, CI state, preflight stamps, or launch records.
Do not add a new transition or artifact identity. The feature treats the mismatch as an unsupported user
change, not as new campaign state.

The user resolves the park through the existing ruling path:

- `abort` terminates the row through the existing abort procedure.
- Restore the PR's live base to the recorded base, then choose `retry`. The existing unpark transition
  resumes normal reconciliation. If the head also moved, ordinary head reconciliation handles it.
- `retry` while the mismatch remains causes the next reconciliation pass to park the row again with a new
  blocker question. A spent ruling never answers the new park.

An already-held row keeps its open question, and the held-row freeze means reconciliation reports the
additional base mismatch in its own output without writing the frozen row or overwriting that question.
Parking waits for the hold to clear through that hold's own exit, because the existing transitions
permit nothing else: `ledger.py park` refuses `awaiting-api` and `repairing` rows — "that state has its
own owner … Resolve it through its own path, not a park" (`ledger.py:1073-1075`) — and `unpark` applies
only to `awaiting-user` (`ledger.py:1106-1108`). So an `awaiting-user` row clears through its ruling, an
`awaiting-api` row through API approval, and a `repairing` row by completing its decided repair — a live
target change does not block the repair, because repair bundles validate the recorded base locally (see
Base preflight, rebase, review, and repair). Whatever the exit, the next reconciliation pass compares
bases again and, if the mismatch remains, parks the now-actionable row with the base-change reason; a
hold that exits into a terminal status (an `abort` ruling, a repair decided as abort) needs no park.
Terminal rows remain immutable.

### Base preflight, rebase, review, and repair

`base-preflight.py` loads the selected row, resolves `effective_base`, and fetches live `baseRefName`. It
refuses `proceed` on a mismatch and routes the reason through the same machine-blocker path. Preflight
gates only work that `dispatch-check` has already cleared for the row, so the row is never held when
preflight parks it. A successful check continues to stamp the existing `base_ok_sha`; no extra preflight
field is needed.

`clean-rebase.py`, `triage.py`, `review-dispatch.py`, fix prompts, and `repair-pass.py` receive the row's
effective base. Any retained `--base` argument must match the accessor. Repair bundles keep their existing
base-ref/base-SHA checks; those checks validate the recorded base locally and do not read live
`baseRefName`, so a decided repair runs to completion even when the live target has changed, and the
mismatch is handled at the repair's exit (see Unsupported base changes park the row).

Review pass identities, fix completion records, repair records, and typed runtime transports do not gain
new version or revision fields. Base changes are unsupported and park the row instead of making evidence
portable to another target.

### Required checks and CI

`required_set` becomes row state because its owner, the base, is row state. The grouped refresh keeps
the existing settle-once CI procedure, applied per base group instead of per run. The refresh separates
two concepts, defined here and nowhere else:

Settlement evidence — who can witness a base's settled value. Any row whose `effective_base` is the
group's base witnesses the settled canonical value when its `effective_required_set` is `declared:` or
`none`. Held and terminal rows count as witnesses: their stored values were read for this base and stay
valid even though those rows take no further campaign action. In particular, a base-mismatch-held row's
`required_set` was read for its recorded base and remains evidence for that base — the hold forbids new
reads and writes for the row, not reuse of the value it already carries. A legacy group can also be
settled purely through its header fallback.

Refresh eligibility — who the refresh may write. A row is eligible when it is nonterminal and its
status is outside the ledger's held set — the schema-owned `HELD_STATUSES` enumeration in `ledger.py`
that `dispatch-check` enforces, not a member list restated here. Only eligible rows are written, and
only eligible rows get CI work. A
base-mismatch-parked row is therefore never a write target, and no GitHub read is performed on its
behalf — a fresh read for its recorded base would stamp requirements for a branch the PR no longer
targets. After an unpark, the next refresh covers the row again.

During each required-set refresh, group rows by `effective_base`. A group is settled when any of its
rows — eligible or not — witnesses the settled value, or when the header fallback settles a legacy
group. A settled group is never reread for the rest of the run: the refresh copies the settled value,
without touching GitHub, to any eligible row in the group whose effective value is still `unknown` (a
newly adopted or just-unparked row) — even when every witness is now held or terminal. A group with no
settled value and at least one eligible row gets one GitHub read, and a success writes the canonical
result to every eligible row in the group — including a legacy row, whose inherited `-` becomes an
explicit value at that point. A group with no eligible rows is never read. Rows on the same base
therefore resolve the same value, while rows on different bases may use different policies. A mid-run
policy change for a settled base is not observed; see the accepted residuals.

`ci-status.py required-set` must support the grouped operation or a selected base group without returning
to header-owned storage. `ci-status.py derive` receives the selected row's
`effective_required_set`. Existing head-SHA checks remain the freshness boundary; no policy revision field
is added.

`unknown` keeps its current fail-closed meaning. A failed read for one base writes or preserves `unknown`
only for eligible rows in that group and blocks those rows from becoming green. Other base groups continue
independently. A later heartbeat retries the read for a still-unsettled group under the existing CI
procedure.

### Merge and local base synchronization

`merge-check.py` and `merge.py` resolve the selected row's `effective_base` and fetch live
`baseRefName`. They fail closed and use the base-change machine-blocker reason unless the values agree.
Ancestry, readiness, merge preconditions, and post-merge synchronization all use the row base.

The serialized drain remains one PR at a time across the whole run. After merging a `v3` PR, the helper
updates local `v3`; after merging a `main` PR, it updates local `main`. There is no run-wide checkout that
must stay on one branch.

The pre-merge and post-merge live checks remain. The small race inside the GitHub merge call remains an
accepted residual, as it is today.

### Stacked PRs are future work

Mixed-base admission allows a normal stack to enter one campaign, but this feature adds no stack graph or
new scheduler. Existing head reconciliation remains the safety mechanism: when a child merges into a
parent branch, the parent's head changes and its head-bound work is reconciled normally. The campaign may
repeat parent review or CI work.

A later feature may derive leaf-first order without mutable base state:

```text
child -> parent  when effective_base(child) == parent.branch
                  and the parent match is unique
```

That optimization can block parent work until active children finish, then let normal head reconciliation
run after each child merge. Ambiguous matches, cycles, and external branch changes need their own design.
They are not part of static mixed-base support.

### Carryover and follow-ups

Carryover format v2 adds `base_branch` to every projected PR object. Its metadata may include a sorted,
deduplicated `base_branches` array for display and pruning. Pruning uses each PR/base pair, so history for
`v3` does not affect a new `main` PR.

Format v1 remains readable. Its one top-level `base_branch` is the effective base of every object in that
file. New distillation always writes v2, including for a single-base run. Existing history is never
rewritten.

A follow-up derived from one PR uses that PR's recorded base as its proposed target. The user may choose a
different target before opening or adopting the follow-up PR. A run-level follow-up has no implicit
target. Every resulting PR enters through normal adoption, which records live `baseRefName` once.

### Labels, lease, and host compatibility

No base-specific label is added, and the label mirror does not change. The owner label continues to
mean membership in one run. The two status labels keep their current meaning: they project the row's
review tally against its tier and skip terminal rows. No label shows a held state today, and this
design adds none — a base-mismatch hold is visible in the ledger row's `status` and `ci_reason` and in
the ledger table, not in any label.

The lease and heartbeat remain per run. `pending_adoption` remains a list of PR numbers. No lease proof,
owner identity, run ID, or run-state file gains a base dimension.

All execution continues through repository helpers, `gh`, Git, and the existing runtime adapter. The
adapter already carries a base in each review transport; callers now supply `effective_base`. Claude Code
native actions, Codex native actions, and cross-engine fallbacks keep the same behavior. No host name,
host-only tool, plugin-root environment variable, or new heartbeat mechanism enters the contract. Follow
`docs/runtime-compatibility.md` when implementing and validating shared plugin changes.

## Data model changes

### Fields

| Record | Field | Meaning | Default and compatibility behavior |
| --- | --- | --- | --- |
| Header | `base_branch` | Legacy base fallback only | Keep existing field. Old value remains usable; new runs write `-`. |
| Header | `required_set` | Legacy required-set fallback only | Keep existing field. Old value remains usable; new runs write `unknown`. |
| Row | `base_branch` | Target recorded from live `baseRefName` at adoption | `-` means inherit the legacy header. New rows must write an explicit value. Tool-owned and immutable after row creation. |
| Row | `required_set` | Canonical requirements for the row's effective base | `-` means inherit the legacy header. New rows start `unknown`; grouped refresh writes the canonical value. |
| Row | `base_ok_sha` | Head cleared by base preflight | Existing field, unchanged. Preflight also performs a live base comparison before `proceed`. |

The `-` inheritance spelling differs from `unknown`. In a new row, `unknown` means a required-set read has
not succeeded and must fail closed. In an old row, `-` means the value remains represented by the old
header field.

### Before: current single-base ledger

```json
{"type":"header","run_id":"g260722-0900-acde1234","base_branch":"main","required_set":"declared:[{\"context\":\"test\",\"app\":\"-\"}]","reviewer":"default"}
{"type":"row","pr":"41","branch":"fix-v3-parser","head_sha":"1111111111111111111111111111111111111111","base_ok_sha":"1111111111111111111111111111111111111111","reviews_ok":"2","ci":"green"}
{"type":"row","pr":"52","branch":"fix-v4-parser","head_sha":"2222222222222222222222222222222222222222","base_ok_sha":"2222222222222222222222222222222222222222","reviews_ok":"2","ci":"green"}
```

Both rows inherit `main` and the header's required set.

### After: mixed-base ledger

Unrelated existing fields are omitted. The on-disk row still contains the complete schema declared by
`ledger.py`.

```json
{"type":"header","run_id":"g260722-0900-acde1234","base_branch":"-","required_set":"unknown","reviewer":"default"}
{"type":"row","pr":"41","branch":"fix-v3-parser","base_branch":"v3","head_sha":"1111111111111111111111111111111111111111","base_ok_sha":"1111111111111111111111111111111111111111","required_set":"declared:[{\"context\":\"v3-test\",\"app\":\"-\"}]","reviews_ok":"2","ci":"green"}
{"type":"row","pr":"52","branch":"fix-v4-parser","base_branch":"main","head_sha":"2222222222222222222222222222222222222222","base_ok_sha":"2222222222222222222222222222222222222222","required_set":"declared:[{\"context\":\"v4-test\",\"app\":\"-\"}]","reviews_ok":"2","ci":"green"}
```

If PR 41 later targets `main`, its ledger row stays unchanged. Reconciliation parks it with:

```text
base changed from v3 to main; not supported mid-run
```

### Legacy row handling

An old row loads with `base_branch: "-"` and `required_set: "-"`. The accessors use the existing header
values. Reconciliation and base preflight compare the inherited base with live `baseRefName` exactly as
they do for an explicit row base.

Because every ledger write serializes the whole store, the first write after the schema addition also
stores those `-` sentinels explicitly on legacy rows. That is the expected on-disk shape, not a
migration: a stored `-` resolves exactly as a missing field does. A legacy row's `base_branch` stays `-`
for the life of the row — row base is creation-only, and the row predates the field. Its `required_set`
stays `-` while its base group is settled through the header fallback; a fresh grouped read for the
group replaces it with an explicit canonical value, exactly as for explicit rows.

An existing valid `base_ok_sha` remains a head stamp. Before any new `proceed`, base preflight confirms
that the live base still equals `effective_base`. No migration command is added, and no write converts a
`-` into a resolved copy of the header value.

### Lease and run state

There is no lease schema change. A lease still identifies one campaign run and owner. The ledger is the
only run-state file that gains fields, and those fields are per PR.

## Behavioral changes by area

| Area | Main files | Change needed |
| --- | --- | --- |
| Run contract and orchestration | `SKILL.md`, `references/run-identity-and-lease.md`, `references/loop-control.md`, `references/pr-adoption.md` | Remove common-base admission; record one live base per new row; resolve row bases on every action; park live mismatches. |
| Ledger ownership | `references/files-and-ledger.md`, `scripts/ledger.py` | Add row `base_branch` and `required_set`; own both effective-value accessors; make row base creation-only; keep header fallback and existing park/unpark transitions. |
| Adoption and reconciliation | `scripts/pr-adopt.py`, `scripts/reconcile.py` | Persist each new row's planned base; fetch distinct bases; compare each existing row with live `baseRefName`; park instead of changing row base, deferring an already-held row to its hold's exit. |
| Review and repair path | `references/reviewer.md`, `references/stage-2-review-gate.md`, `scripts/triage.py`, `scripts/base-preflight.py`, `scripts/clean-rebase.py`, `scripts/review-dispatch.py`, `scripts/worker-prompt.py`, `scripts/repair-pass.py` | Supply `effective_base`; assert retained `--base` values; make preflight compare live and recorded bases. Keep existing artifact identities. |
| CI policy | `references/stage-2-ci.md`, `references/ci-derivation-spec.md`, `scripts/ci-status.py`, `scripts/ci-snapshot.py` | Store required sets per row; group live reads by distinct effective base; derive each row with `effective_required_set`; update header-owned wording. |
| Merge | `references/stage-3-merge.md`, `scripts/merge-check.py`, `scripts/merge.py` | Use the selected row base for ancestry, readiness, merge, and local sync; compare live and recorded bases at both merge doors. |
| Carryover and reports | `references/carryover.md`, `references/followups.md`, `references/bailout-and-final-report.md`, `scripts/carryover.py`, `scripts/nudge.py` | Write/read carryover v2 per-PR bases; keep v1 fallback; report bases and required sets per row or grouped by base. |
| Labels, lease, and runtime | `scripts/label-mirror.py`, `scripts/run-id.py`, `scripts/lease.py`, `scripts/heartbeat.py`, `references/runtime-adapter.md` | No schema or host-route change. Update only wording or tests that assume one header base; hold divergence with the existing `awaiting-user` ledger status (labels unchanged). |
| Tests and fixtures | Campaign helper suites | Cover old-header fallback, mixed `v3`/`main`, grouped required-set reads (settle-once per group, settlement evidence from held and terminal rows, held-row write-ineligibility for every status in `HELD_STATUSES`), unsupported live base changes (actionable rows park; held rows defer to their hold's exit), preflight mismatch, carryover v1/v2, and merge mismatch. |

Update adjacent quick references, examples, fixtures, and comments that restate header ownership in the same
implementation PR as their owner. Before completing implementation, enumerate every consumer of
`base_branch`, `required_set`, `base_ok_sha`, `baseRefName`, `--base`, and carryover format, then account
for every search result.

## Backward compatibility

- Old ledgers load without migration. Legacy rows resolve header values through the two schema-owned
  accessors; the `-` sentinel carries that inheritance whether the field is absent at load or stored by
  a later write.
- There is no migration command. Ordinary writes serialize the full declared schema, so legacy rows gain
  explicit `-` sentinels on the first write; every value they resolve stays the same.
- Existing single-base runs keep their review counts, CI meaning, and merge order. They resolve the same
  effective base for every old row.
- A legacy base-preflight head stamp remains usable only with a successful live-base preflight before the
  next action.
- Existing review, fix, repair, and CI artifact formats remain unchanged.
- New rows always store an explicit base. A new run containing only `main` PRs uses the same row schema as
  a mixed-base run.
- Carryover v1 remains readable indefinitely. New output is v2; no old history file is rewritten.
- `unknown` required-set state remains fail-closed. Header fallback never converts an unreadable value to
  `none`.
- Plugin manifest versions are outside this design. Implementation must not bump either host manifest
  unless the maintainer separately requests a release version.

## Edge cases and accepted residuals

### Edge cases that must be handled

- **Live base differs from the recorded base:** park with the exact recorded and live names. Do not change
  row base or gate state.
- **Base and head differ in one snapshot:** park on the base mismatch first. After the user restores the
  base and retries, normal head reconciliation processes the current head.
- **Required rules differ across bases:** derive one canonical set per distinct base and copy it to that
  base's eligible rows. `unknown` blocks only the affected group.
- **Required rules change for one base:** a settled group keeps its settled value for the rest of the
  run — the retained settle-once procedure never rereads a settled group, matching current single-base
  behavior. Only a group still unsettled observes the new policy when its read is retried. Other groups
  are unaffected either way.
- **A base ref is deleted or unreadable:** reconciliation, preflight, and merge fail closed through the
  existing actionable machine-blocker path.
- **A row is already held:** preserve its open question and report the base mismatch. Parking waits for
  the hold to clear through its own exit (owner: Unsupported base changes park the row); the next
  reconciliation pass then creates the base-change park if divergence remains.
- **A terminal PR's live base later changes:** do not reopen or rewrite the terminal row. Carryover retains
  the base recorded for that campaign row.
- **One base is protected and another has no required checks:** each row preserves the existing
  `declared`/`none`/`unknown` distinction.

### Accepted residuals

This remains a single-user advisory workflow. The design does not add distributed leases, per-base
owners, cross-process transactions, or a database.

- A user can change a PR base in the short interval inside `gh pr merge`. The existing post-merge check
  can report it, but current campaign machinery cannot make the GitHub merge conditional on an expected
  base.
- GitHub branch protection or rulesets can change immediately after they are read. A settled base group
  keeps the value it read for the rest of the run — settle-once, unchanged from today. The design adds
  no mid-run invalidation and does not lock repository policy.
- A hand-edited ignored ledger can be made inconsistent. Loaders and write doors still fail closed on
  malformed state; the design does not defend against every coherent but false value the sole user could
  type.
- Grouped required-set reads are an optimization. A transient failure may leave one base group at
  `unknown` until the next heartbeat, safely delaying those PRs.

These residuals do not weaken non-owner merge refusal, malformed-input handling, or live-base checks at
reconciliation, preflight, and merge.

## Resolved question

A `BASE` column in the user-visible ledger table is a nice-to-have, not a requirement (maintainer
decision, 2026-07-22). It is display-only and does not gate the feature; the simplest form is an
unconditional column added with the row field in PR 1, and it may be dropped or deferred without
affecting anything else in this design.

## Staged implementation plan

Keep mixed-base admission disabled until every base consumer resolves row state. Three implementation PRs
are enough:

1. **Add row ownership and compatibility accessors.** Add row `base_branch` and `required_set`, immutable
   base creation, effective-value fallback, table output, and legacy-ledger tests. Keep common-base
   admission and current consumers unchanged.
   <!-- "Table output" here enacts the Resolved question above, which itself directs "an unconditional
        column added with the row field in PR 1" — the plan and the decision give one answer, not two.
        The same decision pre-authorizes dropping or deferring the column without affecting anything
        else; that latitude is stated once, at the decision's owning site. -->
2. **Convert consumers and fail closed on divergence.** Move adoption, reconciliation, triage, preflight,
   rebase, review/fix/repair inputs, grouped CI policy, merge checks, merge synchronization, carryover v2,
   nudge, and reports to effective row state. Add mismatch parking and all focused fixtures. Keep
   common-base admission as a compatibility checkpoint.
3. **Enable mixed-base admission.** Remove the agreement refusal, fetch distinct bases, update remaining
   owner docs and examples, and add an integration fixture with concurrent `v3` and `main` rows through
   adoption, review, different required sets, serialized merge, mismatch parking, and carryover.

Each PR runs its affected helper fixture suites plus campaign validation. The final PR validates both
Claude Code and Codex plugin layouts and install paths as required by `docs/runtime-compatibility.md`. No
host-specific implementation branch is needed.
