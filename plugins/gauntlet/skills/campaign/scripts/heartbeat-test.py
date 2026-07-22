#!/usr/bin/env python3
"""Fixtures for `heartbeat.py` — the scheduled heartbeat and session-watchdog wake prompts.

They live in a SIBLING file, and `heartbeat.py self-test` FAILS LOUDLY if it cannot load them.

EVERY FIXTURE MUST PIN A RULE with TEETH. The rules worth the most here: the wake is a LEAN
same-session prompt that never LEADS with the campaign invocation (a leading invocation is a skill
re-invocation that re-injects the whole `SKILL.md` into the live session every wake); it carries
exactly `--run` and `--token`, embedding the invocation once as the not-in-context fallback; the
watchdog adds only `--watchdog`; neither carries `--heartbeat-id` (an acquire-time proof) or
`--new`/`#PR` (start-time args that would mint a fresh run every wake).
"""

from __future__ import annotations

from pathlib import Path

from _gauntlet.modules import load_module_from_path
from _gauntlet.testing import capture_cli

OWNER = Path(__file__).resolve().parent / "heartbeat.py"


def _load_owner():
    mod = load_module_from_path("heartbeat_owner", OWNER)
    if mod is None:
        raise RuntimeError(f"cannot load the heartbeat wake-prompt emitter at {OWNER}")
    return mod


H = _load_owner()


def check(cond, msg):
    if not cond:
        raise H.SelfTestFailure(msg)


RUN, TOK = "g260704-0915-a3f29c1b", "aabbccdd"

# Deliberate template copies, machine-checked against the owner by the two template-pin fixtures:
# the owner wins over these — a template edit must land here in the same change.
CALLBACK_EXPECTED = (
    "Gauntlet campaign heartbeat: resume the run per the campaign skill already loaded in this "
    "session, at its loop-control heartbeat entry — "
    f"--run {RUN} --token {TOK}. Only if the campaign skill contract is no longer in context, "
    f"re-invoke it first: /gauntlet:campaign --run {RUN} --token {TOK}"
)
WATCHDOG_EXPECTED = (
    "Gauntlet campaign session-watchdog wake: audit the run per the campaign skill already loaded "
    "in this session (loop-control, Reschedule or exit) — "
    f"--run {RUN} --token {TOK} --watchdog. Only if the campaign skill contract is no longer in "
    f"context, re-invoke it first: /gauntlet:campaign --run {RUN} --token {TOK} --watchdog"
)


# --- the lean two-flag wake contract -------------------------------------------

def t_callback_template_pinned():
    got = H.callback_command("/gauntlet:campaign", RUN, TOK)
    check(got == CALLBACK_EXPECTED,
          f"the wake prompt template drifted from the pinned copy:\n  got      {got!r}\n"
          f"  expected {CALLBACK_EXPECTED!r}")


def t_callback_carries_run_and_token():
    got = H.callback_command("/gauntlet:campaign", RUN, TOK)
    check(f"--run {RUN} --token {TOK}" in got,
          f"the wake must carry exactly `--run <id> --token <tok>`, got {got!r}")


def t_callback_never_leads_with_invocation():
    got = H.callback_command("/gauntlet:campaign", RUN, TOK)
    check(not got.startswith("/gauntlet:campaign"),
          "the wake prompt must NOT lead with the campaign invocation — a leading invocation is a "
          "skill re-invocation that re-injects the entire SKILL.md into the live session every wake")
    check(got.count("/gauntlet:campaign") == 1,
          "the invocation must appear exactly ONCE, as the not-in-context fallback — "
          f"got {got.count('/gauntlet:campaign')} occurrences in {got!r}")


def t_callback_omits_heartbeat_id():
    got = H.callback_command("/gauntlet:campaign", RUN, TOK)
    check("--heartbeat-id" not in got,
          "the wake must NOT carry --heartbeat-id — that is an acquire-time proof, never part of a "
          "resuming heartbeat which only refreshes")


def t_callback_omits_new_and_pr():
    got = H.callback_command("/gauntlet:campaign", RUN, TOK)
    check("--new" not in got and "#" not in got,
          "the wake must carry NO --new and NO #PR — those are start-time args that would mint a fresh "
          "run every heartbeat instead of resuming this one")


