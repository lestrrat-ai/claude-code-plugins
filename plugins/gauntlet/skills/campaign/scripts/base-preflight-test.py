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
import subprocess
import sys
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


def view(*, mergeable="MERGEABLE", mergeStateStatus="CLEAN", baseRefName="main") -> dict:
    # `baseRefName` is the PR's LIVE base: `check --file` compares it with the row's effective base and refuses
    # a retarget. `decide()` ignores it, so the pure-decide fixtures below are unaffected by its presence.
    return {"mergeable": mergeable, "mergeStateStatus": mergeStateStatus, "baseRefName": baseRefName}


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
    result = subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)],
                            capture_output=True, text=True, check=False)
    check(result.returncode == 0, f"could not create fixture remote: {result.stderr.strip()}")
    result = subprocess.run(["git", "clone", str(remote), str(seed)], capture_output=True, text=True, check=False)
    check(result.returncode == 0, f"could not clone fixture seed: {result.stderr.strip()}")
    _configure_repo(seed)
    (seed / "f").write_text("base\n", encoding="utf-8")
    _git(seed, "add", "f")
    _git(seed, "commit", "-m", "base")
    _git(seed, "push", "origin", "main")
    result = subprocess.run(["git", "clone", str(remote), str(candidate)], capture_output=True, text=True, check=False)
    check(result.returncode == 0, f"could not clone candidate worktree: {result.stderr.strip()}")
    _configure_repo(candidate)
    head = _git(candidate, "rev-parse", "HEAD").stdout.strip()
    return candidate, head


def t_proceed_with_file_records_base_ok():
    """A final `proceed` with `--file` stamps `base_ok_sha` = the worktree HEAD; the SAME check WITHOUT `--file`
    writes nothing (the pure decider is preserved)."""
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
        check(json.loads(out) == {"verdict": "proceed", "reason": "GitHub merge state permits base check"},
              f"a current base must proceed, got {out!r}")
        check(_base_ok_sha(ledger, "9") == head,
              f"proceed with --file did not record base_ok_sha = {head!r}: {_base_ok_sha(ledger, '9')!r}")

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
    ("cli-rebase-first", "check --view-json on a DIRTY view exits non-zero with rebase-first", t_cli_rebase_first),
    ("cli-malformed", "a view missing a field fails closed to recheck, never KeyError", t_cli_malformed),
    ("cli-bad-project-root", "an invalid --project-root fails closed to recheck, no traceback",
     t_cli_bad_project_root_fails_closed),
    ("clean-view-stale-base", "a CLEAN second candidate behind a merged sibling rebases",
     t_clean_view_with_stale_base_rebases),
    ("clean-view-current-base", "a CLEAN candidate containing fetched base proceeds",
     t_clean_view_with_current_base_proceeds),
    ("proceed-file-records-base-ok", "a proceed with --file stamps base_ok_sha = HEAD; without --file writes nothing",
     t_proceed_with_file_records_base_ok),
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
