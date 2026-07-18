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

#### THE DERIVATION IS A COMMAND — RUN IT. NEVER DERIVE `ci` BY READING TERMINAL OUTPUT.

**The heartbeat derives `ci` by RUNNING `scripts/ci-status.py`, and by nothing else:**

```sh
python3 <skill>/scripts/ci-status.py derive --pr <N> --head-sha <the LEDGER's head_sha> --rundir <rundir> \
  --required-set "$(python3 <skill>/scripts/ledger.py --file <rundir>/state.jsonl header get required_set)"
```

It performs every step below — FETCH (SHA-pinned, paginated, **both** families), PROMOTE (atomic), VERIFY
(via `scripts/ci-snapshot.py`, which it calls), and DECIDE — and prints **JSON**: the `verdict`, the `ci`
value to write to the ledger, the `reason` (**which rule fired, and which row made it fire** — this is what
`ci_reason` is built from), the evidence counts, `head_moved` + `head_sha_now`, the `required_set` state the
verdict was decided under, the `fingerprint` of the verified snapshot's evidence rows, the `buckets`
CLASSIFY tally (`PASS`/`RUNNING`/`FAIL`/`UNKNOWN_VALUE` — `buckets.RUNNING > 0` is the one fact the
watch policy reads; both `null` when the snapshot never verified), and the path to the snapshot it left
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
families cannot see** are the others. The FETCH bullets below define them — each is `verdict = unusable`,
**`ci = pending`**, **refetch**, and **no snapshot is promoted**. There is deliberately **no `notes` field
in the output**: the tool used to disclose an incomplete read *beside a green verdict*, and a caveat printed
next to the answer it contradicts is a trapdoor, not a disclosure. **Anything the tool knows is missing is a
REFUSAL now** — so `ci = green` from this command means the evidence was complete, not merely annotated.

**WHY THIS IS A COMMAND AND NOT A PROCEDURE YOU FOLLOW.** Every rule in this section was already correct,
and a driver still wrote **`ci = green`** into the ledger for a PR whose checks had **not registered** —
having run `gh pr checks <pr>`, read a line saying no checks were reported for the branch, and judged it
green **by eye**. **ZERO EVIDENCE IS NOT GREEN.** The rules did not fail; the one step that was still a
model **reading output and forming an impression** did. A program cannot get tired, cannot skim, and cannot
decide that "no checks" is close enough to "passing" — so that step is now a program. **The shell commands
below are the SPEC the tool implements — they are documentation, NOT a second procedure to hand-run.**

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

#### FETCH — pinned to the SHA, paginated, BOTH check families, and emitted as JSONL

> **`scripts/ci-status.py derive` DOES ALL OF THIS** ("THE DERIVATION IS A COMMAND", above). The commands
> in this section are the **SPECIFICATION IT IMPLEMENTS** — read them to understand or to review the tool,
> and keep them correct. **Do NOT hand-run them to derive `ci`**: a procedure transcribed by hand is a
> procedure that gets shortcut, and the shortcut is what wrote a false green into the ledger.

A source you never queried reports nothing, and "nothing" parses as "nothing wrong". Read **both** — and
**each fetch also emits a `source` COMPLETION MARKER, so that "we asked and got nothing" is a thing the
artifact can SAY.** Each command below emits its evidence rows **and** its marker, from **one** `jq`, over
the **slurped** pages — so a marker cannot exist for a fetch that did not run:

