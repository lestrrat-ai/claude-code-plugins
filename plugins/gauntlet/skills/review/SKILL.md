---
name: review
description: Reports findings; by default makes no changes. A two-pass adversarial code review focused on security, API consistency/symmetry, and user experience. Pass 1 is hostile (assume the worst, surface everything). Pass 2 is a neutral audit that confirms, adjusts, or refutes each finding. Use when the user asks for a hostile, skeptical, or hard review — not a friendly pass. After the report there is an opt-in handoff to gauntlet:campaign — it can open one PR per confirmed fix (implementing the fixes) and hand them to campaign to gate and merge; decline and it stays report-only, making no source/tracked-file or GitHub changes (it may write ephemeral `.gauntlet/tmp` review scratch).
---

# Review

Two phases, in order. The hostile pass finds; the neutral pass filters. Skipping phase 2 means delivering noise.

## Phase 1 — Hostile Pass

Treat the code as a suspect, not a colleague. Goal: surface everything that *could* be wrong.

## Mindset

- Assume every input is malicious, every caller is confused, every assumption is wrong.
- "It works on happy path" is not evidence. Adversary picks the path.
- Author's intent does NOT excuse behavior. Only the code's actual behavior matters.
- No diplomatic softening. State the defect, the impact, the trigger.

## Scope

Resolve target before reviewing:

- PR / branch → `git diff <base>...HEAD` (and read full files for changed regions, not just hunks)
- Package / dir → enumerate files, read each
- Single file → read whole file

Always read enough surrounding context to understand callers and invariants. Hunks alone hide bugs.

## Sizing — pick a strategy

Measure surface BEFORE reading code:

- PR/branch: `git diff --stat <base>...HEAD` (changed-file count, lines added+removed)
- Dir/package: enumerate files with Glob (count = file count), then `wc -l <files>` for LOC.

Before starting, `mkdir -p .gauntlet/tmp` and delete stale `.gauntlet/tmp/review-*` files from previous runs. `.gauntlet/` should be git-ignored; if it is **not**, **warn the user** rather than editing `.gitignore` — report-only review makes no tracked-file changes, so it never edits `.gitignore` itself (the opt-in handoff / campaign path may add it). Never `rm -rf .gauntlet/` — a campaign's carryover history lives under `.gauntlet/history/`. Intermediate file paths are fixed — concurrent reviews in the same checkout collide; run one at a time.

Pick by the larger of file count or LOC:

| Surface | Strategy |
|---------|----------|
| ≤ 5 files AND ≤ 1000 LOC | **Single-pass**: review all three lenses yourself |
| 6–30 files OR 1000–5000 LOC | **Parallel by lens**: spawn 3 subagents (security, api, ux), each covers the full target |
| 30–200 files, single package or small cluster | **Parallel by lens × area**: subdivide each lens across bounded areas (one subagent per lens × area) |
| > 200 files, whole-repo audit, or sprawling multi-package | **Triage-then-depth** (see "Whole-codebase mode" below) |

A whole-codebase adversarial review IS in scope. Do not narrow the target, sample a "representative subset", or push back on size. Pick the strategy and dispatch. Coverage gaps go in the "Unreviewed" section, not in a refusal.

In parallel mode, you are a **dispatcher**, not a reviewer. Spawn subagents in a single parallel Agent-tool block, wait for all to finish, then merge their outputs. Do NOT also review the code yourself — that produces duplicate findings and burns the main context. Your only Phase 1 reading is whatever sizing requires.

**Bounded parallelism**: dispatch at most ~8 subagents per Agent-tool block. For larger fan-outs, run waves of 8, merging output between waves. This keeps each subagent's findings reviewable and avoids overwhelming the runtime.

## Whole-codebase mode

Triggered by the > 200 files / whole-repo row. The codebase is too big to deep-audit every file with equal rigor in one pass; coverage is by tier, not uniform.

### Step 1 — Map risk surfaces

Spawn ONE `Explore` subagent to enumerate the codebase and rank files into tiers:

