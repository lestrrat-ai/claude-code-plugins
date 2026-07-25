#!/usr/bin/env python3
"""Classify external-review failures and choose a session-only recovery action.

The caller keeps ``Decision.state`` in memory for the current campaign session. This helper never
reads or writes a ledger, run artifact, history entry, cache, or preference.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NoReturn
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from _gauntlet.modules import load_module_from_path


HERE = Path(__file__).resolve().parent
SIBLING = HERE / "reviewer-backoff-test.py"

TRANSIENT = "transient"
TIMER = "timer"
PERMANENT = "permanent"

RETRY_EXTERNAL = "retry-external"
WAIT_EXTERNAL = "wait-external"
FALLBACK_NATIVE = "fallback-native"

RETRY_AFTER_RE = re.compile(
    r"\bretry[\s-]+after\s*:?[\s]*(?P<value>\d+)(?![\d.])"
    r"(?:\s*(?P<unit>seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)\b"
    r"|(?!\s*[A-Za-z]))",
    re.IGNORECASE,
)
DURATION_RE = re.compile(
    r"\b(?:retry(?:ing)?|try\s+again|backoff|wait|reset(?:s)?)"
    r"\s*(?:after|in|for|:)?\s*"
    r"(?P<value>\d+)(?![\d.])\s*"
    r"(?P<unit>seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)\b",
    re.IGNORECASE,
)
TIMER_PHRASE_RE = re.compile(
    r"\b(?:retry(?:ing)?|try\s+again|backoff|wait)\b",
    re.IGNORECASE,
)
MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
ABSOLUTE_RE = re.compile(
    r"\b(?:reset(?:s)?|available|retry(?:ing)?|try\s+again)\s+"
    r"(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\s+"
    r"(?P<day>\d{1,2})(?:,\s*|\s+)"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)"
    r"(?:\s+(?P<year>\d{4}))?\s*"
    r"(?:\((?P<zone>[^)\s]+)\))?",
    re.IGNORECASE,
)

TRANSIENT_MARKERS = (
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "temporarily unavailable",
    "temporary failure",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "rate limit",
    "too many requests",
    "429",
    "502",
    "503",
    "504",
)
PERMANENT_MARKERS = (
    "authentication failed",
    "unauthorized",
    "forbidden",
    "permission denied",
    "invalid api key",
    "invalid token",
    "unknown option",
    "command not found",
    "no such file or directory",
    "usage:",
)


class BackoffError(ValueError):
    """A caller supplied an invalid backoff input."""


_MALFORMED_TIMER = object()


@dataclass(frozen=True)
class ExternalReviewFailure:
    """The closed, typed result of classifying one external-process failure."""

    kind: str
    retry_after_seconds: int | None
    retry_at: datetime | None
    reason: str


@dataclass(frozen=True)
class ExternalReviewSessionState:
    """State retained only by the live campaign session."""

    external_disabled: bool = False
    external_backoff_until: datetime | None = None


# Keep the short name used by the runtime-adapter contract and existing callers.
Failure = ExternalReviewFailure
SessionState = ExternalReviewSessionState


@dataclass(frozen=True)
class Decision:
    action: str
    kind: str
    retry_after_seconds: int | None
    retry_at: str | None
    external_disabled: bool
    external_backoff_until: str | None
    reason: str
    state: ExternalReviewSessionState


def _fail(message: str) -> NoReturn:
    raise BackoffError(message)


def _is_aware(value: object) -> bool:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return False
    try:
        return value.utcoffset() is not None
    except (AttributeError, TypeError, ValueError):
        return False


def _require_aware(value: datetime) -> datetime:
    if not _is_aware(value):
        _fail("timestamps must include a timezone offset")
    return value


def _as_aware(value: object) -> datetime | None:
    return value if _is_aware(value) else None


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _ceil_seconds(delta: timedelta) -> int:
    seconds = delta.days * 86400 + delta.seconds
    return seconds + (1 if delta.microseconds else 0)


def parse_now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        result = datetime.fromisoformat(text)
    except ValueError:
        _fail(f"invalid timestamp: {value!r}")
    return _require_aware(result)


def _unit_seconds(unit: str | None) -> int:
    if unit is None:
        return 1
    unit = unit.lower()
    if unit.startswith("s"):
        return 1
    if unit.startswith("m"):
        return 60
    if unit.startswith("h"):
        return 3600
    if unit.startswith("d"):
        return 86400
    raise ValueError(f"unsupported retry timer unit: {unit}")


def _relative_timer(text: str) -> int | None | object:
    matches = list(RETRY_AFTER_RE.finditer(text)) + list(DURATION_RE.finditer(text))
    if not matches:
        return None
    match = min(matches, key=lambda item: item.start())
    try:
        return int(match.group("value")) * _unit_seconds(match.groupdict().get("unit"))
    except (OverflowError, ValueError):
        return _MALFORMED_TIMER


def _absolute_timer(text: str, now: datetime) -> tuple[int, datetime] | None:
    match = ABSOLUTE_RE.search(text)
    if match is None:
        return None
    zone_name = match.group("zone")
    try:
        zone = ZoneInfo(zone_name) if zone_name else _require_aware(now).tzinfo
        if zone is None:
            return None
        local_now = now.astimezone(zone)
        month = MONTHS[match.group("month").lower()]
        year = int(match.group("year") or local_now.year)
        minute = int(match.group("minute") or 0)
        hour = int(match.group("hour"))
        ampm = match.group("ampm").lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        target = datetime(year, month, int(match.group("day")), hour, minute, tzinfo=zone)
        target_utc = target.astimezone(timezone.utc)
        local_now_utc = local_now.astimezone(timezone.utc)
        if target_utc < local_now_utc:
            if match.group("year"):
                return None
            target = target.replace(year=target.year + 1)
            target_utc = target.astimezone(timezone.utc)
        return max(0, _ceil_seconds(target_utc - local_now_utc)), target
    except (KeyError, OverflowError, ValueError, TypeError, ZoneInfoNotFoundError):
        return None


def _timer(text: str, now: datetime) -> tuple[int, datetime] | None | object:
    relative = _relative_timer(text)
    if relative is _MALFORMED_TIMER:
        return _MALFORMED_TIMER
    if relative is not None:
        try:
            retry_at = (_utc(now) + timedelta(seconds=relative)).astimezone(now.tzinfo)
            return relative, retry_at
        except (OverflowError, ValueError):
            return _MALFORMED_TIMER
    if ABSOLUTE_RE.search(text) is not None:
        absolute = _absolute_timer(text, now)
        return _MALFORMED_TIMER if absolute is None else absolute
    if TIMER_PHRASE_RE.search(text) is not None:
        return _MALFORMED_TIMER
    return None


def _contains_transient_marker(text: str, marker: str) -> bool:
    if marker.isdigit():
        return re.search(rf"(?<![\w.]){re.escape(marker)}(?![\w.])", text) is not None
    return marker in text


def timer_seconds(message: str, now: datetime) -> int | None:
    """Return the provider-supplied delay, or ``None`` when no timer is present."""

    if not isinstance(message, str):
        return None
    current = _as_aware(now)
    if current is None:
        return None
    result = _timer(message, current)
    return None if result is None or result is _MALFORMED_TIMER else result[0]


def _permanent(reason: str) -> ExternalReviewFailure:
    return ExternalReviewFailure(PERMANENT, None, None, reason)


def _valid_failure(value: object) -> bool:
    if not isinstance(value, ExternalReviewFailure) or not isinstance(value.reason, str):
        return False
    if value.kind in (TRANSIENT, PERMANENT):
        return value.retry_after_seconds is None and value.retry_at is None
    if value.kind != TIMER:
        return False
    return (
        type(value.retry_after_seconds) is int
        and value.retry_after_seconds >= 0
        and _is_aware(value.retry_at)
    )


def _valid_state(value: object) -> bool:
    return (
        isinstance(value, ExternalReviewSessionState)
        and type(value.external_disabled) is bool
        and (value.external_backoff_until is None or _is_aware(value.external_backoff_until))
    )


def classify(message: str, now: datetime | None = None) -> ExternalReviewFailure:
    """Classify every external-process error before route selection.

    Permanent markers take precedence. Valid whole-second timer text wins over transient markers;
    malformed or unsupported timer text is permanent for this session.
    """

    if not isinstance(message, str):
        return _permanent("external process error text was not a string")
    text = message.strip()
    current = _as_aware(now if now is not None else datetime.now(timezone.utc))
    if current is None:
        return _permanent("external failure classification had an invalid timestamp")
    if not text:
        return _permanent("external process returned no error text")
    lowered = text.lower()
    for marker in PERMANENT_MARKERS:
        if marker in lowered:
            return _permanent(f"permanent marker: {marker}")
    timer = _timer(text, current)
    if timer is _MALFORMED_TIMER:
        return _permanent("provider supplied an invalid retry timer")
    if timer is not None:
        delay, retry_at = timer
        return ExternalReviewFailure(TIMER, delay, retry_at, "provider supplied a retry timer")
    for marker in TRANSIENT_MARKERS:
        if _contains_transient_marker(lowered, marker):
            return ExternalReviewFailure(TRANSIENT, None, None, f"transient marker: {marker}")
    return _permanent("no safe retry class or timer was identified")


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _max_deadline(first: datetime | None, second: datetime | None) -> datetime | None:
    if first is None:
        return second
    if second is None:
        return first
    return first if _utc(first) >= _utc(second) else second


def _decision(
    action: str,
    failure: ExternalReviewFailure,
    state: ExternalReviewSessionState,
    reason: str,
) -> Decision:
    return Decision(
        action=action,
        kind=failure.kind,
        retry_after_seconds=failure.retry_after_seconds,
        retry_at=_iso(failure.retry_at),
        external_disabled=state.external_disabled,
        external_backoff_until=_iso(state.external_backoff_until),
        reason=reason,
        state=state,
    )


def transition(
    failure: ExternalReviewFailure,
    *,
    retry_spent: bool = False,
    now: datetime | None = None,
    state: ExternalReviewSessionState | None = None,
) -> Decision:
    """Apply one typed failure to session state and return the next recovery action."""

    prior = state if state is not None else ExternalReviewSessionState()
    if not _valid_state(prior):
        prior = ExternalReviewSessionState(external_disabled=True)
        failure = _permanent("external session state was malformed")
    elif not isinstance(failure, ExternalReviewFailure):
        failure = _permanent("external failure classification was malformed")
    elif failure.kind not in (TRANSIENT, TIMER, PERMANENT):
        failure = _permanent("unrecognized external failure classification")
    elif not _valid_failure(failure):
        failure = _permanent("external failure classification was malformed")
    current = _as_aware(now if now is not None else datetime.now(timezone.utc))
    if current is None:
        failure = _permanent("external transition had an invalid timestamp")
        current = datetime.now(timezone.utc)
    prior_deadline_active = (
        prior.external_backoff_until is not None
        and _utc(prior.external_backoff_until) > _utc(current)
    )
    deadline = _max_deadline(prior.external_backoff_until, failure.retry_at if failure.kind == TIMER else None)
    if prior_deadline_active:
        deadline = prior.external_backoff_until
    next_state = ExternalReviewSessionState(prior.external_disabled, deadline)

    if prior.external_disabled:
        return _decision(FALLBACK_NATIVE, failure, next_state, "external route is disabled for this session")

    if failure.kind == PERMANENT:
        next_state = ExternalReviewSessionState(True, deadline)
        return _decision(FALLBACK_NATIVE, failure, next_state, failure.reason)

    if deadline is not None and _utc(deadline) > _utc(current):
        if retry_spent or failure.kind != TIMER:
            return _decision(FALLBACK_NATIVE, failure, next_state, "session external backoff is active")
        wait_seconds = max(0, _ceil_seconds(_utc(deadline) - _utc(current)))
        waiting_failure = ExternalReviewFailure(TIMER, wait_seconds, deadline, failure.reason)
        return _decision(WAIT_EXTERNAL, waiting_failure, next_state, failure.reason)

    if failure.kind in (TRANSIENT, TIMER) and not retry_spent:
        return _decision(RETRY_EXTERNAL, failure, next_state, failure.reason)

    return _decision(FALLBACK_NATIVE, failure, next_state, failure.reason)


def decide(
    message: str,
    *,
    retry_spent: bool = False,
    now: datetime | None = None,
    state: ExternalReviewSessionState | None = None,
) -> Decision:
    """Classify one failure, then return retry, exact-timer wait, or native fallback."""

    current = now if now is not None else datetime.now(timezone.utc)
    return transition(classify(message, current), retry_spent=retry_spent, now=current, state=state)


def sibling_cases() -> list[tuple[str, str, object]]:
    if not SIBLING.is_file():
        raise BackoffError(f"the fixture file {SIBLING} is missing")
    module = load_module_from_path("reviewer_backoff_test", SIBLING, register=True)
    if module is None:
        raise BackoffError(f"the fixture file {SIBLING} cannot be loaded")
    cases = getattr(module, "CASES", None)
    if not cases:
        raise BackoffError(f"the fixture file {SIBLING} exports no CASES")
    return list(cases)


def self_test() -> int:
    failures = 0
    try:
        cases = sibling_cases()
    except BackoffError as exc:
        print(f"FAIL     sibling-fixtures -> {exc}")
        return 1
    for name, description, function in cases:
        try:
            function()
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL     {name:32} -> {description}\n         {type(exc).__name__}: {exc}")
            failures += 1
        else:
            print(f"ok       {name:32} -> {description}")
    if failures:
        print(f"{failures} fixture(s) FAILED — reviewer backoff contract is broken.")
        return 1
    print(f"all {len(cases)} fixtures hold — reviewer backoff contract is intact.")
    return 0


def _message(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    if (args.message is not None) == (args.message_file is not None):
        parser.error("provide exactly one of --message or --message-file")
    try:
        return args.message if args.message is not None else args.message_file.read_text(encoding="utf-8")
    except OSError as exc:
        _fail(f"cannot read --message-file: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    classify_parser = sub.add_parser("classify")
    classify_parser.add_argument("--message")
    classify_parser.add_argument("--message-file", type=Path)
    classify_parser.add_argument("--now")
    decide_parser = sub.add_parser("decide")
    decide_parser.add_argument("--message")
    decide_parser.add_argument("--message-file", type=Path)
    decide_parser.add_argument("--now")
    decide_parser.add_argument("--retry-spent", action="store_true")
    decide_parser.add_argument("--external-disabled", action="store_true")
    decide_parser.add_argument("--backoff-until")
    sub.add_parser("self-test")
    args = parser.parse_args(argv)
    if args.command == "self-test":
        return self_test()
    message = _message(args, parser)
    now = parse_now(args.now)
    if args.command == "classify":
        result = classify(message, now)
    else:
        result = decide(
            message,
            retry_spent=args.retry_spent,
            now=now,
            state=ExternalReviewSessionState(
                external_disabled=args.external_disabled,
                external_backoff_until=parse_now(args.backoff_until) if args.backoff_until else None,
            ),
        )
    print(json.dumps(asdict(result), default=lambda value: value.isoformat(), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BackoffError as exc:
        print(f"reviewer-backoff: {exc}", file=sys.stderr)
        raise SystemExit(2)
