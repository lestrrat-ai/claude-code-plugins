## Rules

- Runs are isolated by `run_id`: a run touches ONLY its own `<rundir>`, its `state.jsonl`, and the PRs
  carrying its `gauntlet-run-<run-id>` label. Adopted PRs keep their OWN head branch, so ownership is
  scoped by that LABEL only (never a branch prefix). NEVER reconcile, review, fix, merge, relabel, or
  clean up another run's work — scope every git/gh scan by that label.
- One active driver per run, enforced by `<rundir>/lease.json` under an atomic `mkdir <rundir>/claim.lock`:
  take/adopt a run only inside the claim lock, and adopt ONLY when its lease is absent or stale
  (`now - updated` > ~30 min); refresh the lease every wake AND around long foreground ops; on a
  self-wake whose lease is fresh but bears a different token, **stand down** — never double-drive a ledger.
- Every self-wake carries `--run <run-id> --token <agent-token>` (ScheduleWakeup + background
  completions); the token re-proves lease ownership so a summarized wake never mistakes its own run for
  another's. Re-read `run_id` from the ledger each wake, never from memory.
- Resume is intent-scoped: a fresh instance resumes via `--run <id>` or an **arg-less** bare invocation
  (adopts the sole orphaned run). A bare invocation **with `#PR` args** and `--new` start an independent
  new run (adopting those PRs) and never pre-empt other live runs; a **non-PR** arg starts nothing —
  it hits the idle prompt.
- Carryover is **one file per run** under `.gauntlet/history/<run-id>.md`. In normal operation a run
  WRITES only its OWN file, so concurrent runs never contend on a shared rewrite. A **fresh** run,
  while pruning history, MAY edit or remove OTHER runs' files — but only those of **finished** runs
  (no live writer/lease), never a file an actively-driven run owns — so there's still no write
  contention with a live writer.
- Run-owned git/GitHub operations are authorized by invocation: `add`, `commit`, `push`, and — on
  adopted PRs — PR update, labels/checks/comments, and merge. Campaign never opens its own PR (PR
  creation lives only in the `gauntlet:review` handoff); it ADOPTS existing PRs. Ask only for public
  API changes, active-run takeover, uncertain carryover pruning, or out-of-scope/destructive work.
- NEVER commit the run's own bookkeeping — the whole `.gauntlet/**` tree: the `<rundir>` under
  `.gauntlet/tmp/**` (ledger, plans, progress, review/CI outputs, lease) and the carryover tree
  `.gauntlet/history/**`. A fix commit stages ONLY the specific source files it changes, by explicit
  path — never `git add -A` / `git add .`, which would sweep in run state. The tree must be
  git-ignored; add `.gauntlet/` to `.gitignore` if missing. Campaign has **NO committed file of its own** —
  no repo-root config; the formatter whitelist is the ledger header's `formatters` field, never a repo file.
- NEVER `rm -rf .gauntlet/` — that destroys the durable carryover history. Only `.gauntlet/tmp/**` is
  disposable.
- NEVER pass destructive instructions (delete, force-push, reset) to an external reviewer command
  (e.g. `codex exec`).
- NEVER use `--dangerously-bypass-approvals-and-sandbox` with an external reviewer; always
  `--sandbox workspace-write`.
- One PR = one unit. Campaign gates whole adopted PRs; do not split or bundle them.
- PR-gating loop is mandatory: adopt PR → triage tier → watch CI + review PR HEAD → merge. Campaign
  gates **existing** PRs; it NEVER writes fixes from scratch (only review/CI fixes on an adopted PR).
- Concurrency is a **rolling cap (~8 in flight), never a barrier wave**: keep up to ~8 review passes
  and ~8 CI-fix subagents in flight, backfilling each freed slot immediately. Never let a draining
  group of PRs stall the backlog — Loop-control step 3 owns this refill.
- Work-conserving dispatch is mandatory: every wake scans all PRs and launches every due
  action that fits a free slot before returning. Waiting is allowed only when no useful action is
  launchable anywhere in the run.