def t_cli_callback_prints_prompt():
    inv = "$gauntlet:campaign"
    code, out, err = capture_cli(H.main, ["callback", "--run", RUN, "--token", TOK, "--invocation", inv])
    check(code == 0, f"a well-formed callback invocation must exit 0, got {code}")
    check(out.strip() == H.callback_command(inv, RUN, TOK),
          f"the CLI must print exactly what the emitter builds, got {out.strip()!r}")
    check(err == "", f"a successful callback must write nothing to stderr, got {err!r}")


def t_cli_fails_closed_on_blank():
    code, out, err = capture_cli(H.main, ["callback", "--run", "", "--token", "t", "--invocation", "/x"])
    check(code != 0, "a blank required value must fail closed with a non-zero exit")
    check(out.strip() == "", f"a refused callback must print NOTHING on stdout, got {out.strip()!r}")
    check("REFUSED" in err, f"the refusal must say REFUSED on stderr, got {err!r}")


def t_host_neutral_invocation():
    got = H.callback_command("$gauntlet:campaign", "g1", "t1")
    check("$gauntlet:campaign --run g1 --token t1" in got,
          "the tool must assume NO host form — the Codex `$` invocation must survive verbatim as the "
          "fallback line, proving the `/` Claude Code form is not hardcoded")
    check("/gauntlet:campaign" not in got,
          f"no `/` host form may leak into a Codex wake prompt, got {got!r}")


def _assert_refused(argv, what):
    code, out, err = capture_cli(H.main, ["callback", *argv])
    check(code != 0, f"a {what} must fail closed with a non-zero exit, got {code}")
    check(out.strip() == "", f"a refused callback ({what}) must print NOTHING on stdout, got {out.strip()!r}")
    check("REFUSED" in err, f"the refusal ({what}) must say REFUSED on stderr, got {err!r}")
    return out


def t_cli_refuses_whitespace_run():
    # A `--run` value carrying whitespace is the argument-injection seam: `g1 --new #99` would smuggle
    # `--new #99` into the wake's two-flag identity — and into its embedded fallback invocation, which a
    # host re-splits into argv when the fallback is taken.
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
    # be refused too. A newline could split the printed prompt into two scheduled commands.
    _assert_refused(["--run", "g1\ttok", "--token", "tok", "--invocation", "/gauntlet:campaign"],
                    "tab-containing --run")
    _assert_refused(["--run", "g1", "--token", "aa\nbb", "--invocation", "/gauntlet:campaign"],
                    "newline-containing --token")


def t_smuggled_args_cannot_reach_stdout():
    # The whole point: forbidden start-time/acquire-time tokens (`--new`, `#PR`, `--heartbeat-id`) hidden
    # behind whitespace must NEVER survive to stdout, because the fallback invocation line would then be
    # re-split into argv and mint a fresh run (or re-present a stale proof) every heartbeat.
    for smuggle in ("g1 --new #99", "g1 #12", "g1 --heartbeat-id deadbeef"):
        out = _assert_refused(["--run", smuggle, "--token", "tok", "--invocation", "/gauntlet:campaign"],
                              f"smuggled {smuggle!r}")
        for forbidden in ("--new", "#", "--heartbeat-id"):
            if forbidden in smuggle:
                check(forbidden not in out,
                      f"a refused callback must not leak {forbidden!r} to stdout, got {out!r}")


# --- session watchdog wake -----------------------------------------------------

def t_watchdog_template_pinned():
    got = H.watchdog_command("/gauntlet:campaign", RUN, TOK)
    check(got == WATCHDOG_EXPECTED,
          f"the watchdog wake template drifted from the pinned copy:\n  got      {got!r}\n"
          f"  expected {WATCHDOG_EXPECTED!r}")


