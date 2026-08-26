#!/usr/bin/env python3
"""Fixtures for `base-preflight.py` — the base-currency decider.

They live in a SIBLING file, and `base-preflight.py self-test` FAILS LOUDLY if it cannot load them.

EVERY FIXTURE HAS TEETH. It asserts the EXACT verdict AND, where the wording is load-bearing, the EXACT
reason — a suite that only checked `verdict == "rebase-first"` would pass against a decider that returned the
wrong reason, and the reason is what the driver acts on. There is one fixture PER `mergeStateStatus` value so
the mapping is pinned TOTALLY over the enum, plus unrecognised-value fixtures that pin the park catch-all.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from _gauntlet import repository as REPO_MOD
from _gauntlet.gitfixture import FIXTURE_EMAIL, GitFixture
from _gauntlet.modules import load_sibling
from _gauntlet.testing import (capture_cli, checker, deeply_nested_json, gh_writing,
                               hostile_json_responses, legacy_view_error_cases)

OWNER = Path(__file__).resolve().parent / "base-preflight.py"


M = load_sibling("base_preflight_owner", OWNER.parent, OWNER.name)


def view(*, mergeable="MERGEABLE", mergeStateStatus="CLEAN", baseRefName="main") -> dict:
    # `baseRefName` is the PR's LIVE base: `check --file` compares it with the row's effective base and refuses
    # a retarget. `decide()` ignores it, so the pure-decide fixtures below are unaffected by its presence.
    return {"mergeable": mergeable, "mergeStateStatus": mergeStateStatus, "baseRefName": baseRefName}


check = checker(M.SelfTestFailure)


def expect(v: dict, verdict: str, reason: "str | None" = None) -> None:
    got = M.decide(v)
    check(got["verdict"] == verdict, f"expected verdict {verdict!r}, got {got!r}")
    if reason is not None:
        check(got["reason"] == reason, f"expected reason {reason!r}, got {got['reason']!r}")


# --- one fixture PER mergeStateStatus value (mergeable defaults to MERGEABLE) --------------------
# The four enum-screen states advance to the graph check; DIRTY/BEHIND demand a rebase; UNKNOWN re-polls.
# Together they cover every value in MERGE_STATE_STATUS_VALUES, so the mapping is pinned TOTALLY over the
# enum.

def t_clean_proceeds():
    expect(view(mergeStateStatus="CLEAN"), "proceed", "GitHub merge state permits base check")


def t_has_hooks_proceeds():
    expect(view(mergeStateStatus="HAS_HOOKS"), "proceed", "GitHub merge state permits base check")


def t_unstable_proceeds():
    # UNSTABLE is about a non-passing/still-running CHECK, not Git ancestry, so it reaches the graph check.
    expect(view(mergeStateStatus="UNSTABLE"), "proceed", "GitHub merge state permits base check")


def t_blocked_proceeds():
    # BLOCKED is about branch-protection/permissions, not Git ancestry, so it reaches the graph check.
    expect(view(mergeStateStatus="BLOCKED"), "proceed", "GitHub merge state permits base check")


def t_dirty_rebases():
    expect(view(mergeStateStatus="DIRTY"), "rebase-first", "conflicts with base — rebase before reviewing/fixing")


def t_behind_reaches_the_enum_screen():
    """BEHIND does NOT block a review. It is a base that ADVANCED, not one that conflicts, and the rebase it
    calls for is owed at the MERGE (`REVIEWABLE_STATES` owns why). `decide` therefore passes it to the graph
    check exactly like CLEAN; what the probe SEES is then recorded, never turned into a refusal.

    THE MUTATION PIN: put `BEHIND` back on a `rebase-first` rule and this goes red — which is the whole
    serialized-drain change, so it must not be reintroduced by accident.
    """
    expect(view(mergeStateStatus="BEHIND"), "proceed", "GitHub merge state permits base check")


def t_unknown_mergestate_rechecks():
    expect(view(mergeStateStatus="UNKNOWN"), "recheck", "mergeability not computed yet — re-poll")


def t_every_mergestate_value_is_mapped():
    # TOTALITY, mechanically: every value the schema declares for mergeStateStatus resolves to a verdict
    # (never a crash), and to one of the three legal verdicts. The per-value fixtures above pin WHICH; this
    # pins that NONE is left unmapped.
    for value in M.MERGE_STATE_STATUS_VALUES:
        got = M.decide(view(mergeStateStatus=value))
        check(got["verdict"] in (M.PROCEED, M.REBASE_FIRST, M.RECHECK, M.PARK),
              f"mergeStateStatus={value!r} produced a non-verdict {got!r}")


# --- mergeable enum -----------------------------------------------------------

def t_conflicting_rebases():
    # CONFLICTING is decided on .mergeable alone; even a CLEAN merge state cannot clear it.
    expect(view(mergeable="CONFLICTING", mergeStateStatus="CLEAN"), "rebase-first",
           "conflicts with base — rebase before reviewing/fixing")


def t_unknown_mergeable_rechecks():
    expect(view(mergeable="UNKNOWN", mergeStateStatus="CLEAN"), "recheck",
           "mergeability not computed yet — re-poll")


# --- the totality catch-all ---------------------------------------------------

def t_unrecognised_mergestate_value_parks():
    expect(view(mergeStateStatus="FROZEN"), "park", "unknown merge state FROZEN — park")


def t_unrecognised_mergeable_value_parks():
    expect(view(mergeable="WOBBLY"), "park", "unknown mergeable value WOBBLY — park")


# --- cross-enum precedence: UNKNOWN / unrecognised WINS over a recognised rebase state ------------
# A view with a recognised CONFLICTING/DIRTY/BEHIND on ONE half and an UNKNOWN or unrecognised value on the
# OTHER must NOT be steered to `rebase-first` on the half we recognise — the uncomputed half re-polls and
# the unclassified half parks. These pin the ordering: fail-safe BEFORE act.

def t_conflicting_with_unknown_mergestate_rechecks():
    # CONFLICTING mergeable but mergeStateStatus not yet computed — re-poll, do NOT rebase on half a view.
    expect(view(mergeable="CONFLICTING", mergeStateStatus="UNKNOWN"), "recheck",
           "mergeability not computed yet — re-poll")


def t_conflicting_with_unrecognised_mergestate_parks():
    expect(view(mergeable="CONFLICTING", mergeStateStatus="FROZEN"), "park",
           "unknown merge state FROZEN — park")


def t_dirty_with_unknown_mergeable_rechecks():
    # DIRTY merge state but mergeable not yet computed — the uncomputed half wins over the DIRTY rebase state.
    expect(view(mergeable="UNKNOWN", mergeStateStatus="DIRTY"), "recheck",
           "mergeability not computed yet — re-poll")


def t_behind_with_unknown_mergeable_rechecks():
    # BEHIND merge state but mergeable not yet computed — the uncomputed half wins over the BEHIND rebase state.
    expect(view(mergeable="UNKNOWN", mergeStateStatus="BEHIND"), "recheck",
           "mergeability not computed yet — re-poll")


# --- CLI: a recorded view makes `check` testable without gh --------------------

def t_cli_missing_ancestry_rechecks():
    with tempfile.TemporaryDirectory() as d:
        vjson = Path(d) / "view.json"
        vjson.write_text(json.dumps(view()), encoding="utf-8")
        code, out, err = capture_cli(M.main, ["check", "--pr", "9", "--view-json", str(vjson)])
        check(code != 0, f"a CLEAN view without ancestry evidence must fail closed (stderr: {err})")
        check(json.loads(out) == {
            "verdict": "recheck",
            "reason": "could not verify base ancestry: base ancestry requires --worktree and --base",
        }, f"the CLI should demand base ancestry before proceeding, got {out!r}")


def t_cli_ancestry_spawn_failure_rechecks():
    """A Git process that cannot start must fail closed through the structured ancestry recheck."""
    def boom(*_args, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "git")

    real_run = M.subprocess.run
    setattr(M.subprocess, "run", boom)
    try:
        with tempfile.TemporaryDirectory() as d:
            vjson = Path(d) / "view.json"
            vjson.write_text(json.dumps(view()), encoding="utf-8")
            code, out, err = capture_cli(
                M.main,
                ["check", "--pr", "9", "--view-json", str(vjson), "--worktree", d, "--base", "main"],
            )
    finally:
        setattr(M.subprocess, "run", real_run)

    check(code != 0, f"a Git spawn failure must exit non-zero (fail closed), got {code} (stderr: {err})")
    check(err == "", f"a Git spawn failure must NOT print a traceback, got stderr {err!r}")
    result = json.loads(out)
    check(result["verdict"] == "recheck",
          f"a Git spawn failure must decide recheck, never proceed, got {result!r}")
    check(result["reason"].startswith("could not verify base ancestry:"),
          f"the reason must name the failed ancestry check, got {result!r}")


def t_cli_rebase_first():
    with tempfile.TemporaryDirectory() as d:
        vjson = Path(d) / "view.json"
        vjson.write_text(json.dumps(view(mergeStateStatus="DIRTY")), encoding="utf-8")
        code, out, err = capture_cli(M.main, ["check", "--pr", "9", "--view-json", str(vjson)])
        check(code != 0, f"a `rebase-first` verdict must exit non-zero so a caller can gate on $? (stderr: {err})")
        result = json.loads(out)
        check(result["verdict"] == "rebase-first",
              f"a DIRTY view must decide rebase-first, got {result!r}")
        check(result["reason"] == "conflicts with base — rebase before reviewing/fixing",
              f"the rebase-first reason drifted, got {result['reason']!r}")


def t_cli_malformed():
    # A valid JSON object MISSING mergeStateStatus — decide() would KeyError on it; the boundary fails closed
    # to a structured recheck with a NON-ZERO exit and NO traceback, never `proceed`.
    with tempfile.TemporaryDirectory() as d:
        vjson = Path(d) / "view.json"
        vjson.write_text(json.dumps({"mergeable": "MERGEABLE"}), encoding="utf-8")
        code, out, err = capture_cli(M.main, ["check", "--pr", "9", "--view-json", str(vjson)])
        check(code != 0, f"a malformed view must exit non-zero (fail closed), got {code} (stderr: {err})")
        result = json.loads(out)
        check(result["verdict"] == "recheck",
              f"a malformed view must decide recheck, never proceed, got {result!r}")
        check(result["reason"].startswith("malformed PR view:"),
              f"the reason must name the malformed view, got {result['reason']!r}")
        check("mergeStateStatus" in result["reason"],
              f"the reason must say which field is missing, got {result['reason']!r}")


def t_cli_bad_project_root_fails_closed():
    # An invalid --project-root makes subprocess.run raise OSError (NotADirectoryError/FileNotFoundError)
    # BEFORE any returncode exists. That must be caught and turned into a fail-closed `recheck` with a
    # NON-ZERO exit and NO traceback — never proceed, never crash. No --view-json, so load_view takes the
    # gh/subprocess path; the bad cwd trips it before gh is ever consulted.
    bad_root = Path(tempfile.gettempdir()) / "base-preflight-no-such-dir-xyz-000"
    check(not bad_root.exists(), f"the test's bogus --project-root must not exist: {bad_root}")
    code, out, err = capture_cli(
        M.main, ["check", "--pr", "9", "--project-root", str(bad_root)])
    check(code != 0, f"a bad --project-root must exit non-zero (fail closed), got {code} (stderr: {err})")
    check(err == "", f"a bad --project-root must NOT print a traceback, got stderr {err!r}")
    result = json.loads(out)
    check(result["verdict"] == "recheck",
          f"a bad --project-root must decide recheck, never proceed, got {result!r}")
    check(result["reason"].startswith("could not fetch PR view:"),
          f"the reason must name the fetch failure, got {result['reason']!r}")


def t_cli_undecodable_view_json_fails_closed():
    # A recorded --view-json whose BYTES are not UTF-8, so the failure comes from the DECODE and the read
    # never reaches the parse. It must fail CLOSED: a structured recheck on stdout, a NON-ZERO exit, and NO
    # traceback. capture_cli only catches SystemExit, so an uncaught decode error would ESCAPE here and fail
    # this fixture — that is the teeth. This row pins the MESSAGE for this input; that no input at all can
    # escape is the hostile-table fixture's job, not this one's.
    with tempfile.TemporaryDirectory() as d:
        vjson = Path(d) / "view.json"
        vjson.write_bytes(b'{"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "x": "\xff"}')
        code, out, err = capture_cli(M.main, ["check", "--pr", "9", "--view-json", str(vjson)])
    check(code != 0, f"an undecodable --view-json must exit non-zero (fail closed), got {code} (stderr: {err})")
    check(err == "", f"an undecodable --view-json must NOT print a traceback, got stderr {err!r}")
    result = json.loads(out)
    check(result["verdict"] == "recheck",
          f"an undecodable --view-json must decide recheck, never proceed, got {result!r}")
    check(result["reason"].startswith("could not fetch PR view:"),
          f"the reason must name the fetch failure, got {result['reason']!r}")


def t_cli_undecodable_gh_stdout_fails_closed():
    # The same invalid bytes, but from a REAL `gh` resolved through PATH. With text=True the decode happens
    # inside communicate(), so the failure surfaces from the subprocess.run CALL rather than from the parse
    # after it — a SPAWN-site failure that looks like neither a read nor a parse. No --view-json, so
    # load_view takes the gh path. Fail CLOSED: a structured recheck on stdout, NON-ZERO exit, NO traceback.
    with gh_writing(b'{"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "x": "\xff"}'):
        code, out, err = capture_cli(M.main, ["check", "--pr", "9"])
    check(code != 0, f"undecodable gh output must exit non-zero (fail closed), got {code} (stderr: {err})")
    check(err == "", f"undecodable gh output must NOT print a traceback, got stderr {err!r}")
    result = json.loads(out)
    check(result["verdict"] == "recheck",
          f"undecodable gh output must decide recheck, never proceed, got {result!r}")
    check(result["reason"].startswith("could not fetch PR view:"),
          f"the reason must name the fetch failure, got {result['reason']!r}")


def t_cli_deep_view_json_fails_closed():
    # A recorded --view-json nested far past the parse's recursion limit. `json.loads` recurses per nesting
    # level, so it raises RecursionError — a RuntimeError, in a different branch of the exception tree from
    # every other input here. It must fail CLOSED: a structured recheck on stdout, a NON-ZERO exit, and NO
    # traceback. capture_cli only catches SystemExit, so an uncaught RecursionError would ESCAPE here and
    # fail this fixture — the teeth. This row pins the MESSAGE for this input; the hostile-table fixture is
    # what pins that no input escapes.
    with tempfile.TemporaryDirectory() as d:
        vjson = Path(d) / "view.json"
        vjson.write_bytes(deeply_nested_json())
        code, out, err = capture_cli(M.main, ["check", "--pr", "9", "--view-json", str(vjson)])
    check(code != 0, f"a too-deep --view-json must exit non-zero (fail closed), got {code} (stderr: {err})")
    check(err == "", f"a too-deep --view-json must NOT print a traceback, got stderr {err!r}")
    result = json.loads(out)
    check(result["verdict"] == "recheck",
          f"a too-deep --view-json must decide recheck, never proceed, got {result!r}")
    check(result["reason"].startswith("could not fetch PR view:"),
          f"the reason must name the fetch failure, got {result['reason']!r}")


def t_cli_deep_gh_stdout_fails_closed():
    # The same too-deep response, but from a REAL `gh` resolved through PATH, so the RecursionError comes
    # out of the SECOND parse — the live-fetch one, a separate `try` from the recorded-file parse above and
    # therefore a separate escape. Every guarantee `_gauntlet/gh.py` makes has to hold at BOTH parse sites,
    # and only a fixture on each proves it. No --view-json, so load_view takes the gh path. Fail CLOSED: a
    # structured recheck on stdout, NON-ZERO exit, NO traceback.
    with gh_writing(deeply_nested_json()):
        code, out, err = capture_cli(M.main, ["check", "--pr", "9"])
    check(code != 0, f"too-deep gh output must exit non-zero (fail closed), got {code} (stderr: {err})")
    check(err == "", f"too-deep gh output must NOT print a traceback, got stderr {err!r}")
    result = json.loads(out)
    check(result["verdict"] == "recheck",
          f"too-deep gh output must decide recheck, never proceed, got {result!r}")
    check(result["reason"].startswith("could not fetch PR view:"),
          f"the reason must name the fetch failure, got {result['reason']!r}")


def t_cli_hostile_responses_never_escape():
    """THE GUARANTEE, not a member list: for EVERY hostile response in the shared table, and at BOTH of the
    fetch's parse sites, the CLI prints a structured verdict and prints no traceback.

    The per-input fixtures above cannot make this claim. Each was written around an exception type that was
    already known, so a family member discovered tomorrow leaves all of them green — which is how a plain
    `ValueError` from an oversized integer literal reached a released decider past clauses that already named
    a decode error and a recursion error. This fixture is driven by DATA: a row added to
    `hostile_json_responses` fails here until the fetch survives it.

    Both sites, every row. The recorded-file parse and the live-fetch parse are separate `try` blocks, so a
    guarantee proved at one says nothing about the other.
    """
    for name, payload in hostile_json_responses():
        with tempfile.TemporaryDirectory() as d:
            vjson = Path(d) / "view.json"
            vjson.write_bytes(payload)
            recorded = capture_cli(M.main, ["check", "--pr", "9", "--view-json", str(vjson)])
        with gh_writing(payload):
            live = capture_cli(M.main, ["check", "--pr", "9"])
        for site, (code, out, err) in (("--view-json", recorded), ("gh stdout", live)):
            label = f"[{name} via {site}]"
            # capture_cli only catches SystemExit, so anything else escaping the fetch lands HERE as a raised
            # exception and the runner reports this fixture as a crash — the teeth.
            check(err == "", f"{label} must NOT print a traceback, got stderr {err!r}")
            check(code != 0, f"{label} must exit non-zero (fail closed), got {code}")
            try:
                result = json.loads(out)
            except json.JSONDecodeError as exc:
                raise M.SelfTestFailure(
                    f"{label} must print a structured verdict on stdout, got {out!r} ({exc})") from exc
            check(result.get("verdict") == "recheck",
                  f"{label} must decide recheck, never proceed, got {result!r}")
            check(str(result.get("reason", "")).startswith("could not fetch PR view:"),
                  f"{label} must name the fetch failure, got {result!r}")
            check(result["reason"].strip() != "could not fetch PR view:",
                  f"{label} must say WHAT failed, not an empty detail, got {result['reason']!r}")


def t_cli_legacy_view_errors_keep_their_exact_wording():
    """THE WHOLE REASON STRING, for every failure that had one before `_gauntlet/gh.py` owned the fetch.

    Extracting the fetch into a shared owner was meant to leave each caller's message BYTE FOR BYTE intact.
    Every fixture above asserts only the `could not fetch PR view: ` PREFIX, so a rewrite of the tail behind
    it — the part a reader actually reads — cannot fail any of them, and one went unnoticed for six rounds of
    review. This row is the assertion that can catch it: the shared table owns the exact tail, and equality
    is the check.
    """
    with tempfile.TemporaryDirectory() as d:
        for name, extra, context, tail in legacy_view_error_cases(Path(d)):
            with context():
                code, out, err = capture_cli(M.main, ["check", "--pr", "9"] + extra)
            label = f"[{name}]"
            check(err == "", f"{label} must NOT print a traceback, got stderr {err!r}")
            check(code != 0, f"{label} must exit non-zero (fail closed), got {code}")
            result = json.loads(out)
            check(result["verdict"] == "recheck",
                  f"{label} must decide recheck, never proceed, got {result!r}")
            expected = f"could not fetch PR view: {tail}"
            check(result["reason"] == expected,
                  f"{label} must print the reason UNCHANGED from before the fetch was shared.\n"
                  f"           expected {expected!r}\n           got      {result['reason']!r}")


def t_gh_writing_restores_an_absent_path():
    # gh_writing's PATH restoration, pinned from the suite whose ORDER its failure mode breaks: the two
    # fixtures above use gh_writing, and everything after them spawns git. An unset PATH and PATH="" are not
    # the same thing to exec — with the variable gone execvp falls back to confstr(_CS_PATH) and a bare
    # `git` still resolves, while PATH="" leaves nothing to search and the spawn raises FileNotFoundError.
    # So a restoration that writes back a defaulted "" leaves the process in a state it was never in, and
    # every later git-using fixture fails — but ONLY when the suite was launched with PATH already absent,
    # which is exactly why running the suite the ordinary way never noticed. Reproduce that launch here.
    outer = os.environ.pop("PATH", None)
    try:
        with gh_writing(b"{}"):
            pass
        check("PATH" not in os.environ,
              f"gh_writing must restore an ABSENT PATH by DELETING it, got {os.environ.get('PATH')!r}")
        # The consequence, not just the variable: a bare command name must still spawn afterwards.
        proc = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False)  # noqa: S603,S607
        check(proc.returncode == 0,
              f"a bare `git` must still spawn after restoration, got exit {proc.returncode}")
    finally:
        if outer is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = outer


# The repository fixtures come from the shared owner, bound to THIS suite's failure type.
_GIT = GitFixture(M.SelfTestFailure)
_git = _GIT.run


def t_clean_view_with_stale_base_records_no():
    """A prior campaign merge advances main while GitHub still calls the second PR CLEAN.

    The enums cannot see it; the fetched graph can. What changed is what the tool DOES with that: the PR is
    still cleared to be reviewed (`proceed`), and the staleness is carried as `base_current: no` for the
    label and the drain to act on. The rebase is owed at the merge, not here.
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        remote = root / "remote.git"
        seed = root / "seed"
        candidate = root / "candidate"

        _GIT.init_bare(remote)
        _GIT.clone(remote, seed)

        (seed / "f").write_text("base\n", encoding="utf-8")
        _git(seed, "add", "f")
        _git(seed, "commit", "-m", "base")
        _git(seed, "push", "origin", "main")

        _git(seed, "checkout", "-b", "first")
        (seed / "first").write_text("first\n", encoding="utf-8")
        _git(seed, "add", "first")
        _git(seed, "commit", "-m", "first candidate")
        _git(seed, "push", "origin", "first")

        _git(seed, "checkout", "main")
        _git(seed, "checkout", "-b", "second")
        (seed / "second").write_text("second\n", encoding="utf-8")
        _git(seed, "add", "second")
        _git(seed, "commit", "-m", "second candidate")
        _git(seed, "push", "origin", "second")

        _GIT.clone(remote, candidate)
        _git(candidate, "checkout", "second")

        # Simulate the first serial campaign merge advancing main after the second PR was reviewed.
        _git(seed, "checkout", "main")
        _git(seed, "merge", "--squash", "first")
        _git(seed, "commit", "-m", "merge first candidate")
        _git(seed, "push", "origin", "main")

        vjson = root / "clean-view.json"
        vjson.write_text(json.dumps(view()), encoding="utf-8")
        code, out, err = capture_cli(
            M.main,
            ["check", "--pr", "9", "--view-json", str(vjson), "--worktree", str(candidate),
             "--base", "main"],
        )
        check(code == 0,
              f"a CLEAN view whose worktree lacks the advanced base is still reviewable (stderr: {err})")
        check(json.loads(out) == {"verdict": "proceed", "reason": "GitHub merge state permits base check",
                                  "base_current": "no"},
              f"a stale base must proceed and REPORT the staleness, got {out!r}")


