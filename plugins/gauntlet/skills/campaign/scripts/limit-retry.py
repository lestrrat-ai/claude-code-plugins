#!/usr/bin/env python3
# ci: pyright
"""Wait before the ONE external-reviewer retry, when the failure was a provider limit.

`reviewer.md` already bounds this failure: retry a capable external process once, then take the fresh
native-worker fallback. WITHOUT this file that retry fires IMMEDIATELY, and an immediate retry against
a usage limit fails exactly as the first launch did — so the pass burns both external launches and
reaches the native fallback having learned nothing. **Inserting a wait before that one retry is this
file's entire job.** It adds no attempt, changes no review rule, and selects no route.

Two questions, nothing else: is this failure text a provider LIMIT (by fixed marker phrase, never by
grammar), and wait how long (the `Retry-After` header, else a fixed default, clamped to a floor and a
cap).

**Two predecessors died of the same two things: matching more phrases, and reading a delay out of
English prose.** Provider wording is an unbounded natural-language space, so a grammar over it
mis-reads real messages — waiting on a delay nobody stated, or relaunching seconds into a limit hours
long. Hence exactly ONE failure class, and exactly ONE delay spelling: RFC 9110's unitless
`Retry-After: <delta-seconds>`, a machine-readable header, not prose. Text this file cannot read is a
NORMAL outcome, not an error — it takes the default wait, then the bounded fallback that already
existed. NEVER add a second failure class (which also needs an ordering rule between classes, and
that is grammar again), a unit alternation, a retry-word scan, or a proximity window.

Nothing here is durable: the caller keeps only how many external launches this pass has spent, and
this file reads no ledger, artifact or cache and writes nothing. Every wait is RELATIVE, so absolute
dates, timezones and DST are out of scope by construction, not by omission.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from _gauntlet.modules import load_module_from_path

HERE = Path(__file__).resolve().parent
SIBLING = HERE / "limit-retry-test.py"     # the fixture suite — this tool's executable contract

WAIT_EXTERNAL = "wait-external"
FALLBACK_NATIVE = "fallback-native"

#: External launches one pass may spend. Matches the attempt budget `runtime-adapter.md`, "Review
#: preparation mapping", already allocates: attempt 1 external, attempt 2 external retry, attempt 3
#: native. There is no attempt 4, so there is no third external try to schedule.
MAX_EXTERNAL_ATTEMPTS = 2
#: Never spin: a sub-second provider hint still costs a real process launch.
MIN_WAIT_SECONDS = 5
#: Past this, waiting is worse than reviewing. A limit that resets in hours means the pass takes the
#: native reviewer NOW rather than stalling the campaign for the reset.
MAX_WAIT_SECONDS = 900
#: What to wait when no `Retry-After` header was readable — the ordinary case. It stays under the cap,
#: so the default never itself forces the fallback.
DEFAULT_WAIT_SECONDS = 300
#: Widest run of SIGNIFICANT digits converted. The shortcut in `retry_after_seconds` reads WIDTH as a
#: proxy for magnitude, and that proxy is only sound once leading zeroes are gone: `0000000000` is ten
#: digits of the value zero, and answering it over-cap discarded a value the header plainly stated. A
#: wider significant run is not refused and not re-read as unreadable: it answers just past
#: `MAX_WAIT_SECONDS`, so `decide`'s existing cap check falls back natively and `int()` never sees an
#: unbounded run (past ~4300 digits CPython raises on the conversion itself).
MAX_READABLE_DIGITS = 9

#: The account is out of budget for now. Marker vocabulary, not grammar: the class is claimed by a
#: fixed phrase at ANY POSITION in the captured text — position-independent, though never inside a
#: longer word (`_marker_pattern`) — and nothing here parses the message's structure. This is the ONLY
#: failure class this file has; see the module docstring for why it must stay the only one.
LIMIT_MARKERS = ("usage limit", "rate limit", "quota", "too many requests", "limit reset", "429")

#: What a Unicode COMBINING MARK (general category `M*`) folds to before any marker is matched. A mark
#: continues the word its base letter starts, but Python's `\w` is `str.isalnum()`-based and a mark is
#: not alphanumeric, so an unfolded mark SATISFIES a word anchor instead of closing it: `quota` +
#: U+0301 + `tion` claimed a usage limit and waited five minutes for a limit nobody hit, on text that
#: is one ordinary word. `_` is the stand-in because it is `\w`-true and exactly one character wide, so
#: the anchors see the word continuation that is really there. It is never shown to anyone: `decide()`
#: reports no slice of the folded text.
_MARK_STAND_IN = "_"


def _phrase(marker: str) -> str:
    """One marker as a pattern whose spaces match ANY run of whitespace. Captured stderr is wrapped
    at the terminal width as a matter of course, so a two-word marker arrives split across a newline
    routinely; a hard-coded single space loses it."""

    return r"\s+".join(re.escape(word) for word in marker.split(" "))


def _marker_pattern(marker: str) -> "re.Pattern[str]":
    """Compile one marker under a single rule: a WHOLE-WORD match, any whitespace run for a space, an
    optional trailing `s` for the plural. The digit shape below REFINES that rule for one kind of
    marker — a narrower anchor for the same purpose — and never leaves a marker unanchored.

    The word anchor is what stops `quota` from claiming a usage limit inside `unquotable` or
    `quotation`, while `monthly quotas exceeded` still matches through the optional plural.

    `\\w` alone does NOT decide what continues a word here, and reading this pattern as if it did is
    how the anchor was believed to hold when it did not. `_fold_marks` has already folded every
    combining mark into a `\\w` character by the time this runs — see `_MARK_STAND_IN` — and that fold
    is half of the guarantee this docstring states."""

    if marker.isdigit():
        # A status code takes the word anchor plus `.`, so `429` is not read out of `1.429` or a
        # version string — and no plural, because `429s` names no other status code.
        return re.compile(rf"(?<![\w.]){_phrase(marker)}(?![\w.])")
    return re.compile(rf"(?<!\w){_phrase(marker)}s?(?!\w)", re.IGNORECASE)


def _fold_marks(message: str) -> str:
    """Fold every Unicode combining mark to `_MARK_STAND_IN`, so the word anchors read a mark as the
    word continuation it is. See `_MARK_STAND_IN` for why this is not a character class inside each
    pattern.

    The category test runs over the message's DISTINCT characters, so the cost is one pass to collect
    them and one `str.translate`, both linear in the message and neither keeping per-match state. An
    all-ASCII message, the ordinary one, returns unchanged without a single category lookup."""

    if message.isascii():
        return message
    table = {ord(character): _MARK_STAND_IN
             for character in set(message)
             if unicodedata.category(character).startswith("M")}
    return message.translate(table) if table else message


_PATTERNS = tuple(_marker_pattern(marker) for marker in LIMIT_MARKERS)

#: RFC 9110's `Retry-After` header in its UNITLESS delta-seconds spelling, and NOTHING else: a header
#: name at the head of a line, the colon IMMEDIATELY after that name, an ASCII integer count of
#: seconds, end of line. Both anchors are what keep this a header read rather than a delay grammar —
#: `Retry-After: 7200 ms` and `Retry-After: 72.5` match nothing at all rather than reading a PARTIAL
#: number, and the HTTP-date spelling is simply not read. Each of those lands on
#: `DEFAULT_WAIT_SECONDS`, the ordinary outcome.
#:
#: The digit class is `[0-9]`, NEVER `\d`. RFC 9110 delta-seconds is `1*DIGIT` — ASCII only — while
#: Python's `\d` matches every Unicode decimal digit and `int()` converts them, so `\d` read an
#: Arabic-Indic, fullwidth or mixed-script spelling as this header and waited on a value the header
#: never stated in the one spelling this file reads. RFC 9110 section 5.1 likewise forbids whitespace
#: between a field name and its colon, so there is no `[ \t]*` in front of the colon; the documented
#: leading indentation and the optional whitespace AFTER the colon both stay.
#:
#: Deliberately UNBOUNDED digits: `MAX_READABLE_DIGITS` bounds the conversion instead, because a width
#: limit in the pattern makes an over-width run match NOTHING, which reads a stated over-cap delay as
#: unreadable rather than as over-cap.
RETRY_AFTER_RE = re.compile(r"(?mi)^[ \t]*retry-after:[ \t]*([0-9]+)[ \t]*\r?$")


def is_limit(message: object) -> bool:
    """Does this captured failure text carry a provider-limit marker?

    Non-text and empty input is not a limit, so the caller takes the native fallback — a complete,
    valid review pass. `_fold_marks` runs ONCE over the whole message before any marker is matched, so
    a marker added to `LIMIT_MARKERS` inherits it. Deliberately not length-bounded: `re.search` keeps
    no per-match state, so a huge capture costs no more per marker.
    """

    if not isinstance(message, str) or not message.strip():
        return False
    text = _fold_marks(message)
    return any(pattern.search(text) for pattern in _PATTERNS)


def retry_after_seconds(message: object) -> int | None:
    """The delta-seconds a `Retry-After` header states, or ``None`` when the text carries none in that
    one spelling — an ordinary answer, not a failure (see ``RETRY_AFTER_RE``). Leading zeroes are
    stripped before anything else, so a zero-padded value is read as the value it states rather than
    as its width. A run of SIGNIFICANT digits wider than ``MAX_READABLE_DIGITS`` answers just past
    ``MAX_WAIT_SECONDS`` rather than a converted value: over-cap is all ``decide`` does with such a
    number anyway."""

    if not isinstance(message, str):
        return None
    match = RETRY_AFTER_RE.search(message)
    if match is None:
        return None
    digits = match.group(1).lstrip("0") or "0"
    if len(digits) > MAX_READABLE_DIGITS:
        return MAX_WAIT_SECONDS + 1
    return int(digits)


@dataclass(frozen=True)
class Decision:
    """What to do next. ``wait_seconds`` is a duration from now, never a deadline."""

    action: str
    limit: bool
    wait_seconds: int
    attempts_spent: int
    attempts_cap: int
    reason: str


def decide(message: object, attempts_spent: object) -> Decision:
    """Decide whether THIS review pass waits and relaunches the external reviewer, or falls back.

    ``attempts_spent`` is how many external launches the pass has already made — the caller's whole
    memory. A value that is not a non-negative integer is treated as exhausted, so a caller bug costs
    one native pass instead of an unbounded retry loop.

    It has NO default, here or at the CLI, and must not be given one. A default of 0 reads every
    failure as the pass's first, so a caller that omits it gets `wait-external` forever and never
    reaches ``MAX_EXTERNAL_ATTEMPTS`` — defeating the whole point of bounding the retries. Omitting it
    must fail loudly instead.
    """

    limit = is_limit(message)
    spent = (attempts_spent if type(attempts_spent) is int and attempts_spent >= 0
             else MAX_EXTERNAL_ATTEMPTS)

    def result(action: str, wait: int, reason: str) -> Decision:
        return Decision(action, limit, wait, spent, MAX_EXTERNAL_ATTEMPTS, reason)

    if not limit:
        return result(FALLBACK_NATIVE, 0,
                      "no provider-limit marker; a wait would not change this failure")
    if spent >= MAX_EXTERNAL_ATTEMPTS:
        return result(FALLBACK_NATIVE, 0,
                      f"the external route already spent its {MAX_EXTERNAL_ATTEMPTS} attempts")
    stated = retry_after_seconds(message)
    delay = DEFAULT_WAIT_SECONDS if stated is None else stated
    if delay > MAX_WAIT_SECONDS:
        return result(FALLBACK_NATIVE, 0,
                      f"the stated wait of {delay}s is longer than the {MAX_WAIT_SECONDS}s wait cap")
    source = ("no Retry-After header was readable, so the default applies" if stated is None
              else f"Retry-After asks for {stated}s")
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
    module = load_module_from_path("limit_retry_test", SIBLING, register=True)
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
        print(f"FAIL     {'sibling-fixtures':28} -> {exc}")
        cases = []
        failures = 1
    for name, rule, function in cases:
        try:
            function()
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL     {name:28} -> {rule}\n         {type(exc).__name__}: {exc}")
            failures += 1
        else:
            print(f"ok       {name:28} -> {rule}")
    if failures:
        print(f"\n{failures} check(s) FAILED — the limit-retry contract is broken.")
        return 1
    print(f"\nall {len(cases)} fixtures hold — the limit-retry contract is intact.")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="Decide whether a failed external reviewer waits and "
                                                 "retries, or falls back to the native worker.")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("decide")
    command.add_argument("--message")
    command.add_argument("--message-file", type=Path)
    # Required, never defaulted — see `decide`'s docstring: a default silently restarts the retry
    # count at every failure and the cap is then never reached.
    command.add_argument("--attempts-spent", type=int, required=True)
    sub.add_parser("self-test")
    args = parser.parse_args(argv)
    if args.command == "self-test":
        return self_test()
    print(json.dumps(asdict(decide(_message(args, parser), args.attempts_spent)), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