def t_watchdog_is_owner_flags_plus_watchdog():
    got = H.watchdog_command("/gauntlet:campaign", RUN, TOK)
    check(f"--run {RUN} --token {TOK} --watchdog" in got,
          f"the watchdog must carry the owner's two flags plus --watchdog, got {got!r}")
    check(not got.startswith("/gauntlet:campaign"),
          "the watchdog wake must NOT lead with the campaign invocation either — same re-injection seam")
    check(f"/gauntlet:campaign --run {RUN} --token {TOK} --watchdog" in got,
          "the watchdog's fallback invocation must carry --watchdog too, or a lost-context session "
          f"would resume as a primary heartbeat instead of an audit, got {got!r}")
    check("--heartbeat-id" not in got and "--new" not in got and "#" not in got,
          "the watchdog must carry no acquire-time proof or start-time args")


def t_watchdog_host_neutral():
    got = H.watchdog_command("$gauntlet:campaign", "g1", "t1")
    check("$gauntlet:campaign --run g1 --token t1 --watchdog" in got,
          "the watchdog must preserve the supplied Codex invocation; no host form is hardcoded")
    check("/gauntlet:campaign" not in got,
          f"no `/` host form may leak into a Codex watchdog wake, got {got!r}")


def t_cli_watchdog_prints_prompt():
    inv = "$gauntlet:campaign"
    code, out, err = capture_cli(H.main, ["watchdog", "--run", RUN, "--token", TOK, "--invocation", inv])
    check(code == 0, f"a well-formed watchdog wake must exit 0, got {code}")
    check(out.strip() == H.watchdog_command(inv, RUN, TOK),
          f"the watchdog CLI must print exactly what the emitter builds, got {out.strip()!r}")
    check(err == "", f"a successful watchdog wake must write nothing to stderr, got {err!r}")


def _assert_watchdog_refused(argv, what):
    code, out, err = capture_cli(H.main, ["watchdog", *argv])
    check(code != 0, f"a {what} must fail closed with a non-zero exit, got {code}")
    check(out.strip() == "", f"a refused watchdog ({what}) must print NOTHING on stdout, got {out.strip()!r}")
    check("REFUSED" in err, f"the watchdog refusal ({what}) must say REFUSED on stderr, got {err!r}")
    return out


def t_watchdog_refuses_unusable_values():
    _assert_watchdog_refused(["--run", "", "--token", "tok", "--invocation", "/gauntlet:campaign"],
                             "blank watchdog run")
    _assert_watchdog_refused(["--run", "g1", "--token", "aa bb", "--invocation", "/gauntlet:campaign"],
                             "whitespace-containing watchdog token")
    out = _assert_watchdog_refused(
        ["--run", "g1 --new #9", "--token", "tok", "--invocation", "/gauntlet:campaign"],
        "watchdog run containing start-time args")
    check("--new" not in out and "#" not in out,
          f"a refused watchdog must not leak smuggled args to stdout, got {out!r}")


CASES = [
    ("callback-template-pinned", "the wake prompt matches the pinned template exactly",
     t_callback_template_pinned),
    ("callback-carries-flags", "the wake carries exactly `--run <id> --token <tok>`",
     t_callback_carries_run_and_token),
    ("callback-lean", "the wake never LEADS with the invocation; it appears once, as the fallback",
     t_callback_never_leads_with_invocation),
    ("omits-heartbeat-id", "the wake never carries --heartbeat-id (acquire-time proof)",
     t_callback_omits_heartbeat_id),
    ("omits-new-and-pr", "the wake never carries --new or #PR (start-time args)",
     t_callback_omits_new_and_pr),
    ("cli-prints-prompt", "the callback subcommand prints the exact wake prompt and nothing else",
     t_cli_callback_prints_prompt),
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
    ("watchdog-template-pinned", "the watchdog wake matches the pinned template exactly",
     t_watchdog_template_pinned),
    ("watchdog-flags", "the watchdog is the owner's two flags plus --watchdog only, lean like the heartbeat",
     t_watchdog_is_owner_flags_plus_watchdog),
    ("watchdog-host-neutral", "the watchdog preserves the supplied host invocation",
     t_watchdog_host_neutral),
    ("watchdog-cli-prints", "the watchdog subcommand prints the exact wake prompt and nothing else",
     t_cli_watchdog_prints_prompt),
    ("watchdog-refuses-unusable", "a malformed watchdog wake prints nothing and refuses",
     t_watchdog_refuses_unusable_values),
]
