#!/usr/bin/env python3
"""Fixtures for `base-retarget.py` — the decider that separates GitHub's own retarget from every other one.

They live in a SIBLING file, and `base-retarget.py self-test` FAILS LOUDLY if it cannot load them.

EVERY FIXTURE HAS TEETH, and the ones that matter most are the PARKS. This tool is the only thing in the
campaign that can rewrite a live row's base without a human, so a fixture suite that only proved the happy
path would certify exactly the wrong half. Each park fixture is one shape that LOOKS like the stacked-PR
merge and is not — a base whose PR is still open, one closed unmerged, one that merged somewhere else, a
deleted branch with no PR at all, a FORK branch that merely shares the name, a merge of that name from
BEFORE this PR existed, one stamped at the very SECOND this PR was created, one whose stamp is the NEWEST
only once the offsets are read as instants, and the one that fits EVERY bound on the parent and still moved
nothing (the user hand-retargeted the PR first, and the eligible merge only followed) — and each asserts
that the recorded base SURVIVES.

The `resolve` fixtures drive the real CLI against a REAL ledger built through `ledger.py` itself, with all
three `gh` reads replaced by recorded responses (`--candidates-json`, `--pr-created-at`,
`--base-events-json`). What they assert is the
ledger AFTER the call, through the accessor — not the tool's own JSON, which would let a decider that
printed `migrate` and wrote nothing pass.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from _gauntlet.modules import load_sibling
from _gauntlet.testing import capture_cli, checker

OWNER = Path(__file__).resolve().parent / "base-retarget.py"

M = load_sibling("base_retarget_owner", OWNER.parent, OWNER.name)

check = checker(M.SelfTestFailure)


# The PR under decision was created BEFORE every fixture merge below, so a fixture says nothing about the
# recency bound unless it sets its own instant. `t_history_before_this_pr_parks` is where that bound is
# pinned and `t_merge_in_the_creation_second_parks` is where its STRICT edge is, and `pr_entry`'s default
# merge is deliberately later than this.
CREATED = "2025-12-31T00:00:00Z"


def pr_entry(number: int, head: str, base: str, *, state: str = "MERGED",
             merged_at: str = "2026-08-18T10:00:00Z", cross_repo: bool = False) -> dict:
    """One `gh pr list --json <CANDIDATE_FIELDS>` entry — same-repository unless `cross_repo` says otherwise."""
    return {"number": number, "headRefName": head, "baseRefName": base, "state": state,
            "mergedAt": merged_at, "isCrossRepository": cross_repo}


# Every fixture merge below is stamped no later than this, so the DEFAULT base move is GitHub's OWN
# retarget of this PR, from the recorded base to the live one, at an instant AFTER the merge that explains
# it. A fixture that pins the CAUSAL EVENT itself sets its own move.
MOVED = "2026-08-18T10:00:02Z"


def auto_move(old: str, new: str, *, at: str = MOVED) -> dict:
    """GitHub's OWN retarget of this PR — the one item type that explains a migrate."""
    return {"__typename": M.GITHUB_RETARGET, "createdAt": at, "oldBase": old, "newBase": new}


def hand_move(old: str, new: str, *, at: str = MOVED) -> dict:
    """A PERSON set this PR's base. User intent, and the item type the merge evidence cannot rule out."""
    return {"__typename": "BaseRefChangedEvent", "createdAt": at, "previousRefName": old,
            "currentRefName": new}


def failed_move(old: str, new: str, *, at: str = MOVED) -> dict:
    """GitHub TRIED to retarget this PR and could not — so this item did not move it anywhere."""
    return {"__typename": "AutomaticBaseChangeFailedEvent", "createdAt": at, "oldBase": old, "newBase": new}


def _run_ledger(ledger: Path, *argv: str) -> subprocess.CompletedProcess:
    proc = subprocess.run([sys.executable, str(M.LEDGER), "--file", str(ledger), *argv],
                          capture_output=True, text=True, check=False)
    check(proc.returncode == 0, f"ledger {' '.join(argv)} failed: {proc.stderr.strip()}")
    return proc


def _field(ledger: Path, pr: str, field: str) -> str:
    return _run_ledger(ledger, "get", "--pr", pr, "--field", field).stdout.strip()


def _row(ledger: Path, pr: str = "36", *, base: str = "fix-a", status: str = "in_review") -> None:
    """A live row for `pr` on `base`, built through the REAL accessor — the shape adoption writes."""
    _run_ledger(ledger, "header", "set", "run_id", "t")
    _run_ledger(ledger, "add-row", "--pr", pr, "--head-sha", "a" * 40, "--base-branch", base)
    _run_ledger(ledger, "set", "--pr", pr, "--status", status)


def _candidates(root: Path, entries: "list[dict]") -> str:
    path = root / "candidates.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return str(path)


def _moves(root: Path, entries: "list[dict]") -> str:
    path = root / "base-moves.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return str(path)


def expect(recorded: str, live: str, candidates: object, decision: str, *, created: str = CREATED,
           moves: object = None) -> dict:
    if moves is None:
        moves = [auto_move(recorded, live)]
    got = M.decide(recorded, live, candidates, created, moves)
    check(got["decision"] == decision, f"expected {decision!r} for {recorded!r} -> {live!r}, got {got!r}")
    return got


# --- the ONE shape that migrates -----------------------------------------------

def t_merged_parent_migrates():
    """The stacked-PR case: PR 35 merged `fix-a` into `main`, so GitHub moved this PR onto `main`."""
    got = expect("fix-a", "main", [pr_entry(35, "fix-a", "main")], M.MIGRATE)
    check(got["evidence"]["parent_prs"] == [35], f"the deciding parent PR must be named, got {got!r}")
    check("35" in got["reason"] and "main" in got["reason"],
          f"the reason must name the parent PR and the new base, got {got['reason']!r}")


