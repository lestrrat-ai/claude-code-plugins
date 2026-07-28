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
import unicodedata
from bisect import bisect_left, bisect_right
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
# Marker vocabulary, not grammar. A class is claimed by a fixed phrase at ANY POSITION in the
# captured text — position-independent, though never inside a longer word (the rule below) — and
# nothing here parses the message's structure.
#
# ONE matching rule covers EVERY marker, and `_marker_pattern` is its only implementation. A marker
# matches when it stands as a WHOLE WORD — never as a substring inside a longer word — with any run
# of whitespace standing in for each of its spaces and an optional trailing `s` for the plural. The
# digit and line-leading shapes below are REFINEMENTS of that rule, tightening it for one kind of
# marker; they are NOT the only markers that carry an anchor. `classify()` runs `_normalize` once
# before any of this, so every marker also inherits the apostrophe and combining-mark folds that
# make the word anchor mean what it says. Add a marker and it inherits all of it.

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
    # EVERY agent verb below is anchored by the FOLLOWING INFINITIVE — `refuse` in both its verb and
    # its noun spelling, and `decline` in all four of its — for one reason: a refusal marker must
    # carry its own refusal sense, and none of these bare stems does. `refused`/`refusal` also
    # matched transport wording — a connection "refused by the upstream", a "connection refusal by
    # the upstream", a peer that "refused the connection". Every one of those is the bare stem as a
    # WHOLE WORD, so the uniform anchor `_marker_pattern` now applies does not separate them; only
    # the following infinitive does. (The anchor did retire one of this vocabulary's original
    # reasons: a bare stem can no longer match inside a longer word such as `ECONNREFUSED`.) The
    # bare stem `decline` matched the same way in billing, auth, and gateway wording, where it is a
    # REQUEST that was declined — a credential or payment outcome, not an agent refusing a task.
    # Each of those manufactured a stop-and-ask and ended autonomous handling of the PR. Wording
    # these markers no longer match lands in `auth`, `transient` or `unknown`, and falls back or is
    # retried.
    # Disclosed residual, deliberately not excluded: browser wording of the form `<host> refused to
    # connect` — and equally `refusal to connect` — still classifies refusal. That is a browser
    # page, not external-reviewer process output, and separating it from an agent refusal would
    # need an exclusion list.
    "refusal to",
    "refused to",
    "decline to",
    "declines to",
    "declined to",
    "declining to",
    # The policy spellings below are the only markers here with a plural at all, and their `-y` ->
    # `-ies` plural is NOT the singular plus `s`, so both forms are spelled out. A plural that
    # merely appends `s` needs no second entry, because `_marker_pattern` admits one trailing `s`
    # inside the word anchor; the `-ies` shape is exactly the case that rule does not cover. That is
    # the ONLY reason a second entry appears here — do not add one for an `-s` plural. Spell the
    # plural in FULL rather than truncating to a stem: `decide()` interpolates the matched marker
    # into the operator-facing reason, so a stem would surface there.
    #
    # Disclosed residual, deliberately NOT narrowed: these six are BARE NOUN PHRASES, so they also
    # match infrastructure prose that merely NAMES one of those systems instead of reporting a
    # refusal by it. INVENTED illustration, live nowhere else in this repository: `the content
    # policy service is down for maintenance` classifies `refusal` and stops the PR for the
    # operator. It stays that way on purpose: every mechanical narrowing available loses genuine
    # refusals the `refusal-stops` fixture pins. An infinitive anchor like the verbs above cannot
    # help, because that fixture's own `This request violates the content policy.` carries no
    # infinitive and no second marker; an exclusion list or a weaker tier ordered after `transient`
    # fails it the same way.
    # Separating the two senses needs the surrounding context, which is grammar, not a marker.
    #
    # A component NOUN was worse still, and `content filter` is therefore gone from this tuple: it
    # named infrastructure that can itself be up or down, so ordinary status prose about that
    # component parked the PR. Do not re-add it. The trade is that a refusal phrased ONLY as that
    # component's name now classifies `unknown` — a wait, then the bounded native fallback, which is
    # the safe landing this file's Purpose already promises, and strictly better than parking a PR
    # on an infrastructure-status message. The refusal sense is still carried by the policy pairs.
    "content policy",
    "content policies",
    "safety policy",
    "safety policies",
    "usage policy",
    "usage policies",
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

