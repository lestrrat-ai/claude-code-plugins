### 2b. CI (event-driven)

Each PR has a background task that waits on `gh pr checks --watch`. **The watch only BLOCKS — it is
never evidence.** When the task completes, a wake **fetches a fresh snapshot pinned to the PR's current
`head_sha`**, verifies it, and decides `ci` **from the snapshot's contents — NEVER from the watch's exit
code** — then writes the `ci`/`reviews_ok` result through `scripts/ledger.py … set --pr <N> --ci <state>
[--reviews_ok 0]` **by field name** (`files-and-ledger.md`), never by hand-editing the row by column
position.

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
gh api --paginate "repos/<owner>/<repo>/commits/<head_sha>/check-runs" \
  --jq '.check_runs[] | {row:"checkrun", sha:"<head_sha>", name:.name, app_id:(.app.id|tostring),
                         status:(.status|ascii_upcase),
                         conclusion:((.conclusion // "-")|ascii_upcase),
                         id:(.details_url // "-")}'

# (2) COMMIT STATUSES — the legacy family, which (1) CANNOT SEE.
gh api --paginate "repos/<owner>/<repo>/commits/<head_sha>/status" \
  --jq '.statuses[] | {row:"status", sha:"<head_sha>", context:.context, state:(.state|ascii_upcase)}'

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
  `/check-runs`: a Kubernetes commit carrying **2 live statuses** reports `check_runs.total_count = 0`.
- **NEVER read the combined status `.state` as a verdict.** It reports **`pending` at ZERO statuses**
  (`repos/cli/cli/commits/trunk/status` → `{"state":"pending","total_count":0}`) — an absence, read as a
  verdict, is a lie in both directions.
- **Honest limit, not a proof:** `/check-runs` is capped at the **1000 most recent check suites**.
  `--paginate` defeats page-size truncation; it does **not** prove completeness at extreme scale. Say
  that, and never claim more.

#### PROMOTE it atomically, STAMP it with the SHA it describes

Write to a temp file **inside `<rundir>`** (same filesystem ⇒ `mv` is an atomic rename), then promote:

```sh
tmp="<rundir>/.ci-<pr>.$$"      # INSIDE <rundir>, so the mv below cannot cross a filesystem
printf '{"row":"header","sha":"%s"}\n' "<head_sha>" > "$tmp"
#   ... append the three fetches above ...
mv "$tmp" "<rundir>/ci-<pr>-<head_sha>.txt"
```

The artifact is **JSONL: EVERY line is one JSON object, with NO exceptions** — the header included. There
is no comment line, no plain-text line, and nothing to special-case: read the file line by line and parse
each line as JSON. Four `row` types, distinguished by the `row` field — and **every EVIDENCE row
(`checkrun`, `status`) carries the SHA it is about**:

| `row` | Fields | Meaning |
|---|---|---|
| `header` | `sha` | The `head_sha` the whole file describes. Exactly one, first line. |
| `checkrun` | `sha`, `name`, `app_id`, `status`, `conclusion`, `id` | Check-run **identity AND verdict**. `conclusion` is `"-"` when absent; `id` is `details_url` (`"-"` when absent). |
| `status` | `sha`, `context`, `state` | Commit-status **verdict**. |
| `witness` | `name`, `id` | Rollup **identity only** — **no `sha`, no verdict**. `id` is `detailsUrl`. |

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

Parse the file **only** if the `header` row's `.sha`, **every `checkrun` and `status` row's `.sha`**,
**and** the filename all equal the ledger's current `head_sha`. **`witness` rows are EXEMPT** — they carry
**no `sha` field at all** and no verdict. Any mismatch means the snapshot describes a **superseded commit**
→ discard it, `ci = pending`, refetch. **NEVER** green off it, and never "fix up" the mismatch. A line that
**does not parse as JSON** is a corrupt snapshot — treat it exactly like a failed fetch: `ci = pending`,
refetch.

**The ledger write is GATED ON the parsed contents.** A guard that runs *beside* the write is not a
guard.

#### CROSS-FETCH AGREEMENT — MULTISET containment on `.id`, NOT equality

The fetches are taken at different times, so they can disagree. But the correct test is **containment**,
not equality — and it is compared on the **identity**, **counting occurrences**:

> **REST ⊇ rollup-witnesses, as MULTISETS over the `.id` field.** The identity of a check run is its
> **`details_url`**, carried as `.id` on **both** row types (REST `.details_url` ≡ rollup `.detailsUrl` —
> the **same value**, carrying the Actions job id). For **every** identity, require
> `count_checkrun(id) >= count_witness(id)`. If **any** `witness` row's `.id` appears **FEWER** times among
> the `checkrun` rows than among the `witness` rows → the REST read is **missing evidence** → `ci =
> pending`, refetch. A **REST-only** row is **FINE** — it can only *add* evidence and cannot hide a
> failure, because the `checkrun` row carries **identity AND verdict in the same row**.

**NEVER compare on NAME, and NEVER compare as a SET — CHECK-RUN NAMES ARE NOT UNIQUE.** Matrix jobs and
reusable workflows routinely emit many runs sharing one name: a live `Homebrew/homebrew-core` commit
(`1f672559`) carries **16** check runs named `status-check`, **15** named `merge`, and **7** named
`comment`. A set-of-names test **cannot see a missing duplicate**: if the rollup holds 7 rows named
`comment` and the REST read returned only 1, `{comment} ⊆ {comment}` **PASSES** while REST is silently
short **6** runs — **any of which could be the failing one**. That run then never reaches the DECIDE rules
and the snapshot **greens on incomplete evidence** — the exact defect this whole section exists to
prevent, reproduced inside the guard meant to catch it. **Counting is what closes it.**

**Count occurrences — do NOT assume the identity is a key.** `details_url` is not *guaranteed* unique for
non-Actions apps, which is **why** the rule compares multiplicities rather than treating the identity as a
primary key. The multiset test is correct **either way**: if the identity is unique the counts are all 1
and it degrades to the set test; if it is not, it still detects the missing duplicate.

**NEVER require the two sets to be EQUAL — that never terminates.** GitHub's rollup **omits
`dynamic`-event check suites BY DESIGN**: on `microsoft/vscode` PR #325532, REST returns **37** runs
including `copilot-pull-request-reviewer` and the rollup returns **36**, omitting it — **stably, across
refetches**. That is a by-design asymmetry, **not motion**, so "sets differ → pending, refetch" spins
forever. This repo ships `gauntlet:copilot-address-reviews`, so its users are exactly the affected ones.

#### DECIDE from the verified file's contents

**KNOWN GAP — these three bullets are NOT an exhaustive mapping.** The conclusion set below is **carried
over unchanged from `main`** and is **known to be incomplete**: `SKIPPED`, `NEUTRAL`, `STARTUP_FAILURE`
and `STALE` are real `CheckConclusionState` values (live `completed`/`skipped` runs exist on
`nodejs/node`) and a `COMPLETED` check run holding one of them matches **none** of the three rules. This
change replaces **where the evidence comes from**, not **what it means** — it deliberately neither
regresses nor improves the classification. The total classification over the real enum lands in the
**next PR in this series**. **NEVER read these bullets as complete.**

Read verdicts from the JSON fields: `.status` and `.conclusion` on `checkrun` rows, `.state` on `status`
rows. The `header` and `witness` rows hold **no verdict** and are never consulted here. "Rows" below means
**evidence rows** — `checkrun` + `status`; the `header` row does not count toward any of them.

- **green** → the snapshot lists **≥1 evidence row**; **every** `checkrun` row has `.status` `COMPLETED`
  and `.conclusion` `SUCCESS`; **every** `status` row has `.state` `SUCCESS`; and containment holds.
  **Zero evidence rows is NOT green** — it means nothing has registered yet.
- **pending** → no usable snapshot (any fetch failed, the file is absent, a line does not parse as JSON, a
  `.sha` does not match, or containment fails), zero evidence rows, or any `checkrun` row whose `.status`
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

**Scope every CI-fix subagent, both tiers:** give it the failing check's logs, the specific failing
file(s), and the worktree path. Tell it **NOT** to re-derive the whole diff or read beyond what the
failure touches.

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
