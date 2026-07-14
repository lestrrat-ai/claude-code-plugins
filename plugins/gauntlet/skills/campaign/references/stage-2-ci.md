### 2b. CI (event-driven)

Each PR has a background task that waits on `gh pr checks --watch`. **The watch only BLOCKS — it is
never evidence.** When the task completes, a wake **fetches a fresh snapshot pinned to the PR's current
`head_sha`**, verifies it, and decides `ci` **from the snapshot's contents — NEVER from the watch's exit
code** — then writes the `ci`/`reviews_ok` result through `scripts/ledger.py … set --pr <N> --ci <state>
[--reviews_ok 0]` **by field name** (`files-and-ledger.md`), never by hand-editing the row by column
position.

#### WHO DOES WHAT — the background task ONLY WATCHES; the WAKE fetches. This section is the DEFINITION.

**This split is normative, and every other file defers to it** (`pr-adoption.md`, `loop-control.md`):

| Actor | Does | Does NOT |
|---|---|---|
| **The background task** | **BLOCKS on `gh pr checks <pr> --watch`, and NOTHING else.** Its **ONLY** job is to block, so that **its completion becomes a wake**. | It **NEVER** fetches, **NEVER** writes `ci-<pr>-<head_sha>.txt`, and **NEVER** produces evidence of any kind. |
| **The wake** | **FETCHES** (SHA-pinned, both families), **PROMOTES** atomically, **VERIFIES** the stamp, **PARSES**, and **DECIDES** `ci`. | — |

**WHY the fetch cannot live in the background task:** the fetch must be pinned to the `head_sha` **the
LEDGER currently holds**, and **only the wake knows that**. A background task that fetched at its own
completion time would pin to whatever SHA *it* saw and could **promote an artifact for a SHA the ledger has
already moved past** — the exact false-green this section exists to prevent, smuggled back in through the
producer. **A watch completion yields a WAKE, never an artifact.**

**NEVER derive CI from `gh pr checks`.** Its output **carries no SHA at all** (`--json headSha` →
*Unknown JSON field*), so you can never prove which commit it describes — right after a push it can
report the **previous** commit's passing checks, and the ledger records a **green for code the PR no
longer contains**. This produced a false green on a live run in this repo, found by dogfooding rather
than by review. Use `gh pr checks --watch` to **wait**; never to decide.

#### FETCH — pinned to the SHA, paginated, BOTH check families, and emitted as JSONL

A source you never queried reports nothing, and "nothing" parses as "nothing wrong". Read **both**:

```sh
# (1) CHECK RUNS — pinned to <head_sha> BY THE URL, and the only source of a check-run verdict.
#     ONE object carries the commit, the IDENTITY (name + app_id + id) AND the RESULT (status +
#     conclusion), so no check-run judgment is ever joined across two fetches. `id` is `details_url`, the
#     CROSS-SOURCE identity the containment test below compares on — REST `.details_url` and rollup
#     `.detailsUrl` are the SAME VALUE. REST status/conclusion are lowercase -> upcased.
#     `sha` comes from GITHUB'S OWN `.head_sha` on each row — NEVER a literal we substitute in.
gh api --paginate "repos/<owner>/<repo>/commits/<head_sha>/check-runs" \
  --jq '.check_runs[] | {row:"checkrun", sha:.head_sha, name:.name, app_id:(.app.id|tostring),
                         status:(.status|ascii_upcase),
                         conclusion:((.conclusion // "-")|ascii_upcase),
                         id:(.details_url // "-")}'

# (2) COMMIT STATUSES — the legacy family, which (1) CANNOT SEE.
#     The response carries the commit ONCE, at the TOP LEVEL (`.sha`) — not on each status. Capture it
#     first and stamp GITHUB'S value onto every row; again, NEVER a literal we substitute in.
gh api --paginate "repos/<owner>/<repo>/commits/<head_sha>/status" \
  --jq '.sha as $sha | .statuses[] |
        {row:"status", sha:$sha, context:.context, state:(.state|ascii_upcase)}'

# (3) ROLLUP — WITNESSES ONLY (identity, no verdict). Used ONLY for the containment test below.
#     The rollup carries no app.id and no commit oid, so it can NEVER be read as a verdict.
gh pr view <pr> --json statusCheckRollup \
  --jq '.statusCheckRollup[]? | select(.__typename=="CheckRun") | {row:"witness", name:.name, id:.detailsUrl}'
```

