## The fix-subagent contract — materialize the prompt, then dispatch its exact bytes

**Use `scripts/worker-prompt.py fix` for the three shipped fix-worker roles: economy CI-fix, `session`
CI-fix, and review-fix.** It materializes the complete prompt for each. These three roles repair an
**existing PR branch** and push to it. Never assemble, shorten, or supplement one of their prompts from
prose.

**The follow-up fixer that opens a NEW PR is a separate workflow, outside these three roles.** It creates
a branch and opens its own PR — which no materializer role does — so its dispatch is owned by
`followups.md`, not by this materializer. It still honors this file's SCOPE and SWEEP discipline; the
no-supplement rule above binds only the three materializer roles, not that workflow's branch-and-PR
creation.

`scripts/worker-prompt-template.txt` is the **one owner of shared prompt wording**: preflight evidence,
scope, semantic sweep, and report. It also owns each role-specific prompt block. `worker-prompt.py`
validates required blocks, binds dynamic data once, and publishes `prompt.txt` plus `metadata.json` as one
atomic directory. The sibling fixture suite pins exact bytes and rejects a template missing a required
block. This file owns the dispatch procedure, not a second copy of prompt text.

### PRE-FLIGHT — the base must be current before ANY fix is dispatched

**BEFORE dispatching ANY fix subagent (review-fix or CI-fix) for a PR, run
`python3 scripts/base-preflight.py check --pr <N> --worktree <worktree> --base <base> --file <state.jsonl>`.**
`<base>` is **this PR row's effective base** — its explicit `base_branch`, else the legacy header fallback
(`ledger.py`'s `effective_base`), never the one header base. With `--file`, the ROW owns the base: `--base`
is an **assertion** that must equal the row's effective base, and the helper also compares the PR's live
`baseRefName` against it. It fetches `origin/<base>` and prints ONE of three verdicts, and the action
**splits by verdict** — only `proceed` clears the dispatch, and the other two are NOT the same response:

- **`proceed`** → the base is current; **dispatch the fix**. The `--file` makes the `proceed` record
  `base_ok_sha` for the current head — the MECHANICAL precondition `ledger.py verdict` enforces
  (`stage-2-review-gate.md`, "Recording a verdict"): a review verdict is refused for a head with no fresh
  `proceed`.
- **`rebase-first`** → the branch conflicts with its base or lacks the refreshed base; do **NOT** dispatch.
  **REBASE the PR onto `<base>`** through `stage-2-review-gate.md`'s base-currency handling, which runs
  `clean-rebase.py` FIRST: a clean base-only rebase (exit 0) PRESERVES the verdicts and label, and only its
  **exit 3** — conflict OR diff-changed — falls back to the JUDGMENT path that resets the gate and re-mirrors
  the label. Then **re-run the pre-flight**.
- **`recheck`** → mergeability is not computed yet, the view carried an unknown value, base ancestry could
  not be verified, **or the PR was retargeted** — its live `baseRefName` no longer equals the row's effective
  base (a different branch NAME, not a mere ADVANCE of the same branch). A retarget is an unsupported mid-run
  change: the reason is the machine-blocker wording a reconcile/re-adoption park records (`base changed from
  <recorded> to <live>; not supported mid-run`); **park the row** on the user through that path rather than
  dispatch. For the other recheck causes, do **NOT** dispatch and do **NOT** rebase — **re-poll**, then
  **re-run the pre-flight**.

A fix authored on a stale or conflicting base is wasted work: it is re-reviewed against the rebased tip
anyway, and its diff may not even apply. The tool fetches and decides only — it performs no rebase; the
driver rebases only on `rebase-first`, exactly as at the review-gate site.

### Materialize the exact prompt bundle

**Write dynamic worker data to byte files, then call the materializer through typed argv.** The concrete
issue file names every affected file. CI roles also receive the exact failing logs through a log file;
review refuses a log file. Repository and worktree context remain argv fields.

```
argv: ["python3", path_join(skill_dir, "scripts", "worker-prompt.py"), "fix",
       "--role", role,
       "--project-root", repository.project_root,
       "--worktree", worktree,
       "--pr", pr,
       "--base", base,
       "--file", state_jsonl,
       "--preflight-verdict", "proceed",
       "--issues-file", issues_file,
       optional_ci("--logs-file", logs_file),
       "--output-dir", attempt_prompt_bundle]
```

`role` is exactly `review`, `ci-session`, or `ci-economy`. `base` is this PR row's effective base (as in the
pre-flight above); `--file <state.jsonl>` makes `worker-prompt.py` assert `--base` equals the `--pr` row's
effective base and refuse a disagreement, so the base baked into the fix prompt can never be a branch the row
does not track. The output directory must not exist. Use
`prompt.txt` as the worker's complete prompt bytes. Read `metadata.json` and dispatch with its `role` and
logical `model_class`; the runtime adapter maps that class to the active host. Never map a host model name
inside this tool or add prompt text after materialization.

**Bundle consistency is guaranteed per directory, never across directories.** A single `os.rename`
publishes `prompt.txt` and `metadata.json` together, so within ONE published bundle directory the
metadata's `prompt_sha256` always describes that exact `prompt.txt`. The tool verifies no pairing at
consumption, and it does not need to: consume both files from the one directory it published. Reading
`prompt.txt` from one bundle and `metadata.json` from a DIFFERENT one — a hand-paired mix of two
git-ignored `.gauntlet/tmp` outputs — is a single-user footgun outside the threat model, not defended
(`CLAUDE.md`, single-user advisory workflow).

**Stop on every refusal.** Missing or conflicting role inputs, any preflight verdict other than `proceed`,
invalid UTF-8/NUL payloads, unsafe paths, template drift, existing output, or partial publication produces
no usable bundle. The script launches no worker and judges no repair.

**THE ORCHESTRATOR RECORDS EVERY SITE THE FIXER LEFT ALONE — as a follow-up, in the same heartbeat it reads the
report.** The template's report block makes the subagent report sites it deliberately did not touch (and any
pre-existing defect it noticed and declined to fix); **that report dies with the subagent**, so a
disposition nobody wrote down is a defect nobody will ever see again. Record each one through
`scripts/followups.py` — the subagent's own report is the entry's evidence, and why the fixer left it
alone is why it was deferred. They are **CANDIDATES, not issues**: local, and never published without the
user's agreement on that specific item (`followups.md`).

**BUT NEVER THE ASSIGNED FINDING — a follow-up is not a verdict, and it CANNOT discharge one.** The
CONFIRMED/ADJUSTED findings the fixer was dispatched at are **fixed, or they stay blocking**: recording one
as a follow-up settles nothing, and a fixer that defers the very defect it was sent to fix has done
nothing. This block records what the fixer left **beside** the fix, never the fix it declined to make
(`followups.md`, "RECORDING A FOLLOW-UP NEVER DISCHARGES A FINDING").
