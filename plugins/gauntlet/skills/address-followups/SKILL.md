---
name: address-followups
description: >-
  Works Gauntlet's durable follow-up queue end to end instead of only listing it. Each open entry is
  resumed at the step its lifecycle state has already reached: a fresh read-only subagent must reproduce
  or refute the claim, only a corroborated entry meeting every autonomy-threshold condition is taken up,
  a separate scoped subagent then authors the fix and opens ONE PR for that one follow-up, and the PRs
  opened this invocation are handed to gauntlet:campaign to gate and merge. Use when the user asks to
  work, drive, clear, action, or process follow-ups. To only SEE the queue, use gauntlet:followups. Args:
  (no args) | --id fuN | --limit N
---

# Work Follow-ups

Invocation: Claude Code `/gauntlet:address-followups`; Codex `$gauntlet:address-followups`.

## This skill is the TRIGGER. It is not the rules.

`../campaign/references/followups.md` owns the follow-up lifecycle, the three-tier autonomy threshold,
the resume-by-state list, and every store transition. **Read it before touching an entry, and read it
from `<skill-dir>/../campaign/references/followups.md`, not from memory.** This skill is the **on-demand
entry point** to that loop: it works the queue when the user asks for it, rather than only when a
campaign heartbeat has spare capacity to spend on it. It changes **who initiates** the loop, never
**what the loop does**.

**NEVER reconstruct the threshold, the conditions, or the legal transitions from this file.** It owns
only what it says here: which entries get picked up, in what order, which worker runs each step, and
what happens to the PRs at the end. Everything else is a pointer, deliberately.

## Runtime adapter

- **`<checkout>` is an input this skill receives, never a constant it knows.** It is the checkout the
  user named with the invocation, and the repository the session is already running in when they named
  none — the same convention the sibling skills state (`../followups/SKILL.md` and `../ledger/SKILL.md`,
  step 1 of each). Neither host supplies it in an environment variable, so NEVER read one for it. Step 0
  resolves it to an absolute repository root **once**, and that one absolute result is what every later
  `<repo-root>`, every path derived from it, and every subagent prompt carries. No worker re-resolves it,
  and none is handed a relative path or left to infer one from its own working directory.
- Resolve `<skill-dir>` from the actual path of this active `SKILL.md`. NEVER depend on
  `CLAUDE_PLUGIN_ROOT`, `PLUGIN_ROOT`, the current working directory, or a repository-relative path.
- Campaign's bundled scripts are `<skill-dir>/../campaign/scripts/`; its references are
  `<skill-dir>/../campaign/references/`. Resolve both to absolute paths and pass those absolute paths
  to every subagent.
- Run every bundled script through its interpreter with that absolute path — `python3
  <abs-path>/followups.py …`. NEVER invoke one bare (`../campaign/SKILL.md`, "Bundled Scripts", owns
  the rule and why).
- "Subagent" means a fresh, context-isolated worker launched through the active host's agent mechanism.
  Describe each worker's scope, permissions, and output in its prompt; do not require a host-specific
  agent type or model name.
- Logical model classes map to the active host through `../campaign/references/runtime-adapter.md`,
  "Model classes".
