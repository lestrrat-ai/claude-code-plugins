# Review

Part of the [gauntlet](../../README.md) plugin.

A hostile, skeptical, two-pass code review. The first pass is adversarial: it treats the code as a
suspect, assumes the worst, and surfaces everything that looks wrong. The second pass is a neutral
audit — by default run in a fresh, separate context — that re-examines each of those findings and
**confirms, adjusts, or refutes** it.
So what you get back isn't a pile of raw complaints — it's a triaged report where the noise has
already been separated from the real problems.

It reviews through three lenses — **security**, **API consistency & symmetry**, and **user
experience** — applied explicitly and separately, because each catches a different class of defect.
And by default it **only reports**: it changes no code and touches nothing on GitHub. You decide what
to do with the findings.

## What it's good for

- Getting a genuinely skeptical review when you want the worst found — not a friendly rubber-stamp
  that tells you the code looks fine.
- Separating real issues from noise: the confirm/adjust/refute second pass triages the hostile pass's
  overclaims so you spend your time on defects that are actually reachable.
- Reviewing anything from a single file or one PR up to a whole codebase — it sizes the depth of the
  review to the target rather than forcing you to narrow it down.
- Feeding confirmed findings straight into [`gauntlet:campaign`](../campaign/README.md) to get them
  fixed and merged, via the opt-in handoff below.

## How to use it

Point it at what you want reviewed — a pull request, a branch, a directory, a single file, or the
whole repository — and it works out how deep to go from the size of the target. A single file gets a
thorough one-pass read; a large package fans the work out across the three lenses; a whole-repo audit
is ranked by risk so the hottest surfaces (untrusted input, auth, crypto, secrets) get the most
rigor and nothing is quietly dropped without being noted.

Reach for it when you specifically want a hard, hostile review — the kind that assumes every input is
malicious and every assumption is wrong — rather than a quick sanity check.

## What to expect

A final report that groups every finding by what the audit concluded, not by lens:

- **Confirmed** — the defect is real, the trigger is reachable, and the severity holds up.
- **Adjusted** — the defect is real but something about it was off: severity too high, trigger
  narrower than claimed, or the proposed fix wrong. The correction is stated.
- **Refuted** — the finding was wrong. It says which validator, guarantee, or invariant rules it out,
  with a code reference. Refuted findings stay in the report on purpose, so you can see what was
  considered and why it was ruled out.
- **Uncertain** — it couldn't decide from the code alone, and it says what information would settle
  it.

The report closes with a per-verdict and per-severity summary and a short list of the
highest-leverage fixes if your time is limited, plus an explicit note of anything neither pass
examined.

The default is strictly report-only. It writes no code, opens no pull requests, and changes nothing
on GitHub. The only thing it may write is ephemeral scratch under `.gauntlet/tmp`. Nothing leaves
that boundary unless you opt into the handoff below.

## From findings to merged fixes: the campaign handoff

After it delivers the report, it asks once — and only once — whether to open a pull request per
confirmed fix and run them through the gauntlet.

Say **no** (or say nothing) and it stops there: report-only, no side effects. Say **yes** and it
implements each Confirmed or Adjusted fix — exactly the change the finding describes, nothing more —
on its own branch, opens one pull request per finding, and hands those PRs to
[`/gauntlet:campaign`](../campaign/README.md). Campaign takes it from there: it adopts the PRs, gates
each one through the reviews its tier requires plus CI, and merges. Refuted and Uncertain findings
are left alone.

So the usual progression is **review to find and confirm, then campaign to gate and merge** — but the
review half never crosses into changing anything until you explicitly say yes.

## Good to know

- The two passes run separately: the neutral audit defaults to a fresh, separate context so it isn't
  anchored by the hostile pass's conclusions (it falls back to a same-context audit only when a fresh
  subagent isn't available). It re-reads the cited code with intent to disprove each finding rather than
  rubber-stamp it. This second pass is mandatory; a single hostile pass on its own is not this skill.
- Any GitHub work in the handoff goes through the `gh` CLI, so that path needs a GitHub remote.
- Full mechanics live in [`SKILL.md`](./SKILL.md).
