#!/usr/bin/env python3
"""Fixtures for `base-preflight.py` — the base-currency decider.

They live in a SIBLING file, and `base-preflight.py self-test` FAILS LOUDLY if it cannot load them.

EVERY FIXTURE HAS TEETH. It asserts the EXACT verdict AND, where the wording is load-bearing, the EXACT
reason — a suite that only checked `verdict == "rebase-first"` would pass against a decider that returned the
wrong reason, and the reason is what the driver acts on. There is one fixture PER `mergeStateStatus` value so
the mapping is pinned TOTALLY over the enum, plus the unrecognised-value fixture that pins the catch-all.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from _gauntlet.modules import load_module_from_path
from _gauntlet.testing import capture_cli

OWNER = Path(__file__).resolve().parent / "base-preflight.py"


def _load_owner():
    mod = load_module_from_path("base_preflight_owner", OWNER)
    if mod is None:
        raise RuntimeError(f"cannot load the base-currency decider at {OWNER}")
    return mod


M = _load_owner()


def view(*, mergeable="MERGEABLE", mergeStateStatus="CLEAN") -> dict:
    return {"mergeable": mergeable, "mergeStateStatus": mergeStateStatus}


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise M.SelfTestFailure(msg)


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


def t_behind_rebases():
    expect(view(mergeStateStatus="BEHIND"), "rebase-first", "base has moved ahead — rebase first")


def t_unknown_mergestate_rechecks():
    expect(view(mergeStateStatus="UNKNOWN"), "recheck", "mergeability not computed yet — re-poll")


def t_every_mergestate_value_is_mapped():
    # TOTALITY, mechanically: every value the schema declares for mergeStateStatus resolves to a verdict
    # (never a crash), and to one of the three legal verdicts. The per-value fixtures above pin WHICH; this
    # pins that NONE is left unmapped.
    for value in M.MERGE_STATE_STATUS_VALUES:
        got = M.decide(view(mergeStateStatus=value))
        check(got["verdict"] in (M.PROCEED, M.REBASE_FIRST, M.RECHECK),
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

def t_unrecognised_mergestate_value_rechecks():
    # A value GitHub's schema does not declare — the catch-all re-polls it, never guesses `proceed`. Pins
    # that the mapping is TOTAL: an unclassified value is fail-closed, not silently cleared.
    expect(view(mergeStateStatus="FROZEN"), "recheck", "unknown merge state FROZEN — re-poll, never guess")


# --- cross-enum precedence: UNKNOWN / unrecognised WINS over a recognised rebase state ------------
# A view with a recognised CONFLICTING/DIRTY/BEHIND on ONE half and an UNKNOWN or unrecognised value on the
# OTHER must NOT be steered to `rebase-first` on the half we recognise — the uncomputed/unclassified half
# wins and we re-poll on a full view. These pin the ordering: fail-safe BEFORE act.

def t_conflicting_with_unknown_mergestate_rechecks():
    # CONFLICTING mergeable but mergeStateStatus not yet computed — re-poll, do NOT rebase on half a view.
    expect(view(mergeable="CONFLICTING", mergeStateStatus="UNKNOWN"), "recheck",
           "mergeability not computed yet — re-poll")


def t_conflicting_with_unrecognised_mergestate_rechecks():
    # CONFLICTING mergeable but an unrecognised merge state (one GitHub added since) — re-poll, never guess.
    expect(view(mergeable="CONFLICTING", mergeStateStatus="FROZEN"), "recheck",
           "unknown merge state FROZEN — re-poll, never guess")


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


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False)
    check(result.returncode == 0,
          f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}")
    return result


def _configure_repo(path: Path) -> None:
    _git(path, "config", "user.email", "fixture@example.invalid")
    _git(path, "config", "user.name", "Fixture")


def t_clean_view_with_stale_base_rebases():
    """A prior campaign merge advances main while GitHub still calls the second PR CLEAN."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        remote = root / "remote.git"
        seed = root / "seed"
        candidate = root / "candidate"

        result = subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)],
                                capture_output=True, text=True, check=False)
        check(result.returncode == 0, f"could not create fixture remote: {result.stderr.strip()}")
        result = subprocess.run(["git", "clone", str(remote), str(seed)],
                                capture_output=True, text=True, check=False)
        check(result.returncode == 0, f"could not clone fixture seed: {result.stderr.strip()}")
        _configure_repo(seed)

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

        result = subprocess.run(["git", "clone", str(remote), str(candidate)],
                                capture_output=True, text=True, check=False)
        check(result.returncode == 0, f"could not clone candidate worktree: {result.stderr.strip()}")
        _configure_repo(candidate)
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
        check(code != 0,
              f"a CLEAN view whose worktree lacks the advanced base must stop for rebase (stderr: {err})")
        check(json.loads(out) == {"verdict": "rebase-first", "reason": "base has moved ahead — rebase first"},
              f"a stale base must rebase despite CLEAN GitHub enums, got {out!r}")