def t_newest_merge_decides():
    """A reused branch: the NEWEST merge from it is the one GitHub retargeted against, not the older one."""
    got = expect("fix-a", "v4",
                 [pr_entry(10, "fix-a", "main", merged_at="2026-01-01T00:00:00Z"),
                  pr_entry(35, "fix-a", "v4", merged_at="2026-08-18T10:00:00Z")], M.MIGRATE)
    check(got["evidence"]["parent_prs"] == [35], f"the NEWEST merge must decide, got {got!r}")


def t_unrelated_entries_are_filtered():
    """`--head` is a filter, not a guarantee: entries for another branch cannot decide this row's base."""
    expect("fix-a", "main",
           [pr_entry(35, "fix-a", "main"), pr_entry(34, "other", "release")], M.MIGRATE)
    # The same response WITHOUT the matching entry explains nothing.
    expect("fix-a", "main", [pr_entry(34, "other", "main")], M.PARK)


def t_requested_fields_are_the_validated_fields():
    """The fetch and the per-candidate check read ONE field list, so a field can never be requested without
    being validated (or validated without being requested)."""
    requested = M.CANDIDATE_FIELDS.split(",")
    named = [*M.CANDIDATE_NUMBERS, *M.CANDIDATE_STRINGS, *M.CANDIDATE_BOOLS]
    check(requested == named, f"CANDIDATE_FIELDS must be derived from the typed field lists, got {requested!r}")
    check("isCrossRepository" in M.CANDIDATE_BOOLS,
          "the fork bound needs the head repository identity in the request")


def t_same_base_is_no_change():
    """A live base equal to the recorded one is decided BEFORE any evidence is read — nothing moved."""
    got = expect("main", "main", "not even a list", M.NO_CHANGE)
    check(got["evidence"] == {}, f"a no-change decision reads no evidence, got {got!r}")


# --- everything else PARKS ------------------------------------------------------

def t_open_parent_parks():
    """The base branch's PR is still OPEN: GitHub retargets on MERGE, so nothing explains the move."""
    got = expect("fix-a", "main", [pr_entry(35, "fix-a", "main", state="OPEN")], M.PARK)
    check("no merged PR" in got["reason"], f"the park must say the evidence was absent, got {got['reason']!r}")


def t_closed_unmerged_parent_parks():
    """The base branch's PR was CLOSED without merging: its content is nowhere, so this is a user change."""
    expect("fix-a", "main", [pr_entry(35, "fix-a", "main", state="CLOSED")], M.PARK)


def t_deleted_base_with_no_pr_parks():
    """The base branch simply vanished — no PR at all. An absent ref cannot establish WHY it is absent."""
    expect("fix-a", "main", [], M.PARK)


def t_parent_merged_elsewhere_parks():
    """The parent merged into `v3`, but this PR now targets `main`. GitHub would have moved it to `v3`, so
    wherever it points came from somewhere else — the one shape most likely to be waved through."""
    got = expect("fix-a", "main", [pr_entry(35, "fix-a", "v3")], M.PARK)
    check("v3" in got["reason"] and "main" in got["reason"],
          f"the park must name both bases so the user can see the mismatch, got {got['reason']!r}")


def t_ambiguous_simultaneous_merges_park():
    """Two merges of the same branch at the SAME instant onto different bases: no single retarget target."""
    got = expect("fix-a", "main",
                 [pr_entry(35, "fix-a", "main"), pr_entry(36, "fix-a", "v3")], M.PARK)
    check(got["evidence"]["parent_bases"] == ["main", "v3"], f"both bases must be reported, got {got!r}")


def t_identical_simultaneous_merges_still_migrate():
    """The same instant is only ambiguous when the bases DISAGREE — two merges onto one base still decide."""
    expect("fix-a", "main", [pr_entry(35, "fix-a", "main"), pr_entry(36, "fix-a", "main")], M.MIGRATE)


def t_mixed_offset_stamps_are_ordered_as_instants():
    """ONE INSTANT HAS MANY SPELLINGS, and both the newest-merge choice and its tie-group are over the
    INSTANTS, never over the stamp text. `instant()` accepts any offset, so `2026-01-01T00:00:00-01:00` IS
    `2026-01-01T01:00:00Z` — while as text `2026-01-01T00:30:00Z` sorts above them both. Ordering the raw
    stamps hands the decision to a merge that is NOT the newest, and splits a tie-group that is really one
    moment; either way a live row's base would move on an ordering this tool does not hold."""
    created = "2026-01-01T00:00:00Z"
    # The newest merge is PR 1 (01:00Z, spelled with a -01:00 offset) and it went to `release`, so `main` is
    # unexplained. As text, PR 2's `00:30` stamp would win and migrate this row onto `main`.
    got = expect("feature-a", "main",
                 [pr_entry(1, "feature-a", "release", merged_at="2026-01-01T00:00:00-01:00"),
                  pr_entry(2, "feature-a", "main", merged_at="2026-01-01T00:30:00Z")], M.PARK,
                 created=created)
    check(got["evidence"]["parent_prs"] == [1] and "release" in got["reason"],
          f"the newest merge BY INSTANT must decide, got {got!r}")
    # The same two merges with the offset-spelled one going to `main` is the ordinary stacked-PR migrate —
    # the bound orders instants, it does not distrust an offset.
    got = expect("feature-a", "main",
                 [pr_entry(1, "feature-a", "main", merged_at="2026-01-01T00:00:00-01:00"),
                  pr_entry(2, "feature-a", "release", merged_at="2026-01-01T00:30:00Z")], M.MIGRATE,
                 created=created)
    check(got["evidence"]["parent_prs"] == [1], f"the newest merge BY INSTANT must decide, got {got!r}")
    # Two SPELLINGS of the same instant are ONE tie-group, so disagreeing bases are the ambiguity park.
    got = expect("feature-a", "main",
                 [pr_entry(1, "feature-a", "main", merged_at="2026-01-01T01:00:00Z"),
                  pr_entry(2, "feature-a", "release", merged_at="2026-01-01T00:00:00-01:00")], M.PARK,
                 created=created)
    check(got["evidence"]["parent_prs"] == [1, 2] and got["evidence"]["parent_bases"] == ["main", "release"],
          f"one instant spelled two ways is ONE tie-group, got {got!r}")
    check("ambiguous" in got["reason"], f"the park must name the ambiguity, got {got['reason']!r}")


