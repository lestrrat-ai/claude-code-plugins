#!/usr/bin/env python3
"""Adopt a PR into a run — the MECHANICAL half, as a command instead of a shell block in a doc.

`references/pr-adoption.md` is the authority; this tool performs its steps 1, 2, 4, 5 and the row of
step 3. It does NOT decide the review TIER: adoption passes a conservative `STANDARD` bootstrap because the
PR-head worktree does not exist until this command resolves it, then — after step 5 — `triage.py derive`
supplies the mechanical floor and the orchestrator decides the tier at or above it before gate work. It does
NOT author the PR's INTENT (step 3a — the driver's working note about what the PR
is for): the driver decides Purpose, PR-specific Non-goals, Threat model and provenance. What this tool
DOES do for intent is MECHANICAL — `intent-sync` folds the operator's run-wide default Non-goals (the
ledger header `default_non_goals`) into the PR's `intent-<pr>.md` managed block, idempotently, deciding no
Non-goal text of its own. Everything here is MECHANICS that a model transcribing a doc gets subtly wrong
under load:

  * READ the PR (one `gh pr view` for the fields the ledger row needs, including the cross-repo field);
  * REFUSE fork/foreign/closed PRs — FAIL CLOSED, touching nothing when it refuses (step 2);
  * REGISTER the ledger row (refuse an existing terminal row; otherwise refresh in place if it exists,
    never a duplicate `add-row`) (step 3, row);
  * RESOLVE the PR-head worktree — DISCOVER the branch's actual checkout via `git worktree list` and REUSE
    it (fast-forwarded, clean), else CREATE one at the default path; fail closed on a dirty/detached/foreign
    checkout or a head-SHA mismatch (step 5);
  * LABEL the PR ours + under review (step 4).

The decision logic lives in a PURE `build_plan()` (and a pure `slugify()`), so every refusal and every
computed row field is pinned by an offline fixture with no live GitHub — the sibling `pr-adopt-test.py` is
this tool's executable contract. `adopt` is the thin executor that runs the real `gh`/`git`/ledger
commands around that plan.

  pr-adopt.py plan  --view-json <f> --run-id <id> --tier <T>   # PURE: parse a view, print the plan, exit 0
  pr-adopt.py adopt --pr <N> --run-id <id> --file <state.jsonl> --tier <T> \
                    --worktrees-root <p> --project-root <p>    # the real thing: gh + git + ledger
  pr-adopt.py intent-sync --file <state.jsonl> --pr <N>        # fold the run's default Non-goals into the
                                                              # PR's intent managed block (idempotent)

pr-adopt performs NO review and NO merge. It gets a PR INTO the run; Loop control drives it from there.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from _gauntlet.atomic import replace_text
from _gauntlet.modules import load_module_from_path

DESCRIPTION = next(iter((__doc__ or "").splitlines()), "")

_HERE = Path(__file__).resolve().parent
SIBLING = _HERE / "pr-adopt-test.py"
LEDGER_PY = _HERE / "ledger.py"

# The run owner label's prefix and the two MUTUALLY EXCLUSIVE status labels. The owner label's colour
# matches the one pr-adoption.md's `gh label create` uses, so an idempotent `--force` create never churns
# it. A PR carries exactly ONE status label, mirroring the live review gate (pr-adoption.md, step 4).
RUN_LABEL_PREFIX = "gauntlet-run-"
REVIEWING_LABEL = "gauntlet-reviewing"
ACCEPTED_LABEL = "gauntlet-accepted"
RUN_LABEL_COLOR = "5319E7"

# The label that marks a PR as authored by this pipeline (gauntlet:review's handoff applies it to every PR
# it opens). It is the ONLY thing that makes `pr_origin` = `gauntlet`; a driver cannot assert it by flag.
GAUNTLET_AUTHORED_LABEL = "gauntlet-authored"

# The EXACT durable reason recorded in `ci_reason` when a re-adoption sees the PR's live base diverge from
# its recorded base. The campaign does NOT migrate a row to a new base; the mismatch is an unsupported user
# change that PARKS the row through the existing machine-blocker transition (design:
# campaign-mixed-base-branches.md, "Unsupported base changes park the row"). The reconcile→loop-control
# routing parks with this SAME wording built from the `base_changed` fact, so the string is a shared
# contract — search `not supported mid-run` to reconcile every site. `{recorded}` is the row's
# `effective_base`, `{live}` the live `baseRefName`.
BASE_CHANGE_PARK_REASON = "base changed from {recorded} to {live}; not supported mid-run"


def _load(name: str, filename: str):
    mod = load_module_from_path(name, _HERE / filename)
    if mod is None:
        raise RuntimeError(f"cannot load {filename}")
    return mod


L = _load("pr_adopt_ledger", "ledger.py")
# `required(tier)` — the review gate's SATISFIED-verdict floor — is OWNED by review-pass.py
# (`required_reviews`); the status label mirrors that gate, so reuse the owner rather than retype the rule.
RP = _load("pr_adopt_review_pass", "review-pass.py")

# THE LIVENESS COUNTERS — RE-EXPORTED from ledger.py, never a second copy of the list. A re-adoption at a
# moved head no longer hand-resets them: the ledger `set --head-sha` door does it (ledger.py's
# `apply_head_sha`; stage-2-ci.md, "THE LIVENESS COUNTERS"). This name survives only for readers/tests that
# ask pr-adopt which fields the set is.
LIVENESS_COUNTERS = L.LIVENESS_COUNTERS


# --- pure decision surface ----------------------------------------------------

def slugify(title: str) -> str:
    """A filesystem-safe slug: lowercase, every run of non-alphanumerics collapsed to one `-`, no leading
    or trailing dash. An all-punctuation title slugs to the empty string, which is a fine `-`-free value."""
    out: list[str] = []
    prev_dash = False
    for ch in title.lower():
        if ch.isalnum() and ch.isascii():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-")


def worktree_for_branch(porcelain_z: str, branch_ref: str) -> "str | None":
    """Resolve the worktree that has `branch_ref` (e.g. `refs/heads/feat-x`) checked out, from the output of
    `git worktree list --porcelain -z` — the ONE discovery chokepoint (pr-adoption.md step 5,
    `parse_nul_porcelain_for_exact_branch`). Returns that worktree's path, or None when the branch is checked
    out NOWHERE (so a fresh worktree must be created).

    Only an exact `branch refs/heads/<name>` entry matches. A worktree whose HEAD is DETACHED, or on a
    DIFFERENT branch, returns None for this branch — which is why a detached or foreign checkout sitting at
    the default path is never mistaken for the branch (its later head-branch pushes would fail).

    The `-z` porcelain format terminates every field with NUL and ends each worktree entry with an extra
    empty field; splitting on NUL and resetting the current path on the empty field walks entries safely."""
    current: "str | None" = None
    for token in porcelain_z.split("\0"):
        if token == "":
            current = None
            continue
        label, _, value = token.partition(" ")
        if label == "worktree":
            current = value
        elif label == "branch" and value == branch_ref:
            return current
    return None


def _pr_origin(view: dict) -> str:
    """WHO WROTE THIS PR — DERIVED from the PR's own labels, never a caller flag (pr-adoption.md, step 3).

    `gauntlet` ONLY when the PR carries the `gauntlet-authored` label, which gauntlet:review's handoff
    applies to every PR it opens; `external` for everything else — the SAFE default. This is a security
    boundary: `pr_origin = gauntlet` unlocks repair-pass's branch-content-rewriting decisions
    (`repair-pass.md`, "The ownership guardrail"), so a driver must NOT be able to claim it on a PR it did
    not open. Reading it from the label the pipeline itself applies removes that door entirely.
    """
    for lbl in view.get("labels") or []:
        name = lbl.get("name") if isinstance(lbl, dict) else lbl
        if name == GAUNTLET_AUTHORED_LABEL:
            return "gauntlet"
    return "external"


def _fork_ref(view: dict) -> str:
    """`<owner>/<repo>` for the fork a cross-repo PR's head lives in, read defensively from the two
    objects `gh pr view` returns (each an object with `login`/`name`, or already a bare string)."""
    owner = view.get("headRepositoryOwner")
    repo = view.get("headRepository")
    owner_s = owner.get("login") if isinstance(owner, dict) else owner
    repo_s = repo.get("name") if isinstance(repo, dict) else repo
    return f"{owner_s}/{repo_s}"


def build_plan(view: dict, *, run_id: str, tier: str, worktrees_root: str) -> dict:
    """Decide adoptability and compute the row — PURE, from a parsed `gh pr view` dict.

    Returns `{"verdict": "refuse", "reason": ...}` for a PR that must NOT be adopted (pr-adoption.md
    step 2), else `{"verdict": "adopt", "row": {...computed fields...}, "labels_add": [...], "branch": ...,
    "worktree": ..., "base": ...}`. FAIL CLOSED: every refusal is checked before any adopt field is built.
    """
    # A fork PR is untrusted, attacker-controllable content this autonomous pipeline would read and act on,
    # and it has no push target for fix commits — campaign gates SAME-REPO PRs only (step 2).
    if view.get("isCrossRepository") is True:
        return {"verdict": "refuse",
                "reason": f"fork PR (head in {_fork_ref(view)}) — campaign gates same-repo PRs only; "
                          f"push a same-repo branch"}

    # A `gauntlet-run-*` label that is not OURS means another run owns this PR — never steal its label.
    ours = f"{RUN_LABEL_PREFIX}{run_id}"
    for lbl in view.get("labels") or []:
        name = lbl.get("name") if isinstance(lbl, dict) else lbl
        if isinstance(name, str) and name.startswith(RUN_LABEL_PREFIX) and name != ours:
            return {"verdict": "refuse", "reason": f"owned by another run ({name})"}

    # Campaign gates OPEN PRs; a merged/closed PR is terminal, not adoptable.
    state = view.get("state")
    if state != "OPEN":
        return {"verdict": "refuse", "reason": f"PR is {state}, not open"}

    branch = view["headRefName"]
    # COMPUTED fields only. The row's `base_branch` is TOOL-OWNED and CREATION-ONLY, so it is NOT in this
    # plan row: `baseRefName` rides the plan under `base`, and the EXECUTOR records it once via `add-row
    # --base-branch` when it creates the row (never here, and never rewritten on a re-adoption).
    # `worktree_owned`/`branch_owned` are decided at worktree creation (step 5), never here.
    row = {
        "head_sha": view["headRefOid"],
        "tier": tier,
        "ci": "pending",
        "status": "in_review",
        "reviews_ok": "0",
        "pr_origin": _pr_origin(view),
        "slug": slugify(str(view.get("title", ""))),
    }
    return {
        "verdict": "adopt",
        "row": row,
        "labels_add": [ours, REVIEWING_LABEL],
        "branch": branch,
        "worktree": str(Path(worktrees_root) / branch),
        "base": view["baseRefName"],
    }


# --- executor -----------------------------------------------------------------

def _run(argv: list[str], *, cwd: "str | None" = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False, cwd=cwd)  # noqa: S603


def _refuse(reason: str) -> int:
    print(f"pr-adopt: REFUSED — {reason}", file=sys.stderr)
    return 1


def cmd_plan(args) -> int:
    """The TESTABLE surface: read a parsed view from disk, print the plan, exit 0 (refuse or adopt alike).
    No `gh`, no `git`, no ledger — pure `build_plan` over a file."""
    view = json.loads(Path(args.view_json).read_text(encoding="utf-8"))
    plan = build_plan(view, run_id=args.run_id, tier=args.tier, worktrees_root=args.worktrees_root)
    print(json.dumps(plan))
    return 0


def _park_base_mismatch(args, pr: str, recorded: str, live: str) -> int:
    """A re-adoption saw the PR's live base diverge from its recorded base. PARK the row on the user through
    the existing machine-blocker transition (`ledger.py park`) with the design's exact reason, and STOP —
    no refresh, no relabel, no base rewrite. The park is the ONE mechanism; this reuses it rather than adding
    a transition. An ALREADY-held row keeps its open question: `park` returns `EXIT_STOP` and leaves the
    existing `ci_reason` untouched (design: "An already-held row keeps its open question")."""
    reason = BASE_CHANGE_PARK_REASON.format(recorded=recorded, live=live)
    park_argv = ["python3", str(LEDGER_PY), "--file", args.file, "park", "--pr", pr, "--reason", reason]
    proc = _run(park_argv, cwd=args.project_root)
    mismatch = {"recorded": recorded, "live": live}
    if proc.returncode == L.EXIT_STOP:
        # Already awaiting-user — a park is open and its question is preserved. Nothing to do but report it.
        print(json.dumps({"pr": pr, "run_id": args.run_id, "parked": False,
                          "already_held": True, "base_mismatch": mismatch}))
        return 0
    if proc.returncode != 0:
        # A terminal row (or other park refusal) — parking does not apply; surface it fail-closed.
        return _refuse(f"PR {pr} base changed (recorded {recorded}, live {live}) but the row could NOT be "
                       f"parked: {proc.stderr.strip()}")
    print(json.dumps({"pr": pr, "run_id": args.run_id, "parked": True,
                      "reason": reason, "base_mismatch": mismatch}))
    return 0


def intent_path(ledger_path: str, pr: str) -> Path:
    """The PR's intent artifact, DERIVED as the sibling of the ledger — never an arbitrary output path. The
    intent lives beside `state.jsonl` in the run dir, and `review-pass.py` owns the `intent-<pr>.md` name;
    reuse its deriver so the two never disagree about where the file is."""
    return RP.intent_path(Path(ledger_path).parent, str(pr))


def sync_intent_defaults(ledger_path: str, pr: str) -> str:
    """Fold the run's default Non-goals into the PR's intent managed block — MECHANICAL, idempotent, atomic.

    Returns `updated` (bytes changed), `unchanged` (already in sync), or `pending-intent` (no intent artifact
    yet — a fresh adoption authors it first, then re-runs this). REFUSES a missing ledger row or an intent
    that will not parse: this tool applies the operator's defaults, it never invents an intent. The defaults
    are read ONLY through `L.default_non_goals`, the managed block is rewritten ONLY through
    `RP.merge_default_non_goals`, and the result is re-validated before it replaces the file.
    """
    header, rows = L.load(Path(ledger_path))
    if L.find_row(rows, str(pr)) is None:
        raise ValueError(f"no ledger row for PR {pr} — adopt it before syncing its intent")
    path = intent_path(ledger_path, pr)
    if not path.exists():
        return "pending-intent"
    try:
        defaults = L.default_non_goals(header)
    except ValueError as exc:
        raise ValueError(f"ledger header `default_non_goals` is malformed ({exc}); repair it before syncing")
    text = path.read_text(encoding="utf-8")
    # Refuse to fold defaults into an intent that is not a usable intent block — a broken base is the
    # driver's to fix (re-author), not this tool's to paper over.
    try:
        RP.parse_intent(text, path)
    except RP.Defect as exc:
        raise ValueError(f"{path.name} is not a usable intent block ({exc}); re-author it before syncing")
    merged = RP.merge_default_non_goals(text, defaults, path)
    # Re-validate the RESULT: the fold must itself produce a usable, in-sync intent, or it does not land.
    RP.parse_intent(merged, path)
    RP.check_default_non_goals(merged, defaults, path)
    if merged == text:
        return "unchanged"
    replace_text(path, merged, temp_prefix=f".{path.name}.", encoding="utf-8")
    return "updated"


def cmd_intent_sync(args) -> int:
    """`intent-sync --file <state.jsonl> --pr <N>` — fold the run defaults into ONE PR's intent block."""
    try:
        result = sync_intent_defaults(args.file, str(args.pr))
    except ValueError as exc:
        return _refuse(str(exc))
    print(json.dumps({"pr": str(args.pr), "intent_sync": result}))
    return 0