#: Every character routinely typed or rendered where an ASCII apostrophe belongs, folded to `'`
#: before any marker is matched. Five refusal markers spell an ASCII apostrophe, and the model
#: families this campaign reviews with emit the typographic form routinely; unfolded, a genuine
#: refusal reaches `unknown`, waits, and is then answered by the orchestrator's OWN engine — the one
#: outcome this file exists to prevent. NFKC is NOT the tool for it: NFKC leaves U+2019 and U+2018
#: exactly as they are, so the set is explicit. Every entry is one character wide, so folding shifts
#: no offset the delay guess later measures.
#:
#: The MODIFIER LETTER range U+02B9-U+02BF is folded WHOLE rather than member by member, and it is
#: spelled as a range so that adding a member is not a decision anyone has to remember to make.
#: Hand-picking members out of it is exactly what left `ʻ` (U+02BB) unfolded while its immediate
#: neighbours `ʹ` (U+02B9) and `ʼ` (U+02BC) were folded — so a genuine `I canʻt help with this
#: review` classified `unknown` and was answered by this campaign's own engine, the worst outcome
#: this file has. Every member of that range is a LETTER to `\w`, so an unfolded one glues a
#: marker's two halves into one word that no marker can ever match; there is no anchor rule that
#: could have caught it, only this fold.
_APOSTROPHE_FOLD = {
    ord(character): "'"
    for character in "‘’‛′´＇`" + "".join(chr(code) for code in range(0x02B9, 0x02C0))
}

#: What a Unicode COMBINING MARK (general category `M*`) folds to before any marker is matched. A
#: mark continues the word its base letter starts, but Python's `\w` is `str.isalnum()`-based and a
#: mark is not alphanumeric, so an unfolded mark SATISFIES a word anchor instead of closing it:
#: `quota` + U+0301 + `tion` claimed a usage limit and waited five minutes for a limit nobody hit,
#: on text that is one ordinary word. `_` is the stand-in because it is `\w`-true and exactly one
#: character wide, so the anchors see the word continuation that is really there and no offset the
#: delay guess later measures moves. It is never shown to anyone: `decide()` reports the marker from
#: the vocabulary above, never a slice of the folded text.
#:
#: Folding here rather than in each pattern is deliberate and is the whole point. The anchoring rule
#: has TWO scans — the markers and `TRIGGER_RE` — and threading a second word-character class
#: through both is how one of them ends up a version behind the other. One fold ahead of both cannot
#: drift, and `DELAY_RE` inherits it for free.
_MARK_STAND_IN = "_"


def _phrase(marker: str) -> str:
    """One marker as a pattern whose spaces match ANY run of whitespace. Captured stderr is wrapped
    at the terminal width as a matter of course, so a two-word marker arrives split across a newline
    routinely; a hard-coded single space loses it."""

    return r"\s+".join(re.escape(word) for word in marker.split(" "))


def _marker_pattern(marker: str) -> "re.Pattern[str]":
    """Compile one marker under the single rule the section header states: a WHOLE-WORD match, any
    whitespace run for a space, an optional trailing `s`. The two shapes below REFINE that rule for
    one kind of marker each — a narrower anchor for the same purpose — and never leave a marker
    unanchored.

    Disclosed consequence of the word anchor, and the reason it is worth it anyway: a marker glued
    to more letters no longer matches, so a runtime's `TimeoutError` or `ConnectTimeout` classifies
    `unknown` rather than `transient`. Both are retryable, so that costs the unknown default wait
    instead of the transient one — while the anchor is what stops `quota` from claiming a usage
    limit inside `unquotable` or `quotation`.

    `\\w` alone does NOT decide what continues a word here, and reading these patterns as if it did
    is how the anchor was believed to hold when it did not. `_normalize` has already folded every
    combining mark into a `\\w` character by the time any of this runs — see `_MARK_STAND_IN` — and
    that fold is half of the guarantee this docstring states."""

    if marker in _LINE_ANCHORED_MARKERS:
        # A banner marker must OPEN a line. That is stricter than the word anchor, so it replaces it.
        return re.compile(rf"(?m)^[ \t]*{_phrase(marker)}", re.IGNORECASE)
    if marker.isdigit():
        # A status code takes the word anchor plus `.`, so `429` is not read out of `1.429` or a
        # version string — and no plural, because `429s` names no other status code.
        return re.compile(rf"(?<![\w.]){_phrase(marker)}(?![\w.])")
    return re.compile(rf"(?<!\w){_phrase(marker)}s?(?!\w)", re.IGNORECASE)