- **T1 (hot)**: untrusted-input entry points (HTTP/RPC handlers, message consumers, file parsers), auth + session code, crypto, deserialization, anything touching secrets/credentials, `unsafe` blocks, FFI/CGO, raw SQL, shell exec, template rendering, IPC.
- **T2 (warm)**: business logic the T1 surfaces call into, persistence layer, internal RPC, background jobs, anything mutating shared state.
- **T3 (cold)**: pure utilities, type defs, generated code, tests, docs, vendored deps.

Output: `.gauntlet/tmp/review-tiers.md` with files grouped by tier, plus a short rationale per tier. Subagent prompt should explicitly say "do NOT review code — only classify by risk surface".

### Step 2 — Depth-audit T1

Run the full **parallel-by-lens × area** flow on T1 files only. All three lenses, full rigor, no severity floor.

### Step 3 — Breadth-scan T2

Spawn one subagent per lens with a tightened prompt:

```
This is a BREADTH SCAN, not a deep audit. Cover the assigned files for
the top defect classes of your lens only — do not exhaustively enumerate.

Lens: <LENS>
Severity floor: MEDIUM (drop LOW findings; raise borderline cases instead
of recording nits).

Top defect classes to look for:
  security → injection, missing auth, secret leaks, broken crypto
  api     → broken symmetric pairs, inconsistent error/return shapes,
            mixed naming for the same concept
  ux      → footguns, silently-wrong defaults, missing required-init traps

Otherwise follow Phase 1 rules from the SKILL.
```

### Step 4 — Spot-check T3

ONE subagent does a rapid pass for CRITICAL-only findings across T3: hardcoded secrets, obvious injection, world-writable artifacts, panics on user input, committed `.env`-style files. Anything below CRITICAL is dropped.

### Step 5 — Merge and Phase 2

Merge all tier outputs into the standard `.gauntlet/tmp/review-findings.md`, tagging each finding with its tier (`[T1]`, `[T2]`, `[T3]`). Run **sharded Phase 2** as defined below. Sort chunks so T1 findings get audited first — that's where confirmation matters most.

The final report's "Unreviewed" section must explicitly state what tiering chose to scan-only or skip, so the user can escalate any tier on a follow-up run.

## Parallel Phase 1 — dispatcher protocol

For each lens, spawn one subagent (`subagent_type: Explore` — read-only is sufficient for Phase 1). Issue ALL subagent calls in a **single parallel Agent-tool block**.

`<SKILL-PATH>` below is this file's own absolute path — `${CLAUDE_PLUGIN_ROOT}/skills/review/SKILL.md` when installed as part of the `gauntlet` plugin. Resolve it to a concrete absolute path before you substitute it into the prompt: the subagent inherits no plugin-root variable and cannot expand it.

Subagent prompt template:

```
You are running Phase 1 of an adversarial code review. Lens: <LENS>.

Read <SKILL-PATH> and apply
ONLY the "<LENS>" subsection of "Lenses". Do not review the other lenses.

Repository root: <absolute path>
Review target: <git ref / files / dir as given by user>
Files in scope: <explicit list, or the command that produces it>
Area assignment (sharded mode only): <subset of files this subagent owns>

Read each in-scope file fully. Read enough surrounding code (callers,
init, validators, middleware, type defs) to make accurate claims about
reachability and invariants.

Output: write findings to .gauntlet/tmp/review-findings-<lens>[-<area>].md
using the format, severity scale, and ID prefixes (SEC-/API-/UX-) defined
in the SKILL. Number sequentially within your lens (and area, if sharded).

End with an "Unreviewed" section listing what you did NOT examine and why.

Do NOT edit code. Do NOT verify findings — that is Phase 2's job. Bias
toward overclaiming.
```

After all subagents return, the dispatcher:

1. Concatenates lens files into `.gauntlet/tmp/review-findings.md`.
2. Dedupes: same `file:line` + same root cause → keep the more specific finding, drop the duplicate. Different defects at the same line stay separate.
3. Renumbers if necessary so IDs remain unique within each lens.
4. Merges all "Unreviewed" lists.