def t_clean_view_with_current_base_proceeds():
    """A CLEAN PR whose HEAD contains fetched main may proceed."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        remote = root / "remote.git"
        seed = root / "seed"
        candidate = root / "candidate"

        _GIT.init_bare(remote)
        _GIT.clone(remote, seed)
        (seed / "f").write_text("base\n", encoding="utf-8")
        _git(seed, "add", "f")
        _git(seed, "commit", "-m", "base")
        _git(seed, "push", "origin", "main")

        _GIT.clone(remote, candidate)

        vjson = root / "clean-view.json"
        vjson.write_text(json.dumps(view()), encoding="utf-8")
        code, out, err = capture_cli(
            M.main,
            ["check", "--pr", "9", "--view-json", str(vjson), "--worktree", str(candidate),
             "--base", "main"],
        )
        check(code == 0, f"a candidate containing the fetched base must proceed (stderr: {err})")
        check(json.loads(out) == {"verdict": "proceed", "reason": "GitHub merge state permits base check",
                                  "base_current": "yes"},
              f"a current base must permit the candidate and report it, got {out!r}")


def t_force_rewritten_base_refreshes_and_rebases():
    """A rewritten remote base is refreshed before ancestry is decided."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        remote = root / "remote.git"
        seed = root / "seed"
        candidate = root / "candidate"

        _GIT.init_bare(remote)
        _GIT.clone(remote, seed)
        (seed / "f").write_text("base\n", encoding="utf-8")
        _git(seed, "add", "f")
        _git(seed, "commit", "-m", "base")
        _git(seed, "push", "origin", "main")

        _GIT.clone(remote, candidate)

        (seed / "f").write_text("first advance\n", encoding="utf-8")
        _git(seed, "commit", "-am", "first advance")
        _git(seed, "push", "origin", "main")
        _git(candidate, "fetch", "origin", "main")
        old_base = _git(candidate, "rev-parse", "refs/remotes/origin/main").stdout.strip()

        (seed / "f").write_text("rewritten advance\n", encoding="utf-8")
        _git(seed, "commit", "--amend", "-am", "rewritten base")
        rewritten_base = _git(seed, "rev-parse", "HEAD").stdout.strip()
        _git(seed, "push", "--force", "origin", "HEAD:refs/heads/main")
        check(old_base != rewritten_base,
              "fixture setup: the remote base rewrite must differ from the stale tracking ref")

        result = M.check_base_ancestry(str(candidate), "main", "origin")
        refreshed_base = _git(candidate, "rev-parse", "refs/remotes/origin/main").stdout.strip()
        check(result == ("stale", ""),
              f"a candidate based on the replaced base must be sent to rebase, got {result!r}")
        check(refreshed_base == rewritten_base,
              "the ancestry check must force-refresh origin/main to the rewritten remote base")