def t_fork_parent_parks():
    """A FORK PR whose head branch is NAMED like the recorded base is not a parent. `gh pr list --head`
    matches the branch NAME across repositories, and merging a fork's `fix-a` retargets nothing that is based
    on THIS repository's `fix-a` — so a merged fork PR alone explains nothing, and anyone may open one."""
    got = expect("fix-a", "main", [pr_entry(35, "fix-a", "main", cross_repo=True)], M.PARK)
    check("THIS repository" in got["reason"],
          f"the park must say the merge was not this repository's branch, got {got['reason']!r}")
    # The same fork PR alongside a REAL same-repo parent decides normally: the fork is dropped, not poison.
    got = expect("fix-a", "main",
                 [pr_entry(35, "fix-a", "main", cross_repo=True), pr_entry(36, "fix-a", "main")], M.MIGRATE)
    check(got["evidence"]["parent_prs"] == [36], f"only the same-repo parent may decide, got {got!r}")


def t_history_before_this_pr_parks():
    """A RECREATED branch: `fix-a` merged into `main` long ago, was recreated, and this PR was later
    hand-retargeted. GitHub retargets the PRs that are OPEN when a merge happens, so a merge that predates
    this PR is history, not the cause of where it now points."""
    old = [pr_entry(7, "fix-a", "main", merged_at="2019-01-02T03:04:05Z")]
    got = expect("fix-a", "main", old, M.PARK, created="2026-05-01T00:00:00Z")
    check("strictly AFTER this PR" in got["reason"] and "2026-05-01" in got["reason"],
          f"the park must name the bound it applied, got {got['reason']!r}")
    check(got["evidence"]["stale_parent_prs"] == [7], f"the merge it refused to use must be named, got {got!r}")
    # The SAME shape with the merge after this PR was created is the ordinary stacked-PR migrate.
    expect("fix-a", "main", old, M.MIGRATE, created="2018-01-01T00:00:00Z")


def t_merge_in_the_creation_second_parks():
    """THE BOUND IS STRICT. `gh` truncates both `mergedAt` and `createdAt` to whole SECONDS, so an EQUAL pair
    is consistent with the parent merging FIRST and this PR being opened later inside that same second — the
    hand-retarget shape, which GitHub moved nothing for. Equality therefore cannot establish that this PR was
    OPEN for the merge, and a bound that cannot be applied parks instead of rewriting a live row's base."""
    same = "2026-05-01T00:00:00Z"
    got = expect("fix-a", "main", [pr_entry(7, "fix-a", "main", merged_at=same)], M.PARK, created=same)
    check("same SECOND" in got["reason"], f"the park must name why equality decides nothing, got {got!r}")
    check(got["evidence"]["stale_parent_prs"] == [7], f"the merge it refused to use must be named, got {got!r}")
    # ONE second later is the ordinary stacked-PR migrate: the orderings no longer collide.
    got = expect("fix-a", "main", [pr_entry(7, "fix-a", "main", merged_at="2026-05-01T00:00:01Z")], M.MIGRATE,
                 created=same)
    check(got["evidence"]["parent_prs"] == [7], f"the eligible parent must decide, got {got!r}")


def t_older_merge_cannot_shadow_a_recent_one():
    """A stale merge of a recreated name does not suppress the REAL parent that merged after this PR."""
    got = expect("fix-a", "main",
                 [pr_entry(7, "fix-a", "main", merged_at="2019-01-02T03:04:05Z"),
                  pr_entry(35, "fix-a", "main", merged_at="2026-06-01T00:00:00Z")], M.MIGRATE,
                 created="2026-05-01T00:00:00Z")
    check(got["evidence"]["parent_prs"] == [35], f"the eligible parent must decide, got {got!r}")


def t_unanchored_instants_park():
    """An instant this tool cannot order is a bound it cannot apply, and an unapplied bound is not a passed
    one: an unparseable or OFFSET-LESS `createdAt`, and the same for a candidate's `mergedAt`."""
    for created in ("not-a-time", "2026-05-01T00:00:00", "", "-"):
        got = expect("fix-a", "main", [pr_entry(35, "fix-a", "main")], M.PARK, created=created)
        check("creation instant" in got["reason"], f"the park must name the unusable instant, got {got!r}")
    for merged_at in ("yesterday", "2026-08-18T10:00:00"):
        got = expect("fix-a", "main", [pr_entry(35, "fix-a", "main", merged_at=merged_at)], M.PARK)
        check("malformed" in got["reason"], f"an unorderable mergedAt is malformed evidence, got {got!r}")


def t_full_page_parks():
    """A FULL page means the newest merge may be outside the window, so the deciding row may not be here."""
    entries = [pr_entry(n, "fix-a", "main") for n in range(M.CANDIDATE_LIMIT)]
    got = expect("fix-a", "main", entries, M.PARK)
    check(str(M.CANDIDATE_LIMIT) in got["reason"], f"the park must name the window, got {got['reason']!r}")
    # One below the cap is a complete answer and decides normally.
    expect("fix-a", "main", entries[:-1], M.MIGRATE)