def _fold_marks(message: str) -> str:
    """Fold every Unicode combining mark to `_MARK_STAND_IN`, so the word anchors read a mark as the
    word continuation it is. See `_MARK_STAND_IN` for why this is not a character class inside each
    pattern.

    The category test runs over the message's DISTINCT characters, so the cost is one pass to
    collect them and one `str.translate`, both linear in the message and neither keeping per-match
    state — which is what `MAX_SCANNED_CHARS` relies on when it leaves the marker scan unbounded. An
    all-ASCII message, the ordinary one, returns unchanged without a single category lookup."""

    if message.isascii():
        return message
    table = {ord(character): _MARK_STAND_IN
             for character in set(message)
             if unicodedata.category(character).startswith("M")}
    return message.translate(table) if table else message


def _normalize(message: str) -> str:
    """Fold the apostrophe variants and the combining marks so one spelling of each marker suffices.

    Applied ONCE to the whole message before any marker is matched — not per marker, not per class —
    so a marker added later inherits it. Whitespace is deliberately NOT rewritten here: collapsing
    newlines would destroy the line structure the line-leading `usage:` banner depends on, and
    `_marker_pattern` already absorbs a wrapped line inside each marker instead."""

    return _fold_marks(message.translate(_APOSTROPHE_FOLD))


_PATTERNS = {
    marker: _marker_pattern(marker)
    for _kind, markers in CLASS_ORDER
    for marker in markers
}

# --- the rough delay guess ---------------------------------------------------
#
# Find a number with a time unit NEAR a retry-ish word, and convert it. That is the whole algorithm.
# It reads the FIRST such pair and ignores every later one, so `1 minute 30 seconds` guesses 60s and
# `30-60 seconds` guesses 60s. Both are close enough to be useful and neither is exact — that is the
# point. Anything it cannot read yields no guess at all, which is not an error.
#
# "Near" is TWO ORDERED PASSES, never one both-sided scan. Pass 1 reads the first pair that FOLLOWS a
# trigger; only when pass 1 finds nothing does pass 2 read the first pair that PRECEDES one. Both use
# the same `TRIGGER_WINDOW`. Ordering — not window width — is what keeps telemetry sitting in front of
# a real trigger from hijacking the guess; see the `TRIGGER_WINDOW` comment for the hazard.

