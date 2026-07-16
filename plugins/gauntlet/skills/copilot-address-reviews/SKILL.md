---
name: copilot-address-reviews
description: Evaluate and address GitHub Copilot PR review items for a GitHub pull request. Use when user provides a GitHub PR link and wants Copilot review comments checked, verified, fixed, committed, and summarized. Fetch review items with `gh`, verify each claim against source/tests before changing code, ask user before making subjective or constraint-driven changes, then work items one by one.
---

# Copilot Address Reviews

Given a GitHub PR link, process Copilot review items one at a time. NEVER assume review is correct. NEVER change code only to satisfy review text.

## Inputs

- PR URL
- Local checkout for PR branch, or clear path to fetch/switch to it
- User confirmation for subjective, design, or constraint-driven items

## Workflow

1. Resolve bundled scripts relative to the directory containing this `SKILL.md`, not the current
   working directory. Resolve the shared typed runtime owner at
   `../campaign/references/runtime-adapter.md` from that same skill directory. NEVER `cd` to the skill
   directory — the fetcher's relative scratch default would land output there.
2. At workflow entry, call the runtime owner's `resolve_repository_context(checkout)` exactly once with
   the supplied local checkout. Then call `run_argv` with these distinct fields (the script creates the
   scratch directory if missing):

   ```text
   run_argv(
     argv: ["bash", fetch_review_items_script, "--tmp-dir", repository.scratch_root, pr_url],
     cwd: repository.project_root, stdin_file: null, stdout_file: null
   )
   ```

   `fetch_review_items_script` is the absolute path resolved in step 1. Invoke bundled scripts through
   their interpreter — `bash` for `.sh`, `python3` for `.py` — NEVER bare: a bare invocation depends on
   the executable bit and the shebang surviving every checkout, archive, and install path.
3. Read `path_join(repository.scratch_root, "copilot-review-items.json")` through `read_bytes` as the
   primary worklist of unresolved items only.
4. Inspect the sibling paths under `repository.scratch_root` named
   `copilot-review-items.raw.json`, `gh-pr-view.json`, and `gh-pr-review-threads.json` through
   `read_bytes` when dedup or extraction needs verification. Never resolve these from cwd.
5. Select next unhandled unresolved item from worklist. NEVER work resolved items.
6. For current item, choose exactly one outcome before moving on:
   - valid → fix
   - invalid → no code change
   - subjective/constraint-driven/ambiguous → ask user
   - deferred/won't fix → ask user, then no code change
   - already fixed on branch → no code change
7. After each valid item:
   - make smallest code change that addresses verified issue
   - run focused tests first
   - add/update tests when they materially prove claim or prevent regression
   - run broader relevant tests when change touches shared behavior
   - commit only files for that item
   - after commit, resolve corresponding GitHub review thread/comment if it is still unresolved
8. After each invalid, deferred, won't-fix, or already-fixed item:
   - record why no code change is needed
   - if current source/tests do not already make decision obvious, add concise code comment near relevant logic so future reviewers can see reasoning or constraint
   - if outcome is deferred or won't fix and user confirmed that outcome, resolve corresponding GitHub review thread/comment if it is still unresolved
   - do not make speculative edits
9. Mark current item handled, then look up whether more unhandled items remain.
10. If more items remain, return to step 5. Continue until worklist is empty.
11. After all items, report every item, decision, evidence, and commit hash when applicable.

## Scripts

Resolve bundled resources relative to this `SKILL.md`. Script directory = `scripts/` next to this file.

- Installed skill layout: skill directory contains `SKILL.md` + `scripts/` as siblings. When installed
  as part of the `gauntlet` plugin, derive its absolute path from the active `SKILL.md` path supplied by
  the host. Do not depend on a plugin-root environment variable.
- Repository layout: skill directory = `plugins/gauntlet/skills/copilot-address-reviews/`.
- Run scripts by absolute path with the `RepositoryContext` fields established at workflow entry. The
  fetch operation in step 2 owns the invocation and scratch operand; do not restate its path formula.
- NEVER assume current working directory already is skill directory.

### `scripts/fetch-review-items.sh`

