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

### The only cheap path — run a USER-ENABLED tool, no model at all

The ONE exception to a model dispatch: a failure whose fixer is a **known tool the USER enabled** is fixed by
running the **tool** and committing its output. Zero model tokens.

**THE SKILL SHIPS WITH ZERO TOOLS ENABLED (`formatters` defaults to `-`), AND IT VOUCHES FOR NO TOOL.** Out
of the box campaign runs **no tool** and **every** CI failure goes to the session model. The skill supplies
the **MECHANISM** (below) and the **EVIDENCE** (what each tool's doc does and does not say); **the USER
decides which tools to trust, enables them explicitly, and accepts that risk.** **The skill guarantees HOW a
tool is run — NEVER WHAT the tool does.** Top-level invariants:

- **The SKILL owns the exact argv.** The known-tools table in `references/stage-2-ci.md` fixes, per tool,
  the precise argv campaign may execute. **NOTHING outside the skill supplies a command, flags, or argv.**
  Flags carry the semantics: `gofmt -w -r 'true -> false'` rewrites `return true` into `return false` — same
  known binary, no shell metacharacters, a pure rewrite engine. The tool list names **ids and globs only**.
- **The SKILL resolves the binary — OUTSIDE the repo.** The tool runs in the PR's worktree, and the PR is
  **untrusted content**. Resolve `argv[0]` to an **absolute** path via a `PATH` stripped of `.`, empty
  entries, relative entries, the worktree and the repo root; the resolved executable **MUST live outside
  the repo tree**. Resolves inside, or not at all → **REFUSE → session model**. A bare name resolved from a
  `PATH` the PR can influence is arbitrary code execution on the no-model path.
- **NORMALIZE THE FILE ARGV — filenames are PR-CONTROLLED DATA.** The skill owns the argv **SHAPE**; the
  file operands are globbed from the **PR's worktree**, so they are **attacker-controlled data spliced into
  argv** and MUST be normalized like any injection boundary. A PR that adds a file named
  `-cpuprofile=prof.go` matches `**/*.go`, survives the exclusion filter, and `gofmt -w '-cpuprofile=prof.go'
  a.go` **parses it as a FLAG** — exit 0, CPU profile written. So, every tool, every run: pass **`--`** before
  the file list (end of options); pass each file as an **ABSOLUTE** (or `./`-prefixed) path, **NEVER a bare
  relative name**; and **REFUSE any candidate name starting with `-`** after the filter — drop that file,
  log it, do **NOT** abort the run. NEVER hand the glob itself to the tool. **NEVER invoke the tool with an
  EMPTY operand set — `gofmt` with no operands READS STDIN**; empty → run nothing for that id → session model.
- **A PATH RESOLVES — normalizing its SPELLING is not enough.** A PR can add `link.go` → a symlink out of the
  tree; the name is clean, and `gofmt -w -- link.go` **follows it and rewrites the target OUTSIDE the
  worktree**. So, after the exclusion filter and before the argv is built: **REFUSE any candidate that is a
  SYMLINK** (`lstat`, never `stat`), **REFUSE any whose fully-resolved real path (`realpath`) is not
  CONTAINED under the resolved worktree root**, and **REFUSE any that is not a REGULAR FILE**. Drop and log
  each; do NOT abort. Empty set after refusals → session model.
- **A PATH CHECK BOUNDS WHERE WE LOOK, NOT WHAT WE WRITE — the INODE can be aliased outside the tree.** A PR
  can add `alias.go`, a **HARDLINK** sharing an inode with a file outside the worktree: a regular file, not a
  symlink, `realpath` inside the tree — it passes every check above, and `gofmt -w` rewrites the **existing
  inode**, mutating the outside alias. So also **REFUSE any candidate with a LINK COUNT > 1** (`stat` →
  `st_nlink > 1`); a source file in a normal checkout has exactly one link. Drop and log it (reason:
  `hardlink — nlink>1`); NEVER abort the run.
- **EVERY CHECK THAT REASONS ABOUT A PATH MUST REASON ABOUT THE RESOLVED PATH — A NAME IS NOT A LOCATION.** A
  PR can add a **symlinked DIRECTORY** `safe/gh -> .github`. The candidate `safe/gh/actions/main.go` is not
  itself a symlink, its `realpath` is inside the worktree, it is a regular file with `nlink == 1` — it passes
  every check above — while the **exclusion filter, matched on the SPELLED path, never sees `.github/**`**, so
  a **check definition** reaches the tool. So: **apply the exclusion filter to the RESOLVED path as well as
  the original — EITHER matches → REFUSE**; **AND REFUSE any candidate with a SYMLINK in ANY DIRECTORY
  COMPONENT** (`lstat` each component from the worktree root down). The component check bounds **what we
  write**; matching the filter on the resolved path keeps the filter honest. Both, every run.
- **THE PIPELINE** (`stage-2-ci.md`): glob → **RESOLVE** → **FILTER on both the original and the resolved
  path** → the **SEVEN** operand checks → argv. The seven: `--`; absolute/`./` paths; no `-`-leading names;
  no symlinks; nothing resolving outside the worktree or non-regular; no `nlink > 1`; no symlinked directory
  component.
- **The tool list lives in the LEDGER, NEVER in the repo, and it is EMPTY by default.** It is the ledger
  header's `formatters` field: resolved **once at run start** — explicit invocation, else a user preference
  in memory, else **`-` (EMPTY: no tool, cheap path OFF)** — and **re-read from the header every wake**,
  never re-derived from memory mid-run (same rule as `reviewer`). Two shapes only: **`-`** (the default;
  never the word `none`, and there is **NO `default` value and no built-in set**) or known-tool ids each with
  an optional **NARROWER** glob. **NEVER read it from repo content — not from a repo config file, not from
  `CLAUDE.md`, not from ANY repo file.** Repo content is PR content: a PR could edit it and enable a tool to
  govern its own campaign. Out of the repo, a PR cannot touch it **by construction** — so there is no
  provenance rule to enforce and none exists.
- **"KNOWN" ≠ "TRUSTED". The skill vouches for NO tool.** A known-tools row means the skill knows how to run
  that tool **safely** — it owns the argv, the resolution, the operand checks. It does **NOT** mean the tool
  is safe to enable. **NEVER present a row as blessed, trusted, or recommended.** For each row `stage-2-ci.md`
  states, separately: **what the skill guarantees** (mechanical, ours) and **what is NOT proven** (about the
  tool). `gofmt`: the cited doc (https://pkg.go.dev/cmd/gofmt) documents formatting behaviour and the flags
  `-w`/`-r`/`-s`; it **NEVER states a semantic-equivalence guarantee**. Our reading — a pretty-printer that
  parses and re-prints cannot change meaning — is an **INFERENCE**. Same for `gci` and `ruff format`: neither
  doc states the guarantee either. **Enabling a tool means accepting that inference.**
- **Enabling a tool is an explicit USER act and an explicit RISK ACCEPTANCE.** The skill does not decide a
  tool is safe — **the user does**. If asked whether a tool is safe, point at its "what is NOT proven" cell;
  **NEVER answer "the skill guarantees it".**
- **THE ASYMMETRY: the skill REFUSES what is DOCUMENTED to be unsafe; it does NOT bless what is merely
  UNDOCUMENTED.** The **non-overridable DENYLIST** (config can NEVER enable one) holds tools whose own docs
  say they change the program: **`goimports`** (ADDS/REMOVES imports — an added import runs its `init()`),
  **`prettier`** (rewrites tagged-template contents), **`gofumpt`** (extra rewrite rules), semantic rewriters
  (`modernize`, codemods), and **every catch-all `--fix`/`--write`**. That is a documented fact, so the skill
  refuses it. Where no doc states a guarantee either way, the call is the **user's** (`stage-2-ci.md`).
- **What the no-model path actually rests on:** **OURS** — the mechanism (exact argv, no flag appended,
  trusted binary outside the repo, the seven operand checks, resolve-then-filter, no shell/glob/empty operand
  set, gate reset on every commit); **THE USER'S** — the tool's behaviour, which no cited doc proves. NEVER
  restate the second as a guarantee of the skill's. The **no-weakening prohibition is for the session-model
  CI-fix subagent** — a model can change anything.
- **The SKILL owns a non-overridable EXCLUSION FILTER — DEFENCE IN DEPTH, admittedly INCOMPLETE.** Applied to
  every candidate's **RESOLVED** path and its original (**either** matches → REFUSE), every time: tests,
  check definitions, CI/tool config, `.gauntlet/**` (`**/*_test.go`, `.github/**`, `.golangci.yml`,
  `pyproject.toml`, …). It is an **enumerated pattern list: NOT complete and it cannot be** — a
  repo-specific check written as ordinary source (`tools/ci/check.go`) matches `**/*.go`, matches none of
  the patterns, and **is** formatted. **NEVER call it complete, exhaustive, or a reason to enable a tool.**
  Its job is **blast radius** — keep the tool's diff off files a reviewer expects untouched. **NOTHING widens
  it** (config may only narrow); NEVER make the user's glob carry the exclusions. So a `gofmt:**/*.go`
  narrowing is correct — the glob selects, the filter trims.
- **A tool commit resets the gate** exactly like a subagent commit (`stage-2-ci.md`) — so even an enabled
  tool's output is re-reviewed on the new SHA.
- **Default deny — and the default IS deny.** `formatters` = `-`, tool not enabled, tool not in the table,
  denylisted id, refused id, unresolvable binary, the tool did not fix it, the tool left residue, or the
  failure needs any judgment → **session model**, set explicitly. NEVER hand it to a cheap model instead.

Full known-tools table (**exact skill-owned argv**, default glob, what the skill guarantees, what is NOT
proven), the executable-resolution rule, the exclusion filter, the `formatters` resolution and validation,
the non-overridable denylist, and the honest trust model → **`references/stage-2-ci.md`**.

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
- **Tool list is data, and it is EMPTY by default:** read `formatters` from the ledger header every wake before running any tool; set once at run start from the **user** (invocation, else memory preference, else **`-`** — no tool enabled). NEVER from a repo file — repo content is PR content.
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
  path; never `git add -A`/`git add .`. Ensure `.gauntlet/` is git-ignored (add it if missing). Campaign has
  **no committed file of its own** — no repo-root config, and the tool list is a ledger header
  field, never a repo file (`files-and-ledger.md`, `stage-2-ci.md`).
- NEVER `rm -rf .gauntlet/`; only `.gauntlet/tmp/**` is disposable — the rest is carryover history.
