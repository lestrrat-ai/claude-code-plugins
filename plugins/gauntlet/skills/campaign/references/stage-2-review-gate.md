## Stage 2 — Gates (orchestrator-owned, reactive)

### 2a. The review gauntlet

**Preconditions — clear Copilot items, CI, and conflicts before reviewing.** A codex review pass is
expensive and is invalidated by any PR-content change, so never spend one on a PR whose current tip
still has review-blocking issues. Before launching a pass, check three things and clear any that are
dirty. Each fix changes PR content, so `reviews_ok` resets to 0 and the review re-starts on the clean
tip:

- **GitHub Copilot review items.** If the PR has any unresolved Copilot review comments, address them
  with `/gauntlet:copilot-address-reviews <pr>` before reviewing (that skill verifies each item against source
  before changing code, works them one at a time, and resolves the threads). Detect them from a
  stored `gh` snapshot — the copilot skill's `fetch-review-items.sh` normalizes unresolved
  Copilot-authored comments into `.gauntlet/tmp/copilot-review-items.json` — never scrape HTML. That path is
  **shared across runs**, so treat it as ephemeral: fetch immediately before acting and **verify the
  JSON is for THIS PR** (re-fetch if a concurrent run overwrote it), and don't interleave two runs'
  copilot-address cycles. No items → no-op.
- **CI failures.** If `ci` is red for the current tip, do NOT review — fix CI first (Stage 2b).
  Handle failures **one at a time per PR/SHA**, and **prefer a scoped subagent** per failure; different
  PRs may fix CI concurrently within the cap.
- **Merge conflicts with `<base>`.** If GitHub flags the PR conflicting/behind
  (`gh pr view <pr> --json mergeable,mergeStateStatus` → `CONFLICTING` / `DIRTY` / `BEHIND`), rebase
  it onto `<base>` before reviewing. Clean rebase with the PR diff unchanged keeps `reviews_ok` but
  sets `ci = pending`; conflict-resolving rebase changes PR content, so it resets the gate (Stage 3
  step 5).

Only launch a review pass once all three are clear for the current tip.

Run reviews **one at a time per PR** — never two at once for the same SHA. When a PR's tip
(`head_sha`) has fewer than two SATISFIED verdicts and no review already running for it, the wake's
dispatch step launches **one** review pass: a **fresh** `codex exec` over the whole `<base>...HEAD`
diff, no shared context, run as a **background** task (its completion is a wake; the loop folds the
verdict in at step 2). The second, corroborating review is launched only **after** the first comes
back SATISFIED — so a still-broken commit never burns the second review before the first has said
"fix it". (Reviews for *different* PRs still run concurrently, up to the ~8 cap; it's only the two
reviews for the same PR that serialize.) Each pass is a separate process with no shared context, so
the second verdict is a fresh, context-isolated execution rather than a continuation influenced by the
first.

**Kill doomed passes — don't let them finish.** If a precondition goes dirty while a review is in
flight on a PR — CI turns red, Copilot items land, a conflict appears — or any content-changing fix
is about to be dispatched for it, **stop the in-flight review task before dispatching the fix**: its
verdict can only describe a SHA that is about to be replaced, so letting it run wastes both the
tokens and the review slot. The freed slot immediately refills with the next due review (Loop
control step 3).

If a pass's `codex exec` can't return a verdict (quota/rate-limit, auth, timeout, or other system
error — see "Codex fallback"), retry it once, then run that pass as a **fresh subagent** reviewing the
whole `<base>...HEAD` diff under the same output contract — a `RESIDUAL-RISK` line on SATISFIED
immediately above exactly one final `VERDICT:` line. A subagent fallback pass counts
toward the two-SATISFIED-verdict gate exactly like a codex pass — it's another fresh, context-isolated
re-roll in its own context.

**Review work-plan ledger — orchestrator-owned, target-generic.** Before launching each review pass,
write `<rundir>/review-<pr>-<n>.plan.jsonl`. The orchestrator owns the plan; the reviewer reports
progress against it but does NOT redefine it. The reviewer is nonetheless expected to critically
evaluate the plan for completeness before executing it, and to flag any omitted dimension via the
amendment mechanism below rather than silently accepting the supplied decomposition as exhaustive.
Derive units from the review target, not from fixed global stages:

- **Code PR default** → changed files/modules, public API/behavior boundaries, cross-file invariants,
  tests/coverage relevant to changed behavior, migration/docs/golden updates when touched.
- **Docs/articles/non-code** → artifact/section units, claim-support/evidence checks, structure/flow,
  tone/audience, repetition, terminology/cross-document consistency, citations/sources if present.
- **Mixed target** → include both code-shaped and artifact-shaped units.