def t_literal_head_base_does_not_follow_remote_head_symref():
    """A branch literally named HEAD must not overwrite the default branch's tracking ref."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        remote = root / "remote.git"
        seed = root / "seed"
        candidate = root / "candidate"

        _GIT.init_bare(remote)
        _GIT.clone(remote, seed)
        (seed / "f").write_text("main\n", encoding="utf-8")
        _git(seed, "add", "f")
        _git(seed, "commit", "-m", "main base")
        _git(seed, "push", "origin", "main")
        main_head = _git(seed, "rev-parse", "HEAD").stdout.strip()

        (seed / "f").write_text("literal HEAD branch\n", encoding="utf-8")
        _git(seed, "commit", "-am", "literal HEAD advance")
        literal_head = _git(seed, "rev-parse", "HEAD").stdout.strip()
        _git(seed, "push", "origin", "HEAD:refs/heads/HEAD")
        check(main_head != literal_head, "fixture setup: main and the literal HEAD branch must differ")

        _GIT.clone(remote, candidate)
        symbolic = _git(candidate, "symbolic-ref", "refs/remotes/origin/HEAD")
        check(symbolic.returncode == 0
              and symbolic.stdout.strip() == "refs/remotes/origin/main",
              "fixture setup: origin/HEAD must be the normal symbolic ref to origin/main")
        origin_main_before = _git(candidate, "rev-parse", "refs/remotes/origin/main").stdout.strip()
        check(origin_main_before == main_head, "fixture setup: origin/main must track the remote main branch")

        result = M.check_base_ancestry(str(candidate), "HEAD", "origin")
        check(result == ("stale", ""),
              f"a candidate behind the literal HEAD branch must be sent to rebase, got {result!r}")
        check(_git(candidate, "rev-parse", "refs/remotes/origin/main").stdout.strip() == origin_main_before,
              "fetching the literal HEAD branch must not follow origin/HEAD and overwrite origin/main")
        private_refs = _git(
            candidate, "for-each-ref", "--format=%(objectname)", "refs/gauntlet/base-fetch").stdout.splitlines()
        check(private_refs == [literal_head],
              f"the private fetched-base ref must resolve to the literal HEAD branch tip, got {private_refs!r}")

        _git(remote, "update-ref", "-d", "refs/heads/HEAD")
        result = M.check_base_ancestry(str(candidate), "HEAD", "origin")
        check(result[0] == "unverified"
              and result[1].startswith("could not fetch +refs/heads/HEAD:refs/gauntlet/base-fetch/"),
              f"a failed literal HEAD fetch must name its exact private refspec, got {result!r}")
        check(_git(candidate, "rev-parse", "refs/remotes/origin/main").stdout.strip() == origin_main_before,
              "a failed literal HEAD fetch must leave origin/main unchanged")


DASH_BASE = "--upload-pack=/bin/false"


def _dash_base_ancestry(root: Path, *, current: bool) -> "tuple[int, dict, str, str, str]":
    """Drive the documented CLI against a legal dash-leading base and a real bare remote."""
    remote, seed, candidate = root / "remote.git", root / "seed", root / "candidate"
    _GIT.init_bare(remote)
    _GIT.clone(remote, seed)
    (seed / "f").write_text("base\n", encoding="utf-8")
    _git(seed, "add", "f")
    _git(seed, "commit", "-m", "base")
    _git(seed, "push", "origin", "main")
    dash_head = f"refs/heads/{DASH_BASE}"
    tracking_ref = f"refs/remotes/origin/{DASH_BASE}"
    _git(seed, "update-ref", dash_head, "HEAD")
    _git(seed, "push", "origin", f"{dash_head}:{dash_head}")

    _GIT.clone(remote, candidate)
    old_base = _git(candidate, "rev-parse", tracking_ref).stdout.strip()

    (seed / "f").write_text("advanced base\n", encoding="utf-8")
    _git(seed, "commit", "-am", "advance dash base")
    advanced_base = _git(seed, "rev-parse", "HEAD").stdout.strip()
    _git(seed, "push", "origin", f"HEAD:{dash_head}")
    check(old_base != advanced_base, "fixture setup: the candidate tracking ref must be stale")

    if current:
        # Import the advanced base through a separate local ref so HEAD contains it. That fetch NAMES the
        # configured remote, so Git ALSO opportunistically updates `tracking_ref` as a side effect — which
        # would pre-satisfy the very refresh this fixture exists to prove. Force the tracking ref back to
        # the stale tip so the operation under test is the only thing that can refresh it.
        _git(candidate, "fetch", "origin", f"{dash_head}:refs/heads/current-dash-base")
        _git(candidate, "checkout", "current-dash-base")
        _git(candidate, "update-ref", tracking_ref, old_base)

    # BOTH callers depend on this: the `refreshed == advanced_base` assertion below proves nothing unless the
    # tracking ref is STALE at the moment the CLI runs. Assert the stale pre-state here and the fixture cannot
    # silently lose its teeth to a setup step that refreshes the ref first.
    check(_git(candidate, "rev-parse", tracking_ref).stdout.strip() == old_base,
          "fixture setup: the base tracking ref must still be stale when the CLI runs, or the refresh "
          "assertion is pre-satisfied and proves nothing")

    vjson = root / "clean-view.json"
    vjson.write_text(json.dumps(view()), encoding="utf-8")
    code, out, err = capture_cli(
        M.main,
        ["check", "--pr", "9", "--view-json", str(vjson), "--worktree", str(candidate),
         "--base", DASH_BASE],
    )
    refreshed_base = _git(candidate, "rev-parse", tracking_ref).stdout.strip()
    return code, json.loads(out), err, refreshed_base, advanced_base


def t_dash_leading_current_base_refreshes_and_proceeds():
    with tempfile.TemporaryDirectory() as d:
        code, result, err, refreshed, remote_head = _dash_base_ancestry(Path(d), current=True)
        check(code == 0,
              f"a current candidate on a dash-leading base must pass the CLI (code={code}, err={err!r})")
        check(result == {"verdict": "proceed", "reason": "GitHub merge state permits base check",
                         "base_current": "yes"},
              f"a current candidate on a dash-leading base must proceed, got {result!r}")
        check(refreshed == remote_head,
              "the dash-leading base fetch must refresh its remote-tracking ref before the current verdict")


def t_dash_leading_stale_base_refreshes_and_reports_no():
    with tempfile.TemporaryDirectory() as d:
        code, result, err, refreshed, remote_head = _dash_base_ancestry(Path(d), current=False)
        check(code == 0,
              f"a stale candidate on a dash-leading base is still reviewable (code={code}, err={err!r})")
        check(result == {"verdict": "proceed", "reason": "GitHub merge state permits base check",
                         "base_current": "no"},
              f"a stale candidate on a dash-leading base must report the staleness, got {result!r}")
        check(refreshed == remote_head,
              "the dash-leading base fetch must refresh its remote-tracking ref before the stale verdict")


def t_candidate_revision_is_checked_instead_of_moved_head():
    """Stage 3 may check a reviewed SHA after the local worktree moves to a different, base-current HEAD."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        remote = root / "remote.git"
        seed = root / "seed"
        candidate = root / "candidate"

        _GIT.init_bare(remote)
        _GIT.clone(remote, seed)
        (seed / "f").write_text("base\n", encoding="utf-8")
        _git(seed, "add", "f")
        _git(seed, "commit", "-m", "base")
        _git(seed, "push", "origin", "main")

        _GIT.clone(remote, candidate)
        _git(candidate, "checkout", "-b", "reviewed")
        (candidate / "reviewed").write_text("reviewed\n", encoding="utf-8")
        _git(candidate, "add", "reviewed")
        _git(candidate, "commit", "-m", "reviewed candidate")
        reviewed = _git(candidate, "rev-parse", "HEAD").stdout.strip()

        (seed / "advanced").write_text("advanced\n", encoding="utf-8")
        _git(seed, "add", "advanced")
        _git(seed, "commit", "-m", "advance base")
        _git(seed, "push", "origin", "main")
        _git(candidate, "fetch", "origin", "main")
        _git(candidate, "checkout", "-B", "moved-local-head", "origin/main")
        local_head = _git(candidate, "rev-parse", "HEAD").stdout.strip()
        check(local_head != reviewed, "fixture requires local HEAD to differ from the reviewed SHA")

        check(M.check_base_ancestry(str(candidate), "main", "origin") == ("current", ""),
              "fixture requires the moved local HEAD to contain the advanced base")
        check(M.check_base_ancestry(str(candidate), "main", "origin", reviewed) == ("stale", ""),
              "the reviewed SHA lacks the advanced base and must be reported stale")


