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
        [--project-root <dir>] [--candidates-json <path>] [--pr-created-at <iso>]
    base-retarget.py decide --recorded fix-a --live main --candidates-json <path> --pr-created-at <iso>
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

THE DECISION IS PURE AND THE EVIDENCE IS A VALUE. `decide()` takes the candidate list and this PR's own
creation instant and returns the verdict; `resolve` establishes both with TWO targeted reads — one
`gh pr list --head <recorded> --state merged` for the candidates, one `gh pr view <pr> --json createdAt` for
the instant. Recorded answers can be handed in with `--candidates-json` and `--pr-created-at` instead, so
every rule below is pinned offline with no `gh` and no network. Neither query is a widening of the run
snapshot: one asks about ONE branch — the base this row records — and one about THIS row's own PR, and
neither re-opens which of the RUN's PRs are live (repo `CLAUDE.md` owns why the run snapshot stays
`--state open`).

FAIL CLOSED, ALWAYS TOWARDS THE PARK. A fetch that fails, a malformed candidate, no merged PR from THIS
REPOSITORY's recorded branch, one whose merge is not stamped strictly AFTER this PR was created, several
with contradictory bases, or a merged PR whose own base is NOT where this PR now points — each is a
divergence this tool cannot explain, and an unexplained divergence is the park the user already knows how to
answer. Only the exact GitHub shape migrates.

TWO BOUNDS KEEP A BRANCH NAME FROM STANDING IN FOR THE MERGE THAT MOVED THIS PR. A head branch NAME is
neither unique nor single-use, so a name match alone is not the event:

  * `gh pr list --head <name>` also returns FORK PRs, whose head branch may be named exactly like a branch
    of this repository. Merging a fork's `v3` retargets nothing based on THIS repository's `v3`, so a
    cross-repository candidate is never a parent (`isCrossRepository`);
  * a branch name can be RECREATED after an earlier merge of the same name. GitHub retargets the PRs that
    are OPEN at the instant of the merge, so a merge that does not postdate this PR's own `createdAt` cannot
    be shown to have moved it — the merge is history, not the cause, and the row parks. That bound is STRICT
    because `gh` stamps both instants at SECOND precision: an EQUAL pair is consistent with the parent
    merging FIRST and this PR being opened later in the same second, which GitHub retargets nothing for.

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
from datetime import datetime
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

# The `gh pr list --json` fields this decision reads, split by the JSON type `gh` produces so the fetch and
# the per-candidate validation read ONE list: `CANDIDATE_FIELDS` is DERIVED from them, so a field added here
# is requested AND checked, and the two can never drift. `number` keeps its own check (`bool` is an `int`).
CANDIDATE_NUMBERS = ("number",)
CANDIDATE_STRINGS = ("headRefName", "baseRefName", "state", "mergedAt")
# WHICH REPOSITORY THE HEAD BRANCH LIVES IN. `--head <name>` filters on the branch NAME only and returns
# FORK PRs too, whose head branch can be named exactly like one of this repository's — merging a fork's
# `v3` retargets nothing based on this repository's `v3`, so a cross-repository candidate is not a parent.
CANDIDATE_BOOLS = ("isCrossRepository",)
CANDIDATE_FIELDS = ",".join((*CANDIDATE_NUMBERS, *CANDIDATE_STRINGS, *CANDIDATE_BOOLS))

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
    """A piece of evidence could not be obtained — never a reason to migrate."""


