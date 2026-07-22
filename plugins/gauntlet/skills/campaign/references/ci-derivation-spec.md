### The CI derivation SPEC — what `ci-status.py derive` and `ci-snapshot.py` implement

**Nothing in this file is a procedure to hand-run.** It is the SPECIFICATION the tools implement — read
it to understand, review, or change `ci-status.py` and `ci-snapshot.py`, and keep it correct: `doc-check`
holds its enums, CLASSIFY tables, DECIDE bullet order and `gh` invocations to the code, `ci-snapshot.py`'s
`fetch_test` executes its fetch filters verbatim, and the fixture + mutation suites pin every rule.

**The DRIVER's doc is `stage-2-ci.md`.** It owns the commands a heartbeat actually runs (`derive`,
`liveness`, `required-set`), the required-set states, the liveness bounds and their caps, the watch
policy, and every action taken on a verdict ("ACT ON THE VERDICT"). A driver never needs this file to
act; a reviewer of the tools always does.

#### FETCH — pinned to the SHA, paginated, BOTH check families, and emitted as JSONL

> **`scripts/ci-status.py derive` DOES ALL OF THIS** (`stage-2-ci.md`, "THE DERIVATION IS A COMMAND"). The commands
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
  see** — which is the failure the SETTLED rule (`stage-2-ci.md`) exists to prevent, arriving by the back
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
  PAGE.** Both REST families return it, and it counts the rows GitHub holds for the commit, across ALL
  pages, not for the page it sits on (observed 2026-07-14: 27 check runs read at `per_page=5` returns six
  pages, each reporting `total_count=27`; the *count-is-the-cross-page-total* behavior is the permanent
  point, the 27 is not). So `total_count` vs the rows the slurp collected is a completeness test, and a
  read that is short FAILS CLOSED (`unusable`, refetch): a row GitHub holds and we do not have could be
  the failing one, and a verdict derived from evidence we KNOW has a hole in it is the false green of this
  whole file wearing a footnote. Every page's count is checked, not the first alone — GitHub RECOMPUTES
  the count per request, so a check that registers between two page fetches makes a later page report a
  higher count than the first (page one says 31, we collect 31, page two says 32), and a rule that trusted
  page one would wave the missing 32nd row through as green; pages that disagree about what the commit
  holds fail closed for the same reason a short read does. A note beside a green is not a disclosure, it is
  a trapdoor — the tool used to print exactly that, and it shipped a green anyway. And a count we cannot
  READ is refused too (absent, or not an integer), on any page: a fail-closed rule that cannot fire is
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
    all — it is the **REQUIRED SET** (`stage-2-ci.md`, "WHAT WERE WE EXPECTING TO SEE?"), which is **DECLARED by the
    base branch** and therefore says what must be present *without asking what showed up*. **Never argue
    from the completeness of the evidence to the completeness of the EXPECTATION.**
- **An EMPTY rollup is a FACT; a MISSING one is NOT EVIDENCE.** `[]` means GitHub says this head has nothing
  in the rollup (legitimate — every suite may be dynamic-event). A response with **no entry list at all** is
  one we cannot read, and taking it for "no witnesses" makes containment a claim about the **empty set**,
  which passes trivially. Refuse it. This is the artifact's founding rule — *an absence must read as "we do
  not know", never as "nothing wrong"* — applied one level up, to the **response**.

##### THE ROLLUP'S `StatusContext` ENTRIES MUST BE VISIBLE IN FAMILY (2)

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
  the PR goes green. **The REQUIRED SET is the closure** (`stage-2-ci.md`, "WHAT WERE WE EXPECTING TO SEE?") — it is
  declared by the base branch, so it does not depend on the rollup showing up, and `green` requires every
  declared check to be **present and passing**.

##### THE TWO SOURCES MUST AGREE ABOUT WHAT A CHECK SAYS

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