- A pending-CI PR must ALWAYS have a live watch: if the CI snapshot reads pending and the watch task
  has exited (including after any rebase/push), relaunch the watch in the same wake — never wait for
  the heartbeat.
- Stop a PR's in-flight review before dispatching content-changing work on it (review fix, CI fix,
  copilot-address, conflict-resolving rebase): a verdict on a doomed SHA wastes tokens and a review
  slot. Refill the slot with the next due review.
- Reconcile from ONE batched `gh pr list --label gauntlet-run-<run-id> --json …` snapshot per wake
  (`<rundir>/prs.json`); per-PR `gh` calls only where the snapshot falls short. Merge-gate CI truth
  stays the re-polled `gh pr checks` snapshot.
- Carryover pruning NEVER blocks a fresh-run start: keep uncertain entries, adopt the run's PRs
  immediately, ask the user asynchronously, and fold the answer in as its own wake.
- Public API surface/behavior changes need user confirmation by default (see Constraints). The
  `api_changes` flag lives in the ledger header and is re-read every wake — never trust memory, never
  auto-merge an unapproved API break.
- Before queueing a review pass on a PR, clear its preconditions on the current tip: address any
  GitHub Copilot review items (`/gauntlet:copilot-address-reviews <pr>`), fix any CI failures (one at a time,
  prefer a scoped subagent), and rebase away any conflict with `<base>`. PR-content changes reset
  verdicts. Clean base-only rebase with unchanged PR diff keeps `reviews_ok` and sets `ci = pending`.
  Never spend a review over open Copilot items, a red check, or a conflicting PR (Stage 2a).
- The review gate is **tier-dependent**: `required(tier)` fresh, context-isolated `SATISFIED` verdicts
  on the same live PR content — **one if TRIVIAL, two otherwise** (any code / agent-doc / sensitive
  change always requires two). Re-derive the tier from `head_sha` each wake.
- **NEVER leave `gauntlet-accepted` on a PR whose live content no longer holds `required(tier)`
  SATISFIED verdicts.** The label is a projection of `reviews_ok`, and it is the only run state a human
  sees on GitHub — a stale `gauntlet-accepted` publicly claims a PR passed a gauntlet it did not. So the
  **gate and the label move together, in the same step**: every action that drops `reviews_ok` to 0 (a
  `NOT SATISFIED` verdict, a review/CI/copilot fix commit, a conflict-resolving rebase, any other
  content change on the head branch) MUST also run `gh pr edit <pr> --remove-label gauntlet-accepted
  --add-label gauntlet-reviewing`. Never defer the swap to the next wake — that leaves the label lying
  until reconcile, and lying forever if the session dies first. A **clean base-only rebase** with an
  unchanged PR diff does NOT reset the gate, so it correctly KEEPS `gauntlet-accepted` (it only sets
  `ci = pending`). Per-wake label reconcile is the self-healing backstop, never the mechanism
  (`stage-2-review-gate.md`, "Status labels mirror the review gate").