```sh
# (1) CHECK RUNS — pinned to <head_sha> BY THE URL, and the only source of a check-run verdict.
#     ONE object carries the commit, the IDENTITY (name + app_id + id) AND the RESULT (status +
#     conclusion), so no check-run judgment is ever joined across two fetches. `id` is `details_url`, the
#     CROSS-SOURCE identity the containment test below compares on — REST `.details_url` and rollup
#     `.detailsUrl` are the SAME VALUE. REST status/conclusion are lowercase -> upcased.
#     `sha` comes from GITHUB'S OWN `.head_sha` on each row — NEVER a literal we substitute in, and the
#     MARKER's sha comes from that same place. The commit oid lives ONLY on the rows here, so a fetch that
#     returned ZERO rows has no oid to carry: its marker's sha is "-", and inventing one is forbidden.
#     THE READ MUST BE COMPLETE: `total_count` is GitHub's own count of the rows it holds FOR THE COMMIT —
#     not for the page — so the SLURPED rows (every page, flattened) are checked against it and a SHORT READ
#     is a HARD ERROR, never a green with a footnote. It is checked against EVERY page's count, not the first
#     alone: GitHub recomputes the count per request, so a check that registers mid-fetch makes a later page
#     report more, and a row we never received would slip past a first-page-only test. A count we cannot READ
#     is refused too: a rule that cannot fire is not a rule. Same test, same reason, in (2).
gh api --paginate --slurp "repos/<owner>/<repo>/commits/<head_sha>/check-runs" | jq -c '
  if ([.[] | select((.check_runs|type) != "array")] | length) > 0
    then error("check-runs: a PAGE carries no check_runs ARRAY — a page that is MISSING the row array is
      NOT a page that has NO rows. `// []` erases that difference: delete the member from an otherwise-green
      response, leave total_count 0, and the count agrees with the zero rows collected, containment holds,
      and the verdict is GREEN. An absence read as nothing-wrong, one field along.")
    else . end
  | [.[].check_runs[]] as $r | [.[].total_count] as $totals
  | if ([$totals[] | select((type) != "number" or (floor) != .)] | length) > 0
    then error("check-runs: a page carries no integer total_count — that is the count GitHub itself
      reports for this commit, read off EVERY page, and it is the ONLY thing we can check our read against.
      Without it we cannot tell a complete read from a truncated one, and cannot-tell is not a green.")
    elif ([$totals[] | select(. != ($r|length))] | length) > 0
    then error("check-runs: total_count=\($totals | map(select(. != ($r|length))) | unique | map(tostring)
      | join(" and ")) but the slurped read collected \($r|length) row(s) — EVIDENCE IS MISSING. total_count
      is read off EVERY page: a check registered between two page fetches makes a LATER page report more than
      the first, and a read that trusted page one would miss it. A row GitHub holds for this commit is not in
      our hands, and it could be the FAILING one. No verdict is derived from a read we KNOW is short.")
    else . end
  | ($r[] | {row:"checkrun", sha:.head_sha, name:.name, app_id:((.app.id // "-")|tostring),
             status:(.status|ascii_upcase),
             conclusion:((.conclusion // "-")|ascii_upcase),
             id:(.details_url // "-")}),
    {row:"source", source:"check-runs", sha:(($r[0].head_sha) // "-"), count:($r|length|tostring)}'

# (2) COMMIT STATUSES — the legacy family, which (1) CANNOT SEE.
#     The response carries the commit ONCE PER PAGE, at the TOP LEVEL (`.sha`) — not on each status — and
#     carries it EVEN WHEN `.statuses` IS EMPTY. That is what makes the marker below able to PROVE a
#     zero-status commit: {"source":"status","sha":"<GITHUB'S>","count":"0"} says "we asked this commit, and
#     it has none". Again GITHUB'S value, NEVER a literal we substitute in.
#     THE SHA IS GITHUB'S OWN, off the RESPONSE — never a literal we substitute in. That is what lets the
#     superseded-response rule (VERIFY, below) FAIL: the header carries the sha we ASKED for and the rows
#     carry the one GitHub NAMED, so the two can disagree, and on a response fetched for a superseded commit
#     they do. Interpolate our own value here and the comparison is a copy against its own source.
#     THIS is the family that carries the failing Jenkins status, so a short read HERE is precisely the
#     evidence gap this section exists to refuse. It gets the SAME completeness test as (1), and the test
#     is APPLIED PER FAMILY: one family checked and the other not is one family short-read in silence.
gh api --paginate --slurp "repos/<owner>/<repo>/commits/<head_sha>/status" | jq -c '
  if ([.[] | select((.statuses|type) != "array")] | length) > 0
    then error("status: a PAGE carries no statuses ARRAY — see (1). A page MISSING the member is not a page
      with an EMPTY one, and this is the family that carries the FAILING Jenkins status: read the absence as
      zero statuses and the commit that has a failing one reports none at all.")
    else . end
  | [.[].statuses[]] as $s | (.[0].sha) as $sha | [.[].total_count] as $totals
  | if ([$totals[] | select((type) != "number" or (floor) != .)] | length) > 0
    then error("status: a page carries no integer total_count — see (1): without it we cannot tell a
      complete read from a truncated one, and cannot-tell is not a green.")
    elif ($sha|type) != "string" or ($sha|length) == 0
    then error("status: the response carries no sha — GitHub names the commit at the TOP LEVEL, even at zero
      statuses, and a response that does not name it cannot say which commit its rows are about.")
    elif ([$totals[] | select(. != ($s|length))] | length) > 0
    then error("status: total_count=\($totals | map(select(. != ($s|length))) | unique | map(tostring)
      | join(" and ")) but the slurped read collected \($s|length) row(s) — EVIDENCE IS MISSING, and the
      status we did not get could be the FAILING Jenkins one. total_count is read off EVERY page: a check
      registered mid-fetch makes a later page report more than the first.")
    else . end
  | ($s[] | {row:"status", sha:$sha, context:.context, state:(.state|ascii_upcase)}),
    {row:"source", source:"status", sha:$sha, count:($s|length|tostring)}'

# (3) ROLLUP — ITS ROWS ARE WITNESSES ONLY (identity, no verdict): the rollup may NEVER be a verdict
#     source. That is a rule about the ARTIFACT, and it is NOT a licence to leave the rollup's own verdict
#     UNREAD — the fetch reads it, and RECONCILES it, and never writes it down. See AGREEMENT, below.
#     The rollup carries no app.id and no commit oid, so it can NEVER be read as a verdict — and its
#     marker's sha is therefore "-", ALWAYS. A sha there would be one WE invented.
#     IT RETURNS TWO KINDS OF ENTRY. `CheckRun` entries become the witnesses. `StatusContext` entries are
#     the ROLLUP'S VIEW OF FAMILY (2), and they do NOT enter the artifact (no oid, no app id — never a
#     verdict); they are checked AGAINST family (2), and a context family (2) does not report FAILS CLOSED.
#     NEITHER KIND ENTERS THE ARTIFACT WITH A VERDICT — AND NEITHER IS READ FOR ONE. But the rollup STATES a
#     verdict for both (a `CheckRun` carries `status` + `conclusion`; a `StatusContext` carries `state`), and
#     a verdict we were HANDED and never LOOKED AT is a verdict that can CONTRADICT family (1)/(2) in
#     silence. It did, and it was GREEN. So both are carried out of this fetch to be RECONCILED against the
#     REST row for the SAME check (see "THE TWO SOURCES MUST AGREE ABOUT WHAT A CHECK SAYS", below) — which
#     is why the witness rows below still carry IDENTITY ONLY: reconciling is not believing.
#     ANY OTHER __typename is a HARD ERROR: a row we cannot read is not a row we may drop, and dropping
#     one is exactly how a required-but-unposted check became invisible.
#     AND A StatusContext WHOSE `state` IS NOT IN THE StatusState ENUM IS A HARD ERROR TOO. Because it never
#     enters the artifact, NO rule downstream can ever refuse it — CLASSIFY (below) never sees it — so it is
#     refused HERE or not at all. Accepted, it green-lights a context whose state nobody has classified: an
#     unrecognised value is NOT a benign value. The enum is the one declared in the enum block, below.
#     `headRefOid` rides along on this SAME call — the PR's current head, read LAST, after both evidence
#     families (see "A MOVED HEAD FAILS CLOSED", above). It never enters the artifact — and a response that
#     does NOT carry it is a FAILED fetch, because a head we cannot read makes that fail-closed rule unable
#     to fire. `statusCheckRollup` must be an ARRAY: `// []` here would turn a response we cannot read into
#     "no witnesses" — see "An EMPTY rollup is a FACT; a MISSING one is NOT EVIDENCE", below.
#     TWO refusals are NOT expressible here, and they are named rather than quietly omitted — BOTH are
#     CROSS-SOURCE, and no single-fetch jq can see another fetch's rows: (a) `$sc` (the rollup's
#     StatusContexts) must be COVERED by family (2); (b) every rollup entry family (1)/(2) DOES report must
#     AGREE with it about the check's state. The tool does both across the fetches (`build_snapshot`); the
#     rules are the FETCH bullets on `StatusContext` and on AGREEMENT, below.
#     `--repo` IS NOT OPTIONAL. `gh pr view <pr>` with no repository resolves the PR IN THE CURRENT
#     CHECKOUT — so a command without it asks whatever repo you are standing in, which is the one thing this
#     fetch must never do. (1) and (2) name the repo in the URL; this one names it here, and `ci-status.py`
#     REFUSES any `gh` argv that names no repository at all.
gh pr view <pr> --repo <owner>/<repo> --json statusCheckRollup,headRefOid | jq -c '
  (.statusCheckRollup) as $all
  | if ($all|type) != "array"
    then error("rollup: statusCheckRollup is not a list — an EMPTY rollup is a FACT GitHub can state, a
      MISSING one is a response we cannot read. Taking it for no-witnesses makes containment a claim about
      the EMPTY SET, which passes trivially: an absence read as nothing-wrong.")
    else . end
  | [$all[] | select(.__typename=="CheckRun")]      as $w
  | [$all[] | select(.__typename=="StatusContext")] as $sc
  | if (($w|length) + ($sc|length)) != ($all|length)
    then error("rollup: an entry of an UNRECOGNISED __typename — teach the tool about it; NEVER drop it")
    elif ([$sc[] | select((.state|ascii_upcase) as $st
          | ["SUCCESS","PENDING","EXPECTED","FAILURE","ERROR"] | index($st) | not)] | length) > 0
    then error("rollup: a StatusContext in an UNRECOGNISED state — StatusState is
      SUCCESS/PENDING/EXPECTED/FAILURE/ERROR, and a state we cannot read is not a state we may drop. It
      never enters the artifact, so nothing downstream can refuse it: accepted here, it is accepted for
      good, and the PR goes GREEN on a state nobody has classified.")
    elif (.headRefOid|type) != "string" or (.headRefOid|length) == 0
    then error("rollup: the response carries no headRefOid — WE CANNOT TELL which commit is the head, so we
      cannot tell whether this evidence describes it. That is not a green; it is a fetch we cannot use.")
    else . end
  | ($w[] | {row:"witness", name:.name, id:(.detailsUrl // "-")}),
    {row:"source", source:"rollup", sha:"-", count:($w|length|tostring)}'
```

Each `jq -c` above emits **one compact JSON object per line** — the artifact is **JSONL**, the same
machine-read convention as `state.jsonl` and the review plan/progress files (`files-and-ledger.md`).

- **`--paginate` is MANDATORY** — `/check-runs` pages at **30**; without it you parse page one and call
  it the whole set. **`--slurp` collects every page into ONE array before `jq` runs**, which is what lets a
  single filter emit the rows *and* a marker whose `count` is the total **across pages** (per-page `--jq`
  cannot know it). `--slurp` and gh's own `--jq` are mutually exclusive, hence the pipe to `jq -c`.
- **EVERY `//` DEFAULT GOES BEFORE THE CONVERSION, NEVER AFTER — `((.x // "-") | tostring)`, and
  NEVER `((.x | tostring) // "-")`.** In jq, `null | tostring` is the **STRING `"null"`**, which is
  **TRUTHY**, so a default placed after the conversion **NEVER FIRES** and the field carries the
  four letters `null` where `-` was meant. This applies to `.app.id` above (a check run need not come
  from an app). The required-set command applies the same rule in Python to `.app_id` and
  `.integration_id`. **It is not cosmetic there: it is a WEDGE.** An unbound
  required check would come out bound to a producer named `"null"`, no evidence row could ever match
  it, and the PR would report `pending (required check absent)` **forever, for a reason nobody can
  see** — which is the failure this file's own SETTLED rule exists to prevent, arriving by the back
  door. `cli/cli`'s `trunk` returns `app_id: null` for **every** one of its required checks, so this
  is the **common** configuration, not an exotic one. `ci-snapshot.py`'s `fetch_test` extracts and runs
  the three snapshot filters against recorded, multi-page payloads. `ci-status.py self-test` drives the
  required-set production functions against recorded classic and ruleset payloads with null bindings and
  multiple pages. Drop pagination or mishandle a null binding in either path and its test goes red.
- **BOTH families are MANDATORY, AND THE ARTIFACT MUST PROVE BOTH WERE READ.** A failing Jenkins/CircleCI
  **commit status is genuinely invisible** to `/check-runs`: a commit can carry **live commit statuses**
  while `/check-runs` reports `check_runs.total_count = 0` for that very commit. Read only one family and
  the other's failures are simply **absent** from your evidence — and an absence parses as "nothing wrong".
  **This rule was, until now, UNENFORCEABLE by the artifact**: a file with no `status` rows said *nothing*
  about whether the status fetch **ran and found none** or was **never made**, and an all-passing
  check-runs-only snapshot therefore verified **GREEN with a mandatory source unqueried**. The `source`
  markers are what close it — see VERIFY below. **A missing marker is `unusable`, NEVER green.**
- **NEVER read the combined status `.state` as a verdict.** A commit carrying **ZERO** statuses reports
  `{"state":"pending","total_count":0}` — an absence, read as a verdict, is a lie in both directions.
  (Illustrative, and expected to drift: observed on 2026-07-13 on
  `repos/cli/cli/commits/trunk/status`. Whether *that* commit still carries zero statuses is a **live
  fact that changes**; the API's behavior **at zero** is the permanent point.)
- **A SHORT READ IS NOT A GREEN — CHECK WHAT YOU COLLECTED AGAINST GITHUB'S OWN `total_count`, ON EVERY
  PAGE.** Both REST families return it, and it counts the rows GitHub holds **for the commit, across ALL
  pages**, not for the page it sits on (observed 2026-07-14: 27 check runs read at `per_page=5` returns six
  pages, each reporting `total_count=27`; the *count-is-the-cross-page-total* behavior is the permanent
  point, the 27 is not). So `total_count` vs the rows the **slurp** collected is a completeness test, and a
  read that is **short FAILS CLOSED** (`unusable`, refetch): a row GitHub holds and we do not have **could be
  the failing one**, and a verdict derived from evidence we KNOW has a hole in it is the false green of this
  whole file wearing a footnote. **Every page's count is checked, not the first alone** — GitHub RECOMPUTES
  the count per request, so a check that registers between two page fetches makes a **later** page report a
  higher count than the first (page one says 31, we collect 31, page two says 32), and a rule that trusted
  page one would wave the missing 32nd row through as green; pages that **disagree** about what the commit
  holds fail closed for the same reason a short read does. **A note beside a green is not a disclosure, it is
  a trapdoor** — the tool used to print exactly that, and it shipped a green anyway. **And a count we cannot
  READ is refused too** (absent, or not an integer), **on any page**: a fail-closed rule that cannot fire is
  not a rule.
- **A PAGE MISSING ITS ROW ARRAY IS NOT A PAGE WITH NO ROWS — AND NO FIELD READ MAY DEFAULT.** The rule
  above was written and *still passed* on a response whose `statuses` member was simply **not there**:
  `(.statuses // [])` — and `page.get("statuses") or []` in the tool — read the absence as an **empty list**,
  so `total_count: 0` agreed with the zero rows collected, containment held, and the verdict was **`green`**
  for a commit whose status rows we never actually received. The row array must be **present and a LIST on
  every page**, or the fetch fails closed. **The class, not the line:** every field read off a GitHub
  response **declares the shape it expects** and refuses anything else — `ci-status.py` has exactly one
  accessor (`field()`), and **it takes no default**. A value that **may** be absent or null (`app`,
  `conclusion`, `detailsUrl`) **says so at the read**; a value that may not, **cannot become one by
  accident**. *`x // []` and `x or []` are the same bug in two languages: they erase the difference between
  "GitHub says there are none" and "GitHub said nothing at all".*
- **Honest limits, and they are NOT closed by the above.** Say them, and never claim more:
  - `/check-runs` is capped at the **1000 most recent check suites**. `--paginate` defeats page-size
    truncation and the `total_count` test defeats a short read — **neither proves completeness at that
    scale**.
  - **The rollup (3) carries no total and is a single un-paginated page**, so *its* completeness cannot be
    proven at all. **That is precisely why it may NEVER be the source of a verdict** — it is a cross-check
    over families (1) and (2), whose completeness IS proven against GitHub's own counts.
  - **A SHORT ROLLUP CANNOT HIDE A FAILING ROW — AND IT COULD ONCE HIDE A MISSING ONE. THOSE ARE NOT THE
    SAME CLAIM, AND THIS FILE USED TO MAKE THE WRONG ONE.** It said a short rollup "can never admit a false
    green, because a failing row would have to be missing from **both** REST families, and their counts say
    it is not". Every word of that is true **about a FAILING row**, and it was **FALSE as a guarantee**: the
    thing a short rollup hides is not a row that failed but **a required check that produced NO ROW AT ALL**
    — an `EXPECTED` `StatusContext`, which no REST family can express and no `total_count` can miss, because
    **there is nothing to count**. Delete that one entry from the rollup response and the coverage rule below
    has nothing to check: all-passing check runs, zero status rows, **GREEN**, on a PR blocked on a check
    nobody has run. **A GUARD WHOSE INPUT CAN BE ABSENT NEVER FIRES.** What closes it is not the rollup at
    all — it is the **REQUIRED SET** ("WHAT WERE WE EXPECTING TO SEE?", below), which is **DECLARED by the
    base branch** and therefore says what must be present *without asking what showed up*. **Never argue
    from the completeness of the evidence to the completeness of the EXPECTATION.**
- **An EMPTY rollup is a FACT; a MISSING one is NOT EVIDENCE.** `[]` means GitHub says this head has nothing
  in the rollup (legitimate — every suite may be dynamic-event). A response with **no entry list at all** is
  one we cannot read, and taking it for "no witnesses" makes containment a claim about the **empty set**,
  which passes trivially. Refuse it. This is the artifact's founding rule — *an absence must read as "we do
  not know", never as "nothing wrong"* — applied one level up, to the **response**.
- **THE ROLLUP'S `StatusContext` ENTRIES MUST BE VISIBLE IN FAMILY (2), OR THE FETCH FAILS CLOSED.** The
  rollup lists commit statuses too, and a `StatusContext` in state **`EXPECTED`** is **a required status
  check that has not been posted yet** — the PR is *blocked* on it. **The REST commit-status API has no
  `EXPECTED` state at all** (success / pending / failure / error), so family (2) **cannot report it**: the
  rollup is the only *evidence* source in which it appears at all. So every rollup `StatusContext` must
  appear among family (2)'s contexts, and one that does not is `unusable`: **the two sources disagree about
  what exists**, and a snapshot built from evidence that cannot be reconciled is not evidence. **A posted
  status DOES appear in both** (verified 2026-07-14 against a Prow PR, whose rollup contexts `tide` and
  `EasyCLA` were both reported by `/status`) — which is why this is a **coverage test** and not a refusal on
  sight: refusing every `StatusContext` would **wedge every Jenkins/Prow repo forever**.
  **AND ITS `state` MUST BE IN THE `StatusState` ENUM, OR THE FETCH FAILS CLOSED TOO** — a value outside it
  is `unusable`, never dropped and never coerced. This is **not** the same rule as the coverage test beside
  it, and the coverage test **cannot** stand in for it: a context that IS reported by family (2) passes
  coverage, and the rollup's own state for it was then accepted **unread**. A `StatusContext` **never enters
  the artifact**, so CLASSIFY (below) — which is what refuses an unknown `.state`, `.status` or
  `.conclusion` on an artifact row — **can never see it**: the value is refused in the FETCH or it is never
  refused at all. It was never refused at all, and a reviewer showed the price: an invented
  `BRAND_NEW_FAILURE` in the rollup, `success` for the SAME context in family (2), verdict **GREEN**. **An
  unrecognised enum value is not a benign one, in any field, from any source.**
  **THIS RULE IS NOT WHAT PROVES A REQUIRED CHECK REGISTERED, AND IT MUST NEVER AGAIN BE SOLD AS THAT.** It
  can only see the contexts the rollup **returned**, and the rollup **cannot be proven complete** ("Honest
  limits", above): a rollup that simply omits the `EXPECTED` entry leaves this rule **nothing to check**, and
  the PR goes green. **The REQUIRED SET is the closure** ("WHAT WERE WE EXPECTING TO SEE?", below) — it is
  declared by the base branch, so it does not depend on the rollup showing up, and `green` requires every
  declared check to be **present and passing**.
- **THE TWO SOURCES MUST AGREE ABOUT WHAT A CHECK SAYS, OR THE FETCH FAILS CLOSED.** The rule above asks
  only whether a check **EXISTS** in both sources. It does **not** ask whether they **SAY THE SAME THING
  ABOUT IT** — and for as long as nobody asked, the tool believed whichever source it happened to parse. A
  reviewer set family (2) to `success` and the rollup to **`FAILURE`** for the **same context**: coverage
  passed (the context is in both), and the verdict was **GREEN**. The check-run family had the identical hole
  and nobody had looked at it at all: the rollup returns each `CheckRun`'s **`status` and `conclusion`**, the
  producer kept the **name** and dropped the **verdict**, so a rollup `FAILURE` beside a REST `success` for
  the same run was green too. **Containment could never have caught either one — existence is not
  agreement.**
  **THIS NEEDS NOBODY TO BE LYING.** Families (1) and (2) are fetched **BEFORE** (3). A check that flips to
  failure **between** those calls — the head never moving, so the MOVED-HEAD rule cannot fire — produces two
  **honest** sources that **contradict** each other, and that is the one thing this tool must never average
  out. So: **every rollup entry whose check IS reported by the REST family must land in the SAME BUCKET as
  the REST row for it** (`CheckRun` → the `checkrun` row with the same `.id`; `StatusContext` → the `status`
  row with the same context), and one that does not is `unusable` — **refetch**. A settled check reports the
  same thing to both.
  **THE CONFLICT IS NEVER RESOLVED, AND THE WAYS OF "RESOLVING" IT ARE ALL THE SAME BUG.** Not by preferring
  the REST row — it is "right" only by the accident of being fetched **first**, and in the race above it is
  the **stale** one. Not by preferring the rollup — it carries **no commit oid**, so it may never be a
  verdict. And **never** by taking the more favourable of the two, which is how a tool ends up optimistic on
  exactly the evidence it should refuse. **Two sources that disagree about one fact are not evidence, and
  untrustworthy evidence is not green.**
  **DISAGREE MEANS A DIFFERENT BUCKET, NOT A DIFFERENT SPELLING.** The two sources use different
  vocabularies, and a comparison of raw values would refuse **every honest PR**: REST `success` and rollup
  `SUCCESS` are one fact in two spellings, and REST `pending` and rollup `EXPECTED` are both **RUNNING**. So
  both sides are mapped through the **CLASSIFY buckets below** (PASS / RUNNING / FAIL) and the **buckets**
  are compared. A value in **no** bucket is its own answer — `an UNRECOGNISED value` — and it agrees only
  with **another** unrecognised value: `BRAND_NEW_FAILURE` in the rollup against `success` in REST is a
  **disagreement**, and it fails closed. (Where **both** sources report the same unrecognised value, the
  artifact carries family (1)/(2)'s copy of it and **CLASSIFY's catch-all** owns it — `UNKNOWN_VALUE`,
  escalate, never green.)
  **A ROLLUP ENTRY WITH NO REST TWIN IS NOT THIS RULE'S BUSINESS** — the coverage rule above owns that for a
  `StatusContext`, and the containment test below owns it for a `CheckRun`. Each rule keeps the case it can
  name precisely. **A MOVED HEAD IS NOT A DISAGREEMENT** either: the rollup then describes the **new** head
  while (1) and (2) describe the old one, so of course they differ — that is the MOVED-HEAD rule's business,
  and it is the better diagnosis.
- **EVERY evidence row's `sha` MUST come from the RESPONSE, NEVER from a literal you interpolate.** Both
  APIs return the commit themselves — `.head_sha` on each check run, `.sha` at the top level of the status
  response — so take it from there. Stamping `sha:"<head_sha>"` into the `--jq` filter would copy the value
  you *asked for* onto rows you have not checked, and the verify rule below would then compare that copy
  against its own source: **it could never fail**. The rows must carry what **GitHub said**, so that
  disagreeing with the ledger is *possible*.

#### PROMOTE it atomically, STAMP it with the SHA it describes

Write to a temp file **inside `<rundir>`** (same filesystem ⇒ `mv` is an atomic rename), then promote:

```sh
# PIPEFAIL IS MANDATORY, and it is not boilerplate. Without it a pipeline reports the exit status of its
# LAST command — `jq` — so a `gh` that DIED would hand jq an EMPTY stdin, jq would print nothing and exit
# 0, and `|| exit 1` would NEVER FIRE. The fetch failed, the shell called it success, and the only thing
# left saying so is the missing marker. Fail at the fetch, not two rules later.
set -o pipefail

tmp="<rundir>/.ci-<pr>.$$"      # INSIDE <rundir>, so the mv below cannot cross a filesystem
# The header records the sha we REQUESTED — this is the ONE row whose sha is ours. Every EVIDENCE row's
# sha, and every MARKER's sha, comes from GitHub (above), which is what makes the verify rule able to fail.
printf '{"row":"header","sha":"%s"}\n' "<head_sha>" > "$tmp"

# Then the THREE fetches above, IN ORDER, each appending its rows AND ITS MARKER. `|| exit 1` on every one:
# a marker is written ONLY by a fetch that SUCCEEDED, and it is written BY THAT FETCH'S OWN jq — so a fetch
# that fails writes NEITHER its rows nor its marker, and nothing is promoted. A marker appended
# UNCONDITIONALLY — after the fetch, by a separate command, on a line of its own — would be exactly the
# RUBBER STAMP this design exists to prevent: it would say "queried" about a fetch that died.
gh api --paginate --slurp ".../check-runs" | jq -c '...(1) above...' >> "$tmp" || exit 1
gh api --paginate --slurp ".../status"     | jq -c '...(2) above...' >> "$tmp" || exit 1
gh pr view <pr> --repo <owner>/<repo> --json statusCheckRollup,headRefOid | jq -c '...(3) above...' >> "$tmp" || exit 1

mv "$tmp" "<rundir>/ci-<pr>-<head_sha>.txt"
```

The artifact is **JSONL: EVERY line is one JSON object, with NO exceptions** — the header included. There
is no comment line, no plain-text line, and nothing to special-case: read the file line by line and parse
each line as JSON. Five `row` types, distinguished by the `row` field — and **every EVIDENCE row
(`checkrun`, `status`) carries the SHA it is about**:

| `row` | Fields | Meaning | Whose SHA |
|---|---|---|---|
| `header` | `sha` | The `head_sha` we **REQUESTED** — what the file was fetched *for*. Exactly one, first line. | **OURS** (the ledger's) |
| `source` | `source`, `sha`, `count` | **COMPLETION MARKER — "this source WAS QUERIED".** Exactly **ONE** per mandatory source (`check-runs`, `status`, `rollup`), written **by that fetch's own `jq`**. `count` = the rows it returned. | **GITHUB'S**, or `"-"` where GitHub has none (below) |
| `checkrun` | `sha`, `name`, `app_id`, `status`, `conclusion`, `id` | Check-run **identity AND verdict**. `app_id` is `"-"` when the run has **no app**; `conclusion` is `"-"` when absent; `id` is `details_url` (`"-"` when absent). | **GITHUB'S** (`.head_sha`) |
| `status` | `sha`, `context`, `state` | Commit-status **verdict**. | **GITHUB'S** (response `.sha`) |
| `witness` | `name`, `id` | Rollup **identity only** — **no `sha`, no verdict**. `id` is `detailsUrl`. | — (none exists) |

**The last column is the point of the whole artifact.** The `header` records what we **asked for**; every
**evidence** row carries the SHA **GitHub itself** put on that row. They come from **two different
sources**, which is the only reason comparing them can tell you anything.

**A `source` marker's `sha` is `"-"` EXACTLY where GitHub's response carries no commit oid — and NOWHERE
ELSE.** This is not a formality; it is what stops the marker from being a self-issued receipt:

| `source` | Its `sha` | Because |
|---|---|---|
| `check-runs` | **GitHub's `.head_sha`** — but `"-"` when it returned **ZERO rows** | the oid lives **on the rows**, so with no rows there is genuinely none. `count:"0"` + `sha:"-"` is the honest statement, and a sha there would be **invented**. |
| `status` | **GitHub's top-level `.sha`, ALWAYS** — never `"-"` | the status response carries `.sha` **even when `.statuses` is empty**. So `{"source":"status","sha":"<GitHub's>","count":"0"}` **PROVES** we asked this commit and it has **no statuses**. A `"-"` there did **not** come from the response. |
| `rollup` | **`"-"`, ALWAYS** | the rollup carries **no commit oid at all** — the same reason `witness` rows are SHA-LESS. Any sha on it is one **WE fabricated**. |

**That middle row is the whole fix.** An artifact with no `status` rows used to be **silent** about whether
the status family had been read. Now it either carries GitHub's own commit oid saying *"asked; none"*, or
it carries **no marker** — and no marker is **`unusable`**.

**WHY JSONL, and NOT a space-delimited row: CHECK-RUN NAMES AND STATUS CONTEXTS CONTAIN SPACES.** This
repo's own two checks are named **`Lint scripts`** and **`Validate plugins`**. A positional parser handed
`checkrun <sha> Lint scripts 15368 COMPLETED SUCCESS <url>` cannot tell where the name ends and the next
field begins — it reads name=`Lint`, app_id=`scripts`, and **every** rule below (SHA verification,
containment, DECIDE) then reads garbage out of shifted fields. In JSON a value containing spaces is just a
string. **A machine-read artifact must NEVER require guessing where a field ends.**

**The `Fields` column is EXACT — every field that row type carries, and NOT ONE MORE.** A row holding a
field its type does not define is **UNUSABLE**, exactly like a row of a type the table does not list:
nothing reads that field, so whatever it claims is neither verified nor refuted — it is one more piece of
evidence **present and not counted**. **NEVER "accept it and ignore it."**

**`witness` rows are IDENTITY-ONLY, SHA-LESS, and NEVER a verdict.** They exist for **one** purpose: the
containment test below. **NEVER write a SHA onto a witness row** — the rollup **carries no commit oid at
all**, so any SHA on that row would be one *we* invented, not one the API vouched for: **fabricated
evidence**. Their SHA-lessness is exactly **WHY** they can never be read as evidence about a commit, and
why they are exempt from the verify rule instead of being patched into it. **A `witness` row carrying a
`sha` therefore makes the snapshot UNUSABLE** → `ci = pending`, refetch. Not "harmless extra detail", and
never something to skip past: it is a value **we fabricated**, sitting in the file, that the verify rule —
which exempts witness rows **by design** — would never check.

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

Parse the file **only** if the `header` row's `.sha`, **every `checkrun` and `status` row's `.sha`**, **every
`source` marker's `.sha` that is not `"-"`**, **and** the filename all equal the ledger's current
`head_sha`. **`witness` rows are EXEMPT** — they carry **no `sha` field at all** and no verdict. Any
mismatch means the snapshot describes a **superseded commit**
→ discard it, `ci = pending`, refetch. **NEVER** green off it, and never "fix up" the mismatch. A line that
**does not parse as JSON** is a corrupt snapshot — treat it exactly like a failed fetch: `ci = pending`,
refetch.

**CHECK THE EXACT SHAPE — "what I need is in there somewhere" is NOT a check.** Every rule below matches
the artifact's shape *exactly*, and each one is that way because the loose version of it says GREEN to a
file that is lying:

- **The FILENAME must be EXACTLY `ci-<pr>-<head_sha>.txt`** — one PR number, **ONE** sha in **LOWERCASE**
  40-hex (a git object id **is** lowercase, and every producer of ours writes it that way), that
  extension. **NEVER** settle for "the expected sha appears somewhere in the name":
  `ci-<pr>-<head_sha>-<old_sha>.txt` contains it *and names two commits*, so it says nothing about which
  one these bytes describe. And **NEVER case-fold the comparison**: `ci-<pr>-<HEAD_SHA>.txt` is a name no
  producer of ours can emit, so a file wearing it came from something we do not know. "Close enough" is
  the substring bug wearing a new hat.
- **The `header` is EXACTLY ONE row, and it is the FIRST line.** These are **two** requirements, and
  **both** are checked. A file that says which commit it is about only *after* it has already listed
  evidence has not said it: those rows were read unstamped. And a **SECOND** header is read by nothing —
  so if it named a different commit, the file would describe **two**, and nothing would notice. (An
  **empty** file lands here too, as "zero headers" — it is **no snapshot**.)
- **Each row type carries an EXACT field set** — the one in the table above. Every field it requires, and
  **NOT ONE MORE**.
- **Every field value is a STRING**, and a value of any other type — a nested object, a number, a list —
  makes the snapshot **UNUSABLE**. This is the *same* rule one level down, and skipping it does not
  produce a lenient verdict, it produces **NO verdict**: a `{"row":{...}}` or a `"conclusion":{...}` is a
  value you cannot compare, and a comparison you cannot make is not a comparison you may assume the
  result of. **A CRASH IS NOT A VERDICT** — the tool failing to have an opinion is the one outcome this
  vocabulary has no word for.
- **A REPEATED member name makes the snapshot UNUSABLE** — in **any** object on the line, nested ones
  included. Never *"last one wins"*, never *"first one wins"*: a field given **two** values means the file
  does not say **one thing**, and evidence that does not say one thing is **not evidence**. This rule
  belongs **at the DECODER**, because that is the last place the duplicate is still **visible**: a JSON
  decoder resolves a repeated key by keeping **one** value and **silently discarding** the other, and every
  rule above only ever sees what survived. So `{"row":"header","sha":"<old>","sha":"<head>"}` verified
  **GREEN** with a **stale commit** sitting in the bytes, and `{"row":"status_context","row":"checkrun",…}`
  verified **GREEN** with the row type the contract **rejects** silently gone. **Present in the bytes,
  reaching NO rule** — the defect this entire section is about, one level *below* every rule written to
  catch it. Parse with a **duplicate-key-rejecting hook** (Python: `object_pairs_hook`); a decoder that
  picks a winner for you has **decided a question that was yours**.
- **A line the decoder cannot decode WITHOUT CRASHING is UNUSABLE too** — a line nested thousands of levels
  deep exhausts the decoder's stack and **raises**, and a raise where a verdict was owed is the tool having
  **no opinion**, not a lenient one. Catch it and call the artifact unusable: a row of the shape this
  contract defines is a **flat object of strings**, so nothing legitimate is ever anywhere near that depth.

**AND THE SOURCES MUST PROVE THEY WERE QUERIED — an ABSENCE must say "we do not know", never "nothing
wrong".** This is the rule this whole section opens with, and until the `source` row existed the artifact
**could not express it**: *"the commit-status fetch RAN and this commit carries zero statuses"* and *"the
commit-status fetch was SKIPPED, or died before appending anything"* produced the **byte-identical file**.
So:

- **EXACTLY ONE `source` marker per MANDATORY source** (`check-runs`, `status`, `rollup`), **and no
  others.** A **missing** marker → **UNUSABLE** (*"a mandatory source was never queried — its failures
  cannot be in this artifact"*). **NEVER green.** **TWO** markers for one source → unusable as well: if they
  disagreed the file would claim two things, and nothing would read the second. A marker for a source the
  contract does not define is **present and not counted**, exactly like an unknown row type.
- **`count` MUST EQUAL the rows of that source actually present** (`check-runs`→`checkrun`,
  `status`→`status`, `rollup`→`witness`). A marker claiming **5** where **3** sit in the file means the
  artifact is **TRUNCATED** — rows the fetch emitted did not survive promotion, and **a row that is not in
  the file could be the failing one** → **UNUSABLE**. A `count` that is not a decimal integer is not a
  count you can **compare**, and a comparison you cannot make is not one you may assume the result of.
- **`sha` MUST be GITHUB'S, and it is compared to the LEDGER'S `head_sha`** — the same two-sources rule as
  the evidence rows, for the same reason: they **can** disagree, so the check **can** fail. A marker that
  disagrees means **GitHub answered about another commit**, so every row that source contributed is about
  that commit → **UNUSABLE**.
- **`sha` may be `"-"` EXACTLY where GitHub gives no oid** (the table above), and nowhere else. A `"-"` on
  the **`status`** marker, or on a **`check-runs`** marker whose fetch **did** return rows, means the value
  was **not built from the response** — and a marker whose sha is not GitHub's **cannot disagree with the
  ledger, so it could never fail**: a **rubber stamp**. A **real sha on the `rollup`** marker is worse — it
  is a value **we invented**, the same fabrication as a sha on a `witness` row. Both → **UNUSABLE**.

**WHY A MARKER IS NOT A RUBBER STAMP.** It carries what **only a fetch that actually ran** could know: a
`count` that must match the file it sits in, and a `sha` that must match a **ledger value it never saw**.
And it **cannot be written for a fetch that failed**, because it is emitted by **that fetch's own `jq`**,
from the **slurped** response, in the **same command** as its rows — a failed fetch writes **neither**.
**NEVER append a marker as a separate, unconditional step after the fetch**: that reintroduces exactly the
stamp-you-wrote-yourself defect this file was written against.

**EVERY line must be READ, and a line you cannot read is NOT a line you may SKIP.** The five `row` types
above are the **whole** vocabulary. A **blank** line, bytes that are **not valid UTF-8** (**never** decode
them leniently — that silently rewrites what the file says), a row of a type **not** in that table, a row
**missing a field its type requires** (a `checkrun` with no `status`, a `status` with no `state`, a
`witness` with no `id`), a row whose value has the **wrong TYPE**, a row that **names a member twice**, or
a row carrying a field its type does **not** define (**a `sha` on a `witness` row** — see below) makes the
snapshot **UNUSABLE** → `ci = pending`, refetch. **NEVER skip past it, and NEVER accept-and-ignore it.**

Skipping is how the false green gets back in: an unrecognised row is not *nothing*, it is something you
**failed to understand** — and if it happened to carry a **FAILURE**, ignoring it turns a red commit green
while every other rule in this section passes. **An unexpected FIELD is the same defect one level down**:
nothing reads it, so whatever it asserts is neither verified nor refuted. **Evidence that is present but
not counted parses as "nothing wrong."**

The `header` and the filename are **ours**, so checking them catches only a *misfiled* artifact (a stale
file left in `<rundir>`). The **evidence rows** are what catch a **wrong-commit fetch** — they are the
part of this rule that can actually fail. **If you ever find yourself writing the ledger's `head_sha` onto
an evidence row, you have deleted the verification**, not implemented it.

**The ledger write is GATED ON the parsed contents.** A guard that runs *beside* the write is not a
guard.

**EVERY RULE ABOVE IS EXECUTED, AND EVERY ONE IS PINNED BY A FIXTURE THAT FAILS WHEN IT IS GONE.** Prose
cannot be run, and three defects shipped in *this prose* — so the rules also exist as
`scripts/ci-snapshot.py` (`self-test` runs the fixtures) and `scripts/mutate-ci-snapshot.py`, which
**removes each rule in turn and FAILS if no fixture notices**. Both run in CI. That second script exists
because a fixture suite cannot see its own worst failure — a rule that **nothing** tests, whose deletion
leaves the suite green — and a hand-written matrix claiming otherwise was **wrong about two rules**.
**"Which rules are unpinned?" is a question the SUITE answers, never one a reviewer has to discover.** If
you change a rule here, change it there; if you add one, mark it (`# MUTATE:<id>:<weakening>`) and give it
a fixture that goes **GREEN** when it is deleted.

**AND THE PRODUCER'S OWN RULES ARE PINNED THE SAME WAY.** `scripts/ci-status-test.py` — run by
`ci-status.py self-test`, in CI — drives **recorded API responses** through the **real fetch path**, and
asserts each fixture's verdict **and the rule that produced it**. Most of those fixtures are **false greens
the tool actually shipped**: a family never fetched, a rollup `StatusContext` dropped on the floor, a page
whose row array was absent, two sources contradicting each other, a head that moved under the fetch.

**AND THIS PROSE CANNOT SILENTLY DRIFT FROM THE CODE ANY MORE.** The enums, the CLASSIFY buckets and the
DECIDE bullet order are written **here**, in prose, **and** encoded in `ci-snapshot.py` as Python —
**nothing compared them**, so one could rot while the other ran, and the rotted one is the copy a reader
believes. `scripts/ci-status.py doc-check` **parses this file** — its enum block, both CLASSIFY tables, and
the order of the DECIDE bullets — and asserts they agree with the sets the code actually classifies with,
**and that the classification is TOTAL over the enums declared here** (every value in exactly one bucket:
the property this section claims, which nothing used to check). It runs in CI. **Edit a rule in this file
without editing the code and the build goes RED** — which is the only kind of "keep them in sync" that has
ever worked.

**AND THE FETCH COMMANDS ABOVE ARE EXECUTED, NOT MERELY READ — because the version of this check that only
read the ENUMS is exactly where the drift got in.** Nothing parsed the `gh … | jq` block, so this file went
on specifying `(.statusCheckRollup // [])` — which turns a **MISSING** rollup into an **EMPTY** one — while
the tool **refused** that shape. The doc and the code disagreed about **what is refused**, in the one place
nothing was looking. Three checks close that, and **none is optional**:

- **`ci-snapshot.py`'s `fetch_test` RUNS the three snapshot filters** (the `--paginate` bullet, above, is
  the same point). It extracts (1)(2)(3) verbatim by their command line and executes them over recorded,
  multi-page API payloads, asserting the exact rows.
- **`ci-status.py self-test` RUNS the required-set producer** over recorded classic-protection and paged
  ruleset payloads. It asserts strict field handling, nullable app bindings, URL encoding, the complete
  union, and the atomic ledger result.
- **`ci-status.py doc-check` checks every `gh` INVOCATION in this file against the argv the code really
  issues** — *every copy of them*, not just the spec block (a recap that drops `,headRefOid` reconstructs a
  fetch the MOVED-HEAD rule can never fire on; that copy had drifted, and this is what caught it). It sweeps
  **every copy of the `ci-status.py derive` command across every skill doc** for `--required-set`, too — the
  flag that decides a merge must not be droppable by a recap.

**TWO refusals are CROSS-SOURCE and no single-fetch filter can state them** — the rollup's `StatusContext`
**coverage**, and the **AGREEMENT** of the two sources about a check they both report. One `jq` filter sees
ONE fetch, so neither can live in the spec above: the tool does both in `build_snapshot()`, the rules are the
FETCH bullets on `StatusContext` and on AGREEMENT, and the fixtures that pin them are
`rollup-expected-status.json`, `rollup-status-conflict.json` and `rollup-checkrun-conflict.json`. **They are
named here rather than quietly omitted**, because a reader who does not find them in the filters must not
conclude they do not exist.

#### CROSS-FETCH AGREEMENT — containment on a USABLE `.id`, NOT equality

**THIS SECTION IS ABOUT WHAT EXISTS, NOT ABOUT WHAT IT SAYS — AND THE TWO ARE DIFFERENT QUESTIONS.** It
answers *did REST see every check the rollup saw?*, and **nothing more**. Whether the two sources **AGREE
ABOUT THE STATE** of a check they **both** report is the FETCH rule "THE TWO SOURCES MUST AGREE ABOUT WHAT A
CHECK SAYS", above, and containment **cannot** stand in for it: a check present in both **passes containment
by existing**, whatever either source says about it. Read this section as the whole cross-fetch story and you
will rebuild the false green that rule exists to close.

The fetches are taken at different times, so they can disagree. But the correct test **of existence** is
**containment**, not equality — compared on the **per-run identity**, and **only** when that identity can
actually tell two runs apart:

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

#### CLASSIFY every row — a TOTAL function over the REAL enum

> **`ci-status.py derive` PERFORMS CLASSIFICATION** ("THE DERIVATION IS A COMMAND", above). This section
> is the **SPECIFICATION IT IMPLEMENTS** — `doc-check` holds these tables and enums to the code — so read
> it to understand or review a verdict, and keep it correct. **Do NOT classify rows by hand to decide
> `ci`.** The buckets are also the vocabulary other rules reuse (AGREEMENT above; `RUNNING` in SETTLED,
> below), which is why they are spelled out here rather than only in code.

**THE RULE — classify over the WHOLE enum, and give every unlisted value somewhere to go.** A rule that
names only the values you happened to think of leaves **holes**, and a value that falls in a hole matches
**no** branch: it is not green, not red, not pending — so it can never resolve, and the PR **wedges
forever**. Enumerate from the schema, never from memory.

Classification reads **only** the JSON verdict fields: `.status` and `.conclusion` on `checkrun` rows,
`.state` on `status` rows. The `header`, `source` and `witness` rows hold **no verdict** and are **never**
classified. "Rows" below means **evidence rows** — `checkrun` + `status`; the `header` and `source` rows do
not count toward any of them.

The enums below are **introspected from GitHub's GraphQL schema**, not recalled:

```
CheckStatusState      REQUESTED QUEUED IN_PROGRESS COMPLETED WAITING PENDING
CheckConclusionState  SUCCESS FAILURE TIMED_OUT CANCELLED ACTION_REQUIRED NEUTRAL SKIPPED
                      STARTUP_FAILURE STALE
StatusState           SUCCESS PENDING EXPECTED FAILURE ERROR
```

**`checkrun` rows** — classify on `.status`, then `.conclusion`:

```
.status QUEUED | IN_PROGRESS | WAITING | PENDING | REQUESTED   -> RUNNING
.status COMPLETED                                              -> classify on .conclusion, below
.conclusion SUCCESS | SKIPPED | NEUTRAL                        -> PASS
.conclusion FAILURE | TIMED_OUT | CANCELLED | ACTION_REQUIRED | STARTUP_FAILURE | STALE
                                                               -> FAIL
ANY OTHER VALUE (either field)                                 -> UNKNOWN_VALUE
```

**NEVER write that first line as `.status != COMPLETED -> RUNNING`. A NEGATED TEST IS A CATCH-ALL WEARING
A DISGUISE** — and it is the very defect this section exists to kill, so do not reintroduce it here. `!=
COMPLETED` matches **every value you have never heard of** and maps it onto a verdict **chosen in
advance**: a `CheckStatusState` GitHub adds tomorrow would classify `RUNNING`, the driver would wait for
it to finish, and it would **never reach** the `UNKNOWN_VALUE` escalation below — the fail-closed rule
would be dead for `.status`, silently. **Only an EXPLICIT MEMBERSHIP TEST leaves a hole for the catch-all
to catch.** The same holds for every rule here: name the values, never negate them.

The catch-all is what makes this **total**, and it is **not decoration**: `.conclusion` is `"-"` when the
field is **absent**, and `"-"` is **not a `CheckConclusionState`**. On a row that is still running that is
harmless — its `.status` already classified it `RUNNING`. But a row that is `COMPLETED` while carrying
`.conclusion "-"` has **no verdict at all**, and it falls to `UNKNOWN_VALUE` exactly like an enum value
GitHub added tomorrow. **That is the catch-all doing its job — never "read through" the `-` to a guess.**

**`status` rows** — classify on `.state`:

```
SUCCESS                                    -> PASS
PENDING | EXPECTED                         -> RUNNING    # EXPECTED = declared but not yet posted
FAILURE | ERROR                            -> FAIL       # ERROR IS A FAILURE — never shrug it off
ANY OTHER VALUE                            -> UNKNOWN_VALUE
```

**`SKIPPED` is a PASS — and this is the difference between a campaign that can go green and one that
cannot.** GitHub itself rolls it up that way: `cli/cli` PR #13856 carries **6 × `SKIPPED` + 1 ×
`SUCCESS`** and its `statusCheckRollup.state` is **`SUCCESS`**. Treating `SKIPPED` as anything else is
not a conservative choice — it is a **wedge**: a skipped run is `COMPLETED` (so not pending), is not
`SUCCESS` (so not green), and is not a failure (so not red), and it therefore matches **no rule at all**.
Skipped runs are **routine, not exotic** — path filters, conditional jobs and excluded matrix legs produce
them in bulk (illustrative, and expected to drift: **188 of 310** check runs observed on `cli/cli` trunk on
2026-07-13 were `skipped`; the **CLAIM** is permanent, never the counts). A rule set without it can **never
go green** on `cli/cli`, `grafana`, `vscode`, or `next.js`.

**`STARTUP_FAILURE` and `STALE` are FAILURES.** A rule set that omits them from the red list calls them
not-a-failure and merges over them — a **false green**.

**`NEUTRAL → PASS` is the one mapping here that is DOCS-BASED, NOT EXECUTED.** It shares GitHub's
non-failure bucket with `SKIPPED` (which *is* verified above), but no live `NEUTRAL` run was found to
confirm it. **Say so; do not launder it into a verified claim.** If a `NEUTRAL` run ever turns out to
block a merge, this is the line that was wrong.

#### DECIDE — first match wins

> **`ci-status.py derive` EVALUATES THESE BULLETS** — its JSON's `verdict` and `reason` name the bullet
> that matched and the row that made it match. The bullet LOGIC is the **SPECIFICATION THE TOOL
> IMPLEMENTS** (`doc-check` pins the bullet order; the `*-outranks-*` fixtures pin it behaviourally):
> keep it correct, and **NEVER re-evaluate it by hand against a snapshot** — a hand evaluation that
> disagrees with the JSON is the by-eye derivation this file exists to kill. **What each bullet ALSO
> carries is the DRIVER ACTION for its outcome** — the watch policy, the fix dispatch, the escalation.
> Those are yours: act on the `verdict` the JSON printed, and do what its bullet says to do.

**THE REGISTRATION GAP IS CLOSED — `green` means the required set passed.** It did not always. `green`
once meant only *"every check that had REGISTERED by the time we looked had passed"*, which says nothing
about whether a **required** check ever registered at all: a required check that has not registered is not
a failing row, it is **NO ROW**, so the snapshot was nonempty, all-passing, and **not the truth about the
commit** — a false green with a disclaimer printed beside it. **The disclaimer is gone because the hole is
gone**, not because it was talked away:

- **The expected set is now READ, not assumed** — from branch protection **and** rulesets, into the ledger's
  `required_set` ("WHAT WERE WE EXPECTING TO SEE?" below). That read is the mechanism the rule always
  needed and never had.
- **`green` now requires every declared required check to be PRESENT and PASSING**, matched on producer.
- **The states that cannot support the claim do not make it.** A missing declared check and an unreadable
  required set are **`pending` bullets below** — non-merging outcomes, each bounded and escalated — never a
  green with a footnote. **A caveat attached to a green is not a caveat; it is a merge.**

The bullets are evaluated **in this order — first match wins.**

**THE ORDER IS PART OF THE RULE — and `red` OUTRANKS `UNKNOWN_VALUE` DELIBERATELY.** When a snapshot
carries **both** a `FAIL` row and an `UNKNOWN_VALUE` row, `red` wins and the PR gets its CI fix. That is
the intended behavior, and it costs nothing:

- **A known failure is actionable NOW, and it BLOCKS THE MERGE regardless.** `red` never merges (Stage 3
  merges only on `ci == green`), so deciding `red` first can never wave the unknown value through.
- **The unknown value is DEFERRED, NOT DISCARDED.** The fix lands, the head moves, and the **next**
  derivation re-reads a fresh snapshot for the new `head_sha`. With no `FAIL` left to outrank it, the
  unknown value falls to `UNKNOWN_VALUE` and the PR **parks** exactly as it must. Nothing is skipped —
  only sequenced.
- **Parking first would be strictly worse:** a PR with a real, fixable failure would sit on a human for a
  value that could not have merged it anyway.

**Why NO ORDER here can produce a false green:** `green` is the **last** bullet and demands that **EVERY**
evidence row classify `PASS`. An `UNKNOWN_VALUE` row is **not** `PASS`, so **while any unknown value is in
the snapshot the `green` bullet cannot match — no matter which bullet is evaluated first.** Every earlier
bullet (`UNUSABLE`, `red`, `UNKNOWN_VALUE`, `pending`) is a **non-merging** outcome. So the ordering can
only decide *which non-green state* is recorded and *how fast the PR moves*; it can **never** decide
*green*. That is what makes `red`-first safe, and it is the property to preserve if these bullets are
ever re-ordered again.

- **UNUSABLE → `ci = pending`, refetch** → no usable snapshot: any fetch failed, the file is absent, or it
  **fails ANY rule in VERIFY above** — misnamed, header not first or not alone, a line that does not parse
  as JSON, an unknown row type, a missing or unexpected field, a field whose **value is not a string**, a
  `.sha` (the `header` row's, **any** evidence row's, or the filename's) that does not match the ledger's
  `head_sha`, **a mandatory `source` marker missing, duplicated, mis-counted, or carrying a sha that is not
  GitHub's** — or containment **cannot be established**: it fails, **or** a `witness` `.id` is
  null/duplicated so the test proves nothing.
- **red** → **any** evidence row classifies `FAIL`. Other rows still `RUNNING` does **not** change this —
  a failure is actionable now.

  **If the PR is HELD** (`ledger.py … dispatch-check --pr <N>` exits non-zero — it is parked on a human,
  or `repairing` after a review-loop cap), record `ci = red` and
  **dispatch NO fix** — a held PR dispatches nothing until its question is answered (`loop-control.md`
  step 3, "held-status guard"). A CI fix on a PR whose diff the reassessment pass is about to rescope is
  work thrown away twice over.
  **Being held does not change the watch either way** — watching is observation, not work-dispatch, so the
  watch follows the normal policy below ("WATCH ONLY WHAT CAN MOVE"): alive while a row can still move,
  and **not** relaunched once nothing can. Parking never stops a warranted watch and never starts an
  unwarranted one. Otherwise → any failing row → **stop any review pass in flight on that PR first** (Loop control
  step 3 — the fix will replace its SHA, so the verdict is already void; free the slot), then
  **CLASSIFY the failure** from the check logs ("Classify, then set the model class" below) **before
  dispatching anything**, and dispatch a **scoped CI-fix subagent** into `<worktree>` — the PR row's
  ledger `worktree` column value, the single source of truth for this PR's checkout path (resolved by
  `pr-adoption.md`'s repository-context-aware operation). Its fix commits + pushes to the PR's **own
  head branch** → **apply the gate reset** below.
- **UNKNOWN_VALUE → escalate, NEVER guess** → **no evidence row classifies `FAIL`** (else `red` above
  already won) and an evidence row carries a value not in the enums above (GitHub added one, or a
  `COMPLETED` `checkrun` row carries no `.conclusion`). **Do NOT** map it to green or pending, and do
  **NOT** invent a red for it: **ESCALATE** it — park the PR (`status = awaiting-user`) naming the
  offending value and the row it came from, through the **ESCALATE** definition below ("`pending` MUST NOT
  BE AN ABSORBING STATE"), which is the one place park entry is defined: it writes `ci_reason` and clears
  `blocker_ruling`. **`liveness` performs this park itself** when handed an `unclassified` derivation
  ("THE BOOKKEEPING IS A COMMAND", below) — prompting the user stays with the driver. A value nobody has
  classified is not evidence of anything, and guessing a bucket for it is how a hole becomes a wedge or a
  false green.

  **State the invariant EXACTLY, because the `red`-first order narrows it.** It is **NOT** "an unknown
  value is never bucketed" — a snapshot that also holds a `FAIL` is recorded `red`, and that is correct
  (above). What holds without exception is:
  - **An unknown value NEVER produces `green`.** The `green` bullet requires **every** evidence row to
    classify `PASS`, and an `UNKNOWN_VALUE` row never does. It cannot merge, on any ordering.
  - **An unknown value parks the PR AS SOON AS no `FAIL` outranks it** — on this derivation if there is
    no `FAIL`, otherwise on the next one, once the CI fix has cleared the failure. **It is deferred, never
    dropped**, and it outranks `pending`: a still-running row does **not** postpone the park.
- **pending** → any evidence row classifies `RUNNING` → leave `ci = pending`. **This is the ONLY outcome
  that warrants a watch** ("WATCH ONLY WHAT CAN MOVE" below): a row can still move on its own, so if the
  watch task has exited, **relaunch it in this same heartbeat** — a PR with a still-RUNNING row must never sit
  unwatched waiting for the fallback heartbeat. **It is also BOUNDED**: "a row can still move" is a claim the row
  makes, not a promise it keeps, so if the whole check set then sits unchanged for the CI STALL CAP, the
  PR escalates ("RUNNING-STALL", below). `pending` is not a place a PR may live forever.
- **pending (nothing registered)** → the snapshot lists **zero evidence rows**. **Zero evidence rows is NOT
  green** — it means nothing has registered yet. **Do NOT watch it**: there is no row that could move, so
  there is nothing to block on. If nothing ever registers, SETTLED below escalates it instead of letting it
  spin.
- **pending (required set unreadable)** → `required_set` is **CANNOT READ** ("WHAT WERE WE EXPECTING TO
  SEE?" below). We do not know what this commit was supposed to show, so **no snapshot of it can be
  green** — not even an all-`PASS` one. **Do NOT watch it**: no row moving would answer the question that
  is open. It is **bounded like every other `pending`**: nothing is RUNNING, so SETTLED below strikes it
  and escalates at the STRIKE CAP, naming the read that failed. Re-attempt the read each heartbeat while it is
  `unknown` — a transient failure clears itself well inside that budget.
- **pending (required check missing)** → `required_set` is **DECLARED** and a declared required check has
  **no row** in the snapshot (matched on name **and** producer — below). **A check that has not registered
  is not a failing row, it is NO ROW**, and a snapshot missing it is nonempty, all-`PASS`, and **not the
  truth about this commit**. **NEVER green.** Bounded the same way: if it never registers, SETTLED
  escalates with the check **named** (that is exactly the "which check never registered" ESCALATE already
  promises). A declared check that IS present but still running is not this bullet — it is a `RUNNING` row,
  so plain `pending` above already caught it, and it is watched, as it should be.
- **green** → **all three `source` markers are present and hold** (VERIFY above — otherwise you do not know
  what you did not read); **≥1 evidence row**; **every** evidence row classifies `PASS`; containment
  holds **on a usable identity** (every `witness` `.id` non-null and unique); **and the required set is
  ACCOUNTED FOR** — `required_set` is **DECLARED** and every declared check is **present AND classified
  `PASS`, proven from the SAME row, matched on producer**, or `required_set` is **NONE DECLARED**, which is
  a **read fact, not an absence of one**: the base branch requires nothing, so nothing required can be
  missing. **`green` now means the required set passed. It carries NO caveat, and it never did license
  one** — the two states that could not support the claim (`CANNOT READ`, a missing declared check) are
  **`pending` bullets above**, not footnotes under this one.

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
(FETCH above owns that rule). Bound to an app that **does not exist**, a check can never be matched by any
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
  commit-status rows carry **no producer field at all** (FETCH above: the status response has none to
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
the FETCH above emits, nothing else — and it exists for the same reason the derivation is a command: a
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
  classified, see CLASSIFY above), so they can never be the thing that is or is not moving. A `source`
  marker's `count` is a **restatement** of the evidence rows it counts (VERIFY enforces exactly that), so
  including it would add nothing and could only double-count.
- **The row's `sha` is EXCLUDED** — VERIFY has already proved every evidence row's `sha` equals the
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

`RUNNING` in both definitions is **the CLASSIFY bucket above, verbatim**, and `derive` emits its tally:
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
file kills on sight (CLASSIFY, above).

**A STRIKE — AND A STALL CLOCK — MEAN "NOBODY IS COMING". NEVER PARK A PR THE DRIVER IS ALREADY
REPAIRING.** `SETTLED` and `RUNNING-STALL` are about **CI**, not about **campaign**: a **red** PR whose
rows are all terminal is SETTLED on the **first** derivation, and a CI-fix subagent in flight does **not**
change `head_sha` until it pushes — so an ungated strike rule would park, within the **STRIKE CAP**'s worth
of heartbeats, the exact PR the driver is actively fixing, and an ungated stall clock would start timing a
`RUNNING` row the driver is about to make irrelevant.

**MACHINE ACTION** = **any work campaign dispatches that can produce a new `head_sha` on this PR.** That
**PROPERTY is the definition, and it is the whole of it.** **APPLY THE PROPERTY — NEVER CONSULT A LIST.**
Of any work in question, ask: *when it completes, can it put a new commit on this PR's head?* If yes it is
a MACHINE ACTION, **whether or not it appears in any enumeration anywhere in this repo** — the same idiom
as the parked-PR guard's "does this MUTATE the PR?, **not** is it on a list" (`SKILL.md`). A set defined
by a property but **applied** through its examples is a set that silently shrinks every time a member is
added somewhere else — which is exactly how the list below went stale once already.

- **NON-NORMATIVE EXAMPLES. Illustrative only; they DO NOT BOUND THE SET:** a CI-fix subagent (either
  tier, including an escalation from the cheap one), a review-fix subagent, a copilot-address fix, a
  refutation commit, and **every rebase and base refresh — the conflict-resolving rebase and the CLEAN
  BASE-ONLY one alike** (Stage 2a's precondition rebase, `stage-2-review-gate.md`; the post-merge
  reconcile, `stage-3-merge.md` step 6). Work that has the property but is missing from this list is
  **still** a machine action.
- **The CLEAN BASE-ONLY REBASE IS ONE — it is the member this list omitted, and the omission parked
  healthy PRs.** It qualifies for exactly the reason every other member does: **it MOVES `head_sha`**
  ("THE LIVENESS COUNTERS" below, which resets the counters at it for that same reason). Whether it also
  resets the **gate** is a **DIFFERENT QUESTION** — it does not — and it is **not this one**: the property
  here is `head_sha`, not `reviews_ok`. A PR merely **DUE** for a clean rebase would otherwise keep
  striking (or keep its stall clock running) against a head the rebase is about to replace, and **park —
  a spurious park, with the work already on its way**.
- **The CI watch is NOT a machine action, and neither is a review pass.** They fail the property: neither
  pushes a commit, so neither can move CI. A settled PR under review is still settled, a stalled one is
  still stalled, and suppressing either bound for them would wedge the PR exactly as before.

**In flight** = dispatched for this PR at this `head_sha` and not yet completed — **the same in-flight
test `loop-control.md` step 3 already applies** to suppress a duplicate dispatch ("CI red and no fix is
already in flight for that PR/SHA"). The strike rule and the stall clock read that same fact; they do not
invent a second one.

**Due** = **this heartbeat would launch it** — it is not in flight, and nothing but a free concurrency slot is
missing. That, too, is a **property, not a fixed list of scans**: whichever rule OWNS that dispatch is the
one that says so. For a CI fix it is `loop-control.md` step 3's dispatch scan; for a rebase it is the rule
that owns the rebase (Stage 2a's preconditions, `stage-2-review-gate.md`; the step-6 reconcile,
`stage-3-merge.md`) finding the PR behind/conflicting on this heartbeat. A PR **frozen by a park** has **no**
machine action due — the held-status guard forbids every one of them (`loop-control.md`), so nothing is
coming, which is why the park is the terminus and not another wait.

**IT STILL TERMINATES — a liveness rule that can be suppressed forever is not a liveness rule.** The STOP
is keyed to a machine action being **DUE or IN FLIGHT**, and **every machine action ENDS**, so the STOP
ends with it — the broader definition above does not widen it into a wedge:

- **A fix subagent is bounded**: a stuck task is retried **once** and then aborted permanently, and "CI
  fails identically after a fix attempt" is itself a stop condition (`bailout-and-final-report.md`).
- **A rebase is bounded by construction**: it either lands a new head — which **resets the counters at the
  site that moved it**, the correct outcome, not a suppression — or it fails; either way it is afterwards
  neither due nor in flight.
- **DUE cannot persist**: the only thing a due action waits on is a **free concurrency slot**, and slots
  free as work completes.

So a PR whose machine actions are **all exhausted** — a **red** PR whose CI-fix budget is **spent**, not
behind or conflicting, so no rebase is owed — has **none due and none in flight**: the STOP does not fire,
strikes accrue (or the stall clock runs) on the next derivations, and **it reaches its cap and parks**,
like any other settled or stalled PR. The gate suppresses a bound **only while work that can move
`head_sha` is actually coming**, never merely because the PR is red.

**ESCALATE** = park the PR (`status = awaiting-user`, `ci_reason` = the blocker **named**: which check
never registered, **which check has been `RUNNING` since when without the check set moving**, which value
was unrecognized, which read was denied), **and `blocker_ruling` = `-` in
that same row write** ("THE RULING IS CONSUMED EXACTLY ONCE" below — a ruling already on the
row answers the **previous** park, never this one), and tell the user. **For the three liveness bounds,
`ci-status.py liveness` performs that row write itself** (exit `3` — "THE BOOKKEEPING IS A COMMAND",
above); telling the user is the half that stays with the driver. It does **not**
abort the run or close the PR — the run's other PRs keep going. At **this** park — a CI one — `ci_reason` is
**the DECIDE reason for this snapshot**: the bullet that matched and the row that made it match, never a
bare restatement of `ci`. (The field itself is **wider than CI**: it is the durable machine-blocker reason,
and `stage-3-merge.md`'s merge-precondition parks write it with `ci` **green**. `files-and-ledger.md` owns
that definition.) A park that cannot name its blocker is not actionable.

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
  derivation. A heartbeat may be a fresh agent instance: an answer that lives only in the driver's head is an
  answer that gets re-asked.

**NOTHING THIS SECTION RELIES ON LIVES IN THE DRIVER'S HEAD — and that is a PROPERTY, not the list that
used to stand here.** A heartbeat may be a fresh agent instance, so: **if a LATER derivation, the escalation
prompt, or the unpark has to read it, it is a LEDGER FIELD.** The durable set is therefore **the ledger row
schema itself** — `files-and-ledger.md`'s row-field definitions, and the `ROW_FIELDS`/`ROW_DEFAULTS` in
`scripts/ledger.py` that own it — and a field added there is durable **with no edit to this section**. A
list retyped here rots the next time one is added, and the one that stood here rotted **twice**: it first
dropped `ci_reason`, the very thing the park asks the user about, and its replacement dropped
`ci_fingerprint`, without which every heartbeat sees CI as having moved and **no bound ever fires at all**.
There is no third attempt: **the members are not retyped here, in any form, marked or not.** Every write
goes through the owning tool, **by field name**, never by hand-editing the row: the derivation-driven
fields are `ci-status.py liveness`'s ("THE BOOKKEEPING IS A COMMAND", above), and everything else —
the unpark, the reset sites, the non-CI parks — is `scripts/ledger.py … set --pr <N>`
(`files-and-ledger.md`).

**HOW state dies with the context is a CLASS, and it is stated as one — never per field**, so a field
added to the schema tomorrow is already covered here:

- **A COUNTER that dies never reaches its cap.** A fresh instance restarts the count from zero, so the
  bound never fires.
- **A CLOCK is worse**: it does not merely lose the elapsed time, it **silently restarts** it. Every clock
  is therefore a **timestamp on disk**, so that **any** heartbeat computes the elapsed time from the ledger
  alone, remembering nothing.
- **EVIDENCE OF WHAT CI LOOKED LIKE LAST TIME is worse still, because losing it looks like SUCCESS.** The
  derivation decides that CI **moved** by comparing this snapshot against what the row says it saw before;
  with that gone, **every** heartbeat sees motion, **every** heartbeat resets the counters and the clock, and the
  bounds never fire — the wedge this whole section exists to close, reopened silently and with no error.
- **A REASON that dies leaves the park UNANSWERABLE.** The escalation prompt above is built from the
  blocker the human is being asked to rule on; a fresh agent that lost it cannot even ask the question, so
  the park has no exit.
- **A RULING that dies gets re-asked** ("THE RULING IS CONSUMED EXACTLY ONCE" below).

**THE RULING IS CONSUMED EXACTLY ONCE — a durable answer that is never spent is a park that unparks
itself.** `blocker_ruling` must be **DURABLE** (it survives a context loss) **AND spent EXACTLY ONCE** (it
answers the park it was written for, and no other). Both halves, or neither holds:

- **ENTERING a machine-blocker park sets `blocker_ruling` = `-`** (ESCALATE above), in the **same**
  `ledger.py … set` call that writes `status = awaiting-user` — one atomic row write, so no heartbeat can ever
  observe a freshly parked row still carrying the previous park's answer.
- **CONSUMING a `retry` sets it back to `-`** in the same call that unparks the PR (`loop-control.md`
  step 3).
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
exactly two kinds of site:

1. **Any `head_sha` change — whether or not the gate resets with it.** The new head is new evidence, so
   the old head's liveness says nothing about it. **THE TRIGGER IS THE PROPERTY — "this site wrote a new
   `head_sha`" — and NOT membership of a list.** Every site that writes one resets them, including a site
   added after this paragraph was written, with **no edit here**. Today's sites, **illustratively and
   NON-EXHAUSTIVELY**: **"Any campaign commit to the PR head resets the gate"** (below — CI-fix,
   review-fix, copilot-item fix, refutation commit); **Stage 2a's precondition rebase**
   (`stage-2-review-gate.md`) and **`stage-3-merge.md`, step 6's reconcile** — for each, BOTH branches: a
   clean base-only rebase, which does **not** reset the gate, and a conflict-resolving rebase, which does;
   **`loop-control.md` step 1's ledger refresh** and **`pr-adoption.md` step 3's row refresh** (a
   formatter/bot commit, a manual push, any content change this run did not dispatch).
2. **An unpark by `retry`** (`loop-control.md` step 3) — no new head, but the user changed something
   **outside** the PR (they re-ran the workflow, killed the hung runner, registered the missing check), so
   the strikes and the stall clock that measured the old attempt are void.

**`liveness`'s stale-derivation refusal ("THE BOOKKEEPING IS A COMMAND", above) is NOT a substitute for
the site doing it — it is the seam that makes the site's reset SAFE.** The sites above **write the new
`head_sha` to the row**, so by the time CI is re-derived the ledger already reads the new head and there
is nothing left to detect. The reset belongs **at the site that moves `head_sha`**; what `liveness` adds
is the other half: a derivation still pinned to the OLD head is refused outright, so stale evidence can
never spend the budget the site just reset.

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
  instance can do on its first heartbeat. A duration accumulated in context is a duration that resets to zero
  every time the session dies — which is the failure that made these counters durable in the first place.

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

`UNUSABLE` is the one DECIDE outcome with **no fingerprint** (above), so `settled_strikes` can say nothing
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
- On escalation `ci_reason` names **the VERIFY rule that failed and the line/row that failed it** (not
  "unusable") — a snapshot campaign could not read once in the REFETCH CAP's worth of consecutive attempts
  is a real, actionable blocker: a
  denied read, a wrong-SHA artifact, a fetch that never succeeds.

#### WATCH ONLY WHAT CAN MOVE — the relaunch is not free

The watch is warranted by **a row that can still move**, never by the `ci` value — and "a row can still
move" is read off **`derive`'s JSON: `buckets.RUNNING > 0`**, never off a hand classification of the
snapshot:

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
REFUTATION of a review finding (`stage-2-review-gate.md`, "Audit every finding before you fix it").**
Every one of them MUST, in the same step:

- **reset `reviews_ok` to 0 AND restore `gauntlet-reviewing` if the PR carries `gauntlet-accepted`**
  (`gh pr edit <pr> --remove-label gauntlet-accepted --add-label gauntlet-reviewing`) — the gate and its
  label move together, never one without the other (`stage-2-review-gate.md`, "Status labels mirror the
  review gate");
- **re-derive `ci` from a fresh snapshot for the NEW `head_sha`, and launch a watch if — and only if —
  that snapshot holds a row that can still move** ("WATCH ONLY WHAT CAN MOVE" above). The new commit
  **resets the liveness counters** ("THE LIVENESS COUNTERS" above), so the PR gets a clean budget.
  **NEVER launch the watch unconditionally on the push**: at that instant the checks may not have
  registered yet, the snapshot holds **zero evidence rows**, and `gh pr checks --watch` would exit in
  about a second — a heartbeat per second, forever, on a PR nothing is watching *for*;
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
   worktree to the PR head, and hand the failure to a **`session`-class** CI-fix worker. **Escalation is
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
commit to the PR head resets `reviews_ok` to 0, restores `gauntlet-reviewing`, resets the liveness
counters ("THE LIVENESS COUNTERS"), re-derives CI for the new tip and watches it **only if a row can still
move** ("WATCH ONLY WHAT CAN MOVE"), and re-enters Stage 2a in the `session` class.

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