- Entry point for PR review item discovery.
- Use `gh` CLI only. NEVER scrape HTML.
- Save raw GitHub output under `repository.scratch_root` first.
- Fetch PR metadata + all pages of review threads/comments.
- Normalize unresolved Copilot-authored review comments into the raw-worklist sibling named in workflow
  step 4.
- Invoke `scripts/dedup_review_items.py` to write the primary worklist named in workflow step 3.
- If GitHub response shape is incomplete for current PR, extend GraphQL query or inspect raw JSON before changing code.

### `scripts/dedup_review_items.py`

- Python scope is dedup only.
- Input: normalized raw item JSON array.
- Output: deduped JSON object with representative items + grouped ids.
- NEVER expand Python script into code validation, test selection, or fix synthesis.

## Fetch Review Items

- Start with the typed operation in workflow step 2; that step owns the invocation form and every
  dynamic operand.
- Treat the primary worklist from workflow step 3 as unresolved-item candidates, not truth.
- Inspect Copilot review submissions as well as inline discussion comments. Do not assume a `#pullrequestreview-...` URL is represented directly in normalized output.
- If user points to review submission URL or review id, map it to attached inline review comments and confirm those comments are present in worklist before proceeding.
- Filter results to authors that represent GitHub Copilot review bots. Do not assume exact login string is stable if raw output shows another Copilot variant.
- Exclude resolved items from scope. Ignore them unless raw GitHub output suggests resolution state is wrong.
- Capture for each item:
  - review/comment id
  - file path + line/range
  - exact claim/request
  - thread state if available
- Read raw GitHub output when:
  - review submission URL needs to be mapped to inline comments
  - dedup merged items unexpectedly
  - line/path metadata looks wrong
  - thread state affects whether item still needs work

## Validate Before Editing

Verify claim with source and tests before changing code.

### Code Analysis

- Read surrounding implementation, callers, tests, and PR diff context.
- Check whether review conflicts with intentional invariants, API contracts, compatibility rules, performance limits, or repository conventions.
- Prefer smallest proof that establishes whether review is valid.

### Test Strategy

- Use R/G testing for general development: make failing case visible first, then change code until test passes.
- If existing tests already cover claim, run them first.
- If claim is behavioral and uncovered, add focused test that fails before fix and passes after fix.
- If claim is not directly testable, explain why and use code-level reasoning instead.
- NEVER add a test that merely encodes Copilot preference when behavior is intentionally unspecified.

## Ask User Instead Of Deciding

Ask user before changing code when item is primarily about:

- naming, style, or readability with no correctness issue
- API shape, public behavior, or compatibility policy
- performance vs. simplicity tradeoff
- logging/error wording
- architectural direction or ownership boundaries
- possible arbitrary local constraint that cannot be verified from code/tests

When asking user, include:

- review item summary
- current behavior
- why item appears subjective or constraint-driven
- recommended options

## Fix Rules

- Keep fix scoped to verified issue.
- Avoid opportunistic cleanup unless required for fix or approved by user.
- If multiple Copilot items map to same defect, ask user before combining them into one commit.
- If current branch already addresses item, record it as already fixed and move on.

## Review Interaction

- NEVER post GitHub comments, review replies, review submissions, or issue comments on user's behalf.
- Resolve relevant GitHub review thread/comment only after committed fix exists.
- Resolve relevant GitHub review thread/comment after user-confirmed deferred/won't-fix decision exists.
- NEVER resolve review thread/comment before commit for that item exists.
- NEVER resolve deferred/won't-fix thread/comment before user decision for that item exists.
- Resolve thread/comment without posting reply text.
- Report decisions to user in local output instead.
- If a GitHub comment would help, ask user first. Draft text only when user asks for it.

## Commit Rules

- One focused commit per addressed item.
- Stage only files relevant to current item.
- Write commit message around behavioral change, not around Copilot.
- Follow repository/user git rules before `git add` / `git commit`.
- Do not create no-op commit for rejected, subjective, deferred, won't-fix, or already-fixed items.

## Final Report

Report items in review order or grouped by file. For each item include:

- item identifier or file/line
- Copilot claim summary
- decision: fixed / rejected / deferred / won't fix / already fixed
- evidence: code reading, tests run, or user instruction
- commit hash for fixed items