# --- `--file`: a real `proceed` RECORDS base_ok_sha on the ledger (the precondition `verdict` enforces) -----
# base-preflight is the ONLY sanctioned writer of `base_ok_sha`: on a final `proceed`, and only when a ledger
# is named, it resolves the worktree's HEAD and shells out to `ledger.py base-ok`. `decide()` stays pure;
# these fixtures drive the CLI end to end (git worktree + a real sibling ledger), never `decide` directly.


def _run_ledger(ledger: Path, *argv: str) -> subprocess.CompletedProcess:
    proc = subprocess.run([sys.executable, str(M.LEDGER), "--file", str(ledger), *argv],
                          capture_output=True, text=True, check=False)
    check(proc.returncode == 0, f"ledger {' '.join(argv)} failed: {proc.stderr.strip()}")
    return proc


def _ledger_row(ledger: Path, pr: str, head_sha: str, base: str = "main") -> None:
    """Build a ledger through the REAL sibling accessor with one row for `pr` at `head_sha` (base_ok_sha `-`),
    carrying an EXPLICIT row base — the shape `pr-adopt.py` writes for every new row (`--base-branch`)."""
    _run_ledger(ledger, "header", "set", "run_id", "t")
    _run_ledger(ledger, "add-row", "--pr", pr, "--head-sha", head_sha, "--base-branch", base)


