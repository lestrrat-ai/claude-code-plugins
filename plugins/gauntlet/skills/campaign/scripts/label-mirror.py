#!/usr/bin/env python3
"""Reconcile a PR's STATUS LABEL with its review gate — the ONE way the label swap is done.

THE RULE THIS ENCODES: the gate and its label move together (`stage-2-review-gate.md`, "Status labels
mirror the review gate"). A PR whose tip holds `required(tier)` SATISFIED verdicts wears
`gauntlet-accepted`; otherwise it wears `gauntlet-reviewing`. The label is a PROJECTION of `reviews_ok`,
and it is the one piece of run state a human reads on GitHub — a stale `gauntlet-accepted` is a false
PUBLIC claim that a PR passed a gauntlet it did not.

WHY THIS IS A COMMAND AND NOT A `gh pr edit` A DRIVER TYPES BY HAND. The swap was a two-command idiom the
orchestrator ran at every gate-reset site — the verdict tally, a CI/review fix push, a content-changing
rebase (conflict-resolving or diff-changed), a re-adoption. A step a fresh-context heartbeat must re-derive and retype at N sites is a step it forgets at
one of them, and the miss is invisible until a human reads the wrong label on GitHub. This tool reads the
ledger row, computes the desired label the same way the gate does, and applies the canonical idempotent
swap — so no driver hand-runs it and no driver runs it wrong.

    label-mirror.py mirror --ledger <state.jsonl> --pr <N> --repo owner/name [--dry-run]
    label-mirror.py self-test   run every fixture (label-mirror-test.py)

It touches EXACTLY the two status labels and nothing else. A run's ownership label (`gauntlet-run-<id>`)
is adoption's business (`pr-adoption.md`) and is NEVER added or removed here. A terminal row
(`merged`/`aborted`) is DONE: the tool skips it and makes no GitHub call at all. It FAILS CLOSED — a row
that is missing, or whose `tier` nobody set, is refused loudly (exit 2), never defaulted; a `gh` call that
fails or returns output it cannot parse is exit 1 with the stderr shown, never a guess.

`required(tier)` is REUSED from `nudge.py`, never retyped — the same helper `merge-check.py` borrows. This
tool holds no second opinion about how many verdicts a tier needs.

THE FIXTURE SUITE IS THE SIBLING `label-mirror-test.py`, this tool's EXECUTABLE CONTRACT; `self-test`
loads it by a `__file__`-relative path and FAILS LOUDLY if it is missing — a self-test that passes because
it found no tests is not a passing gate.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Callable, NoReturn

from _gauntlet.modules import load_module_from_path

_HERE = Path(__file__).resolve().parent
SIBLING = _HERE / "label-mirror-test.py"     # the fixture suite — this tool's executable contract


def _load(name: str, filename: str):
    mod = load_module_from_path(name, _HERE / filename)
    if mod is None:
        raise RuntimeError(f"cannot load {filename}")
    return mod


# The schema owner. `load` and `COUNT_RE` are imported, never restated — the ledger format has one parser,
# and the count format ("a decimal from 0 up") has one definition.
L = _load("label_mirror_ledger", "ledger.py")

# `required(tier)` — 1 if TRIVIAL else 2 — is REUSED, never retyped, exactly as `merge-check.py` does. The
# rule already lives in `nudge.py`; a copy here would be the drift this repo keeps killing.
_N = _load("label_mirror_nudge", "nudge.py")
REQUIRED = _N.required

# --- the two labels, and NOTHING ELSE is ever added or removed ------------------------------------
ACCEPTED = "gauntlet-accepted"
REVIEWING = "gauntlet-reviewing"

# TERMINAL — a DONE row is skipped, no GitHub call at all. `merged`/`aborted` are the two terminal
# statuses (the ledger `status` field, `files-and-ledger.md`, owns the vocabulary).
TERMINAL = ("merged", "aborted")

# The triage tiers (`stage-2-review-gate.md`, 2a-triage, owns the vocabulary). This ALLOW-LIST exists only
# to REFUSE a tier nobody set: `nudge.required()` maps any non-`TRIVIAL` value to 2, so a `-` or a typo'd
# tier would silently pick `gauntlet-reviewing` — a label chosen for a PR that was never triaged. An
# explicit membership test refuses it instead. `required()` still owns the COUNT for a tier that IS set.
TIERS = ("TRIVIAL", "STANDARD", "HIGH")


# A `gh` runner: argv -> a finished process (`.returncode`, `.stdout`, `.stderr`). Injected so the suite
# drives the whole tool with RECORDED responses and no network; the default talks to real `gh`.
Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _real_run(argv: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603


class LabelError(Exception):
    """A `gh` call failed or returned output we cannot read. The reconcile fails CLOSED — exit 1."""


def fail(msg: str) -> NoReturn:
    """An OPERATOR ERROR — a row that is missing, or a tier nobody set. Not a GitHub failure, and not a
    reconcile outcome: the caller handed us something we cannot act on. Exit 2, never a default."""
    print(f"label-mirror: {msg}", file=sys.stderr)
    raise SystemExit(2)


def parse_reviews_ok(row: dict) -> int:
    """`reviews_ok` as a NUMBER, refusing anything that is not one — the same count format the ledger holds
    every tally to (`COUNT_RE`, imported). A hand-edited `reviews_ok` of `-` or `two` is a corrupt store,
    not a number to guess at, and greening or reviewing a PR off it would be a label chosen from garbage."""
    value = row["reviews_ok"]
    if not L.COUNT_RE.match(value):
        fail(f"pr {row['pr']}: reviews_ok is {value!r}, not a count (a decimal from 0 up) — a value the "
             f"gate cannot count is a corrupt store, never a label to pick from")
    return int(value)


def desired_and_other(tier: str, reviews_ok: int) -> "tuple[str, str]":
    """(desired label, the OTHER label) for a row whose `tier` is already known-valid. PURE.

    `gauntlet-accepted` iff the tally meets `required(tier)`, else `gauntlet-reviewing`. The count is
    `required(tier)`'s to own; this only compares against it.
    """
    if reviews_ok >= REQUIRED(tier):
        return ACCEPTED, REVIEWING
    return REVIEWING, ACCEPTED


def current_labels(run: Runner, pr: str, repo: str) -> list[str]:
    """The PR's current label NAMES, from `gh pr view <pr> --repo <repo> --json labels`.

    Every failure raises `LabelError`, which the caller turns into a fail-closed exit 1: a spawn failure, a
    non-zero exit, output that is not JSON, or a `labels` array that is absent or malformed. A label set we
    cannot READ is never treated as an empty one — that would let a swap fire against a PR whose real labels
    we never saw.
    """
    argv = ["gh", "pr", "view", str(pr), "--repo", repo, "--json", "labels"]
    try:
        proc = run(argv)
    except OSError as exc:
        raise LabelError(f"could not run `gh pr view {pr}`: {exc}") from exc
    if proc.returncode != 0:
        raise LabelError(f"`gh pr view {pr}` exited {proc.returncode}: {proc.stderr.strip()}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise LabelError(f"`gh pr view {pr}` response is not JSON ({exc})") from exc
    labels = data.get("labels") if isinstance(data, dict) else None
    if not isinstance(labels, list):
        raise LabelError(f"`gh pr view {pr}` response has no `labels` array — got {data!r}")
    names: list[str] = []
    for entry in labels:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise LabelError(f"`gh pr view {pr}` returned a label without a string `name`: {entry!r}")
        names.append(entry["name"])
    return names


def apply_swap(run: Runner, argv: list[str], pr: str) -> None:
    """Run the canonical idempotent swap. A spawn failure or non-zero exit raises `LabelError` — exit 1."""
    try:
        proc = run(argv)
    except OSError as exc:
        raise LabelError(f"could not run `gh pr edit {pr}`: {exc}") from exc
    if proc.returncode != 0:
        raise LabelError(f"`gh pr edit {pr}` exited {proc.returncode}: {proc.stderr.strip()}")


def mirror(ledger_path: Path, pr: str, repo: str, *, dry_run: bool = False,
           run: Runner = _real_run) -> int:
    """Reconcile ONE PR's status label with its gate. Print one JSON object; return the exit code.

    Order: no row -> refuse (2); a terminal row -> skip, no GitHub call; a tier nobody set -> refuse (2);
    then read the live labels and apply the idempotent swap only if the label is not already right.
    """
    _header, rows = L.load(ledger_path)
    pr = str(pr)
    row = next((r for r in rows if r["pr"] == pr), None)
    if row is None:
        fail(f"no ledger row for pr {pr} — a PR this run never adopted has no gate to mirror a label from")

    status = row["status"]
    if status in TERMINAL:
        # DONE. Touch nothing — a merged/aborted PR's labels are not campaign's to move.
        print(json.dumps({"pr": pr, "skipped": "terminal"}))
        return 0

    tier = row["tier"]
    if tier not in TIERS:
        fail(f"pr {pr}: tier is {tier!r}, not one of {'/'.join(TIERS)} — a tier nobody set cannot pick a "
             f"label. Triage the PR first (`stage-2-review-gate.md`, 2a-triage)")

    reviews_ok = parse_reviews_ok(row)
    required = REQUIRED(tier)
    desired, other = desired_and_other(tier, reviews_ok)
    argv = ["gh", "pr", "edit", pr, "--repo", repo, "--add-label", desired, "--remove-label", other]

    try:
        current = current_labels(run, pr, repo)
    except LabelError as exc:
        print(f"label-mirror: {exc}", file=sys.stderr)
        return 1

    # Already right: the desired label is present AND the other absent. Anything else needs the swap —
    # desired missing, the other lingering, or both labels at once (what a missed swap actually leaves).
    changed = not (desired in current and other not in current)

    out: dict = {"pr": pr, "tier": tier, "reviews_ok": reviews_ok, "required": required,
                 "desired": desired, "current": current, "changed": changed}
    if changed or dry_run:
        out["argv"] = argv

    if changed and not dry_run:
        try:
            apply_swap(run, argv, pr)
        except LabelError as exc:
            print(f"label-mirror: {exc}", file=sys.stderr)
            return 1

    print(json.dumps(out))
    return 0


# --- self-test: the executable contract lives in the SIBLING module ------------

class SelfTestFailure(AssertionError):
    """A rule this file claims to enforce does not hold."""


def _sibling_cases() -> list:
    if not SIBLING.exists():
        raise SelfTestFailure(
            f"the fixture suite is NOT AT {SIBLING} — `self-test` has NO SUBJECT, and a check that cannot "
            f"find the thing it tests must FAIL, never pass.")
    mod = load_module_from_path("label_mirror_test", SIBLING, register=True)
    if mod is None:
        raise SelfTestFailure(f"{SIBLING} exists but cannot be loaded as a module")
    cases = getattr(mod, "CASES", None)
    if not cases:
        raise SelfTestFailure(f"{SIBLING} exports no CASES — every rule in this file is unpinned while the "
                              f"suite still exits 0")
    return list(cases)


def self_test() -> int:
    """Run the sibling suite over every fixture. Non-zero on any failure."""
    failures = 0
    try:
        cases = _sibling_cases()
    except SelfTestFailure as exc:
        print(f"FAIL     {'sibling-fixtures':30} -> the fixtures in {SIBLING.name} must be RUNNABLE\n"
              f"         {exc}")
        print("\n1 check(s) FAILED — the label-mirror's contract is broken.")
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
        print(f"{failures} fixture(s) failed — the label-mirror's contract is broken.")
        return 1
    print(f"all {len(cases)} fixtures hold — the label-mirror's contract is intact.")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(description=next(iter((__doc__ or "").splitlines()), ""))
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mirror", help="reconcile one PR's status label with its review gate")
    m.add_argument("--ledger", required=True, type=Path, help="the run ledger (<rundir>/state.jsonl)")
    m.add_argument("--pr", required=True, help="PR number")
    m.add_argument("--repo", required=True, help="owner/name — NEVER resolved from the checkout")
    m.add_argument("--dry-run", action="store_true", help="print the decision and argv, apply nothing")

    sub.add_parser("self-test", help="run every fixture (label-mirror-test.py)")

    args = p.parse_args(argv)

    if args.cmd == "self-test":
        return self_test()
    return mirror(args.ledger, args.pr, args.repo, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