_UNIT_MILLIS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}
_UNIT_ALTERNATION = (
    r"milliseconds?|millisecs?|millis|msecs?|ms"
    r"|seconds?|secs?|s"
    r"|minutes?|mins?|m"
    r"|hours?|hrs?|h"
    r"|days?|d"
)
#: THE LEAD-IN HAS ONE JOB: never start reading in the MIDDLE of a number a provider wrote. It is a
#: WHITELIST, and that direction is the whole fix. The rule used to LIST the characters that may not
#: precede the digits — a digit, a decimal point, a thousands separator, an ASCII sign — and a list
#: of what is forbidden leaves every character NOT on it a way in. It let `1,800 seconds` read as
#: 800 and `10,000 seconds` as 0, which relaunched the external reviewer five seconds into a limit
#: hours long and burned the pass's attempt; once those ASCII spellings were listed, `retry after
#: −2 seconds` (U+2212 MINUS SIGN) read as 2 and `retry after 1٫5 seconds` (U+066B ARABIC DECIMAL
#: SEPARATOR) read as 5 the same way. Every one of those takes a magnitude and drops the sign or the
#: integer part beside it: a PARTIALLY read number, which the caller then acts on — not the ordinary
#: `unreadable` landing this file is built around.
#: So the scan now begins ONLY where the text positively says a number begins: at the start of the
#: message, after whitespace, after the approximation marker `~`, or after an opening bracket.
#: Anything else — a digit, any separator, any sign, in any script, spelled any way — leaves the
#: number UNREAD and the failure takes its class default. Adding a lead-in is how a new shape becomes
#: readable; nothing has to be enumerated to keep an unfamiliar one safe.
#: ONE EXCEPTION keeps a RANGE readable: a `-` that ITSELF follows a digit separates two numbers
#: rather than signing one, so `30-60 seconds` still guesses 60 exactly as the section header above
#: says it does. Admitting a bare `-` would take `retry after -2 hours` back to 7200.
#: `\b` after the unit keeps `3pm` and `2026-07-25T13:00:00Z` from yielding a delay.
#: The value group is deliberately UNBOUNDED: `MAX_READABLE_DIGITS` bounds the conversion instead,
#: because a width limit in the pattern makes an over-width run match NOTHING (the lead-in admits no
#: start inside the digits), which reads a stated over-cap delay as `unreadable` rather than as
#: over-cap.
DELAY_RE = re.compile(
    rf"(?:\A|(?<=\s)|(?<=[~(\[])|(?<=(?<=\d)-))"
    rf"(?P<value>\d+)\s*(?P<unit>{_UNIT_ALTERNATION})\b",
    re.IGNORECASE,
)
#: A number only means a delay when something retry-ish introduces it. Without this, `attempt 2 of
#: 3` and `reviewed 40 files` would both read as timers.
#: The alternation is anchored at BOTH ENDS, under the same rule `_marker_pattern` applies to every
#: marker: a trigger counts only as a WHOLE WORD. The leading `\b` alone stopped telemetry from
#: posing as a trigger — `retrieved 40 files in 3 seconds` no longer supplies one, and `service
#: unavailable` no longer poses as `available` — but an open tail let a trigger match inside a
#: longer word, so ordinary prose stating no wait at all produced a real one: `The waiter was quoted
#: at 900 seconds` guessed 900s, the cap exactly, and `resettlement of 300 seconds of logs` guessed
#: 300s. Anchoring the markers and leaving this scan a version behind is what made that possible;
#: the two scans share one rule now.
#: EVERY INFLECTION IS THEREFORE SPELLED OUT, and that is the only reason this list is long. A bare
#: stem with `\b` after it loses `waiting`, `resets`, `resumed` and `retrying` — wordings providers
#: actually use — so a stem is not an option here. Add a trigger and spell its inflections; never
#: shorten one back to a stem, and never re-open the tail to cover an inflection you left out.
TRIGGER_RE = re.compile(
    r"\b(?:retry|retries|retrying|retried|try\s+again"
    r"|wait|waits|waiting|waited"
    r"|back\s?offs?"
    r"|reset|resets|resetting"
    r"|available"
    r"|resume|resumes|resumed|resuming"
    r"|cool\s?downs?)\b",
    re.IGNORECASE,
)
#: How far from a trigger word a number may sit and still be that trigger's delay — the same distance
#: on both sides. Arbitrary, and deliberately generous enough for `retry in about 60 seconds`.
#: The two passes above are ordered rather than merged into one both-sided scan BECAUSE a provider
#: sentence can carry telemetry in front of its real trigger: in `usage limit reached: retrieved 12
#: files in 2 seconds before the cap; try again in 45 minutes`, the telemetry `2 seconds` sits inside
#: this window in FRONT of `try again`, so a both-sided scan guesses 2 and relaunches the external
#: reviewer seconds into a 45-minute limit, burning the pass's last external attempt. The after-trigger
#: pass wins that tie and answers 2700; the before-trigger pass only ever runs when NO pair follows any
#: trigger at all (`Rate limit reached. 2 hours until retry.`). No width can separate those two cases.
TRIGGER_WINDOW = 40
#: Widest digit run the guess converts. A wider one is not refused and not re-read as unreadable: it
#: answers just past `MAX_WAIT_SECONDS`, so the caller's existing cap check falls back natively and
#: `int()` never sees an unbounded run (past ~4300 digits CPython raises on the conversion itself).
#: Disclosed consequence: the fallback reason then quotes that sentinel, not the stated delay.
MAX_READABLE_DIGITS = 9
#: Widest slice of a message the delay guess reads. Both of its passes MATERIALIZE every match in the
#: text before choosing one, so their cost scales with the message rather than with the answer, and a
#: runaway CLI's stderr capture is an unbounded input. Bounding once at the top of the guess covers
#: BOTH passes; bounding either scan on its own only moves the amplification to the other.
#: Disclosed consequence, the same one `MAX_READABLE_DIGITS` discloses: a delay stated past this
#: bound is simply not read, so the failure takes its kind's default and then the ordinary bounded
#: fallback — the `unreadable` landing Purpose bullet 2 already promises, not a new failure mode.
#: ONLY the guess is bounded, and `classify()`'s marker scan deliberately is NOT: `re.search` keeps
#: no per-match state, so it costs no more on a huge message, while bounding it would drop a refusal
#: marker sitting past the bound and silently turn a `stop-and-ask` into the same-engine native
#: fallback this file exists to prevent. Do not "complete" this fix by bounding `classify()` too.
#: `_normalize` ahead of that scan is linear in the message and keeps no per-match state either, so
#: it leaves that reasoning intact — see `_fold_marks` for the one part of it that is not a bare
#: `str.translate`.
MAX_SCANNED_CHARS = 65536


