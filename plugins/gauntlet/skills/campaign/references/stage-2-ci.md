### 2b. CI (event-driven)

Each PR has a background task that waits on `gh pr checks --watch`. The watch only **blocks**; it is
never the source of truth. When the task completes, a wake derives `ci` and writes the `ci`/`reviews_ok`
result through `scripts/ledger.py … set --pr <N> --ci <state> [--reviews_ok 0]` **by field name**
(`files-and-ledger.md`), never by hand-editing the row by column position.

**Derive `ci` from BOTH check families, PINNED TO `head_sha` — never from an unpinned PR-level snapshot.**
`gh pr checks <pr>` is **not pinned to a commit**: right after a push, the new commit's checks have not
registered yet, so it happily returns the **previous** commit's passing checks. Reading that snapshot
records a green that belongs to code the PR no longer contains — a false green on a SHA nothing ever
tested. The ledger pins `ci` to `head_sha`, so the query that fills it MUST pin too:

#### TWO CHECK FAMILIES — reading one of them is reading HALF the evidence

**GitHub reports CI through TWO INDEPENDENT APIs, and a failure in either one blocks nothing you cannot
see:**

- **Checks** — check runs (`/commits/<sha>/check-runs`). GitHub Actions, most modern Apps.
- **Commit statuses** — the legacy status API (`/commits/<sha>/status`). **Jenkins, CircleCI, Buildkite,
  Travis, and a large fraction of third-party bots still report here.**

**A failing commit status is COMPLETELY INVISIBLE to the check-runs endpoint.** One Actions check run
succeeds, one Jenkins commit status **FAILS** → a check-runs-only snapshot is **nonempty and all-success**
→ `ci = green`. **FALSE.** Deriving CI from `/check-runs` alone does not read a partial set of the
evidence; it leaves **AN ENTIRE FAMILY UNREAD.**

**The snapshot MUST cover BOTH families.** `gh pr view <pr> --json headRefOid,statusCheckRollup` returns
both in **ONE** response, and `headRefOid` comes from the **same payload** — so the SHA stamp and the rows
it stamps cannot disagree. Each `statusCheckRollup[]` entry is discriminated by `__typename`:

| `__typename` | Family | Name field | Result field | Result values |
|---|---|---|---|---|
| `CheckRun` | Checks | `.name` | `.conclusion` (with `.status`) | `.status`: `QUEUED`/`IN_PROGRESS`/`COMPLETED`; `.conclusion`: `SUCCESS`/`FAILURE`/`TIMED_OUT`/`CANCELLED`/`ACTION_REQUIRED`/… (null until completed) |
| `StatusContext` | Commit statuses | `.context` | `.state` (there is **no** `.conclusion`) | `SUCCESS`/`FAILURE`/`PENDING`/`ERROR` |

**These are UPPERCASE** (GraphQL enums), unlike the REST endpoints' lowercase `completed`/`success`. A
`StatusContext` has **NO `.status`/`.conclusion` pair** — it has a single `.state`, and `ERROR` is a
**failure**, not a curiosity. **Never write a parser that reads `.conclusion` on every row: a
`StatusContext` yields `null` there, and a `null` conclusion that gets treated as "not a failure" is
exactly the false green this section exists to prevent.**

**NEVER derive the legacy family's verdict from the COMBINED status `state`** (`/commits/<sha>/status`'s
top-level `.state`). It reads **`pending` when there are ZERO statuses** — indistinguishable from
"statuses are still running". It is not a verdict: **the only safe reading of a combined `pending` is
`pending`, NEVER green.** Enumerate the individual contexts (`statusCheckRollup`'s `StatusContext` rows,
or `.statuses[]`) and judge each one; a family with **zero** contexts contributes nothing, which is not
the same as a family that is pending.

**The snapshot is SHA-SCOPED — `ci-<pr>-<head_sha>.txt` — and NAMES ITS SHA in its first line.** A
shared, SHA-less `ci-<pr>.txt` is a false-green machine: a watch launched for SHA **A** can finish
**after** the PR has advanced to SHA **B** (campaign pushes fixes constantly; every commit resets the
gate and moves the head). It fetches A's checks correctly, writes them into the shared file, and the
consumer parses them against the ledger's current `head_sha` (**B**) — recording **green for B on A's
checks**. Pinning the QUERY is not enough: the **ARTIFACT** must carry the SHA it describes, in its
**name** and in its **contents**.

