#!/usr/bin/env python3
"""Fixtures for `heartbeat.py` — the scheduled-heartbeat callback command.

They live in a SIBLING file, and `heartbeat.py self-test` FAILS LOUDLY if it cannot load them.

EVERY FIXTURE MUST PIN A RULE with TEETH. The rules worth the most here are the ones that keep the callback
a RESUME and nothing else: it carries exactly `--run` and `--token`, it never carries `--heartbeat-id`
(an acquire-time proof), and it never carries `--new`/`#PR` (start-time args that would mint a fresh run
every heartbeat). Each of those, if it regressed, would silently break the resume contract.
"""

from __future__ import annotations

from pathlib import Path

from _gauntlet.modules import load_module_from_path
from _gauntlet.testing import capture_cli

OWNER = Path(__file__).resolve().parent / "heartbeat.py"


def _load_owner():
    mod = load_module_from_path("heartbeat_owner", OWNER)
    if mod is None:
        raise RuntimeError(f"cannot load the heartbeat callback emitter at {OWNER}")
    return mod


H = _load_owner()


def check(cond, msg):
    if not cond:
        raise H.SelfTestFailure(msg)


# --- the two-flag callback contract -------------------------------------------

def t_callback_is_two_flags():
    got = H.callback_command("/gauntlet:campaign", "g260704-0915-a3f29c1b", "aabbccdd")
    check(got == "/gauntlet:campaign --run g260704-0915-a3f29c1b --token aabbccdd",
          f"the callback must be exactly `<invocation> --run <id> --token <tok>`, got {got!r}")


def t_callback_omits_heartbeat_id():
    got = H.callback_command("/gauntlet:campaign", "g260704-0915-a3f29c1b", "aabbccdd")
    check("--heartbeat-id" not in got,
          "the callback must NOT carry --heartbeat-id — that is an acquire-time proof, never part of a "
          "resuming heartbeat which only refreshes")


def t_callback_omits_new_and_pr():
    got = H.callback_command("/gauntlet:campaign", "g260704-0915-a3f29c1b", "aabbccdd")
    check("--new" not in got and "#" not in got,
          "the callback must carry NO --new and NO #PR — those are start-time args that would mint a fresh "
          "run every heartbeat instead of resuming this one")


def t_cli_callback_prints_command():
    r, t, inv = "g260704-0915-a3f29c1b", "aabbccdd", "$gauntlet:campaign"
    code, out, err = capture_cli(H.main, ["callback", "--run", r, "--token", t, "--invocation", inv])
    check(code == 0, f"a well-formed callback invocation must exit 0, got {code}")
    check(out.strip() == f"{inv} --run {r} --token {t}",
          f"the CLI must print the exact callback command, got {out.strip()!r}")
    check(err == "", f"a successful callback must write nothing to stderr, got {err!r}")


def t_cli_fails_closed_on_blank():
    code, out, err = capture_cli(H.main, ["callback", "--run", "", "--token", "t", "--invocation", "/x"])
    check(code != 0, "a blank required value must fail closed with a non-zero exit")
    check(out.strip() == "", f"a refused callback must print NOTHING on stdout, got {out.strip()!r}")
    check("REFUSED" in err, f"the refusal must say REFUSED on stderr, got {err!r}")


def t_host_neutral_invocation():
    got = H.callback_command("$gauntlet:campaign", "g1", "t1")
    check(got.startswith("$gauntlet:campaign "),
          "the tool must assume NO host form — the Codex `$` invocation must survive verbatim, proving the "
          "`/` Claude Code form is not hardcoded")


CASES = [
    ("callback-two-flags", "the callback is exactly `<invocation> --run <id> --token <tok>`",
     t_callback_is_two_flags),
    ("omits-heartbeat-id", "the callback never carries --heartbeat-id (acquire-time proof)",
     t_callback_omits_heartbeat_id),
    ("omits-new-and-pr", "the callback never carries --new or #PR (start-time args)",
     t_callback_omits_new_and_pr),
    ("cli-prints-command", "the callback subcommand prints the exact command and nothing else",
     t_cli_callback_prints_command),
    ("cli-fails-closed", "a blank required value fails closed, prints nothing, says REFUSED",
     t_cli_fails_closed_on_blank),
    ("host-neutral", "the invocation is passed in — no `/` host form is hardcoded",
     t_host_neutral_invocation),
]