Each `--jq` above emits **one compact JSON object per line** — the artifact is **JSONL**, the same
machine-read convention as `state.jsonl` and the review plan/progress files (`files-and-ledger.md`).

- **`--paginate` is MANDATORY** — `/check-runs` pages at **30**; without it you parse page one and call
  it the whole set.
- **BOTH families are MANDATORY.** A failing Jenkins/CircleCI **commit status is genuinely invisible** to
  `/check-runs`: a commit can carry **live commit statuses** while `/check-runs` reports
  `check_runs.total_count = 0` for that very commit. Read only one family and the other's failures are
  simply **absent** from your evidence — and an absence parses as "nothing wrong".
- **NEVER read the combined status `.state` as a verdict.** A commit carrying **ZERO** statuses reports
  `{"state":"pending","total_count":0}` — an absence, read as a verdict, is a lie in both directions.
  (Illustrative, and expected to drift: observed on 2026-07-13 on
  `repos/cli/cli/commits/trunk/status`. Whether *that* commit still carries zero statuses is a **live
  fact that changes**; the API's behavior **at zero** is the permanent point.)
- **Honest limit, not a proof:** `/check-runs` is capped at the **1000 most recent check suites**.
  `--paginate` defeats page-size truncation; it does **not** prove completeness at extreme scale. Say
  that, and never claim more.
- **EVERY evidence row's `sha` MUST come from the RESPONSE, NEVER from a literal you interpolate.** Both
  APIs return the commit themselves — `.head_sha` on each check run, `.sha` at the top level of the status
  response — so take it from there. Stamping `sha:"<head_sha>"` into the `--jq` filter would copy the value
  you *asked for* onto rows you have not checked, and the verify rule below would then compare that copy
  against its own source: **it could never fail**. The rows must carry what **GitHub said**, so that
  disagreeing with the ledger is *possible*.

#### PROMOTE it atomically, STAMP it with the SHA it describes

Write to a temp file **inside `<rundir>`** (same filesystem ⇒ `mv` is an atomic rename), then promote:

```sh
tmp="<rundir>/.ci-<pr>.$$"      # INSIDE <rundir>, so the mv below cannot cross a filesystem
# The header records the sha we REQUESTED — this is the ONE row whose sha is ours. Every EVIDENCE row's
# sha comes from GitHub (above), which is what makes the verify rule able to fail at all.
printf '{"row":"header","sha":"%s"}\n' "<head_sha>" > "$tmp"
#   ... append the three fetches above ...
mv "$tmp" "<rundir>/ci-<pr>-<head_sha>.txt"
```

The artifact is **JSONL: EVERY line is one JSON object, with NO exceptions** — the header included. There
is no comment line, no plain-text line, and nothing to special-case: read the file line by line and parse
each line as JSON. Four `row` types, distinguished by the `row` field — and **every EVIDENCE row
(`checkrun`, `status`) carries the SHA it is about**:

| `row` | Fields | Meaning | Whose SHA |
|---|---|---|---|
| `header` | `sha` | The `head_sha` we **REQUESTED** — what the file was fetched *for*. Exactly one, first line. | **OURS** (the ledger's) |
| `checkrun` | `sha`, `name`, `app_id`, `status`, `conclusion`, `id` | Check-run **identity AND verdict**. `conclusion` is `"-"` when absent; `id` is `details_url` (`"-"` when absent). | **GITHUB'S** (`.head_sha`) |
| `status` | `sha`, `context`, `state` | Commit-status **verdict**. | **GITHUB'S** (response `.sha`) |
| `witness` | `name`, `id` | Rollup **identity only** — **no `sha`, no verdict**. `id` is `detailsUrl`. | — (none exists) |

**The last column is the point of the whole artifact.** The `header` records what we **asked for**; every
**evidence** row carries the SHA **GitHub itself** put on that row. They come from **two different
sources**, which is the only reason comparing them can tell you anything.

**WHY JSONL, and NOT a space-delimited row: CHECK-RUN NAMES AND STATUS CONTEXTS CONTAIN SPACES.** This
repo's own two checks are named **`Lint scripts`** and **`Validate plugins`**. A positional parser handed
`checkrun <sha> Lint scripts 15368 COMPLETED SUCCESS <url>` cannot tell where the name ends and the next
field begins — it reads name=`Lint`, app_id=`scripts`, and **every** rule below (SHA verification,
containment, DECIDE) then reads garbage out of shifted fields. In JSON a value containing spaces is just a
string. **A machine-read artifact must NEVER require guessing where a field ends.**

**`witness` rows are IDENTITY-ONLY, SHA-LESS, and NEVER a verdict.** They exist for **one** purpose: the
containment test below. **NEVER write a SHA onto a witness row** — the rollup **carries no commit oid at
all**, so any SHA on that row would be one *we* invented, not one the API vouched for: **fabricated
evidence**. Their SHA-lessness is exactly **WHY** they can never be read as evidence about a commit, and
why they are exempt from the verify rule instead of being patched into it.

The `id` on a witness row (the rollup's `detailsUrl`) is **not** a SHA and not a verdict: it is the
**cross-source identity** the containment test counts on. It is safe to carry precisely because it is
inert — nothing reads a result off it.

**If ANY fetch fails, the snapshot is NOT EVIDENCE.** `--paginate` leaves **partial output on disk** when
it dies mid-run, and an error body lands in the redirect target. A failed or partial fetch → `ci =
pending`, refetch — **NEVER** parse it, and **NEVER** promote it.

#### VERIFY THE STAMP BEFORE PARSING

**THE PRINCIPLE — A STAMP YOU WROTE YOURSELF IS NOT EVIDENCE.** Verification compares a value **the
SOURCE produced** against the value **you expected**. Comparing your own literal to your own literal is a
**tautology dressed as a check**: it passes unconditionally, including on a snapshot fetched for the
**wrong commit**. That is precisely the *"assumption treated as observation"* failure this whole section
exists to prevent — **NEVER** build the check out of your own input.

So the check has force **only** because the `checkrun`/`status` rows carry **GitHub's OWN** SHA
(`.head_sha` / the status response's top-level `.sha`) while the ledger's `head_sha` is **ours**: two
independent sources, so they **CAN** disagree — and if the snapshot describes a superseded commit, they
**WILL**.

Parse the file **only** if the `header` row's `.sha`, **every `checkrun` and `status` row's `.sha`**,
**and** the filename all equal the ledger's current `head_sha`. **`witness` rows are EXEMPT** — they carry
**no `sha` field at all** and no verdict. Any mismatch means the snapshot describes a **superseded commit**
→ discard it, `ci = pending`, refetch. **NEVER** green off it, and never "fix up" the mismatch. A line that
**does not parse as JSON** is a corrupt snapshot — treat it exactly like a failed fetch: `ci = pending`,
refetch.

The `header` and the filename are **ours**, so checking them catches only a *misfiled* artifact (a stale
file left in `<rundir>`). The **evidence rows** are what catch a **wrong-commit fetch** — they are the
part of this rule that can actually fail. **If you ever find yourself writing the ledger's `head_sha` onto
an evidence row, you have deleted the verification**, not implemented it.

**The ledger write is GATED ON the parsed contents.** A guard that runs *beside* the write is not a
guard.

#### CROSS-FETCH AGREEMENT — containment on a USABLE `.id`, NOT equality

The fetches are taken at different times, so they can disagree. But the correct test is **containment**,
not equality — compared on the **per-run identity**, and **only** when that identity can actually tell two
runs apart:

> **FIRST, the identity must be USABLE — a NULL or DUPLICATED witness identity is UNVERIFIABLE, never
> "fine".** If **any** `witness` row's `.id` is **null/absent** (`"-"`), **or** two `witness` rows share
> the **SAME** `.id`, the containment test **CANNOT prove REST saw everything** → `ci = pending`,
> refetch/escalate — **NEVER green off it**. Only when every witness `.id` is **non-null and unique** does
> the test below mean anything.
>
> **THEN: REST ⊇ rollup-witnesses over the `.id` field.** The identity of a check run is its
> **`details_url`**, carried as `.id` on **both** row types (REST `.details_url` ≡ rollup `.detailsUrl` —
> the **same value**, carrying the Actions job id). **Every** `witness` row's `.id` must appear among the
> `checkrun` rows' `.id`s. If **any** does not → the REST read is **missing evidence** → `ci = pending`,
> refetch. A **REST-only** row is **FINE** — it can only *add* evidence and cannot hide a failure, because
> the `checkrun` row carries **identity AND verdict in the same row**.

**WHY the guard fails CLOSED — an identity that cannot distinguish two runs cannot prove one of them was
seen.** Counting occurrences is **not** a substitute for a usable identity: if two witnesses share id `A`,
REST can return one row genuinely matching `A` plus a **different, REST-only** row that also carries `A`,
and `count_checkrun(A) = 2 >= count_witness(A) = 2` **PASSES** while a witnessed run is in fact **MISSING**
— the extra REST row silently *compensates* for it, and the compensation is invisible. So a degenerate
identity is treated as **"cannot tell"**, never as **"agrees"**. In practice GitHub Actions gives each job
a **unique** `details_url` (it carries the job id), so this rarely fires — but **"rarely" is not "never"**,
and a containment test that silently degrades is **worse** than one that says it cannot tell.

**NEVER compare on NAME — CHECK-RUN NAMES ARE NOT UNIQUE.** Matrix jobs and reusable workflows routinely
emit **many** check runs sharing **one** name, so a name is **not an identity**. (Illustrative, and
expected to drift: a live `Homebrew/homebrew-core` commit (`1f672559`) carried, when observed on
2026-07-13, **dozens** of check runs named `status-check`, and dozens more sharing the names `merge` and
`comment`. Those are **live counts on an active commit** — they change; the **CLAIM** is what is
permanent, never the numbers.) A set-of-names test **cannot see a missing duplicate**: if the rollup holds
*n* rows named `comment` and the REST read returned only **1**, `{comment} ⊆ {comment}` **PASSES** while
REST is silently short *n−1* runs — **any of which could be the failing one**. That run then never reaches
the DECIDE rules and the snapshot **greens on incomplete evidence** — the exact defect this whole section
exists to prevent, reproduced inside the guard meant to catch it. **Comparing on the per-run identity is
what closes it** — and the null/duplicate guard above is what keeps that identity meaningful.

**NEVER require the two sets to be EQUAL — that never terminates.** GitHub's rollup **omits
`dynamic`-event check suites BY DESIGN**: REST can legitimately return a check run — a
`copilot-pull-request-reviewer` run is one — that the rollup **does not list at all**, and it stays that
way **across refetches**. (Illustrative, and expected to drift: observed on 2026-07-13 on
`microsoft/vscode` PR #325532, where the REST read held exactly that run and the rollup did not. The
**CLAIM** is what is permanent, never the counts on either side.) That asymmetry is **by design, NOT
motion**, so "sets differ → pending, refetch" spins forever — which is precisely why a **REST-only** row
is **FINE**. This repo ships `gauntlet:copilot-address-reviews`, so its users are exactly the affected
ones.

#### DECIDE from the verified file's contents

**KNOWN GAP — these three bullets are NOT an exhaustive mapping.** The conclusion set below is **carried
over unchanged from `main`** and is **known to be incomplete**: `SKIPPED`, `NEUTRAL`, `STARTUP_FAILURE`
and `STALE` are real `CheckConclusionState` values (live `completed`/`skipped` runs exist on
`nodejs/node`) and a `COMPLETED` check run holding one of them matches **none** of the three rules. This
change replaces **where the evidence comes from**, not **what it means** — it deliberately neither
regresses nor improves the classification. The total classification over the real enum lands in the
**next PR in this series**. **NEVER read these bullets as complete.**

**KNOWN GAP — THE REGISTRATION GAP: `green` here does NOT mean the required set passed.** `main`'s green
rule also demanded that "**the expected checks are actually present**". This change does **NOT** carry that
requirement forward, and that omission is a **deliberate, disclosed** one — not an oversight, and **not** a
claim that the risk is gone:

- **Why it is dropped: `main`'s version was UNIMPLEMENTABLE.** `main` names **no mechanism** for knowing
  what checks are *expected* — it demands a comparison against a set it never defines. The rule below is an
  **honest restatement of what the evidence can actually deliver**, not a weakening dressed up as one.
- **THE RISK IS REAL AND IT IS NAMED.** Where required checks exist but have **not registered yet** on the
  `head_sha`, a snapshot holding **one** registered, successful check derives **green** — and campaign can
  merge over a required check it **never saw**. `green` here means **ONLY**: *"every check that had
  registered by the time we looked had passed."* It does **NOT** mean the required set passed, and it does
  **NOT** mean the required set is complete.
- **NEVER claim more than the registration gap allows when reporting a green.** Not in the ledger, not in
  the report, not to the user. `green` is a statement about **what was observed**, never about what was
  **expected**.
- **The mechanism that closes it lands in the `required-set` PR later in this series** — it reads the base
  branch's required checks from **branch protection + rulesets**, records `required_set` as
  DECLARED / NONE DECLARED / CANNOT READ, and makes `green` require **every declared required check to be
  present AND passing**. Until that lands, this gap is **open**.

Read verdicts from the JSON fields: `.status` and `.conclusion` on `checkrun` rows, `.state` on `status`
rows. The `header` and `witness` rows hold **no verdict** and are never consulted here. "Rows" below means
**evidence rows** — `checkrun` + `status`; the `header` row does not count toward any of them.

- **green** → the snapshot lists **≥1 evidence row**; **every** `checkrun` row has `.status` `COMPLETED`
  and `.conclusion` `SUCCESS`; **every** `status` row has `.state` `SUCCESS`; and containment holds **on a
  usable identity** (every `witness` `.id` non-null and unique).
  **Zero evidence rows is NOT green** — it means nothing has registered yet. This bullet is subject to the
  **registration gap** above: it proves only that **what had registered** passed, **never** that the
  required set is complete.
- **pending** → no usable snapshot (any fetch failed, the file is absent, a line does not parse as JSON, a
  `.sha` does not match, or containment **cannot be established** — it fails, **or** a `witness` `.id` is
  null/duplicated so the test proves nothing), zero evidence rows, or any `checkrun` row whose `.status`
  is not yet `COMPLETED` / any `status` row whose `.state` is `PENDING` → leave `ci = pending` and, if the
  watch task has exited, **relaunch it in this same wake** — a pending PR must never sit unwatched waiting
  for the heartbeat.
- **red** → any `checkrun` row whose `.conclusion` is `FAILURE` / `TIMED_OUT` / `CANCELLED` /
  `ACTION_REQUIRED`, or any `status` row whose `.state` is `FAILURE` or `ERROR` (**`ERROR` is a failure** —
  never shrug it off as a glitch).

  **If the PR is PARKED** (`status` = `awaiting-user` / `awaiting-api`), record `ci = red` and
  **dispatch NO fix** — a parked PR dispatches nothing until the user answers (`loop-control.md` step 3).
  The **watch keeps running** either way: watching is observation, not work-dispatch, so a parked PR's CI
  state stays fresh. Otherwise → any failing row → **stop any review pass in flight on that PR first** (Loop control
  step 3 — the fix will replace its SHA, so the verdict is already void; free the slot), then
  **CLASSIFY the failure** from the check logs ("Classify, then set the model" below) **before
  dispatching anything**, and dispatch a **scoped CI-fix subagent** into `<worktree>` — the PR row's
  ledger `worktree` column value, the single source of truth for this PR's checkout path (created at
  adoption/pre-review per `pr-adoption.md`; default `.worktrees/<headRefName>` when campaign creates
  it, else a reused existing checkout). Its fix commits + pushes to the PR's **own head branch** →
  **apply the gate reset** below.

#### Any campaign commit to the PR head resets the gate

**THE RULE — every commit campaign pushes to a PR's head branch is a PR-content change, whatever wrote
it: a cheap CI-fix subagent, a session-model CI-fix subagent, a review-fix subagent, or an inline
REFUTATION of a review finding (`stage-2-review-gate.md`, "Audit every finding before you fix it").**
Every one of them MUST, in the same step:

- **reset `reviews_ok` to 0 AND restore `gauntlet-reviewing` if the PR carries `gauntlet-accepted`**
  (`gh pr edit <pr> --remove-label gauntlet-accepted --add-label gauntlet-reviewing`) — the gate and its
  label move together, never one without the other (`stage-2-review-gate.md`, "Status labels mirror the
  review gate");
- **relaunch the CI watch immediately**;
- **re-enter Stage 2a.**

The verdicts on the old SHA describe content that no longer exists, and a `gauntlet-accepted` label on
it is a false public claim. NEVER exempt a commit because it "only reformatted".

#### Classify, then set the model — never dispatch straight off a red check

**Set the model EXPLICITLY on every dispatch** (`SKILL.md`, "Subagent Dispatch"). An unset model
silently inherits the session model — a cost decision taken by default. Classify the failure from the
check logs FIRST; the class picks the model:

| Failure class | Model | Why |
|---|---|---|
| **Formatting / lint** — the fix is exactly what a standard formatter or autofixer produces | **`sonnet`** (**`haiku`** only when the failure is trivially mechanical) | It does NOT author a fix from scratch: it runs a deterministic tool, **READS the resulting diff**, verifies it, and **escalates** anything it cannot verify. Downgraded **on purpose**. |
| **Everything else** — failing product test, compile error, flake, anything needing judgment — **and every escalation from the cheap subagent** | **session model** | It authors code that gets merged, and nothing downstream validates it. |

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
   It **chooses the tool** — campaign does not hand it a command line — subject to the hard rules below,
   and it **PREFLIGHTS every file** before formatting it (hard rules: symlink / symlinked parent).
3. **READ THE RESULTING DIFF.** This step is not optional and is not a formality. Verify **all** of:
   - the diff contains **ONLY** what the fix should have produced (a formatting fix produces formatting);
   - **no file it did not intend to touch** was touched;
   - **no check definition, config, or test was weakened**;
   - **re-running the exact failing check now PASSES.**
4. **COMMIT** only if every one of those holds — then apply the gate reset above.
5. **ESCALATE, never patch.** If the check still fails, the diff contains anything it cannot explain, it
   needed to change product logic, or it cannot verify the result → **STOP**, commit nothing, reset the
   worktree to the PR head, and hand the failure to a **session-model** CI-fix subagent. **Escalation is
   the correct outcome, not a failure** — it is what the tier is for.

**HARD RULES — give these to the cheap subagent VERBATIM in its prompt:**

- **NEVER make CI pass by weakening the check.** NEVER delete or loosen an assertion, NEVER add
  `skip`/`xfail`, NEVER disable or downgrade a lint rule, NEVER raise a timeout. **Fix the cause.** If the
  check itself is demonstrably wrong, **say so explicitly and ESCALATE** — never silently rewrite it.
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
- **PREFLIGHT EVERY FILE BEFORE FORMATTING IT — refuse it if the write can land outside the worktree:**
  - it **IS a symlink** (`lstat`, never `stat`);
  - **ANY directory component of its path is a symlink**.

  Refuse = **do not format that file**; log it; carry on with the rest. If nothing is left to format,
  **ESCALATE**.

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
verify**; and **every commit campaign pushes is still gated by the full review gauntlet** — any campaign
commit to the PR head resets `reviews_ok` to 0, restores `gauntlet-reviewing`, relaunches the CI watch,
and re-enters Stage 2a on the session model.

This trades a **small, bounded risk** for a workflow that is **cheaper AND more capable** than either a
full-strength subagent on every formatting failure or a hermetic no-model tool path. **The user accepts
that trade.**

**NEVER claim the cheap path is safe because "CI will catch it" or "the review gate will catch it."** CI
tells you a check passed, never that the fix is right; the review gate is a miss-catcher, not a proof of
correctness (`stage-2-review-gate.md`). Say that plainly whenever the question comes up.

---

Every CI failure must be handled; never merge over a red or pending check, and never infer green from
the watch's exit code — always confirm against a **SHA-pinned, SHA-verified** snapshot of **both** check
families.

CI fixes serialize only within one PR/SHA. Different PRs with red CI may run scoped CI-fix subagents
concurrently within the dispatcher cap.

---