- **If the host has no fresh-worker mechanism, STOP and report that.** Do not investigate inline and do
  not fix inline. The two separate subagents are the whole guarantee (`followups.md`, "The two subagents
  are the load-bearing part"); running either in the driver's context removes it.

## Args

Claude Code: `/gauntlet:address-followups [--id fuN] [--limit N]`

Codex: `$gauntlet:address-followups [--id fuN] [--limit N]`

- No argument works every resumable entry, in the order Step 1 fixes.
- `--id fuN` works exactly that entry and no other.
- `--limit N` stops after N entries have reached an actionable outcome.

These are the only **flags**. The checkout is an input rather than a flag, and the Runtime adapter's
`<checkout>` rule above owns where it comes from — this list is not the skill's full set of inputs.

## When a command refuses — one rule, for every step

Every command this file tells an agent to run — every `git`, every `gh`, and every `followups.py`
invocation, wherever it appears below and whether the driver or a subagent runs it — can refuse.
**Refusing** means a non-zero exit **or** output that cannot be used: empty, truncated, or not the shape
the step reads. This section is the **only** rule for all of them. No step below restates it, none
overrides it, and none is exempt.

On any refusal, always:

- **Relay the command's stderr as-is, and do not paraphrase it** — the same thing the sibling skills do
  (`../followups/SKILL.md` and `../ledger/SKILL.md`, final step of each).
- **Write nothing to the store and mutate no PR.** A command that refused reported nothing about the
  entry, so a transition read out of an error is **invented, never observed**. **NEVER infer a store
  move, a PR state, or a decision from a failure.** The branches next to these commands are destructive
  — Step 2's `merged` deletes the entry outright — and a guessed one destroys work nothing can rebuild.
- **Do not retry it silently.** Report the refusal and let the user re-invoke.

How far the refusal stops the invocation then depends on which command refused:

- **Step 0 or Step 1 refuses → stop the invocation** and report why. Nothing after them has a repository
  root, a store, or a work list, and working a list that lost entries to an error reads as "the queue is
  clear" when it is not.
- **Every other command refuses → leave the entry it belonged to unworked and move to the next entry.**
  That is the default and it needs no per-step list: the store still holds the state the entry already
  had, so Step 1's ordering re-picks it on a later invocation. Step 6 must list it as unworked and name
  the command that refused.
- **`open-pr` refuses after its PR is already open** → the PR exists and the store does not name it.
  Report that PR ref next to the entry id in Step 6, because the driver's memory of the pair dies with
  the driver's context. Do not close the PR and do not re-attempt the record here.

## Step 0 — resolve, then refuse early

1. Resolve the repository root to an absolute path from the `<checkout>` the Runtime adapter defines:
   `git -C <checkout> rev-parse --show-toplevel`. Carry that result as `<repo-root>`; nothing below
   re-runs it.
2. The store is `<repo-root>/.gauntlet/followups.jsonl`. It is **user-local and git-ignored**; it is
   never created here. If it does not exist, report that there is no follow-up store and **stop** — an
   empty queue and an absent store are different answers, and inventing the file hides which one it was.
3. Render the queue once, before any work, so the user sees what is about to be worked:
   `python3 <abs>/followups.py --file <abs-store> table`. Print its stdout as-is.

## Step 1 — build the work list from the STORE, never from memory

Ask the store which entries are where:

```text
python3 <abs>/followups.py --file <abs-store> list --where state=<state>
```

**Which states are resumable, and what each one resumes at, is owned by `followups.md`, "WORKING A
FOLLOW-UP".** `python3 <abs>/followups.py --help` prints each subcommand's exact from-set→to edge, which
is the authority on the legal store move. Read both; do not retype either here.

Work the resumable entries in this order, which is this file's own rule:

1. `in-pr` — a PR is already open for it and may be sitting **outside** the campaign gate. Finishing
   that costs one reconcile and closes the largest hole; starting new work while it is open widens it.
2. `accepted`, then `self-accepted`, then `reopened` — the decision is already made and only the PR is
   missing.
3. `corroborated` — investigated already, so it resumes at the take-up decision, **not** at another
   investigation.
4. `candidate` — the only state that starts with an investigation.

`refuted` is **not** picked up. It is re-investigated only when new evidence may overturn it, and an
invocation of this skill supplies none. `rejected` is the user's terminal ruling and is never resumed.

Apply `--id` and `--limit` **after** ordering, and **say what was left unworked and why**. A skill that
silently stops at a cap reads as "the queue is clear" when it is not.

## Step 2 — reconcile every `in-pr` entry against its live PR

An `in-pr` entry names the PR addressing it, and that PR may have moved since the entry was written.
`followups.md` requires the move to be recorded **in the turn that saw it**, because the driver's memory
of it dies with the driver's context. Read the PR once —
`gh pr view <ref> --json state,labels` — and act on what it says. The four cases below are the whole set
of things a **successful** read can say; anything else — a non-zero exit, empty output, output missing
either field — is a refusal and is handled by "When a command refuses", not by guessing which case it
was:

- **merged** → `python3 <abs>/followups.py --file <abs-store> merged --id fuN`. The entry is deleted and
  the command prints it in full; that print is the handoff, so put it in the report.
- **closed without merging** → `closed-unmerged --id fuN`. The entry returns to open work as `reopened`
  with its history intact, and it is then eligible for Step 5 in this same invocation.
- **open, carrying no `gauntlet-run-*` label** → no campaign owns it, so it is sitting outside the gate.
  Add it to Step 7's hand-off.
- **open, carrying a `gauntlet-run-*` label** → a run already owns it. **Touch nothing** — not the PR,
  not its labels, not the entry. Report it and move on. A run's PRs are its own
  (`../campaign/references/run-identity-and-lease.md`, "Isolation invariant").

## Step 3 — INVESTIGATE (Tier 1, read-only, free)

For every selected entry whose state resumes at an investigation, dispatch **one subagent per entry**,
in the model class `../campaign/SKILL.md`, "Worker Dispatch — logical model class", assigns the
**Follow-up investigator**. Read that class there; this file does not restate it.

Investigation is **read-only with respect to the repository** and the store's accessor locks its own
writes, so these may be dispatched as one parallel batch. Nothing later in this skill may be.

Each prompt carries, as data:

- the absolute repository root;
- the entry itself, from `python3 <abs>/followups.py --file <abs-store> get --id fuN`;
- the absolute `followups.py` path and the absolute `--file` store path;
- the absolute path of `../campaign/references/followups.md`, which the worker reads itself.

Each worker's job is to **reproduce the claim or show it cannot happen**, and to record the outcome with
its evidence through `corroborate` or `refute`. It changes no tracked file, opens nothing, and publishes
nothing.

**A refutation is the most valuable outcome, not a failure.** A worker that can only ever confirm is a
rubber stamp with a longer runtime, and this repository has already spent review rounds "fixing" a bug
that was never real (`AGENTS.md`/`CLAUDE.md`, "Your OWN diagnosis is a claim too").

**The driver NEVER investigates inline**, and never skips from a claim straight to a fix.

## Step 4 — the ACT decision (Tier 2)

**Only a `corroborated` entry reaches this step.** The other states arriving at Step 5 already carry a
decision, and re-deciding one would overwrite a ruling that was already made.

`followups.md`, "THE AUTONOMY THRESHOLD", owns the conditions and `followups.py take-up` enforces them,
refusing the step when any is asserted without evidence. **Read them there and evidence each one.**

- Every condition holds and is evidenced → `take-up`, then Step 5.
- Any condition fails, **or you are unsure whether it holds** → surface the entry to the user with its
  question and move to the next entry. That is the normal outcome, not a failure state.
- **NEVER run `accept`.** It is the user's edge and the only way into `accepted`.
- **NEVER run `publish`.** There is no autonomous path to it, from any state.

## Step 5 — FIX, and open ONE PR

Four kinds of entry reach this step. Each already carries its decision, which is why only the first came
through Step 4:

- one Step 4 just took up (`self-accepted`);
- a `self-accepted` entry that was already in the store — the driver took it up in an earlier session
  and no PR exists yet;
- a `reopened` entry — its PR died, so it resumes at opening the **replacement** PR;
- an `accepted` entry **whose ruling authorized a FIX**. The entry records *when* the user decided and
  **not what they approved**, so when it cannot be told whether the ruling was for a fix or for
  publication, **surface it and work the next entry**. Publication is Tier 3 and is never this skill's
  to do.

None of them is looked up against an existing PR first: there is no durable follow-up-to-PR key before
`open-pr` writes one, so an interrupted earlier session can leave a duplicate PR here. That gap is a
known non-goal of the loop, owned by `followups.md`, "Same-run idempotency is a deliberate non-goal" —
do not invent a reconciliation for it.

For each, dispatch one subagent in the model class `../campaign/SKILL.md`, "Worker
Dispatch — logical model class", assigns the **Follow-up fixer**.

**Strictly one at a time.** Finish an entry's PR before starting the next entry's fix. One follow-up per
PR, never a grab-bag: a PR bundling several is one no reviewer can reason about, and its partial
rejection strands the rest.

- The worker's scope and semantic-sweep discipline are owned by
  `../campaign/references/fix-subagent-contract.md`. The follow-up fixer that opens a **new** PR is
  outside that file's three materializer roles, so its prompt is assembled here rather than through
  `worker-prompt.py`.
- The worker's prompt carries, as data, the same absolute paths Step 3's prompt list names, plus the
  entry itself. It resolves none of them for itself.
- The target base and how it is chosen per follow-up are owned by `followups.md`, "APPLICABLE →
  `take-up`, then a FIX SUBAGENT that opens a PR". Hand the worker a worktree branched from that base.
- The worker branches, commits, pushes, and opens the PR **carrying the `gauntlet-authored` label**.
  Without it, adoption reads the PR as `external` and campaign's own later repair of the PR it authored
  is blocked (`../campaign/references/pr-adoption.md`, `pr_origin`).
- Record it in the **same turn that saw the PR open**:
  `python3 <abs>/followups.py --file <abs-store> open-pr --id fuN --pr <ref>`. The entry stays in the
  store and now names which PR is addressing it.

## Step 6 — REPORT, before the hand-off

Report **before** Step 7, because Step 7 ends this skill and anything unsaid is lost with it. For every
entry touched, give its id, its title, and its outcome: reconciled, corroborated, refuted, taken up with
its PR, or surfaced for a ruling. Then:

- list every entry left unworked and why (`--limit`, an unresumable state, a dispatch that failed, a
  command that refused and which one, a PR another run owns);
- list every question a user must answer, each with the entry it belongs to;
- print in full every entry a deleting step removed, since the store no longer holds it;
- name the PRs about to be handed to campaign.

## Step 7 — FOLD the PRs into a campaign

Collect every PR this invocation opened, plus every open PR Step 2 found sitting outside the gate. As
the **last action of the invocation**, hand them to campaign in **one** invocation with bare `#PR`
arguments:

```text
Claude Code: /gauntlet:campaign #<pr> #<pr> …
Codex:       $gauntlet:campaign #<pr> #<pr> …
```

Bare `#PR` arguments **start a new run that adopts those PRs**
(`../campaign/references/run-identity-and-lease.md`, "Resolving a heartbeat"). It therefore takes no
existing lease and cannot collide with a run another driver is already driving. Do **not** try to adopt
into a live run from here, and do not run `pr-adopt.py` directly.

Every PR then faces the same review gauntlet as any other. That is the point of `self-accepted` rather
than `accepted`: this skill may take a follow-up up on its own, but the PR it produces is judged by the
independent gate, never self-approved.

If there is nothing to hand over, invoke nothing and stop after Step 6. **Invoking campaign ends this
skill** — the campaign drives its own loop from there.

## Critical rules

- **Two subagents, in that order, always.** The investigation reproduces before anything changes; the
  fixer authors code the gauntlet then judges. Never one worker doing both, and never the driver doing
  either inline.
- **A refused command is not an answer.** "When a command refuses — one rule, for every step" owns what
  happens then, and it is the only place in this file that says.
- **NEVER hand-edit `.gauntlet/followups.jsonl`.** `followups.py` is the only door; every concurrent run
  writes that one file and nothing can rebuild a lost entry.
- **NEVER `accept` for the user, and NEVER `publish`.** An issue is a published claim made in the user's
  name (`followups.md`, Tier 3).
- **NEVER bundle two follow-ups into one PR.**
- **NEVER commit anything under `.gauntlet/`.** It is driver bookkeeping, not repository content. Stage
  only the source files a fix touches, by explicit path.
- **Recording a follow-up never discharges anything.** If a fixer reports a site it deliberately left
  alone, record it as a **new** candidate through `followups.py add` — it is evidence for a future
  entry, not a substitute for the work in hand.
