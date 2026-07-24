#!/usr/bin/env python3
# ci: pyright
"""Fixtures for `reviewer-liveness.py` — the stdout-stream liveness probe.

They live in a SIBLING file, and `reviewer-liveness.py self-test` FAILS LOUDLY if it cannot load them.

EVERY FIXTURE PINS A RULE WITH TEETH: it asserts the verdict on one side of a boundary AND the opposite
verdict on the other, so a probe that returned a constant would go red. The probe reports facts and
decides nothing; these fixtures pin exactly which fact each input produces.
"""
from __future__ import annotations

import builtins
import io
import os
import tempfile
from pathlib import Path

from _gauntlet.modules import load_module_from_path

OWNER = Path(__file__).resolve().parent / "reviewer-liveness.py"


def _load_owner():
    mod = load_module_from_path("reviewer_liveness_owner", OWNER)
    if mod is None:
        raise RuntimeError(f"cannot load the reviewer-liveness probe at {OWNER}")
    return mod


R = _load_owner()
WINDOW = R.DEFAULT_QUIET_WINDOW_SECONDS


def check(cond, msg):
    if not cond:
        raise R.SelfTestFailure(msg)


def classify(*, exists, size, mtime_epoch, now_epoch, quiet_window=WINDOW):
    return R.classify(exists=exists, size=size, mtime_epoch=mtime_epoch,
                      now_epoch=now_epoch, quiet_window=quiet_window)


# --- alive vs quiet: the core boundary ----------------------------------------

def t_recent_write_reads_alive():
    # A stream written well within the window is a live, emitting process.
    v = classify(exists=True, size=4096, mtime_epoch=1000.0, now_epoch=1000.0 + 5)
    check(v["verdict"] == "alive", "a stream written 5s ago (window 180s) must read 'alive', not "
          f"{v['verdict']!r}")


def t_quiet_past_window_reads_quiet():
    # No write for longer than the window is a hung-process candidate.
    v = classify(exists=True, size=4096, mtime_epoch=1000.0, now_epoch=1000.0 + WINDOW + 30)
    check(v["verdict"] == "quiet", f"a stream unwritten for window+30s must read 'quiet', not "
          f"{v['verdict']!r}")


def t_window_boundary_is_closed_on_quiet():
    # The boundary is EXACT and closed on 'quiet': age == window is quiet; one second less is alive.
    at_window = classify(exists=True, size=1, mtime_epoch=1000.0, now_epoch=1000.0 + WINDOW)
    just_under = classify(exists=True, size=1, mtime_epoch=1000.0, now_epoch=1000.0 + WINDOW - 1)
    check(at_window["verdict"] == "quiet", "age == window must read 'quiet' (boundary closed on quiet)")
    check(just_under["verdict"] == "alive", "age == window-1 must read 'alive' — the boundary has teeth")


def t_future_mtime_reads_alive():
    # A future mtime (clock skew, or a write mid-stat) is a FRESH write — never 'quiet'.
    v = classify(exists=True, size=10, mtime_epoch=1000.0, now_epoch=1000.0 - 60)
    check(v["verdict"] == "alive", "a future mtime (negative age) must read 'alive', never 'quiet'")


# --- absent stream ------------------------------------------------------------

def t_absent_stream_reads_absent():
    v = classify(exists=False, size=None, mtime_epoch=None, now_epoch=1000.0)
    check(v["verdict"] == "absent", "a missing stream file must read 'absent'")
    check(v["stream_bytes"] is None and v["age_seconds"] is None,
          "an absent stream reports no size and no age")


# --- the reported facts, and that it STATS (never reads) ----------------------

def t_reports_size_when_present_and_none_when_absent():
    present = classify(exists=True, size=777, mtime_epoch=1000.0, now_epoch=1000.0 + 1)
    absent = classify(exists=False, size=None, mtime_epoch=None, now_epoch=1000.0)
    check(present["stream_bytes"] == 777, "a present stream must report its byte size")
    check(absent["stream_bytes"] is None, "an absent stream must report None, never a number — teeth")


def t_probe_uses_real_stat_never_reads_content():
    # Exercise the real os.stat path: size comes from stat, verdict from the file's own mtime — the probe
    # never OPENS the file for reading. An old mtime reads 'quiet'; a fresh one reads 'alive'.
    #
    # TEETH for the load-bearing safety property: the transcript is large, so probe() must reach its
    # result via os.stat ALONE and never open the stream (opening + reading it would flood the driver's
    # context). We PIN that here by forbidding open during the probe() call: builtins.open and io.open are
    # patched to raise the instant anything tries to open the stream path, so a future regression that
    # starts reading the transcript turns this fixture RED instead of passing green. os.stat is left
    # untouched, so the size/mtime path a correct probe uses still works. The tempfile is written BEFORE
    # the guard is installed, so only the probe() call runs under it, and the originals are restored in a
    # finally so the patch cannot leak to another fixture.
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "review-1-1.out"
        body = b"streamed reasoning tokens\n" * 100
        p.write_bytes(body)
        os.utime(p, (1000.0, 1000.0))  # mtime far in the past relative to our injected 'now'
        target = os.path.realpath(str(p))

        real_builtins_open = builtins.open
        real_io_open = io.open

        def _forbid_stream_open(orig):
            def guarded(file, *args, **kwargs):
                try:
                    same = os.path.realpath(os.fspath(file)) == target
                except TypeError:
                    same = False  # a non-path (e.g. an int fd) is never our stream
                if same:
                    raise AssertionError(
                        "probe() OPENED the stream file — it must stat only, never read the transcript")
                return orig(file, *args, **kwargs)
            return guarded

        builtins.open = _forbid_stream_open(real_builtins_open)
        io.open = _forbid_stream_open(real_io_open)
        try:
            stale = R.probe(str(p), WINDOW, now_epoch=1000.0 + WINDOW + 5)
            fresh = R.probe(str(p), WINDOW, now_epoch=1000.0 + 1)
        finally:
            builtins.open = real_builtins_open
            io.open = real_io_open

        check(stale["verdict"] == "quiet", "a real file with an old mtime must read 'quiet' via os.stat")
        check(stale["stream_bytes"] == len(body),
              "stream_bytes must equal the file's real size (proving it stat'd, not guessed)")
        check(fresh["verdict"] == "alive", "the same file read at a fresh 'now' must read 'alive' — teeth")


def t_missing_path_probes_absent():
    with tempfile.TemporaryDirectory() as d:
        gone = Path(d) / "never-created.out"
        v = R.probe(str(gone), WINDOW, now_epoch=1000.0)
        check(v["verdict"] == "absent", "probing a nonexistent path must read 'absent', never crash")


CASES = [
    ("recent-alive", "a recent write reads alive", t_recent_write_reads_alive),
    ("quiet-past-window", "no write past the window reads quiet", t_quiet_past_window_reads_quiet),
    ("boundary-closed-quiet", "age == window is quiet; window-1 is alive", t_window_boundary_is_closed_on_quiet),
    ("future-mtime-alive", "a future mtime reads alive, never quiet", t_future_mtime_reads_alive),
    ("absent-reads-absent", "a missing stream reads absent with no size/age", t_absent_stream_reads_absent),
    ("reports-size", "size reported when present, None when absent", t_reports_size_when_present_and_none_when_absent),
    ("stats-never-reads", "probe stats the real file (size from stat), never reads it", t_probe_uses_real_stat_never_reads_content),
    ("missing-path-absent", "probing a missing path reads absent, never crashes", t_missing_path_probes_absent),
]