def _unit_millis(unit: str) -> int:
    text = unit.lower()
    if text.startswith(("ms", "milli")):
        return _UNIT_MILLIS["ms"]
    return _UNIT_MILLIS[text[0]]


def _readable_seconds(match: "re.Match[str]") -> int:
    """Convert one accepted number-and-unit pair. Both passes share this so the width rule and the
    millisecond conversion cannot drift apart."""

    digits = match.group("value")
    if len(digits) > MAX_READABLE_DIGITS:
        return MAX_WAIT_SECONDS + 1
    millis = int(digits) * _unit_millis(match.group("unit"))
    return math.ceil(millis / 1000)


def guess_delay_seconds(message: str) -> int | None:
    """Roughly how long the message asks the campaign to wait, or ``None`` when nothing readable
    sits near a retry-ish word. ``None`` is an ordinary answer, not a failure. A digit run wider
    than ``MAX_READABLE_DIGITS`` answers just past ``MAX_WAIT_SECONDS`` rather than a converted
    value — over-cap is all the caller does with such a number anyway.

    Two ordered passes: a pair FOLLOWING a trigger first, and only if there is none, a pair
    PRECEDING one. Never one both-sided scan — see ``TRIGGER_WINDOW``.

    Only the first ``MAX_SCANNED_CHARS`` are read, because both passes materialize every match at
    once; a delay stated past that bound reads as no delay at all. The marker scan is NOT bounded —
    see ``MAX_SCANNED_CHARS``."""

    message = message[:MAX_SCANNED_CHARS]
    triggers = list(TRIGGER_RE.finditer(message))
    if not triggers:
        return None
    pairs = list(DELAY_RE.finditer(message))
    # Pass 1 — the first pair that follows a trigger end by at most the window.
    ends = sorted(match.end() for match in triggers)
    for match in pairs:
        start = match.start()
        index = bisect_right(ends, start)
        if index and start - ends[index - 1] <= TRIGGER_WINDOW:
            return _readable_seconds(match)
    # Pass 2 — only now: the first pair that precedes a trigger start by at most the window.
    starts = sorted(match.start() for match in triggers)
    for match in pairs:
        end = match.end()
        index = bisect_left(starts, end)
        if index < len(starts) and starts[index] - end <= TRIGGER_WINDOW:
            return _readable_seconds(match)
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

    `_normalize` runs first and its result is what BOTH halves read, so the marker scan and the
    delay guess can never disagree about what the message says.
    """

    if not isinstance(message, str) or not message.strip():
        return Failure(UNKNOWN, None, None)
    text = _normalize(message)
    for kind, markers in CLASS_ORDER:
        for marker in markers:
            if _PATTERNS[marker].search(text):
                return Failure(kind, marker, guess_delay_seconds(text))
    return Failure(UNKNOWN, None, guess_delay_seconds(text))


def decide(message: object, attempts_spent: object) -> Decision:
    """Classify one failure and return the next action for THIS review pass.

    ``attempts_spent`` is how many external launches the pass has already made. It is the caller's
    whole memory: no deadline, no disable flag, nothing durable. A value that is not a non-negative
    integer is treated as exhausted, so a caller bug costs one native pass instead of an unbounded
    retry loop.

    It has NO default, here or at the CLI, and must not be given one. A default of 0 reads every
    failure as the pass's first, so a caller that omits it gets `wait-external` forever and never
    reaches ``MAX_EXTERNAL_ATTEMPTS`` — which would defeat the whole point of bounding the retries.
    Omitting it must fail loudly instead.
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
            # Required, never defaulted — see `decide`'s docstring: a default silently restarts the
            # retry count at every failure and the cap is then never reached.
            command.add_argument("--attempts-spent", type=int, required=True)
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
