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


def watchdog_command(invocation: str, run_id: str) -> str:
    """Assemble the watchdog resurrection poke: `<invocation> --run <run-id> --watchdog`, and NOTHING else.

    A poke is fired by a persistent scheduler to check on a run whose heartbeat chain may have died. It is
    TOKEN-FREE BY DESIGN: unlike the `callback` (a resume by the SAME owner, which carries `--token`), a poke
    might land BESIDE a still-live heartbeat chain. A token-bearing poke could then be adopted as a second
    driver and DOUBLE-DRIVE the one run, so the poke carries no token — it resolves the lease first and stands
    down when the primary is alive. It likewise carries no `--new`/`#PR` (start-time args that would mint a
    fresh run) and no `--heartbeat-id` (an acquire-time proof). The host form is passed in, so this stays
    host-neutral, exactly like `callback_command`.
    """
    return f"{invocation} --run {run_id} --watchdog"


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

    wd = sub.add_parser("watchdog", help="print the TOKEN-FREE watchdog resurrection poke command")
    wd.add_argument("--run", required=True, help="the run id the poke checks on (never mints a new run)")
    wd.add_argument("--invocation", required=True,
                    help="the host campaign invocation — passed in so this tool hardcodes no host form")

    sub.add_parser("self-test", help="run every fixture and assert the rules this file enforces still hold")
    return parser


def _reject_unusable(fields: "list[tuple[str, str]]") -> bool:
    """Fail closed on any required value that is empty OR contains ANY whitespace. Shared by `callback` and
    `watchdog`: whitespace is the argument-injection seam — a `--run "g1 --new #99"` would smuggle extra
    tokens past the fixed-shape guarantee once the printed line is re-split into argv. No legitimate value (a
    `g<date>-<rand>` run-id, a hex token, a `/gauntlet:campaign` invocation) carries whitespace. On a bad
    value: print the refusal on stderr, print NOTHING on stdout, return True so the caller returns non-zero.
    """
    for flag, val in fields:
        if not val or any(ch.isspace() for ch in val):
            print(f"heartbeat: REFUSED — {flag} must not be empty or contain whitespace. A value that is "
                  f"blank or carries any whitespace character (space, tab, newline) cannot go into the "
                  f"scheduled command: whitespace would smuggle extra argv tokens past the fixed-shape "
                  f"guarantee. Nothing was printed.", file=sys.stderr)
            return True
    return False


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "self-test":
        return self_test()

    if args.cmd == "callback":
        if _reject_unusable([("--run", args.run), ("--token", args.token),
                             ("--invocation", args.invocation)]):
            return 1
        print(callback_command(args.invocation, args.run, args.token))
        return 0

    if args.cmd == "watchdog":
        # The poke is TOKEN-FREE by design, so it validates only `--run` and `--invocation` — the same
        # empty/whitespace fail-closed the callback applies, so a smuggled `--token`/`--new`/`#PR` hidden
        # behind whitespace can never survive to stdout and be re-split into a double-driving argv.
        if _reject_unusable([("--run", args.run), ("--invocation", args.invocation)]):
            return 1
        print(watchdog_command(args.invocation, args.run))
        return 0

    parser.error("a subcommand is required (callback | watchdog | self-test)")


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