**Fetch ATOMICALLY — NEVER redirect a fetch straight onto the snapshot the parser reads:**

```
tmp="$(mktemp -p <rundir>)"        # temp INSIDE <rundir>: mv is an atomic rename only WITHIN a filesystem
# BOTH families + the SHA they belong to, out of ONE payload. The '# sha:' line is emitted by the SAME
# query that emits the rows, so the stamp can never describe a different commit than the rows do.
if gh pr view <pr> --json headRefOid,statusCheckRollup --jq '
      "# sha: \(.headRefOid)",
      (.statusCheckRollup[] |
        if .__typename == "CheckRun"
        then "checkrun\t\(.name)\t\(.status)\t\(.conclusion)"
        else "status\t\(.context)\t\(.state)" end)' > "$tmp"; then
  mv "$tmp" <rundir>/ci-<pr>-<head_sha>.txt   # complete, SHA-stamped — safe to parse
else
  rm -f "$tmp"                     # partial/failed fetch — NOT evidence of anything
  # ci = pending; relaunch the watch; NEVER parse a partial snapshot, NEVER green
fi
```

`<head_sha>` in the FILENAME is the SHA the watch was launched for; the `# sha:` line **inside** is the
SHA GitHub says the head is **now**. The consumer checks **both** against the ledger's current `head_sha`
(below). They disagree exactly when the head moved under the watch — **expected**, and it means
**discard**, never green.

**PRODUCER IDENTITY — the rollup carries no `app.id`.** If (and only if) the declared required-check set
carries an **app binding** (`app_id` / `integration_id` — see "Prove the expected checks registered"),
**also** fetch the SHA-pinned check runs **with their producer**, appending into the **same** temp file so
it is promoted by the **same** atomic `mv`:

```
gh api --paginate repos/<owner>/<repo>/commits/<head_sha>/check-runs \
  --jq '.check_runs[] | "app\t\(.name)\t\(.app.id)"' >> "$tmp"
```

**BOTH fetches must exit 0** before the snapshot is promoted — a failure in either one is a partial
snapshot, which is **NOT EVIDENCE**. If an app-bound required check's producer cannot be resolved from
the promoted snapshot, its presence is **NOT PROVEN** → `ci = pending`, **NEVER green**.

**`mktemp -p <rundir>` is REQUIRED — NEVER plain `mktemp`.** Plain `mktemp` lands in `$TMPDIR`
(usually `/tmp`), typically a **different filesystem** from `<rundir>` (which lives in the repo tree).
`mv` is an **atomic rename only within one filesystem**; across filesystems it degrades to
**copy + unlink**, so the destination is **visible mid-copy** — precisely the partial-file state the
temp-file dance exists to prevent. A temp file in `<rundir>` makes the promotion a same-directory
rename. (It also obeys the project rule: scratch goes under `<rundir>`, **NEVER** `/tmp`.)

**Any fetch writes AS IT GOES** — `gh --paginate` streams each page as it arrives, and a single response
streams as it downloads. `> <rundir>/ci-<pr>-<head_sha>.txt` truncates the snapshot and then streams into
it, so a fetch that dies partway — network error, rate limit, timeout, SIGTERM — leaves **the rows that
already arrived on disk**. Parse that and you read a nonempty, all-success file and record **green for a
SHA whose CI was never fully observed**. The temp file + `mv` makes the snapshot appear **only when every
fetch completed**.

**`--paginate` is REQUIRED on every REST fetch — NEVER drop it.** `gh api` fetches **one page** unless
told otherwise, and the check-runs endpoint pages at **30**. A commit with more check runs than one page —
any matrix build — puts the rest on page 2, and a rule that says "**every** run passed" then evaluates
only the runs it can SEE. A failing or pending run on page 2 is invisible.

