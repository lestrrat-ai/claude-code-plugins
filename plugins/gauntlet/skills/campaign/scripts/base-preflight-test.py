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
# The four base-current states clear a fix; DIRTY/BEHIND demand a rebase; UNKNOWN re-polls. Together they
# cover every value in MERGE_STATE_STATUS_VALUES, so the mapping is pinned TOTALLY over the enum.

def t_clean_proceeds():
    expect(view(mergeStateStatus="CLEAN"), "proceed", "branch is current with base")


def t_has_hooks_proceeds():
    expect(view(mergeStateStatus="HAS_HOOKS"), "proceed", "branch is current with base")


def t_unstable_proceeds():
    # UNSTABLE is about a non-passing/still-running CHECK, not a stale base — a fix/review may proceed.
    expect(view(mergeStateStatus="UNSTABLE"), "proceed", "branch is current with base")


def t_blocked_proceeds():
    # BLOCKED is about branch-protection/permissions, not a stale base — base-currency still clears.
    expect(view(mergeStateStatus="BLOCKED"), "proceed", "branch is current with base")


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


# --- CLI: a recorded view makes `check` testable without gh --------------------

def t_cli_proceed():
    with tempfile.TemporaryDirectory() as d:
        vjson = Path(d) / "view.json"
        vjson.write_text(json.dumps(view()), encoding="utf-8")
        code, out, err = capture_cli(M.main, ["check", "--pr", "9", "--view-json", str(vjson)])
        check(code == 0, f"a `proceed` verdict must exit 0 (stderr: {err})")
        check(json.loads(out) == {"verdict": "proceed", "reason": "branch is current with base"},
              f"the CLI should print the proceed verdict, got {out!r}")


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


CASES = [
    ("clean-proceeds", "CLEAN -> proceed", t_clean_proceeds),
    ("has-hooks-proceeds", "HAS_HOOKS -> proceed", t_has_hooks_proceeds),
    ("unstable-proceeds", "UNSTABLE is a check signal, not a stale base -> proceed", t_unstable_proceeds),
    ("blocked-proceeds", "BLOCKED is a permission signal, not a stale base -> proceed", t_blocked_proceeds),
    ("dirty-rebases", "DIRTY -> rebase-first", t_dirty_rebases),
    ("behind-rebases", "BEHIND -> rebase-first", t_behind_rebases),
    ("unknown-mergestate-rechecks", "UNKNOWN merge state -> recheck", t_unknown_mergestate_rechecks),
    ("mergestate-total", "every mergeStateStatus value maps to a verdict (totality)",
     t_every_mergestate_value_is_mapped),
    ("conflicting-rebases", "CONFLICTING decided on .mergeable alone -> rebase-first", t_conflicting_rebases),
    ("unknown-mergeable-rechecks", "UNKNOWN mergeability -> recheck", t_unknown_mergeable_rechecks),
    ("unrecognised-value-rechecks", "an unrecognised merge state re-polls (totality catch-all)",
     t_unrecognised_mergestate_value_rechecks),
    ("cli-proceed", "check --view-json on a CLEAN view exits 0 with proceed", t_cli_proceed),
    ("cli-rebase-first", "check --view-json on a DIRTY view exits non-zero with rebase-first", t_cli_rebase_first),
    ("cli-malformed", "a view missing a field fails closed to recheck, never KeyError", t_cli_malformed),
]