def t_malformed_evidence_parks():
    """A payload this tool does not understand is not evidence — every malformation parks, none is skipped."""
    for candidates in ({"not": "a list"},
                       ["not an object"],
                       [{"headRefName": "fix-a", "baseRefName": "main", "state": "MERGED"}],
                       [{"number": 35, "headRefName": "fix-a", "baseRefName": "main",
                         "state": "MERGED", "mergedAt": 17}],
                       [{"number": True, "headRefName": "fix-a", "baseRefName": "main",
                         "state": "MERGED", "mergedAt": "2026-08-18T10:00:00Z",
                         "isCrossRepository": False}],
                       # The head-repository identity is part of the shape: absent, or a STRING that merely
                       # reads like a bool, is a payload whose fork bound cannot be applied.
                       [{"number": 35, "headRefName": "fix-a", "baseRefName": "main",
                         "state": "MERGED", "mergedAt": "2026-08-18T10:00:00Z"}],
                       [{"number": 35, "headRefName": "fix-a", "baseRefName": "main",
                         "state": "MERGED", "mergedAt": "2026-08-18T10:00:00Z",
                         "isCrossRepository": "false"}]):
        got = expect("fix-a", "main", candidates, M.PARK)
        check("malformed" in got["reason"], f"a malformed payload must say so, got {got['reason']!r}")


def t_one_bad_entry_poisons_the_list():
    """A malformed entry does NOT get skipped past a good one: dropping it silently is how a fail-closed
    check becomes a guess about the entry it could not read."""
    expect("fix-a", "main", [pr_entry(35, "fix-a", "main"), {"number": 36}], M.PARK)



# --- the CAUSAL EVENT: an ELIGIBLE merge is not the merge that MOVED this PR ------

def t_hand_retarget_before_the_parent_merge_parks():
    """THE SHAPE EVERY BOUND ON THE PARENT LETS THROUGH, and the one this tool exists to refuse. The PR is
    opened on `v3`, the USER hand-retargets it to `main`, and only THEN does the `v3` parent (base `main`)
    merge. Same repository, MERGED, head `v3`, merged strictly after this PR was created, parent base
    `main` — every fetched field of the stacked-PR case — and that merge moved nothing, because this PR's
    base was already `main` at the merge instant. Only this PR's OWN timeline separates the two."""
    parent = [pr_entry(100, "v3", "main", merged_at="2026-08-02T00:00:00Z")]
    created = "2026-08-01T00:00:00Z"
    got = expect("v3", "main", parent, M.PARK, created=created,
                 moves=[hand_move("v3", "main", at="2026-08-01T12:00:00Z")])
    check("BaseRefChangedEvent" in got["reason"],
          f"the park must name what actually moved the base, got {got['reason']!r}")
    check(got["evidence"]["base_move"]["type"] == "BaseRefChangedEvent",
          f"the refused move must be reported as evidence, got {got!r}")
    # The SAME parent merge, with GitHub's own retarget as the newest move, is the stacked-PR migrate.
    got = expect("v3", "main", parent, M.MIGRATE, created=created,
                 moves=[auto_move("v3", "main", at="2026-08-02T00:00:02Z")])
    check(got["evidence"]["base_move"]["type"] == M.GITHUB_RETARGET,
          f"the deciding move must be reported as evidence, got {got!r}")


def t_failed_automatic_retarget_parks():
    """GitHub TRIED to retarget this PR and could not, so that item moved it nowhere. Whatever put it on
    the live base, GitHub is not claiming it did."""
    got = expect("fix-a", "main", [pr_entry(35, "fix-a", "main")], M.PARK,
                 moves=[failed_move("fix-a", "main")])
    check("AutomaticBaseChangeFailedEvent" in got["reason"],
          f"the park must name the item it refused, got {got['reason']!r}")


def t_no_base_move_parks():
    """Something put this PR on a base it was not opened with and GitHub's own timeline does not say what.
    An ABSENT event cannot show that the eligible merge is what moved it."""
    got = expect("fix-a", "main", [pr_entry(35, "fix-a", "main")], M.PARK, moves=[])
    check("no timeline item" in got["reason"],
          f"the park must say the causal event was absent, got {got['reason']!r}")


def t_retarget_of_other_refs_parks():
    """GitHub's own retarget, but of a DIFFERENT pair of refs — an EARLIER retarget of this PR, not the
    move from the recorded base to the live one."""
    for move in (auto_move("v9", "main"), auto_move("fix-a", "v9")):
        got = expect("fix-a", "main", [pr_entry(35, "fix-a", "main")], M.PARK, moves=[move])
        check("not from 'fix-a' to 'main'" in got["reason"],
              f"the park must name both pairs so the mismatch is visible, got {got['reason']!r}")


def t_retarget_before_the_deciding_merge_parks():
    """GitHub's own retarget of the right refs, stamped BEFORE the merge that is supposed to explain it:
    they are not the same event, so that merge is not what moved this PR."""
    parent = [pr_entry(35, "fix-a", "main", merged_at="2026-08-18T10:00:00Z")]
    got = expect("fix-a", "main", parent, M.PARK,
                 moves=[auto_move("fix-a", "main", at="2026-08-18T09:59:59Z")])
    check("BEFORE PR 35 merged" in got["reason"],
          f"the park must name the ordering it applied, got {got['reason']!r}")
    # Stamped AT the merge instant is NOT before it, and the bound here is deliberately not strict: `gh`
    # truncates both stamps to whole seconds, and GitHub's retarget follows its merge closely enough to
    # land inside the same one. The strict bound is the one against `createdAt`, where an equal pair is
    # genuinely ambiguous; here it is not — nothing else could have moved the base in that second.
    expect("fix-a", "main", parent, M.MIGRATE, moves=[auto_move("fix-a", "main", at="2026-08-18T10:00:00Z")])