**The rollup is a BOUNDED WINDOW too — prove you saw ALL of it.** `statusCheckRollup` is a GraphQL
connection that `gh` requests with a fixed cap (currently the **last 100** contexts). A PR whose combined
check-run + status-context count reaches that cap gets a **silently TRUNCATED** snapshot — the dropped
`--paginate` defect wearing a different hat. **If the rollup comes back AT the cap, the set is not proven
complete → `ci = pending`, NEVER green.** Fall back to the paginated REST endpoints for **BOTH** families
— `/commits/<head_sha>/check-runs` **and** `/commits/<head_sha>/status`, enumerating `.statuses[]` (REST
values are **lowercase**; **never** read the combined top-level `.state`) — and stamp `# sha: <head_sha>`
onto the result as before.

#### VERIFY THE SNAPSHOT'S SHA BEFORE PARSING IT

**NEVER parse a snapshot you cannot prove belongs to the current head.** Before reading any check rows:

1. Read the ledger's current `head_sha` for the PR.
2. Open `<rundir>/ci-<pr>-<head_sha>.txt` and confirm its `# sha:` line equals that `head_sha`.
3. **Missing file, or a `# sha:` line that does not match → `ci = pending`, relaunch the watch, NEVER
   green.** The snapshot describes a commit that no longer matters. **Discard it. NEVER parse it.**

**A watch that finishes for a superseded SHA is NOT an error — it is EXPECTED.** The head advanced under
it. Its result is simply **discarded**; the relaunched watch observes the current head. NEVER "fix" this
by trusting the stale result, and NEVER treat the mismatch as a bug to work around.

#### PROVE THE EXPECTED CHECKS REGISTERED — a nonempty snapshot is not a COMPLETE one

**"Every run in the snapshot passed" proves every **REGISTERED** run passed. It does NOT prove every
**EXPECTED** check has **REGISTERED**.** Checks register **ASYNCHRONOUSLY**: a 5-second lint can complete
and register before a slower workflow — or a GitHub App check — even **EXISTS**. A snapshot taken at that
moment is pinned, paginated, atomic and SHA-stamped, and **STILL WRONG**: nonempty, all-success → green,
while the check that would have failed **has not appeared yet**. We already reject the ZERO case; this is
the SOME case.

The **WATCH cannot supply the missing proof** — it only **BLOCKS**, it is **NEVER evidence**.

**When the repo DECLARES required checks, that declaration IS the expected set. READ IT — from BOTH
places it can live.** A repo may use classic branch protection, rulesets, or **both**; neither endpoint
sees the other's declarations:

```
# (a) classic branch protection. REQUIRES the token to have Administration: read.
gh api repos/<owner>/<repo>/branches/<base>/protection/required_status_checks \
  --jq '.checks[] | "\(.context)\t\(.app_id)"'      # app_id is null when unbound

# (b) rulesets. Readable WITHOUT admin — and the classic endpoint CANNOT SEE these.
gh api repos/<owner>/<repo>/rules/branches/<base> \
  --jq '.[] | select(.type=="required_status_checks")
             | .parameters.required_status_checks[] | "\(.context)\t\(.integration_id)"'
```

The expected set is the **UNION** of both. **Read both — always.** Prefer (b) precisely because it needs
no admin: a token that cannot read (a) can still read (b), and a required check declared only in a
ruleset is invisible to (a) at **any** permission level.

##### "I CANNOT SEE ANY" IS NOT "THERE ARE NONE" — three states, NEVER two

**A 404 from the classic protection endpoint does NOT mean "not declared."** That endpoint 404s **BOTH**
when the branch is unprotected **AND** when the token lacks **Administration: read**. Collapsing those two
into "not declared" **silently downgrades to the weaker rule** and permits green with a required check
missing. **This is not hypothetical — it has already happened to an operator of this skill**, who queried
it against a repo, got 404, concluded "no branch protection", and was right **only by luck**: the repo
**did** have a ruleset, which that endpoint cannot see. Had the ruleset declared required checks, the
method would have reported "none declared."

**Classify the read into exactly one of THREE states. NEVER fold the third into the second:**

