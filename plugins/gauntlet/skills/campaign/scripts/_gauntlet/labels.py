"""The campaign's GitHub label VOCABULARY — the ONE owner of every label name the gauntlet writes.

Three tools write labels (`label-mirror.py` reconciles them, `pr-adopt.py` applies them at adoption,
`reconcile.py` reports them), and each of them used to carry its own copy of the two names. That was
survivable while the vocabulary was two fixed strings. It is not survivable now: a reviewing label
carries its own TALLY (`gauntlet-reviewing 1/2`), so the name is COMPUTED, and a second copy of the
computation is a second answer to "which label does this row wear".

TWO INDEPENDENT AXES, and keeping them independent is the point:

* the **GATE** axis — exactly one of `gauntlet-accepted` / `gauntlet-reviewing <ok>/<required>`, a
  projection of `reviews_ok` against `required(tier)` (`stage-2-review-gate.md`, "Status labels mirror
  the review gate");
* the **BASE-CURRENCY** axis — `gauntlet-rebase-pending`, present exactly while the row records that its
  head no longer contains its base. It says only that a rebase is OWED, never that the gate moved, so a
  PR can wear `gauntlet-accepted` and `gauntlet-rebase-pending` at once — that pair is the normal state
  of an accepted PR waiting its turn in the serialized drain (`stage-3-merge.md`, "Step 6").

Nothing here decides anything and nothing here talks to GitHub. It computes NAMES and ARGUMENT VECTORS;
each caller runs them through its own `gh` seam, because which door a tool exits through is that tool's
business (the stance `repository.py` takes).

`required(tier)` is NOT defined here. It belongs to the review gate, and its callers already hold the
owner they trust (`nudge.required` / `review-pass.required_reviews`); every function below takes the
count as an ARGUMENT so this module can never become a third opinion about it.
"""

from __future__ import annotations

# --- the run-ownership axis, which this module NEVER sweeps --------------------
#
# `gauntlet-run-<id>` is ADOPTION's business (`pr-adoption.md`) and `gauntlet-authored` is the review
# handoff's. They are named here only so a reader sees the whole vocabulary in one place, and so
# `is_status_label` can be read against the complete list of what it deliberately EXCLUDES. No function
# in this module ever adds or removes either one.
RUN_PREFIX = "gauntlet-run-"
RUN_COLOR = "5319E7"
AUTHORED = "gauntlet-authored"

# --- the gate axis -------------------------------------------------------------
ACCEPTED = "gauntlet-accepted"
ACCEPTED_COLOR = "0E8A16"
ACCEPTED_DESCRIPTION = "gauntlet: passed its reviews"

# The STEM of every reviewing label. A live reviewing label is this stem plus a space and the tally
# (`gauntlet-reviewing 1/2`); the BARE stem is the LEGACY name every PR wore before the tally existed,
# and it is still recognised as a gate label so a reconcile sweeps it off rather than leaving two.
REVIEWING = "gauntlet-reviewing"
REVIEWING_COLOR = "FBCA04"

# --- the base-currency axis ----------------------------------------------------
REBASE_PENDING = "gauntlet-rebase-pending"
REBASE_PENDING_COLOR = "1D76DB"
REBASE_PENDING_DESCRIPTION = "gauntlet: behind its base; rebases when it reaches the merge front"


def gate_label(reviews_ok: int, required: int) -> str:
    """The ONE gate label a row wears. PURE.

    `gauntlet-accepted` once the tally meets the floor, else `gauntlet-reviewing <ok>/<required>`. The
    tally is spelled into the name so the ONE piece of run state a human reads on GitHub says how far
    along a PR is, not merely that it is not done.
    """
    if reviews_ok >= required:
        return ACCEPTED
    return f"{REVIEWING} {reviews_ok}/{required}"