def t_malformed_base_move_parks():
    """A base-move payload this tool cannot read is not evidence, exactly as a malformed candidate is not —
    including a payload carrying MORE items than the newest-only read asked for, where picking one would be
    a guess about which."""
    for moves in ({"not": "a list"},
                  ["not an object"],
                  [{"createdAt": MOVED, "oldBase": "fix-a", "newBase": "main"}],
                  [{"__typename": M.GITHUB_RETARGET, "oldBase": "fix-a", "newBase": "main"}],
                  [{"__typename": M.GITHUB_RETARGET, "createdAt": MOVED, "newBase": "main"}],
                  [{"__typename": M.GITHUB_RETARGET, "createdAt": "yesterday", "oldBase": "fix-a",
                    "newBase": "main"}],
                  [{"__typename": "ReadyForReviewEvent", "createdAt": MOVED}],
                  [auto_move("fix-a", "main"), hand_move("fix-a", "main")]):
        got = expect("fix-a", "main", [pr_entry(35, "fix-a", "main")], M.PARK, moves=moves)
        check("base-move" in got["reason"], f"a malformed base move must say so, got {got['reason']!r}")


def t_queried_move_types_are_the_understood_types():
    """The `gh api graphql` request is BUILT from `BASE_MOVE_TYPES`, so a timeline type can never be asked
    for without being understood — or understood without being asked for — and the read never takes more
    items than `base_move` accepts."""
    for name, (old, new) in M.BASE_MOVE_TYPES.items():
        check(M._timeline_enum(name) in M.BASE_MOVE_QUERY,
              f"{name} must be in the itemTypes filter, got {M.BASE_MOVE_QUERY!r}")
        check(f"... on {name}{{createdAt {old} {new}}}" in M.BASE_MOVE_QUERY,
              f"{name} must select its own createdAt and ref fields, got {M.BASE_MOVE_QUERY!r}")
    check(f"last:{M.BASE_MOVE_LIMIT}," in M.BASE_MOVE_QUERY,
          f"the read must ask for the newest {M.BASE_MOVE_LIMIT}, got {M.BASE_MOVE_QUERY!r}")
    check(M.GITHUB_RETARGET in M.BASE_MOVE_TYPES,
          "the one item type that migrates must be one of the types the read understands")


# --- `resolve`: the decision, the ledger write, and the exit code ----------------

def t_resolve_migrates_the_row():
    """A migrate REWRITES the recorded base and voids everything the old base authorized, through the real
    ledger: `required_set` and `base_ok_sha` back to their defaults, `ci` back to pending — while the review
    tally SURVIVES, because a retarget moves no content."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ledger = root / "state.jsonl"
        _row(ledger)
        # The gate state a live row accumulates, EARNED through the real doors: a base-preflight stamp for
        # the head, two landed verdicts on it (a hand-raised tally is refused), a green ci and a read
        # required set. All of it describes the OLD base.
        _run_ledger(ledger, "base-ok", "--pr", "36", "--head-sha", "a" * 40)
        for _ in range(2):
            _run_ledger(ledger, "verdict", "--pr", "36", "--head-sha", "a" * 40, "--verdict", "satisfied")
        _run_ledger(ledger, "set", "--pr", "36", "--ci", "green",
                    "--required-set", "declared:[\"build\"]", "--settled-strikes", "3")
        code, out, err = capture_cli(M.main, ["resolve", "--pr", "36", "--live", "main",
                                              "--file", str(ledger),
                                              "--pr-created-at", CREATED, "--candidates-json",
                                              _candidates(root, [pr_entry(35, "fix-a", "main")]),
                                              "--base-events-json",
                                              _moves(root, [auto_move("fix-a", "main")])])
        check(code == 0, f"a migrate must exit 0 so the caller continues (stderr: {err})")
        result = json.loads(out)
        check(result["decision"] == M.MIGRATE and result["wrote"] == "retarget",
              f"resolve must report the write it performed, got {result!r}")
        check(_field(ledger, "36", "base_branch") == "main", "the recorded base must move to the live base")
        check(_field(ledger, "36", "reviews_ok") == "2",
              "a retarget moves no content, so the review tally must SURVIVE it")
        check(_field(ledger, "36", "required_set") == "-",
              "the required-check set is derived from the base and must be re-read for the new one")
        check(_field(ledger, "36", "base_ok_sha") == "-",
              "the base-preflight proceed was decided against the OLD base and must be voided")
        check(_field(ledger, "36", "ci") == "pending", "ci must be re-derived against the new required set")
        check(_field(ledger, "36", "settled_strikes") == "0", "the liveness counters must reset with the ci")


def t_resolve_releases_a_park_for_this_base_change():
    """The whole point of the transition: a row a fail-closed consumer PARKED on this exact base change is
    released by the migrate, so the user is never asked a question the evidence already answered."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ledger = root / "state.jsonl"
        _row(ledger)
        _run_ledger(ledger, "park", "--pr", "36", "--reason",
                    M.L.BASE_CHANGE_PARK_REASON.format(recorded="fix-a", live="main"))
        code, out, err = capture_cli(M.main, ["resolve", "--pr", "36", "--live", "main",
                                              "--file", str(ledger),
                                              "--pr-created-at", CREATED, "--candidates-json",
                                              _candidates(root, [pr_entry(35, "fix-a", "main")]),
                                              "--base-events-json",
                                              _moves(root, [auto_move("fix-a", "main")])])
        check(code == 0, f"a migrate over an explained park must exit 0 (stderr: {err})")
        check(json.loads(out)["wrote"] == "retarget", "the release happens through the retarget transition")
        check(_field(ledger, "36", "status") == "in_review", "the row must be released back to the live status")
        check(_field(ledger, "36", "ci_reason") == "-", "the answered question must be cleared")
        check(_field(ledger, "36", "base_branch") == "main", "and the base must have moved")