| State | How you get there | What green requires |
|---|---|---|
| **DECLARED** | (a) and/or (b) **succeeded** and the union is **non-empty** | Every required check **PRESENT** in the snapshot — **matched by producer identity** (below) — **and successful**. One that has not registered → `ci = pending`, **NEVER green**. This is the **only** configuration in which registration completeness can be **PROVEN**. |
| **NONE DECLARED** | **Provable only when the required-set read SUCCEEDED and came back EMPTY** — (b) succeeded and returned no `required_status_checks` rule, **and** (a) either succeeded-and-empty or 404'd **while (b) proves the branch's rules are readable and declare none** | There is **NO expected set**; registration completeness **CANNOT BE PROVEN** — not by campaign, not by the check-runs API, and **not by `mergeStateStatus`** (GitHub cannot block on a check it does not know about either). **SAY SO** — "The registration gap" below. |
| **CANNOT READ** | **Any** error on **either** endpoint that is not a proven-empty read: 404/403 on (a) **without** Administration: read, any error on (b), a network/rate-limit failure, a malformed response | **UNKNOWN — and UNKNOWN IS NOT "NONE DECLARED."** Do **NOT** silently fall through to the weaker rule. **Record the uncertainty on the PR row and in the report; NEVER claim registration completeness; NEVER state or imply "no required checks are declared."** A required check **may** exist and **may** be missing from the snapshot, and you cannot tell. Retry the read (it may be transient), and **prefer the rulesets endpoint — it needs no admin.** If it still cannot be read, the merge gate rests on GitHub's own `mergeStateStatus == CLEAN` alone (`stage-3-merge.md`) — which **does** know the required set — and campaign must say plainly that **it** could not verify it. |

**THE RULE, stated so it cannot be misread: an ERROR that also means "you may not look" is NEVER evidence
that there is nothing to see. NEVER infer the ABSENCE of a requirement from a failure to read it.**

##### RIGHT NAME IS NOT RIGHT PRODUCER — match the app too

**A required check can be bound to a specific APP** — `app_id` in classic branch protection,
`integration_id` in a ruleset. When it is, **the name alone does not identify it**: required check `build`
from app **123**; app **999** also reports a check named `build`, and it **succeeds**; app 123's `build`
has **not registered yet**. A name-only presence test sees `build` + success → **green, from the WRONG
PRODUCER**, while the check that actually gates the branch has not run.

- **Required check declares an app binding** (`app_id`/`integration_id` **non-null**) → the presence test
  MUST match **BOTH** the name **AND** the producer: a snapshot check run whose `.name` equals the context
  **AND** whose `.app.id` equals that `app_id`. A same-named run from any other app **DOES NOT SATISFY
  IT.** (`.app.id` comes from the SHA-pinned check-runs fetch — the rollup does not carry it. If the
  producer cannot be resolved, the check is **NOT PROVEN PRESENT** → `ci = pending`, **NEVER green**.)
- **Required check declares NO app binding** (`app_id`/`integration_id` **null**) → **ANY producer of that
  name satisfies it.** Do **NOT** over-tighten this: an unbound requirement is genuinely name-only, and
  demanding a producer match there would wedge CI at `pending` forever. Legacy **commit statuses** are
  matched by **context name** the same way.

#### SIX questions, SIX sources — never collapse them

This is where the defect came from: "never trust exit status, only contents" is right for **deciding**
green/red/pending, and WRONG as a reason to ignore whether the snapshot is COMPLETE.

- **Did we read every evidence SOURCE?** → the **SET OF FAMILIES QUERIED** decides whether an entire class
  of failures was even **capable** of appearing. **Checks AND commit statuses — both.** A source you never
  queried reports nothing, and "nothing" parses as "nothing wrong". A check-runs-only snapshot cannot see a
  failing Jenkins status **at all**.
- **Did we get all the evidence from them?** → the **FETCH's exit status** (of **every** fetch) decides
  whether the snapshot is usable **at all**. A failed or partial fetch is **NOT EVIDENCE** — it is
  `ci = pending`, relaunch the watch, and **NEVER** green. NEVER parse a partial snapshot. A rollup at its
  context cap is partial too.
- **Is the evidence about THIS commit?** → the snapshot's **`# sha:` line** (and its filename) decides
  whether it may be parsed at all. It MUST equal the ledger's current `head_sha`; if not, the snapshot
  is about a superseded commit — discard it, `ci = pending`, relaunch, **NEVER** green.
- **Do we KNOW what we expect to see?** → the **REQUIRED-SET READ's outcome** decides whether the next
  question can be answered at all. **DECLARED / NONE DECLARED / CANNOT READ — three states, never two.**
  **CANNOT READ is NOT "none declared"**: an error that also means "you may not look" is never evidence
  that there is nothing to see. Record the uncertainty; never claim completeness.
