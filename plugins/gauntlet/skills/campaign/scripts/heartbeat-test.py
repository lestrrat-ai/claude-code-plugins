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


def _assert_refused(argv, what):
    code, out, err = capture_cli(H.main, ["callback", *argv])
    check(code != 0, f"a {what} must fail closed with a non-zero exit, got {code}")
    check(out.strip() == "", f"a refused callback ({what}) must print NOTHING on stdout, got {out.strip()!r}")
    check("REFUSED" in err, f"the refusal ({what}) must say REFUSED on stderr, got {err!r}")
    return out


def t_cli_refuses_whitespace_run():
    # A `--run` value carrying whitespace is the argument-injection seam: `g1 --new #99` would smuggle
    # `--new #99` into the scheduled callback once the printed line is re-split into argv.
    _assert_refused(["--run", "g1 --new #99", "--token", "tok", "--invocation", "/gauntlet:campaign"],
                    "whitespace-containing --run")


def t_cli_refuses_whitespace_token():
    _assert_refused(["--run", "g1", "--token", "aa bb", "--invocation", "/gauntlet:campaign"],
                    "whitespace-containing --token")


def t_cli_refuses_whitespace_invocation():
    _assert_refused(["--run", "g1", "--token", "tok", "--invocation", "/gauntlet:campaign --new"],
                    "whitespace-containing --invocation")


def t_cli_refuses_tab_and_newline():
    # Whitespace is not only the space character: a tab or a newline is the same injection seam and must
    # be refused too. A newline could split the printed line into two scheduled commands.
    _assert_refused(["--run", "g1\ttok", "--token", "tok", "--invocation", "/gauntlet:campaign"],
                    "tab-containing --run")
    _assert_refused(["--run", "g1", "--token", "aa\nbb", "--invocation", "/gauntlet:campaign"],
                    "newline-containing --token")


def t_smuggled_args_cannot_reach_stdout():
    # The whole point: forbidden start-time/acquire-time tokens (`--new`, `#PR`, `--heartbeat-id`) hidden
    # behind whitespace must NEVER survive to stdout, because a scheduler would then re-split them into argv
    # and mint a fresh run (or re-present a stale proof) every heartbeat.
    for smuggle in ("g1 --new #99", "g1 #12", "g1 --heartbeat-id deadbeef"):
        out = _assert_refused(["--run", smuggle, "--token", "tok", "--invocation", "/gauntlet:campaign"],
                              f"smuggled {smuggle!r}")
        for forbidden in ("--new", "#", "--heartbeat-id"):
            if forbidden in smuggle:
                check(forbidden not in out,
                      f"a refused callback must not leak {forbidden!r} to stdout, got {out!r}")


# --- the token-free watchdog poke contract ------------------------------------

def t_watchdog_line_shape():
    got = H.watchdog_command("/gauntlet:campaign", "g260704-0915-a3f29c1b")
    check(got == "/gauntlet:campaign --run g260704-0915-a3f29c1b --watchdog",
          f"the poke must be exactly `<invocation> --run <id> --watchdog`, got {got!r}")


def t_watchdog_carries_run_and_watchdog_and_nothing_forbidden():
    """The poke's WHOLE contract: it carries `--run` and `--watchdog`, and NONE of `--token`, `--new`, `#PR`,
    or `--heartbeat-id`. A token-bearing poke beside a live chain could be adopted as a second driver and
    double-drive the run; the start-time/acquire-time args would mint a fresh run or replay a stale proof."""
    got = H.watchdog_command("/gauntlet:campaign", "g260704-0915-a3f29c1b")
    check("--run" in got, "the poke must carry --run — it names the run to check on")
    check("--watchdog" in got, "the poke must carry --watchdog — the flavor that resolves the lease first")
    for forbidden in ("--token", "--new", "#", "--heartbeat-id"):
        check(forbidden not in got,
              f"the poke must NOT carry {forbidden!r} — a token could double-drive; --new/#PR would mint a "
              f"fresh run; --heartbeat-id is an acquire-time proof a lease-resolving poke never presents")


def t_watchdog_host_neutral():
    got = H.watchdog_command("$gauntlet:campaign", "g1")
    check(got.startswith("$gauntlet:campaign "),
          "the poke must assume NO host form — the Codex `$` invocation must survive verbatim")