def is_status_label(name: str) -> bool:
    """Is `name` a label THIS machinery owns and may therefore remove? PURE.

    True for the gate axis (`gauntlet-accepted`, the legacy bare `gauntlet-reviewing`, and any tallied
    `gauntlet-reviewing <n>/<m>`) and for the base-currency axis (`gauntlet-rebase-pending`).

    FALSE for `gauntlet-run-<id>` and `gauntlet-authored`, which are other owners' state: a sweep that
    took the run label off would drop the PR out of its own run, and one that took `gauntlet-authored`
    off would rewrite the PR's recorded provenance. The exclusion is the load-bearing half of this
    predicate, which is why both names are defined above rather than left implicit.
    """
    return name in (ACCEPTED, REVIEWING, REBASE_PENDING) or name.startswith(REVIEWING + " ")


def desired_labels(reviews_ok: int, required: int, *, rebase_pending: bool) -> list[str]:
    """Every status label the row SHOULD wear, gate axis first. PURE."""
    labels = [gate_label(reviews_ok, required)]
    if rebase_pending:
        labels.append(REBASE_PENDING)
    return labels


def reconcile(current: "list[str]", desired: "list[str]") -> "tuple[list[str], list[str]]":
    """`(to_add, to_remove)` for one PR, given its LIVE labels and the labels it should wear. PURE.

    `to_add` is every desired label not already present. `to_remove` is every label this machinery owns
    (`is_status_label`) that is present and not desired — which is what sweeps a stale tally, a stale
    `gauntlet-accepted`, and the legacy bare `gauntlet-reviewing` off in one pass. A label this machinery
    does not own is never in either list.

    Both lists empty means the labels are already right, and the caller makes no GitHub call at all.
    Order is stable (desired order for adds, live order for removes) so a caller's argv is a FUNCTION of
    its inputs and a fixture can pin it.
    """
    have = set(current)
    to_add = [name for name in desired if name not in have]
    want = set(desired)
    to_remove = [name for name in current if is_status_label(name) and name not in want]
    return to_add, to_remove


def describe(name: str) -> "tuple[str, str]":
    """`(color, description)` for a label this machinery creates. PURE.

    Every label is created — never merely added — because a tallied name is COMPUTED, so the set of
    reviewing labels a repository needs depends on the tiers its PRs get triaged to. A bootstrap list
    typed into a doc would go stale the first time `required(tier)` grew a value; creating on demand
    cannot. Callers pass the result to `create_argv`.

    An unrecognised name raises `KeyError` rather than defaulting: a label whose spelling this module
    does not recognise is one it has no business creating in the repository.
    """
    if name == ACCEPTED:
        return ACCEPTED_COLOR, ACCEPTED_DESCRIPTION
    if name == REBASE_PENDING:
        return REBASE_PENDING_COLOR, REBASE_PENDING_DESCRIPTION
    if name == REVIEWING or name.startswith(REVIEWING + " "):
        tally = name[len(REVIEWING):].strip()
        detail = f" ({tally} verdicts)" if tally else ""
        return REVIEWING_COLOR, f"gauntlet: under review{detail}"
    raise KeyError(name)


def create_argv(name: str, repo: "str | None" = None) -> list[str]:
    """The idempotent `gh label create … --force` argv for `name`. PURE.

    `--force` creates the label or updates it in place, so running this before every add is safe on every
    resume — the same idempotent-create idiom `pr-adoption.md` already uses for the run-owner label.
    """
    color, description = describe(name)
    argv = ["gh", "label", "create", name, "--color", color, "--description", description, "--force"]
    if repo:
        argv += ["--repo", repo]
    return argv


def edit_argv(pr: str, to_add: "list[str]", to_remove: "list[str]",
              repo: "str | None" = None) -> list[str]:
    """The ONE `gh pr edit` argv that applies a reconcile. PURE.

    Only labels that actually need to move appear: a `--remove-label` is emitted for a label the PR
    genuinely carries, never speculatively. Returns the bare `gh pr edit` prefix when there is nothing to
    do, which no caller should run — check `to_add`/`to_remove` first.
    """
    argv = ["gh", "pr", "edit", str(pr)]
    if repo:
        argv += ["--repo", repo]
    for name in to_add:
        argv += ["--add-label", name]
    for name in to_remove:
        argv += ["--remove-label", name]
    return argv
