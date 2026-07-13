# Campaign

Part of the [gauntlet](../../README.md) plugin.

Point it at existing pull requests and it drives each one to merge: it re-reviews the PR against a
strict quality bar, waits for CI to go green, and merges — all on its own, hands-off. It doesn't go
hunting for problems and it doesn't write fixes from scratch; it **gates PRs that already exist**
(yours, or ones [`/gauntlet:review`](../review/README.md) opened for you) and merges each only once it
clears the bar.

Think of it as an automated senior reviewer that follows through: it defends each PR through repeated
context-isolated review rounds, fixes up whatever review or CI turns up on the PR itself, waits for
CI, and ships.

## What it's good for

- Driving a batch of open pull requests to merge under one strict, repeatable quality gate.
- Following up on a `gauntlet:review` report — turning its confirmed findings (opened as PRs) into
  actual merged fixes.
- Gating agent-facing changes (a `SKILL.md`, `CLAUDE.md`, prompt or reference file) with the same
  two-pass rigor as source code — those never get the lighter docs-only treatment.

## How to use it

```
/gauntlet:campaign #12          # adopt PR #12 into a run, gate it, merge it
/gauntlet:campaign #12 #15      # adopt several PRs into one run
/gauntlet:campaign              # resume this run's PRs, or prompt if there's nothing to gate
/gauntlet:campaign --new #20    # start a fresh run for a new set of PRs
```

Give it one or more PR numbers and it **adopts** them into a run: it labels each PR so the run owns
it, classifies the change by the *kind* of files it touches — human-facing docs vs code vs
agent-consumed docs vs sensitive surfaces — to pick a review tier (the change's size never enters
into it), then starts gating. Run it **once** — it schedules its
own follow-ups and keeps working until every adopted PR is merged or set aside; you don't need to
keep it open or re-run it.

Run it plain, with no arguments, and it picks up the PRs already under this run and continues where
it left off. If there's nothing left to gate it doesn't invent work — it tells you so and points you
at `gauntlet:review` to find issues, or asks for PR numbers. There's no whole-repo sweep and no
area/topic argument any more: campaign gates PRs you hand it, it doesn't go looking for problems.