def _legacy_ledger_row(ledger: Path, pr: str, head_sha: str, header_base: str = "main") -> None:
    """An OLD-shape ledger: the row carries NO explicit base (`base_branch` stays `-`), so its effective base
    INHERITS the legacy header `base_branch`. Proves `check --file` resolves through the accessor's fallback."""
    _run_ledger(ledger, "header", "set", "run_id", "t")
    _run_ledger(ledger, "header", "set", "base_branch", header_base)
    _run_ledger(ledger, "add-row", "--pr", pr, "--head-sha", head_sha)


def _base_ok_sha(ledger: Path, pr: str) -> str:
    return _run_ledger(ledger, "get", "--pr", pr, "--field", "base_ok_sha").stdout.strip()


def _ledger_field(ledger: Path, pr: str, field: str) -> str:
    return _run_ledger(ledger, "get", "--pr", pr, "--field", field).stdout.strip()


def t_frozen_view_with_file_parks_candidate():
    """Stage 3 sequence: the required preflight sees FROZEN and records the ledger park before returning."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ledger = root / "state.jsonl"
        _ledger_row(ledger, "9", "a" * 40)
        _run_ledger(ledger, "set", "--pr", "9", "--status", "in_review")
        vjson = root / "frozen.json"
        vjson.write_text(json.dumps(view(mergeStateStatus="FROZEN")), encoding="utf-8")

        code, out, err = capture_cli(
            M.main,
            ["check", "--pr", "9", "--view-json", str(vjson), "--base", "main", "--file", str(ledger)],
        )

        check(code != 0, f"a parked candidate must not clear preflight (stderr: {err})")
        check(json.loads(out) == {"verdict": "park", "reason": "unknown merge state FROZEN — park"},
              f"FROZEN must return the park action, got {out!r}")
        check(_ledger_field(ledger, "9", "status") == "awaiting-user",
              "FROZEN did not reach ledger.py park before preflight returned")
        check(_ledger_field(ledger, "9", "ci_reason") == "unknown merge state FROZEN — park",
              "the machine-blocker park did not name the unrecognized value")
        check(_ledger_field(ledger, "9", "blocker_ruling") == "-",
              "park entry must clear the ruling through ledger.py's atomic transition")
        check(_base_ok_sha(ledger, "9") == "-",
              "a machine-blocker park must not stamp the base as cleared")


def t_unknown_view_with_file_rechecks_without_park():
    """Recognized transient UNKNOWN remains a re-poll and leaves the candidate active."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ledger = root / "state.jsonl"
        _ledger_row(ledger, "9", "a" * 40)
        _run_ledger(ledger, "set", "--pr", "9", "--status", "in_review")
        vjson = root / "unknown.json"
        vjson.write_text(json.dumps(view(mergeStateStatus="UNKNOWN")), encoding="utf-8")

        code, out, err = capture_cli(
            M.main,
            ["check", "--pr", "9", "--view-json", str(vjson), "--base", "main", "--file", str(ledger)],
        )

        check(code != 0, f"UNKNOWN must not clear preflight (stderr: {err})")
        check(json.loads(out) == {"verdict": "recheck", "reason": "mergeability not computed yet — re-poll"},
              f"recognized UNKNOWN must remain the re-poll action, got {out!r}")
        check(_ledger_field(ledger, "9", "status") == "in_review",
              "recognized UNKNOWN incorrectly entered the machine-blocker park")


def _current_base_worktree(root: Path) -> "tuple[Path, str]":
    """A candidate clone that CONTAINS fetched main (so base-preflight reaches `proceed`). Returns (worktree,
    HEAD sha)."""
    remote, seed, candidate = root / "remote.git", root / "seed", root / "candidate"
    _GIT.init_bare(remote)
    _GIT.clone(remote, seed)
    (seed / "f").write_text("base\n", encoding="utf-8")
    _git(seed, "add", "f")
    _git(seed, "commit", "-m", "base")
    _git(seed, "push", "origin", "main")
    _GIT.clone(remote, candidate)
    head = _git(candidate, "rev-parse", "HEAD").stdout.strip()
    return candidate, head


def _stale_base_worktree(root: Path) -> "tuple[Path, str]":
    """A candidate clone whose HEAD does NOT contain fetched main — main advanced after the clone, exactly
    as it does when a sibling campaign PR merges. Returns (worktree, HEAD sha)."""
    remote, seed, candidate = root / "remote.git", root / "seed", root / "candidate"
    _GIT.init_bare(remote)
    _GIT.clone(remote, seed)
    (seed / "f").write_text("base\n", encoding="utf-8")
    _git(seed, "add", "f")
    _git(seed, "commit", "-m", "base")
    _git(seed, "push", "origin", "main")
    _GIT.clone(remote, candidate)
    head = _git(candidate, "rev-parse", "HEAD").stdout.strip()
    (seed / "later").write_text("sibling merged\n", encoding="utf-8")
    _git(seed, "add", "later")
    _git(seed, "commit", "-m", "a sibling PR merged")
    _git(seed, "push", "origin", "main")
    return candidate, head