- **EXACTLY ONE `source` marker per MANDATORY source** (`check-runs`, `status`, `rollup`), and no
  others. A missing marker → **UNUSABLE** (*"a mandatory source was never queried — its failures
  cannot be in this artifact"*). **NEVER green.** TWO markers for one source → unusable as well: if they
  disagreed the file would claim two things, and nothing would read the second. A marker for a source the
  contract does not define is present and not counted, exactly like an unknown row type.
- **`count` MUST EQUAL the rows of that source actually present** (`check-runs`→`checkrun`,
  `status`→`status`, `rollup`→`witness`). A marker claiming 5 where 3 sit in the file means the
  artifact is TRUNCATED — rows the fetch emitted did not survive promotion, and a row that is not in
  the file could be the failing one → **UNUSABLE**. A `count` that is not a decimal integer is not a
  count you can compare, and a comparison you cannot make is not one you may assume the result of.
- **`sha` MUST be GITHUB'S, and it is compared to the LEDGER'S `head_sha`** — the same two-sources rule as
  the evidence rows, for the same reason: they can disagree, so the check can fail. A marker that
  disagrees means GitHub answered about another commit, so every row that source contributed is about
  that commit → **UNUSABLE**.
- **`sha` may be `"-"` EXACTLY where GitHub gives no oid** (the table above), and nowhere else. A `"-"` on
  the `status` marker, or on a `check-runs` marker whose fetch did return rows, means the value
  was not built from the response — and a marker whose sha is not GitHub's cannot disagree with the
  ledger, so it could never fail: a rubber stamp. A real sha on the `rollup` marker is worse — it
  is a value we invented, the same fabrication as a sha on a `witness` row. Both → **UNUSABLE**.

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
  **every copy of the `ci-status.py derive` command across every skill doc** for a named required set
  (`--ledger`, which resolves the row's `effective_required_set`, or an explicit `--required-set`), too — the
  set that decides a merge must not be droppable by a recap.

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

> **`ci-status.py derive` PERFORMS CLASSIFICATION** (`stage-2-ci.md`, "THE DERIVATION IS A COMMAND"). This section
> is the **SPECIFICATION IT IMPLEMENTS** — `doc-check` holds these tables and enums to the code — so read
> it to understand or review a verdict, and keep it correct. **Do NOT classify rows by hand to decide
> `ci`.** The buckets are also the vocabulary other rules reuse (AGREEMENT above; `RUNNING` in
> `stage-2-ci.md`'s SETTLED), which is why they are spelled out here rather than only in code.

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
> disagrees with the JSON is the by-eye derivation this file exists to kill. **The DRIVER ACTION for
> each outcome — the watch policy, the fix dispatch, the escalation prompt — lives in `stage-2-ci.md`,
> "ACT ON THE VERDICT"**: act on the `verdict` the JSON printed, per that table.

**THE REGISTRATION GAP IS CLOSED — `green` means the required set passed.** It did not always. `green`
once meant only *"every check that had REGISTERED by the time we looked had passed"*, which says nothing
about whether a **required** check ever registered at all: a required check that has not registered is not
a failing row, it is **NO ROW**, so the snapshot was nonempty, all-passing, and **not the truth about the
commit** — a false green with a disclaimer printed beside it. **The disclaimer is gone because the hole is
gone**, not because it was talked away:

- **The expected set is now READ, not assumed** — from branch protection **and** rulesets, into the ledger's
  `required_set` (`stage-2-ci.md`, "WHAT WERE WE EXPECTING TO SEE?"). That read is the mechanism the rule always
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
  a failure is actionable now. The driver's response — the held-PR guard, stopping an in-flight review,
  classifying the failure, the fix dispatch, the gate reset — is `stage-2-ci.md`, "ACT ON THE VERDICT".
- **UNKNOWN_VALUE → escalate, NEVER guess** → **no evidence row classifies `FAIL`** (else `red` above
  already won) and an evidence row carries a value not in the enums above (GitHub added one, or a
  `COMPLETED` `checkrun` row carries no `.conclusion`). **Do NOT** map it to green or pending, and do
  **NOT** invent a red for it: **ESCALATE** it — park the PR (`status = awaiting-user`) naming the
  offending value and the row it came from, through the **ESCALATE** definition (`stage-2-ci.md`, "`pending` MUST NOT
  BE AN ABSORBING STATE"), which is the one place park entry is defined: it writes `ci_reason` and clears
  `blocker_ruling`. **`liveness` performs this park itself** when handed an `unclassified` derivation
  (`stage-2-ci.md`, "THE BOOKKEEPING IS A COMMAND") — prompting the user stays with the driver. A value nobody has
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
- **pending** → any evidence row classifies `RUNNING` → leave `ci = pending`. **This outcome warrants a
  watch** (`stage-2-ci.md`, "WATCH ONLY WHAT CAN MOVE", owns the whole policy — a `red` verdict with a
  still-`RUNNING` row is watched too): a row can still move on its own, so if the
  watch task has exited, **relaunch it in this same heartbeat** — a PR with a still-RUNNING row must never sit
  unwatched waiting for the fallback heartbeat. **It is also BOUNDED**: "a row can still move" is a claim the row
  makes, not a promise it keeps, so if the whole check set then sits unchanged for the CI STALL CAP, the
  PR escalates (`stage-2-ci.md`, "RUNNING-STALL"). `pending` is not a place a PR may live forever.
- **pending (nothing registered)** → the snapshot lists **zero evidence rows**. **Zero evidence rows is NOT
  green** — it means nothing has registered yet. **Do NOT watch it**: there is no row that could move, so
  there is nothing to block on. If nothing ever registers, SETTLED (`stage-2-ci.md`) escalates it instead of letting it
  spin.
- **pending (required set unreadable)** → `required_set` is **CANNOT READ** (`stage-2-ci.md`, "WHAT WERE WE EXPECTING TO
  SEE?"). We do not know what this commit was supposed to show, so **no snapshot of it can be
  green** — not even an all-`PASS` one. **Do NOT watch it**: no row moving would answer the question that
  is open. It is **bounded like every other `pending`**: nothing is RUNNING, so SETTLED (`stage-2-ci.md`) strikes it
  and escalates at the STRIKE CAP, naming the read that failed. Re-attempt the read each heartbeat while it is
  `unknown` — a transient failure clears itself well inside that budget.
- **pending (required check missing)** → `required_set` is **DECLARED** and a declared required check has
  **no row** in the snapshot (matched on name **and** producer — below). **A check that has not registered
  is not a failing row, it is NO ROW**, and a snapshot missing it is nonempty, all-`PASS`, and **not the
  truth about this commit**. **NEVER green.** Bounded the same way: if it never registers, SETTLED (`stage-2-ci.md`)
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