Where do the PRs come from? You open them, or `gauntlet:review` does. Run `gauntlet:review` first for
a confirmed-findings report; at the end it can open one PR per confirmed fix and hand them
straight to a campaign — see [the handoff below](#where-the-prs-come-from-the-review-handoff).

Come back later and it still does the sensible thing. `--run <id>` resumes a specific run; `--new`
(or just "start a fresh run") begins a fresh run over a new PR set — which is also how you
deliberately run two at once over different PRs. A fresh run isn't a blind redo: it remembers what
earlier runs learned (which PRs it gave up on, which it set aside as your call) so it doesn't
re-litigate the same ground.

## Where the PRs come from: the review handoff

Campaign gates PRs; it doesn't find the problems. [`/gauntlet:review`](../review/README.md) is the
other half. Review runs its two-pass adversarial pass and, by default, only reports — it makes no
source/tracked-file or GitHub changes (it may write ephemeral `.gauntlet/tmp` review scratch). But at
the end of a confirmed-findings report it offers an opt-in step: open one pull request per
confirmed fix, then invoke `/gauntlet:campaign #PRs` on exactly those PRs. That handoff is where a
finding turns into code and a PR; campaign takes it from there and drives each PR to merge. Decline
the offer and review stays report-only — no source or GitHub changes are written. So the usual
progression is **`gauntlet:review` to find and confirm, then `gauntlet:campaign` to gate and merge**.
You can also skip review entirely and hand campaign PR numbers you opened yourself.

## What to expect

It drives each adopted PR to merge and merges it itself once the PR passes the reviews its tier
requires and CI is green. How many reviews depends on what the PR touches: a documentation-only PR
(human-facing prose alone) needs **one**; anything touching code or agent-facing files — source,
`SKILL.md`, `CLAUDE.md`, prompts, CI, scripts — always gets the full **two-pass** gate. (Two reviews
rather than one because a single stochastic review can miss a defect — not because two runs are
statistically independent; reading the same diff under the same review task makes their verdicts
correlated.) Aside from the public-API confirmation described below, there's no approval step along
the way, so starting it is your sign-off — and a run over
several PRs can keep going for a while before it's done.

The loop works on each PR in place: it reviews the PR's current HEAD and watches its CI. When a
review or CI failure needs fixing, a scoped subagent commits and pushes the fix onto the PR's **own**
branch — a new HEAD that resets the gate (verdicts and CI are pinned to a SHA). It never writes a fix
from scratch or opens a PR of its own; every change it makes is in service of getting an existing PR
through.

It also doesn't wait around. Everything long-running — reviews, CI watches, fix subagents — happens
in the background across all the adopted PRs at once, so at any moment it's doing all the work that's
ready to do.

You can follow along on GitHub: each PR is labeled `gauntlet-reviewing` while it's working through
the loop, and that flips to `gauntlet-accepted` once it has passed the review(s) its tier requires —
one for a TRIVIAL docs-only PR, two for anything touching code or agent-facing files (the skill
creates the labels if your repo doesn't have them).

The label flips **back** just as readily. Anything that changes a PR's content after it was accepted —
a CI fix, a rebase that had to resolve conflicts, a stray push to the branch — invalidates the reviews
it had passed, so the PR returns to `gauntlet-reviewing` and must earn its verdicts again on the new
content. The label always describes the code that is on the PR *right now*, so `gauntlet-accepted`
never means "this passed at some point" — it means "this, as it currently stands, passed."

By default it checks with you before changing anything in your public API — exported signatures,
formats, CLI flags, defaults, or any behavior callers depend on — so it never merges a breaking
change behind your back. Tell it up front that breakage is fine and it'll stop asking.

It tidies up as it goes, but it leaves your branches alone. Campaign **never** deletes a merged PR's
**remote** head branch — that's your repo's job: if you've turned on GitHub's "Automatically delete head
branches" setting, GitHub removes it on merge; otherwise it stays. Either way it's the repo setting, not
campaign, that decides. Locally it removes only the worktree and branch it created itself for that PR; a
pre-existing checkout or a pre-existing local branch it merely reused (e.g. your own branch already
checked out) — or your main checkout — is left untouched and reported. If a fix just can't clear the
bar, it retries once, then sets that one aside with a note on why and moves on rather than stalling
everything else. When it's finished you get a short rundown: what merged, what it gave up on, and
anything it left for you to weigh in on.

## Flow

```mermaid
flowchart TD
    A(["invoke /gauntlet:campaign #PRs"]) --> B{PR numbers, no args, or --new?}
    B -- "#PRs / --new #PRs" --> C[adopt each PR: ledger row + run label,<br/>launch CI watch]
    B -- "no args" --> D{PRs already under this run?}
    D -- yes --> C
    D -- no --> E([prompt: run gauntlet:review<br/>or pass PR numbers])
    C --> F[triage tier per PR head SHA]
    F --> G{tier}
    G -- "TRIVIAL: human-docs only" --> H[target: 1 SATISFIED review]
    G -- "STANDARD / HIGH: any code or agent-doc" --> I[target: 2 SATISFIED reviews]
    H --> M[[event loop: gate each PR]]
    I --> M

    M --> N{required SATISFIED verdicts on current SHA?}
    N -- no --> PC{preconditions clear?<br/>Copilot done, CI not red, no conflict}
    PC -- no --> PCF[clear it first: address Copilot / fix CI / rebase] --> M
    PC -- yes --> O[run one review on HEAD SHA<br/>next only after the previous passes]
    O --> P{SATISFIED?}
    P -- no --> Q[scoped fix subagent: commit + push<br/>to the PR's own branch, new SHA]
    P -- yes --> M
    N -- yes --> R{CI status on current SHA?}
    R -- red --> S[scoped CI-fix subagent: commit + push, new SHA]
    R -- pending --> M
    Q --> T[reset gate - verdicts and CI are SHA-pinned,<br/>re-triage tier on the new SHA]
    S --> T
    T --> M
    R -- green --> U[merge: serialized, auto, squash<br/>no --delete-branch; repo auto-delete setting governs remote branch]
    U --> U2[sync local base branch to remote ff-only]
    U2 --> V[cleanup campaign-created worktree/branch only<br/>reused checkouts + branches left in place, mark merged]
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

- You can run more than one at a time in the same repo — say one gating PRs `#12 #13` and another
  gating `#20`. Each is its own isolated run with its own pull requests and bookkeeping, so they never
  step on each other; a PR already owned by one run's label won't be stolen by another. And if a run
  gets interrupted, another agent can pick it up right where it left
  off: it can tell a run that's still being actively driven from one that's been abandoned, so it only
  ever resumes an orphaned run and never doubles up on one already in progress.
- By default the reviewer is Claude's own subagents, so it runs with nothing extra installed. For a
  stronger gauntlet you can point it at a reviewer that runs a different agent/model than the
  orchestrator — Codex CLI (`codex exec`) is the recommended example, since an independent engine
  catches defects a same-model re-roll can miss. Name it when you invoke the campaign ("review with
  codex") or record it as your preferred reviewer (memory or `CLAUDE.md`). If an external reviewer
  can't return a verdict because of a system problem — quota or rate limits, auth, a timeout — it
  retries once and then falls back to its own subagents, so a transient outage slows a run down but
  doesn't stall it. A reviewer that never gets going at all — hung on input, a bad path, a sandbox
  denial — is caught the same way: every review pass has to write *something* to its progress file
  within about five minutes of being dispatched, and one that writes nothing at all is killed and
  relaunched rather than left hanging. The bar there is just "is it alive", so anything the reviewer
  writes clears it; a review that is merely slow is judged by a separate, longer timer.
- It works through GitHub PRs via the `gh` CLI, so the repo needs a GitHub remote.
- Before it spends a review on a PR, it first clears anything that would waste one: it addresses any
  GitHub Copilot review comments, fixes failing CI, and rebases a PR that has fallen into conflict
  with the base branch — then reviews the clean result.
- Some CI failures — pure formatting ones — it fixes by running the **formatter itself**, with no model
  involved at all. A tool only qualifies if its own documentation guarantees the output is *semantically
  equivalent* to the input (an AST-preserving pretty-printer, not a text munger); anything else, including
  every `--fix`-style linter and every code rewriter, goes to a full-strength fix subagent. The table is
  deliberately **short** — three tools: `gofmt`, `gci`, and `ruff format`. (`ruff format` only counts if your
  Ruff config leaves `format.docstring-code-format` off — with it on, Ruff reformats Python code *inside
  docstrings*, which changes what your strings contain. A tool's guarantee can depend on how it's configured,
  so campaign checks your config before trusting it.)

  Tools you might expect and won't find: **`goimports`**, because it *adds* missing imports and *removes*
  unreferenced ones — adding an import runs that package's `init()`, and a guessed import can be the wrong
  package; and **`gofumpt`**, because it applies extra rewrite rules on top of gofmt's layout and documents
  them as a rule list, not as semantics-preserving. Both are formatters in the colloquial sense and neither
  meets the bar, which is the point: the bar is the tool's own documentation, not the vibe of its diff.

  You don't configure that list in a file. **Name the formatters when you invoke campaign** ("use gofmt and
  gci", or "no formatters" to switch the shortcut off entirely), or **record a preference in memory**
  and campaign will pick it up on later runs. Say nothing and you get the built-in default set. Whatever it
  resolves to is fixed once, at the start of the run, and written into the run's ledger `formatters` field —
  `default` for the built-in set, `-` for the shortcut switched off, otherwise the tool ids you named — so a
  later wake, or a fresh agent that picks the run up, uses the same list rather than quietly reverting to the
  default.

  There is deliberately **no config file for this, and campaign will not read one from your repo** — not a
  file at the repo root, not `CLAUDE.md`, not anything else in the tree. Files in your repo are things a pull
  request can edit, and this list is what decides whether a change gets committed to that same pull request
  *without a review pass*. If it lived in the repo, a pull request could widen the rules that govern its own
  review. Keeping it out of the repo makes that impossible by construction, rather than something campaign
  has to defend against.

  Naming a tool is all you get to do. You do **not** supply a command, flags, or an argv — campaign owns the
  exact command line for each tool it knows (`gofmt -w --`, `gci write --`, `ruff format --`).
  Flags are not cosmetic: `gofmt -w -r 'true -> false'` is still `gofmt`, but the `-r` flag turns it into a
  rewrite engine that changes `return true` into `return false`. Checking *which tool* runs is not enough if
  you also get to pick *how* it runs, so campaign doesn't let you pick.

  Campaign also picks the **binary**, not just the command line. The tool runs inside the pull request's own
  worktree, and that pull request is untrusted content, so campaign resolves the executable to an absolute
  path outside your repo before running it — a pull request that ships a file called `gofmt` never gets
  executed. If the real tool can't be found outside the repo, campaign refuses the shortcut and uses a model
  instead.

  And it is careful about the **filenames** it hands the tool, because those come out of the pull request
  too. A pull request can add a file called `-cpuprofile=prof.go`; it is a perfectly good match for `**/*.go`,
  and `gofmt -w '-cpuprofile=prof.go' a.go` doesn't format it — it reads it as a *flag* and writes a CPU
  profile. So campaign always passes `--` before the file list, always passes absolute paths rather than bare
  names, and drops any candidate file whose name starts with `-` (it logs it and carries on with the rest).
  Campaign owns the *shape* of the command; the filenames in it are pull-request data, and get treated as
  data.

  Spelling a path safely is not the same as knowing where it *leads*. A pull request can also add `link.go` —
  a **symlink** whose target sits outside the worktree entirely. The name looks fine, but `gofmt -w -- link.go`
  follows it and rewrites the file it points at. So campaign checks what each candidate resolves to, not just
  how it is written: it drops symlinks, drops anything that isn't a plain regular file, and drops anything
  whose fully-resolved real path lands outside the worktree. Each drop is logged; the rest of the run
  continues.

  Every known tool has a default glob (`gofmt` → `**/*.go`, `ruff format` → `**/*.py`), so you normally name
  nothing but the tool. If you do narrow one to a subdirectory, the glob may only **narrow** the default,
  never widen it — and you should not try to write exclusions into it. Campaign applies its **own** exclusion
  filter afterwards, every time, and nothing widens it: tests, check definitions, CI workflows, and tool
  config (`**/*_test.go`, `.github/**`, `.golangci.yml`, `pyproject.toml`, …) are removed no matter what
  your glob says. The glob picks the candidates; campaign's filter protects the files that gate the review.
  An exclusion list *you* maintain would eventually miss one, and campaign commits this tool's output without
  a review pass — it must never be able to weaken the checks that gate it. (A glob that *directly* names a
  protected path is refused outright.)

  Teaching campaign a genuinely new tool is a change to the skill — reviewed and gated like any other code
  change. And to be clear about what all this buys: the tool list comes from *you*, so the denylist and the
  narrow shape of the list are a **guard against footguns**, not a security boundary against yourself. The
  real boundary is that the tool runs on untrusted pull-request content, which is why campaign resolves the
  binary outside your repo and owns the exclusion filter itself.
- It keeps a small `.gauntlet/history/` at the repo root (git-ignored, one file per run) to remember what past
  runs learned. That's the memory a fresh run carries over. Each fresh run also tidies that file,
  dropping entries that no longer apply to the current code — and when it isn't sure an entry is
  safe to drop, it asks you first rather than guessing.
- Full mechanics live in [`SKILL.md`](./SKILL.md) and [`references/`](./references/).