def t_stale_base_with_file_still_stamps_and_records_no():
    """A BEHIND PR is cleared to be reviewed, and BOTH readings land in one write.

    This is the pin on the deadlock the serialized drain would otherwise create. `ledger.py verdict` refuses
    unless `base_ok_sha == head_sha`, so if a behind PR could not reach `proceed` it could never record a
    verdict — and a PR that cannot be reviewed can never reach the front of the drain to be rebased. So the
    stamp MUST land for a stale base, and the staleness must ride along as `base_current: no` for the label
    to project.

    THE MUTATION PIN: make a stale ancestry withhold `proceed` again and this goes red.
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        candidate, head = _stale_base_worktree(root)
        vjson = root / "clean.json"
        vjson.write_text(json.dumps(view()), encoding="utf-8")
        ledger = root / "state.jsonl"
        _ledger_row(ledger, "9", head)
        code, out, err = capture_cli(M.main, ["check", "--pr", "9", "--view-json", str(vjson),
                                              "--worktree", str(candidate), "--base", "main",
                                              "--file", str(ledger)])
        check(code == 0, f"a behind PR must still be cleared to review (stderr: {err})")
        check(json.loads(out)["base_current"] == "no", f"the staleness must be reported, got {out!r}")
        check(_base_ok_sha(ledger, "9") == head,
              f"a behind PR must still be stamped, or its verdicts can never land: "
              f"{_base_ok_sha(ledger, '9')!r}")
        check(_ledger_field(ledger, "9", "base_current") == "no",
              f"the ledger must record the staleness: {_ledger_field(ledger, '9', 'base_current')!r}")


def t_proceed_with_file_records_base_ok():
    """A final `proceed` with `--file` stamps `base_ok_sha` = the worktree HEAD AND records the ancestry
    reading in the same write; the SAME check WITHOUT `--file` writes nothing (the pure decider is
    preserved)."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        candidate, head = _current_base_worktree(root)
        vjson = root / "clean.json"
        vjson.write_text(json.dumps(view()), encoding="utf-8")

        # WITH --file: proceed, and the ledger row is stamped for the live head.
        ledger = root / "state.jsonl"
        _ledger_row(ledger, "9", head)
        check(_base_ok_sha(ledger, "9") == "-", "fixture setup: base_ok_sha must start `-`")
        code, out, err = capture_cli(M.main, ["check", "--pr", "9", "--view-json", str(vjson),
                                              "--worktree", str(candidate), "--base", "main", "--file", str(ledger)])
        check(code == 0, f"a current base with --file must proceed (stderr: {err})")
        check(json.loads(out) == {"verdict": "proceed", "reason": "GitHub merge state permits base check",
                                  "base_current": "yes"},
              f"a current base must proceed, got {out!r}")
        check(_base_ok_sha(ledger, "9") == head,
              f"proceed with --file did not record base_ok_sha = {head!r}: {_base_ok_sha(ledger, '9')!r}")
        check(_ledger_field(ledger, "9", "base_current") == "yes",
              f"proceed with --file did not record base_current: {_ledger_field(ledger, '9', 'base_current')!r}")

        # WITHOUT --file: still proceed, but NOTHING is written to a ledger.
        ledger2 = root / "state2.jsonl"
        _ledger_row(ledger2, "9", head)
        code, out, err = capture_cli(M.main, ["check", "--pr", "9", "--view-json", str(vjson),
                                              "--worktree", str(candidate), "--base", "main"])
        check(code == 0, f"the same check without --file must still proceed (stderr: {err})")
        check(_base_ok_sha(ledger2, "9") == "-",
              f"a proceed with NO --file wrote base_ok_sha anyway: {_base_ok_sha(ledger2, '9')!r} — the pure "
              f"decider must write nothing")


def t_non_proceed_with_file_leaves_base_ok():
    """`rebase-first` and `recheck` NEVER stamp — even with `--file`. Only `proceed` stamps `base_ok_sha`, so
    these non-proceed decisions leave it at `-` and a later verdict stays refused. The Stage 3 park fixture
    pins the same no-stamp rule while separately proving the machine-blocker write."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ledger = root / "state.jsonl"
        _ledger_row(ledger, "9", "a" * 40)
        for name, mss, want in (("rebase-first", "DIRTY", "rebase-first"), ("recheck", "UNKNOWN", "recheck")):
            vjson = root / f"{name}.json"
            vjson.write_text(json.dumps(view(mergeStateStatus=mss)), encoding="utf-8")
            code, out, err = capture_cli(M.main, ["check", "--pr", "9", "--view-json", str(vjson),
                                                  "--file", str(ledger)])
            check(code != 0, f"[{name}] a non-proceed must exit non-zero (stderr: {err})")
            check(json.loads(out)["verdict"] == want, f"[{name}] expected {want}, got {out!r}")
            check(_base_ok_sha(ledger, "9") == "-",
                  f"[{name}] a non-proceed decision stamped base_ok_sha: {_base_ok_sha(ledger, '9')!r}")


# --- `--file`: the ROW owns the base — `--base` is an assertion, a live retarget refuses --------------------
# These drive the CLI with `--view-json` so no gh/git worktree is needed: the base checks all run BEFORE the
# ancestry probe, so a refusal is reached without a real base to fetch.


def t_file_base_assertion_mismatch_rechecks():
    """`--base` disagreeing with the row's effective base is REFUSED — the flag is an assertion, not a source."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ledger = root / "state.jsonl"
        _ledger_row(ledger, "9", "a" * 40, base="main")
        vjson = root / "v.json"
        vjson.write_text(json.dumps(view(baseRefName="main")), encoding="utf-8")
        code, out, err = capture_cli(M.main, ["check", "--pr", "9", "--view-json", str(vjson),
                                              "--base", "v3", "--file", str(ledger)])
        check(code != 0, f"a --base disagreeing with the row must fail closed (stderr: {err})")
        result = json.loads(out)
        check(result["verdict"] == "recheck", f"a --base mismatch must recheck, never proceed, got {result!r}")
        check("disagrees" in result["reason"] and "effective base" in result["reason"],
              f"the reason must name the --base disagreement, got {result['reason']!r}")


def t_file_origin_prefixed_base_matches():
    """An `origin/<base>` form of `--base` matches the row's bare effective base (the prefix is stripped)."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ledger = root / "state.jsonl"
        _ledger_row(ledger, "9", "a" * 40, base="main")
        vjson = root / "v.json"
        # DIRTY so the run stops at the (post-assertion) decide step, not the ancestry probe — proving the
        # `origin/main` assertion PASSED (a refusal there would name the disagreement instead).
        vjson.write_text(json.dumps(view(mergeStateStatus="DIRTY", baseRefName="main")), encoding="utf-8")
        code, out, err = capture_cli(M.main, ["check", "--pr", "9", "--view-json", str(vjson),
                                              "--base", "origin/main", "--file", str(ledger)])
        check(code != 0, f"a DIRTY view still rebases (stderr: {err})")
        result = json.loads(out)
        check(result["verdict"] == "rebase-first",
              f"origin/main must satisfy the assertion and fall through to decide, got {result!r}")


def t_file_origin_named_base_matches_itself():
    """A base LITERALLY named `origin/<x>` (a legal branch name) matches itself: `--base origin/release`
    against a stored `origin/release` must pass the assertion, never be read as a disagreement because one
    side was stripped (`ledger.py base_agrees` — identical strings always agree). A bare `--base release`
    still disagrees: the STORED base is never stripped."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ledger = root / "state.jsonl"
        _ledger_row(ledger, "9", "a" * 40, base="origin/release")
        vjson = root / "v.json"
        # DIRTY so the run stops at the (post-assertion) decide step, not the ancestry probe — proving the
        # identical-string assertion PASSED (a refusal there would name the disagreement instead).
        vjson.write_text(json.dumps(view(mergeStateStatus="DIRTY", baseRefName="origin/release")),
                         encoding="utf-8")
        code, out, err = capture_cli(M.main, ["check", "--pr", "9", "--view-json", str(vjson),
                                              "--base", "origin/release", "--file", str(ledger)])
        check(code != 0, f"a DIRTY view still rebases (stderr: {err})")
        result = json.loads(out)
        check(result["verdict"] == "rebase-first",
              f"identical origin/release strings must agree and fall through to decide, got {result!r}")
        # The bare form does NOT assert a base literally named origin/release — the stored base is never
        # stripped, so this refuses as a disagreement.
        code, out, err = capture_cli(M.main, ["check", "--pr", "9", "--view-json", str(vjson),
                                              "--base", "release", "--file", str(ledger)])
        check(code != 0, f"a bare --base against an origin/-named stored base must fail closed (stderr: {err})")
        result = json.loads(out)
        check(result["verdict"] == "recheck" and "disagrees" in result["reason"],
              f"a bare --base must disagree with a stored origin/-named base, got {result!r}")


