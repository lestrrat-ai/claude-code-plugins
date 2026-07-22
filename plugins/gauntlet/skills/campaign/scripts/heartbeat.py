#!/usr/bin/env python3
"""Emit the scheduled heartbeat and session-watchdog wake prompts.

A scheduled wake fires INTO THE SAME LIVE SESSION — it is the SAME owner resuming its own run, one turn
later, with the campaign skill contract already loaded. It is NOT a fresh agent instance. So the wake this
tool prints is a LEAN PROMPT that re-enters the loaded skill's heartbeat entry; it does NOT lead with the
campaign invocation, because on a slash-command host a leading invocation re-injects the entire `SKILL.md`
into the session on every wake, and over a long campaign that walks the driver into context exhaustion
(field feedback from a live 8-PR run). The invocation appears exactly once, at the tail, as the explicit
fallback for a session whose context no longer holds the skill contract.

The wake carries ONLY two flags:

    --run <run-id> --token <agent-token>

and never any other. A resuming owner goes through `lease.py`'s REFRESH path, not `acquire` — per the
single-user policy (`AGENTS.md`), an owner's refresh needs NO heartbeat proof. In particular:

- It NEVER carries `--heartbeat-id`. That is an ACQUIRE-TIME proof — minted when the heartbeat is armed and
  handed to `lease.py acquire` to take the run. A resuming heartbeat refreshes and re-proves nothing, so the
  proof has no place in the wake. (This is the one thing the old `lease.py` NO_HEARTBEAT message got
  wrong, and this tool is the corrected owner of that command.)
- It NEVER carries `--new` or a `#PR` argument. Those are START-TIME args that CREATE or ADOPT a run;
  replaying them on every heartbeat would mint a fresh run each wake. `--run` RESUMES the existing one.
- It hardcodes NO host form. The invocation string (`/gauntlet:campaign` on Claude Code,
  `$gauntlet:campaign` on Codex) is PASSED IN via `--invocation`, so this tool is host-neutral.

The watchdog is a SECOND, session-scoped wake at a longer cadence. It carries the current owner's token and
adds `--watchdog`, which makes the campaign audit its soundness before rescheduling. It is not an orphan
recovery path and never claims to survive a dead session.

This tool does NOT arm either wake. Arming is the host's own scheduler call, which is an agent-only action
that cannot be scripted — `runtime-adapter.md` owns that mechanism. This tool only PRINTS the prompt the
agent then schedules through it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _gauntlet.modules import load_module_from_path

DESCRIPTION = "Print a scheduled heartbeat or session-watchdog wake prompt."

_HERE = Path(__file__).resolve().parent
SIBLING = _HERE / "heartbeat-test.py"


def _wake_prompt(intro: str, invocation: str, flags: str) -> str:
    """One lean same-session wake shape for both wakes: intro, the two-flag identity, then the
    invocation exactly once as the not-in-context fallback — never as the leading token, where a
    slash-command host would expand it into a full skill re-injection every wake."""
    return (f"{intro} — {flags}. Only if the campaign skill contract is no longer in context, "
            f"re-invoke it first: {invocation} {flags}")


def callback_command(invocation: str, run_id: str, token: str) -> str:
    """Assemble the scheduled-heartbeat wake prompt: EXACTLY two flags, `--run` and `--token`.

    A scheduled heartbeat resumes the same owner in the same live session, hitting `lease.py`'s refresh
    path, which needs no proof — so the wake never carries `--heartbeat-id` (acquire-time only) and never
    carries `--new`/`#PR` (start-time args that would mint a fresh run each wake). The host form is
    supplied by the caller as `invocation`, so this stays host-neutral.
    """
    return _wake_prompt(
        "Gauntlet campaign heartbeat: resume the run per the campaign skill already loaded in this "
        "session, at its loop-control heartbeat entry",
        invocation, f"--run {run_id} --token {token}")


def watchdog_command(invocation: str, run_id: str, token: str) -> str:
    """Build a session-scoped soundness-audit wake prompt for the current owner."""
    return _wake_prompt(
        "Gauntlet campaign session-watchdog wake: audit the run per the campaign skill already loaded "
        "in this session (loop-control, Reschedule or exit)",
        invocation, f"--run {run_id} --token {token} --watchdog")


# --- cli ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest="cmd")

    cb = sub.add_parser("callback", help="print the wake prompt to schedule for the next heartbeat")
    cb.add_argument("--run", required=True, help="the run id to RESUME (never mints a new run)")
    cb.add_argument("--token", required=True, help="the agent token the heartbeat carries")
    cb.add_argument("--invocation", required=True,
                    help="the host campaign invocation (Claude Code `/gauntlet:campaign`, "
                         "Codex `$gauntlet:campaign`) — passed in so this tool hardcodes no host form")

    wd = sub.add_parser("watchdog", help="print the session-watchdog soundness-audit wake prompt")
    wd.add_argument("--run", required=True, help="the existing run id the watchdog audits")
    wd.add_argument("--token", required=True, help="the current owner token")
    wd.add_argument("--invocation", required=True,
                    help="the host campaign invocation — passed in so this tool hardcodes no host form")

    sub.add_parser("self-test", help="run every fixture and assert the rules this file enforces still hold")
    return parser


def _reject_unusable(fields: "list[tuple[str, str]]") -> bool:
    """Fail closed on any required value that is empty OR contains ANY whitespace:
    whitespace is the argument-injection seam — a `--run "g1 --new #99"` would smuggle extra
    tokens past the fixed-shape guarantee once the wake's two-flag identity or its embedded fallback
    invocation is re-split into argv. No legitimate value (a `g<date>-<rand>` run-id, a hex token, a
    `/gauntlet:campaign` invocation) carries whitespace. On a bad
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
        if _reject_unusable([("--run", args.run), ("--token", args.token),
                             ("--invocation", args.invocation)]):
            return 1
        print(watchdog_command(args.invocation, args.run, args.token))
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
        print("\n1 check(s) FAILED — the campaign wake-command tool's contract is broken.")
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
        print(f"{failures} check(s) FAILED — the campaign wake-command tool's contract is broken.")
        return 1
    print(f"all {len(cases)} fixtures hold — the campaign wake-command tool's contract is intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
