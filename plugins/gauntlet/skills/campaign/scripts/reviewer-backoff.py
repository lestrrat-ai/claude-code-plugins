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
REFUSAL = "refusal"
UNRECOGNIZED = "unrecognized"

RETRY_EXTERNAL = "retry-external"
WAIT_EXTERNAL = "wait-external"
FALLBACK_NATIVE = "fallback-native"
STOP_AND_ASK = "stop-and-ask"

_TIMER_END = r"(?:\Z|[.;:!?]|,(?!\d))"
_UNITLESS_TIMER_END = r"(?:\Z|[.;!?]|,(?!\d))"
RETRY_AFTER_RE = re.compile(
    r"\bretry[\s-]+after\s*:?[\s]*(?P<value>\d+)(?![\d.])"
    rf"(?:\s*(?P<unit>seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)"
    rf"(?=\s*{_TIMER_END})"
    rf"|(?=\s*{_UNITLESS_TIMER_END}))",
    re.IGNORECASE,
)
DURATION_RE = re.compile(
    r"\b(?:retry(?:ing)?|try\s+again|backoff|wait|reset(?:s)?|available|unavailable)"
    r"\s*(?:after|in|for|:)?\s*"
    r"(?P<value>\d+)(?![\d.])\s*"
    r"(?P<unit>seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)"
    rf"(?=\s*{_TIMER_END})",
    re.IGNORECASE,
)
TIMER_PHRASE_RE = re.compile(
    r"\b(?:retry(?:ing)?|try\s+again|backoff|wait)\b"
    r"|\b(?:reset(?:s)?|available|unavailable)\b"
    r"(?=\s*(?:after|in|for|at|on)\b|\s*:\s*|\s+\d)",
    re.IGNORECASE,
)
ABSOLUTE_TIMER_PREFIX_RE = re.compile(
    r"\b(?:reset(?:s)?|available|retry(?:ing)?|try\s+again)\s+"
    r"(?:(?:at|on)\s+)?"
    r"(?:[A-Za-z]+\s+\d{1,2}|\d{1,2}\b)",
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
    r"(?:(?:at|on)\s+)?"
    r"(?P<month>[A-Za-z]+)\s+"
    r"(?P<day>\d{1,2})(?:,\s*|\s+)"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)"
    r"(?:\s*(?P<offset>Z|[+-]\d{2}:?\d{2}))?"
    r"(?:\s+(?P<year>\d{4}))?\s*"
    r"(?:\s*\((?P<zone>[^)\s,]+)(?:\s*,?\s*(?P<zone_offset>Z|[+-]\d{2}:?\d{2}))?\))?"
    rf"(?=\s*{_TIMER_END})",
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
REFUSAL_MARKERS = (
    "can't help",
    "cannot help",
    "can't assist",
    "cannot assist",
    "won't help",
    "able to help",
    "able to assist",
    "provide assistance",
    "decline",
    "refusal",
    "refused",
    "content filter",
    "content policy",
    "safety policy",
    "usage policy",
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


class InvalidMessageFile(BackoffError):
    """The message file could not be decoded as UTF-8."""


_MALFORMED_TIMER = object()


@dataclass(frozen=True)
class ExternalReviewFailure:
    """The closed, typed result of classifying one external-process failure."""

    kind: str
    retry_after_seconds: int | None
    retry_at: datetime | None
    reason: str
    timer_identity: str | None = None


@dataclass(frozen=True)
class ExternalReviewSessionState:
    """State retained only by the live campaign session."""

    external_disabled: bool = False
    external_backoff_until: datetime | None = None
    external_backoff_timer_id: str | None = None
    external_backoff_pr: int | None = None


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


def _timer_identity(value: str) -> str:
    return " ".join(value.casefold().split())


def _parse_offset(value: str | None) -> timezone | None | object:
    if value is None:
        return None
    if value.upper() == "Z":
        return timezone.utc
    match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", value)
    if match is None:
        return _MALFORMED_TIMER
    hours = int(match.group(2))
    minutes = int(match.group(3))
    if hours > 23 or minutes > 59:
        return _MALFORMED_TIMER
    delta = timedelta(hours=hours, minutes=minutes)
    if match.group(1) == "-":
        delta = -delta
    try:
        return timezone(delta)
    except ValueError:
        return _MALFORMED_TIMER


def _resolve_local_deadline(
    local: datetime,
    zone: object,
    explicit_offset: timezone | None,
    *,
    validate_explicit_offset: bool = False,
) -> datetime | None:
    if explicit_offset is not None:
        if validate_explicit_offset:
            valid_offsets: set[timedelta] = set()
            for fold in (0, 1):
                candidate = local.replace(tzinfo=zone, fold=fold)
                candidate_utc = candidate.astimezone(timezone.utc)
                round_trip = candidate_utc.astimezone(zone)
                if round_trip.replace(tzinfo=None) == local:
                    candidate_offset = candidate.utcoffset()
                    if candidate_offset is not None:
                        valid_offsets.add(candidate_offset)
            if explicit_offset.utcoffset(None) not in valid_offsets:
                return None
        return local.replace(tzinfo=explicit_offset)
    candidates: dict[datetime, datetime] = {}
    for fold in (0, 1):
        candidate = local.replace(tzinfo=zone, fold=fold)
        candidate_utc = candidate.astimezone(timezone.utc)
        round_trip = candidate_utc.astimezone(zone)
        if round_trip.replace(tzinfo=None) == local:
            candidates.setdefault(candidate_utc, candidate)
    if len(candidates) != 1:
        return None
    return next(iter(candidates.values()))


@dataclass(frozen=True)
class _TimerCandidate:
    start: int
    end: int
    delay: int
    retry_at: datetime
    identity: str


def _relative_candidates(text: str, now: datetime) -> list[_TimerCandidate] | object:
    matches = list(RETRY_AFTER_RE.finditer(text)) + list(DURATION_RE.finditer(text))
    candidates: list[_TimerCandidate] = []
    seen_spans: set[tuple[int, int]] = set()
    for match in sorted(matches, key=lambda item: item.start()):
        span = (match.start(), match.end())
        if span in seen_spans:
            continue
        seen_spans.add(span)
        try:
            delay = int(match.group("value")) * _unit_seconds(match.groupdict().get("unit"))
            retry_at = (_utc(now) + timedelta(seconds=delay)).astimezone(now.tzinfo)
        except (OverflowError, ValueError):
            return _MALFORMED_TIMER
        candidates.append(_TimerCandidate(
            match.start(), match.end(), delay, retry_at, _timer_identity(match.group(0))
        ))
    return candidates


def _absolute_timer_match(match: re.Match[str] | None, now: datetime) -> tuple[int, datetime, str] | None:
    if match is None:
        return None
    zone_name = match.group("zone")
    try:
        offset = _parse_offset(match.group("offset"))
        zone_offset = _parse_offset(match.group("zone_offset"))
        if offset is _MALFORMED_TIMER or zone_offset is _MALFORMED_TIMER:
            return None
        if offset is not None and zone_offset is not None and offset != zone_offset:
            return None
        explicit_offset = zone_offset if zone_offset is not None else offset
        zone = ZoneInfo(zone_name) if zone_name else (explicit_offset or _require_aware(now).tzinfo)
        if zone is None:
            return None
        local_now = now.astimezone(zone)
        month = MONTHS[match.group("month").lower()]
        year = int(match.group("year") or local_now.year)
        minute = int(match.group("minute") or 0)
        hour = int(match.group("hour"))
        if not 1 <= hour <= 12:
            return None
        ampm = match.group("ampm").lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        target_local = datetime(year, month, int(match.group("day")), hour, minute)
        if not match.group("year") and target_local < local_now.replace(tzinfo=None):
            target_local = target_local.replace(year=target_local.year + 1)
        target = _resolve_local_deadline(
            target_local,
            zone,
            explicit_offset,
            validate_explicit_offset=zone_name is not None,
        )
        if target is None:
            return None
        target_utc = target.astimezone(timezone.utc)
        local_now_utc = local_now.astimezone(timezone.utc)
        if target_utc < local_now_utc and match.group("year"):
            return None
        return max(0, _ceil_seconds(target_utc - local_now_utc)), target, _timer_identity(match.group(0))
    except (KeyError, OverflowError, ValueError, TypeError, ZoneInfoNotFoundError):
        return None


def _absolute_timer(text: str, now: datetime) -> tuple[int, datetime, str] | None:
    return _absolute_timer_match(ABSOLUTE_RE.search(text), now)


def _timer_candidates(text: str, now: datetime) -> list[_TimerCandidate] | object:
    relative = _relative_candidates(text, now)
    if relative is _MALFORMED_TIMER:
        return _MALFORMED_TIMER

    candidates = list(relative)
    for match in ABSOLUTE_RE.finditer(text):
        absolute = _absolute_timer_match(match, now)
        if absolute is None:
            return _MALFORMED_TIMER
        delay, retry_at, identity = absolute
        candidates.append(_TimerCandidate(
            match.start(), match.end(), delay, retry_at, identity
        ))

    valid_spans = [(candidate.start, candidate.end) for candidate in candidates]
    for match in TIMER_PHRASE_RE.finditer(text):
        if not any(start <= match.start() < end for start, end in valid_spans):
            return _MALFORMED_TIMER
    for match in ABSOLUTE_TIMER_PREFIX_RE.finditer(text):
        if not any(start <= match.start() < end for start, end in valid_spans):
            return _MALFORMED_TIMER
    return candidates


def _timer(text: str, now: datetime) -> tuple[int, datetime, str] | None | object:
    candidates = _timer_candidates(text, now)
    if candidates is _MALFORMED_TIMER:
        return _MALFORMED_TIMER
    if not candidates:
        return None
    candidate = min(candidates, key=lambda item: item.start)
    return candidate.delay, candidate.retry_at, candidate.identity


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


def _refusal(reason: str) -> ExternalReviewFailure:
    return ExternalReviewFailure(REFUSAL, None, None, reason)


def _unrecognized(reason: str) -> ExternalReviewFailure:
    return ExternalReviewFailure(UNRECOGNIZED, None, None, reason)


def _valid_failure(value: object) -> bool:
    if not isinstance(value, ExternalReviewFailure) or not isinstance(value.reason, str):
        return False
    if value.kind in (TRANSIENT, PERMANENT, REFUSAL, UNRECOGNIZED):
        return (
            value.retry_after_seconds is None
            and value.retry_at is None
            and value.timer_identity is None
        )
    if value.kind != TIMER:
        return False
    return (
        type(value.retry_after_seconds) is int
        and value.retry_after_seconds >= 0
        and _is_aware(value.retry_at)
        and (
            value.timer_identity is None
            or (type(value.timer_identity) is str and bool(value.timer_identity))
        )
    )


def _valid_state(value: object) -> bool:
    return (
        isinstance(value, ExternalReviewSessionState)
        and type(value.external_disabled) is bool
        and (value.external_backoff_until is None or _is_aware(value.external_backoff_until))
        and (
            value.external_backoff_timer_id is None
            or (type(value.external_backoff_timer_id) is str and bool(value.external_backoff_timer_id))
        )
        and (
            value.external_backoff_pr is None
            or (type(value.external_backoff_pr) is int and value.external_backoff_pr > 0)
        )
        and (
            (
                value.external_backoff_until is None
                and value.external_backoff_timer_id is None
                and value.external_backoff_pr is None
            )
            or (
                value.external_backoff_until is not None
                and type(value.external_backoff_pr) is int
                and value.external_backoff_pr > 0
            )
        )
    )


def _read_failure(path: Path) -> ExternalReviewFailure:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        _fail(f"cannot read --failure-file: {exc}")
    except UnicodeDecodeError as exc:
        _fail(f"--failure-file is not valid UTF-8: {exc}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        _fail(f"--failure-file is not valid JSON: {exc}")
    if not isinstance(payload, dict):
        _fail("--failure-file must contain one ExternalReviewFailure object")
    expected = {"kind", "retry_after_seconds", "retry_at", "reason", "timer_identity"}
    if set(payload) != expected:
        _fail("--failure-file has the wrong ExternalReviewFailure fields")

    kind = payload["kind"]
    retry_after_seconds = payload["retry_after_seconds"]
    retry_at_value = payload["retry_at"]
    reason = payload["reason"]
    timer_identity = payload["timer_identity"]
    if not isinstance(kind, str) or not isinstance(reason, str):
        _fail("--failure-file has non-text kind or reason")
    if retry_after_seconds is not None and type(retry_after_seconds) is not int:
        _fail("--failure-file has a non-integer retry_after_seconds")
    if retry_at_value is not None and not isinstance(retry_at_value, str):
        _fail("--failure-file has a non-text retry_at")
    if timer_identity is not None and not isinstance(timer_identity, str):
        _fail("--failure-file has a non-text timer_identity")
    retry_at = parse_now(retry_at_value) if retry_at_value is not None else None
    failure = ExternalReviewFailure(
        kind,
        retry_after_seconds,
        retry_at,
        reason,
        timer_identity,
    )
    if not _valid_failure(failure):
        _fail("--failure-file contains a malformed ExternalReviewFailure")
    return failure


def classify(message: str, now: datetime | None = None) -> ExternalReviewFailure:
    """Classify every external-process error before route selection.

    Refusal markers take precedence over every other marker: a reviewer that declined the task on
    content or policy grounds is not a transport failure, and swapping in a same-engine native
    reviewer would silently drop the engine diversity the gate relies on. That marker set is
    deliberately NOT exhaustive; refusal wording it does not match stays unrecognized.
    Permanent markers come next. Valid whole-second timer text wins over transient markers;
    malformed or unsupported timer text is permanent for this session. Opaque text is unrecognized
    and falls back without disabling the external route. A numeric offset paired with a named timezone
    must match that zone's valid offset for the target local time.
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
    for marker in REFUSAL_MARKERS:
        if marker in lowered:
            return _refusal(f"refusal marker: {marker}")
    for marker in PERMANENT_MARKERS:
        if marker in lowered:
            return _permanent(f"permanent marker: {marker}")
    timer = _timer(text, current)
    if timer is _MALFORMED_TIMER:
        return _permanent("provider supplied an invalid retry timer")
    if timer is not None:
        delay, retry_at, timer_identity = timer
        return ExternalReviewFailure(
            TIMER, delay, retry_at, "provider supplied a retry timer", timer_identity
        )
    for marker in TRANSIENT_MARKERS:
        if _contains_transient_marker(lowered, marker):
            return ExternalReviewFailure(TRANSIENT, None, None, f"transient marker: {marker}")
    return _unrecognized("no recognized retry class or timer was identified")


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
    pr_number: int | None = None,
) -> Decision:
    """Apply one typed failure to session state and return the next recovery action.

    A refusal never recovers automatically: it returns ``stop-and-ask`` with the session state
    unchanged, before any retry, timer wait, or native fallback can consume it.
    """

    prior = state if state is not None else ExternalReviewSessionState()
    if not _valid_state(prior):
        prior = ExternalReviewSessionState(external_disabled=True)
        failure = _permanent("external session state was malformed")
    elif state is not None and pr_number is None:
        failure = _permanent("external review PR number was omitted for a session transition")
    elif pr_number is not None and (type(pr_number) is not int or pr_number <= 0):
        failure = _permanent("external review PR number was malformed")
        pr_number = None
    elif not isinstance(failure, ExternalReviewFailure):
        failure = _permanent("external failure classification was malformed")
    elif failure.kind not in (TRANSIENT, TIMER, PERMANENT, REFUSAL, UNRECOGNIZED):
        failure = _unrecognized("unrecognized external failure classification")
    elif not _valid_failure(failure):
        failure = _permanent("external failure classification was malformed")
    elif failure.kind == TIMER and pr_number is None:
        failure = _permanent("external review PR number is required for a timer transition")
    current = _as_aware(now if now is not None else datetime.now(timezone.utc))
    if current is None:
        failure = _permanent("external transition had an invalid timestamp")
        current = datetime.now(timezone.utc)
    if failure.kind == REFUSAL:
        return _decision(STOP_AND_ASK, failure, prior, failure.reason)
    if prior.external_disabled:
        return _decision(FALLBACK_NATIVE, failure, prior, "external route is disabled for this session")
    prior_deadline_active = (
        prior.external_backoff_until is not None
        and _utc(prior.external_backoff_until) > _utc(current)
    )
    same_timer_identity = (
        failure.kind == TIMER
        and failure.timer_identity is not None
        and failure.timer_identity == prior.external_backoff_timer_id
    )
    same_timer_owner = (
        failure.kind == TIMER
        and prior.external_backoff_pr == pr_number
    )
    different_timer_owner = (
        failure.kind == TIMER
        and prior.external_backoff_pr is not None
        and pr_number is not None
        and prior.external_backoff_pr != pr_number
    )
    same_timer_reentry = (
        same_timer_owner
        and same_timer_identity
        and failure.retry_at is not None
        and _utc(failure.retry_at) == _utc(prior.external_backoff_until)
    )
    deadline = _max_deadline(prior.external_backoff_until, failure.retry_at if failure.kind == TIMER else None)
    timer_id = failure.timer_identity if failure.kind == TIMER else prior.external_backoff_timer_id
    timer_pr = prior.external_backoff_pr
    if failure.kind == TIMER and pr_number is not None:
        timer_pr = pr_number
    if failure.kind == TIMER and prior.external_backoff_until is not None:
        incoming_deadline = failure.retry_at
        if same_timer_reentry:
            deadline = prior.external_backoff_until
            timer_id = prior.external_backoff_timer_id
            failure = ExternalReviewFailure(
                TIMER, 0, prior.external_backoff_until, failure.reason, failure.timer_identity
            )
        elif same_timer_owner and incoming_deadline is not None:
            deadline = incoming_deadline
            timer_id = failure.timer_identity
        elif different_timer_owner and prior_deadline_active:
            deadline = prior.external_backoff_until
            timer_id = prior.external_backoff_timer_id
            timer_pr = prior.external_backoff_pr
        elif (
            incoming_deadline is not None
            and _utc(incoming_deadline) <= _utc(prior.external_backoff_until)
        ):
            deadline = prior.external_backoff_until
            if prior.external_backoff_timer_id is not None:
                timer_id = prior.external_backoff_timer_id
    next_state = ExternalReviewSessionState(prior.external_disabled, deadline, timer_id, timer_pr)

    if failure.kind == PERMANENT:
        next_state = ExternalReviewSessionState(True, deadline, timer_id, timer_pr)
        return _decision(FALLBACK_NATIVE, failure, next_state, failure.reason)

    if different_timer_owner and prior_deadline_active:
        return _decision(FALLBACK_NATIVE, failure, next_state,
                         "session external backoff belongs to another PR")

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
    pr_number: int | None = None,
) -> Decision:
    """Classify one failure, then return retry, exact-timer wait, native fallback, or stop-and-ask."""

    current = now if now is not None else datetime.now(timezone.utc)
    return transition(
        classify(message, current), retry_spent=retry_spent, now=current, state=state,
        pr_number=pr_number,
    )


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
    except UnicodeDecodeError as exc:
        raise InvalidMessageFile(f"--message-file is not valid UTF-8: {exc}") from exc
    except OSError as exc:
        _fail(f"cannot read --message-file: {exc}")


def _state_from_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> ExternalReviewSessionState:
    if args.backoff_timer_id is not None and args.backoff_until is None:
        parser.error("--backoff-timer-id requires --backoff-until")
    if args.backoff_pr is not None and args.backoff_until is None:
        parser.error("--backoff-pr requires --backoff-until")
    return ExternalReviewSessionState(
        external_disabled=args.external_disabled,
        external_backoff_until=parse_now(args.backoff_until) if args.backoff_until else None,
        external_backoff_timer_id=args.backoff_timer_id,
        external_backoff_pr=args.backoff_pr,
    )


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
    decide_parser.add_argument("--backoff-timer-id")
    decide_parser.add_argument("--backoff-pr", type=int)
    decide_parser.add_argument("--pr", "--pr-number", dest="pr_number", type=int)
    decide_parser.add_argument("--failure-file", type=Path)
    sub.add_parser("self-test")
    args = parser.parse_args(argv)
    if args.command == "self-test":
        return self_test()
    now = parse_now(args.now)
    if args.command == "classify":
        try:
            result = classify(_message(args, parser), now)
        except InvalidMessageFile as exc:
            result = _permanent(str(exc))
    else:
        if args.failure_file is not None and (args.message is not None or args.message_file is not None):
            parser.error("provide --failure-file instead of --message or --message-file")
        if args.backoff_until is not None and args.failure_file is None:
            parser.error("--backoff-until requires --failure-file to preserve the typed failure")
        state = _state_from_args(args, parser)
        try:
            failure = (
                _read_failure(args.failure_file)
                if args.failure_file is not None
                else classify(_message(args, parser), now)
            )
        except InvalidMessageFile as exc:
            failure = _permanent(str(exc))
        result = transition(
            failure,
            retry_spent=args.retry_spent,
            now=now,
            state=state,
            pr_number=args.pr_number,
        )
    print(json.dumps(asdict(result), default=lambda value: value.isoformat(), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BackoffError as exc:
        print(f"reviewer-backoff: {exc}", file=sys.stderr)
        raise SystemExit(2)
