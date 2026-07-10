## Run-owned operation scope

Invoking this skill authorizes the git/GitHub operations it performs on branches/PRs it creates:
`add`, `commit`, `push`, PR create/update, labels/checks/comments, and merge.

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

- **`ask` (default)** — when a fix would cross the line, do NOT make the change. Park that finding
  (status `awaiting-api`, `api_approval: -`), show the user the proposed change and what it would
  break, and ask whether to proceed. Keep working the other findings meanwhile. On approval, apply it
  and set `api_approval: approved@<iso>` (the finding resumes normal fanout); if the user declines,
  set `api_approval: declined@<iso>`, set the finding aside as skipped (status `aborted`), and report
  it. Both decisions are durable: before re-asking on a later wake, check `api_approval` — a finding
  already approved or declined is settled, never re-ask it.
- **`allowed`** — proceed without asking. Set this *only* when the user, at invocation, explicitly
  said API breakage is acceptable (e.g. "allow API changes" / "ignore breakage").

**Store the flag in the ledger and re-consult it every wake.** Derive `api_changes: ask|allowed` once
from the invocation and record it in the ledger header. A run is long, so NEVER trust in-context
memory for this — re-read the flag from the ledger before any API-affecting change, so the behavior
can't drift mid-run. A blanket "yes, stop asking" from the user flips the header to `allowed`; a
one-off "yes" approves only that finding (recorded durably in its `api_approval`) and leaves the
flag at `ask`.

Backstop: when you scan a PR you built, flag any public-API change in its diff. Under `ask`, an
unapproved API change must not merge — revert it or get approval first (grounds for `NOT SATISFIED`).
