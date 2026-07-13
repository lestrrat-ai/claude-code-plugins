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
`<files>` = the paths matched by the entry's validated `files` glob. Adding a tool, or changing an argv,
is a **SKILL change** (gated, reviewed), NEVER a config change.

| `id` | argv — **skill-owned, exact** | guarantee | precondition — MUST hold in THIS repo |
|---|---|---|---|
| `gofmt` | `["gofmt", "-w", <files>]` | AST-preserving Go pretty-printer (`go/printer`); never alters string-literal contents | none |
| `gofumpt` | `["gofumpt", "-w", <files>]` | same printer, stricter layout rules; never alters string-literal contents | none |
| `goimports` | `["goimports", "-w", <files>]` | rewrites the **import block only** | none |
| `gci` | `["gci", "write", <files>]` | import grouping/ordering **only** — Go package init order is by **dependency**, not by import order in a file | none |
| `ruff format` | `["ruff", "format", <files>]` | verifies its output is **AST-equivalent** to the input | the repo's Ruff config does **NOT** enable `format.docstring-code-format` |

**NEVER append a flag to a table argv.** NEVER `-r`, NEVER `-s`, NEVER a catch-all `--fix`, NEVER anything
the table does not list. Execute it **WITHOUT a shell** — never `sh -c`, `bash -c`, `os.system`, or any
shell string.

**REMOVED — golangci-lint `whitespace`**: no safe fixer exists for it. Its only fix path is the catch-all
`golangci-lint run --fix`, which the denylist forbids. NEVER invent a command for it; a `whitespace`
failure goes to the session model.

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
  rule, not a preference. Enforced at the config layer by the `files` constraint below.
- **NEVER whitelisted**: a failing product test (making a test pass is not the same as fixing the bug), a
  compile error, and any rule that rewrites logic.

Key it on the **tool's IDENTITY and its documented guarantee** — NEVER on a judgment that the failure
"looks mechanical", NEVER on the category "formatter". **Default deny. Unknown check, unlisted tool,
missing/unparseable config, or a refused entry → session model.**

#### Repo config — `.gauntlet.yml`, read from the BASE branch

A hardcoded tool list is meaningless in a Rust/Java/Ruby repo, so the **selection** is repo-configurable —
the argv is not. The file lives at the **repo root** and is **COMMITTED** — it is NOT under the git-ignored
`.gauntlet/` tree (`files-and-ledger.md`).

**Each entry carries EXACTLY two fields: `id` and `files`. Nothing else.**

```yaml
formatters:
  - id: gofmt              # MUST be an id in the known-tools table; the skill owns its argv
    files: "**/*.go"       # glob this tool may touch — MUST NOT reach a check def, config, or test
```

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

1. **EXACTLY the fields `id` and `files`.** Both present.
2. **NO `command`, `args`, `argv`, `flags`, or any other key** — any of them → **REFUSE**. The skill owns
   the argv; config that supplies one is trying to re-open the `gofmt -r` hole. A shell string anywhere →
   REFUSE, and campaign never runs a shell regardless.
3. **`id` is in the known-tools table.** Not in the table → REFUSE. Config NEVER introduces a binary; it
   selects one. Resolve the binary from `PATH` at run time and NEVER honour a `PATH` entry the repo's own
   config injected.
4. **`files` is a glob that CANNOT match a check definition, config, or test path.** REFUSE any glob that
   can reach `.golangci.yml`, `.github/**`, `**/*_test.go`, `test/**`, `tests/**`, `conftest.py`,
   `pyproject.toml`, `ruff.toml`/`.ruff.toml`, `.gauntlet.yml`, or any other check/config/test path. A
   bare `**` or `.` that sweeps the repo → REFUSE. This is the no-weakening prohibition, enforced at the
   config layer instead of trusted to the tool.
5. **The table's precondition for that `id` VERIFIES in this repo** (see "conditional on its
   configuration"). Cannot verify → REFUSE.

**REFUSING means: log the entry and why, IGNORE it, and route that failure to the session model. NEVER
silently honour a refused entry.** Refusing one entry does not invalidate the others.

**Merge semantics:** start from the built-in defaults; the repo's `formatters:` list **replaces** them by
`id` — an entry re-scopes the `files` of a known tool of the same `id`, and a built-in the repo omits while
listing others is **removed** for that repo (e.g. a repo that does not want `gci` simply lists the ones it
wants). An entry whose `id` is not a known tool is REFUSED, not appended. `formatters: []` **disables the
cheap path entirely** — every failure goes to the session model, always a safe choice. **Missing file →
built-in defaults only. Unparseable file → cheap path OFF for this run** (default deny; never guess at a
half-parsed whitelist).

Then, in order:

1. **Whitelisted tool → run the TOOL, no model (prefer this always).** In `<worktree>`, run the
   **table's exact argv** for that `id`, **WITHOUT a shell**, over the paths its validated `files` glob
   matches. NEVER add a flag; NEVER a catch-all `--fix`. ACCEPT only if **both** hold: re-running the
   **exact** failing check now **passes**, AND the diff touches **no check definition, config, or test**.
   Then commit + push — **zero model spend** — and **apply the gate reset** above ("Any campaign commit to
   the PR head resets the gate"): `reviews_ok` to 0 + relabel, relaunch the watch, re-enter 2a. A tool
   commit gates exactly like a subagent commit.
2. **Everything else → the scoped CI-fix subagent on the session model**, set explicitly, per the red-CI
   dispatch above. Covers: the tool did not fix it, the tool left residue, the tool/check is not
   whitelisted, the config entry was refused, the config is missing/unparseable, or the failure needs any
   judgment.

If the tool's run fails either acceptance point → **discard the work** (reset the worktree to the PR
head) and **re-dispatch the same failure on the session model**. NEVER patch a formatter run in place;
NEVER commit an unverified one; NEVER hand the failure to a cheap model instead.

**Residual risk, stated honestly:** the whitelist is only as strong as each tool's documented guarantee —
a tool bug, or a repo config/plugin that switches on non-formatting rules, is the whole of the exposure.
Run whitelisted tools with the project's own config and no extra rule sets, and NEVER re-derive the
whitelist's safety from the review gate: it stands on the TOOL being incapable of changing semantics, or it
does not stand at all.

Every CI failure must be handled; never merge over a red or pending check, and never infer green from
the watch's exit code alone — always confirm against the re-polled snapshot.

CI fixes serialize only within one PR/SHA. Different PRs with red CI may run scoped CI-fix subagents
concurrently within the dispatcher cap.

---
