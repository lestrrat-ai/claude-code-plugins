---
name: campaign
description: >-
  Gates PRs to merge. A self-looping review-to-merge campaign: existing PRs are adopted into a run (pass PR numbers, or discover this run's labelled PRs), each PR is triaged to a review tier, and a per-PR review gauntlet (tier-dependent fresh, context-isolated SATISFIED verdicts on the whole PR diff, reviewed one at a time) plus event-driven CI monitoring gate an auto-merge. Multiple isolated runs (each keyed by a run-id, with a lease so only one agent drives each) can run concurrently in one repo. Drives its own loop via ScheduleWakeup — invoke once, no /loop wrapper. Campaign never writes fixes from scratch; to find issues first use gauntlet:review. Args (distinct modes): #PR... | --new #PR... | --run <id> | no args
---

# Campaign

Self-looping, reactive PR-review-to-merge pipeline.

Claude Code is orchestrator + gatekeeper. The **adversarial reviewer** is a selectable role: by
default Claude's own subagents (no external tool required); use the user's preferred reviewer when one
is set (explicit invocation, or a preference in memory/`CLAUDE.md`/carryover). A reviewer running a
different agent/model than the orchestrator (e.g. Codex CLI) is recommended for stronger reviewer
diversity but never required — see `references/reviewer.md`. Reviews and CI watches run as background
tasks; gates and merges stay centralized. Campaign gates **existing** PRs; it never writes fixes from
scratch — to find issues first, use `gauntlet:review`, which after its report offers to open one PR per
confirmed fix and hand them straight here (`/gauntlet:campaign #PRs`). That opt-in handoff is a common
way PRs enter a campaign; you can also open them yourself.

Invoke once. This skill drives its own loop via `ScheduleWakeup`; do NOT wrap it in `/loop`.

## Args

`/gauntlet:campaign  #PR... | --new #PR... | --run <id> | (no args)`

These are **distinct modes**, not freely-composable flags: `--new` requires a `#PR` set; `--run <id>`
resumes and takes no `#PR`/`--new`; `#PR` args alone start/adopt into a run. Invalid combinations
(e.g. `--new` with no PRs, or `--run` with `#PR`) are rejected / fall through to the idle prompt.

- `#12` / `#12 #15` -> adopt those existing PRs into a run; gate + merge them.
- No argument -> discover this run's labelled PRs and continue. If none and nothing to do, PROMPT:
  "No PRs under a campaign. Run gauntlet:review to find issues, or pass PR numbers to gate."
- Non-PR arg (e.g. `auth`) -> same prompt (the old area/topic sweep arg is REMOVED).
- `--run <id>` -> resume specific run; self-wakes also carry internal `--token`.
- `--new` or "fresh run" -> force independent new run-id for a new PR set (with carryover).

## Bundled Scripts

Bundled scripts live in `scripts/`, resolved relative to the directory holding this `SKILL.md` (not
the current working directory) — installed as `${CLAUDE_PLUGIN_ROOT}/skills/campaign/scripts/`, or in
the repo at `plugins/gauntlet/skills/campaign/scripts/`. Pass their absolute paths to subtasks. The
review gauntlet's `scripts/emit-progress.py` (already present there) emits canonical reviewer progress
events (see `references/stage-2-review-gate.md`). `scripts/ledger.py` is the schema-owning accessor for
`state.jsonl` — read/write the ledger header and per-PR rows **by field name** through it, never by column
position (see `references/files-and-ledger.md`); pass its absolute path to subtasks the same way.

## Subagent Dispatch — model per class

Campaign spawns subagents for several jobs. **Set the model explicitly on every dispatch.** With no
model set, a subagent inherits the session model (often the most expensive one) — so an unset model is
a silent cost decision, taken by default, on every subagent this skill launches.

| Subagent | Model | Why |
|---|---|---|
| Review pass (default reviewer) | **session model** | It *is* the gate. A weaker verdict is a worse gate — the one thing never worth cheapening. |
| Fresh-subagent fallback review | **session model** | Same job as a review pass; counts toward the gate identically. |
| Review-fix (after `NOT SATISFIED`) | **session model** | Authors code that gets merged, judged only by another full review pass. A cheap bad fix burns a whole review pass and a gate reset — it *costs* more than the tier saves. |
| **CI-fix** | **session model** | Also authors code that gets merged. CI does **not** validate it: a wrong fix can turn CI green — by weakening a check, or by being plain wrong in product code that no check covers. Dispatched under an explicit no-weakening prohibition (`stage-2-ci.md`), which constrains the fixer but proves nothing about the fix. |
| Root-cause **mapper** | **session model** | Read-only, but NOT low-judgment: it enumerates a full matrix and confirms each gap with a repro. A weaker model **under-maps**, which is the exact failure the mapper exists to prevent (`root-cause-pass.md`). "Read-only" is not a licence to downgrade. |

**NO SUBAGENT IS EVER RUN ON A DOWNGRADED MODEL**, and the reason is uniform. Every subagent here either
*is* the gate (review passes), writes **code that gets merged** (CI-fix, review-fix), or produces the
enumeration an expensive fix depends on (mapper). Nothing downstream *guarantees* a bad result is caught:
CI misses a wrong-but-green fix, and the review gate is a miss-catcher, not a proof of correctness
(`stage-2-review-gate.md`). And there is **no mechanical guard** that makes a weak model's edits safe to
accept: a diff that *looks* like formatting is **not** a proof of semantic equivalence (see below). NEVER
justify a downgrade by claiming something downstream will catch it.

### The only cheap path — run the formatter TOOL, no model at all

Some CI failures need **no subagent**: run the fixer **tool** and commit its output. Zero model tokens,
zero model risk.

**THE CRITERION — a tool is whitelisted ONLY IF it guarantees its output is SEMANTICALLY EQUIVALENT to
its input**: an AST-preserving pretty-printer, not a text munger. Whitelisting is **per-TOOL**, and the
burden is the **tool's documented behaviour**. **If you cannot point to that guarantee, the tool is NOT
whitelisted — use the session model.** There is NO blanket "formatters are safe" rule: a tool that looks
like a formatter can still rewrite meaning (see `prettier`, below).

The guarantee belongs to the whitelisted **tool's output** and to **nothing else**. It does **not**
transfer to a model that hand-edits the same file: a diff that merely LOOKS like formatting is not
semantics-preserving. In a whitespace-significant language it can move behavior while staying
formatter-clean — indenting `result.append("always")` under an `if` is a pure-indentation edit that turns
`["always"]` into `[]`, and `ruff format` is perfectly happy with it.

**The LIST is configurable. The CRITERION is NOT.** The whitelist = the skill's **built-in defaults**, as
the repo's `.gauntlet.yml` re-configures or removes them. A hardcoded set of *flags* is meaningless in a
Rust/Java/Ruby repo; the criterion above is the safety property and is **NEVER overridable by config**.
Config tunes **which known tools run, with which flags, over which files** — it can **NEVER introduce a
binary outside the known-tools table** below.

**Built-in defaults — the KNOWN-TOOLS TABLE.** These are the ONLY binaries campaign may execute on the
no-model path. Adding a tool here is a **SKILL change**, NEVER a config change (`stage-2-ci.md`).

| `id` | `argv[0]` | guarantee | precondition |
|---|---|---|---|
| `gofmt` | `gofmt` | AST-preserving Go pretty-printer; never touches string-literal contents | none |
| `gofumpt` | `gofumpt` | same, stricter layout; never touches string-literal contents | none |
| `goimports` | `goimports` | import block only | none |
| `gci` | `gci` | import grouping/ordering only — Go init order is by **dependency**, not by import order | none |
| `ruff format` | `ruff` | verifies its output is AST-equivalent to the input | repo's Ruff config does **NOT** enable `format.docstring-code-format` |

**golangci-lint `whitespace` is NOT a default**: it has **no safe fixer** — its only fix path is the
catch-all `golangci-lint run --fix`, which the denylist forbids. NEVER invent a command for it.

**A tool's guarantee can be CONDITIONAL on its CONFIGURATION.** `ruff format` with
`format.docstring-code-format` enabled reformats Python code **inside docstrings** — it rewrites string
contents, so its output is NOT AST-equivalent. Therefore: every `guarantee` MUST state the **conditions
under which it holds**, and campaign MUST **verify those conditions hold in this repo** (read the tool's
config from the **base** branch) before taking the no-model path. Condition violated, or undeterminable →
**NOT whitelisted for this repo → session model.**

**The DENYLIST — the skill's own, and config CANNOT widen past it. Refuse any entry that is:**

- **`prettier`**: it reformats the **contents** of tagged template literals (`` gql`…` ``, `` css`…` ``),
  changing the runtime string the tag function receives. That is a semantic change made by the tool itself.
- any **generic or unscoped** "whitespace" / "trailing-whitespace" fixer, which can rewrite content inside
  string literals, heredocs, or Markdown (e.g. trailing double-space hard breaks). A tool that cannot
  promise it leaves literal content alone is NOT whitelisted.
- every **semantic rewriter** — `modernize`, codemods, `pyupgrade`, `2to3`, any rule that rewrites logic. A
  `modernize` rewrite can PASS its own rule while CHANGING BEHAVIOR (e.g. `sort.Slice` → `slices.SortFunc`
  with a reversed or non-equivalent comparator): lint-clean, semantics changed.
- every **catch-all fixer** — `golangci-lint run --fix`, `ruff --fix`, `eslint --fix`, `cargo clippy --fix`,
  or any `--fix`/`--write` flag on a linter that applies semantic rules. A whitelisted run MUST invoke
  **only the whitelisted formatter**, NEVER a catch-all `--fix`.
- anything whose command can touch a **check definition, config, or test** (the no-weakening prohibition —
  a hard rule, not a preference).
- **NEVER whitelisted**: a failing product test (making a test pass is not the same as fixing the bug), a
  compile error, and any rule that rewrites logic.

**Repo config — `.gauntlet.yml` at the repo root** (COMMITTED, unlike the git-ignored `.gauntlet/` tree).
It **re-configures the flags and files of KNOWN tools** and **removes** built-ins; `formatters: []`
disables the cheap path entirely (everything goes to the session model — always a safe choice). It can
**NEVER introduce a binary that is not in the known-tools table.**

**The command's SHAPE is constrained, not just the tool's category** — the attack is command
*construction*: an entry can cite `gofmt`'s guarantee while executing `./scripts/fmt.sh`. So every entry
MUST carry `id`, `command`, `files`, `guarantee`, and:

- **`command` is an argv LIST** — `command: ["gofmt", "-w"]`. **Executed WITHOUT a shell.** NEVER `sh -c`,
  NEVER `bash -c`, NEVER a shell string.
- **REFUSE any shell metacharacter** in the argv — `&&`, `||`, `;`, `|`, `>`, `<`, `$(`, backticks,
  newlines. No legitimate formatter entry needs them.
- **`argv[0]` MUST be a bare tool name in the known-tools table** — NOT a path (`./x`, `/usr/local/bin/x`),
  NOT a wrapper script, NOT an alias, NOT `sh`/`env`/`xargs`.
- **`guarantee` MUST state the conditions under which it holds**, and those conditions MUST **verify in
  this repo**. Undeterminable → REFUSE. "It's just formatting" is NOT a guarantee.

**Any entry failing ANY of these is REFUSED** — logged, ignored, and that failure routes to the **session
model**. Full schema, validation, and merge order: `stage-2-ci.md`.

**NEVER read `.gauntlet.yml` from the PR's worktree or head — ALWAYS from the BASE branch**
(`git show origin/<base>:.gauntlet.yml`). If campaign read the whitelist from the PR under review, a PR
could **widen the whitelist that governs its own campaign** — adding a semantic rewriter and earning an
unreviewed tool commit on its own head. That is self-gating in config form. A PR that edits `.gauntlet.yml`
therefore takes effect only **after it merges**, gated like any other change.

**Trust model, stated honestly.** `.gauntlet.yml` comes from the base branch, so its author already has
**write access to the repo** — the same access that can rewrite `.github/workflows`. The denylist and the
command-shape rules are a **guard against footguns and accidental misuse, NOT a security boundary against a
malicious committer**. NEVER claim otherwise. What the base-branch rule DOES buy is real and MUST be kept:
**a PR under review cannot widen the whitelist that governs its own campaign.**

Eligibility is keyed on the **tool's IDENTITY and its documented guarantee** — NEVER on an impression that
a failure "looks mechanical", and NEVER on the *category* "formatter". A vibes-based whitelist is the same
unsound reasoning in a new hat. **Default deny, everywhere: unknown tool, unlisted tool, missing or
unparseable config, or a refused entry → session model.** The cheap path is opt-in per tool, never inferred.

| When | Action |
|---|---|
| **Whitelisted tool** (prefer always) | Run the **tool** in `<worktree>`, **WITHOUT a shell**, from its validated **argv** — the built-in default (`["gofmt","-w"]`, `["gofumpt","-w"]`, `["goimports","-w"]`, `["gci","write"]`, `["ruff","format"]`) or the validated `command` from base-branch `.gauntlet.yml`. NEVER a catch-all `--fix`. Re-run the **exact** failing check; it must pass and the diff must touch **no check definition, config, or test**. Then commit + push — and **reset the gate exactly as any other PR-content change does** (`stage-2-ci.md`). **No model at all.** |
| **Everything else** | Dispatch the scoped CI-fix subagent on the **session model**, set explicitly. Covers: the tool did not fix it, the tool left residue, the tool/check is not whitelisted, a config entry was **refused** (bad shape, unknown `argv[0]`, unverifiable guarantee condition), the config is missing/unparseable, or the failure needs any judgment (product tests, compile errors, semantic lint rules, logic). |

If the tool's output does not clear the failing check, or it touched a check definition, config, or test
→ **discard the work** (reset the worktree to the PR head) and **re-dispatch the same failure on the
session model**. NEVER commit an unverified formatter run. NEVER hand a "formatting" failure to a cheap
model instead.

**Residual risk, stated honestly:** the whitelist is only as strong as each tool's documented guarantee —
a tool bug, or a config/plugin that switches on non-formatting rules, is the whole of the exposure. Run
whitelisted tools with the project's own config and no extra rule sets. Do NOT admit a `.gauntlet.yml`
entry past a guarantee you can point to, and NEVER re-derive its safety from the review gate: the
whitelist stands on the TOOL being incapable of changing semantics, or it does not stand at all.

**The biggest lever is not the model — it is the reviewer.** Review passes re-read the whole PR diff,
`required(tier)` times per SHA, and re-run from scratch on every gate reset, so they dominate campaign's
subagent spend. Running an **external reviewer** (e.g. `codex exec`, see `references/reviewer.md`) moves
that cost off the subagent pool entirely — the quality argument (reviewer diversity) and the cost
argument point the same way.

**Scope every fix subagent.** Give it the worktree path and the specific issue list, and tell it **not**
to re-derive the whole diff or re-read the repo beyond the named files. An unscoped fixer re-reads
everything it was already told.

## Load Discipline

Read references on demand. Do NOT load every reference up front.

Always read before touching run state:

- `references/run-identity-and-lease.md`
- `references/files-and-ledger.md`

Read `references/loop-control.md` at each wake before dispatch.

Read stage refs only when that stage/action is due:

| Situation | Read |
|-----------|------|
| Public API change, run-owned operation scope | `references/scope-and-constraints.md` |
| Fresh run, carryover | `references/carryover.md` |
| Selecting the reviewer; external-reviewer failure/fallback | `references/reviewer.md` |
| Adopting PRs into a run (worktree/labels/ledger row) | `references/pr-adoption.md` |
| PR review gauntlet / progress ledger | `references/stage-2-review-gate.md` |
| Repeated sibling findings / shared root cause | `references/root-cause-pass.md` |
| CI watch, check polling, CI fix | `references/stage-2-ci.md` |
| Merge candidate / base refresh / cleanup | `references/stage-3-merge.md` |
| Stuck task, abort, final report | `references/bailout-and-final-report.md` |
| Rule lookup / uncertainty | `references/critical-rules.md` |

## Core Invariants

- **PR-gating:** adopt PR -> triage tier -> watch CI + review PR HEAD -> merge. PRs are **adopted, not
  generated** — campaign gates existing PRs and never writes fixes from scratch.
- **Work-conserving:** every wake reconciles, folds completions, launches all due work up to caps,
  drains still-ready PRs serially, then reschedules only when no useful action remains launchable.
- **Driver never blocks:** reviews and CI watches run as background tasks — completions are wakes.
  A pending-CI PR always has a live watch; a review doomed by a pending content change is stopped,
  not awaited.
- **Run isolation:** touch only this run's `<rundir>`, ledger, labels, branches, PRs, and worktrees.
- **One active driver:** lease controls ownership; never double-drive one run.
- **Base branch is data:** read `base_branch` from ledger every wake; never assume `main`.
- **Reviewer is data:** read `reviewer` from ledger every wake before dispatching any review; set once at run start, never re-derived from memory (else an explicit/preferred reviewer silently reverts to default on a self-wake or adoption).
- **Remote branch cleanup isn't campaign's job:** campaign never passes `--delete-branch`; the repo's *Automatically delete head branches* setting governs the remote head branch. Local worktree/branch cleanup follows the per-PR `worktree_owned`/`branch_owned` flags.
- **Review gate is tier-dependent:** `required(tier)` fresh, context-isolated `SATISFIED` verdicts on
  same live PR content + green CI — **1 if TRIVIAL, else 2** (any code/agent-doc/sensitive change is 2).
- **Sequential same-PR reviews:** launch review 2 only after review 1 is `SATISFIED` (TRIVIAL needs
  only one).
- **Progress ledger:** reviewer progress means planned unit `done` or accepted amendment, not vague
  output.
- **No green by watch exit:** derive CI from re-polled `gh pr checks` snapshot.
- **Public API changes require user confirmation** unless the ledger's `api_changes` field is `allowed`.

## Wake Skeleton

1. **Resolve run + lease;** adopt only absent/stale lease, stand down if fresh different owner.
2. **Adopt `#PR` args + reconcile run-labelled PRs.** For each explicit `#PR`, adopt it per
   `references/pr-adoption.md` (refresh existing row on re-adoption, never duplicate); then reconcile
   every PR carrying this run's `gauntlet-run-<run-id>` label from a batched snapshot. Treat `state.jsonl`
   as cache.
3. **Fold completed review / CI / fix tasks** against the SHA each ran on.
4. **Triage tier per PR, then launch due gate work up to caps.** Re-derive each PR's tier from its
   `head_sha` (deterministic file-class triage), then launch reviews up to `required(tier)`, CI
   watches/fixes, precondition clearing (Copilot items / red CI / base conflict), and base refresh;
   stop in-flight reviews doomed by a content change.
5. **Merge ready PRs** one at a time until no candidate remains immediately ready after base refresh.
6. **Launch audit + heartbeat — before sleeping, verify every due launch actually happened.** Re-run
   step 4's dispatch scan across both concurrency pools (CI-fix subagents and review passes each have
   their own cap): confirm every due review pass was launched, a CI watch is live for every pending-CI
   PR, and — whenever any non-terminal work remains — a `ScheduleWakeup` heartbeat is actually
   scheduled. If any due launch or the heartbeat is missing, launch it and re-audit. NEVER sleep with
   due work un-launched or the heartbeat unscheduled.
