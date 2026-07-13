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
  step 3 — the fix will replace its SHA, so the verdict is already void; free the slot), then
  diagnose from the check logs and dispatch a scoped CI-fix subagent into `<worktree>` — the PR row's
  ledger `worktree` column value, the single source of truth for this PR's checkout path (created at
  adoption/pre-review per `pr-adoption.md`; the ledger-recorded `<worktree>` path — default
  `.worktrees/<headRefName>` when campaign creates it, else a reused existing checkout).

  **Classify the failure first** (see "Whitelist classification" below). Not whitelisted →
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
it: a CI-fix subagent, a review-fix subagent, or a whitelisted TOOL run with no model at all.** Every one
of them MUST, in the same step:

- **reset `reviews_ok` to 0 AND restore `gauntlet-reviewing` if the PR carries `gauntlet-accepted`**
  (`gh pr edit <pr> --remove-label gauntlet-accepted --add-label gauntlet-reviewing`) — the gate and its
  label move together, never one without the other (`stage-2-review-gate.md`, "Status labels mirror the
  review gate");
- **relaunch the CI watch immediately**;
- **re-enter Stage 2a.**

NEVER treat a tool-written commit as exempt: the verdicts on the old SHA describe content that no longer
exists, and a `gauntlet-accepted` label on it is a false public claim.

#### Whitelist classification — run the skill-owned TOOL, or the session model

Before dispatching anything, decide whether the failing check's fixer is a **whitelisted tool**. **A tool
is whitelisted ONLY IF it guarantees its output is SEMANTICALLY EQUIVALENT to its input** — an
AST-preserving pretty-printer, not a text munger — and the burden is the tool's **documented behaviour**.
**Cannot point to that guarantee → NOT whitelisted → session model.** There is NO blanket "formatters are
safe" rule.

That guarantee belongs to the whitelisted **tool's output** and to nothing else — a model hand-editing the
same file does NOT inherit it, however formatting-like its diff looks. A pure-indentation edit can move
behavior in a whitespace-significant language and still be formatter-clean, so there is no diff-shape
guard that makes a cheap model's edit safe to accept. **NO SUBAGENT IS EVER RUN ON A DOWNGRADED MODEL.**

**The SKILL owns the exact argv. Config does NOT supply a command.** The known-tools table below fixes,
per tool, the precise argv campaign may execute. The repo's `.gauntlet.yml` selects **only which of those
tools run, over which files**. The **CRITERION** is the skill's and is **NEVER configurable** — no config
key relaxes it.

**WHY config gets no flags — the flags carry the semantics.** Tool identity is NOT sufficient:

```
gofmt -w -r 'true -> false'     # argv[0] is gofmt. A known tool. No shell metacharacters.
```

`-r` turns `gofmt` from a pretty-printer into a **rewrite engine**: it rewrites `return true` into
`return false`. It would pass every identity check, every metacharacter check, and every denylist check.
So campaign REMOVES the degree of freedom instead of trying to police it: a config entry that supplies
`command`, `args`, `argv`, or any flag is **REFUSED**.

#### The KNOWN-TOOLS TABLE — the skill's, argv and all

The ONLY binaries campaign may execute on the no-model path, in the ONLY form it may execute them.
`<files>` = the tool's **default glob**, optionally NARROWED by the entry's validated `files`, with the
skill's **exclusion filter** applied afterwards — always (both below). Adding a tool, changing an argv, or
changing a default glob is a **SKILL change** (gated, reviewed), NEVER a config change.

| `id` | argv — **skill-owned, exact** | default `files` glob | guarantee | precondition — MUST hold in THIS repo |
|---|---|---|---|---|
| `gofmt` | `["gofmt", "-w", <files>]` | `**/*.go` | AST-preserving Go pretty-printer (`go/printer`); never alters string-literal contents | none |
| `gofumpt` | `["gofumpt", "-w", <files>]` | `**/*.go` | same printer, stricter layout rules; never alters string-literal contents | none |
| `goimports` | `["goimports", "-w", <files>]` | `**/*.go` | rewrites the **import block only** | none |
| `gci` | `["gci", "write", <files>]` | `**/*.go` | import grouping/ordering **only** — Go package init order is by **dependency**, not by import order in a file | none |
| `ruff format` | `["ruff", "format", <files>]` | `**/*.py` | verifies its output is **AST-equivalent** to the input | the repo's Ruff config does **NOT** enable `format.docstring-code-format` |

Every tool has a default glob, so a **missing `.gauntlet.yml` has a fully defined file set**: the table's
defaults, filtered. NEVER invent a default glob for a tool; NEVER widen one.

**NEVER append a flag to a table argv.** NEVER `-r`, NEVER `-s`, NEVER a catch-all `--fix`, NEVER anything
the table does not list. Execute it **WITHOUT a shell** — never `sh -c`, `bash -c`, `os.system`, or any
shell string.

**REMOVED — golangci-lint `whitespace`**: no safe fixer exists for it. Its only fix path is the catch-all
`golangci-lint run --fix`, which the denylist forbids. NEVER invent a command for it; a `whitespace`
failure goes to the session model.

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
table's, unchanged.

#### A tool's guarantee can be CONDITIONAL on its configuration

`ruff format` is the worked example. With `format.docstring-code-format` enabled, Ruff **reformats Python
code inside docstrings** — it rewrites the contents of a string literal, so its output is **NOT**
AST-equivalent. The AST-equivalence guarantee holds **only while that setting is OFF** (its default).

**The general rule:**

- The table states the **conditions under which each guarantee holds**. A guarantee with unstated
  conditions is not a guarantee.
- Campaign MUST **verify those conditions hold in THIS repo** before taking the no-model path — read the
  repo's tool config (for Ruff: `pyproject.toml` `[tool.ruff.format]`, `ruff.toml`/`.ruff.toml`) on the
  **base branch**, the same provenance rule as `.gauntlet.yml`.
- Condition enabled, OR **cannot be determined** → the tool is **NOT whitelisted for this repo** →
  session model. Default deny; NEVER assume a default.

#### NON-OVERRIDABLE DENYLIST — the skill's; `.gauntlet.yml` CANNOT widen past it

Nothing below is ever admitted to the table, and no config entry may name it:

- **`prettier`**: it reformats the **contents** of tagged template literals (`` gql`…` ``, `` css`…` ``),
  changing the runtime string the tag function receives — a semantic change made by the tool itself.
- any **generic or unscoped** "whitespace" / "trailing-whitespace" fixer that can rewrite content inside
  string literals, heredocs, or Markdown (e.g. trailing double-space hard breaks).
- a **semantic rewriter** — `modernize`, codemods, `pyupgrade`, `2to3`, any rule that rewrites logic. A
  `modernize` rewrite can PASS its own rule while CHANGING BEHAVIOR (e.g. `sort.Slice` → `slices.SortFunc`
  with a reversed or non-equivalent comparator): lint-clean, semantics changed.
- a **catch-all fixer** — `golangci-lint run --fix`, `ruff --fix`, `eslint --fix`, `cargo clippy --fix`, or
  any `--fix`/`--write` flag on a linter that applies semantic rules. A whitelisted run invokes **only the
  table's argv**, NEVER a catch-all `--fix`.
- any run that can touch a **check definition, config, or test** — the no-weakening prohibition, a hard
  rule, not a preference. Enforced by the skill's **exclusion filter** below, NEVER by trusting a glob.
- **NEVER whitelisted**: a failing product test (making a test pass is not the same as fixing the bug), a
  compile error, and any rule that rewrites logic.

Key it on the **tool's IDENTITY and its documented guarantee** — NEVER on a judgment that the failure
"looks mechanical", NEVER on the category "formatter". **Default deny. Unknown check, unlisted tool,
UNPARSEABLE config, an unresolvable binary, or a refused entry → session model.** (A **missing** config is
not a refusal: it means the known-tools table's defaults.)

#### THE SKILL-OWNED EXCLUSION FILTER — applied AFTER the glob, EVERY time

**The glob SELECTS candidates. The FILTER decides what is touched.** After expanding the tool's file set
(its default glob, narrowed by any validated `files`), campaign **REMOVES** every path below — always,
regardless of what the glob said, whether or not a config exists. **Config CANNOT widen this filter, and
NEVER carries the exclusions itself.**

Excluded — never handed to a tool, never in a tool commit:

- **tests**: `**/*_test.go`, `test/**`, `tests/**`, `**/testdata/**`, `**/__tests__/**`, `conftest.py`,
  `**/test_*.py`, `**/*_test.py`, `**/*.spec.*`, `**/*.test.*`
- **check definitions / CI**: `.github/**`, `.gitlab-ci.yml`, `Makefile`, `**/*.mk`, any CI workflow file
- **tool / lint / build config**: `.golangci.yml`/`.golangci.yaml`, `ruff.toml`/`.ruff.toml`,
  `pyproject.toml`, `setup.cfg`, `tox.ini`, `.editorconfig`, `.pre-commit-config.yaml`
- **campaign's own config**: `.gauntlet.yml`, and the git-ignored `.gauntlet/**`
- anything else that **defines, configures, or is** a check

**WHY the filter and not the glob:** an exclusion list a **USER** writes will omit something — one forgotten
pattern and a tool commit lands on a check definition with no model and no review. The skill owns the list,
so it is complete and it cannot rot per-repo. **A repo-relative filter is the guarantee; a refusal is not.**

Therefore `files: "**/*.go"` is **VALID and CORRECT**: the glob selects the Go files, the filter drops
`**/*_test.go` and everything else it must not touch. The user never enumerates an exclusion.

**Still REFUSE an OBVIOUSLY HOSTILE glob** — one that targets an excluded path **DIRECTLY** (`files:
".golangci.yml"`, `files: ".github/**"`, `files: "**/*_test.go"`): it is a config trying to weaken the
checks that gate it, and it MUST be logged and refused rather than silently emptied by the filter. But the
refusal is a **signal**, NEVER the guarantee — the filter is what makes the run safe.

**Empty file set after filtering → run NOTHING for that entry** and route the failure to the session model.

#### Repo config — `.gauntlet.yml`, read from the BASE branch

A hardcoded tool list is meaningless in a Rust/Java/Ruby repo, so the **selection** is repo-configurable —
the argv is not. The file lives at the **repo root** and is **COMMITTED** — it is NOT under the git-ignored
`.gauntlet/` tree (`files-and-ledger.md`).

**Each entry carries EXACTLY: `id` (required) and `files` (optional). Nothing else.**

```yaml
formatters:
  - id: gofmt              # MUST be an id in the known-tools table; the skill owns its argv
    files: "**/*.go"       # OPTIONAL — may only NARROW the tool's default glob; omit to take the default
```

**`files` may only NARROW the tool's default glob, NEVER widen it.** A `files` glob that matches paths the
default does not (e.g. `**/*` for `gofmt`, whose default is `**/*.go`) → **REFUSE**. Omitted `files` → the
table's default. Either way the exclusion filter applies.

**Read it from the BASE branch, NEVER from the PR's worktree or head:**

```
git show origin/<base>:.gauntlet.yml
```

**NEVER read `.gauntlet.yml` from the PR under review.** If campaign took the whitelist from the PR's own
head, a PR could **widen the whitelist that governs its own campaign** — selecting a tool and a glob and
earning an unreviewed tool commit on its own head. That is the self-gating hazard in config form. A PR that
edits `.gauntlet.yml` therefore takes effect only **after it merges**, gated like any other change.

#### TRUST MODEL — say it plainly

`.gauntlet.yml` is read from the **base branch**, so it is authored by people with **write access to the
repo** — the same people who can already edit `.github/workflows` and make CI do anything. The denylist and
the schema rules are therefore a **guard against footguns and accidental misuse, NOT a security boundary
against a malicious committer.** NEVER present them as one.

What the base-branch rule DOES buy is real, and MUST be kept: **a PR under review cannot widen the
whitelist that governs its own campaign.** That is the property being defended.

#### VALIDATION — an entry is ACCEPTED only if ALL of these hold

1. **`id` present; `files` optional. NOTHING else.**
2. **NO `command`, `args`, `argv`, `flags`, or any other key** — any of them → **REFUSE**. The skill owns
   the argv; config that supplies one is trying to re-open the `gofmt -r` hole. A shell string anywhere →
   REFUSE, and campaign never runs a shell regardless.
3. **`id` is in the known-tools table.** Not in the table → REFUSE. Config NEVER introduces a binary; it
   selects one. The binary is resolved to a **trusted absolute executable outside the repo** ("RESOLVE
   argv[0]" above) — NEVER from a `PATH` the repo or the PR can influence. Unresolvable, or resolving
   inside the repo/worktree → REFUSE.
4. **`files`, if present, only NARROWS the tool's default glob** — it MUST NOT match anything outside it.
   Widening → REFUSE. And REFUSE an **obviously hostile** glob that directly targets a check definition,
   config, or test (`.golangci.yml`, `.github/**`, `**/*_test.go`, …), or a repo-sweeping bare `**`/`.`.
   The **exclusion filter still applies to every accepted entry** — the refusal catches intent, the filter
   is the guarantee.
5. **The table's precondition for that `id` VERIFIES in this repo** (see "conditional on its
   configuration"). Cannot verify → REFUSE.

**REFUSING means: log the entry and why, IGNORE it, and route that failure to the session model. NEVER
silently honour a refused entry.** Refusing one entry does not invalidate the others.

**Merge semantics:** start from the built-in defaults (**the known-tools table's ids, each with its default
glob**); the repo's `formatters:` list **replaces** them by `id` — an entry NARROWS the `files` of a known
tool of the same `id`, and a built-in the repo omits while listing others is **removed** for that repo (e.g.
a repo that does not want `gci` simply lists the ones it wants). An entry whose `id` is not a known tool is
REFUSED, not appended. `formatters: []` **disables the cheap path entirely** — every failure goes to the
session model, always a safe choice. **Missing file → the table's built-in defaults, each with its default
glob, exclusion filter applied as always** — NEVER an invented or broadened default. **Unparseable file →
cheap path OFF for this run** (default deny; never guess at a half-parsed whitelist).

Then, in order:

1. **Whitelisted tool → run the TOOL, no model (prefer this always).** In `<worktree>`, run the
   **table's exact argv** for that `id` with `argv[0]` **resolved to a trusted absolute executable outside
   the repo**, **WITHOUT a shell**, over the tool's file set (default glob, narrowed by any validated
   `files`, **exclusion filter applied**). NEVER add a flag; NEVER a catch-all `--fix`. ACCEPT only if
   **both** hold: re-running the **exact** failing check now **passes**, AND the diff touches **no check
   definition, config, or test**. Then commit + push — **zero model spend** — and **apply the gate reset**
   above ("Any campaign commit to the PR head resets the gate"): `reviews_ok` to 0 + relabel, relaunch the
   watch, re-enter 2a. A tool commit gates exactly like a subagent commit.
2. **Everything else → the scoped CI-fix subagent on the session model**, set explicitly, per the red-CI
   dispatch above. Covers: the tool did not fix it, the tool left residue, the tool/check is not
   whitelisted, the config entry was refused, the config is **unparseable**, the binary **cannot be
   resolved to a trusted executable outside the repo**, the filtered file set is **empty**, or the failure
   needs any judgment. (A **missing** config is NOT in this list — it means the table's defaults.)

If the tool's run fails either acceptance point → **discard the work** (reset the worktree to the PR
head) and **re-dispatch the same failure on the session model**. NEVER patch a formatter run in place;
NEVER commit an unverified one; NEVER hand the failure to a cheap model instead.

**Residual risk, stated honestly:** the whitelist stands on the binary actually BEING the tool (what the
outside-the-repo resolution buys) and on each tool's documented guarantee — a tool bug, or a repo
config/plugin that switches on non-formatting rules, is the rest of the exposure.
Run whitelisted tools with the project's own config and no extra rule sets, and NEVER re-derive the
whitelist's safety from the review gate: it stands on the TOOL being incapable of changing semantics, or it
does not stand at all.

Every CI failure must be handled; never merge over a red or pending check, and never infer green from
the watch's exit code alone — always confirm against the re-polled snapshot.

CI fixes serialize only within one PR/SHA. Different PRs with red CI may run scoped CI-fix subagents
concurrently within the dispatcher cap.

---