def cmd_adopt(args) -> int:
    pr = str(args.pr)
    run_label = f"{RUN_LABEL_PREFIX}{args.run_id}"
    # gh resolves its target repo from the CWD when `--repo` is absent, but git and the ledger run in
    # `--project-root`. Left in the invoking checkout, gh would label a DIFFERENT repo than the one the
    # ledger tracks. So scope every gh call to project-root; with `--repo` the flag wins and cwd is moot.
    gh_cwd = args.project_root

    # 1. Read the PR — METADATA ONLY. `body` is deliberately NOT requested: on a fork PR it is
    # attacker-controlled content, and adoption's decision never needs it, so it is never fetched or parsed.
    view_argv = ["gh", "pr", "view", pr]
    if args.repo:
        view_argv += ["--repo", args.repo]
    view_argv += ["--json", "number,title,headRefName,headRefOid,baseRefName,labels,state,"
                            "isCrossRepository,headRepositoryOwner,headRepository"]
    proc = _run(view_argv, cwd=gh_cwd)
    if proc.returncode != 0:
        return _refuse(f"`gh pr view {pr}` exited {proc.returncode}: {proc.stderr.strip()}")
    try:
        view = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return _refuse(f"`gh pr view {pr}` output is not JSON ({exc})")

    # 2. Decide. On refuse, touch NOTHING — no label, no row, no worktree.
    plan = build_plan(view, run_id=args.run_id, tier=args.tier, worktrees_root=args.worktrees_root)
    if plan["verdict"] == "refuse":
        return _refuse(str(plan["reason"]))

    branch = str(plan["branch"])
    base = str(plan["base"])
    row = plan["row"]
    planned_head = str(row["head_sha"])

    # Load the ledger ONCE for the re-adoption checks here and the row refresh in step 4.
    header, rows = L.load(Path(args.file))
    existing = L.find_row(rows, pr)

    # TERMINAL ROW GATE — runs before every label, ledger and worktree mutation. A terminal row records
    # that this run already finished or gave up on the PR; an OPEN PR with labels removed is not permission
    # to revive it. Re-adoption applies only to new or non-terminal rows.
    if existing is not None and existing.get("status") in L.TERMINAL_STATUSES:
        status = existing["status"]
        return _refuse(f"PR {pr} already has terminal ledger status {status}; terminal rows are not "
                       f"re-adoptable")

    # RE-ADOPTION BASE GATE — runs BEFORE any write. The recorded row base is IMMUTABLE (ledger CREATE_ONLY),
    # and the campaign never migrates a row to a new base. If the PR's live base no longer matches the row's
    # `effective_base` (its explicit base, else the legacy header), PARK the row on the user through the
    # existing machine-blocker transition and STOP — refresh no evidence, rewrite no base, apply no label
    # (design: "Unsupported base changes park the row"). A NEW row (existing is None) has no recorded base to
    # diverge from; it records the live base in step 4.
    if existing is not None:
        recorded_base = L.effective_base(header, existing)
        if recorded_base != base:
            return _park_base_mismatch(args, pr, recorded_base, base)

    # 3. Ensure the run owner label exists (idempotent — `--force` creates or updates).
    label_argv = ["gh", "label", "create", run_label, "--color", RUN_LABEL_COLOR,
                  "--description", f"gauntlet: run {args.run_id}", "--force"]
    if args.repo:
        label_argv += ["--repo", args.repo]
    proc = _run(label_argv, cwd=gh_cwd)
    if proc.returncode != 0:
        return _refuse(f"could not create label {run_label}: {proc.stderr.strip()}")

    # 4. Register the row — refresh in place if it exists, else add. Never a duplicate row.
    #
    # `set` writes only the fields it NAMES, so preservation is the default — a refresh names only what it
    # must recompute and everything else survives untouched. What that is depends on whether the head moved:
    #   * NEW row: initialize the SHA-bound gate fields AND `status` (the fresh row's starting state).
    #   * MOVED head on an existing row: the new head is new evidence, so reset the SHA-bound EVIDENCE — the
    #     review tally and ci here, and THE LIVENESS COUNTERS at the accessor. The `--head-sha` write below
    #     resets those counters itself (ledger.py's `apply_head_sha`; stage-2-ci.md, "THE LIVENESS COUNTERS")
    #     — this refresh MUST NOT hand-reset them. `status` is PRESERVED: it tracks a HUMAN decision, not the
    #     SHA, so a head refresh must never un-hold a PR the user has not ruled on (awaiting-user, aborted,
    #     repairing).
    #   * UNCHANGED head on an existing row: name none of the SHA-bound fields — the accumulated verdicts,
    #     ci and liveness state all describe content that is still there and are preserved (the accessor's
    #     same-value `--head-sha` write is a no-op, so the counters are untouched).
    # `existing`/`rows` were loaded above (the re-adoption base gate needs them); nothing has written the
    # ledger since, so they are still current.
    head_changed = existing is not None and existing.get("head_sha") != planned_head
    verb = "set" if existing is not None else "add-row"
    ledger_argv = ["python3", str(LEDGER_PY), "--file", args.file, verb, "--pr", pr,
                   "--branch", branch,
                   "--head-sha", planned_head,
                   "--slug", str(row["slug"]),
                   "--pr-origin", str(row["pr_origin"])]
    if existing is None:
        # A NEW row RECORDS its live base once, through the CREATE_ONLY `--base-branch` door. Every new row
        # carries an explicit base — even in a single-base run — so the base is per-row from creation. `set`
        # has no `--base-branch` flag, so a re-adoption can never rewrite it (the gate above parks instead).
        ledger_argv += ["--base-branch", base,
                        "--tier", str(row["tier"]),
                        "--ci", str(row["ci"]),
                        "--status", str(row["status"]),
                        "--reviews-ok", str(row["reviews_ok"])]
    elif head_changed:
        ledger_argv += ["--tier", str(row["tier"]),
                        "--ci", str(row["ci"]),
                        "--reviews-ok", str(row["reviews_ok"])]
    proc = _run(ledger_argv, cwd=args.project_root)
    if proc.returncode != 0:
        return _refuse(f"ledger {verb} for PR {pr} failed: {proc.stderr.strip()}")

    # 5. Resolve the PR-head worktree — DISCOVER the branch's ACTUAL checkout, never assume the default path.
    # On any git failure, refuse and say the row/label already landed — a half-adoption must be VISIBLE.
    half = (f"(the run label {run_label} and the ledger row for PR {pr} were ALREADY written; "
            f"the PR's labels were NOT applied)")
    branch_ref = f"refs/heads/{branch}"
    origin_ref = f"refs/remotes/origin/{branch}"
    # FETCH both tracking refs first, so every worktree choice is made against ground truth, not a stale
    # local ref. The review step diffs origin/<base>...HEAD, so the base tracking ref must exist too.
    for ref in (base, branch):
        fetch = _run(["git", "-C", args.project_root, "fetch", "origin",
                      f"refs/heads/{ref}:refs/remotes/origin/{ref}"])
        if fetch.returncode != 0:
            return _refuse(f"git fetch of {ref} failed: {fetch.stderr.strip()} {half}")

    # DISCOVERY CHOKEPOINT: the branch may already be checked out — at the project root, at the default
    # worktree path, or at any other worktree — and `git worktree add` REFUSES a branch checked out elsewhere
    # (exit 128, one checkout per branch), which would leave a half-adoption. Ask git where it actually is,
    # once, and record THAT path — never blindly add at the default.
    listing = _run(["git", "-C", args.project_root, "worktree", "list", "--porcelain", "-z"])
    if listing.returncode != 0:
        return _refuse(f"git worktree list failed: {listing.stderr.strip()} {half}")
    existing_checkout = worktree_for_branch(listing.stdout, branch_ref)

    if existing_checkout is not None:
        # REUSE the discovered checkout — wherever it sits (root, the default path, or another worktree). It
        # must be CLEAN (never adopt over uncommitted work) and fast-forwardable to the fetched origin head.
        worktree = existing_checkout
        status = _run(["git", "-C", worktree, "status", "--porcelain", "--untracked-files=all"])
        if status.returncode != 0:
            return _refuse(f"git status in reused worktree {worktree} failed: "
                           f"{status.stderr.strip()} {half}")
        if status.stdout.strip():
            return _refuse(f"reused worktree {worktree} for branch {branch} is DIRTY (uncommitted changes) "
                           f"— refusing to adopt over a dirty tree {half}")
        ff = _run(["git", "-C", worktree, "merge", "--ff-only", origin_ref])
        if ff.returncode != 0:
            return _refuse(f"fast-forward of reused worktree {worktree} to origin/{branch} failed "
                           f"(it is not a fast-forward of the PR head): {ff.stderr.strip()} {half}")
        # PRESERVE created-ownership on a re-adoption of the SAME worktree campaign itself created: a first
        # adopt that CREATED it recorded worktree_owned=yes, and clobbering that to `no` would strand it from
        # Stage-3 cleanup. A genuinely pre-existing external checkout (first adoption, or a differently
        # recorded worktree) is `no`/`no`.
        if existing is not None and existing.get("worktree") == worktree:
            worktree_owned = existing.get("worktree_owned", "no")
            branch_owned = existing.get("branch_owned", "no")
        else:
            worktree_owned, branch_owned = "no", "no"
    else:
        # No checkout of the branch anywhere → CREATE one at the default path from the fetched origin head. A
        # detached HEAD, or a DIFFERENT branch, occupying the default path is NOT this branch's checkout —
        # discovery returned None — so `git worktree add` below hits an occupied path and FAILS CLOSED here,
        # never a silent adoption of a checkout that is not the PR head.
        worktree = str(plan["worktree"])
        local = _run(["git", "-C", args.project_root, "show-ref", "--verify", "--quiet", branch_ref])
        if local.returncode == 0:
            # The local branch exists but is checked out nowhere: add a worktree on it, then bring it to the
            # fetched origin head. Never `-B` — that could reset a pre-existing local branch.
            add = _run(["git", "-C", args.project_root, "worktree", "add", worktree, branch])
            if add.returncode != 0:
                return _refuse(f"git worktree add for {branch} failed: {add.stderr.strip()} {half}")
            ff = _run(["git", "-C", worktree, "merge", "--ff-only", origin_ref])
            if ff.returncode != 0:
                return _refuse(f"fast-forward of new worktree {worktree} to origin/{branch} failed: "
                               f"{ff.stderr.strip()} {half}")
            branch_owned = "no"
        elif local.returncode == 1:
            # No local branch either (ref absent): create it from the fetched origin head.
            add = _run(["git", "-C", args.project_root, "worktree", "add", "-b", branch, worktree,
                        origin_ref])
            if add.returncode != 0:
                return _refuse(f"git worktree add -b for {branch} failed: {add.stderr.strip()} {half}")
            branch_owned = "yes"
        else:
            return _refuse(f"git show-ref for {branch} failed unexpectedly (exit {local.returncode}): "
                           f"{local.stderr.strip()} {half}")
        worktree_owned = "yes"

    # VERIFY the resolved worktree is at the PLANNED head — after any fast-forward or create. A checkout that
    # cannot be brought to the recorded head is STALE (a same-named local branch left behind, or a remote
    # moved since the snapshot); refuse rather than record a worktree that does not match the PR head.
    rev = _run(["git", "-C", worktree, "rev-parse", "HEAD"])
    if rev.returncode != 0:
        return _refuse(f"git rev-parse HEAD in worktree {worktree} failed: {rev.stderr.strip()} {half}")
    actual = rev.stdout.strip()
    if actual != planned_head:
        return _refuse(f"worktree {worktree} for branch {branch} is at {actual} but PR {pr}'s head is "
                       f"{planned_head} — the checkout is STALE (does not match the PR head); refusing {half}")

    set_argv = ["python3", str(LEDGER_PY), "--file", args.file, "set", "--pr", pr,
                "--worktree", worktree, "--worktree-owned", worktree_owned,
                "--branch-owned", branch_owned]
    proc = _run(set_argv, cwd=args.project_root)
    if proc.returncode != 0:
        return _refuse(f"ledger set of worktree fields for PR {pr} failed: {proc.stderr.strip()}")

    # 6. Label it ours and set the ONE status label from the LIVE gate. The two status labels are mutually
    # exclusive, so whichever we apply, we remove the other IN THE SAME CALL. `gauntlet-accepted` only when
    # the (preserved-or-reset) gate is met at THIS head — reviews_ok >= required(tier); otherwise it is
    # under review. A fresh adoption and a head change both reset reviews_ok to 0, so they read as reviewing.
    final = L.find_row(L.load(Path(args.file))[1], pr) or {}
    reviews_ok = int(final.get("reviews_ok", "0") or "0")
    tier_now = final.get("tier", str(row["tier"]))
    gate_met = reviews_ok >= RP.required_reviews(tier_now)
    status_label = ACCEPTED_LABEL if gate_met else REVIEWING_LABEL
    other_label = REVIEWING_LABEL if gate_met else ACCEPTED_LABEL
    edit_argv = ["gh", "pr", "edit", pr, "--add-label", run_label,
                 "--add-label", status_label, "--remove-label", other_label]
    if args.repo:
        edit_argv += ["--repo", args.repo]
    proc = _run(edit_argv, cwd=gh_cwd)
    if proc.returncode != 0:
        return _refuse(f"`gh pr edit {pr}` (labels) failed: {proc.stderr.strip()} "
                       f"(the ledger row and worktree for PR {pr} were already written)")

    # 7. Fold the run's default Non-goals into the PR's intent managed block. On an eligible RE-ADOPTION
    # (terminal rows stopped above) the intent artifact is already present, so this syncs it automatically;
    # on a FRESH adoption it does not exist yet (the driver authors it next, then runs `intent-sync`), so
    # this reports `pending-intent`. A malformed ledger/intent surfaces as a refusal rather than a silent
    # skip — but the row and labels already landed, exactly like a half-adoption, and re-running
    # `intent-sync` recovers it.
    try:
        intent_sync = sync_intent_defaults(args.file, pr)
    except ValueError as exc:
        return _refuse(f"row and labels for PR {pr} landed, but intent-sync failed: {exc}")

    # 8. Adoption summary.
    print(json.dumps({
        "pr": pr,
        "run_id": args.run_id,
        "row_written": True,
        "worktree": worktree,
        "worktree_owned": worktree_owned,
        "labels_added": [run_label, status_label],
        "label_removed": other_label,
        "intent_sync": intent_sync,
    }))
    return 0