- Reviews are fresh, context-isolated re-rolls: a separate reviewer invocation each pass (Claude
  subagent by default, or the user's preferred reviewer), no shared context. A second pass re-rolls a
  stochastic reviewer to catch a missed defect — the two are NOT statistically independent (the same
  diff, task, and protocol correlate them; same-reviewer passes also share model/prompt), so the gate
  is a miss-catcher, not a proof of correctness.
- Before each review, write an orchestrator-owned `review-<pr>-<n>.plan.jsonl` (per-pass — a relaunch
  reuses it); reviewers append progress events against planned units to the **active launch attempt's**
  progress file (`review-<pr>-<n>.progress.jsonl` for attempt 1, `review-<pr>-<n>.a<k>.progress.jsonl`
  for a relaunch — only the attempt named in the active `pass_identity` is read or counted). Meaningful
  progress = planned unit `done` or accepted plan amendment, not vague "still working" output. Two
  distinct bars, never collapsed: **launch evidence** = ANY reviewer-written line after `pass_identity`
  (a `started`/`done` `progress` event *or* a `plan_amendment_request`) — none within ~5 min of dispatch
  → the pass never started → kill + relaunch into attempt-scoped artifacts per the Stage 2a launch
  check. **Meaningful progress** is the stronger bar (`done`/accepted amendment) — stale for ~15 min →
  suspicious review → retry/fallback per Stage 2a. Both bars judge a **live** process. A pass whose
  task is **dead** with no verdict (killed session) ignores launch evidence entirely and dispatches on
  `launch_attempt` alone: `1` → relaunch once; `2` → fresh-subagent fallback. Never leave a dead pass
  on neither branch.
- Reviewers do not own the plan but must not treat it as presumptively complete: critically evaluate
  its coverage first, and raise any omitted dimension or materially wrong unit via a
  `plan_amendment_request` event rather than silently reviewing only the listed units. Never rewrite
  the plan or self-grant units (Stage 2a).
- After finishing every planned unit, a pass runs a brief UNSTRUCTURED ADVERSARIAL SWEEP for defects
  outside the plan's decomposition (cross-unit interactions, unstated assumptions, edge cases,
  unenumerated categories). It complements — never replaces — the plan, reports only concrete
  `file:line` defects at the normal finding bar (a real one → NOT SATISFIED), and treats "nothing
  found" as a fine result; no speculative "might be fragile" notes (Stage 2a).
- A SATISFIED verdict carries one `RESIDUAL-RISK: <area> — <why>` line (the least-certain part of the
  diff). It is calibration metadata, never a finding: it never withholds the gate, never enters the fix
  loop, and is never fed into the corroborating review. Do not manufacture a concern to fill it; a real
  defect found while identifying it is a normal finding → NOT SATISFIED (Stage 2a).
- One decision at N sites is the most common root cause. Trigger the §2a-deep root-cause pass on the
  **first** "missing/wrong at site X" finding (its shape, not a round count), map the whole space with
  a dedicated **read-only mapper** subagent — never one that also fixes, which under-maps toward what
  it can reach — and fix at a **single chokepoint**. Hard backstop: a 2nd `NOT SATISFIED` on one PR
  forces the pass (Bailout).
- When a PR's tier requires two reviews they run **sequentially, never queued together**: launch the
  first, wait for its verdict, and launch the second **only if the first came back SATISFIED**. A
  NOT-SATISFIED first review means a fix lands and the SHA changes, so a concurrently-queued second
  review would be burned on a commit that's about to be replaced — wasted tokens. A **TRIVIAL** PR
  needs only one SATISFIED pass, so there is no second review to sequence. (Reviews for *different* PRs
  still run concurrently; only the two for the same PR serialize. See Stage 2a.)
- Verdicts are pinned to reviewed PR content: any PR-content change (review fix / CI fix /
  conflict-resolving rebase / bot or manual PR-branch commit) makes prior verdicts stale. Base
  advancement with no conflict and unchanged PR diff does NOT invalidate verdicts; carry `reviews_ok`
  forward, update `head_sha`, and require fresh CI.
- Resume vs. fresh run is decided by **liveness**, not by `state.jsonl` existing: live work → resume;
  a finished prior run → ask the user before a fresh run; `--new` → fresh run with
  carryover (Loop control step 1). A finished run must never silently exit "all done" or silently
  restart.
- A fresh run carries over prior knowledge from `.gauntlet/history/` (merged/aborted PR record, to
  dedup and inform) but still judges every adopted PR fresh — carryover is advisory, never
  auto-accept/reject.
- Prune `.gauntlet/history/` at every fresh run: drop only entries unambiguously moot against
  current `<base>`; for anything uncertain, list it and ask the user before deleting. Never silently
  prune an entry you're unsure about.
- **Set the model explicitly on EVERY subagent dispatch** (`SKILL.md`, "Subagent Dispatch"). An unset
  model silently inherits the session model — a cost decision taken by default. **NO SUBAGENT IS EVER RUN
  ON A DOWNGRADED MODEL**: review passes and the subagent-fallback review *are* the gate; CI-fix and
  review-fix write **code that gets merged**; the root-cause **mapper** feeds an expensive fix. Nothing
  downstream *guarantees* a bad result is caught — CI misses a wrong-but-green fix, and the review gate is
  a miss-catcher, not a proof of correctness — so no class's mistakes are reliably absorbed. NEVER claim CI
  catches a bad fix. NEVER downgrade the mapper on the grounds that it is "read-only": read-only is not
  low-judgment, and an under-map is invisible.