def instant(value: object) -> "datetime | None":
    """The UTC instant `value` names, or `None` when it does not name one — PURE, and never raising.

    `gh` reports both timestamps this tool compares (`mergedAt`, `createdAt`) as ISO-8601 with a `Z` suffix.
    A value that will not parse, or that carries NO offset, returns `None`: an instant with no anchor cannot
    be ordered against another one, and a comparison this tool cannot make is a park, never a migrate.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


# --- pure decision surface ----------------------------------------------------

def _verdict(decision: str, reason: str, *, recorded: str, live: str,
             evidence: "dict | None" = None) -> dict:
    """One decision, in the shape every caller and fixture reads."""
    return {"decision": decision, "reason": reason, "recorded": recorded, "live": live,
            "evidence": evidence or {}}


def parent_candidates(candidates: object, recorded: str) -> "tuple[list[dict], str | None]":
    """The merged SAME-REPOSITORY PRs whose HEAD is `recorded`, as `(parents, problem)` — a malformed list
    yields a problem.

    Validation is strict and TOTAL over the payload: the response must be a JSON array, every entry an object
    carrying each `CANDIDATE_FIELDS` name at the JSON type `gh` produces (`number` is an int,
    `isCrossRepository` a bool, the rest strings). One malformed entry poisons the whole list rather than
    being skipped — a payload this tool does not understand is not evidence, and silently dropping the entry
    that would have DECIDED the question is exactly how a fail-closed check turns into a guess.

    `gh pr list --head` is a filter on the branch NAME, not a guarantee about the branch:

      * the entries are still checked against `recorded` here, so a response for another branch cannot decide
        this row's base;
      * a CROSS-REPOSITORY entry is dropped. `--head <name>` matches a FORK's head branch of the same name,
        and merging a fork branch retargets nothing that is based on THIS repository's branch, so such an
        entry is not a parent — and, being an ordinary fork PR anyone may open, must never become one.
    """
    if not isinstance(candidates, list):
        return [], f"candidate list is not a JSON array (got {type(candidates).__name__})"
    parents: "list[dict]" = []
    for index, entry in enumerate(candidates):
        problem = field_problem(entry, strings=CANDIDATE_STRINGS, bools=CANDIDATE_BOOLS)
        if problem is not None:
            return [], f"candidate {index}: {problem}"
        if not isinstance(entry, dict):   # unreachable through `field_problem`, and never assumed
            return [], f"candidate {index}: view is not a JSON object (got {type(entry).__name__})"
        if "number" not in entry:
            return [], f"candidate {index}: missing field 'number'"
        # `bool` is a subclass of `int`, so a JSON `true` would otherwise pass as a PR number.
        if not isinstance(entry["number"], int) or isinstance(entry["number"], bool):
            return [], f"candidate {index}: field 'number' is not a JSON number"
        if (entry["headRefName"] == recorded and entry["state"] == MERGED_STATE
                and entry["isCrossRepository"] is False):
            parents.append(entry)
    return parents, None


def decide(recorded: str, live: str, candidates: object, created: object) -> dict:
    """Migrate or park, from the recorded base, the live base, the merged PRs from the recorded branch, and
    this PR's own `createdAt`.

    PURE — no I/O, no raising. The ONE shape that migrates is GitHub's own retarget:

        the most recently merged SAME-REPOSITORY PR whose HEAD was `recorded`, merged STRICTLY after
        this PR was created, has `baseRefName == live`.

    Both bounds on the parent are there because a head branch NAME identifies neither one repository nor one
    branch instance: `--head` also matches FORK branches of the same name, and a name can be recreated after
    an earlier merge of it. A candidate that fails either bound is a name collision, not the event.

    Everything else parks, and each park names what was missing rather than restating the shared user-facing
    wording (`resolve` records that; this explains it to the transcript):

      * a same-name base is `no-change`, decided BEFORE any evidence is read — nothing moved, so nothing is
        owed. This is the ordinary case a caller reaches when it re-checks a row it already migrated;
      * a `created` that names no anchored instant — the recency bound below cannot be applied, and an
        unapplied bound is not a passed one;
      * a malformed candidate list — a wrong-shaped entry, or a parent whose `mergedAt` cannot be ordered —
        so no candidate is evidence;
      * a FULL page — the newest merge may be outside the window, so the deciding row may not be here;
      * no merged SAME-REPOSITORY PR from the recorded branch at all: the branch was deleted, its PR is still
        open or was closed unmerged, or every match was a fork's like-named branch. GitHub retargets on the
        MERGE of THIS repository's branch, so none of those explain where this PR now points;
      * every such merge stamped NO LATER than this PR's creation: GitHub retargets the PRs that are OPEN
        when the merge happens, so a merge older than this PR is history that cannot have moved it, and a
        merge stamped at the very same SECOND cannot show this PR was open for it either (see the strict
        bound below). This is the recreated-branch shape — a name merged long ago, recreated, and
        hand-retargeted since;
      * several merged PRs from that branch whose newest merge is AMBIGUOUS — two merged at the same instant
        (the same MOMENT, however each stamp is spelled) and they disagree about their own base, so "the base
        GitHub moved this PR to" has no single answer;
      * a newest merged parent whose own base is NOT `live`: GitHub would have moved this PR to the parent's
        base, so wherever it points came from somewhere else.
    """
    if recorded == live:
        return _verdict(NO_CHANGE, "the live base already equals the recorded base", recorded=recorded, live=live)
    born = instant(created)
    if born is None:
        return _verdict(PARK, f"this PR's creation instant {created!r} is not an ISO-8601 timestamp with an "
                              f"offset, so a merge cannot be ordered against it",
                        recorded=recorded, live=live)
    parents, problem = parent_candidates(candidates, recorded)
    if problem is not None:
        return _verdict(PARK, f"the merged-PR evidence for {recorded!r} is malformed: {problem}",
                        recorded=recorded, live=live)
    if isinstance(candidates, list) and len(candidates) >= CANDIDATE_LIMIT:
        return _verdict(PARK, f"{recorded!r} returned a FULL page of {CANDIDATE_LIMIT} merged PRs, so the "
                              f"newest merge may be outside the window this read",
                        recorded=recorded, live=live)
    if not parents:
        return _verdict(PARK, f"no merged PR of THIS repository has head {recorded!r}, so nothing explains "
                              f"the retarget — GitHub retargets a PR when its base branch's PR MERGES",
                        recorded=recorded, live=live)
    timed: "list[tuple[dict, datetime]]" = []
    for entry in parents:
        moment = instant(entry["mergedAt"])
        if moment is None:
            return _verdict(PARK, f"the merged-PR evidence for {recorded!r} is malformed: PR "
                                  f"{entry['number']} carries mergedAt {entry['mergedAt']!r}, which is not "
                                  f"an ISO-8601 timestamp with an offset",
                            recorded=recorded, live=live)
        timed.append((entry, moment))
    # Paired with its entry rather than keyed by PR number: a payload is not trusted to hold each number once.
    # THE BOUND IS STRICT, and the equal case is REACHABLE, not theoretical: `gh` reports both `mergedAt`
    # and `createdAt` truncated to whole SECONDS, and PRs opened by one script share a stamp routinely. An
    # EQUAL pair therefore cannot separate "this PR was already open when the parent merged" (GitHub
    # retargeted it) from "the parent merged first and this PR was created later in that same second"
    # (GitHub retargeted nothing, so the divergence is a hand-retarget). A bound that cannot be applied is
    # not a passed one, and this tool parks rather than migrating a live row on a coin flip.
    recent = [(entry, moment) for entry, moment in timed if moment > born]
    if not recent:
        stale = sorted(entry["number"] for entry in parents)
        return _verdict(PARK, f"no merged PR from {recorded!r} ({stale}) is stamped strictly AFTER this PR "
                              f"was created ({created}), and GitHub retargets the PRs that are OPEN when a "
                              f"merge happens — an older merge is history that did not move this one, and a "
                              f"merge stamped in the same SECOND cannot show this PR was open for it",
                        recorded=recorded, live=live,
                        evidence={"stale_parent_prs": stale, "pr_created_at": created})
    # THE NEWEST MERGE AND ITS TIE-GROUP ARE CHOSEN ON THE PARSED INSTANTS, NEVER ON THE `mergedAt` TEXT.
    # `instant()` accepts ANY offset, so ONE moment has many spellings: `2026-01-01T01:00:00Z` and
    # `2026-01-01T00:00:00-01:00` name the same instant, and `2026-01-01T00:30:00Z` sorts above BOTH as text
    # while being the older merge. Ordering the strings would therefore hand the decision to a merge that is
    # not the newest, and split a tie-group that is really one instant — both of which decide a live row's
    # base on an ordering this tool does not hold. The datetimes built above are the ordering.
    newest = max(moment for _, moment in recent)
    latest = sorted((entry for entry, moment in recent if moment == newest),
                    key=lambda entry: entry["number"])
    bases = {entry["baseRefName"] for entry in latest}
    numbers = [entry["number"] for entry in latest]
    # The stamp is reported as the deciding parent SPELLED it, so the evidence quotes the payload verbatim.
    evidence = {"parent_prs": numbers, "merged_at": latest[0]["mergedAt"], "parent_bases": sorted(bases),
                "pr_created_at": created}
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


# The `gh pr view --json` field the recency bound reads, and nothing else: this tool judges ONE fact about
# the row's own PR. `body` and every other attacker-influenced field stay unfetched and unparsed.
PR_VIEW_FIELDS = "createdAt"


def fetch_created(pr: str, *, repo: "str | None", cwd: "str | None",
                  pr_created_at: "str | None") -> str:
    """This PR's own `createdAt`, from `gh` or from a RECORDED value — the anchor of the recency bound.

    Raises `FetchError` on anything that is not a payload carrying that field as a string; a caller turns
    that into a park, never a migrate. The bound is only as trustworthy as the instant it is applied to, so
    an unreadable or wrong-shaped view decides nothing.
    """
    if pr_created_at is not None:
        return pr_created_at
    argv = ["gh", "pr", "view", str(pr), "--json", PR_VIEW_FIELDS]
    if repo:
        argv += ["--repo", repo]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False, cwd=cwd)  # noqa: S603
    except Exception as exc:   # noqa: BLE001 — a spawn that fails is not evidence
        raise FetchError(f"could not run `gh pr view`: {type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        raise FetchError(f"`gh pr view` exited {proc.returncode}: {proc.stderr.strip()}")
    try:
        view = json.loads(proc.stdout)
    except Exception as exc:   # noqa: BLE001 — an unparseable response is not evidence
        raise FetchError(f"could not parse `gh pr view` output: {type(exc).__name__}: {exc}") from exc
    problem = field_problem(view, strings=("createdAt",))
    if problem is not None:
        raise FetchError(f"`gh pr view` returned no usable creation instant: {problem}")
    return str(view["createdAt"])


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
            candidates_json: "str | None", pr_created_at: "str | None") -> int:
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
        # No divergence: `decide` answers before it reads any evidence, so neither read is owed here.
        result = decide(recorded, live, [], "")
        print(json.dumps({**result, "pr": pr, "wrote": None}))
        return 0
    try:
        candidates = fetch_candidates(recorded, repo=repo, cwd=project_root, candidates_json=candidates_json)
        created = fetch_created(pr, repo=repo, cwd=project_root, pr_created_at=pr_created_at)
    except FetchError as exc:
        # Evidence this tool could not obtain is not evidence AGAINST a retarget either — but it is not
        # evidence FOR one, and only evidence FOR one migrates. Park, and say the read failed.
        result = _verdict(PARK, f"the retarget evidence for {recorded!r} could not be read: {exc}",
                          recorded=recorded, live=live)
    else:
        result = decide(recorded, live, candidates, created)

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
    r.add_argument("--pr-created-at", help="a recorded `gh pr view --json createdAt` value — decide without "
                                           "calling gh")

    d = sub.add_parser("decide", help="the PURE decision, printed as JSON; reads no ledger and writes nothing")
    d.add_argument("--recorded", required=True, help="the base the row records")
    d.add_argument("--live", required=True, help="the PR's live baseRefName")
    d.add_argument("--candidates-json", required=True,
                   help="a recorded `gh pr list --head <recorded> --state merged` response")
    d.add_argument("--pr-created-at", required=True,
                   help="the PR's own `createdAt` — a merged parent older than it retargeted nothing")

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
        result = decide(args.recorded, args.live, candidates, args.pr_created_at)
        print(json.dumps(result))
        return 0 if result["decision"] in (MIGRATE, NO_CHANGE) else 1
    return resolve(args.pr, args.live, args.file, repo=args.repo, project_root=args.project_root,
                   candidates_json=args.candidates_json, pr_created_at=args.pr_created_at)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