If a subagent returns a thin or empty report for a lens that obviously has surface (e.g., security lens on a package with HTTP handlers), re-dispatch it with a sharper file list — do not paper over with your own findings.

## Lenses

Apply each lens explicitly. Do NOT collapse them — each finds different defects.

### 1. Security

Check, in order:

- **Input trust**: Any value from user / network / file / env treated as safe? Injection (SQL, shell, template, header, log, path traversal, SSRF, XXE, deserialization).
- **Auth**: Authentication present where required? Authorization checked AFTER authentication? IDOR (object refs without ownership check)? Privilege escalation paths?
- **Secrets**: Hardcoded keys, tokens, passwords? Secrets logged, returned in errors, or sent to telemetry? `.env` / config files committed?
- **Crypto**: Custom crypto? Weak algos (MD5, SHA1 for security, DES, ECB)? Hardcoded IVs / nonces? `math/rand` where `crypto/rand` required? Cert verification disabled?
- **Concurrency**: TOCTOU between check and use? Shared mutable state without sync? Map writes without lock? Goroutine leaks?
- **Resource limits**: Unbounded loops, allocations, recursion, request body reads, regex on user input (ReDoS)? Missing timeouts on network/IO?
- **Error info leakage**: Stack traces / internal paths / DB errors returned to clients?
- **Dependencies**: New deps — known CVEs? Maintained? Pinned? Typosquat risk?

### 2. API Consistency + Symmetry

Check across the package / module, not just the changed function:

- **Naming**: Same concept named the same way everywhere? `Get`/`Fetch`/`Load` mixed for same operation? Plural vs. singular for collections?
- **Parameter order**: `ctx` first? Receiver/subject before modifier? Same order across sibling funcs?
- **Symmetric pairs**: `Open`/`Close`, `Lock`/`Unlock`, `Begin`/`Commit`+`Rollback`, `Subscribe`/`Unsubscribe`, `Add`/`Remove`, `Create`/`Delete`, `Encode`/`Decode`, `Marshal`/`Unmarshal` — both halves present? Identical signatures where applicable?
- **Return types**: Error position consistent (last)? `(T, bool)` vs. `(T, error)` mixed for similar lookups? Nil vs. zero-value vs. error conventions consistent?
- **Optionality**: Options pattern used the same way? Required vs. optional split consistent? Defaults documented and applied uniformly?
- **Idempotency**: Operations that should be idempotent actually are? Retries safe?
- **Error types**: Sentinel errors vs. typed errors vs. wrapped — pattern consistent? Caller can distinguish cases it needs to?
- **Visibility**: Exported surface minimal? Internals leaked through return types (e.g., returning concrete `*Foo` when interface suffices, or vice versa)?

### 3. User Experience

"User" = downstream caller, integrator, operator, end user — whichever applies. Check each that applies:

- **Footguns**: Easy to misuse correctly? Zero value usable or trap? Required init step that compiles fine if skipped?
- **Errors**: Message tells caller what went wrong AND what to do? Or just "invalid input"? Wrapped errors preserve cause? Caller can programmatically handle (typed/sentinel) AND human can read (message)?
- **Surprises**: Function name implies one thing, does another? Side effects undocumented? Mutates input? Blocks unexpectedly? Allocates unexpectedly?
- **Defaults**: Default behavior safe? Or does silent fallback hide bugs?
- **Discoverability**: Required option / required call order obvious from signature? Or only from reading source?
- **Docs vs. behavior**: Doc comment matches actual behavior? Examples compile and run? Edge cases (nil, empty, zero, negative) documented?
- **Observability**: Failures debuggable from logs/metrics alone, or does user need a debugger?
- **Migration**: Breaking change? Deprecation path? Old + new coexist?

## Phase 1 Output (intermediate, NOT the final report)

Write findings to `.gauntlet/tmp/review-findings.md`. One section per lens. Within each section, one finding per bullet:

```
[ID] [SEVERITY] <file>:<line> — <one-line defect>
  Trigger: <how to reproduce / when this fires>
  Impact: <what breaks, who notices>
  Fix: <concrete change, not "consider X">
```