def t_file_live_retarget_rechecks():
    """The PR's live `baseRefName` differs from the row's effective base -> the retarget refusal, with the
    EXACT machine-blocker wording a re-adoption/reconcile park records (never proceed, never rebase-first)."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ledger = root / "state.jsonl"
        _ledger_row(ledger, "9", "a" * 40, base="main")
        vjson = root / "v.json"
        vjson.write_text(json.dumps(view(baseRefName="v9")), encoding="utf-8")
        code, out, err = capture_cli(M.main, ["check", "--pr", "9", "--view-json", str(vjson),
                                              "--base", "main", "--file", str(ledger)])
        check(code != 0, f"a live retarget must fail closed (stderr: {err})")
        result = json.loads(out)
        check(result["verdict"] == "recheck", f"a retarget must recheck, never proceed, got {result!r}")
        check(result["reason"] == "base changed from main to v9; not supported mid-run",
              f"the retarget reason must be the exact machine-blocker wording, got {result['reason']!r}")


def t_file_legacy_row_inherits_header_base():
    """An OLD row (no explicit base) resolves through the legacy header, and the live comparison uses THAT
    inherited base: a live `baseRefName` differing from the header base refuses with the header value named."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ledger = root / "state.jsonl"
        _legacy_ledger_row(ledger, "9", "a" * 40, header_base="main")
        vjson = root / "v.json"
        vjson.write_text(json.dumps(view(baseRefName="v9")), encoding="utf-8")
        code, out, err = capture_cli(M.main, ["check", "--pr", "9", "--view-json", str(vjson),
                                              "--file", str(ledger)])
        check(code != 0, f"a legacy row whose live base moved must fail closed (stderr: {err})")
        result = json.loads(out)
        check(result["reason"] == "base changed from main to v9; not supported mid-run",
              f"the inherited header base must drive the comparison, got {result['reason']!r}")


def t_file_missing_row_rechecks():
    """`--file` naming a PR with no ledger row fails closed — the base cannot be resolved, never proceed."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ledger = root / "state.jsonl"
        _ledger_row(ledger, "9", "a" * 40, base="main")
        vjson = root / "v.json"
        vjson.write_text(json.dumps(view()), encoding="utf-8")
        code, out, err = capture_cli(M.main, ["check", "--pr", "77", "--view-json", str(vjson),
                                              "--file", str(ledger)])
        check(code != 0, f"an unknown PR row must fail closed (stderr: {err})")
        result = json.loads(out)
        check(result["verdict"] == "recheck" and "no ledger row for pr 77" in result["reason"],
              f"a missing row must recheck and name the PR, got {result!r}")


def t_cli_help_names_both_file_writes():
    """`check --help` names both ledger writes; omitting `--file` is the only pure/no-write form."""
    code, out, err = capture_cli(M.main, ["check", "--help"])
    check(code == 0, f"`check --help` must exit successfully (stderr: {err})")
    help_text = " ".join(out.split())
    check("`proceed` records base_ok_sha" in help_text,
          f"`--file` help omitted the proceed stamp: {help_text!r}")
    check("`park` records the ledger-owned machine blocker" in help_text,
          f"`--file` help omitted the park transition: {help_text!r}")
    check("Absent: the pure decider, no write" in help_text,
          f"`--file` help no longer names the no-write form: {help_text!r}")


def t_shared_remote_and_clone_primitives():
    """`GitFixture.init_bare` and `.clone`'s OWN contract, exercised directly rather than through a fixture.

    Both are new shared primitives, so their semantics need a check that does not depend on any suite's
    scenario. The load-bearing parts are that `init_bare` really produces a BARE repository on the named
    branch (an ordinary one would accept a commit and break every push-based fixture in a way that only
    shows up much later), that `clone` leaves a usable commit identity behind, and that BOTH raise the
    bound failure type instead of returning a failed result — the inline blocks they replaced in this file
    checked their own return codes, and the one in `clean-rebase-test.py` did NOT.
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        remote = root / "remote.git"
        clone = root / "clone"

        _GIT.init_bare(remote, branch="trunk")
        check((remote / "HEAD").exists() and not (remote / ".git").exists(),
              "init_bare must produce a bare repository, not one with a working tree")
        check(_git(remote, "symbolic-ref", "HEAD").stdout.strip() == "refs/heads/trunk",
              "init_bare must honour the requested default branch")

        _GIT.clone(remote, clone)
        check(_git(clone, "config", "user.email").stdout.strip() == FIXTURE_EMAIL,
              "clone must leave a commit identity behind, so the clone can commit")
        # The identity is the point: a commit here would fail on a machine with no global git config.
        (clone / "f").write_text("x\n", encoding="utf-8")
        _git(clone, "add", "f")
        _git(clone, "commit", "-m", "proves the identity works")

        # Both RAISE rather than returning a failed result, which is the property the replaced inline
        # blocks asserted by hand and the one in clean-rebase-test.py did not assert at all. The target
        # is an existing regular FILE: `git init` happily creates missing parent directories, so a
        # merely-absent path is not a failure at all and would prove nothing here.
        blocker = root / "a-file"
        blocker.write_text("not a directory\n", encoding="utf-8")
        for label, call in (("init_bare", lambda: _GIT.init_bare(blocker)),
                            ("clone", lambda: _GIT.clone(remote, blocker))):
            try:
                call()
            except M.SelfTestFailure:
                pass
            else:
                check(False, f"{label} must raise the bound failure type when git fails")


def t_malformed_repo_rechecks_before_any_fetch():
    """A malformed `--repo` fails closed to `recheck` at the CLI boundary, before any `gh` runs.

    This tool had NO validation at all: the value went straight into a `gh` argv, and gh answered about a
    repository the caller never named. The refusal exits non-zero like every other non-`proceed` verdict,
    so a caller gating on `$?` cannot mistake it for a cleared base.
    """
    code, out, _err = capture_cli(M.main, ["check", "--pr", "9", "--repo", "not-a-repo"])
    check(code != 0, "a malformed --repo must exit non-zero")
    result = json.loads(out)
    check(result["verdict"] == "recheck", f"a malformed --repo must fail closed to recheck, got {result!r}")
    check("'not-a-repo'" in result["reason"], f"the reason must quote the value, got {result!r}")


def t_empty_repo_rechecks_before_view_and_absent_still_fetches():
    """An explicit empty `--repo` refuses before the view read, while an omitted flag still fetches.

    The guard must distinguish the two argparse values: `""` means the caller supplied an invalid repository;
    `None` means the optional flag was absent and permits the current-checkout fetch path.
    """
    calls = []

    def fetch(pr, *, fields, repo=None, cwd=None, view_json=None):
        calls.append((pr, fields, repo, cwd, view_json))
        return view(mergeStateStatus="DIRTY"), None

    old = M.pr_view_json
    setattr(M, "pr_view_json", fetch)
    try:
        empty = capture_cli(M.main, ["check", "--pr", "9", "--repo", ""])
        empty_calls = list(calls)
        absent = capture_cli(M.main, ["check", "--pr", "9"])
    finally:
        setattr(M, "pr_view_json", old)

    code, out, _err = empty
    check(code != 0, "an empty --repo must exit non-zero")
    result = json.loads(out)
    check(result["verdict"] == "recheck", f"an empty --repo must fail closed to recheck, got {result!r}")
    check(repr("") in result["reason"], f"the refusal must quote the empty value, got {result!r}")
    check(empty_calls == [], f"an empty --repo must refuse before the view read, got {empty_calls!r}")

    code, out, _err = absent
    check(code != 0, "an omitted --repo may reach its normal non-proceed result")
    result = json.loads(out)
    check(result["verdict"] == "rebase-first",
          f"an omitted --repo must retain the current-checkout fetch path, got {result!r}")
    check(calls == [("9", M.VIEW_FIELDS, None, None, None)],
          f"an omitted --repo must fetch with repo=None, got {calls!r}")


def t_shared_repo_validator_semantics():
    """`_gauntlet/repository.py`'s OWN contract, exercised directly rather than through a caller.

    Six tools now refuse a malformed `--repo` through this one check, so its semantics need a fixture that
    does not depend on any caller's door. Five of those tools had NO validation at all before, which is
    what makes the accept side matter as much as the reject side: a check that refuses a legitimate
    repository would break every one of them at once.
    """
    fp = REPO_MOD.repo_problem
    for good in ("lestrrat-ai/claude-code-plugins", "a/b", "o/name.with.dots",
                 "o/name_with_underscores", "o/-leading-hyphen-is-legal-in-a-NAME",
                 "A0/b9", "a" * REPO_MOD.OWNER_MAX_LENGTH + "/" + "r" * REPO_MOD.REPOSITORY_MAX_LENGTH):
        check(fp(good) is None, f"{good!r} is a legal owner/name and must be accepted")

    for bad, why in (
        ("", "empty"),
        ("owner", "no slash at all"),
        ("owner/", "empty name"),
        ("/name", "empty owner"),
        ("a/b/c", "two slashes is not owner/name"),
        ("-lead/b", "an owner may not START with a hyphen"),
        ("trail-/b", "an owner may not END with a hyphen"),
        ("a--b/c", "an owner's hyphens are single, never doubled"),
        ("a b/c", "a space is not an identifier character"),
        ("o/na me", "a space is not an identifier character in a name either"),
        ("o/na/me", "a slash inside the name is a third field"),
        ("a" * (REPO_MOD.OWNER_MAX_LENGTH + 1) + "/b", "over GitHub's owner length limit"),
        ("a/" + "r" * (REPO_MOD.REPOSITORY_MAX_LENGTH + 1), "over GitHub's name length limit"),
    ):
        problem = fp(bad)
        check(problem is not None, f"{bad!r} must be refused ({why})")
        assert problem is not None  # narrow for the type checker; `check` above is the readable guard
        check(repr(bad) in problem, f"the refusal must QUOTE what it refused, got {problem!r}")

    # A shell-metacharacter value is refused like any other malformed one. It reaches `gh` argv, and the
    # point of checking at the boundary is that it never gets that far.
    check(fp("o/$(touch pwned)") is not None, "a shell-metacharacter name must be refused")