- **The only cheap path: a CI check whose fixer is a WHITELISTED TOOL — run the TOOL, no model at all**
  (`SKILL.md`, "The only cheap path"; full table/schema/validation in `stage-2-ci.md`). **A tool is
  whitelisted ONLY IF it guarantees its output is SEMANTICALLY EQUIVALENT to its input** — an AST-preserving
  pretty-printer, not a text munger — on the burden of a **CITED SOURCE: a LINK to the tool's own
  documentation and the passage it rests on, QUOTED. The word "documented" is NOT a citation, and a citation
  that does NOT SUPPORT its claim is WORSE than none** — it launders our own belief as the source's. Cannot
  point to a source → NOT whitelisted → session model. There is **NO blanket "formatters are safe" rule**. The
  guarantee is the TOOL's and does **NOT** transfer to a model hand-editing the same file — a diff that
  *looks* like formatting is NOT a proof of semantic equivalence (a pure-indentation edit can move behavior
  in a whitespace-significant language and stay formatter-clean).
- **THE GUARANTEE = THE CRITERION + THE OPERAND CHECKS — no list is part of it.** The criterion bounds
  **what the tool can do**: `gofmt` re-prints the program without changing its meaning, so **whatever it
  touches, it CANNOT change the meaning of** — a reformatted test asserts exactly what it asserted before,
  and **weakening a check requires a SEMANTIC change the skill-owned argv cannot make**. The **seven operand
  checks** bound **what we write**. Together they are what make the no-model path safe, and they depend on
  **NO list being complete**. NEVER re-derive that safety from the exclusion filter.
- **THE SKILL OWNS THE EXACT ARGV; NOTHING ELSE SUPPLIES A COMMAND.** The known-tools table
  (`stage-2-ci.md`) fixes each tool's precise argv (today: `gofmt -w --` over that id's normalized files).
  **Flags carry the semantics** — `gofmt -w -r 'true -> false'`
  rewrites `return true` into `return false` with a known `argv[0]` and no shell metacharacters, so tool
  identity alone is NOT sufficient. The whitelist therefore carries **ids and globs only** — it can NEVER
  name a `command`/`args`/`argv`/flag. NEVER append a flag to a table argv. Adding a tool, or changing an
  argv, is a **SKILL change**.