def t_resolve_parks_an_unexplained_retarget():
    """An unexplained retarget parks with the SHARED machine-blocker wording — the same question the user
    already knows how to rule on — and exits non-zero so the caller acts on nothing else this pass."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ledger = root / "state.jsonl"
        _row(ledger)
        code, out, err = capture_cli(M.main, ["resolve", "--pr", "36", "--live", "main",
                                              "--file", str(ledger), "--pr-created-at", CREATED,
                                              "--candidates-json", _candidates(root, []),
                                              "--base-events-json",
                                              _moves(root, [auto_move("fix-a", "main")])])
        check(code != 0, f"an unexplained retarget must gate the caller closed (stderr: {err})")
        result = json.loads(out)
        check(result["wrote"] == "park", f"resolve must record the park, got {result!r}")
        check(_field(ledger, "36", "status") == "awaiting-user", "the row must be parked on the user")
        check(_field(ledger, "36", "ci_reason") == "base changed from fix-a to main; not supported mid-run",
              "the park must record the EXACT shared wording every base-change park uses")
        check(_field(ledger, "36", "base_branch") == "fix-a", "and the recorded base must SURVIVE the park")


def t_resolve_keeps_an_open_question_it_did_not_ask():
    """A row already parked on a DIFFERENT question keeps it: `park` never overwrites an open question, and
    a retarget may not answer one."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ledger = root / "state.jsonl"
        _row(ledger)
        _run_ledger(ledger, "park", "--pr", "36", "--reason", "CI has been stalled for 3 refetches")
        code, out, err = capture_cli(M.main, ["resolve", "--pr", "36", "--live", "main",
                                              "--file", str(ledger),
                                              "--pr-created-at", CREATED, "--candidates-json",
                                              _candidates(root, [pr_entry(35, "fix-a", "main")]),
                                              "--base-events-json",
                                              _moves(root, [auto_move("fix-a", "main")])])
        check(code != 0, f"a row held on another question must gate closed (stderr: {err})")
        check("ledger_refusal" in json.loads(out) or json.loads(out).get("already_held") is True,
              f"the refusal to answer another question must be reported, got {out!r}")
        check(_field(ledger, "36", "ci_reason") == "CI has been stalled for 3 refetches",
              "the OPEN question must survive untouched")
        check(_field(ledger, "36", "base_branch") == "fix-a", "and no base may be written underneath it")


def t_resolve_parks_a_fork_named_parent():
    """END TO END through the real ledger: a merged FORK PR whose head branch is named like the recorded base
    parks the row and leaves the recorded base standing. Anyone can open that PR from a fork, so a migrate on
    it would put a row's base under an outside writer's control."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ledger = root / "state.jsonl"
        _row(ledger)
        code, out, err = capture_cli(M.main, ["resolve", "--pr", "36", "--live", "main",
                                              "--file", str(ledger), "--pr-created-at", CREATED,
                                              "--candidates-json",
                                              _candidates(root, [pr_entry(35, "fix-a", "main",
                                                                          cross_repo=True)]),
                                              "--base-events-json",
                                              _moves(root, [auto_move("fix-a", "main")])])
        check(code != 0, f"a fork-named parent must gate the caller closed (stderr: {err})")
        check(json.loads(out)["wrote"] == "park", f"the row must be parked on the user, got {out!r}")
        check(_field(ledger, "36", "base_branch") == "fix-a", "and the recorded base must SURVIVE")


def t_resolve_parks_a_recreated_branch_hand_retarget():
    """END TO END through the real ledger: `fix-a` merged into `main` years ago, the branch was recreated,
    this row was adopted on it, and the PR was HAND-retargeted to `main`. The historical merge still carries
    the matching name, and the row must park on the user rather than migrate on it."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ledger = root / "state.jsonl"
        _row(ledger)
        code, out, err = capture_cli(M.main, ["resolve", "--pr", "36", "--live", "main",
                                              "--file", str(ledger),
                                              "--pr-created-at", "2026-05-01T00:00:00Z",
                                              "--candidates-json",
                                              _candidates(root, [pr_entry(7, "fix-a", "main",
                                                                          merged_at="2019-01-02T03:04:05Z")]),
                                              "--base-events-json",
                                              _moves(root, [auto_move("fix-a", "main")])])
        check(code != 0, f"a merge older than the PR must gate the caller closed (stderr: {err})")
        check(json.loads(out)["wrote"] == "park", f"the row must be parked on the user, got {out!r}")
        check(_field(ledger, "36", "ci_reason") == "base changed from fix-a to main; not supported mid-run",
              "the park must record the shared machine-blocker wording")
        check(_field(ledger, "36", "base_branch") == "fix-a", "and the recorded base must SURVIVE")


def t_resolve_parks_a_hand_retarget_the_parent_merge_only_followed():
    """END TO END through the real ledger, on the exact sequence the merge evidence alone waves through: the
    row records `v3`, a fail-closed door has ALREADY parked it on `base changed from v3 to main`, and the
    user hand-retargeted the PR BEFORE the `v3` parent merged into `main`. The park is a question the user
    still owes an answer to, so it must SURVIVE — and so must the recorded base."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ledger = root / "state.jsonl"
        _row(ledger, "42", base="v3")
        _run_ledger(ledger, "park", "--pr", "42", "--reason",
                    M.L.BASE_CHANGE_PARK_REASON.format(recorded="v3", live="main"))
        code, out, err = capture_cli(M.main, ["resolve", "--pr", "42", "--live", "main",
                                              "--file", str(ledger),
                                              "--pr-created-at", "2026-08-01T00:00:00Z",
                                              "--candidates-json",
                                              _candidates(root, [pr_entry(100, "v3", "main",
                                                                          merged_at="2026-08-02T00:00:00Z")]),
                                              "--base-events-json",
                                              _moves(root, [hand_move("v3", "main",
                                                                      at="2026-08-01T12:00:00Z")])])
        check(code != 0, f"a hand retarget must gate the caller closed (stderr: {err})")
        check(json.loads(out)["decision"] == M.PARK, f"a hand retarget must never migrate, got {out!r}")
        check(_field(ledger, "42", "base_branch") == "v3", "the recorded base must SURVIVE")
        check(_field(ledger, "42", "status") == "awaiting-user", "and the row must stay parked on the user")
        check(_field(ledger, "42", "ci_reason") == "base changed from v3 to main; not supported mid-run",
              "with the question the user still owes an answer to left standing")


def t_resolve_refuses_a_terminal_row():
    """A merged row's base records what it merged INTO; nothing retargets it, and the tool must not park it."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ledger = root / "state.jsonl"
        _row(ledger)
        _run_ledger(ledger, "set", "--pr", "36", "--status", "merged")
        code, out, err = capture_cli(M.main, ["resolve", "--pr", "36", "--live", "main",
                                              "--file", str(ledger),
                                              "--pr-created-at", CREATED, "--candidates-json",
                                              _candidates(root, [pr_entry(35, "fix-a", "main")]),
                                              "--base-events-json",
                                              _moves(root, [auto_move("fix-a", "main")])])
        check(code != 0, f"a terminal row must gate closed (stderr: {err})")
        check("ledger_refusal" in json.loads(out), "the transition's refusal must be reported, not swallowed")
        check(_field(ledger, "36", "base_branch") == "fix-a", "a terminal row's base must not move")
        check(_field(ledger, "36", "status") == "merged", "and its status must not move either")


