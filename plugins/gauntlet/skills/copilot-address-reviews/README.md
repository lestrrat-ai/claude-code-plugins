# Copilot Address Reviews

Part of the [gauntlet](../../README.md) plugin.

Point it at a GitHub pull request and it works through that PR's **GitHub Copilot review
comments** one at a time. It never assumes the review is right: for each comment it checks the
claim against the actual source and tests *before* touching anything, fixes only what turns out to
be genuinely valid, asks you before subjective or design or constraint-driven changes, resolves the
threads for the items it fixes (plus any deferred or won't-fix items you've confirmed), and finishes
with a summary of every item and what it decided.

The core stance is evidence first. It will **never** change code just to make a review comment go
away — a Copilot suggestion is a claim to verify, not an instruction to follow. If the code is
already correct, the item is rejected with the reasoning recorded and no fix made.

## What it's good for

- Clearing a PR's Copilot review comments in a way you can trust, without rubber-stamping whatever
  the bot said.
- Separating the real issues from the noise — verified defects get fixed, everything else is
  explained rather than blindly patched.
- Leaving an auditable trail: every item ends with a decision, the evidence behind it, and (for
  fixes) the commit that addressed it.

## How to use it

Invoke it with a GitHub PR URL. Use `/gauntlet:copilot-address-reviews` in Claude Code or
`$gauntlet:copilot-address-reviews` in Codex:

```
address the Copilot reviews on https://github.com/owner/repo/pull/42
```

It needs the PR's branch checked out locally, or a clear path to fetch and switch to it, since it
addresses review items on an existing checkout — it doesn't open or merge the PR. It talks to GitHub
through the `gh` CLI (which figures out the repo from the PR URL), so `gh` must be installed and
authenticated for the PR's GitHub host.

It goes item by item and doesn't decide the judgment calls for you. When an item is subjective
(naming, style, wording), touches public API or compatibility, is a performance-vs-simplicity
tradeoff, or looks driven by a local constraint it can't verify from the code, it pauses and asks
you — with a summary of the item, the current behavior, why it's a judgment call, and its
recommended options — before making any change. Deferred and won't-fix outcomes are your call too.

## What to expect

Every item ends with exactly one outcome:

- **Valid** — it makes the smallest change that addresses the verified issue, runs focused tests
  first (adding one that fails before the fix and passes after, when that materially proves the
  claim), commits only that item's files, and resolves the corresponding review thread.
- **Invalid, deferred, won't-fix, or already-fixed** — no fix. It records why, and if the
  reasoning isn't already obvious from the source or tests it may leave a brief code comment near the
  relevant logic so a future reviewer sees the decision. Deferred and won't-fix threads are resolved
  only after you've confirmed that outcome.

It commits per item — one item, its own focused commit, built around the behavioral change rather
than around Copilot — and never makes a no-op commit for an item it didn't fix. When several comments turn out to be the
same underlying defect, it asks before folding them into one commit. It runs focused
tests for the item at hand and reaches for broader tests when the change touches shared behavior.
At the end it reports every item with its decision, the evidence, and the commit hash where one
applies.

## Good to know

- It only works **unresolved** items and never re-touches ones that are already resolved.
- It reads review data through the `gh` CLI only — it never scrapes GitHub HTML.
- It resolves the supplied checkout once through the shared typed repository-context owner, then writes
  its scratch files under that repository's scratch root, never inside the skill directory or from an
  ambient project variable.
- It never posts comments or replies on your behalf; decisions are reported to you locally.
- Full mechanics live in [`SKILL.md`](./SKILL.md).