`ID` = `SEC-1`, `API-1`, `UX-1`, ... — stable IDs so phase 2 can reference them.

Severity: `CRITICAL` (exploitable / data loss / silent corruption), `HIGH` (likely bug or hostile-input crash), `MEDIUM` (inconsistency, footgun, poor error), `LOW` (nit with rationale).

Also list **unreviewed areas** explicitly. "I didn't look at X" is a finding, not a gap to hide.

## Phase 1 Rules

- NEVER soften findings. "Might be worth considering" is banned. State the defect.
- NEVER report a finding without a concrete reproduction trigger or code citation.
- NEVER invent issues to fill a lens. Empty lens → say "no findings" and move on.
- ALWAYS cite `file:line`. No floating claims.
- Do NOT edit code. Review only.

## Phase 2 — Neutral Audit

Verify every finding from phase 1. Default: spawn a subagent so the audit runs in a fresh context with no commitment to the hostile framing.

### Preferred — Subagent (fresh context)

Use the Agent tool with `subagent_type: Explore` (read-only is sufficient; use `general-purpose` only if the audit needs to run scripts).

**Single audit** (≤ 10 findings): one subagent verifies all findings.

**Sharded audit** (> 10 findings): split findings into chunks of 5–8 by ID. Keep related findings together where possible (e.g., findings citing the same file or chain of callers in one chunk — co-located findings share read context). Spawn one audit subagent per chunk in a **single parallel Agent-tool block**, each restricted to its assigned IDs. Each writes to `.gauntlet/tmp/review-verdicts-<chunk>.md`. Dispatcher concatenates into `.gauntlet/tmp/review-verdicts.md`.

Subagent prompt template:

```
You are auditing a code review for accuracy. You are NOT the original
reviewer. Your job is to either confirm or refute each finding by reading
the cited code and its surrounding context.

Findings file: .gauntlet/tmp/review-findings.md
Assigned IDs (audit ONLY these; ignore others): <e.g., SEC-1, SEC-2, API-3>
Repository root: <absolute path>
Review target: <branch/PR/dir as given by user>

For each assigned finding, read the cited file:line AND enough surrounding
code (callers, validators, auth middleware, init code) to judge whether
the trigger is actually reachable in this codebase.

Ground external-API claims. When a finding's reasoning depends on how a
stdlib / framework / third-party function behaves (e.g. "tls.Conn.SetDeadline
doesn't propagate to the underlying conn", "json.Decoder accepts trailing
data", "context.Background().Deadline() panics"), DO NOT accept that
claim at face value from Phase 1. Open the dependency source or
authoritative doc and verify the actual behavior. Cite the source you
read (e.g. `$GOROOT/src/crypto/tls/conn.go:<line>`, official doc URL,
package's own godoc) in the verdict. A claim about external behavior you
cannot ground is grounds for UNCERTAIN, not CONFIRMED. The hostile pass
overclaims about external semantics as often as about local ones —
verifying these is Phase 2's job.

For each finding, emit one verdict:

- CONFIRMED — defect real, trigger reachable, severity correct.
- ADJUSTED — defect real but severity wrong / fix wrong / trigger
  narrower than claimed. State the correction.
- REFUTED — finding is wrong. State which guarantee / validator /
  invariant invalidates it, with file:line.
- UNCERTAIN — cannot determine from code alone. State what info is
  needed.

Verdict honesty:

- "CONFIRMED with caveat" is not a verdict. If you can articulate the
  caveat, the verdict is either ADJUSTED (defect narrower / less severe
  than claimed) or REFUTED (caveat invalidates the claim). Forcing
  yourself to pick one of the four labels exposes hedges as the audit
  dodging its job.
- "Pre-refactor parity" / "behavior matches prior code" is NOT a verdict
  modifier on its own. Resolve it: either the prior code was correct
  (→ REFUTED for the new code, since both are correct) or the prior
  code was also buggy (→ CONFIRMED as a pre-existing defect, with
  trigger and impact stated independently of when it was introduced).
  "Same as before, so confirmed but not a regression" is hedging.
- "Sloppy but not exploitable", "design issue exists in both versions",
  "technically a leak but unreachable in practice" — same pattern. Pick
  REFUTED, ADJUSTED, or CONFIRMED with a specific trigger.

Bias toward REFUTED and ADJUSTED. The hostile pass overclaims by design.
Confirming a finding requires evidence it is reachable, not absence of
evidence it isn't.

Output: write verdicts to .gauntlet/tmp/review-verdicts[-<chunk>].md,
one block per finding ID. Do not rewrite the findings — only judge them.
```