7. **Terminal -> carryover/report;** otherwise refresh lease and return (step 6 has already ensured the
   heartbeat is scheduled).

## Critical Rules

- NEVER review unpublished local work.
- NEVER spend review over open Copilot items, red checks, or conflicts.
- NEVER pass destructive instructions to an external reviewer command (e.g. `codex exec`).
- NEVER use `--dangerously-bypass-approvals-and-sandbox`; use `--sandbox workspace-write`.
- NEVER force-push/reset/delete outside explicit stage procedure and run scope.
- NEVER touch another run's PR/branch/worktree.
- NEVER merge over red/pending CI or stale review verdicts.
- NEVER add "Test plan" section to PR bodies.
- NEVER commit run/scratch files (the whole `.gauntlet/**` tree) — they are
  driver bookkeeping, not repo content. Stage only the specific source files a fix touches, by explicit
  path; never `git add -A`/`git add .`. Ensure `.gauntlet/` is git-ignored (add it if missing). The repo's
  `.gauntlet.yml` (repo root) is NOT part of that tree: it is committed repo config, read from the **base**
  branch (`files-and-ledger.md`, `stage-2-ci.md`).
- NEVER `rm -rf .gauntlet/`; only `.gauntlet/tmp/**` is disposable — the rest is carryover history.
