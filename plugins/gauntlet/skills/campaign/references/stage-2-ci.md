### 2b. CI (event-driven)

Each PR has a background task that waits on `gh pr checks --watch`, then **re-polls** `gh pr checks
<pr>` into `ci-<pr>.txt`. The watch only blocks; the re-polled snapshot is the source of truth. When
the task completes, a wake reads the file and decides `ci` **from the file's contents — never from
the watch exit code** — and writes the `ci`/`reviews_ok` result through `scripts/ledger.py … set --pr
<N> --ci <state> [--reviews_ok 0]` **by field name** (`files-and-ledger.md`), never by hand-editing the
row by column position:

- **green** → ONLY if the snapshot shows **zero failing lines AND zero pending lines** and the
  expected checks are actually present. `gh pr checks --watch` can exit 0 while checks are still
  pending or have not yet registered, so a clean exit is not evidence of green.
- **pending** → any line still pending, or the expected checks haven't appeared yet → not green;
  leave `ci = pending` and, if the watch task has exited, **relaunch it in this same wake** — a
  pending PR must never sit unwatched waiting for the heartbeat.
- **red** → any failing line → **stop any review pass in flight on that PR first** (Loop control
  step 3 — the fix will replace its SHA, so the verdict is already void; free the slot), then diagnose
  from the check logs and **CLASSIFY the failure** ("Tool classification" below) **before dispatching
  anything**. A failure whose fixer is a known tool **the user ENABLED for this run** → run the **TOOL, no
  model**. Everything else → a scoped CI-fix subagent into `<worktree>` — the PR row's
  ledger `worktree` column value, the single source of truth for this PR's checkout path (created at
  adoption/pre-review per `pr-adoption.md`; the ledger-recorded `<worktree>` path — default
  `.worktrees/<headRefName>` when campaign creates it, else a reused existing checkout).

  No enabled known tool for it (the default state — **campaign ships with NO tool enabled**) →
  **dispatch on the session model** — set the model explicitly, and do NOT downgrade it (`SKILL.md`,
  "Subagent Dispatch"). Its output is **code that gets merged**, and nothing downstream validates it: a
  wrong fix can turn CI **green** — by weakening the check, or by being plain wrong in product code no
  check covers — and the review gate is a miss-catcher, not a proof of correctness. A green check means
  the check passed, never that the fix is right. NEVER claim CI catches a bad fix.

  **Scope it**: give it the failing check's logs, the specific failing file(s), and the worktree path,
  and tell it NOT to re-derive the whole diff or read beyond what the failure touches.

  **Tell it, verbatim in its prompt**: it MUST fix the *cause* of the failure. It MUST NEVER make CI
  pass by weakening the check — NEVER delete or loosen an assertion, NEVER add `skip`/`xfail`, NEVER
  disable or downgrade a lint rule, NEVER raise a timeout — UNLESS the check itself is demonstrably
  wrong, in which case it MUST say so explicitly and name the change in its output so the review gate
  can judge it.

  Its fix commits + pushes to the PR's **own head branch** → **apply the gate reset** below
  ("Any campaign commit to the PR head resets the gate").

#### Any campaign commit to the PR head resets the gate

**THE RULE — every commit campaign pushes to a PR's head branch is a PR-content change, whatever wrote
it: a CI-fix subagent, a review-fix subagent, or an enabled TOOL run with no model at all.** Every one
of them MUST, in the same step:

- **reset `reviews_ok` to 0 AND restore `gauntlet-reviewing` if the PR carries `gauntlet-accepted`**
  (`gh pr edit <pr> --remove-label gauntlet-accepted --add-label gauntlet-reviewing`) — the gate and its
  label move together, never one without the other (`stage-2-review-gate.md`, "Status labels mirror the
  review gate");
- **relaunch the CI watch immediately**;
- **re-enter Stage 2a.**

NEVER treat a tool-written commit as exempt: the verdicts on the old SHA describe content that no longer
exists, and a `gauntlet-accepted` label on it is a false public claim.

#### Tool classification — run an ENABLED KNOWN tool, or the session model

**THE SKILL SHIPS WITH ZERO TOOLS ENABLED. `formatters` defaults to `-`.** Out of the box campaign runs
**no tool at all** and **every** CI failure goes to the session model. Enabling a tool is an explicit
**USER** act ("The tool list" below).

**The skill makes NO safety claim about ANY tool.** It supplies the **MECHANISM** (the exact argv, the
binary resolution, the seven operand checks, the exclusion filter, the gate reset) and the **EVIDENCE**
(what each tool's own doc does and does not say). **The USER decides which tools to trust, and accepts that
risk.** State it plainly, every time it comes up: **the skill guarantees HOW a tool is run — NEVER WHAT the
tool does.**

Before dispatching anything, decide whether the failing check's fixer is a **KNOWN tool that this run's
`formatters` header ENABLES**. Not enabled, not known, or `formatters` is `-` → **session model**. Default
deny; there is no blanket "formatters are safe" rule, and campaign NEVER infers a tool from the failure
"looking mechanical".

**"KNOWN" MEANS THE SKILL KNOWS HOW TO RUN IT SAFELY — IT DOES NOT MEAN "SAFE TO ENABLE".** The skill owns
that tool's exact argv, its operand checks, and its resolution rules. It does **NOT** vouch for the tool.
**NEVER present a known-tools row as blessed, trusted, whitelisted, or recommended by the skill.**

Whatever a tool does, it does NOT transfer to a model hand-editing the same file — a pure-indentation edit
can move behavior in a whitespace-significant language and still be formatter-clean, so there is no
diff-shape guard that makes a cheap model's edit safe to accept. **NO SUBAGENT IS EVER RUN ON A DOWNGRADED
MODEL.**

**The SKILL owns the exact argv. NOTHING else supplies a command.** The known-tools table below fixes,
per tool, the precise argv campaign may execute. The run's **`formatters` ledger header field** selects
**only which of those tools run, over which files** ("The tool list" below).

**WHY nothing outside the skill gets flags — the flags carry the semantics.** Tool identity is NOT
sufficient:

```
gofmt -w -r 'true -> false'     # argv[0] is gofmt. A known tool. No shell metacharacters.
```

`-r` turns `gofmt` from a pretty-printer into a **rewrite engine**: it rewrites `return true` into
`return false`. It would pass every identity check, every metacharacter check, and every denylist check.
So campaign REMOVES the degree of freedom instead of trying to police it: the tool list carries
**ids and globs only** — it can NEVER name a `command`, `args`, `argv`, or a flag.

#### The KNOWN-TOOLS TABLE — what the SKILL guarantees, and what is NOT proven

**A row is NOT an endorsement.** These are the ONLY binaries campaign may execute on the no-model path, in
the ONLY form it may execute them — **and ONLY when the user has enabled them.** `<files>` = the tool's
**default glob**, optionally NARROWED by a validated per-id glob from the `formatters` header field, with
the skill's **exclusion filter** applied afterwards — always (both below). Adding a tool, changing an argv,
or changing a default glob is a **SKILL change** (gated, reviewed).

Every row's **"what the skill guarantees"** is the same mechanical set, and it is ALL the skill guarantees:
the argv is **skill-owned and exact**; **NO flag is ever appended**; `argv[0]` is resolved to a **trusted
absolute executable OUTSIDE the repo** via a sanitized `PATH`; the **seven operand checks** run on every
candidate; **resolve-then-filter** (the exclusion filter matches the original AND the resolved path); the
tool is run **without a shell**, **never with a glob**, **never with an empty operand set**; and **any commit
it makes RESETS the gate**.

| `id` | argv — **skill-owned, exact** | default `files` glob | what the skill guarantees (mechanical, OURS) | what is NOT proven (about the TOOL) |
|---|---|---|---|---|
| `gofmt` | `["gofmt", "-w", "--", <files>]` | `**/*.go` | Exactly the argv on the left, over checked operands. `-w` (*"If a file's formatting is different from gofmt's, overwrite it with gofmt's version"*) is the ONLY mutation it performs. `-r` (*"Apply the rewrite rule to the source before reformatting"*) and `-s` (*"Try to simplify code"*) are the documented flags that TRANSFORM the source beyond re-printing it — **neither is in the argv, and nothing may append one** | **The cited doc (https://pkg.go.dev/cmd/gofmt) documents formatting behaviour and flags. It NEVER states a semantic-equivalence guarantee.** Our reading — a pretty-printer that parses and re-prints cannot change program meaning — is an **INFERENCE, not a documented guarantee**. **Enabling `gofmt` means accepting that inference.** |
| `gci` | `["gci", "write", "--", <files>]` | `**/*.go` | Exactly the argv on the left, over checked operands. `write` is the ONLY subcommand; no section/custom-order flag is ever appended | **The cited docs (https://github.com/daixiang0/gci) describe import ORDERING and GROUPING. They NEVER state that gci neither adds nor removes an import**, and never state semantic equivalence. The Go init-order argument (imports drive `init()`; reordering them does not) is **OURS, an INFERENCE**. **Enabling `gci` means accepting it.** |
| `ruff format` | `["ruff", "format", "--", <files>]` | `**/*.py` | Exactly the argv on the left, over checked operands. No `--fix`, no `--select`, no config flag is ever appended; the repo's own Ruff config governs | **The cited docs (https://docs.astral.sh/ruff/formatter/) describe formatting behaviour. They do NOT state an AST/semantic-equivalence guarantee** we can rely on. Separately, `format.docstring-code-format` (https://docs.astral.sh/ruff/settings/#format_docstring-code-format) reformats code **inside docstrings** — i.e. rewrites string CONTENTS — and it is the **repo's config**, not ours, that decides whether it is on. **Enabling `ruff format` means accepting both.** |

**THE ASYMMETRY — say it, do not soften it:** the skill **REFUSES what is documented to be unsafe**; it
**does NOT bless what is merely undocumented — that call is the USER's.** A tool whose docs say it CHANGES
the program is **denied and cannot be enabled** ("NON-OVERRIDABLE DENYLIST" below) — that is a documented
fact, so the skill can and does refuse it. A tool whose docs say nothing either way sits in the table above:
the skill will run it correctly **if you enable it**, and it makes **no claim** that doing so is safe.

**A "what is NOT proven" cell MUST carry a LINK to the tool's own documentation and say exactly what that
doc does and does NOT state. NEVER upgrade an inference into a guarantee.** The word "documented" is not
evidence, and **a citation that does not SUPPORT its claim is WORSE than none** — it launders our belief as
the source's. **FOLLOW the link before you trust a cell.** `gofmt`, `gci` and `ruff format` were once
presented as *proven* semantics-preserving on exactly such citations; none of the three docs says so, and
the honest table says so instead of removing the tools and pretending the question was settled.

**NEVER append a flag to a table argv.** NEVER `-r`, NEVER `-s`, NEVER a catch-all `--fix`, NEVER anything
the table does not list. Execute it **WITHOUT a shell** — never `sh -c`, `bash -c`, `os.system`, or any
shell string.

**golangci-lint `whitespace` is NOT a known tool** — no safe fixer exists: its only fix path is the
catch-all `golangci-lint run --fix`, which the denylist forbids. NEVER invent a command for it; a
`whitespace` failure goes to the session model.

#### RESOLVE argv[0] TO A TRUSTED ABSOLUTE EXECUTABLE — OUTSIDE THE REPO

**The tool runs IN THE PR'S WORKTREE, and the PR under review is UNTRUSTED CONTENT.** A bare name resolved
from a `PATH` the PR can influence — `.`, an empty entry, a repo-local `bin/`, any relative directory —
lets the PR ship a file called `gofmt` and have campaign **execute it**. The argv would be exactly the
table's; the BINARY would be the attacker's. That is arbitrary code execution on the path that runs with
**no model and no review**. The table's names are identifiers, NEVER things to hand to a `PATH` lookup as-is.

Before execution, EVERY time:

1. **Resolve `argv[0]` to an ABSOLUTE path**, using a **sanitized PATH** built by REMOVING: `.`, `..`, any
   empty entry (a leading/trailing/doubled `:` — an empty entry means the CWD), any RELATIVE entry, the
   worktree, the repo root, and any directory INSIDE either. NEVER resolve against the ambient `PATH`
   unfiltered; NEVER let the repo's own files or config inject a `PATH` entry.
2. **The resolved executable MUST live OUTSIDE the repo/worktree tree.** Resolve symlinks too — a symlink
   outside pointing inside is still the PR's binary. Inside the tree → **REFUSE**.
3. **Cannot resolve → REFUSE.** No fallback, no search elsewhere, no install.
4. **REFUSE means: session model.** NEVER execute a binary the PR could have supplied.

Run the tool with the **worktree as CWD** but **NEVER with the worktree on `PATH`** and NEVER with the
worktree as the lookup base. Pass the **resolved absolute path** as `argv[0]`; the rest of the argv is the
table's, unchanged — with the file operands NORMALIZED per the next rule.

#### NORMALIZE THE FILE ARGV — filenames are PR-CONTROLLED DATA

The skill owns the argv **SHAPE**. It does **NOT** own the file operands: `<files>` comes from globbing the
**PR's worktree**, so every filename in it is **attacker-controlled data spliced into argv**. Data spliced
into argv MUST be normalized — exactly like any other injection boundary. Owning the shape is NOT owning
the argv.

**The repro — a filename that is an OPTION** (this is why the rule exists, do NOT delete it):

```
# The PR adds a file literally named:  -cpuprofile=prof.go
# It matches **/*.go. It survives the exclusion filter. It is passed as a file operand:
gofmt -w '-cpuprofile=prof.go' a.go     # exit 0 — gofmt parsed it as a FLAG, and wrote a CPU profile
```

argv[0] was the trusted binary, the argv was the table's, no shell was involved, no model ran — and PR
content still changed what the command **did** and what it **touched**. A bare relative filename is not a
path; it is a token the tool is free to parse as an option.

**The second repro — a filename that RESOLVES OUT OF THE TREE** (normalizing the SPELLING is not enough):

```
# The PR adds a symlink:  link.go -> /home/user/.ssh/authorized_keys   (or ../../other-repo/x.go)
# It matches **/*.go. It survives the exclusion filter. Its NAME is clean: no leading `-`.
gofmt -w -- /wt/link.go                  # gofmt FOLLOWED the symlink and rewrote its TARGET
```

**A path is DATA THAT RESOLVES, and resolution can escape the tree.** Spelling the operand safely (`--`,
absolute path, no leading `-`) says nothing about what the kernel opens. So campaign checks what each
candidate RESOLVES TO, not just how it is spelled. Reason on record: **symlink escape**.

**The third repro — a HARDLINK, which passes both checks above** (this is the layer BELOW `realpath`):

```
# The PR adds a hardlink:  alias.go  →  same INODE as /home/user/other-repo/x.go
# It is a REGULAR file. It is NOT a symlink (lstat says regular). Its realpath is INSIDE the worktree.
gofmt -w -- /wt/alias.go                 # gofmt rewrote the INODE — the outside alias changed too
```

**A path check bounds where we LOOK; it does NOT bound what we WRITE — the inode can be aliased outside the
tree.** `gofmt -w` truncates and rewrites the **EXISTING INODE**, so containment of the **PATH** is not
containment of the **DATA**. A hardlink is a regular file whose path is inside the worktree and whose data
is shared with a path outside it: it defeats the symlink check (it is not a link) and the containment check
(its real path is inside). Reason on record: **hardlink — nlink>1**.

**The fourth repro — a SYMLINKED DIRECTORY COMPONENT, which defeats the EXCLUSION FILTER** (the filter was
applied to the SPELLING, not to the LOCATION):

```
# The PR adds a symlink DIRECTORY:  safe/gh  ->  .github
# Candidate: safe/gh/actions/main.go
#   - not itself a symlink (lstat says regular file)   - realpath is INSIDE the worktree (.github/… is in-tree)
#   - a regular file, nlink == 1                       - name has no leading `-`
#   - and `safe/gh/**` does NOT match the filter's `.github/**`
gofmt -w -- /wt/safe/gh/actions/main.go   # a CHECK DEFINITION, handed to the tool. Every earlier check passed.
```

**THE PRINCIPLE — the generalisation of every round above: EVERY check that reasons about a path MUST reason
about the RESOLVED path. A NAME IS NOT A LOCATION.** The exclusion filter is a check about a path, so it too
must run on what the path RESOLVES TO — matching `safe/gh/actions/main.go` against `.github/**` asks the
wrong question. Reason on record: **symlinked directory component / excluded after resolution**.

**THE PIPELINE, in this order, every tool, every run:** expand the glob → **RESOLVE** each candidate →
**FILTER on BOTH the original and the resolved path** → run the **seven** operand checks → build the argv.

**EVERY tool, EVERY run, ALL SEVEN — no exceptions:**

1. **END-OF-OPTIONS.** Pass `--` immediately before the file list. Every tool in the table accepts it
   (`gofmt`, via Go's `flag` package), and the table's argv already carries it. Nothing after `--` can be
   read as a flag. A tool that has no `--` NEVER enters the table.
2. **NEVER PASS A BARE RELATIVE NAME.** Pass each file as a path that **cannot be mistaken for an option**:
   an **ABSOLUTE** path (worktree root joined to the relative path) — or, if a tool needs relative paths, a
   **`./`-prefixed** one. Belt-and-braces: this holds even for a tool whose `--` handling is broken.
3. **REFUSE a candidate whose basename or path starts with `-`.** A legitimate source file is NEVER named
   that way.
4. **REFUSE a candidate that is a SYMLINK.** `lstat` the candidate itself (NEVER `stat` — `stat` follows the
   link and hides exactly what is being tested). Symlink → DROP it. **A source file that must be formatted is
   never a symlink**, and the tool would open and rewrite the link's TARGET.
5. **REFUSE a candidate that does not RESOLVE INSIDE THE WORKTREE, or is not a REGULAR FILE.** Fully resolve
   the candidate (`realpath` — every symlink followed, `..` collapsed), likewise resolve the worktree root,
   then require: (a) the real path is **CONTAINED under the resolved worktree root** — string-compare on a
   path-component boundary, never a bare prefix match (`/wt-evil` is not under `/wt`); and (b) the resolved
   target is a **REGULAR FILE** — a directory, device, fifo, or socket is REFUSED. Escapes the tree, or is
   not a regular file → DROP it.
6. **REFUSE a candidate whose LINK COUNT is greater than 1.** `stat` the candidate and read `st_nlink`;
   `st_nlink > 1` → DROP it. A source file in a normal checkout has **exactly one link**. A multi-link file
   is either a **hardlink escape** (the inode is aliased outside the tree, and `gofmt -w` rewrites the
   INODE) or something we have **no reason to format** — either way it does not get handed to the tool.
7. **REFUSE a candidate with a SYMLINK in ANY DIRECTORY COMPONENT of its path.** Walk the components from
   the resolved worktree root down (`wt/safe`, `wt/safe/gh`, `wt/safe/gh/actions`, …) and `lstat` **each**;
   ANY component that is a symlink → DROP the candidate. Check 4 only tests the LAST component, so a
   symlinked directory slips a candidate past it — and past the exclusion filter, which was matched on the
   SPELLED path. A source file that must be formatted **never** sits under a symlinked directory. This check
   is load-bearing: it bounds **what we write**. Matching the exclusion filter on the resolved path too is
   **defence in depth, NEVER a substitute for this check** ("THE SKILL-OWNED EXCLUSION FILTER" below) — run
   BOTH anyway.

**The exclusion filter runs on the ORIGINAL and on the RESOLVED path; checks 3–7 run AFTER it and BEFORE the
argv is built** — the filter decides which candidates survive; these decide which surviving candidates are
handed to the tool at all. Every refusal: **DROP that file from the set — do NOT abort the run** — and **LOG
it**: the id, the refused path, and the reason (`-`-leading name / symlink / escapes the worktree / not a
regular file / hardlink — nlink>1 / symlinked directory component / excluded after resolution).

Refusing files can empty the set → then run NOTHING for that id and route the failure to the session model
("Empty file set after filtering" below). Refusing a file NEVER widens anything and NEVER fails the PR.

**NEVER INVOKE THE TOOL WITH AN EMPTY OPERAND SET.** `gofmt` with no file operands **reads stdin** and
writes the formatted result to stdout — a run that is not the run we intended, on input we did not choose.
An empty set is NOT "a no-op run": it is **run NOTHING for that id**, and route the failure to the session
model. Check the set is non-empty **immediately before building the argv**, every time.

**NEVER pass the glob itself to the tool** (`gofmt -w .`, `gofmt -w '**/*.go'`) — that hands file selection
to the tool and bypasses the exclusion filter AND this normalization. Campaign expands, filters, refuses,
normalizes, and passes the resulting **explicit path list**.

#### NON-OVERRIDABLE DENYLIST — the ONE thing the skill REFUSES

**These are KNOWN-BAD: their own docs say they CHANGE the program. That is a documented FACT, not an
inference — so the skill refuses them, and NO config can enable one.** Nothing below is ever admitted to
the known-tools table, and the tool list may NEVER name it:

- **`goimports`** (https://pkg.go.dev/golang.org/x/tools/cmd/goimports): its doc says it **ADDS missing
  imports and REMOVES unreferenced ones**. An added import runs that package's `init()`; a guessed import
  can resolve to the **wrong package**. Documented to change the program.
- **`prettier`**: it reformats the **contents** of tagged template literals (`` gql`…` ``, `` css`…` ``),
  changing the runtime string the tag function receives — documented, and a semantic change.
- **`gofumpt`** (https://github.com/mvdan/gofumpt): applies **EXTRA rewrite rules** beyond gofmt's layout,
  documented as a rule LIST of source-construct edits. "It is basically gofmt" is NOT an argument.
- any **generic or unscoped** "whitespace" / "trailing-whitespace" fixer that can rewrite content inside
  string literals, heredocs, or Markdown (e.g. trailing double-space hard breaks).
- a **semantic rewriter** — `modernize`, codemods, `pyupgrade`, `2to3`, any rule that rewrites logic. A
  `modernize` rewrite can PASS its own rule while CHANGING BEHAVIOR (e.g. `sort.Slice` → `slices.SortFunc`
  with a reversed or non-equivalent comparator): lint-clean, semantics changed.
- every **catch-all fixer** — `golangci-lint run --fix`, `ruff --fix`, `eslint --fix`, `cargo clippy --fix`,
  or **any `--fix`/`--write` flag** on a linter that applies semantic rules. An enabled run invokes **only
  the table's argv**, NEVER a catch-all `--fix`.
- **NEVER a fixer at all**: a failing product test (making a test pass is not the same as fixing the bug), a
  compile error, and any rule that rewrites logic.

**THE ASYMMETRY IS THE POINT: the skill refuses what is DOCUMENTED to be unsafe; it does NOT bless what is
merely UNDOCUMENTED — that call is the user's** (the known-tools table above). Key the denial on the tool's
**IDENTITY and its own documentation** — NEVER on a judgment that a failure "looks mechanical", NEVER on the
category "formatter". **Default deny: unknown check, tool not in the table, tool not enabled, `formatters`
= `-` (the default), an unresolvable binary, or a refused id → session model.**

#### WHAT THE NO-MODEL PATH ACTUALLY RESTS ON — say it honestly

Two things, and only one of them is ours.

1. **OURS — the MECHANISM.** The skill-owned exact argv (no flag ever appended), `argv[0]` resolved to a
   trusted absolute executable **outside the repo**, the **seven operand checks**, resolve-then-filter, no
   shell, no glob, no empty operand set, and the **gate reset on any commit**. This bounds **WHAT WE
   RUN and WHAT WE WRITE**, mechanically, every run.
2. **THE USER'S — the TOOL's behaviour.** Whether the tool preserves the meaning of what it touches is
   **NOT proven by any doc we can cite** (the table above). The user enabled it; the user accepted that
   inference. **NEVER restate it as a guarantee of the skill's, and NEVER re-derive it from the exclusion
   filter below.**

So: **a reformatted check definition is only as safe as the tool the user trusted.** That is exactly why the
exclusion filter keeps tests, check definitions and CI config out of the tool's file set — **defence in
depth**, and admittedly incomplete.

**The no-weakening prohibition belongs to the SESSION-MODEL CI-fix subagent** — it is a model, it can make
any change, so the prohibition is load-bearing there and goes verbatim into its prompt ("red" above). It is
not a substitute for the mechanism on the tool path, and the mechanism is not a substitute for it.

#### THE SKILL-OWNED EXCLUSION FILTER — defence in depth, admittedly INCOMPLETE

**It is an enumerated PATTERN LIST. It is NOT complete and CANNOT BE.** A repo-specific check implemented as
ordinary source — a Go checker at `tools/ci/check.go` — matches `**/*.go`, matches **NONE** of the patterns
below, passes every operand check, and **IS handed to the tool and committed with no model.** That is the
honest state of it. **NEVER describe this filter as complete or exhaustive.**

**Its purpose is BLAST RADIUS: keep the tool's diff small and off files a reviewer expects untouched.** It
is a mitigation, not a proof — and it is not a reason to enable a tool.

**Skill-owned. Config may NARROW it, NEVER widen it, and the tool list NEVER carries the exclusions
itself** — a user-written exclusion list omits more, and rots per-repo. So `gofmt:**/*.go` is VALID: the
glob SELECTS, the filter TRIMS.

**Match it against BOTH the original path AND the RESOLVED one — EITHER matches → REFUSE.** `realpath` each
candidate (symlinks followed, `..` collapsed), take that real path relative to the resolved worktree root.
A filter matched only on the spelling is defeated by one symlinked directory (`safe/gh -> .github`; the
fourth repro above): if we apply the filter at all, apply it to the location and not the name. **A NAME IS
NOT A LOCATION.**

Excluded — dropped from the tool's file set:

- **tests**: `**/*_test.go`, `test/**`, `tests/**`, `**/testdata/**`, `**/__tests__/**`, `conftest.py`,
  `**/test_*.py`, `**/*_test.py`, `**/*.spec.*`, `**/*.test.*`
- **check definitions / CI**: `.github/**`, `.gitlab-ci.yml`, `Makefile`, `**/*.mk`, any CI workflow file
- **tool / lint / build config**: `.golangci.yml`/`.golangci.yaml`, `ruff.toml`/`.ruff.toml`,
  `pyproject.toml`, `setup.cfg`, `tox.ini`, `.editorconfig`, `.pre-commit-config.yaml`
- **campaign's own run state**: the git-ignored `.gauntlet/**`

**Still REFUSE an OBVIOUSLY HOSTILE glob** — one that targets an excluded path **DIRECTLY**
(`gofmt:.golangci.yml`, `gofmt:.github/**`, `gofmt:**/*_test.go`): LOG it and REFUSE it rather than let the
filter silently empty it. It catches **intent**, which is worth catching even where the tool could do no
harm.

**AFTER the filter, the file argv is still PR data**: run all **seven** operand checks on every surviving
candidate ("NORMALIZE THE FILE ARGV" above). The filter only decides *which* candidates survive; the operand
checks are what bound the write. Every run.

**Empty file set after filtering (or after refusals) → run NOTHING for that id** and route the failure to
the session model. **NEVER invoke the tool with zero operands** — `gofmt` with no operands reads **stdin**.

#### The tool list — EMPTY by default, resolved at run start, stored in the ledger, NEVER in repo content

**The default is EMPTY. Nothing is enabled unless the USER enables it.** A hardcoded tool list is
meaningless in a Rust/Java/Ruby repo — and, more to the point, **the skill is not the one who gets to decide
a tool is safe.** The selection comes from the USER; the argv never does. **It is NEVER read from any file
in the repo.**

**Resolve ONCE at run start**, then record it in the ledger header field `formatters`
(`files-and-ledger.md`) — the same resolve-once / record-in-the-header pattern the `reviewer` field
follows ("The reviewer", `reviewer.md`). Priority order:

1. **Explicit invocation.** The user named tools for this run → use them.
2. **User preference from memory.** A recorded preference (a memory entry, or a prior run's carryover)
   naming preferred tools → use it. Do NOT invent a preference; use one only when it actually exists.
3. **EMPTY — `-`.** No explicit list and no preference → **no tool runs; the cheap path is OFF; every CI
   failure goes to the session model.** This is the default and it is NEVER "the table's tools". There is
   **NO built-in set** — do NOT invent one.

**The header field has exactly two shapes — NEVER any other spelling:**

- `-` — **EMPTY: no tool enabled, cheap path OFF.** The **default**, and `ledger.py`'s value when the field
  was never written. **The sentinel is `-` — NEVER the word `none`, NEVER the word `default`.** A user who
  says "no formatters" / "none" is asking for this; campaign writes **`-`**.
- a comma-separated list of known-tool ids, each optionally `:<glob>`-suffixed (`gofmt:internal/**/*.go`).

An **unset/absent** field means `-`. **There is no `default` value any more; if you see one, treat it as
`-`** and rewrite the header.

**Enabling a tool is a RISK ACCEPTANCE by the user.** When the user names one, campaign runs it exactly as
the table says — and makes **no claim** that the tool preserves meaning. Every commit it makes still resets
the gate, so it is re-reviewed like any other content change. If the user asks whether a tool is safe: point
at that tool's **"what is NOT proven"** cell and let them decide. **NEVER answer "yes, the skill guarantees
it".**

**Re-read `formatters` from the ledger header on EVERY wake, before any tool run. NEVER re-derive it from
memory mid-run** — a wake may be a fresh agent instance, and re-deriving would silently revert an explicit
choice (identical rule, identical reason, to `reviewer`).

**NEVER take the tool list from repo content — NOT from a repo-root config file, NOT from
`CLAUDE.md`, NOT from ANY file in the repo.** Repo content **is PR content**: a PR can edit it. A tool list
a PR can edit is a list a PR can **widen to govern its own campaign** — selecting a tool and a glob and
earning an unreviewed tool commit on its own head. That is the self-gating hazard, and it is why the list
lives in the ledger (git-ignored run state, `files-and-ledger.md`) and comes from the user. `CLAUDE.md` is
NOT an exception: it is worktree-loaded repo content, so a PR can edit it too.

Because the list can never come from the repo, **a PR cannot touch it BY CONSTRUCTION.** No provenance rule
is needed, and none exists — do NOT reintroduce one.

#### TRUST MODEL — say it plainly

**The skill trusts NO tool. It ships with none enabled and vouches for none.** What it owns is **HOW** a
tool is run, never **WHAT** the tool does. The user enables tools and thereby accepts the risk; the skill's
denylist refuses only what a tool's own doc says is unsafe.

The tool list is the **user's**, given at invocation or from their own memory — not repo content, so
there is no "malicious committer" to defend against there. The denylist and the id-only
shape are a **guard against footguns and accidental misuse**, NOT a security boundary. NEVER present them
as one.

What IS a security boundary: **the tool runs on UNTRUSTED PR CONTENT, inside the PR's worktree.** That is
why `argv[0]` is resolved to a trusted absolute executable **outside the repo** ("RESOLVE argv[0]" above),
why the **file operands are normalized, resolved, checked for aliasing, AND filtered on the RESOLVED path** —
they come from the PR's tree, so they are attacker-controlled data spliced into argv; a symlink among them
makes the tool write **outside the worktree**, a **hardlink** makes it write outside the worktree while every
path check passes, and a **symlinked directory component** walks a candidate into an **excluded** location
while the spelled path looks innocent ("NORMALIZE THE FILE ARGV" above) — and why the exclusion filter is the
skill's and not the user's.

#### VALIDATION — an id in the `formatters` list is ACCEPTED only if ALL of these hold

1. **The value carries EXACTLY an `id`, and OPTIONALLY a narrowing glob. NOTHING else.** No `command`, no
   `args`/`argv`, no flags — the skill owns the argv, and anything supplying one re-opens the `gofmt -r`
   hole. Campaign never runs a shell regardless.
2. **The `id` is in the known-tools table.** Not in the table → REFUSE. The list NEVER introduces a binary;
   it selects one. **A denylisted id (`goimports`, `prettier`, `gofumpt`, any catch-all `--fix`, …) is
   REFUSED no matter who names it** — config cannot enable it. The binary is resolved to a **trusted
   absolute executable outside the repo** ("RESOLVE argv[0]" above) — NEVER from a `PATH` the repo or the PR
   can influence. Unresolvable, or resolving inside the repo/worktree → REFUSE.
3. **The glob, if present, only NARROWS the tool's default glob** — it MUST NOT match anything outside it.
   Widening → REFUSE. And REFUSE an **obviously hostile** glob that directly targets a check definition,
   config, or test (`.golangci.yml`, `.github/**`, `**/*_test.go`, …), or a repo-sweeping bare `**`/`.`.
   The **exclusion filter still applies to every accepted id** — defence in depth, never a reason to trust
   the tool.

**REFUSING means: log the id and why, IGNORE it, and route that failure to the session model. NEVER
silently honour a refused id.** Refusing one id does not invalidate the others.

**Resolution semantics:** the user's list is the WHOLE list — there are **no built-ins to replace**. A known
tool the user does not name is **not run**. An id that is not a known tool, or is denylisted, is **REFUSED**,
not appended: that failure goes to the session model. **`-`** (the default, and an unset field) means **no
tool runs at all**.

Then, in order:

1. **An ENABLED known tool for this failure → run the TOOL, no model.** In `<worktree>`, run the
   **table's exact argv** for that `id` with `argv[0]` **resolved to a trusted absolute executable outside
   the repo**, **WITHOUT a shell**, over the tool's file set (default glob, narrowed by any validated
   glob, each candidate **RESOLVED**, the **exclusion filter applied to BOTH its original and its resolved
   path**, then **`-`-leading names, symlinks, symlinked directory components, non-regular files, paths whose
   real path escapes the worktree, and files with `nlink > 1` all refused, and the operands normalized —
   `--` + absolute/`./` paths**). **NEVER run the tool with an EMPTY operand set** (`gofmt` with no operands
   reads stdin) — empty → session model. NEVER add a flag; NEVER a catch-all `--fix`. ACCEPT only if
   **both** hold: re-running the **exact** failing check now **passes**, AND the diff touches **no check
   definition, config, or test**. Then commit + push — **zero model spend** — and **apply the gate reset**
   above ("Any campaign commit to the PR head resets the gate"): `reviews_ok` to 0 + relabel, relaunch the
   watch, re-enter 2a. A tool commit gates exactly like a subagent commit.
2. **Everything else → the scoped CI-fix subagent on the session model**, set explicitly, per the red-CI
   dispatch above. Covers: **`formatters` is `-` — the DEFAULT, so this is the DEFAULT ROUTE for every CI
   failure**; the tool is known but **not enabled**; the tool did not fix it; the tool left residue; the
   tool/check is not in the table; the id was refused; the binary **cannot be resolved to a trusted
   executable outside the repo**; the filtered file set is **empty**; or the failure needs any judgment.

If the tool's run fails either acceptance point → **discard the work** (reset the worktree to the PR
head) and **re-dispatch the same failure on the session model**. NEVER patch a tool run in place;
NEVER commit an unverified one; NEVER hand the failure to a cheap model instead.

**Residual risk, stated honestly:** on this path campaign guarantees the **mechanism** — the binary really is
the tool (what the outside-the-repo resolution buys), the argv is the table's, the operands are checked. It
does **NOT** guarantee the tool preserves meaning: **no cited doc states that, the user accepted that
inference when enabling the tool**, and a tool bug or a repo config/plugin that switches on non-formatting
rules is the rest of the exposure. Run enabled tools with the project's own config and no extra rule sets.
**NEVER re-derive this path's safety from the review gate, and NEVER upgrade the user's risk acceptance into
a claim of the skill's.**

Every CI failure must be handled; never merge over a red or pending check, and never infer green from
the watch's exit code alone — always confirm against the re-polled snapshot.

CI fixes serialize only within one PR/SHA. Different PRs with red CI may run scoped CI-fix subagents
concurrently within the dispatcher cap.

---