def t_clean_view_with_current_base_proceeds():
    """A CLEAN PR whose HEAD contains fetched main may proceed."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        remote = root / "remote.git"
        seed = root / "seed"
        candidate = root / "candidate"

        result = subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)],
                                capture_output=True, text=True, check=False)
        check(result.returncode == 0, f"could not create fixture remote: {result.stderr.strip()}")
        result = subprocess.run(["git", "clone", str(remote), str(seed)],
                                capture_output=True, text=True, check=False)
        check(result.returncode == 0, f"could not clone fixture seed: {result.stderr.strip()}")
        _configure_repo(seed)
        (seed / "f").write_text("base\n", encoding="utf-8")
        _git(seed, "add", "f")
        _git(seed, "commit", "-m", "base")
        _git(seed, "push", "origin", "main")

        result = subprocess.run(["git", "clone", str(remote), str(candidate)],
                                capture_output=True, text=True, check=False)
        check(result.returncode == 0, f"could not clone candidate worktree: {result.stderr.strip()}")
        _configure_repo(candidate)

        vjson = root / "clean-view.json"
        vjson.write_text(json.dumps(view()), encoding="utf-8")
        code, out, err = capture_cli(
            M.main,
            ["check", "--pr", "9", "--view-json", str(vjson), "--worktree", str(candidate),
             "--base", "main"],
        )
        check(code == 0, f"a candidate containing the fetched base must proceed (stderr: {err})")
        check(json.loads(out) == {"verdict": "proceed", "reason": "GitHub merge state permits base check"},
              f"a current base must permit the candidate, got {out!r}")


CASES = [
    ("clean-proceeds", "CLEAN passes the enum screen", t_clean_proceeds),
    ("has-hooks-proceeds", "HAS_HOOKS passes the enum screen", t_has_hooks_proceeds),
    ("unstable-proceeds", "UNSTABLE is a check signal and reaches the graph check", t_unstable_proceeds),
    ("blocked-proceeds", "BLOCKED is a permission signal and reaches the graph check", t_blocked_proceeds),
    ("dirty-rebases", "DIRTY -> rebase-first", t_dirty_rebases),
    ("behind-rebases", "BEHIND -> rebase-first", t_behind_rebases),
    ("unknown-mergestate-rechecks", "UNKNOWN merge state -> recheck", t_unknown_mergestate_rechecks),
    ("mergestate-total", "every mergeStateStatus value maps to a verdict (totality)",
     t_every_mergestate_value_is_mapped),
    ("conflicting-rebases", "CONFLICTING decided on .mergeable alone -> rebase-first", t_conflicting_rebases),
    ("unknown-mergeable-rechecks", "UNKNOWN mergeability -> recheck", t_unknown_mergeable_rechecks),
    ("unrecognised-value-rechecks", "an unrecognised merge state re-polls (totality catch-all)",
     t_unrecognised_mergestate_value_rechecks),
    ("conflicting+unknown-rechecks", "CONFLICTING + UNKNOWN merge state re-polls, never rebases",
     t_conflicting_with_unknown_mergestate_rechecks),
    ("conflicting+unrecognised-rechecks", "CONFLICTING + unrecognised merge state re-polls, never rebases",
     t_conflicting_with_unrecognised_mergestate_rechecks),
    ("dirty+unknown-mergeable-rechecks", "DIRTY + UNKNOWN mergeable re-polls, never rebases",
     t_dirty_with_unknown_mergeable_rechecks),
    ("behind+unknown-mergeable-rechecks", "BEHIND + UNKNOWN mergeable re-polls, never rebases",
     t_behind_with_unknown_mergeable_rechecks),
    ("cli-missing-ancestry", "a CLEAN view without base ancestry fails closed", t_cli_missing_ancestry_rechecks),
    ("cli-rebase-first", "check --view-json on a DIRTY view exits non-zero with rebase-first", t_cli_rebase_first),
    ("cli-malformed", "a view missing a field fails closed to recheck, never KeyError", t_cli_malformed),
    ("cli-bad-project-root", "an invalid --project-root fails closed to recheck, no traceback",
     t_cli_bad_project_root_fails_closed),
    ("clean-view-stale-base", "a CLEAN second candidate behind a merged sibling rebases",
     t_clean_view_with_stale_base_rebases),
    ("clean-view-current-base", "a CLEAN candidate containing fetched base proceeds",
     t_clean_view_with_current_base_proceeds),
]