def t_cli_watchdog_prints_command():
    r, inv = "g260704-0915-a3f29c1b", "$gauntlet:campaign"
    code, out, err = capture_cli(H.main, ["watchdog", "--run", r, "--invocation", inv])
    check(code == 0, f"a well-formed poke invocation must exit 0, got {code}")
    check(out.strip() == f"{inv} --run {r} --watchdog",
          f"the CLI must print the exact poke command, got {out.strip()!r}")
    check(err == "", f"a successful poke must write nothing to stderr, got {err!r}")


def _assert_watchdog_refused(argv, what):
    code, out, err = capture_cli(H.main, ["watchdog", *argv])
    check(code != 0, f"a {what} must fail closed with a non-zero exit, got {code}")
    check(out.strip() == "", f"a refused poke ({what}) must print NOTHING on stdout, got {out.strip()!r}")
    check("REFUSED" in err, f"the refusal ({what}) must say REFUSED on stderr, got {err!r}")
    return out


def t_cli_watchdog_fails_closed_on_blank_and_whitespace():
    _assert_watchdog_refused(["--run", "", "--invocation", "/x"], "blank --run")
    _assert_watchdog_refused(["--run", "g1", "--invocation", ""], "blank --invocation")
    _assert_watchdog_refused(["--run", "g1 --new #9", "--invocation", "/gauntlet:campaign"],
                             "whitespace-containing --run")
    _assert_watchdog_refused(["--run", "g1\ttok", "--invocation", "/gauntlet:campaign"], "tab --run")
    _assert_watchdog_refused(["--run", "g1", "--invocation", "/gauntlet:campaign --new"],
                             "whitespace-containing --invocation")


def t_watchdog_smuggled_token_cannot_reach_stdout():
    # The point of the token-free design: a `--token`/`--new`/`#PR`/`--heartbeat-id` hidden behind whitespace
    # in --run must NEVER survive to stdout, where a scheduler would re-split it into a double-driving argv.
    for smuggle in ("g1 --token deadbeef", "g1 --new #99", "g1 #12", "g1 --heartbeat-id deadbeef"):
        out = _assert_watchdog_refused(["--run", smuggle, "--invocation", "/gauntlet:campaign"],
                                       f"smuggled {smuggle!r}")
        for forbidden in ("--token", "--new", "#", "--heartbeat-id"):
            if forbidden in smuggle:
                check(forbidden not in out,
                      f"a refused poke must not leak {forbidden!r} to stdout, got {out!r}")


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
    ("refuse-whitespace-run", "a --run carrying whitespace is refused (argument-injection seam)",
     t_cli_refuses_whitespace_run),
    ("refuse-whitespace-token", "a --token carrying whitespace is refused",
     t_cli_refuses_whitespace_token),
    ("refuse-whitespace-invocation", "an --invocation carrying whitespace is refused",
     t_cli_refuses_whitespace_invocation),
    ("refuse-tab-and-newline", "tab and newline count as whitespace and are refused too",
     t_cli_refuses_tab_and_newline),
    ("smuggle-blocked", "--new/#PR/--heartbeat-id hidden behind whitespace never reach stdout",
     t_smuggled_args_cannot_reach_stdout),
    ("watchdog-line-shape", "the poke is exactly `<invocation> --run <id> --watchdog`",
     t_watchdog_line_shape),
    ("watchdog-run-and-watchdog-only", "the poke carries --run/--watchdog and none of --token/--new/#PR/--heartbeat-id",
     t_watchdog_carries_run_and_watchdog_and_nothing_forbidden),
    ("watchdog-host-neutral", "the poke's invocation is passed in — no `/` host form is hardcoded",
     t_watchdog_host_neutral),
    ("watchdog-cli-prints", "the watchdog subcommand prints the exact poke and nothing else",
     t_cli_watchdog_prints_command),
    ("watchdog-fails-closed", "a blank or whitespace value fails closed, prints nothing, says REFUSED",
     t_cli_watchdog_fails_closed_on_blank_and_whitespace),
    ("watchdog-smuggle-blocked", "a smuggled --token/--new/#PR/--heartbeat-id never reaches stdout",
     t_watchdog_smuggled_token_cannot_reach_stdout),
]