def t_resolve_reads_the_recorded_base_from_the_ledger():
    """A caller passes only the LIVE base. A row whose live base already matches is `no-change`: no evidence
    is read, nothing is written, and the caller continues."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ledger = root / "state.jsonl"
        _row(ledger, base="main")
        code, out, err = capture_cli(M.main, ["resolve", "--pr", "36", "--live", "main",
                                              "--file", str(ledger)])
        check(code == 0, f"a row already on its live base must continue (stderr: {err})")
        result = json.loads(out)
        check(result["decision"] == M.NO_CHANGE and result["wrote"] is None,
              f"no divergence means no write, got {result!r}")


def t_resolve_parks_a_legacy_row_by_its_inherited_base():
    """An OLD row carries no explicit base and inherits the legacy header. The comparison — and the recorded
    park wording — must use THAT inherited base, resolved through the accessor."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ledger = root / "state.jsonl"
        _run_ledger(ledger, "header", "set", "run_id", "t")
        _run_ledger(ledger, "header", "set", "base_branch", "v3")
        _run_ledger(ledger, "add-row", "--pr", "36", "--head-sha", "a" * 40)
        code, _, err = capture_cli(M.main, ["resolve", "--pr", "36", "--live", "main",
                                            "--file", str(ledger), "--pr-created-at", CREATED,
                                            "--candidates-json", _candidates(root, []),
                                            "--base-events-json",
                                            _moves(root, [auto_move("v3", "main")])])
        check(code != 0, f"an unexplained retarget of a legacy row parks (stderr: {err})")
        check(_field(ledger, "36", "ci_reason") == "base changed from v3 to main; not supported mid-run",
              "the inherited header base must be the one named in the park")


def t_resolve_fails_closed_on_unreadable_evidence():
    """Evidence that could not be read is not evidence FOR a retarget, and only evidence FOR one migrates."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ledger = root / "state.jsonl"
        _row(ledger)
        code, out, err = capture_cli(M.main, ["resolve", "--pr", "36", "--live", "main",
                                              "--file", str(ledger),
                                              "--candidates-json", str(root / "absent.json")])
        check(code != 0, f"an unreadable read must gate closed (stderr: {err})")
        check(json.loads(out)["decision"] == M.PARK, "an unreadable read parks, never migrates")
        check(_field(ledger, "36", "base_branch") == "fix-a", "and the recorded base survives")


def t_resolve_refuses_an_unnamed_live_base():
    """A `-` or empty `--live` names no branch, so no divergence can be judged — and nothing is written."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ledger = root / "state.jsonl"
        _row(ledger)
        for live in ("-", "  "):
            code, out, _ = capture_cli(M.main, ["resolve", "--pr", "36", "--live", live,
                                                "--file", str(ledger)])
            check(code != 0, f"--live {live!r} must fail closed")
            check(json.loads(out)["decision"] == M.PARK, f"--live {live!r} must not migrate")
        check(_field(ledger, "36", "status") == "in_review", "and no ledger write may have happened")


def t_resolve_refuses_a_malformed_repo():
    """An explicit `--repo` is interpolated into the `gh` argv, so it is checked at the CLI boundary."""
    code, out, _ = capture_cli(M.main, ["resolve", "--pr", "36", "--live", "main", "--file", "/nonexistent",
                                        "--repo", "not a repo"])
    check(code != 0, "a malformed --repo must fail closed")
    check("--repo" in json.loads(out)["reason"], "the refusal must name the flag it rejected")


def t_decide_cli_is_pure():
    """`decide` is the pure surface: it reads a recorded response, writes no ledger, and gates on `$?`."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        path = _candidates(root, [pr_entry(35, "fix-a", "main")])
        moves = _moves(root, [auto_move("fix-a", "main")])
        code, out, _ = capture_cli(M.main, ["decide", "--recorded", "fix-a", "--live", "main",
                                            "--pr-created-at", CREATED, "--candidates-json", path,
                                            "--base-events-json", moves])
        check(code == 0 and json.loads(out)["decision"] == M.MIGRATE, f"expected a migrate, got {out!r}")
        code, out, _ = capture_cli(M.main, ["decide", "--recorded", "fix-a", "--live", "v9",
                                            "--pr-created-at", CREATED, "--candidates-json", path,
                                            "--base-events-json", moves])
        check(code != 0 and json.loads(out)["decision"] == M.PARK, f"expected a park, got {out!r}")


def t_park_wording_is_the_shared_constant():
    """The park wording is the LEDGER's constant, not a second spelling of it: every base-change park in the
    campaign records the same question, and `retarget`'s release matches against that same string."""
    check(M.L.BASE_CHANGE_PARK_REASON.format(recorded="a", live="b")
          == "base changed from a to b; not supported mid-run",
          "the shared park wording changed — every site that routes on it must be swept with it")


