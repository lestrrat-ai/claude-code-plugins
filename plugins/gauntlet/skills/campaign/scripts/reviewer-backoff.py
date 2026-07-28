#!/usr/bin/env python3
# ci: pyright
"""Guess how long an unusable external reviewer needs, then BOUND the retries.

The campaign's external reviewer (`codex exec`, or `claude -p` on the other host) sometimes cannot
run at all — most often the account hit a usage limit. This helper answers three questions and
nothing else:

1. Is the reviewer unusable, and in what WAY? — by MARKER, never by grammar.
2. Roughly how long should the campaign wait? — a rough guess, else the class default.
3. May it try the external route again at all? — a fixed cap, then the native reviewer.

**The guess is deliberately approximate, and the delay text is deliberately OPTIONAL.** Provider
wording is an unbounded natural-language space, so text this helper cannot read is a NORMAL outcome,
not an error: it yields the class default. Nothing a message says can disable the external route,
because there is no such state to set — the only thing a caller keeps is how many external attempts
the current pass has spent. That is also why every delay here is RELATIVE ("wait about N seconds
from now"): a pending wait lives in the calling process and dies with it, so absolute dates, month
names, timezones, and DST are out of scope by construction, not by omission.

**A predecessor of this file parsed provider delay text exactly** — relative delays, absolute
deadlines, month names, zones, DST folds — and made unparseable timer text `permanent`, which
disabled the external reviewer for the whole session. Fail-closed on an unbounded input space is
what made it fail: every unreadable phrasing became a session-wide outage. This file inverts that.
Unreadable is ordinary; the worst outcome any message can produce is one native review pass, which
is a complete, valid pass.

One failure never recovers on its own: a reviewer that REFUSED the task on content or policy
grounds returns `stop-and-ask`. Falling back would swap in the orchestrator's OWN engine, silently
dropping the engine diversity the gate relies on, so the operator decides instead.

This helper reads no ledger, run artifact, history entry, cache, or preference, and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from _gauntlet.modules import load_module_from_path

HERE = Path(__file__).resolve().parent
SIBLING = HERE / "reviewer-backoff-test.py"

# --- classes, actions, and the fixed schedule --------------------------------

REFUSAL = "refusal"
NOT_FOUND = "not-found"
AUTH = "auth"
USAGE_LIMIT = "usage-limit"
TRANSIENT = "transient"
UNKNOWN = "unknown"

WAIT_EXTERNAL = "wait-external"
FALLBACK_NATIVE = "fallback-native"
STOP_AND_ASK = "stop-and-ask"

#: External launches one pass may spend. Matches the attempt budget `runtime-adapter.md`, "Review
#: preparation mapping", already allocates: attempt 1 external, attempt 2 external retry, attempt 3
#: native. There is no attempt 4, so there is no third external try to schedule.
MAX_EXTERNAL_ATTEMPTS = 2
#: Never spin: a sub-second provider hint still costs a real process launch.
MIN_WAIT_SECONDS = 5
#: Past this, waiting is worse than reviewing. A limit that resets in hours means the pass takes the
#: native reviewer NOW rather than stalling the campaign for the reset.
MAX_WAIT_SECONDS = 900
#: What to wait when the message carries no readable delay. These differ per class on purpose: a
#: usage limit is minutes-to-hours, a network blip is seconds. Every value stays under the cap, so a
#: default never itself forces the fallback.
DEFAULT_WAIT_SECONDS = {USAGE_LIMIT: 300, TRANSIENT: 30, UNKNOWN: 60}

# --- markers -----------------------------------------------------------------
#
# Marker vocabulary, not grammar. A class is claimed by a fixed phrase appearing anywhere in the
# captured text; nothing here parses the message's structure.

REFUSAL_MARKERS = (
    "can't help",
    "cannot help",
    "can't assist",
    "cannot assist",
    "won't help",
    # Every refusal marker carries its OWN negation. A positive stem like `able to help` matches both
    # "I am able to help" and "I'm unable to help with that", so it cannot tell a refusal from
    # ordinary prose — and a manufactured refusal stops the campaign for the operator.
    "unable to help",
    "not able to help",
    "unable to assist",
    "not able to assist",
    "unable to provide assistance",
    "not able to provide assistance",
    "cannot provide assistance",
    "can't provide assistance",
    "won't provide assistance",
    "decline",
    "refusal",
    # The agent sense of this stem is anchored by the following infinitive. The bare stem `refused`
    # also matched transport wording — a connection "refused by the upstream", a peer that "refused
    # the connection" — and, because a non-digit marker compiles with no word boundary, it matched
    # as a pure substring inside `ECONNREFUSED`. Each of those manufactured a stop-and-ask and ended
    # autonomous handling of the PR. Transport wording this marker no longer matches lands in
    # `transient` or `unknown` and is retried or falls back.
    # Disclosed residual, deliberately not excluded: browser wording of the form `<host> refused to
    # connect` still classifies refusal. That is a browser page, not external-reviewer process
    # output, and separating it from an agent refusal would need an exclusion list.
    "refused to",
    "content filter",
    "content policy",
    "safety policy",
    "usage policy",
)
#: The tool itself cannot run. A wait cannot fix a missing binary or a mistyped flag.
NOT_FOUND_MARKERS = (
    "command not found",
    "no such file or directory",
    "executable file not found",
    "unknown option",
)
#: Same class, weaker evidence. A help banner is a strong hint the tool could not run, but it can ride
#: along with a real limit or auth message in the same capture, so it is matched only AFTER the classes
#: a wait or the operator can fix.
NOT_FOUND_WEAK_MARKERS = ("usage:",)
#: The account cannot authenticate. A wait cannot fix it either; only the operator can.
AUTH_MARKERS = (
    "authentication failed",
    "unauthorized",
    "forbidden",
    "permission denied",
    "invalid api key",
    "invalid token",
    "not logged in",
    "please log in",
)
#: The account is out of budget for now. This is the case the whole helper exists for.
USAGE_LIMIT_MARKERS = (
    "usage limit",
    "rate limit",
    "quota",
    "too many requests",
    "limit reset",
    "429",
)
#: The transport hiccuped. Usually seconds.
TRANSIENT_MARKERS = (
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "connection closed",
    "disconnected",
    "broken pipe",
    "network is unreachable",
    "temporarily unavailable",
    "temporary failure",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "502",
    "503",
    "504",
)

#: Class order. The first class with a matching marker wins; `unknown` is what is left. Refusal
#: leads because a declined task is not a transport failure and must never be recovered by swapping
#: in a same-engine reviewer. The two "cannot run at all" classes come next so that, say, `codex:
#: command not found` is never read as a retryable blip. The WEAK not-found tier sits after the
#: classes a wait or the operator can fix, because a help banner alongside a real limit message means
#: the limit — but still before `transient`, so a banner naming a `--timeout` flag stays not-found.
CLASS_ORDER = (
    (REFUSAL, REFUSAL_MARKERS),
    (NOT_FOUND, NOT_FOUND_MARKERS),
    (AUTH, AUTH_MARKERS),
    (USAGE_LIMIT, USAGE_LIMIT_MARKERS),
    (NOT_FOUND, NOT_FOUND_WEAK_MARKERS),
    (TRANSIENT, TRANSIENT_MARKERS),
)
#: Classes that end the pass's external route immediately: a wait would change nothing.
CANNOT_RUN = (NOT_FOUND, AUTH)

#: Markers that are a line-leading banner rather than a phrase. `usage:` identifies a CLI help dump
#: only when it OPENS a line; anywhere else it is ordinary telemetry prose (`token usage: 41234`,
#: `memory usage: 82%`).
_LINE_ANCHORED_MARKERS = frozenset({"usage:"})


def _marker_pattern(marker: str) -> "re.Pattern[str]":
    if marker.isdigit():
        return re.compile(rf"(?<![\w.]){re.escape(marker)}(?![\w.])")
    if marker in _LINE_ANCHORED_MARKERS:
        return re.compile(rf"(?m)^[ \t]*{re.escape(marker)}", re.IGNORECASE)
    return re.compile(re.escape(marker), re.IGNORECASE)


_PATTERNS = {
    marker: _marker_pattern(marker)
    for _kind, markers in CLASS_ORDER
    for marker in markers
}

# --- the rough delay guess ---------------------------------------------------
#
# Find a number with a time unit somewhere after a retry-ish word, and convert it. That is the whole
# algorithm. It reads the FIRST such pair and ignores every later one, so `1 minute 30 seconds`
# guesses 60s and `30-60 seconds` guesses 60s. Both are close enough to be useful and neither is
# exact — that is the point. Anything it cannot read yields no guess at all, which is not an error.

_UNIT_MILLIS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}
_UNIT_ALTERNATION = (
    r"milliseconds?|millisecs?|millis|msecs?|ms"
    r"|seconds?|secs?|s"
    r"|minutes?|mins?|m"
    r"|hours?|hrs?|h"
    r"|days?|d"
)
#: `(?<![\d.])` keeps the scan off the tail of a longer number and off a fraction's decimals, so
#: `1.5 seconds` reads as no delay at all (default) rather than as 5 seconds. `\b` after the unit
#: keeps `3pm` and `2026-07-25T13:00:00Z` from yielding one.
DELAY_RE = re.compile(
    rf"(?<![\d.])(?P<value>\d{{1,9}})\s*(?P<unit>{_UNIT_ALTERNATION})\b",
    re.IGNORECASE,
)
#: A number only means a delay when something retry-ish introduces it. Without this, `attempt 2 of
#: 3` and `reviewed 40 files` would both read as timers.
TRIGGER_RE = re.compile(
    r"retr|try\s+again|wait|back\s?off|reset|available|resume|cool\s?down",
    re.IGNORECASE,
)
#: How far after a trigger word a number may sit and still be that trigger's delay. Arbitrary, and
#: deliberately generous enough for `retry in about 60 seconds`.
TRIGGER_WINDOW = 40


def _unit_millis(unit: str) -> int:
    text = unit.lower()
    if text.startswith(("ms", "milli")):
        return _UNIT_MILLIS["ms"]
    return _UNIT_MILLIS[text[0]]


def guess_delay_seconds(message: str) -> int | None:
    """Roughly how long the message asks the campaign to wait, or ``None`` when nothing readable
    sits near a retry-ish word. ``None`` is an ordinary answer, not a failure."""

    ends = sorted(match.end() for match in TRIGGER_RE.finditer(message))
    if not ends:
        return None
    for match in DELAY_RE.finditer(message):
        start = match.start()
        index = bisect_right(ends, start)
        if index and start - ends[index - 1] <= TRIGGER_WINDOW:
            millis = int(match.group("value")) * _unit_millis(match.group("unit"))
            return math.ceil(millis / 1000)
    return None


# --- the two results ---------------------------------------------------------


@dataclass(frozen=True)
class Failure:
    """What one captured external-process failure looks like: a class, the marker that claimed it,
    and a rough delay when one was readable."""

    kind: str
    marker: str | None
    delay_seconds: int | None


@dataclass(frozen=True)
class Decision:
    """What to do next. ``wait_seconds`` is a duration from now, never a deadline."""

    action: str
    kind: str
    marker: str | None
    wait_seconds: int
    attempts_spent: int
    attempts_cap: int
    reason: str


def classify(message: object) -> Failure:
    """Assign one captured failure to a marker class.

    Unrecognized text is `unknown`, never a class that ends the external route for anything beyond
    the current pass. Non-text and empty input is `unknown` too: the caller loses nothing but this
    pass's external attempt, and a native pass is a complete pass.
    """

    if not isinstance(message, str) or not message.strip():
        return Failure(UNKNOWN, None, None)
    for kind, markers in CLASS_ORDER:
        for marker in markers:
            if _PATTERNS[marker].search(message):
                return Failure(kind, marker, guess_delay_seconds(message))
    return Failure(UNKNOWN, None, guess_delay_seconds(message))


def decide(message: object, attempts_spent: object = 0) -> Decision:
    """Classify one failure and return the next action for THIS review pass.

    ``attempts_spent`` is how many external launches the pass has already made. It is the caller's
    whole memory: no deadline, no disable flag, nothing durable. A value that is not a non-negative
    integer is treated as exhausted, so a caller bug costs one native pass instead of an unbounded
    retry loop.
    """

    failure = classify(message)
    spent = (
        attempts_spent
        if type(attempts_spent) is int and attempts_spent >= 0
        else MAX_EXTERNAL_ATTEMPTS
    )

    def result(action: str, wait: int, reason: str) -> Decision:
        return Decision(action, failure.kind, failure.marker, wait, spent,
                        MAX_EXTERNAL_ATTEMPTS, reason)

    if failure.kind == REFUSAL:
        return result(STOP_AND_ASK, 0,
                      f"the reviewer refused the task ({failure.marker}); the operator decides, "
                      f"never a silent same-engine fallback")
    if failure.kind in CANNOT_RUN:
        return result(FALLBACK_NATIVE, 0,
                      f"the external reviewer cannot run at all ({failure.marker}); "
                      f"waiting would not change that")
    if spent >= MAX_EXTERNAL_ATTEMPTS:
        return result(FALLBACK_NATIVE, 0,
                      f"the external route already spent its {MAX_EXTERNAL_ATTEMPTS} attempts")
    read = failure.delay_seconds
    delay = DEFAULT_WAIT_SECONDS[failure.kind] if read is None else read
    if delay > MAX_WAIT_SECONDS:
        return result(FALLBACK_NATIVE, 0,
                      f"the guessed delay of about {delay}s is longer than the "
                      f"{MAX_WAIT_SECONDS}s wait cap")
    source = (f"no delay was readable, so the {failure.kind} default applies"
              if read is None else f"the message asks for roughly {read}s")
    return result(WAIT_EXTERNAL, max(delay, MIN_WAIT_SECONDS),
                  f"{source}; relaunch the external reviewer after the wait")


# --- CLI ---------------------------------------------------------------------


def _message(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    if (args.message is not None) == (args.message_file is not None):
        parser.error("provide exactly one of --message or --message-file")
    if args.message is not None:
        return args.message
    try:
        return args.message_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        parser.error(f"cannot read --message-file: {exc}")


def sibling_cases() -> "list[tuple[str, str, Callable[[], None]]]":
    if not SIBLING.is_file():
        raise RuntimeError(f"the fixture file {SIBLING} is missing")
    module = load_module_from_path("reviewer_backoff_test", SIBLING, register=True)
    if module is None:
        raise RuntimeError(f"the fixture file {SIBLING} cannot be loaded")
    cases = getattr(module, "CASES", None)
    if not cases:
        raise RuntimeError(f"the fixture file {SIBLING} exports no CASES — every rule in this file "
                           f"is unpinned while the suite still exits 0")
    return list(cases)


def self_test() -> int:
    failures = 0
    try:
        cases = sibling_cases()
    except RuntimeError as exc:
        print(f"FAIL     {'sibling-fixtures':32} -> {exc}")
        print("\n1 check(s) FAILED — the reviewer-backoff contract is broken.")
        return 1
    for name, rule, function in cases:
        try:
            function()
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL     {name:32} -> {rule}\n         {type(exc).__name__}: {exc}")
            failures += 1
        else:
            print(f"ok       {name:32} -> {rule}")
    print()
    if failures:
        print(f"{failures} check(s) FAILED — the reviewer-backoff contract is broken.")
        return 1
    print(f"all {len(cases)} fixtures hold — the reviewer-backoff contract is intact.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify an external-reviewer failure and choose "
                                                 "the next action for this review pass.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("classify", "decide"):
        command = sub.add_parser(name)
        command.add_argument("--message")
        command.add_argument("--message-file", type=Path)
        if name == "decide":
            command.add_argument("--attempts-spent", type=int, default=0)
    sub.add_parser("self-test")
    args = parser.parse_args(argv)
    if args.command == "self-test":
        return self_test()
    message = _message(args, parser)
    result = (classify(message) if args.command == "classify"
              else decide(message, args.attempts_spent))
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
