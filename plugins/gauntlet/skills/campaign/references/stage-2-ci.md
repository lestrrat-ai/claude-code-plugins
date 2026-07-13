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
gh api --paginate --slurp "repos/<owner>/<repo>/commits/<head_sha>/check-runs" | jq -c '
  [.[].check_runs[]] as $r
  | ($r[] | {row:"checkrun", sha:.head_sha, name:.name, app_id:(.app.id|tostring),
             status:(.status|ascii_upcase),
             conclusion:((.conclusion // "-")|ascii_upcase),
             id:(.details_url // "-")}),
    {row:"source", source:"check-runs", sha:(($r[0].head_sha) // "-"), count:($r|length|tostring)}'

# (2) COMMIT STATUSES — the legacy family, which (1) CANNOT SEE.
#     The response carries the commit ONCE, at the TOP LEVEL (`.sha`) — not on each status — and carries it
#     EVEN WHEN `.statuses` IS EMPTY. That is what makes the marker below able to PROVE a zero-status
#     commit: {"source":"status","sha":"<GITHUB'S>","count":"0"} says "we asked this commit, and it has
#     none". Again GITHUB'S value, NEVER a literal we substitute in.
gh api --paginate --slurp "repos/<owner>/<repo>/commits/<head_sha>/status" | jq -c '
  [.[].statuses[]] as $s | (.[0].sha) as $sha
  | ($s[] | {row:"status", sha:$sha, context:.context, state:(.state|ascii_upcase)}),
    {row:"source", source:"status", sha:$sha, count:($s|length|tostring)}'

# (3) ROLLUP — WITNESSES ONLY (identity, no verdict). Used ONLY for the containment test below.
#     The rollup carries no app.id and no commit oid, so it can NEVER be read as a verdict — and its
#     marker's sha is therefore "-", ALWAYS. A sha there would be one WE invented.
gh pr view <pr> --json statusCheckRollup | jq -c '
  [.statusCheckRollup[]? | select(.__typename=="CheckRun")] as $w
  | ($w[] | {row:"witness", name:.name, id:.detailsUrl}),
    {row:"source", source:"rollup", sha:"-", count:($w|length|tostring)}'
```

Each `jq -c` above emits **one compact JSON object per line** — the artifact is **JSONL**, the same
machine-read convention as `state.jsonl` and the review plan/progress files (`files-and-ledger.md`).

- **`--paginate` is MANDATORY** — `/check-runs` pages at **30**; without it you parse page one and call
  it the whole set. **`--slurp` collects every page into ONE array before `jq` runs**, which is what lets a
  single filter emit the rows *and* a marker whose `count` is the total **across pages** (per-page `--jq`
  cannot know it). `--slurp` and gh's own `--jq` are mutually exclusive, hence the pipe to `jq -c`.
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
gh pr view <pr> --json statusCheckRollup   | jq -c '...(3) above...' >> "$tmp" || exit 1

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
| `checkrun` | `sha`, `name`, `app_id`, `status`, `conclusion`, `id` | Check-run **identity AND verdict**. `conclusion` is `"-"` when absent; `id` is `details_url` (`"-"` when absent). | **GITHUB'S** (`.head_sha`) |
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

#### CLASSIFY every row — a TOTAL function over the REAL enum

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

Evaluate the bullets **in this order — first match wins.**

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

  **If the PR is PARKED** (`status` = `awaiting-user` / `awaiting-api`), record `ci = red` and
  **dispatch NO fix** — a parked PR dispatches nothing until the user answers (`loop-control.md` step 3).
  **The park does not change the watch either way** — watching is observation, not work-dispatch, so the
  watch follows the normal policy below ("WATCH ONLY WHAT CAN MOVE"): alive while a row can still move,
  and **not** relaunched once nothing can. Parking never stops a warranted watch and never starts an
  unwarranted one. Otherwise → any failing row → **stop any review pass in flight on that PR first** (Loop control
  step 3 — the fix will replace its SHA, so the verdict is already void; free the slot), then
  **CLASSIFY the failure** from the check logs ("Classify, then set the model" below) **before
  dispatching anything**, and dispatch a **scoped CI-fix subagent** into `<worktree>` — the PR row's
  ledger `worktree` column value, the single source of truth for this PR's checkout path (created at
  adoption/pre-review per `pr-adoption.md`; default `.worktrees/<headRefName>` when campaign creates
  it, else a reused existing checkout). Its fix commits + pushes to the PR's **own head branch** →
  **apply the gate reset** below.
- **UNKNOWN_VALUE → escalate, NEVER guess** → **no evidence row classifies `FAIL`** (else `red` above
  already won) and an evidence row carries a value not in the enums above (GitHub added one, or a
  `COMPLETED` `checkrun` row carries no `.conclusion`). **Do NOT** map it to green or pending, and do
  **NOT** invent a red for it: park the PR (`status = awaiting-user`) naming the offending value and the
  row it came from. A value nobody has classified is not evidence of anything, and guessing a bucket for
  it is how a hole becomes a wedge or a false green.

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
  watch task has exited, **relaunch it in this same wake** — a PR with a still-RUNNING row must never sit
  unwatched waiting for the heartbeat.
- **pending (nothing registered)** → the snapshot lists **zero evidence rows**. **Zero evidence rows is NOT
  green** — it means nothing has registered yet. **Do NOT watch it**: there is no row that could move, so
  there is nothing to block on. If nothing ever registers, SETTLED below escalates it instead of letting it
  spin.
- **green** → **all three `source` markers are present and hold** (VERIFY above — otherwise you do not know
  what you did not read); **≥1 evidence row**; **every** evidence row classifies `PASS`; and containment
  holds **on a usable identity** (every `witness` `.id` non-null and unique). This bullet is subject to the
  **registration gap** above: it proves only that **what had registered** passed, **never** that the
  required set is complete.

#### `pending` MUST NOT BE AN ABSORBING STATE — SETTLED, then ESCALATE

**THE INVARIANT — every non-green state MUST declare (a) what event would leave it, and (b) what happens
if that event NEVER COMES. A rule with no answer to (b) is FORBIDDEN. Apply this to every rule you add
to this file, before you write it down.**

The failure this prevents is subtle and it has already happened: hardening a rule set against a false
*green* — one "→ pending, NEVER green" clause at a time, each one correct on its own — produces a
machine that in common configurations can **never go green at all**. Many rules enter `pending`; nothing
leaves it except CI itself changing; and the bailout is **disabled while `ci == pending`**. So the PR
sits there forever and **no one is ever told**. A wedge is not safer than a false green — it is just a
failure that never files a report.

The missing concept is **"CI has STOPPED MOVING and the rule is STILL unsatisfied."** `ci = pending`
cannot express it, because `pending` conflates *still running* with *stuck*.

**The FINGERPRINT is computed over the VERIFIED snapshot's EVIDENCE ROWS — the JSONL the FETCH above
emits, nothing else.** Serialize each `checkrun` and `status` row into one canonical line, sort those
lines bytewise, prefix the ledger's `head_sha`, and hash:

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
- **A snapshot that is not VERIFIED has NO fingerprint.** `UNUSABLE` never yields one — its rows were
  never trusted. It is handled by its own line in the watch table below (bounded refetch, then escalate),
  never by a strike counted against evidence we rejected.

```
SETTLED  ==  NO evidence row classifies RUNNING       # nothing left that could move on its own
         AND fingerprint == ledger.ci_fingerprint     # and it did not move since the last derivation
```

`RUNNING` here is **the CLASSIFY bucket above, verbatim** — a `checkrun` whose `.status` is
`QUEUED`/`IN_PROGRESS`/`WAITING`/`PENDING`/`REQUESTED`, or a `status` row whose `.state` is
`PENDING`/`EXPECTED`. Do **not** re-derive it from `ci`: `red` outranks `pending` in DECIDE, so a snapshot
recorded `red` can still hold a **RUNNING** row, and that PR is still moving.

Per derivation, in this order:

```
head_sha changed          -> ci_fingerprint = fp ; settled_strikes = 0     # new commit, new evidence
fp != ci_fingerprint      -> ci_fingerprint = fp ; settled_strikes = 0     # still MOVING — be patient
SETTLED and ci != green   -> settled_strikes += 1
settled_strikes >= 2      -> ESCALATE
```

**ESCALATE** = park the PR (`status = awaiting-user`, `ci_reason` = the blocker **named**: which check
never registered, which value was unrecognized, which read was denied), and tell the user. It does **not**
abort the run or close the PR — the run's other PRs keep going. `ci_reason` is **the DECIDE reason for
this snapshot** — the bullet that matched and the row that made it match — never a bare restatement of
`ci`. A park that cannot name its blocker is not actionable.

`settled_strikes` and `ci_fingerprint` live **in the ledger, not in the driver's head**: a wake may be a
fresh agent instance, and a strike count that dies with the context is a strike count that never reaches
its cap. Write them through `scripts/ledger.py … set --pr <N>` **by field name**, like every other field
(`files-and-ledger.md`).

#### WATCH ONLY WHAT CAN MOVE — the relaunch is not free

The watch is warranted by **a row that can still move**, never by the `ci` value:

| DECIDE outcome | Watch? |
|---|---|
| **pending** — an evidence row classifies `RUNNING` | **YES** — ensure a watch task is alive; relaunch it in this same wake if it has exited. |
| **pending (nothing registered)** — zero evidence rows | **NO.** Nothing to block on. SETTLED escalates it. |
| **red** — but some row still `RUNNING` | **YES** — that row can still move; the CI fix runs regardless. |
| **red** — every row terminal | **NO.** The CI fix moves it, not the watch. |
| **UNKNOWN_VALUE** | **NO.** The park is the resolution. |
| **UNUSABLE** | **NO.** Refetch with backoff, **bounded** — then ESCALATE. |
| **green** | **NO.** |

**NEVER relaunch the watch merely because `ci == pending`.** On a settled PR `gh pr checks --watch`
**exits in about a second** — there is nothing left to block on — and a task completion is **itself a
wake**. So "pending → relaunch the watch" on a settled-but-not-green PR burns a **fresh-context wake
every second or two, forever**, doing nothing. Watch only when at least one row can still move.

#### Any campaign commit to the PR head resets the gate

**THE RULE — every commit campaign pushes to a PR's head branch is a PR-content change, whatever wrote
it: a cheap CI-fix subagent, a session-model CI-fix subagent, a review-fix subagent, or an inline
REFUTATION of a review finding (`stage-2-review-gate.md`, "Audit every finding before you fix it").**
Every one of them MUST, in the same step:

- **reset `reviews_ok` to 0 AND restore `gauntlet-reviewing` if the PR carries `gauntlet-accepted`**
  (`gh pr edit <pr> --remove-label gauntlet-accepted --add-label gauntlet-reviewing`) — the gate and its
  label move together, never one without the other (`stage-2-review-gate.md`, "Status labels mirror the
  review gate");
- **re-derive `ci` from a fresh snapshot for the NEW `head_sha`, and launch a watch if — and only if —
  that snapshot holds a row that can still move** ("WATCH ONLY WHAT CAN MOVE" above). The new commit
  resets `ci_fingerprint` and `settled_strikes` (SETTLED above), so the PR gets a clean liveness budget.
  **NEVER launch the watch unconditionally on the push**: at that instant the checks may not have
  registered yet, the snapshot holds **zero evidence rows**, and `gh pr checks --watch` would exit in
  about a second — a wake per second, forever, on a PR nothing is watching *for*;
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