CASES = [
    ("merged-parent-migrates", "a parent PR that merged the recorded base into the live base migrates",
     t_merged_parent_migrates),
    ("newest-merge-decides", "a reused branch is decided by its NEWEST merge", t_newest_merge_decides),
    ("filters-other-branches", "entries for another head branch cannot decide this row",
     t_unrelated_entries_are_filtered),
    ("same-base-no-change", "a live base equal to the recorded one reads no evidence", t_same_base_is_no_change),
    ("request-matches-validation", "the requested candidate fields are the validated ones",
     t_requested_fields_are_the_validated_fields),
    ("fork-parent-parks", "a merged FORK PR sharing the branch name is never a parent", t_fork_parent_parks),
    ("history-before-pr-parks", "a merge older than the PR itself cannot have retargeted it",
     t_history_before_this_pr_parks),
    ("same-second-merge-parks", "a merge stamped in the PR's own creation second cannot have retargeted it",
     t_merge_in_the_creation_second_parks),
    ("recent-parent-still-decides", "a stale merge does not shadow the parent that merged after the PR",
     t_older_merge_cannot_shadow_a_recent_one),
    ("unanchored-instants-park", "a createdAt or mergedAt this tool cannot order parks",
     t_unanchored_instants_park),
    ("open-parent-parks", "a base branch whose PR is still open parks", t_open_parent_parks),
    ("closed-parent-parks", "a base branch whose PR was closed unmerged parks", t_closed_unmerged_parent_parks),
    ("no-pr-parks", "a vanished base branch with no PR parks", t_deleted_base_with_no_pr_parks),
    ("merged-elsewhere-parks", "a parent that merged into a DIFFERENT base parks",
     t_parent_merged_elsewhere_parks),
    ("ambiguous-parks", "simultaneous merges onto different bases park", t_ambiguous_simultaneous_merges_park),
    ("ambiguous-only-when-bases-differ", "simultaneous merges onto ONE base still migrate",
     t_identical_simultaneous_merges_still_migrate),
    ("mixed-offset-instants", "stamps spelled with different offsets order and tie-group as INSTANTS",
     t_mixed_offset_stamps_are_ordered_as_instants),
    ("full-page-parks", "a full candidate page parks; one below the cap decides", t_full_page_parks),
    ("malformed-parks", "every malformed candidate payload parks", t_malformed_evidence_parks),
    ("bad-entry-poisons", "a malformed entry is never skipped past a good one", t_one_bad_entry_poisons_the_list),
    ("hand-retarget-parks", "a hand retarget the parent merge only FOLLOWED parks, on identical merge evidence",
     t_hand_retarget_before_the_parent_merge_parks),
    ("failed-auto-retarget-parks", "an automatic retarget GitHub could not complete moved nothing",
     t_failed_automatic_retarget_parks),
    ("no-base-move-parks", "an absent base-move event cannot show the merge moved this PR", t_no_base_move_parks),
    ("retarget-other-refs-parks", "GitHub's retarget of a DIFFERENT pair of refs is not this move",
     t_retarget_of_other_refs_parks),
    ("retarget-before-merge-parks", "a retarget stamped before the deciding merge is a different event",
     t_retarget_before_the_deciding_merge_parks),
    ("malformed-base-move-parks", "every malformed base-move payload parks", t_malformed_base_move_parks),
    ("queried-types-understood", "the graphql request is built from the understood timeline types",
     t_queried_move_types_are_the_understood_types),
    ("resolve-migrates", "a migrate rewrites the base and voids what the old base authorized",
     t_resolve_migrates_the_row),
    ("resolve-releases-park", "a migrate releases a park opened for this same base change",
     t_resolve_releases_a_park_for_this_base_change),
    ("resolve-parks", "an unexplained retarget parks with the shared wording and keeps the recorded base",
     t_resolve_parks_an_unexplained_retarget),
    ("resolve-keeps-other-question", "a row held on another question keeps it and moves no base",
     t_resolve_keeps_an_open_question_it_did_not_ask),
    ("resolve-fork-parent", "a merged fork PR sharing the branch name parks the row",
     t_resolve_parks_a_fork_named_parent),
    ("resolve-recreated-branch", "a recreated branch's historical merge parks the row",
     t_resolve_parks_a_recreated_branch_hand_retarget),
    ("resolve-hand-retarget", "a hand retarget an eligible parent merge only followed keeps its park",
     t_resolve_parks_a_hand_retarget_the_parent_merge_only_followed),
    ("resolve-terminal-refused", "a terminal row is neither retargeted nor parked",
     t_resolve_refuses_a_terminal_row),
    ("resolve-reads-recorded-base", "the recorded base comes from the ledger, never from the caller",
     t_resolve_reads_the_recorded_base_from_the_ledger),
    ("resolve-legacy-row", "a legacy row is compared and parked by its inherited header base",
     t_resolve_parks_a_legacy_row_by_its_inherited_base),
    ("resolve-unreadable-evidence", "evidence that could not be read parks",
     t_resolve_fails_closed_on_unreadable_evidence),
    ("resolve-unnamed-live-base", "an empty or `-` --live is refused before any write",
     t_resolve_refuses_an_unnamed_live_base),
    ("resolve-malformed-repo", "a malformed --repo is refused at the CLI boundary",
     t_resolve_refuses_a_malformed_repo),
    ("decide-cli-pure", "the decide subcommand reads a recorded response and gates on $?", t_decide_cli_is_pure),
    ("shared-park-wording", "the park wording is the ledger's shared constant",
     t_park_wording_is_the_shared_constant),
]
