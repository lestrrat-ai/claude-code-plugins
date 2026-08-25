#!/usr/bin/env python3
"""Fixtures for `label-mirror.py` — the status-label reconciler.

They live in a SIBLING file, and `label-mirror.py self-test` FAILS LOUDLY if it cannot load them.

EVERY FIXTURE HAS TEETH. Each drives the REAL `mirror()` over a temp ledger (built through the ledger
accessor) and a FAKE `gh` seam (recorded responses, no network), and asserts the JSON FIELDS — not just the
exit code. The swap case pins the EXACT argv, because the argv is what actually moves the label; the
terminal and refusal cases assert the fake was NEVER called, because "makes no GitHub call" is the whole
promise there.
"""

from __future__ import annotations

import ast
import json
import subprocess
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from _gauntlet.modules import load_sibling
from _gauntlet.testing import capture_cli, checker

OWNER = Path(__file__).resolve().parent / "label-mirror.py"


M = load_sibling("label_mirror_owner", OWNER.parent, OWNER.name)
L = M.L


check = checker(M.SelfTestFailure)


class FakeGh:
    """A recorded `gh` runner. Answers `pr view`, `pr edit` and `label create` from canned responses, records
    every argv, and REFUSES any other command — a fixture that reaches an unexpected `gh` call is a fixture
    testing something it did not mean to."""

    def __init__(self, *, view=None, edit=(0, "", ""), create=(0, "", "")) -> None:
        self.view = view          # (returncode, stdout, stderr) for `gh pr view`, or None to refuse it
        self.edit = edit          # (returncode, stdout, stderr) for `gh pr edit`
        self.create = create      # (returncode, stdout, stderr) for `gh label create`
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> "subprocess.CompletedProcess[str]":
        self.calls.append(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            resp = self.view
        elif argv[:3] == ["gh", "pr", "edit"]:
            resp = self.edit
        elif argv[:3] == ["gh", "label", "create"]:
            resp = self.create
        else:
            raise AssertionError(f"unexpected gh call: {argv}")
        if resp is None:
            raise AssertionError(f"gh call not expected in this fixture: {argv}")
        rc, out, err = resp
        return subprocess.CompletedProcess(argv, rc, out, err)

    @property
    def edited(self) -> bool:
        return any(a[:3] == ["gh", "pr", "edit"] for a in self.calls)

    @property
    def created(self) -> "list[str]":
        """The label NAMES this fixture created, in call order."""
        return [a[3] for a in self.calls if a[:3] == ["gh", "label", "create"]]


def view_with(*labels: str) -> tuple:
    """A successful `gh pr view --json labels` response carrying exactly these label names."""
    return (0, json.dumps({"labels": [{"name": n} for n in labels]}), "")


def build_ledger(d: Path, *, status="in_review", tier="STANDARD", reviews_ok="0", pr="9",
                 base_current="-") -> Path:
    led = d / "state.jsonl"
    header = dict(L.HEADER_DEFAULTS)
    header["run_id"] = "g1"
    row = dict(L.ROW_DEFAULTS)
    row.update(pr=pr, status=status, tier=tier, reviews_ok=reviews_ok, base_current=base_current)
    L.dump(led, header, [row])
    return led


def verdict(parsed: "dict | None") -> dict:
    """The printed verdict JSON, or a fixture failure saying it was absent.

    `drive` returns `None` for empty stdout because the REFUSAL fixtures assert exactly that. The
    success-path fixtures need the dict, so they come through here rather than each restating the
    None check before subscripting."""
    if parsed is None:
        raise M.SelfTestFailure("expected verdict JSON on stdout, got nothing")
    return parsed


def drive(led: Path, pr: str, repo: str, fake: FakeGh, *,
          dry_run=False) -> "tuple[int, dict | None, str]":
    """Run the REAL `mirror()` with the fake seam; return (exit_code, parsed_stdout_or_None, stderr)."""
    out, err = StringIO(), StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = M.mirror(led, pr, repo, dry_run=dry_run, run=fake)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    text = out.getvalue().strip()
    parsed = json.loads(text) if text else None
    return code, parsed, err.getvalue()


REPO = "o/n"
ACCEPTED = "gauntlet-accepted"
REBASE_PENDING = "gauntlet-rebase-pending"
SWAP_TO_ACCEPTED = ["gh", "pr", "edit", "9", "--repo", REPO,
                    "--add-label", ACCEPTED, "--remove-label", "gauntlet-reviewing 1/2"]
SWAP_TO_REVIEWING = ["gh", "pr", "edit", "9", "--repo", REPO,
                     "--add-label", "gauntlet-reviewing 1/2", "--remove-label", ACCEPTED]


# --- the swap is applied, with the EXACT argv ---------------------------------

def t_reviewing_to_accepted_swaps():
    with tempfile.TemporaryDirectory() as d:
        led = build_ledger(Path(d), tier="STANDARD", reviews_ok="2")   # 2/2 -> accepted
        fake = FakeGh(view=view_with("gauntlet-reviewing 1/2", "gauntlet-run-g1"))
        code, out, _err = drive(led, "9", REPO, fake)
        out = verdict(out)
    check(code == 0, f"a met gate must reconcile and exit 0, got {code}")
    out = verdict(out)
    check(out["changed"] is True, f"a reviewing->accepted swap must report changed, got {out!r}")
    check(out["desired"] == [ACCEPTED], f"desired must be accepted alone, got {out!r}")
    check(out["required"] == 2 and out["reviews_ok"] == 2, f"tier/tally must be reported, got {out!r}")
    check(out["current"] == ["gauntlet-reviewing 1/2", "gauntlet-run-g1"], f"current labels must be reported, got {out!r}")
    check(out["argv"] == SWAP_TO_ACCEPTED, f"the argv must be the canonical idempotent swap, got {out.get('argv')!r}")
    check(fake.created == [ACCEPTED], f"the label added must be created first, got {fake.created!r}")
    check(fake.edited, "the swap must actually call `gh pr edit`")


# --- the run-owner label is never swept, whatever else moves -------------------

def t_run_label_is_never_removed():
    """A sweep that took `gauntlet-run-<id>` off would drop the PR out of its own run — every later
    label-scoped query stops seeing it. `gauntlet-authored` is another owner's state for the same reason.
    Both must survive a reconcile that moves everything around them."""
    with tempfile.TemporaryDirectory() as d:
        led = build_ledger(Path(d), tier="STANDARD", reviews_ok="2")
        fake = FakeGh(view=view_with("gauntlet-reviewing 0/2", "gauntlet-run-g1", "gauntlet-authored"))
        code, out, _err = drive(led, "9", REPO, fake)
        out = verdict(out)
    check(code == 0, f"the reconcile must exit 0, got {code}")
    removed = [out["argv"][i + 1] for i, a in enumerate(out["argv"]) if a == "--remove-label"]
    check(removed == ["gauntlet-reviewing 0/2"],
          f"only the stale gate label may be removed, got {removed!r}")


# --- already right: no swap, no edit call -------------------------------------

def t_accepted_stays():
    with tempfile.TemporaryDirectory() as d:
        led = build_ledger(Path(d), tier="STANDARD", reviews_ok="2")
        fake = FakeGh(view=view_with(ACCEPTED), edit=None, create=None)  # any edit/create is a failure
        code, out, _err = drive(led, "9", REPO, fake)
        out = verdict(out)
    check(code == 0, f"an already-accepted PR reconciles to a no-op, got {code}")
    check(out["changed"] is False, f"no swap is needed, got {out!r}")
    check("argv" not in out, f"a no-op reconcile prints no argv, got {out!r}")
    check(not fake.edited, "an already-right label must trigger NO `gh pr edit`")


def t_reviewing_stays():
    with tempfile.TemporaryDirectory() as d:
        led = build_ledger(Path(d), tier="STANDARD", reviews_ok="1")   # 1/2 -> reviewing
        fake = FakeGh(view=view_with("gauntlet-reviewing 1/2"), edit=None, create=None)
        code, out, _err = drive(led, "9", REPO, fake)
        out = verdict(out)
    check(code == 0, f"a short gate already reviewing is a no-op, got {code}")
    check(out["desired"] == ["gauntlet-reviewing 1/2"] and out["changed"] is False,
          f"expected reviewing no-op, got {out!r}")
    check(not fake.edited, "an already-reviewing label must trigger NO edit")


# --- a re-adoption tier escalation flips a stale gauntlet-accepted back to reviewing --

def t_readopt_escalation_flips_accepted_to_reviewing():
    # An UNCHANGED re-adoption preserves reviews_ok (here 1), and pr-adopt.py's adoption-time labeling
    # applied gauntlet-accepted under the PRESERVED TRIVIAL (required 1). The adoption-time tier DECISION
    # then raises the tier to STANDARD (required 2), so 1/2 is short and the stale, publicly-visible
    # gauntlet-accepted MUST flip to gauntlet-reviewing — the co-located mirror in pr-adoption.md,
    # "Adoption-time tier decision", is what makes that happen. It is NOT a no-op here.
    with tempfile.TemporaryDirectory() as d:
        led = build_ledger(Path(d), tier="STANDARD", reviews_ok="1")   # 1/2 -> reviewing
        fake = FakeGh(view=view_with(ACCEPTED, "gauntlet-run-g1"))
        code, out, _err = drive(led, "9", REPO, fake)
        out = verdict(out)
    check(code == 0, f"a short gate after escalation must reconcile and exit 0, got {code}")
    out = verdict(out)
    check(out["changed"] is True,
          f"an accepted->reviewing swap must report changed, got {out!r}")
    check(out["desired"] == ["gauntlet-reviewing 1/2"], f"desired must be reviewing after escalation, got {out!r}")
    check(out["required"] == 2 and out["reviews_ok"] == 1, f"tier/tally must be reported, got {out!r}")
    check(out["current"] == [ACCEPTED, "gauntlet-run-g1"],
          f"current labels must be reported, got {out!r}")
    check(out["argv"] == SWAP_TO_REVIEWING,
          f"the argv must be the canonical reviewing-restoring swap, got {out.get('argv')!r}")
    check(fake.edited, "the escalation swap must actually call `gh pr edit`")


# --- refusals: a missing row and an unset tier, both before any gh call -------

def t_missing_row_refused():
    with tempfile.TemporaryDirectory() as d:
        led = build_ledger(Path(d), pr="9")            # holds pr 9
        fake = FakeGh(view=None, edit=None)            # any gh call is a failure
        code, out, err = drive(led, "42", REPO, fake)  # ask for pr 42
    check(code == 2, f"a missing row must refuse loudly (exit 2), got {code}")
    check(out is None and "no ledger row for pr 42" in err, f"the refusal must name the missing row, got {err!r}")
    check(fake.calls == [], "a missing row must be refused BEFORE any gh call")


def t_unset_tier_refused():
    with tempfile.TemporaryDirectory() as d:
        led = build_ledger(Path(d), tier="-", reviews_ok="2")   # tier nobody set
        fake = FakeGh(view=None, edit=None)
        code, out, err = drive(led, "9", REPO, fake)
    check(code == 2, f"a tier nobody set must refuse loudly (exit 2), got {code}")
    check(out is None and "tier is '-'" in err, f"the refusal must name the unset tier, got {err!r}")
    check(fake.calls == [], "an unset tier must be refused BEFORE any gh call")


# --- terminal rows are skipped with NO gh call at all -------------------------

def t_terminal_skipped_no_gh():
    for status in ("merged", "aborted"):
        with tempfile.TemporaryDirectory() as d:
            led = build_ledger(Path(d), status=status, tier="STANDARD", reviews_ok="2")
            fake = FakeGh(view=None, edit=None)   # ANY gh call fails the fixture
            code, out, _err = drive(led, "9", REPO, fake)
        check(code == 0, f"a {status} row is skipped, exit 0, got {code}")
        check(out == {"pr": "9", "skipped": "terminal"}, f"a {status} row prints the terminal skip, got {out!r}")
        check(fake.calls == [], f"a {status} row must make NO gh call at all")


# --- a gh view failure fails closed to exit 1 ---------------------------------

def t_gh_view_failure_exit_1():
    with tempfile.TemporaryDirectory() as d:
        led = build_ledger(Path(d), tier="STANDARD", reviews_ok="2")
        fake = FakeGh(view=(1, "", "gh: could not resolve to a PullRequest"), edit=None)
        code, out, err = drive(led, "9", REPO, fake)
    check(code == 1, f"a failed `gh pr view` must fail closed (exit 1), got {code}")
    check(out is None, "a failed view prints no verdict JSON")
    check("exited 1" in err and "could not resolve" in err, f"the stderr must show the gh failure, got {err!r}")
    check(not fake.edited, "a failed view must never reach the edit")


# --- a gh edit failure fails closed to exit 1 ---------------------------------

def t_gh_edit_failure_exit_1():
    with tempfile.TemporaryDirectory() as d:
        led = build_ledger(Path(d), tier="STANDARD", reviews_ok="2")
        fake = FakeGh(view=view_with("gauntlet-reviewing 0/2"), edit=(1, "", "gh: label not found"))
        code, out, err = drive(led, "9", REPO, fake)
    check(code == 1, f"a failed `gh pr edit` must fail closed (exit 1), got {code}")
    check(out is None, "a failed edit prints no success JSON — the swap did not land")
    check("exited 1" in err and "label not found" in err, f"the stderr must show the gh failure, got {err!r}")
    check(fake.edited, "the edit WAS attempted (that is what failed)")


# --- dry-run computes the swap but applies nothing ----------------------------

def t_dry_run_no_edit():
    with tempfile.TemporaryDirectory() as d:
        led = build_ledger(Path(d), tier="STANDARD", reviews_ok="2")
        fake = FakeGh(view=view_with("gauntlet-reviewing 1/2"), edit=None, create=None)  # any write fails the fixture
        code, out, _err = drive(led, "9", REPO, fake, dry_run=True)
        out = verdict(out)
    check(code == 0, f"a dry-run exits 0, got {code}")
    check(out["changed"] is True and out["argv"] == SWAP_TO_ACCEPTED,
          f"a dry-run must show the swap it WOULD apply, got {out!r}")
    check(not fake.edited, "a dry-run must apply NOTHING — no `gh pr edit`")


# --- the required(tier) boundary, exactly, for TRIVIAL(1) and STANDARD(2) -----

def t_required_boundary():
    # (tier, reviews_ok, expected desired) — each straddles required(tier) by exactly one, and each short
    # gate spells its OWN tally, which is the whole point of the tallied name: 0/1 and 1/2 are different
    # labels, so a reader sees how far along a PR is, not merely that it is not done.
    for tier, ok, desired in [
        ("TRIVIAL", "1", ACCEPTED),                 # 1/1 meets the floor
        ("TRIVIAL", "0", "gauntlet-reviewing 0/1"),  # 0/1 short
        ("STANDARD", "2", ACCEPTED),                 # 2/2 meets the floor
        ("STANDARD", "1", "gauntlet-reviewing 1/2"),  # 1/2 short — one verdict in
        ("STANDARD", "0", "gauntlet-reviewing 0/2"),  # 0/2 short — none yet
        ("HIGH", "2", ACCEPTED),                     # HIGH needs 2, like STANDARD
    ]:
        with tempfile.TemporaryDirectory() as d:
            led = build_ledger(Path(d), tier=tier, reviews_ok=ok)
            # Seed current with a DIFFERENT gate label so the swap is always "changed" and observable.
            other = "gauntlet-reviewing 9/9" if desired == ACCEPTED else ACCEPTED
            fake = FakeGh(view=view_with(other))
            code, out, _err = drive(led, "9", REPO, fake)
            out = verdict(out)
        check(code == 0, f"[{tier} {ok}] exit 0, got {code}")
        check(out["desired"] == [desired],
              f"[{tier} {ok}/{out['required']}] desired must be {desired!r}, got {out!r}")
        check(fake.created == [desired], f"[{tier} {ok}] the added label must be created, got {fake.created!r}")


def t_rebase_pending_is_an_independent_axis():
    """`gauntlet-rebase-pending` tracks `base_current`, NOT the gate — so an ACCEPTED PR that is behind its
    base wears both labels at once. That pair is the normal state of a PR waiting its turn in the serialized
    drain, and a reconcile that treated the two axes as one label would keep swapping them forever.

    Only an explicit `no` claims the PR is behind: `-` means nothing has probed this head yet, which is not
    a claim in either direction, so no label is applied for it.
    """
    cases = [
        # (reviews_ok, base_current, desired labels)
        ("2", "no", [ACCEPTED, REBASE_PENDING]),        # accepted AND behind — both, together
        ("2", "yes", [ACCEPTED]),                       # accepted and current — gate label alone
        ("2", "-", [ACCEPTED]),                         # no reading yet — never a guessed label
        ("1", "no", ["gauntlet-reviewing 1/2", REBASE_PENDING]),   # short AND behind
    ]
    for ok, base_current, desired in cases:
        with tempfile.TemporaryDirectory() as d:
            led = build_ledger(Path(d), tier="STANDARD", reviews_ok=ok, base_current=base_current)
            fake = FakeGh(view=view_with())            # a bare PR: everything desired must be added
            code, out, _err = drive(led, "9", REPO, fake)
            out = verdict(out)
        check(code == 0, f"[{ok},{base_current}] exit 0, got {code}")
        check(out["desired"] == desired, f"[{ok},{base_current}] desired must be {desired!r}, got {out!r}")
        added = [out["argv"][i + 1] for i, a in enumerate(out["argv"]) if a == "--add-label"]
        check(added == desired, f"[{ok},{base_current}] the argv must add exactly those, got {added!r}")


def t_rebase_pending_is_swept_when_the_row_says_current():
    """A PR whose rebase LANDED must lose the label in the same reconcile that sees the row say so —
    a `gauntlet-rebase-pending` left behind is a false public claim that a PR is still stuck."""
    with tempfile.TemporaryDirectory() as d:
        led = build_ledger(Path(d), tier="STANDARD", reviews_ok="2", base_current="yes")
        fake = FakeGh(view=view_with(ACCEPTED, REBASE_PENDING, "gauntlet-run-g1"))
        code, out, _err = drive(led, "9", REPO, fake)
        out = verdict(out)
    check(code == 0, f"the reconcile must exit 0, got {code}")
    check(out["argv"] == ["gh", "pr", "edit", "9", "--repo", REPO, "--remove-label", REBASE_PENDING],
          f"only the stale rebase label may move, got {out.get('argv')!r}")
    check(fake.created == [], f"a reconcile that ADDS nothing must create nothing, got {fake.created!r}")


def t_no_tool_spells_a_label_itself():
    """NO bundled tool may carry a label NAME as a string literal — `_gauntlet/labels.py` owns every
    spelling and every tool computes from it.

    This is the mechanical form of a rule that would otherwise be an exhortation. The vocabulary used to be
    two fixed strings copied into three tools, which was survivable; a tallied name is COMPUTED, so a
    second copy of the computation is a second answer to "which label does this row wear", and the copy
    goes wrong silently — on GitHub, where a human reads it.

    It reads STRING LITERALS through `ast`, not raw text: comments and docstrings explaining the labels are
    exactly what a doc-heavy tree is made of, and flagging those would make the check unusable. A docstring
    is a string literal, so docstrings are skipped explicitly. Sibling `*-test.py` suites are exempt —
    a fixture must be able to name the label it asserts, or it is asserting the code's own opinion.
    """
    names = (M.labels.ACCEPTED, M.labels.REVIEWING, M.labels.REBASE_PENDING)
    owner = Path(M.labels.__file__).resolve()
    offenders: list[str] = []
    for path in sorted(OWNER.parent.rglob("*.py")):
        if path.name.endswith("-test.py") or path.resolve() == owner:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.body:
                continue
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.add(id(first.value))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings
                    and any(name in node.value for name in names)):
                offenders.append(f"{path.name}:{node.lineno}: {node.value!r}")
    check(not offenders,
          "a label name is spelled inside a tool instead of coming from `_gauntlet/labels.py`:\n  "
          + "\n  ".join(offenders))


