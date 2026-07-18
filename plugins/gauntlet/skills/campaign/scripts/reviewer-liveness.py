#!/usr/bin/env python3
"""Report whether a dispatched reviewer's OUTPUT STREAM is still moving.

The review gate already declares a reviewer stalled when no MEANINGFUL PROGRESS —
a planned unit `done`, or an accepted amendment — lands for ~15 min
(`stage-2-review-gate.md`). That timer is deliberately coarse: progress events
fire only when a whole unit completes, minutes apart, so BETWEEN units a live
reviewer grinding one unit and a hung one look identical until the cap.

A cross-engine reviewer launched as a background task writes its reasoning to a
stdout stream that grows CONTINUOUSLY while the model emits — a far finer
PROCESS-LIVENESS signal than the unit-granular progress file. This probe reads
that stream's SIZE and MTIME ONLY (never its content — the transcript is large
and reading it would flood the driver's context) and reports whether it was
written within a quiet window.

It DECIDES NOTHING and always exits 0. It is a MISS-CATCHER, not a verdict: a
growing stream proves the process is ALIVE, so the driver need not declare a
false stall while the progress file is merely coarse-stale; a stream quiet past
the window CORROBORATES a hang, so the driver can apply `reviewer.md`'s retry
budget without waiting the full meaningful-progress cap. Crucially, stream growth
is PROCESS liveness, NOT meaningful progress: it MUST NOT reset the
meaningful-progress timer (`stage-2-review-gate.md`) — a reviewer that streams
forever without completing a unit is still stalled at that cap. This signal kills
a hung process faster; it never extends patience for one that will not converge.

Only the background-task (cross-engine) route has a pollable stream. A
native-worker reviewer's transcript is not safely pollable, so that route keeps
the progress-file + completion-notification model; do not point this probe at it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from _gauntlet.modules import load_module_from_path

DESCRIPTION = "Report a background reviewer's stdout-stream liveness (stat only; decides nothing)."

# A live streaming reviewer writes its stdout far more often than this; a stream with NO write for a
# full window, while no meaningful progress has landed, is a hung-process candidate. Kept well under the
# ~15-min meaningful-progress cap so a dead process is caught sooner, never later.
DEFAULT_QUIET_WINDOW_SECONDS = 180

_HERE = Path(__file__).resolve().parent
SIBLING = _HERE / "reviewer-liveness-test.py"


def classify(*, exists: bool, size: "int | None", mtime_epoch: "float | None",
             now_epoch: float, quiet_window: int) -> dict:
    """Pure verdict from a stat result. `alive` iff the stream was written within the window.

    - absent: no stream file yet (the task has not started, or its output is gone).
    - alive:  age < quiet_window — written recently, so the process is emitting NOW.
    - quiet:  age >= quiet_window — no write for a full window; a hung-process candidate to
              corroborate against the (unchanged) meaningful-progress state before acting.
    The boundary is closed on `quiet`: age == quiet_window reads `quiet`.
    """
    if not exists:
        return {"verdict": "absent", "stream_bytes": None, "age_seconds": None,
                "quiet_window_seconds": quiet_window}
    assert mtime_epoch is not None
    age = now_epoch - mtime_epoch
    verdict = "quiet" if age >= quiet_window else "alive"
    return {"verdict": verdict, "stream_bytes": size, "age_seconds": age,
            "quiet_window_seconds": quiet_window}


def probe(stream: str, quiet_window: int, now_epoch: float) -> dict:
    """Stat the stream file (never read it) and classify. Missing file -> absent."""
    try:
        st = os.stat(stream)
    except FileNotFoundError:
        return classify(exists=False, size=None, mtime_epoch=None,
                        now_epoch=now_epoch, quiet_window=quiet_window)
    return classify(exists=True, size=st.st_size, mtime_epoch=st.st_mtime,
                    now_epoch=now_epoch, quiet_window=quiet_window)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("probe", help="report the stream's liveness verdict as JSON")
    p.add_argument("--stream", required=True,
                   help="path to the reviewer's background-task stdout file (stat'd, never read)")
    p.add_argument("--quiet-window-seconds", "--quiet_window_seconds", type=int,
                   default=DEFAULT_QUIET_WINDOW_SECONDS,
                   help=f"a stream unwritten for this long reads 'quiet' (default {DEFAULT_QUIET_WINDOW_SECONDS})")
    p.add_argument("--now-epoch", "--now_epoch", type=float, default=None,
                   help="reference 'now' as epoch seconds (default: the wall clock)")

    sub.add_parser("self-test", help="run every fixture and assert the rules this file enforces")

    args = parser.parse_args(argv)

    if args.cmd == "self-test":
        return self_test()
    if args.cmd == "probe":
        now = args.now_epoch if args.now_epoch is not None else _now()
        print(json.dumps(probe(args.stream, args.quiet_window_seconds, now)))
        return 0
    parser.print_help()
    return 2


def _now() -> float:
    import time
    return time.time()


# --- self-test ----------------------------------------------------------------

class SelfTestFailure(AssertionError):
    """A rule this file claims to enforce does not hold."""


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise SelfTestFailure(msg)


def sibling_cases() -> list:
    if not SIBLING.exists():
        raise SelfTestFailure(f"the fixture file {SIBLING} IS MISSING — this suite has no fixtures to run "
                              f"and CANNOT report health. Every rule this file enforces is now unpinned.")
    mod = load_module_from_path("reviewer_liveness_test", SIBLING, register=True)
    if mod is None:
        raise SelfTestFailure(f"{SIBLING} exists but cannot be loaded as a module")
    cases = getattr(mod, "CASES", None)
    if not cases:
        raise SelfTestFailure(f"{SIBLING} exports no CASES — every rule in this file is unpinned while the "
                              f"suite still exits 0")
    return list(cases)


def self_test() -> int:
    failures = 0
    try:
        cases = sibling_cases()
    except SelfTestFailure as exc:
        print(f"FAIL     {'sibling-fixtures':32} -> the fixtures in {SIBLING.name} must be RUNNABLE\n"
              f"         {exc}")
        print("\n1 check(s) FAILED — the reviewer-liveness probe's contract is broken.")
        return 1
    for name, rule, fn in cases:
        try:
            fn()
        except SelfTestFailure as exc:
            print(f"FAIL     {name:32} -> {rule}\n         {exc}")
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL     {name:32} -> {rule}\n         raised {type(exc).__name__}: {exc}")
            failures += 1
        else:
            print(f"ok       {name:32} -> {rule}")
    print()
    if failures:
        print(f"{failures} check(s) FAILED — the reviewer-liveness probe's contract is broken.")
        return 1
    print(f"all {len(cases)} fixtures hold — the reviewer-liveness probe's contract is intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
