#!/usr/bin/env python3
"""Emit the scheduled-heartbeat callback command — the exact line an agent schedules for its next wake.

A scheduled heartbeat is the SAME owner resuming its own run, so when that callback runs it goes through
`lease.py`'s REFRESH path, not `acquire`. Per the single-user policy (`AGENTS.md`), an owner's refresh needs
NO heartbeat proof. So the callback this tool prints carries ONLY two flags:

    <invocation> --run <run-id> --token <agent-token>

and nothing else. In particular:

- It NEVER carries `--heartbeat-id`. That is an ACQUIRE-TIME proof — minted when the heartbeat is armed and
  handed to `lease.py acquire` to take the run. A resuming heartbeat refreshes and re-proves nothing, so the
  proof has no place in the callback. (This is the one thing the old `lease.py` NO_HEARTBEAT message got
  wrong, and this tool is the corrected owner of that command.)
- It NEVER carries `--new` or a `#PR` argument. Those are START-TIME args that CREATE or ADOPT a run;
  replaying them on every heartbeat would mint a fresh run each wake. `--run` RESUMES the existing one.
- It hardcodes NO host form. The invocation string (`/gauntlet:campaign` on Claude Code,
  `$gauntlet:campaign` on Codex) is PASSED IN via `--invocation`, so this tool is host-neutral.

This tool does NOT arm the heartbeat. Arming is the host's own `ScheduleWakeup`/cron call, which is an
agent-only action that cannot be scripted — `runtime-adapter.md` owns that mechanism. This tool only PRINTS
the command the agent then schedules through it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _gauntlet.modules import load_module_from_path

DESCRIPTION = "Print the scheduled-heartbeat callback command (two flags only: --run and --token)."

_HERE = Path(__file__).resolve().parent
SIBLING = _HERE / "heartbeat-test.py"


def callback_command(invocation: str, run_id: str, token: str) -> str:
    """Assemble the scheduled-heartbeat callback: EXACTLY two flags, `--run` and `--token`.

    A scheduled heartbeat resumes the same owner, hitting `lease.py`'s refresh path, which needs no proof —
    so the callback never carries `--heartbeat-id` (acquire-time only) and never carries `--new`/`#PR`
    (start-time args that would mint a fresh run each wake). The host form is supplied by the caller as
    `invocation`, so this stays host-neutral.
    """
    return f"{invocation} --run {run_id} --token {token}"


# --- cli ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest="cmd")

    cb = sub.add_parser("callback", help="print the command to schedule for the next heartbeat")
    cb.add_argument("--run", required=True, help="the run id to RESUME (never mints a new run)")
    cb.add_argument("--token", required=True, help="the agent token the heartbeat carries")
    cb.add_argument("--invocation", required=True,
                    help="the host campaign invocation (Claude Code `/gauntlet:campaign`, "
                         "Codex `$gauntlet:campaign`) — passed in so this tool hardcodes no host form")

    sub.add_parser("self-test", help="run every fixture and assert the rules this file enforces still hold")
    return parser


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "self-test":
        return self_test()

    if args.cmd == "callback":
        # Fail closed: a blank required value must never be assembled into the callback. Refuse loudly on
        # stderr, print NOTHING on stdout, and return non-zero so a caller cannot schedule a broken command.
        for flag, val in (("--run", args.run), ("--token", args.token), ("--invocation", args.invocation)):
            if not (val or "").strip():
                print(f"heartbeat: REFUSED — {flag} is empty or whitespace-only. A blank required value "
                      f"cannot go into the scheduled-heartbeat callback. Nothing was printed.",
                      file=sys.stderr)
                return 1
        print(callback_command(args.invocation, args.run, args.token))
        return 0

    parser.error("a subcommand is required (callback | self-test)")


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
    mod = load_module_from_path("heartbeat_test", SIBLING, register=True)
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
        print("\n1 check(s) FAILED — the heartbeat callback tool's contract is broken.")
        return 1
    for name, rule, fn in cases:
        try:
            fn()
        except SelfTestFailure as exc:
            print(f"FAIL     {name:30} -> {rule}\n         {exc}")
            failures += 1
        except Exception as exc:  # noqa: BLE001 — a fixture that CRASHES has not passed
            print(f"FAIL     {name:30} -> {rule}\n         raised {type(exc).__name__}: {exc}")
            failures += 1
        else:
            print(f"ok       {name:30} -> {rule}")
    print()
    if failures:
        print(f"{failures} check(s) FAILED — the heartbeat callback tool's contract is broken.")
        return 1
    print(f"all {len(cases)} fixtures hold — the heartbeat callback tool's contract is intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