- **THE TABLE HOLDS ONE TOOL — `gofmt`. The criterion was applied TO OURSELVES.** Its cell quotes
  https://pkg.go.dev/cmd/gofmt (*"Gofmt formats Go programs. It uses tabs for indentation and blanks for
  alignment"*). **`-w`** — *"If a file's formatting is different from gofmt's, overwrite it with gofmt's
  version"* — **CHANGES THE SOURCE: it writes the formatted result back to the file. That is what we WANT,
  and it is the ONLY mutation the skill-owned argv performs.** **`-r`** — *"Apply the rewrite rule to the
  source before reformatting"* — and **`-s`** — *"Try to simplify code"* — are the only documented flags that
  apply a **non-formatting source TRANSFORMATION** (they change the PROGRAM, beyond re-printing it), and
  **NEITHER is in the skill-owned argv**. **`-cpuprofile` is a separate documented flag that WRITES A FILE**
  (it does not transform the source); it is named only because a FILENAME shaped like `-cpuprofile=x.go` is
  the injection repro. The doc lists others still (`-l`, `-d`, `-e`, …). **GUARD — this claim has been stated
  wrongly THREE times: NEVER say "exactly two flags", "the only two flags", or "the only flags that CHANGE
  the source". State ONLY the TRANSFORMATION property.** Safety rests on: the argv is **skill-owned and
  exact**; **NO flag is ever appended**; and
  **no file operand can be read as a flag** (`--` + the operand-normalization rules) — the injection repro
  below is a file named `-cpuprofile=prof.go`. Each `guarantee` cell MUST carry a **LINK to the tool's own doc and the passage
  QUOTED**; **FOLLOW the link before trusting the cell** — a claim that cannot be tied to a source means the
  tool comes **OUT of the table**. **The cheap path therefore covers Go formatting and NOTHING else; every
  other CI failure — ALL Python/JS/etc formatting included — goes to the SESSION MODEL.** That is the safe
  default, not a regression, and the small table is **the rule working — NEVER a limitation to route
  around**. **REJECTED, and re-adding one is a SKILL change that must first defeat the reason** — the bar is
  **a source that STATES the guarantee, quoted in the cell**; "it is a formatter" / "probably fine" /
  "widely used" are NOT admissible: **`ruff format`** (https://docs.astral.sh/ruff/formatter/ — the cited
  docs state **no AST-equivalence guarantee**); **`gci`** (https://github.com/daixiang0/gci — the cited docs
  describe import ordering/grouping and **never state** it neither adds nor removes an import); `goimports`
  (https://pkg.go.dev/golang.org/x/tools/cmd/goimports) — **ADDS missing imports and REMOVES unreferenced
  ones**; an added import runs that package's `init()` and a guessed import can be the **wrong package** →
  not semantics-preserving. `gofumpt` (https://github.com/mvdan/gofumpt) — **extra rewrite rules beyond
  gofmt's layout**, stated as a rule list, **never** as semantics-preserving; "it is basically
  gofmt" is NOT an argument. Fewer tools that provably hold beats more that mostly do.
- **NORMALIZE THE FILE ARGV — FILENAMES ARE PR-CONTROLLED DATA** (`stage-2-ci.md`). The skill owns the argv
  **SHAPE**, not the operands: `<files>` is globbed from the **PR's worktree**, so every filename is
  attacker-controlled data spliced into argv, and MUST be normalized like any injection boundary. **Repro:**
  a PR adds a file named `-cpuprofile=prof.go`; it matches `**/*.go`, survives the exclusion filter, and
  `gofmt -w '-cpuprofile=prof.go' a.go` exits 0 having parsed it as a **FLAG** and written a CPU profile —
  trusted binary, skill-owned argv, no shell, no model, and PR content still steered the command. Therefore,
  EVERY tool, EVERY run: (a) pass **`--`** before the file list — a tool without `--` NEVER enters the table;
  (b) pass every file as an **ABSOLUTE** (or `./`-prefixed) path — **NEVER a bare relative name**, which is
  belt-and-braces even if `--` fails; (c) **REFUSE any candidate name starting with `-`** — **DROP that file**
  and log it; do **NOT** abort the run. NEVER pass the glob itself to the tool.
  **NEVER invoke the tool with an EMPTY operand set — `gofmt` with no operands READS STDIN.** Empty set after
  refusals → run nothing for that id → session model.
- **A PATH IS DATA THAT RESOLVES — NORMALIZING ITS SPELLING IS NOT ENOUGH** (`stage-2-ci.md`). **Repro:** the
  PR adds `link.go`, a **symlink** pointing outside the worktree. It matches `**/*.go`, survives the exclusion
  filter, and its name is clean — and `gofmt -w -- link.go` **FOLLOWS it and rewrites the target OUTSIDE the
  repo**. So, on every candidate — **AFTER the exclusion filter, BEFORE the argv is built**: **REFUSE a
  SYMLINK** (`lstat` the candidate itself; NEVER `stat`, which follows the link and hides the very thing being
  tested — a source file that must be formatted is never a symlink); **REFUSE a candidate whose fully-resolved
  real path (`realpath`, symlinks followed, `..` collapsed) is not CONTAINED under the resolved worktree root**
  (compare on a path-component boundary — `/wt-evil` is not under `/wt`); and **REFUSE a candidate that is not
  a REGULAR FILE** (directory, device, fifo, socket). **DROP** each and **LOG** it (id, path, reason —
  symlink escape / escapes the worktree / not a regular file); do NOT abort. Empty set → session model.
- **A PATH CHECK BOUNDS WHERE WE LOOK; IT DOES NOT BOUND WHAT WE WRITE — THE INODE CAN BE ALIASED OUTSIDE THE
  TREE** (`stage-2-ci.md`). **Repro:** the PR adds `alias.go`, a **HARDLINK** sharing an INODE with a file
  outside the worktree. It is a **regular file**, it is **not a symlink** (`lstat` says regular), and its
  **`realpath` is INSIDE the worktree** — it passes every check above. `gofmt -w` truncates and rewrites the
  **EXISTING INODE**, so formatting it **MUTATES THE OUTSIDE ALIAS**. Therefore, the sixth check: **REFUSE any
  candidate with a LINK COUNT > 1** (`stat` → `st_nlink > 1`). A source file in a normal checkout has exactly
  **one** link; a multi-link file is a hardlink escape or something we have no reason to format. **DROP** and
  **LOG** it (id, path, reason — `hardlink — nlink>1`); **NEVER abort the run**. Empty set → session model.
- **EVERY CHECK THAT REASONS ABOUT A PATH MUST REASON ABOUT THE RESOLVED PATH — A NAME IS NOT A LOCATION**
  (`stage-2-ci.md`). **Repro:** the PR adds a **symlinked DIRECTORY** `safe/gh -> .github`. The candidate
  `safe/gh/actions/main.go` is **not itself a symlink**, its **`realpath` is INSIDE the worktree**, it is a
  **regular file with `nlink == 1`**, and its name is clean — it passes all six checks above — while the
  **exclusion filter, matched on the SPELLED path, never sees `.github/**`**. A **check definition** is handed
  to the tool, with no model and no review. Therefore: **apply the skill-owned EXCLUSION FILTER to the
  RESOLVED path as well as the original — EITHER matches → REFUSE**; **AND the seventh check: REFUSE any
  candidate with a SYMLINK in ANY DIRECTORY COMPONENT** (walk the components from the resolved worktree root
  down, `lstat` each; any symlink → **DROP** + **LOG**, reason `symlinked directory component` /
  `excluded after resolution`). The component check bounds **what we write**; matching the filter on the
  resolved path keeps the filter honest (it is **defence in depth**, never the guarantee). Run **BOTH**.
  **THE PIPELINE:** glob → **RESOLVE** → **FILTER both spellings** → the **seven** checks → argv.
- **RESOLVE `argv[0]` TO A TRUSTED ABSOLUTE EXECUTABLE OUTSIDE THE REPO** (`stage-2-ci.md`). The tool runs
  in the PR's worktree and **the PR under review is untrusted content**: a bare name resolved from a `PATH`
  the PR can influence lets it ship its own `gofmt` and earns it **arbitrary code execution on the path that
  runs with no model and no review**. Before every run, resolve `argv[0]` to an **absolute** path using a
  `PATH` stripped of `.`, `..`, empty entries, **all relative entries**, the worktree, the repo root, and any
  directory inside them; the resolved executable (symlinks followed) **MUST live OUTSIDE the repo/worktree
  tree**. Resolves inside, or does not resolve → **REFUSE → session model**. NEVER run the tool with the
  worktree on `PATH` or as the lookup base. NEVER execute a binary the PR could have supplied.
- **THE SKILL OWNS A NON-OVERRIDABLE EXCLUSION FILTER — DEFENCE IN DEPTH, NOT THE GUARANTEE.** Applied to
  the RESOLVED path **and** the original, EVERY time (**either** matches → REFUSE; a filter matched on the
  spelling alone is defeated by a symlinked directory — see the resolved-path rule above). It drops tests
  (`**/*_test.go`, `test/**`, `tests/**`, `**/testdata/**`, `conftest.py`, …), check definitions
  (`.github/**`, …), CI/tool/build config (`.golangci.yml`, `ruff.toml`, `pyproject.toml`, …), and
  `.gauntlet/**`. **It is an enumerated PATTERN LIST: NOT complete, and it CANNOT be** — a repo-specific
  check written as ordinary source (`tools/ci/check.go`) matches `**/*.go`, matches **none** of the
  patterns, passes every operand check, and **is** handed to the tool. **NEVER call it complete, exhaustive,
  or the thing that makes the no-model path safe** (that is the criterion + the operand checks, above). Its
  job is **BLAST RADIUS**: keep the tool's diff off files a reviewer expects untouched. **NOTHING widens it**
  — config may only NARROW — and the whitelist NEVER carries the exclusions itself: a user-written exclusion
  list omits more and rots per-repo. The glob SELECTS; the filter TRIMS. A `gofmt:**/*.go` narrowing is
  therefore VALID. A refusal of an obviously hostile glob catches **intent**, not weakening.
- **THE FORMATTER WHITELIST LIVES IN THE LEDGER HEADER (`formatters`), NEVER IN A REPO FILE.** Resolved
  **once at run start** from the **user** — explicit invocation, else a preference in memory, else the
  known-tools table's built-in defaults (`default`). **The DISABLING SENTINEL is `-`, NEVER the word
  `none`**: `-` turns the cheap path OFF (every CI failure → session model); `default` (and an **unset**
  field) means the built-in set. The two are NEVER interchangeable. **Re-read it from the header every
  wake; NEVER re-derive it from memory mid-run** (a wake may be a fresh agent instance — same rule and same
  reason as `reviewer`). **NEVER take it from repo content: not from a repo-root config file, not from
  `CLAUDE.md`, not from ANY file in the repo.** Repo content **is PR content** — a PR that could edit the
  whitelist could **widen the whitelist that governs its own campaign**, earning an unreviewed tool commit
  on its own head (self-gating). Out of the repo, a PR cannot touch it **by construction**: there is no
  provenance rule to enforce, and none exists — do NOT reintroduce one.
- **The whitelist carries EXACTLY known-tool ids + an OPTIONAL NARROWER glob** (`gofmt:internal/**/*.go`).
  Every known tool has a **default glob** in the table (`gofmt` → `**/*.go`),
  so an **unset `formatters` has a fully defined file set: the table's defaults, filtered**. A glob may only
  NARROW that default → a widening glob is REFUSED; so is an **obviously hostile** one that directly targets
  a check def, config, or test (`gofmt:.golangci.yml`), or a repo-sweeping bare `**`/`.`. A named list
  **replaces** the built-ins — an omitted known tool is not run. It **NEVER introduces a binary outside the
  known-tools table**. **Any id failing any rule is REFUSED** — log it, ignore it, route that failure to the
  **session model**. NEVER silently honour a refused id.
- **golangci-lint `whitespace` is NOT in the table**: no safe fixer — its only fix path is the denylisted
  catch-all `golangci-lint run --fix`. NEVER invent a command for it.
- **NON-OVERRIDABLE DENYLIST — NOTHING widens it**: `prettier` — it reformats the contents of tagged
  template literals (`` gql`…` ``, `` css`…` ``), changing the runtime string the tag receives; any
  generic/unscoped "whitespace" or "trailing-whitespace" fixer that can touch string literals, heredocs, or
  Markdown; every **semantic rewriter** — `modernize`, codemods, `pyupgrade`, `2to3` (a `modernize` rewrite
  can pass its own rule while changing behavior); every **catch-all fixer** — `golangci-lint run --fix`,
  `ruff --fix`, `eslint --fix`, `cargo clippy --fix`, any `--fix`/`--write` flag on a linter that applies
  semantic rules (a whitelisted run invokes ONLY the table's argv). Failing product tests, compile errors,
  and any rule that rewrites logic are NEVER whitelisted.
- **TRUST MODEL — do NOT overclaim.** The formatter whitelist is the **user's** (invocation or their own
  memory), not repo content, so there is no malicious-committer threat model here at all: the denylist, the
  criterion, and the id-only shape are a **guard against footguns and accidental misuse, NOT a security
  boundary**. NEVER present them as one. What IS a boundary: **the tool runs on UNTRUSTED PR CONTENT inside
  the PR's worktree** — which is exactly why `argv[0]` resolves to a trusted executable **outside the repo**,
  why the **file operands are normalized, resolved, checked for aliasing, AND filtered on the RESOLVED path**
  (they are PR data spliced into
  argv; a **symlink** among them walks the tool out of the tree, a **hardlink** walks the WRITE out of the
  tree with every path check passing, and a **symlinked directory component** walks a candidate into an
  **excluded** location while its spelling looks innocent), and why the exclusion filter is the skill's, not
  the user's.
- **Default deny, everywhere.** Keyed on the **tool's IDENTITY and its CITED documented guarantee**, NEVER on a
  failure "looking mechanical" or on the category "formatter". Unknown check, unlisted tool, an unresolvable
  binary, or a refused id → session model (an **unset** `formatters` header is not one of these: it means
  the table's defaults). The cheap path is keyed to the table, never inferred.
  ACCEPT the tool's run **only if both hold**: (a) re-running the **exact** failing check now **passes**;
  AND (b) the diff touches **no check definition, config, or test**. Either fails → **discard the work**
  (reset the worktree to the PR head) and **re-dispatch the same failure on the session model**. NEVER patch
  a formatter run in place; NEVER commit an unverified one; NEVER hand the failure to a cheap model instead.
- **A whitelisted TOOL's commit resets the gate exactly like a subagent's** (`stage-2-ci.md`, "Any campaign
  commit to the PR head resets the gate"). Every commit campaign pushes to a PR head — CI-fix subagent,
  review-fix subagent, or tool run with no model — MUST in the same step reset `reviews_ok` to 0 AND
  restore `gauntlet-reviewing` if the PR carries `gauntlet-accepted`, then relaunch the CI watch and
  re-enter Stage 2a. NEVER treat a tool-written commit as exempt.
- **Everything the tool does not fix → the scoped CI-fix subagent on the session model**, set explicitly:
  tool left residue, check not whitelisted, or the failure needs any judgment.
- A CI-fix subagent MUST be dispatched under the no-weakening prohibition (`stage-2-ci.md`): fix the
  cause, NEVER gut an assertion, add `skip`/`xfail`, disable a lint rule, or raise a timeout to force
  green. **The prohibition is load-bearing THERE** — a session-model fixer *can* change semantics, so it
  *can* weaken a check. **The TOOL path does not need it: the tool cannot weaken what it cannot change.**
- Scope every fix subagent to its worktree + concrete issue list; tell it NOT to re-derive the whole diff.
  That — with the tool-only formatter path and an external reviewer taking review passes off the subagent
  pool — is where savings live. Never a downgraded model.
- Default reviewer is Claude's own subagents; no external tool is required. Use the user's preferred
  reviewer when one is set (explicit invocation, or a preference in memory/`CLAUDE.md`/carryover). A
  different-model reviewer (e.g. Codex) is recommended for diversity but never required. See
  "The reviewer".
- If an *external* reviewer can't deliver a verdict (quota/rate-limit, auth, timeout, or other system
  error — *not* a real finding list / `VERDICT:` line), retry once, then do the equivalent work with
  your own subagents: a fresh, context-isolated subagent review pass in
  Stage 2a. The gate is unchanged — note any fallback pass in the report. See "The reviewer".
- CI status comes from a re-polled `gh pr checks` snapshot with **zero fail AND zero pending lines** —
  never from the `--watch` exit code (it can exit 0 on pending/unregistered checks). No green, no merge.
- The run targets a **base branch** (`base_branch` in the ledger header), which is **not assumed to
  be `main`** — it is the `baseRefName` of the adopted PRs (must agree across them, else prompt).
  Reviews diff `origin/<base>...HEAD` and PRs merge into `<base>`; a fix worktree branches off the PR's OWN
  head branch/SHA, never off `<base>` (see `pr-adoption.md`). Re-read it each wake (see "Base branch").
- After every merge, fast-forward local `<base>` to `origin/<base>` (Stage 3 step 4) so subsequent
  `origin/<base>...HEAD` diffs and rebases branch off the just-merged tip, not a stale base. If the
  fast-forward fails, bail out — never force it.
- No "Test plan" section in PR bodies.