- **Is EVERYTHING WE EXPECT TO SEE THERE, FROM THE RIGHT PRODUCER?** → the **REQUIRED-CHECK SET** (classic
  protection **∪** rulesets) decides whether the snapshot is **COMPLETE AS A SET**. Every required check
  MUST be **present** — **matched on `app.id` too wherever the declaration binds an app** — and
  successful; one that has not registered → `ci = pending`, relaunch, **NEVER** green. **Where none are
  declared, this question HAS NO ANSWER** — see "The registration gap".
- **What does the evidence say?** → the **FILE's contents** decide **green / red / pending** — never a
  command's exit status.
- The **WATCH** is none of them: `gh pr checks --watch` only **blocks**. **NEVER infer green from its exit
  code** (it can exit 0 on pending/unregistered checks).

**The pattern — this is the EIGHTH instance of the same defect class in this file: green derived from
evidence that is INCOMPLETE, or ABOUT THE WRONG THING.**

1. **unpinned QUERY** → the **WRONG COMMIT's** checks;
2. **no `--paginate`** → only **PAGE 1**;
3. **partial fetch** → only the pages that **ARRIVED**;
4. **SHA-less ARTIFACT** → a snapshot for a **SUPERSEDED COMMIT**, parsed as current;
5. **UNREGISTERED CHECKS** → only the checks that had **APPEARED**, parsed as the whole set;
6. **ONE CHECK FAMILY** → only **CHECK RUNS**, with the whole legacy **COMMIT-STATUS** family unread —
   a failing Jenkins status invisible;
7. **NAME-ONLY REQUIRED-CHECK MATCH** → the **WRONG PRODUCER's** same-named check, counted as the
   required one;
8. **"404 = NOT DECLARED"** → an **UNREADABLE** required-set, read as an **EMPTY** one.

Every one produced a nonempty, all-success snapshot that was not the truth about the current head.
**Before you parse a snapshot: PROVE THE SOURCE IS COMPLETE (no unread check family), PROVE IT IS
COMPLETE (no missing page, no partial fetch), PROVE IT IS ABOUT THE COMMIT YOU CARE ABOUT, PROVE YOU KNOW
WHAT YOU EXPECT TO SEE — and PROVE THAT EVERYTHING YOU EXPECT IS THERE, WITH THE IDENTITY YOU EXPECT.**

**And the rule the eighth instance adds, which generalises past CI: NEVER INFER THE ABSENCE OF A
REQUIREMENT FROM AN ERROR THAT ALSO MEANS "YOU MAY NOT LOOK." "I cannot see any" is NOT "there are
none."**

Then decide **from the complete, SHA-verified file's contents**:

- **green** → the snapshot lists **≥1 row**; **every** `checkrun` row has `status = COMPLETED` and
  `conclusion = SUCCESS`; **every** `status` row has `state = SUCCESS`; **AND every DECLARED required
  check is present among them — matched on producer where the declaration binds an app — and successful.**
  Zero rows is **NOT** green — it means nothing has registered yet. Where **none** are declared, `green`
  means **only** *"every check that had registered by the time we looked had passed"* — read "The
  registration gap" and never claim more than that. Where the required set **CANNOT BE READ**, green
  carries the **stronger** caveat that a required check may exist and be missing, and campaign **cannot
  tell** — never silently treat it as "none declared".