# --- CLI ----------------------------------------------------------------------

def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="PURE: parse a `gh pr view` JSON file, print the adoption plan, exit 0")
    p.add_argument("--view-json", required=True, help="path to a parsed `gh pr view` JSON document")
    p.add_argument("--run-id", required=True, help="this run's id (the owner label is gauntlet-run-<id>)")
    p.add_argument("--tier", required=True,
                   help="bootstrap tier; adoption passes STANDARD, then the orchestrator decides the real "
                        "tier at or above triage.py's floor before gate work")
    p.add_argument("--worktrees-root", default=".worktrees", help="root under which the head worktree sits")

    a = sub.add_parser("adopt", help="the real thing: read the PR, refuse/register/worktree/label")
    a.add_argument("--pr", required=True, help="PR number to adopt")
    a.add_argument("--run-id", required=True, help="this run's id")
    a.add_argument("--file", required=True, help="the run ledger (<rundir>/state.jsonl)")
    a.add_argument("--tier", required=True,
                   help="bootstrap tier; adoption passes STANDARD, then the orchestrator decides the real "
                        "tier at or above triage.py's floor before gate work")
    a.add_argument("--worktrees-root", required=True, help="root under which the head worktree sits")
    a.add_argument("--project-root", required=True, help="the repo checkout git/ledger commands run in")
    a.add_argument("--repo", help="owner/name (default: the project-root checkout's)")

    s = sub.add_parser("intent-sync",
                       help="fold the run's default Non-goals into ONE PR's intent-<pr>.md managed block")
    s.add_argument("--file", required=True, help="the run ledger (<rundir>/state.jsonl)")
    s.add_argument("--pr", required=True, help="PR number whose intent to sync")

    sub.add_parser("self-test", help="run every fixture (pr-adopt-test.py's CASES)")

    args = parser.parse_args(argv)
    if args.cmd == "self-test":
        return self_test()
    if args.cmd == "plan":
        return cmd_plan(args)
    if args.cmd == "intent-sync":
        return cmd_intent_sync(args)
    return cmd_adopt(args)