CASES = [
    ("malformed-repo-rechecks", "a malformed --repo fails closed to recheck at the CLI boundary, before any gh call", t_malformed_repo_rechecks_before_any_fetch),
    ("empty-repo-rechecks", "an empty --repo refuses before the view read; an omitted --repo still fetches", t_empty_repo_rechecks_before_view_and_absent_still_fetches),
    ("shared-repo-validator", "the shared --repo validator's own semantics: what it accepts, what it refuses, and that it quotes the value", t_shared_repo_validator_semantics),
    ("shared-remote-clone", "the shared bare-remote and clone primitives: bare on the named branch, a usable identity, and a raise on failure", t_shared_remote_and_clone_primitives),
    ("clean-proceeds", "CLEAN passes the enum screen", t_clean_proceeds),
    ("has-hooks-proceeds", "HAS_HOOKS passes the enum screen", t_has_hooks_proceeds),
    ("unstable-proceeds", "UNSTABLE is a check signal and reaches the graph check", t_unstable_proceeds),
    ("blocked-proceeds", "BLOCKED is a permission signal and reaches the graph check", t_blocked_proceeds),
    ("dirty-rebases", "DIRTY -> rebase-first", t_dirty_rebases),
    ("behind-reviewable", "BEHIND -> proceed: an advanced base no longer blocks a review",
     t_behind_reaches_the_enum_screen),
    ("unknown-mergestate-rechecks", "UNKNOWN merge state -> recheck", t_unknown_mergestate_rechecks),
    ("mergestate-total", "every mergeStateStatus value maps to a verdict (totality)",
     t_every_mergestate_value_is_mapped),
    ("conflicting-rebases", "CONFLICTING decided on .mergeable alone -> rebase-first", t_conflicting_rebases),
    ("unknown-mergeable-rechecks", "UNKNOWN mergeability -> recheck", t_unknown_mergeable_rechecks),
    ("unrecognised-mergestate-parks", "an unrecognised merge state parks (totality catch-all)",
     t_unrecognised_mergestate_value_parks),
    ("unrecognised-mergeable-parks", "an unrecognised mergeable value parks (totality catch-all)",
     t_unrecognised_mergeable_value_parks),
    ("conflicting+unknown-rechecks", "CONFLICTING + UNKNOWN merge state re-polls, never rebases",
     t_conflicting_with_unknown_mergestate_rechecks),
    ("conflicting+unrecognised-parks", "CONFLICTING + unrecognised merge state parks, never rebases",
     t_conflicting_with_unrecognised_mergestate_parks),
    ("dirty+unknown-mergeable-rechecks", "DIRTY + UNKNOWN mergeable re-polls, never rebases",
     t_dirty_with_unknown_mergeable_rechecks),
    ("behind+unknown-mergeable-rechecks", "BEHIND + UNKNOWN mergeable re-polls, never rebases",
     t_behind_with_unknown_mergeable_rechecks),
    ("file-frozen-parks", "Stage 3 preflight routes a FROZEN API view through ledger.py park",
     t_frozen_view_with_file_parks_candidate),
    ("file-unknown-rechecks", "Stage 3 preflight leaves recognized UNKNOWN active for re-poll",
     t_unknown_view_with_file_rechecks_without_park),
    ("cli-missing-ancestry", "a CLEAN view without base ancestry fails closed", t_cli_missing_ancestry_rechecks),
    ("cli-ancestry-spawn-failure", "a Git spawn failure fails closed to a structured ancestry recheck",
     t_cli_ancestry_spawn_failure_rechecks),
    ("cli-rebase-first", "check --view-json on a DIRTY view exits non-zero with rebase-first", t_cli_rebase_first),
    ("cli-malformed", "a view missing a field fails closed to recheck, never KeyError", t_cli_malformed),
    ("cli-bad-project-root", "an invalid --project-root fails closed to recheck, no traceback",
     t_cli_bad_project_root_fails_closed),
    ("cli-undecodable-view-json", "a --view-json that is not UTF-8 fails closed to recheck, no traceback",
     t_cli_undecodable_view_json_fails_closed),
    ("cli-undecodable-gh-stdout", "gh stdout that is not UTF-8 fails closed to recheck, no traceback",
     t_cli_undecodable_gh_stdout_fails_closed),
    ("cli-deep-view-json", "a --view-json too deeply nested to parse fails closed to recheck, no traceback",
     t_cli_deep_view_json_fails_closed),
    ("cli-deep-gh-stdout", "gh stdout too deeply nested to parse fails closed to recheck, no traceback",
     t_cli_deep_gh_stdout_fails_closed),
    ("cli-hostile-responses", "every hostile response in the shared table fails closed at BOTH parse sites",
     t_cli_hostile_responses_never_escape),
    ("cli-legacy-view-messages", "every pre-existing view-fetch failure keeps its EXACT reason string",
     t_cli_legacy_view_errors_keep_their_exact_wording),
    ("gh-writing-restores-absent-path", "gh_writing restores an absent PATH by deleting it, never as ''",
     t_gh_writing_restores_an_absent_path),
    ("clean-view-stale-base", "a CLEAN second candidate behind a merged sibling proceeds, reporting no",
     t_clean_view_with_stale_base_records_no),
    ("clean-view-current-base", "a CLEAN candidate containing fetched base proceeds, reporting yes",
     t_clean_view_with_current_base_proceeds),
    ("force-rewritten-base", "a rewritten remote base is refreshed before ancestry reports stale",
     t_force_rewritten_base_refreshes_and_rebases),
    ("literal-head-base",
     "a literal HEAD base uses a private ref and leaves the symbolic origin/HEAD target unchanged",
     t_literal_head_base_does_not_follow_remote_head_symref),
    ("dash-base-current", "a dash-leading base refreshes its tracking ref and reports current ancestry",
     t_dash_leading_current_base_refreshes_and_proceeds),
    ("dash-base-stale", "a dash-leading base refreshes its tracking ref and reports stale ancestry",
     t_dash_leading_stale_base_refreshes_and_reports_no),
    ("candidate-revision-not-head", "an explicit candidate revision is checked instead of a moved local HEAD",
     t_candidate_revision_is_checked_instead_of_moved_head),
    ("proceed-file-records-base-ok", "a proceed with --file stamps base_ok_sha = HEAD; without --file writes nothing",
     t_proceed_with_file_records_base_ok),
    ("stale-base-still-stamps", "a BEHIND PR still stamps base_ok_sha and records base_current=no",
     t_stale_base_with_file_still_stamps_and_records_no),
    ("non-proceed-file-no-stamp", "rebase-first/recheck never stamp base_ok_sha, even with --file",
     t_non_proceed_with_file_leaves_base_ok),
    ("file-base-assertion-mismatch", "--file: --base disagreeing with the row's effective base rechecks",
     t_file_base_assertion_mismatch_rechecks),
    ("file-origin-prefixed-base", "--file: an origin/<base> --base satisfies the assertion",
     t_file_origin_prefixed_base_matches),
    ("file-origin-named-base", "--file: a base literally named origin/<x> matches itself; a bare form disagrees",
     t_file_origin_named_base_matches_itself),
    ("file-live-retarget", "--file: a live baseRefName retarget rechecks with the machine-blocker wording",
     t_file_live_retarget_rechecks),
    ("file-legacy-row-header-base", "--file: an old row inherits the header base for the live comparison",
     t_file_legacy_row_inherits_header_base),
    ("file-missing-row", "--file: an unknown PR row fails closed to recheck", t_file_missing_row_rechecks),
    ("cli-help-file-writes", "check --help names proceed and park writes; absent --file stays pure",
     t_cli_help_names_both_file_writes),
]