- **pending** → no usable snapshot (any fetch failed, file absent, rollup at its context cap, or its
  `# sha:` does not match the ledger's current `head_sha`), zero rows listed, any `checkrun` row not yet
  `COMPLETED`, any `status` row in `PENDING`, **or any declared required check ABSENT from the snapshot
  (name **and** producer), or present but not yet finished** → leave `ci = pending` and, if the watch task
  has exited, **relaunch it in this same wake** — a pending PR must never sit unwatched waiting for the
  heartbeat.
- **red** → any `checkrun` row whose conclusion is `FAILURE` / `TIMED_OUT` / `CANCELLED` /
  `ACTION_REQUIRED`, **or any `status` row whose state is `FAILURE` or `ERROR`** (`ERROR` is a failure —
  never shrug it off as a glitch).

#### The registration gap — stated honestly, NEVER papered over

**When the repo declares NO required checks, campaign CANNOT prove that every check it should have seen
had registered when it looked.** The check-runs API reports what EXISTS; it cannot report what is still
coming. A check that registers later could still **FAIL** after `ci = green` was recorded. That is a
**RESIDUAL RISK, not a solved problem. NEVER describe it as closed.**

**Mitigations — they are MITIGATIONS, not PROOFS. NEVER present any of them as one:**

- the **watch settles first** — `gh pr checks --watch` blocks until the checks it can see have finished,
  so the snapshot is taken late, not at push time. It narrows the window; it does not close it.
- **`mergeStateStatus == CLEAN` is REQUIRED at the merge gate** (`stage-3-merge.md`) — it catches a
  check that HAS registered and is failing or still pending (with no required checks declared, a failing
  check reads `UNSTABLE`, not `CLEAN`). **It does NOT prove registration completeness**: GitHub cannot
  block on a check it does not know about either.
- **THE ACTUAL REMEDY IS THE USER'S:** declaring the checks **REQUIRED** in branch protection or a
  ruleset gives campaign an expected set to verify, and GitHub a set to block on. **That — and only
  that — closes the gap for real.** Say so when the question comes up.

**When the required set CANNOT BE READ, the gap is WIDER, not the same — and it is a DIFFERENT gap.** The
"NONE DECLARED" language above says *we know there is no expected set*. **CANNOT READ says we do not know
whether there is one.** A required check may be declared, may be missing from the snapshot, and campaign
**cannot tell** — so it must **NEVER** reuse the "none declared" wording, which would assert something it
did not observe. Record the failed read, name the endpoint and the reason (missing **Administration:
read** is the common one), and say plainly: **the expected set could not be read, so registration
completeness is not merely unproven — it is unknown.** Retry, and **prefer the rulesets endpoint, which
needs no admin.** GitHub's `mergeStateStatus == CLEAN` still applies at merge and **does** know the
required set (`stage-3-merge.md`) — lean on it, and be explicit that it is GitHub verifying that, not
campaign.

**Never write `ci = green` unconditionally after a watch returns.** The write MUST be conditional on the
parsed contents above. A guard that runs beside the write rather than gating it is not a guard — this is
a real failure mode: the watch exits, the snapshot is stale or empty, and `green` is recorded anyway.

**On `red`: FIRST, is the PR PARKED?** If `status` is `awaiting-user` or `awaiting-api`, record `ci = red`
and **dispatch NO fix** — a parked PR is FROZEN and campaign takes no action that MUTATES it until the
user answers (`loop-control.md` step 3, "parked-status guard"). The **watch keeps running** either way:
watching is **observation, not work-dispatch**, so a parked PR's CI state stays fresh while it waits.

**Otherwise:** **stop any review pass in flight on that PR first** (Loop control step 3 — the fix will
replace its SHA, so the verdict is already void; free the slot), then **CLASSIFY the failure** from the
check logs ("Classify, then set the model" below) **before dispatching anything**, and dispatch a
**scoped CI-fix subagent** into `<worktree>` — the PR row's ledger `worktree` column value, the single
source of truth for this PR's checkout path (created at adoption/pre-review per `pr-adoption.md`;
default `.worktrees/<headRefName>` when campaign creates it, else a reused existing checkout). Its fix
commits + pushes to the PR's **own head branch** → **apply the gate reset** below.

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

Every CI failure must be handled; never merge over a red or pending check, never infer green from the
watch's exit code, never derive CI from **one check family** (check runs **and** commit statuses — both),
never parse a snapshot whose fetch did not succeed, never parse a snapshot whose
`# sha:` does not match the ledger's current `head_sha`, never accept checks that are not pinned to
the current `head_sha`, never call it green while a **declared required check** has not registered **from
the producer the declaration binds it to**, never read an **unreadable** required-set as an **empty** one
— and never merge unless GitHub itself reports `MERGEABLE` + `mergeStateStatus == CLEAN`
(`stage-3-merge.md`).

CI fixes serialize only within one PR/SHA. Different PRs with red CI may run scoped CI-fix subagents
concurrently within the dispatcher cap.

---