Plan JSONL schema:

```
{"type":"unit","id":"u01","kind":"file","target":"xsd/validate_idc.go","checks":["value canonicalization","union member selection"]}
{"type":"unit","id":"u02","kind":"cross-cutting","target":"IDC key equality","checks":["primitive tags","list boundaries","keyref parity"]}
```

Rules:

- Keep units auditable and finite. Prefer 5–15 units; split huge units, merge tiny mechanical ones.
- The plan describes PR content, so **reuse it across passes on unchanged content**: for pass 2 on
  the same SHA (or clean base-only rebase, diff unchanged), copy pass 1's plan to
  `review-<pr>-2.plan.jsonl` instead of re-deriving. Re-derive only when PR content changed.
- Each unit MUST name concrete `target` + concrete `checks`.
- For code, include at least one cross-cutting unit when behavior spans files or packages.
- For non-code, include at least one cross-artifact/whole-piece unit when multiple artifacts/sections
  exist.
- **The reviewer must not treat the plan as presumptively complete.** Before working the units, judge
  whether they cover the dimensions this target actually needs; deterministic coverage is a design
  goal, but the orchestrator's decomposition can still miss something. When a materially important
  review dimension is omitted (or a unit is wrong), the reviewer MUST append a `plan_amendment_request`
  event naming the gap rather than silently reviewing only the listed units — an unraised omission is a
  reviewer failure. Requesting an amendment is the *only* sanctioned response: the reviewer never
  rewrites the plan or self-grants units, and unapproved amendments do NOT count as plan units. The
  orchestrator folds that request on the next wake and either updates the plan + restarts the review
  pass, or ignores it with a note; the reviewer completes the existing units meanwhile.

Progress JSONL schema:

```
{"type":"progress","unit":"u01","status":"started","ts":"2026-07-06T00:00:00Z"}
{"type":"progress","unit":"u01","status":"done","ts":"2026-07-06T00:04:00Z","evidence":"checked canonicalization paths and edge-case tests"}
{"type":"plan_amendment_request","ts":"2026-07-06T00:05:00Z","reason":"diff changes generated docs; add doc consistency unit","proposed_unit":{"id":"u99","kind":"docs","target":"docs/generated.md","checks":["sync with API behavior"]}}
```

Meaningful progress = a `done` event for a planned unit, or an accepted plan amendment. `started`
events and vague "still working" lines prove only process liveness and MUST NOT reset the meaningful
progress timer. The reviewer MUST append progress events immediately as units complete, not batch them
at final output. If no meaningful progress lands for ~15 min while the review process is still alive,
mark the review suspicious; if it remains stale on the next wake, treat it as a codex system failure:
retry once, then use the fresh-subagent fallback. Ignore any late verdict from a stale/superseded
attempt unless its attempt id still matches the active review pass.

```
codex exec --sandbox workspace-write -c "sandbox_workspace_write.network_access=true" -C $PROJECT/.worktrees/<branch> \
  -o <rundir>/review-<pr>-<n>.txt \
  "Review the changes on this branch vs <base> (the whole git diff <base>...HEAD). \
   First read $PROJECT/<rundir>/review-<pr>-<n>.plan.jsonl, then critically assess whether its units \
   cover the review dimensions this change actually needs — the plan is the orchestrator's starting \
   point, not a guarantee of complete coverage. If an important dimension is missing or a unit is \
   wrong, append a plan_amendment_request event to the progress JSONL naming the gap; do NOT silently \
   limit your review to the listed units, and do NOT rewrite the plan yourself. Then append progress \
   JSONL to $PROJECT/<rundir>/review-<pr>-<n>.progress.jsonl as each planned unit starts and finishes; \
   progress counts only when it references a planned unit and includes concrete evidence. \
   After every planned unit is done, do a brief UNSTRUCTURED ADVERSARIAL SWEEP: deliberately hunt for \
   defects no plan unit would naturally catch — cross-unit interactions, unstated assumptions, edge \
   cases, and whole categories the plan did not enumerate. This complements the plan, never replaces \
   it. Report only concrete file:line defects that would actually fail, at the same bar as any finding; \
   finding nothing is a fine and common result — do NOT lower the bar or list speculative 'might be \
   fragile' concerns. \
   List any issues with file:line and a concrete fix. If — and only if — your verdict is SATISFIED, \
   output one line immediately above the verdict, in the form RESIDUAL-RISK: <area or file> — <why \
   this was the hardest part to verify fully>, naming the part of the diff you checked with the LEAST \
   certainty relative to the rest. It is a calibration signal, NOT a finding, and does not weaken your \
   SATISFIED — do not manufacture a concern to fill it; if identifying it surfaces a real defect, list \
   it with file:line and return NOT SATISFIED instead. End with exactly one line: \
   'VERDICT: SATISFIED' or 'VERDICT: NOT SATISFIED'."   # run in background
```