def t_malformed_repo_fails_before_any_gh_call():
    """A malformed `--repo` is refused at the CLI boundary, before the mirror touches GitHub.

    This tool had NO validation, and `--repo` here is documented as NEVER resolved from the checkout — so
    an unvalidated value was the only thing naming the repository whose labels get edited.
    """
    with tempfile.TemporaryDirectory() as d:
        led = build_ledger(Path(d), tier="STANDARD", reviews_ok="2")
        code, _out, err = capture_cli(M.main, ["mirror", "--ledger", str(led), "--pr", "9",
                                               "--repo", "not-a-repo"])
        check(code != 0, "a malformed --repo must be refused")
        check("'not-a-repo'" in err, f"the refusal must quote the value, got {err!r}")


CASES = [
    ("malformed-repo-refused", "a malformed --repo is refused at the CLI boundary, before any gh call", t_malformed_repo_fails_before_any_gh_call),
    ("reviewing-to-accepted", "a met gate swaps reviewing->accepted with the exact argv", t_reviewing_to_accepted_swaps),
    ("readopt-escalation", "a re-adoption tier escalation flips a stale accepted->reviewing with the exact argv", t_readopt_escalation_flips_accepted_to_reviewing),
    ("accepted-stays", "an already-accepted PR is a no-op — no edit call", t_accepted_stays),
    ("reviewing-stays", "an already-reviewing short gate is a no-op — no edit call", t_reviewing_stays),
    ("missing-row", "a PR with no ledger row is refused (exit 2), before any gh call", t_missing_row_refused),
    ("unset-tier", "a tier nobody set is refused (exit 2), before any gh call", t_unset_tier_refused),
    ("terminal-skip", "a merged/aborted row is skipped with NO gh call at all", t_terminal_skipped_no_gh),
    ("view-failure", "a failed `gh pr view` fails closed (exit 1), never reaches the edit", t_gh_view_failure_exit_1),
    ("edit-failure", "a failed `gh pr edit` fails closed (exit 1), no success JSON", t_gh_edit_failure_exit_1),
    ("dry-run", "a dry-run shows the swap argv but applies nothing", t_dry_run_no_edit),
    ("required-boundary", "reviews_ok at exactly required(tier) picks accepted; one under spells its own tally", t_required_boundary),
    ("run-label-never-removed", "a reconcile never sweeps gauntlet-run-<id> or gauntlet-authored", t_run_label_is_never_removed),
    ("rebase-pending-axis", "gauntlet-rebase-pending tracks base_current independently of the gate", t_rebase_pending_is_an_independent_axis),
    ("rebase-pending-swept", "a row that says current loses a standing gauntlet-rebase-pending", t_rebase_pending_is_swept_when_the_row_says_current),
    ("no-tool-spells-a-label", "no bundled tool carries a label name as a string literal — labels.py owns every spelling", t_no_tool_spells_a_label_itself),
]
