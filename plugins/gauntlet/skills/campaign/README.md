# Campaign

Part of the [gauntlet](../../README.md) plugin.

Point it at your code and it runs a tough, end-to-end review cycle for you: an adversarial review
finds problems, each real one gets fixed in its own pull request, every PR is re-reviewed until it
passes a strict quality bar **and** CI is green, and then it merges — all on its own, hands-off.

Think of it as an automated senior reviewer that doesn't just leave comments, but follows through:
files the fixes, defends them through repeated review rounds, waits for CI, and ships.

## What it's good for

- Hardening a codebase or a feature area before a release.
- Turning "someone should really review this" into actual merged fixes.
- Running a thorough pass unattended while you do something else.

## How to use it

```
/gauntlet:campaign                 # campaign over the whole repo
/gauntlet:campaign auth & sessions # campaign over just that area or topic
/gauntlet:campaign --new           # force a brand-new run, even over an old one
```

Run it **once** — that's it. It schedules its own follow-ups and keeps working until everything is
resolved; you don't need to keep it open or re-run it.

If you do come back and run it again later, it does the sensible thing. Run it plain (no area) and it
resumes a run that's still in progress; if the last run already finished it asks whether to start a
fresh one rather than restarting silently or insisting everything's already fixed. Give it an area
(or `--new`) and it starts a new run instead — which is also how you deliberately run two at once over
different parts of the code. A fresh run isn't a blind redo — it remembers what earlier runs learned (which findings it
gave up on, which it set aside as your call, and which it judged not worth fixing) so it can pick up
the unfinished threads and not re-litigate the same non-issues. Add `--new` (or just say "start a
fresh run") to force a new run immediately without being asked.

## What to expect

It opens a pull request for each problem worth fixing and merges them itself once they pass two
separate, context-isolated reviews of the same PR content and CI is green. (Two fresh reviews rather than
one because a single stochastic review can miss a defect — not because two runs are statistically
independent; reading the same diff under the same review task makes their verdicts correlated.) There's no approval step along the way, so
starting it is your sign-off — and a whole-repo run can spin up several PRs and keep going for a
while before it's done.

The loop is PR-first: it implements, commits, pushes, opens or updates the PR, then watches CI and
reviews that PR's current HEAD. Review and CI fixes get another commit and push on the same PR; it
doesn't keep work local until everything has passed.

It also doesn't wait around. On a big scope the review sweep runs in slices, and fixes start flowing
as soon as the first findings are confirmed — while the rest of the sweep is still going. Everything
long-running happens in the background, so at any moment it's doing all the work that's ready to do.

You can follow along on GitHub: each PR is labeled `gauntlet-reviewing` while it's working through
the loop, and that flips to `gauntlet-accepted` once it has passed both reviews (the skill creates
the labels if your repo doesn't have them).

By default it checks with you before changing anything in your public API — exported signatures,
formats, CLI flags, defaults, or any behavior callers depend on — so it never merges a breaking
change behind your back. Tell it up front that breakage is fine and it'll stop asking.

It tidies up as it goes, deleting merged branches and their worktrees. If a fix just can't clear the
bar, it retries once, then sets that one aside with a note on why and moves on rather than stalling
everything else. When it's finished you get a short rundown: what merged, what it gave up on, and
anything it left for you to weigh in on.

## Flow

```mermaid
flowchart TD
    A(["invoke /gauntlet:campaign"]) --> A1{"live work, finished run, or --new?"}
    A1 -- live work --> A2[resume: reconcile + continue loop] --> M
    A1 -- finished run --> A3{start a new run?}
    A3 -- no --> A4([prior final report])
    A3 -- yes --> B
    A1 -->|"nothing yet / --new"| B
    B{area or topic given?}
    B -- yes --> C[reviewer sweeps that area]
    B -- no --> D[reviewer whole-repo sweep, in slices]
    C --> E["neutral verification pass<br/>(streamed: each slice verifies as it lands)"]
    D --> E
    E --> F{more than 10 findings?}
    F -- yes --> G[parallel verifier shards] --> H[dedup each survivor against the run's set on arrival]
    F -- no --> I[single verifier]
    H --> J
    I --> J[survivors = CONFIRMED or ADJUSTED]
    J --> K[Stage 1: fan out - one PR per finding,<br/>starting while the sweep continues]
    K --> L0[per finding: worktree off base branch, implement the fix]
    L0 --> AB{changes public API surface or behavior?}
    AB -- no --> L[commit, push, open PR, launch CI watch]
    AB -- yes --> AC{api_changes flag?}
    AC -- allowed --> L
    AC -- ask --> AD[park finding awaiting-api, confirm with user]
    AD --> AE{approved?}
    AE -- yes --> L
    AE -- no --> AF[skip - report at end]
    L --> M[[event loop: gate each PR]]

    M --> N{2 SATISFIED on current SHA?}
    N -- no --> O[run one review on HEAD SHA<br/>second only after the first passes]
    O --> P{SATISFIED?}
    P -- no --> Q[scoped fix subagent: commit + push, new SHA]
    P -- yes --> M
    N -- yes --> R{CI status on current SHA?}
    R -- red --> S[scoped CI-fix subagent: commit + push, new SHA]
    R -- pending --> M
    Q --> T[reset gates - verdicts and CI are SHA-pinned]
    S --> T
    T --> M
    R -- green --> U[merge: serialized, auto, squash, delete-branch]
    U --> U2[sync local base branch to remote ff-only]
    U2 --> V[cleanup worktree + local branch, mark merged]
    V --> W{all PRs merged or aborted?}
    W -- no --> M
    W -- yes --> X[write carryover ledger + final report] --> X2([done])

    M -. 1h cap exceeded .-> Y{first attempt?}
    Y -. yes .-> Z[retry once in fresh worktree]
    Z -.-> M
    Y -. no .-> AA[abort + write log, continue others]
    AA -.-> W
```

## Good to know

- You can run more than one at a time in the same repo — say one over `auth` and another over
  `storage`. Each is its own isolated run with its own pull requests and bookkeeping, so they never
  step on each other. And if a run gets interrupted, another agent can pick it up right where it left
  off: it can tell a run that's still being actively driven from one that's been abandoned, so it only
  ever resumes an orphaned run and never doubles up on one already in progress.
- By default the reviewer is Claude's own subagents, so it runs with nothing extra installed. For a
  stronger gauntlet you can point it at a reviewer that runs a different agent/model than the
  orchestrator — Codex CLI (`codex exec`) is the recommended example, since an independent engine
  catches defects a same-model re-roll can miss. Name it when you invoke the campaign ("review with
  codex") or record it as your preferred reviewer (memory or `CLAUDE.md`). If an external reviewer
  can't return a verdict because of a system problem — quota or rate limits, auth, a timeout — it
  retries once and then falls back to its own subagents, so a transient outage slows a run down but
  doesn't stall it.
- It works through GitHub PRs via the `gh` CLI, so the repo needs a GitHub remote.
- Before it spends a review on a PR, it first clears anything that would waste one: it addresses any
  GitHub Copilot review comments, fixes failing CI, and rebases a PR that has fallen into conflict
  with the base branch — then reviews the clean result.
- It keeps a small `.gauntlet/history/` at the repo root (git-ignored, one file per run) to remember what past
  runs learned. That's the memory a fresh run carries over. Each fresh run also tidies that file,
  dropping entries that no longer apply to the current code — and when it isn't sure an entry is
  safe to drop, it asks you first rather than guessing.
- Full mechanics live in [`SKILL.md`](./SKILL.md) and [`references/`](./references/).