# --- self-test ----------------------------------------------------------------

class SelfTestFailure(AssertionError):
    """A rule this file claims to enforce does not hold."""


def sibling_cases() -> list:
    if not SIBLING.exists():
        raise SelfTestFailure(f"the fixture file {SIBLING} IS MISSING — this suite has no fixtures to run "
                              f"and CANNOT report health. Every rule this file enforces is now unpinned.")
    mod = load_module_from_path("pr_adopt_test", SIBLING, register=True)
    if mod is None:
        raise SelfTestFailure(f"{SIBLING} exists but cannot be loaded as a module")
    cases = getattr(mod, "CASES", None)
    if not cases:
        raise SelfTestFailure(f"{SIBLING} exports no CASES — every rule in this file is unpinned while the "
                              f"suite still exits 0")
    return list(cases)


def self_test() -> int:
    failures = 0
    try:
        cases = sibling_cases()
    except SelfTestFailure as exc:
        print(f"FAIL     {'sibling-fixtures':30} -> the fixtures in {SIBLING.name} must be RUNNABLE\n"
              f"         {exc}")
        print("\n1 check(s) FAILED — pr-adopt's contract is broken.")
        return 1
    for name, rule, fn in cases:
        try:
            fn()
        except SelfTestFailure as exc:
            print(f"FAIL     {name:30} -> {rule}\n         {exc}")
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL     {name:30} -> {rule}\n         raised {type(exc).__name__}: {exc}")
            failures += 1
        else:
            print(f"ok       {name:30} -> {rule}")
    print()
    if failures:
        print(f"{failures} check(s) FAILED — pr-adopt's contract is broken.")
        return 1
    print(f"all {len(cases)} fixtures hold — pr-adopt's contract is intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
