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

import json
import subprocess
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from _gauntlet.modules import load_module_from_path

OWNER = Path(__file__).resolve().parent / "label-mirror.py"


def _load_owner():
    mod = load_module_from_path("label_mirror_owner", OWNER)
    if mod is None:
        raise RuntimeError(f"cannot load the label-mirror at {OWNER}")
    return mod


M = _load_owner()
L = M.L


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise M.SelfTestFailure(msg)


class FakeGh:
    """A recorded `gh` runner. Answers `pr view` and `pr edit` from canned responses, records every argv,
    and REFUSES any other command — a fixture that reaches an unexpected `gh` call is a fixture testing
    something it did not mean to."""

    def __init__(self, *, view=None, edit=(0, "", "")) -> None:
        self.view = view          # (returncode, stdout, stderr) for `gh pr view`, or None to refuse it
        self.edit = edit          # (returncode, stdout, stderr) for `gh pr edit`
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> "subprocess.CompletedProcess[str]":
        self.calls.append(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            resp = self.view
        elif argv[:3] == ["gh", "pr", "edit"]:
            resp = self.edit
        else:
            raise AssertionError(f"unexpected gh call: {argv}")
        if resp is None:
            raise AssertionError(f"gh call not expected in this fixture: {argv}")
        rc, out, err = resp
        return subprocess.CompletedProcess(argv, rc, out, err)

    @property
    def edited(self) -> bool:
        return any(a[:3] == ["gh", "pr", "edit"] for a in self.calls)


def view_with(*labels: str) -> tuple:
    """A successful `gh pr view --json labels` response carrying exactly these label names."""
    return (0, json.dumps({"labels": [{"name": n} for n in labels]}), "")


def build_ledger(d: Path, *, status="in_review", tier="STANDARD", reviews_ok="0", pr="9") -> Path:
    led = d / "state.jsonl"
    header = dict(L.HEADER_DEFAULTS)
    header["run_id"] = "g1"
    row = dict(L.ROW_DEFAULTS)
    row.update(pr=pr, status=status, tier=tier, reviews_ok=reviews_ok)
    L.dump(led, header, [row])
    return led


def drive(led: Path, pr: str, repo: str, fake: FakeGh, *, dry_run=False) -> tuple:
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
SWAP_TO_ACCEPTED = ["gh", "pr", "edit", "9", "--repo", REPO,
                    "--add-label", "gauntlet-accepted", "--remove-label", "gauntlet-reviewing"]
SWAP_TO_REVIEWING = ["gh", "pr", "edit", "9", "--repo", REPO,
                     "--add-label", "gauntlet-reviewing", "--remove-label", "gauntlet-accepted"]


# --- the swap is applied, with the EXACT argv ---------------------------------

def t_reviewing_to_accepted_swaps():
    with tempfile.TemporaryDirectory() as d:
        led = build_ledger(Path(d), tier="STANDARD", reviews_ok="2")   # 2/2 -> accepted
        fake = FakeGh(view=view_with("gauntlet-reviewing", "gauntlet-run-g1"))
        code, out, _err = drive(led, "9", REPO, fake)
    check(code == 0, f"a met gate must reconcile and exit 0, got {code}")
    check(out is not None and out["changed"] is True, f"a reviewing->accepted swap must report changed, got {out!r}")
    check(out["desired"] == "gauntlet-accepted", f"desired must be accepted, got {out!r}")
    check(out["required"] == 2 and out["reviews_ok"] == 2, f"tier/tally must be reported, got {out!r}")
    check(out["current"] == ["gauntlet-reviewing", "gauntlet-run-g1"], f"current labels must be reported, got {out!r}")
    check(out["argv"] == SWAP_TO_ACCEPTED, f"the argv must be the canonical idempotent swap, got {out.get('argv')!r}")
    check(fake.edited, "the swap must actually call `gh pr edit`")


# --- already right: no swap, no edit call -------------------------------------

def t_accepted_stays():
    with tempfile.TemporaryDirectory() as d:
        led = build_ledger(Path(d), tier="STANDARD", reviews_ok="2")
        fake = FakeGh(view=view_with("gauntlet-accepted"), edit=None)  # edit=None => any edit is a failure
        code, out, _err = drive(led, "9", REPO, fake)
    check(code == 0, f"an already-accepted PR reconciles to a no-op, got {code}")
    check(out["changed"] is False, f"no swap is needed, got {out!r}")
    check("argv" not in out, f"a no-op reconcile prints no argv, got {out!r}")
    check(not fake.edited, "an already-right label must trigger NO `gh pr edit`")


def t_reviewing_stays():
    with tempfile.TemporaryDirectory() as d:
        led = build_ledger(Path(d), tier="STANDARD", reviews_ok="1")   # 1/2 -> reviewing
        fake = FakeGh(view=view_with("gauntlet-reviewing"), edit=None)
        code, out, _err = drive(led, "9", REPO, fake)
    check(code == 0, f"a short gate already reviewing is a no-op, got {code}")
    check(out["desired"] == "gauntlet-reviewing" and out["changed"] is False, f"expected reviewing no-op, got {out!r}")
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
        fake = FakeGh(view=view_with("gauntlet-accepted", "gauntlet-run-g1"))
        code, out, _err = drive(led, "9", REPO, fake)
    check(code == 0, f"a short gate after escalation must reconcile and exit 0, got {code}")
    check(out is not None and out["changed"] is True,
          f"an accepted->reviewing swap must report changed, got {out!r}")
    check(out["desired"] == "gauntlet-reviewing", f"desired must be reviewing after escalation, got {out!r}")
    check(out["required"] == 2 and out["reviews_ok"] == 1, f"tier/tally must be reported, got {out!r}")
    check(out["current"] == ["gauntlet-accepted", "gauntlet-run-g1"],
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
        fake = FakeGh(view=view_with("gauntlet-reviewing"), edit=(1, "", "gh: label not found"))
        code, out, err = drive(led, "9", REPO, fake)
    check(code == 1, f"a failed `gh pr edit` must fail closed (exit 1), got {code}")
    check(out is None, "a failed edit prints no success JSON — the swap did not land")
    check("exited 1" in err and "label not found" in err, f"the stderr must show the gh failure, got {err!r}")
    check(fake.edited, "the edit WAS attempted (that is what failed)")


# --- dry-run computes the swap but applies nothing ----------------------------

def t_dry_run_no_edit():
    with tempfile.TemporaryDirectory() as d:
        led = build_ledger(Path(d), tier="STANDARD", reviews_ok="2")
        fake = FakeGh(view=view_with("gauntlet-reviewing"), edit=None)  # any edit fails the fixture
        code, out, _err = drive(led, "9", REPO, fake, dry_run=True)
    check(code == 0, f"a dry-run exits 0, got {code}")
    check(out["changed"] is True and out["argv"] == SWAP_TO_ACCEPTED,
          f"a dry-run must show the swap it WOULD apply, got {out!r}")
    check(not fake.edited, "a dry-run must apply NOTHING — no `gh pr edit`")


# --- the required(tier) boundary, exactly, for TRIVIAL(1) and STANDARD(2) -----

def t_required_boundary():
    # (tier, reviews_ok, expected desired) — each straddles required(tier) by exactly one.
    for tier, ok, desired in [
        ("TRIVIAL", "1", "gauntlet-accepted"),    # 1/1 meets the floor
        ("TRIVIAL", "0", "gauntlet-reviewing"),   # 0/1 short
        ("STANDARD", "2", "gauntlet-accepted"),   # 2/2 meets the floor
        ("STANDARD", "1", "gauntlet-reviewing"),  # 1/2 short
        ("HIGH", "2", "gauntlet-accepted"),       # HIGH needs 2, like STANDARD
    ]:
        with tempfile.TemporaryDirectory() as d:
            led = build_ledger(Path(d), tier=tier, reviews_ok=ok)
            # Seed current with the OTHER label so the swap is always "changed" and observable.
            other = "gauntlet-reviewing" if desired == "gauntlet-accepted" else "gauntlet-accepted"
            fake = FakeGh(view=view_with(other))
            code, out, _err = drive(led, "9", REPO, fake)
        check(code == 0, f"[{tier} {ok}] exit 0, got {code}")
        check(out["desired"] == desired, f"[{tier} {ok}/{out['required']}] desired must be {desired}, got {out!r}")


CASES = [
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
    ("required-boundary", "reviews_ok at exactly required(tier) picks accepted; one under picks reviewing", t_required_boundary),
]
