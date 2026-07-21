#!/usr/bin/env python3
"""Mint a campaign run-id and ATOMICALLY create its run directory.

A run-id namespaces everything a run owns — its `<rundir>`, its ledger, and its `gauntlet-run-<run-id>`
PR labels. Until now, minting one was an inline shell snippet in `run-identity-and-lease.md`
(a minute-resolution timestamp plus a short random hex suffix) and the "create the run dir atomically, retry on
the rare clash" step was adapter **pseudocode** (`create_run_directory(...)`) with no bundled
implementation. THIS script now owns both — the mint and the atomic create + retry — and the adapter's
`create_run_directory` delegates here. That
atomicity is a real correctness property, not a detail: the `mkdir` that FAILS when the directory already
exists is the single thing that stops two freshly-started runs from silently sharing one rundir and ledger
— the exact double-drive the lease exists to prevent, one layer up. Prose a driver reproduces by hand is
where that guarantee quietly goes missing; a tool is where it holds.

This owns run-id minting and the atomic create-or-retry. It does NOT mint the agent token (that is
`lease.py mint`, deliberately separate and run-id-independent) and it does NOT arm the heartbeat or take
the lease — those remain their own steps (`run-identity-and-lease.md`, `lease.py`).
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

from _gauntlet.modules import load_module_from_path

_HERE = Path(__file__).resolve().parent
SIBLING = _HERE / "run-id-test.py"

# 4 bytes = 8 hex chars = 32 bits, the same width `lease.py mint` uses for the agent token.
# Kept identical so the two random values read the same.
RAND_BYTES = 4
# Minute-resolution timestamps collide only for runs started in the same minute; the random suffix then
# carries uniqueness, and the atomic mkdir + retry is the backstop. A handful of attempts is plenty — the
# suffix is 32 bits, so a clash is already rare, and exhausting the retries means something is wrong
# (a non-collision mkdir failure), which must fail closed rather than loop.
DEFAULT_MAX_ATTEMPTS = 8


class RunIdCollision(Exception):
    """Could not mint a FRESH run directory — refuse rather than reuse an existing run's dir."""


def make_run_id(now: datetime, rand: str) -> str:
    """Build a run-id: ``g`` + ``YYMMDD-HHMM`` + ``-`` + ``rand``. Pure — inject ``now`` and ``rand``.

    The shape is filesystem- and label-safe (``[a-z0-9-]`` only): it is both a directory name and part of
    the ``gauntlet-run-<run-id>`` PR label. Local time matches the prose's `date +%y%m%d-%H%M` (no `-u`).
    """
    return f"g{now.strftime('%y%m%d-%H%M')}-{rand}"


def new_run(
    runs_dir: Path,
    *,
    now_fn: "Callable[[], datetime]",
    rand_fn: "Callable[[], str]",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> "tuple[str, Path]":
    """Mint a run-id and ATOMICALLY create ``<runs_dir>/<run-id>``; return ``(run_id, rundir)``.

    ``Path.mkdir()`` without ``exist_ok`` raises ``FileExistsError`` if the directory already exists — that
    atomic check-and-create is the guarantee: two fresh runs can never end up in one dir. On the rare
    collision, mint a FRESH id (new random suffix) and retry, bounded by ``max_attempts``. FAIL CLOSED:
    exhausting the attempts RAISES rather than reusing a dir — a run that cannot get its own directory must
    not start, exactly as `lease.py` refuses rather than adopt a run it cannot prove is free.

    The PARENT (`<runs_dir>`, i.e. `.gauntlet/tmp`) is created non-atomically (``parents=True,
    exist_ok=True``); it is shared scaffolding, not the per-run directory whose uniqueness matters.
    """
    runs_dir.mkdir(parents=True, exist_ok=True)
    last: "str | None" = None
    for _ in range(max_attempts):
        run_id = make_run_id(now_fn(), rand_fn())
        rundir = runs_dir / run_id
        try:
            rundir.mkdir()  # ATOMIC: raises FileExistsError if it already exists — never overwrites
        except FileExistsError:
            last = run_id
            continue
        return run_id, rundir
    raise RunIdCollision(
        f"could not mint a fresh run-id under {runs_dir} after {max_attempts} attempt(s) "
        f"(last tried {last!r}) — refusing to reuse an existing run directory"
    )


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="Mint a campaign run-id and create its run directory.")
    sub = parser.add_subparsers(dest="cmd")
    p_new = sub.add_parser("new", help="mint a run-id and ATOMICALLY create its run directory, printing "
                                       "{\"run_id\", \"rundir\"} as JSON")
    p_new.add_argument("--runs-dir", required=True,
                       help="the directory runs live under (e.g. <checkout>/.gauntlet/tmp); created if absent")
    p_new.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS,
                       help=f"collision-retry bound before failing closed (default {DEFAULT_MAX_ATTEMPTS})")
    sub.add_parser("self-test", help="run every fixture and assert the rules this file enforces still hold")
    args = parser.parse_args(argv)

    if args.cmd == "self-test":
        return self_test()
    if args.cmd == "new":
        try:
            run_id, rundir = new_run(
                Path(args.runs_dir),
                now_fn=datetime.now,
                rand_fn=lambda: secrets.token_hex(RAND_BYTES),
                max_attempts=args.max_attempts,
            )
        except (RunIdCollision, OSError) as exc:
            print(f"run-id: REFUSED — {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"run_id": run_id, "rundir": str(rundir)}))
        return 0

    parser.error("a subcommand is required (new | self-test)")


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
    mod = load_module_from_path("run_id_test", SIBLING, register=True)
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
        print(f"FAIL     {'sibling-fixtures':30} -> the fixtures in {SIBLING.name} must be RUNNABLE\n"
              f"         {exc}")
        print("\n1 check(s) FAILED — the run-id tool's contract is broken.")
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
        print(f"{failures} check(s) FAILED — the run-id tool's contract is broken.")
        return 1
    print(f"all {len(cases)} fixtures hold — the run-id tool's contract is intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
