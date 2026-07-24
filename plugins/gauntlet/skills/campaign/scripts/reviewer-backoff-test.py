#!/usr/bin/env python3
"""Focused fixtures for ``reviewer-backoff.py``."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from _gauntlet.modules import load_module_from_path


OWNER = Path(__file__).resolve().with_name("reviewer-backoff.py")
MODULE = load_module_from_path("reviewer_backoff_owner", OWNER, register=True)
if MODULE is None:
    raise RuntimeError(f"cannot load the reviewer backoff helper at {OWNER}")


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_absolute_timer_uses_provider_timezone() -> None:
    tokyo_now = datetime(2026, 7, 25, 12, 0, tzinfo=MODULE.ZoneInfo("Asia/Tokyo"))
    result = MODULE.classify("quota exhausted; resets Jul 27, 9pm (Asia/Tokyo)", tokyo_now)
    check(result.kind == MODULE.TIMER, "absolute reset was not classified as a timer")
    check(result.retry_after_seconds == 205200, "Tokyo absolute reset delay changed")

    utc_now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    utc_result = MODULE.classify("quota exhausted; resets Jul 27, 9pm (Asia/Tokyo)", utc_now)
    check(utc_result.retry_after_seconds == 172800,
          "absolute reset was not converted from the provider timezone")


def test_relative_timer_waits_exactly() -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    result = MODULE.decide("rate limited; retry after 90 seconds", now=now)
    check(result.action == MODULE.WAIT_EXTERNAL, "relative timer did not wait")
    check(result.retry_after_seconds == 90, "relative timer delay changed")
    check(result.retry_at == "2026-07-25T12:01:30+00:00", "relative retry timestamp changed")
    check(result.state.external_backoff_until == datetime(2026, 7, 25, 12, 1, 30,
                                                           tzinfo=timezone.utc),
          "timer state was not returned for the live session")


def test_timer_retries_at_deadline() -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    deadline = datetime(2026, 7, 25, 12, 1, 30, tzinfo=timezone.utc)
    failure = MODULE.classify("retry after 90 seconds", now=now)
    result = MODULE.transition(failure, now=deadline,
                               state=MODULE.SessionState(external_backoff_until=deadline))
    check(result.action == MODULE.RETRY_EXTERNAL, "timer did not retry at the exact deadline")


def test_transient_retries_immediately() -> None:
    result = MODULE.decide("upstream timeout")
    check(result.kind == MODULE.TRANSIENT, "timeout was not transient")
    check(result.action == MODULE.RETRY_EXTERNAL, "transient failure did not retry")


def test_permanent_disables_external_route_for_session() -> None:
    result = MODULE.decide("permission denied by provider")
    check(result.kind == MODULE.PERMANENT, "permission failure was not permanent")
    check(result.action == MODULE.FALLBACK_NATIVE, "permanent failure did not fall back")
    check(result.external_disabled and result.state.external_disabled,
          "permanent failure did not disable the session route")


def test_unknown_fails_closed_as_permanent() -> None:
    result = MODULE.decide("opaque provider failure")
    check(result.kind == MODULE.PERMANENT, "unknown failure was not permanent")
    check(result.action == MODULE.FALLBACK_NATIVE, "unknown failure did not fall back")
    check(result.state.external_disabled, "unknown failure did not disable the session route")


def test_unrecognized_typed_failure_fails_closed() -> None:
    failure = MODULE.Failure("future-kind", None, None, "future provider classification")
    result = MODULE.transition(failure)
    check(result.kind == MODULE.PERMANENT, "unknown typed failure was not normalized to permanent")
    check(result.state.external_disabled, "unknown typed failure did not disable the session route")


def test_disabled_state_is_session_only_and_blocks_new_launches() -> None:
    state = MODULE.SessionState(external_disabled=True)
    result = MODULE.decide("temporary failure", now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
                           state=state)
    check(result.action == MODULE.FALLBACK_NATIVE, "disabled session route was retried")
    check(result.state == state, "disabled state was changed outside the caller's session state")


def test_timer_after_retry_falls_back_but_keeps_timer() -> None:
    result = MODULE.decide(
        "retry after 2 minutes",
        retry_spent=True,
        now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
    )
    check(result.action == MODULE.FALLBACK_NATIVE, "spent timer retry did not fall back")
    check(result.kind == MODULE.TIMER and result.retry_after_seconds == 120,
          "spent timer classification changed")
    check(result.state.external_backoff_until is not None,
          "spent timer did not retain session backoff")
    check(not result.external_disabled, "timer failure incorrectly disabled external review")


def test_zero_delay_timer_retries_immediately() -> None:
    result = MODULE.decide("retry-after: 0", now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc))
    check(result.kind == MODULE.TIMER and result.action == MODULE.RETRY_EXTERNAL,
          "zero-delay timer was not an immediate retry")


CASES = [
    ("absolute-provider-timezone", "absolute reset uses provider timezone", test_absolute_timer_uses_provider_timezone),
    ("relative-exact-wait", "relative delay is preserved exactly", test_relative_timer_waits_exactly),
    ("timer-deadline-retry", "timer retries at its exact deadline", test_timer_retries_at_deadline),
    ("transient-immediate-retry", "transient failure retries immediately", test_transient_retries_immediately),
    ("permanent-session-disable", "permanent failure disables only this session", test_permanent_disables_external_route_for_session),
    ("unknown-permanent", "unknown failure fails closed", test_unknown_fails_closed_as_permanent),
    ("unknown-typed-permanent", "unknown typed failure fails closed", test_unrecognized_typed_failure_fails_closed),
    ("session-state", "disabled state blocks later launches in this session", test_disabled_state_is_session_only_and_blocks_new_launches),
    ("spent-timer-fallback", "spent timer falls back and retains session backoff", test_timer_after_retry_falls_back_but_keeps_timer),
    ("zero-delay-timer", "zero-delay timer retries immediately", test_zero_delay_timer_retries_immediately),
]


def main() -> int:
    failures = 0
    for name, description, function in CASES:
        try:
            function()
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL     {name:32} -> {description}: {exc}")
            failures += 1
        else:
            print(f"ok       {name:32} -> {description}")
    if failures:
        print(f"{failures} fixture(s) FAILED")
        return 1
    print(f"reviewer-backoff fixtures: {len(CASES)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
