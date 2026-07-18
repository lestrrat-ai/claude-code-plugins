## Run-owned operation scope

Invoking this skill authorizes the git/GitHub operations it performs on the branches/PRs it **adopts**
(existing PRs the run adopted and tagged with its owner label): `add`, `commit`, `push`, PR update,
labels/checks/comments, and merge. The campaign does not invent work: it opens a PR of its own only for a
**follow-up it has TAKEN UP** under the autonomy threshold (`followups.md` — a corroborated claim, every
ACT condition evidenced, and the resulting PR gated like any other); otherwise PR creation lives in the
gauntlet:review handoff. **Passing a `#PR` (or discovering one under this run's label) authorizes the
campaign to gate and merge that PR** — adopt it, run the review gauntlet on it, push review/CI fixes to
its branch, and merge it when the gate and CI are green.

Do NOT ask again before run-owned operations when the state machine reaches them.

Scope does NOT cover unrelated branches/PRs, destructive git operations, force-push/reset, or
cross-run work. Worktree removal still goes through merged-branch verification in Stage 3.

## Constraints

**Public API changes require user confirmation — on by default.** A fix may not modify the project's
public API *surface or its observable behavior* without the user's say-so:

- *Surface* — exported functions, types, and methods and their signatures; public constants/enums;
  serialized formats and wire/HTTP contracts; CLI flags; config keys.
- *Behavior* — the observable contract of the above (return/error semantics, defaults, output shape)
  even when the signature is unchanged.

Internal-only changes that leave both identical need no confirmation.

Handling depends on the run's `api_changes` flag, stored in the ledger header:

- **`ask` (default)** — when a fix would cross the line, do NOT make the change. Park that PR
  (status `awaiting-api`, `api_approval: -`), show the user the proposed change and what it would
  break, and ask whether to proceed. Keep working the other PRs meanwhile. On approval, apply it
  and set `api_approval: approved@<iso>` (the PR resumes normal gating); if the user declines,
  set `api_approval: declined@<iso>`, set the PR aside as skipped (status `aborted`), and report
  it. Both decisions are durable: before re-asking on a later heartbeat, check `api_approval` — a PR
  already approved or declined is settled, never re-ask it.
- **`allowed`** — proceed without asking. Set this *only* when the user, at invocation, explicitly
  said API breakage is acceptable (e.g. "allow API changes" / "ignore breakage").

**Store the flag in the ledger and re-consult it every heartbeat.** Derive the `api_changes` header field
(`ask` | `allowed`) once from the invocation and record it via `ledger.py --file <state.jsonl> header
set api_changes <ask|allowed>`. A run is long, so NEVER trust in-context
memory for this — re-read the flag from the ledger before any API-affecting change, so the behavior
can't drift mid-run. A blanket "yes, stop asking" from the user flips the header to `allowed`; a
one-off "yes" approves only that PR (recorded durably in its `api_approval`) and leaves the
flag at `ask`.

Backstop: when you scan a PR you built or adopted, flag any public-API change in its diff. Under `ask`, an
unapproved API change must not merge — revert it or get approval first (grounds for `NOT SATISFIED`).