When subagents return, read findings + verdicts and merge.

### Fallback — Same-context audit

If subagent unavailable or user declines, do the audit yourself, but explicitly switch stance:

1. Re-read each cited location with intent to *disprove* the finding.
2. For each finding, search for: input validators upstream, auth checks, type-system guarantees, framework defaults, tests that already cover the case.
3. For findings whose reasoning turns on the behavior of stdlib / framework / third-party APIs, read the dependency source (or authoritative doc) — do not accept the Phase 1 claim about external behavior on trust. Cite what you read.
4. Apply the same CONFIRMED / ADJUSTED / REFUTED / UNCERTAIN verdicts. Reject "CONFIRMED with caveat" and "pre-refactor parity" as verdicts — resolve to one of the four labels with the reasoning explicit.
5. If you cannot find evidence to refute, that is NOT a confirmation — mark UNCERTAIN.

Same-context bias is real. Default to harsher self-criticism than feels natural.

## Dispatcher spot-check before final report

Phase 2 subagents can miss things too. Before merging verdicts into the final report, spot-check every CONFIRMED and ADJUSTED verdict for the two failure modes most likely to produce false confirmations:

1. **Unverified external-API claim.** Does the verdict's reasoning rest on a claim about how a stdlib / framework / third-party function behaves? If so, did the audit cite the source it read to ground that claim? If not, the audit may have accepted Phase 1's claim on trust. Demote to UNCERTAIN and either re-dispatch the audit with an explicit "read the dependency source" instruction, or verify the claim yourself before promoting it.
2. **Hedged confirmation.** Phrases like "CONFIRMED with caveat", "matches prior code", "this is sloppy but not exploitable", "the defect exists but" — every one is a sign the auditor couldn't fully commit. Re-read the verdict; if the hedge resolves to "the code is actually fine," demote to REFUTED with the reason. If it resolves to "the code is buggy in a way the audit struggled to articulate," dig in until you can articulate it.

A 30-second pass over the 8–10 confirmed findings is cheaper than shipping a final report that misleads the user about which fixes matter. Past failure: a Phase 2 audit confirmed a TLS handshake-deadline finding by repeating Phase 1's (incorrect) claim that `*tls.Conn.SetDeadline` doesn't delegate to the underlying conn. The grounding step — reading `crypto/tls/conn.go` — would have refuted both in one minute.

## Reflexive self-check before recommending fixes

The dispatcher spot-check above catches hedges in audit verdicts. But the dispatcher's own final report — especially the "Top N fixes" recommendation and any prose summarizing confirmed findings — is a NEW piece of writing that can introduce its own hedges and ungrounded claims. **Apply the same rules to your own writing.** A spot-check that doesn't apply to the spot-checker has a blind spot exactly where it matters most.

Before producing the final report, walk every item in your "Top N" recommendation through these checks:

1. **Hedge ban on your own prose.** Re-read each top-N entry with the question: "if the user implements this exactly, will it fix a real defect, or am I recommending a defensive cleanup whose justification reduces to 'a future caller might'?" If the latter, drop or downgrade. Watch especially for: "validation asymmetry between X and Y" (resolve to: which is correct?), "the in-tree codepath is safe today but..." (then it's REFUTED), "low-level primitive vs high-level wrapper" (if the wrapper is the user-facing API, the primitive is internal — REFUTED on convention grounds), "consistency improvement" (cleanups are not the same as fixes — separate them).
2. **External-API claims in the report must cite source.** Every "X function panics on Y" / "stdlib doesn't validate Z" / "Go arithmetic overflows here" — back it with a `$GOROOT/...` file:line or authoritative doc URL inline in the report. If you can't cite it in 30 seconds, demote to UNCERTAIN or omit. Do this BEFORE writing the recommendation, not after the user pushes back.
3. **Math claims in particular.** Overflow / truncation / underflow claims require an arithmetic sanity check. For Go: `uint32 * time.Second` cannot overflow `int64` (max value `4.29e9 * 1e9 = 4.29e18` ≪ `9.22e18`). Don't recommend a fix for an overflow that can't happen.
4. **Prefer narrow fixes over new API surface, especially on stable libraries.** Before recommending a new option, a new typed budget, a new error class, a new extension point — ask in order:
   - Is there an existing knob whose **default** is wrong? Tighten it.
   - Is there an existing knob whose **semantics** are wrong (e.g., per-iteration when it should be cumulative)? Fix the semantics.
   - Is the bug a missing **direct** validation (RFC-specified field size, minimum length)? Add it at the parse site, no option needed.
   - Does the caller already have a **first-class lever** for this (e.g., `context.WithTimeout` for CPU bounds, rate-limit at the edge, request-size cap at the proxy)? Document it; do not duplicate it in the library.

   Only after all four fail does a new option earn its place. Each new option on a stable library is a permanent tax on every reader of the API, and "5 knobs to defend against 1 attack class" almost always means the defect is narrower than the proposal. Watch for: "WithMax* / WithBudget* / WithLimit*" appearing more than once in the same top-N (smell of overengineering), "we need this for defense-in-depth" (collapses to #4), and "systematic" framings that fan out into N options when 1 default tweak would do.

This step takes ~60 seconds for an 8-item top-N. Skipping it means recommending fixes the user will reasonably reject after they read the code — wasting their time and your credibility.

Past failures:
- A whole-codebase review produced a Top-3 fixes list where the spot-check had already caught 9 hedge-confirmations in the audit output, but two of the three top-N items still relied on the same patterns (one with an unverified `time.Duration` overflow claim — `uint32 * time.Second` was assumed to overflow `int64` but cannot — and one with a "future external caller might forget" footgun). Both were dropped when the user re-applied the rules. The fix-rate of the recommended top-3 was 33% — and the user noticed before code was changed. Apply checks 1–3 before, not after.
- A different review correctly identified a defect class with several related instances (per-iteration limits that compound across an outer loop the attacker controls). The recommended fix was a "unified work budget" exposed as ~5 new typed options, one per instance. The user pushed back: every caller now has to learn N options and pick numbers they have no basis for, while a deadline/cancellation primitive the runtime already provides bounds the underlying resource; meanwhile the actual defects were narrower (one existing limit had per-iteration semantics where cumulative was needed; one default was set far higher than realistic usage; one missing validation was specified by the relevant standard and just wasn't implemented). The right fix was small and in-place. The wrong fix was an option fan-out that would have looked impressive in a changelog and degraded the API forever. Check 4 above exists because of this failure.

## Final Report

Merge phase 1 findings with phase 2 verdicts. Group by verdict, not by lens:

```
## Confirmed (N)
[SEC-1] [HIGH] path/file.go:42 — <defect>
  Trigger: ...
  Impact: ...
  Fix: ...
  Audit: confirmed — <one-line why audit agreed>

## Adjusted (N)
[API-2] [was MEDIUM, now LOW] path/file.go:88 — <defect>
  ...
  Audit: <what changed and why>

## Refuted (N)
[UX-3] path/file.go:120 — <original claim>
  Audit: refuted — <validator/guarantee that invalidates it, file:line>

## Uncertain (N)
[SEC-5] path/file.go:200 — <defect>
  Audit: cannot verify — <what info would resolve it>
```

End with:

- **Summary**: counts per verdict + per severity (confirmed + adjusted only).
- **Top 3**: highest-leverage confirmed fixes if author has limited time.
- **Unreviewed**: areas neither pass examined.

## Global Rules

- Phase 2 is mandatory. A single-pass hostile review is NOT this skill.
- Refuted findings stay in the report. The user benefits from seeing what was considered and ruled out.
- Do NOT edit code during the review itself. Review only; the user decides what to fix. The one
  exception is the opt-in campaign handoff below — and only after an explicit "yes".

## Handoff to campaign (opt-in)

Default is report-only. Everything above makes no source/tracked-file changes and no GitHub changes —
it opens no PRs and edits no committed files (it may write ephemeral `.gauntlet/tmp` review scratch).
This section is the ONLY path where this skill changes tracked state (writing code, opening PRs), and
it runs only on an explicit "yes".

After delivering the confirmed-findings report, offer exactly once:

> Open a PR per confirmed fix and run the gauntlet on them? [y/N]

- **No, or no answer (default)** → stop here. Report-only. Nothing is implemented, committed, pushed,
  labelled, or merged. This is where the skill ends unless the user opts in.
- **Yes** → hand the confirmed fixes to `gauntlet:campaign`. Fix-implementation lives here now
  (salvaged from the old campaign fan-out); the campaign itself never writes fixes from scratch.

On "yes", **first resolve the base branch** — for a PR/branch review target it's the PR's base /
the branch's upstream; for a package/dir/single-file target where no base was resolved, use the repo's
default branch or **ask the user** (never branch off an unresolved base). Then, for each **Confirmed**
or **Adjusted** finding only (skip Refuted and Uncertain), in its own branch off that resolved base:

1. Implement the fix — exactly the change in the finding's `Fix:`, nothing more; no drive-by edits.
   **"Nothing more" bounds the DEFECTS you fix, not the SITES one fix touches.** If the change alters a
   DEFINITION (a rule, a command, a schema, a format) or a FACT (a count, a name, an API behavior), then
   SWEEP the tree for every place that RESTATES it and correct each one **in this same PR**: a definition
   left with a stale restatement is a definition that lies, and that sweep is part of the fix, not a
   drive-by. Report every site you found and its disposition, including any you deliberately left alone.
   **Sweep the way the contract says** — `grep` alone finds only what a *changed* definition invalidated;
   it cannot find a stale restatement of a rule you just INTRODUCED. (This paragraph is a
   **non-authoritative summary** of the same contract campaign puts in every fix subagent's prompt —
   `${CLAUDE_PLUGIN_ROOT}/skills/campaign/references/fix-subagent-contract.md` is the complete DEFINITION
   and **wins over this** wherever they differ; read it, never reconstruct it from this summary.)
2. `git commit` the fix, then `git push` the branch.
3. `gh pr create` one PR for that finding. Title from the finding; body cites the finding ID,
   trigger, impact, and fix. Do **NOT** apply any `gauntlet-run-*` owner label — review does not know
   a campaign run-id, and pre-labelling with a fabricated run-id would make campaign's adoption refuse
   the PR (pr-adoption rejects a PR already owned by a *different* run). Leave each PR with **no
   run-owner label**; a neutral `gauntlet-reviewing` status label is fine but is not required. Create
   the status label with `gh label create` if you apply it and the repo lacks it.

One finding = one PR. Never batch multiple findings into a single PR.

Then invoke the campaign on exactly those PRs:

```
/gauntlet:campaign #<pr1> #<pr2> ...
```

Campaign mints its own run-id, adopts the PRs — applying its `gauntlet-run-<run-id>` owner label to
each during adoption (a fresh handoff PR carries no owner label, so it adopts cleanly) — gates each
through its tier's reviews plus CI, and merges. It only gates and merges what this handoff opened, and
never re-writes these fixes. Agent-facing changes (a `SKILL.md`,
`CLAUDE.md`, prompt, or reference file) always draw the full two-pass gate there, same as source.

Handoff rules:

- Ask once. Never open PRs without an explicit "yes".
- Only Confirmed and Adjusted findings become PRs.
- On decline, deliver the report as-is and stop — no side effects.