As each verdict lands, tally it for the SHA it ran on:

- **NOT SATISFIED** → dispatch a scoped fix subagent into that worktree with the issue list; it
  commits + pushes → HEAD advances → the SHA's tally is void. A later wake starts a fresh review on
  the new tip. (Because reviews are sequential, no second review was spent on this broken commit.)
- **SATISFIED** → record it. If it's the **first** for this SHA, the next wake launches the second
  (corroborating) review on the same SHA. If it's the **second** SATISFIED on the same SHA, the
  review gate is met for this HEAD — swap the PR's label:
  `gh pr edit <pr> --remove-label gauntlet-reviewing --add-label gauntlet-accepted`.

Every pass reviews the whole `<base>...HEAD` diff (not just the last fix-delta), so accumulated fixes
are always judged as one piece.

**Unstructured adversarial sweep.** After a pass finishes every planned unit, it runs one brief
free-form sweep for defects the plan's decomposition would never surface — cross-unit interactions,
unstated assumptions, edge cases, and whole categories no unit enumerated. It **complements** the
structured plan and never replaces it: the units still run in full first. The sweep reports through the
normal finding channel — only concrete `file:line` defects that would actually fail, held to the same
bar as any other finding, so a real one drives `NOT SATISFIED`. It is NOT a brainstorm: "nothing found"
is the expected, honest common outcome, and speculative "might be fragile" notes are not findings and
do not block SATISFIED. (This is distinct from a `plan_amendment_request`, which fixes the plan
structurally; the sweep finds a defect now, regardless of the plan.)

**Residual-risk signal (SATISFIED only).** A SATISFIED verdict carries one
`RESIDUAL-RISK: <area> — <why>` line naming the part of the diff the pass verified with the least
certainty, relative to the rest. It is calibration metadata, never a finding and never a verdict
input: a SATISFIED with a residual-risk line is a **full** SATISFIED, and the line NEVER withholds the
gate, NEVER enters the fix loop, and is NEVER fed into the corroborating review (which stays
context-isolated). It reflects the gauntlet's purpose — lower the odds a defect survives stochastic
variation, not claim certainty — by making residual uncertainty explicit instead of hidden behind a
binary verdict. Record it with the verdict and carry **each accepting pass's** line into the final
report, grouped by PR (a PR accepts on two SATISFIED passes, so two lines). Its only aggregate use:
when **both** accepting passes on the same content name the same area, note that convergence in the
report, and the orchestrator MAY add a plan unit covering it the next time the PR content changes and a
fresh review round starts — but it never blocks the current gate.

**Gate is two fresh, context-isolated SATISFIED verdicts on the same PR content.** The two passes are
not statistically or epistemically independent observations — they judge the same diff under the same
review task and protocol (and, on the normal both-codex path, the same model and prompt), so their
verdicts are correlated and this is not a probabilistic proof of correctness. What the second pass
buys is a re-roll of a stochastic reviewer: a fresh execution, with none of the first pass's context
to anchor it, that can catch a defect the first pass happened to miss. Record the reviewed SHA
(`git rev-parse HEAD`) with each pass. A verdict counts while its SHA equals the live tip. It also
continues to count after `<base>` advances if the PR is still non-conflicting and the PR diff/content
is unchanged (e.g. clean base-only rebase); carry `reviews_ok` forward to the new `head_sha` and set
`ci = pending`. The moment PR content changes — review fix, CI fix, conflict-resolving rebase, a
formatter/bot commit on the PR branch, or manual push — earlier verdicts are stale and `reviews_ok`
drops to 0. Pinning to SHA plus the clean-base-only exception makes the gate verifiable from git while
not burning reviews merely because another PR merged cleanly. A `NOT SATISFIED` invalidates that
content's tally even before a fix lands. The two satisfied verdicts and green CI must all describe the
same live PR content; CI must still be green for the current HEAD SHA.

**Status labels mirror the review gate.** A PR carries `gauntlet-reviewing` until its current HEAD
holds two SATISFIED verdicts for the same live PR content, then `gauntlet-accepted`. Because any
PR-content change resets the gate, if an accepted PR's content later changes — a CI fix,
conflict-resolving rebase, formatter/bot commit, etc. — swap the label back
(`--remove-label gauntlet-accepted --add-label gauntlet-reviewing`). A clean base-only rebase with
unchanged PR diff keeps the review label state but sets `ci = pending`. Reconcile labels against the
live gate state each wake so they never lie.
