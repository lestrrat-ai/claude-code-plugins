#!/usr/bin/env python3
"""DECIDE what to do about a PR whose LIVE base no longer equals the base its ledger row records.

A campaign row records the base it was adopted on. When the live `baseRefName` diverges from it, exactly one
cause is fully explained by GitHub's own mechanics, with no user intent involved:

    merging PR A (head `<b>`) makes GitHub RETARGET every open PR based on `<b>` onto A's own base.

That is the stacked-PR case, and the campaign FOLLOWS it: the row's work did not change, so the row moves to
the new base and keeps going. Every other divergence — a hand-retarget, a deleted base branch, a base whose
PR is still open or was closed unmerged — is a user change this pipeline cannot interpret, and it PARKS the
row on the user exactly as it always did.

    base-retarget.py resolve --pr 36 --live main --file <state.jsonl> [--repo owner/name]
        [--project-root <dir>] [--candidates-json <path>]
    base-retarget.py decide --recorded fix-a --live main --candidates-json <path>
    base-retarget.py self-test    run every fixture (base-retarget-test.py)

`resolve` reads the row's RECORDED base itself (`ledger.py`'s `effective_base`) — a caller passes only the
LIVE base it observed, so the two can never be swapped. It then establishes the evidence, decides, and
performs the ONE ledger write that follows:

  * `migrate` -> `ledger.py retarget --pr <N> --from <recorded> --to <live>`, the transition that owns what a
    base move invalidates (`ledger.py`, `cmd_retarget`). It also RELEASES a park opened for this same base
    change, which is what lets the fail-closed consumers stay fail-closed: `pr-adopt.py`'s re-adoption gate
    and `merge.py`'s merge door still park the instant they see a divergence, and the next heartbeat's
    `resolve` clears that park when the evidence explains it.
  * `park` -> `ledger.py park --pr <N> --reason <BASE_CHANGE_PARK_REASON>`, the shared machine-blocker
    wording every base-change park in the campaign records, so the user's ruling path is unchanged.

THE DECISION IS PURE AND THE EVIDENCE IS A VALUE. `decide()` takes the candidate list and returns the
verdict; `resolve` fetches that list with ONE `gh pr list --head <recorded> --state merged` call. A recorded
response can be handed in with `--candidates-json` instead, so every rule below is pinned offline with no
`gh` and no network. That query is NOT a widening of the run snapshot: it asks about ONE branch — the base
this row records — and never re-opens which of the RUN's PRs are live (repo `CLAUDE.md` owns why the run
snapshot stays `--state open`).

FAIL CLOSED, ALWAYS TOWARDS THE PARK. A fetch that fails, a malformed candidate, no merged PR from the
recorded branch, several with contradictory bases, or a merged PR whose own base is NOT where this PR now
points — each is a divergence this tool cannot explain, and an unexplained divergence is the park the user
already knows how to answer. Only the exact GitHub shape migrates.

The verdict is printed as JSON on stdout and the EXIT CODE gates a caller's `$?`: 0 when the row now matches
its live base (`migrate`, or `no-change` when it already did), non-zero when it does not (`park`, or a write
that could not land). The fixture suite is the SIBLING `base-retarget-test.py`, this tool's executable
contract; `self-test` loads it by a `__file__`-relative path and FAILS LOUDLY if it is missing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from _gauntlet.modules import load_sibling
from _gauntlet.repository import repo_problem
from _gauntlet.testing import run_sibling_suite
from _gauntlet.view import field_problem

_HERE = Path(__file__).resolve().parent
SIBLING = _HERE / "base-retarget-test.py"      # the fixture suite — this tool's executable contract
LEDGER = _HERE / "ledger.py"                   # owns the row schema, both writes, and the park wording

L = load_sibling("base_retarget_ledger", _HERE, "ledger.py")

MIGRATE, PARK, NO_CHANGE = "migrate", "park", "no-change"

# The `gh pr list --json` fields this decision reads, and the page it reads them from. The fields are named
# ONCE and reused by the fetch and by the per-candidate validation, so a field added to one is added to both.
CANDIDATE_FIELDS = "number,headRefName,baseRefName,state,mergedAt"

# How many merged PRs from the recorded branch are considered. A branch that carried more than this many
# merged PRs is not a shape this decides: the extra pages are never fetched, so the newest merge could be
# outside the window, and a decision made on a window that might not hold the deciding row would be a guess.
# The cap is therefore a FAIL-CLOSED boundary, not a performance knob — a full page parks (see `decide`).
CANDIDATE_LIMIT = 50

# The MERGED state string GitHub reports for a merged PR, and the only state that explains a retarget. A
# CLOSED PR's branch is not merged anywhere, so a PR retargeted after it is not carrying reviewed content
# onto a base that contains its parent's work — that is a user change, and it parks.
MERGED_STATE = "MERGED"


class FetchError(RuntimeError):
    """The candidate list could not be obtained — never a reason to migrate."""


# --- pure decision surface ----------------------------------------------------

def _verdict(decision: str, reason: str, *, recorded: str, live: str,
             evidence: "dict | None" = None) -> dict:
    """One decision, in the shape every caller and fixture reads."""
    return {"decision": decision, "reason": reason, "recorded": recorded, "live": live,
            "evidence": evidence or {}}


def parent_candidates(candidates: object, recorded: str) -> "tuple[list[dict], str | None]":
    """The merged PRs whose HEAD is `recorded`, as `(parents, problem)` — a malformed list yields a problem.

    Validation is strict and TOTAL over the payload: the response must be a JSON array, every entry an object
    carrying each `CANDIDATE_FIELDS` name at the JSON type `gh` produces (`number` is an int, the rest
    strings). One malformed entry poisons the whole list rather than being skipped — a payload this tool does
    not understand is not evidence, and silently dropping the entry that would have DECIDED the question is
    exactly how a fail-closed check turns into a guess.

    `gh pr list --head` is a filter, not a guarantee: the entries are still checked against `recorded` here,
    so a response for another branch cannot decide this row's base.
    """
    if not isinstance(candidates, list):
        return [], f"candidate list is not a JSON array (got {type(candidates).__name__})"
    parents: "list[dict]" = []
    for index, entry in enumerate(candidates):
        problem = field_problem(entry, strings=("headRefName", "baseRefName", "state", "mergedAt"))
        if problem is not None:
            return [], f"candidate {index}: {problem}"
        if not isinstance(entry, dict):   # unreachable through `field_problem`, and never assumed
            return [], f"candidate {index}: view is not a JSON object (got {type(entry).__name__})"
        if "number" not in entry:
            return [], f"candidate {index}: missing field 'number'"
        # `bool` is a subclass of `int`, so a JSON `true` would otherwise pass as a PR number.
        if not isinstance(entry["number"], int) or isinstance(entry["number"], bool):
            return [], f"candidate {index}: field 'number' is not a JSON number"
        if entry["headRefName"] == recorded and entry["state"] == MERGED_STATE:
            parents.append(entry)
    return parents, None


def decide(recorded: str, live: str, candidates: object) -> dict:
    """Migrate or park, from the recorded base, the live base, and the merged PRs from the recorded branch.

    PURE — no I/O, no raising. The ONE shape that migrates is GitHub's own retarget:

        the most recently merged PR whose HEAD was `recorded` has `baseRefName == live`.

    Everything else parks, and each park names what was missing rather than restating the shared user-facing
    wording (`resolve` records that; this explains it to the transcript):

      * a same-name base is `no-change`, decided BEFORE any evidence is read — nothing moved, so nothing is
        owed. This is the ordinary case a caller reaches when it re-checks a row it already migrated;
      * a malformed candidate list, so no candidate is evidence;
      * a FULL page — the newest merge may be outside the window, so the deciding row may not be here;
      * no merged PR from the recorded branch at all: the branch was deleted, or its PR is still open or was
        closed unmerged. GitHub retargets on MERGE, so none of those explain where this PR now points;
      * several merged PRs from that branch whose newest merge is AMBIGUOUS — two merged at the same instant
        and they disagree about their own base, so "the base GitHub moved this PR to" has no single answer;
      * a newest merged parent whose own base is NOT `live`: GitHub would have moved this PR to the parent's
        base, so wherever it points came from somewhere else.
    """
    if recorded == live:
        return _verdict(NO_CHANGE, "the live base already equals the recorded base", recorded=recorded, live=live)
    parents, problem = parent_candidates(candidates, recorded)
    if problem is not None:
        return _verdict(PARK, f"the merged-PR evidence for {recorded!r} is malformed: {problem}",
                        recorded=recorded, live=live)
    if isinstance(candidates, list) and len(candidates) >= CANDIDATE_LIMIT:
        return _verdict(PARK, f"{recorded!r} returned a FULL page of {CANDIDATE_LIMIT} merged PRs, so the "
                              f"newest merge may be outside the window this read",
                        recorded=recorded, live=live)
    if not parents:
        return _verdict(PARK, f"no merged PR has head {recorded!r}, so nothing explains the retarget — "
                              f"GitHub retargets a PR when its base branch's PR MERGES",
                        recorded=recorded, live=live)
    newest = max(entry["mergedAt"] for entry in parents)
    latest = [entry for entry in parents if entry["mergedAt"] == newest]
    bases = {entry["baseRefName"] for entry in latest}
    numbers = sorted(entry["number"] for entry in latest)
    evidence = {"parent_prs": numbers, "merged_at": newest, "parent_bases": sorted(bases)}
    if len(bases) > 1:
        return _verdict(PARK, f"PRs {numbers} from {recorded!r} merged at the same instant onto different "
                              f"bases {sorted(bases)}, so the retarget target is ambiguous",
                        recorded=recorded, live=live, evidence=evidence)
    parent_base = bases.pop()
    if parent_base != live:
        return _verdict(PARK, f"PR {numbers[0]} merged {recorded!r} into {parent_base!r}, but this PR now "
                              f"targets {live!r} — the retarget did not come from that merge",
                        recorded=recorded, live=live, evidence=evidence)
    return _verdict(MIGRATE, f"GitHub retargeted this PR onto {live!r} when PR {numbers[0]} merged "
                             f"{recorded!r} into it", recorded=recorded, live=live, evidence=evidence)


# --- evidence ------------------------------------------------------------------

def fetch_candidates(recorded: str, *, repo: "str | None", cwd: "str | None",
                     candidates_json: "str | None") -> object:
    """The merged PRs whose head branch is `recorded`, from `gh` or from a RECORDED response file.

    Raises `FetchError` on anything that is not a parsed JSON payload — a caller turns that into a park,
    never a migrate. The branch name is passed as `--head=<value>` so a legal dash-leading branch name is a
    VALUE to `gh`, never parsed as one of its options.
    """
    if candidates_json is not None:
        try:
            return json.loads(Path(candidates_json).read_text(encoding="utf-8"))
        except Exception as exc:   # noqa: BLE001 — a recorded response that will not load is not evidence
            raise FetchError(f"could not read {candidates_json}: {type(exc).__name__}: {exc}") from exc
    argv = ["gh", "pr", "list", f"--head={recorded}", "--state", "merged",
            "--json", CANDIDATE_FIELDS, "--limit", str(CANDIDATE_LIMIT)]
    if repo:
        argv += ["--repo", repo]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False, cwd=cwd)  # noqa: S603
    except Exception as exc:   # noqa: BLE001 — a spawn that fails is not evidence either
        raise FetchError(f"could not run `gh pr list`: {type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        raise FetchError(f"`gh pr list` exited {proc.returncode}: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except Exception as exc:   # noqa: BLE001 — an unparseable response is not evidence
        raise FetchError(f"could not parse `gh pr list` output: {type(exc).__name__}: {exc}") from exc


# --- executor ------------------------------------------------------------------

def _ledger(ledger_file: str, cwd: "str | None", *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(LEDGER), "--file", ledger_file, *argv],   # noqa: S603
                          capture_output=True, text=True, check=False, cwd=cwd)


def recorded_base(ledger_file: str, pr: str) -> "tuple[str | None, str | None]":
    """The row's RECORDED base, as `(base, problem)` — resolved through `ledger.py`, never re-derived here.

    A caller hands in the LIVE base only. Reading the recorded side from the ledger is what makes the two
    impossible to swap, and it means the row's legacy header inheritance is resolved by the one accessor that
    owns it (`effective_base` via `require_effective_base`, which also fails closed on an unresolved base).
    """
    try:
        header, rows = L.load(Path(ledger_file))
    except Exception as exc:   # noqa: BLE001 — a store that will not load decides nothing
        return None, f"could not read {ledger_file}: {type(exc).__name__}: {exc}"
    row = L.find_row(rows, str(pr))
    if row is None:
        return None, f"no ledger row for pr {pr} — its recorded base cannot be resolved"
    return L.require_effective_base(header, row, str(pr))


def resolve(pr: str, live: str, ledger_file: str, *, repo: "str | None", project_root: "str | None",
            candidates_json: "str | None") -> int:
    """Decide and perform the ONE ledger write that follows. Exit 0 iff the row now matches its live base."""
    live = live.strip()
    if live in ("", "-"):
        print(json.dumps({"decision": PARK, "reason": f"--live {live!r} does not name a branch, so no "
                                                      f"divergence can be judged", "pr": pr}))
        return 1
    recorded, problem = recorded_base(ledger_file, pr)
    if problem is not None or recorded is None:
        print(json.dumps({"decision": PARK, "reason": str(problem), "pr": pr}))
        return 1
    if recorded == live:
        result = decide(recorded, live, [])
        print(json.dumps({**result, "pr": pr, "wrote": None}))
        return 0
    try:
        candidates = fetch_candidates(recorded, repo=repo, cwd=project_root, candidates_json=candidates_json)
    except FetchError as exc:
        # Evidence this tool could not obtain is not evidence AGAINST a retarget either — but it is not
        # evidence FOR one, and only evidence FOR one migrates. Park, and say the read failed.
        result = _verdict(PARK, f"the merged-PR evidence for {recorded!r} could not be read: {exc}",
                          recorded=recorded, live=live)
    else:
        result = decide(recorded, live, candidates)

    if result["decision"] == MIGRATE:
        proc = _ledger(ledger_file, project_root, "retarget", "--pr", str(pr),
                       "--from", recorded, "--to", live)
        if proc.returncode != 0:
            # The transition refused (a terminal row, another hold, a park on a different question). The row
            # keeps its recorded base and its open question; report it and gate the caller closed.
            print(json.dumps({**result, "pr": pr, "wrote": None,
                              "ledger_refusal": proc.stderr.strip(), "exit": proc.returncode}))
            return 1
        print(json.dumps({**result, "pr": pr, "wrote": "retarget", "row": json.loads(proc.stdout)}))
        return 0

    reason = L.BASE_CHANGE_PARK_REASON.format(recorded=recorded, live=live)
    proc = _ledger(ledger_file, project_root, "park", "--pr", str(pr), "--reason", reason)
    if proc.returncode == L.EXIT_STOP:
        # A park is ALREADY open on this row and its question is preserved — `park` never overwrites one.
        print(json.dumps({**result, "pr": pr, "wrote": None, "already_held": True}))
        return 1
    if proc.returncode != 0:
        print(json.dumps({**result, "pr": pr, "wrote": None, "ledger_refusal": proc.stderr.strip(),
                          "exit": proc.returncode}))
        return 1
    print(json.dumps({**result, "pr": pr, "wrote": "park", "parked_reason": reason,
                      "row": json.loads(proc.stdout)}))
    return 1


# --- self-test: the executable contract lives in the SIBLING module ------------

class SelfTestFailure(AssertionError):
    """A rule this file claims to enforce does not hold."""


def self_test() -> int:
    """Run every fixture in the sibling suite on the shared runner (`_gauntlet/testing.py`)."""
    return run_sibling_suite(SIBLING, "base_retarget_test", failure=SelfTestFailure,
                             subject="the base-retarget decider's contract")


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(description=next(iter((__doc__ or "").splitlines()), ""))
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("resolve", help="decide a live base divergence for one PR and perform the ledger "
                                       "write it implies (retarget or park)")
    r.add_argument("--pr", required=True, help="PR number (ledger row key)")
    r.add_argument("--live", required=True, help="the PR's LIVE baseRefName, as observed. The RECORDED base "
                                                 "is read from the ledger — never passed in")
    r.add_argument("--file", required=True, help="the ledger (state.jsonl) this row lives in")
    r.add_argument("--repo", help="owner/name (default: the current checkout's)")
    r.add_argument("--project-root", help="run `gh` and `ledger.py` with this as their working directory")
    r.add_argument("--candidates-json", help="a recorded `gh pr list` response — decide without calling gh")

    d = sub.add_parser("decide", help="the PURE decision, printed as JSON; reads no ledger and writes nothing")
    d.add_argument("--recorded", required=True, help="the base the row records")
    d.add_argument("--live", required=True, help="the PR's live baseRefName")
    d.add_argument("--candidates-json", required=True,
                   help="a recorded `gh pr list --head <recorded> --state merged` response")

    sub.add_parser("self-test", help="run every fixture (base-retarget-test.py)")

    args = p.parse_args(argv)

    if args.cmd == "self-test":
        return self_test()
    # An explicit --repo is interpolated into the `gh` argv this tool builds, so it is checked at the CLI
    # boundary before anything runs. `_gauntlet/repository.py` owns the check and its wording.
    if getattr(args, "repo", None) is not None:
        problem = repo_problem(args.repo)
        if problem is not None:
            print(json.dumps({"decision": PARK, "reason": problem}))
            return 1
    if args.cmd == "decide":
        try:
            candidates = fetch_candidates(args.recorded, repo=None, cwd=None,
                                          candidates_json=args.candidates_json)
        except FetchError as exc:
            print(json.dumps(_verdict(PARK, str(exc), recorded=args.recorded, live=args.live)))
            return 1
        result = decide(args.recorded, args.live, candidates)
        print(json.dumps(result))
        return 0 if result["decision"] in (MIGRATE, NO_CHANGE) else 1
    return resolve(args.pr, args.live, args.file, repo=args.repo, project_root=args.project_root,
                   candidates_json=args.candidates_json)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
