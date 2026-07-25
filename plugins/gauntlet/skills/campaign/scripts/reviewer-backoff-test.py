#!/usr/bin/env python3
"""Focused fixtures for ``reviewer-backoff.py``."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from _gauntlet.modules import load_module_from_path


OWNER = Path(__file__).resolve().with_name("reviewer-backoff.py")
MODULE = load_module_from_path("reviewer_backoff_owner", OWNER, register=True)
if MODULE is None:
    raise RuntimeError(f"cannot load the reviewer backoff helper at {OWNER}")


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_cli(*args: str) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, str(OWNER), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    check(completed.returncode == 0,
          f"reviewer-backoff CLI failed ({completed.returncode}): {completed.stderr!r}")
    payload = json.loads(completed.stdout)
    check(isinstance(payload, dict), f"reviewer-backoff CLI returned non-object JSON: {payload!r}")
    return payload


def test_absolute_timer_uses_provider_timezone() -> None:
    tokyo_now = datetime(2026, 7, 25, 12, 0, tzinfo=MODULE.ZoneInfo("Asia/Tokyo"))
    result = MODULE.classify("quota exhausted; resets Jul 27, 9pm (Asia/Tokyo)", tokyo_now)
    check(result.kind == MODULE.TIMER, "absolute reset was not classified as a timer")
    check(result.retry_after_seconds == 205200, "Tokyo absolute reset delay changed")

    utc_now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    utc_result = MODULE.classify("quota exhausted; resets Jul 27, 9pm (Asia/Tokyo)", utc_now)
    check(utc_result.retry_after_seconds == 172800,
          "absolute reset was not converted from the provider timezone")


def test_absolute_timer_uses_elapsed_time_across_dst() -> None:
    now = datetime(2026, 3, 8, 1, 30, tzinfo=MODULE.ZoneInfo("America/New_York"))
    result = MODULE.classify("quota exhausted; resets Mar 8, 3:30am (America/New_York)", now)
    check(result.kind == MODULE.TIMER, "DST deadline was not classified as a timer")
    check(result.retry_after_seconds == 3600,
          "absolute deadline was subtracted as wall-clock time across DST")


def test_timer_wait_uses_elapsed_time_across_dst() -> None:
    now = datetime(2026, 3, 8, 1, 30, tzinfo=MODULE.ZoneInfo("America/New_York"))
    failure = MODULE.classify("quota exhausted; resets Mar 8, 3:30am (America/New_York)", now)
    result = MODULE.transition(failure, now=now)
    check(result.action == MODULE.WAIT_EXTERNAL, "DST timer did not wait")
    check(result.retry_after_seconds == 3600,
          "timer wait was subtracted as wall-clock time across DST")


def test_absolute_timer_dst_gap_fails_closed() -> None:
    now = datetime(2026, 3, 1, 12, 0, tzinfo=MODULE.ZoneInfo("America/New_York"))
    result = MODULE.decide("quota exhausted; resets Mar 8, 2:30am (America/New_York)", now=now)
    check(result.kind == MODULE.PERMANENT, "DST gap was accepted as an absolute timer")
    check(result.action == MODULE.FALLBACK_NATIVE, "DST gap did not fall back natively")
    check(result.external_disabled, "DST gap did not disable the external route")


def test_absolute_timer_dst_fold_fails_closed() -> None:
    now = datetime(2026, 3, 1, 12, 0, tzinfo=MODULE.ZoneInfo("America/New_York"))
    result = MODULE.decide("quota exhausted; resets Nov 1, 1:30am (America/New_York)", now=now)
    check(result.kind == MODULE.PERMANENT, "DST fold was accepted without an explicit offset")
    check(result.action == MODULE.FALLBACK_NATIVE, "DST fold did not fall back natively")
    check(result.external_disabled, "DST fold did not disable the external route")


def test_absolute_timer_explicit_offset_resolves_dst_fold() -> None:
    now = datetime(2026, 3, 1, 12, 0, tzinfo=MODULE.ZoneInfo("America/New_York"))
    result = MODULE.decide(
        "quota exhausted; resets Nov 1, 1:30am -05:00 (America/New_York)",
        now=now,
    )
    check(result.kind == MODULE.TIMER, "explicit offset did not resolve the DST fold")
    check(result.retry_at == "2026-11-01T01:30:00-05:00",
          "explicit offset changed the absolute provider deadline")


def test_absolute_timer_explicit_offset_resolves_both_dst_fold_offsets() -> None:
    now = datetime(2026, 3, 1, 12, 0, tzinfo=MODULE.ZoneInfo("America/New_York"))
    for offset in ("-04:00", "-05:00"):
        result = MODULE.decide(
            f"quota exhausted; resets Nov 1, 1:30am {offset} (America/New_York)",
            now=now,
        )
        check(result.kind == MODULE.TIMER,
              f"valid DST fold offset {offset} was rejected")


def test_absolute_timer_rejects_contradictory_named_zone_offset() -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    result = MODULE.decide(
        "rate limit; resets Jul 27, 9pm +14:00 (Asia/Tokyo)",
        now=now,
    )
    check(result.kind == MODULE.PERMANENT,
          "contradictory named-zone offset was accepted as a timer")
    check(result.action == MODULE.FALLBACK_NATIVE,
          "contradictory named-zone offset did not fall back natively")
    check(result.external_disabled,
          "contradictory named-zone offset did not disable the external route")


def test_relative_timer_waits_exactly() -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    result = MODULE.decide("rate limited; retry after 90 seconds", now=now)
    check(result.action == MODULE.WAIT_EXTERNAL, "relative timer did not wait")
    check(result.retry_after_seconds == 90, "relative timer delay changed")
    check(result.retry_at == "2026-07-25T12:01:30+00:00", "relative retry timestamp changed")
    check(result.state.external_backoff_until == datetime(2026, 7, 25, 12, 1, 30,
                                                           tzinfo=timezone.utc),
          "timer state was not returned for the live session")


def test_unitless_relative_timer_defaults_to_seconds() -> None:
    result = MODULE.classify(
        "rate limited; retry after 90",
        now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
    )
    check(result.kind == MODULE.TIMER,
          "unitless relative timer was not classified as a timer")
    check(result.retry_after_seconds == 90,
          "unitless relative timer did not default to seconds")


def test_comma_grouped_relative_timer_fails_closed() -> None:
    result = MODULE.decide(
        "rate limited; retry after 90,000 seconds",
        now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
    )
    check(result.kind == MODULE.PERMANENT,
          "comma-grouped relative timer was accepted")
    check(result.action == MODULE.FALLBACK_NATIVE,
          "comma-grouped relative timer did not fall back")
    check(result.state.external_disabled,
          "comma-grouped relative timer did not disable the session route")


def test_unsupported_relative_timer_unit_fails_closed() -> None:
    result = MODULE.decide(
        "rate limited; retry after 90 bananas",
        now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
    )
    check(result.kind == MODULE.PERMANENT,
          "unsupported relative timer unit was accepted")
    check(result.action == MODULE.FALLBACK_NATIVE,
          "unsupported relative timer unit did not fall back")
    check(result.state.external_disabled,
          "unsupported relative timer unit did not disable the session route")


def test_unsupported_relative_timer_punctuation_fails_closed() -> None:
    for message in (
        "rate limited; retry after 90%",
        "rate limited; retry after 90 seconds%",
        "rate limited; available in 90 seconds%",
    ):
        result = MODULE.decide(
            message,
            now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
        )
        check(result.kind == MODULE.PERMANENT,
              f"unsupported relative timer punctuation was accepted: {message!r}")
        check(result.action == MODULE.FALLBACK_NATIVE,
              f"unsupported relative timer punctuation did not fall back: {message!r}")
        check(result.state.external_disabled,
              f"unsupported relative timer punctuation did not disable the session route: {message!r}")


def test_incomplete_absolute_reset_fails_closed() -> None:
    result = MODULE.decide(
        "rate limit; resets Jul 27",
        now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
    )
    check(result.kind == MODULE.PERMANENT,
          "incomplete absolute reset was accepted as a transient failure")
    check(result.action == MODULE.FALLBACK_NATIVE,
          "incomplete absolute reset did not fall back")
    check(result.state.external_disabled,
          "incomplete absolute reset did not disable the session route")


def test_absolute_timer_suffix_fails_closed() -> None:
    result = MODULE.decide(
        "rate limit; resets Jul 27, 9pm (Asia/Tokyo)%",
        now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
    )
    check(result.kind == MODULE.PERMANENT,
          "absolute timer with an unsupported suffix was accepted")
    check(result.action == MODULE.FALLBACK_NATIVE,
          "absolute timer with an unsupported suffix did not fall back")
    check(result.state.external_disabled,
          "absolute timer with an unsupported suffix did not disable the session route")


def test_malformed_timer_before_valid_timer_fails_closed() -> None:
    result = MODULE.decide(
        "rate limit; retry after never seconds; retry after 90 seconds",
        now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
    )
    check(result.kind == MODULE.PERMANENT,
          "malformed earlier timer clause was ignored")
    check(result.action == MODULE.FALLBACK_NATIVE,
          "mixed malformed and valid timers did not fall back")
    check(result.state.external_disabled,
          "mixed malformed and valid timers did not disable the session route")


def test_malformed_reset_timer_fails_closed() -> None:
    for message in (
        "temporarily unavailable; reset after 90 bananas",
        "temporarily unavailable; reset after 1.5 seconds",
    ):
        result = MODULE.decide(
            message,
            now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
        )
        check(result.kind == MODULE.PERMANENT,
              f"malformed reset timer was accepted: {message!r}")
        check(result.action == MODULE.FALLBACK_NATIVE,
              f"malformed reset timer did not fall back: {message!r}")
        check(result.state.external_disabled,
              f"malformed reset timer did not disable the session route: {message!r}")


def test_session_timer_deadline_is_not_extended_on_reentry() -> None:
    first_now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    failure = MODULE.classify("retry after 90 seconds", now=first_now)
    first = MODULE.transition(failure, now=first_now, pr_number=188)
    second = MODULE.transition(
        failure,
        now=first_now.replace(second=30),
        state=first.state,
        pr_number=188,
    )
    expected_deadline = datetime(2026, 7, 25, 12, 1, 30, tzinfo=timezone.utc)
    check(first.state.external_backoff_until == expected_deadline,
          "initial timer did not create the expected session deadline")
    check(second.action == MODULE.WAIT_EXTERNAL,
          "active session timer did not keep the external route waiting")
    check(second.retry_after_seconds == 60,
          "re-entry did not wait for the remaining session timer")
    check(second.state.external_backoff_until == expected_deadline,
          "re-entry extended the active session deadline")


def test_active_identical_new_timer_replaces_deadline() -> None:
    first_now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    first = MODULE.decide("retry after 90 seconds", now=first_now, pr_number=188)
    later_now = first_now.replace(second=30)
    later_failure = MODULE.classify("retry after 90 seconds", later_now)
    result = MODULE.transition(later_failure, now=later_now, state=first.state, pr_number=188)
    expected_deadline = datetime(2026, 7, 25, 12, 2, tzinfo=timezone.utc)
    check(result.action == MODULE.WAIT_EXTERNAL,
          "active identical fresh timer did not keep the external route waiting")
    check(result.retry_after_seconds == 90,
          "active identical fresh timer was shortened to the old deadline")
    check(result.state.external_backoff_until == expected_deadline,
          "active identical fresh timer did not replace the old deadline")


def test_active_later_timer_replaces_deadline_and_identity() -> None:
    first_now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    first = MODULE.decide("retry after 90 seconds", now=first_now, pr_number=188)
    later_now = first_now.replace(second=10)
    later_failure = MODULE.classify("retry after 2 minutes", later_now)
    result = MODULE.transition(later_failure, now=later_now, state=first.state, pr_number=188)
    expected_deadline = datetime(2026, 7, 25, 12, 2, 10, tzinfo=timezone.utc)
    check(result.action == MODULE.WAIT_EXTERNAL,
          "active later timer did not keep the external route waiting")
    check(result.retry_after_seconds == 120,
          "active later timer was shortened to the earlier deadline")
    check(result.state.external_backoff_until == expected_deadline,
          "active later timer did not replace the earlier deadline")
    check(result.state.external_backoff_timer_id == later_failure.timer_identity,
          "active later timer did not retain its timer identity")


def test_active_earlier_timer_replaces_deadline_for_same_pr() -> None:
    first_now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    first = MODULE.decide("retry after 120 seconds", now=first_now, pr_number=188)
    later_now = first_now.replace(second=10)
    result = MODULE.decide(
        "retry after 30 seconds",
        now=later_now,
        state=first.state,
        pr_number=188,
    )
    expected_deadline = datetime(2026, 7, 25, 12, 0, 40, tzinfo=timezone.utc)
    check(result.action == MODULE.WAIT_EXTERNAL,
          "same-PR earlier timer did not keep the external route waiting")
    check(result.retry_after_seconds == 30,
          "same-PR earlier timer retained the old delay")
    check(result.state.external_backoff_until == expected_deadline,
          "same-PR earlier timer retained the old deadline")
    check(result.state.external_backoff_pr == 188,
          "same-PR timer did not retain its PR owner")


def test_active_timer_for_another_pr_falls_back_and_retains_owner() -> None:
    first_now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    first = MODULE.decide("retry after 120 seconds", now=first_now, pr_number=188)
    later_now = first_now.replace(second=10)
    result = MODULE.decide(
        "retry after 30 seconds",
        now=later_now,
        state=first.state,
        pr_number=189,
    )
    expected_deadline = datetime(2026, 7, 25, 12, 2, tzinfo=timezone.utc)
    check(result.action == MODULE.FALLBACK_NATIVE,
          "another PR was allowed to use the active external timer")
    check(result.retry_after_seconds == 30,
          "another PR lost its provider timer while falling back")
    check(result.state.external_backoff_until == expected_deadline,
          "another PR changed the existing session backoff")
    check(result.state.external_backoff_pr == 188,
          "another PR replaced the existing session timer owner")


def test_expired_timer_for_another_pr_replaces_deadline_and_waits() -> None:
    first_now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    first = MODULE.decide("retry after 120 seconds", now=first_now, pr_number=188)
    later_now = datetime(2026, 7, 25, 12, 2, 1, tzinfo=timezone.utc)
    result = MODULE.decide(
        "retry after 30 seconds",
        now=later_now,
        state=first.state,
        pr_number=189,
    )
    expected_deadline = datetime(2026, 7, 25, 12, 2, 31, tzinfo=timezone.utc)
    check(result.action == MODULE.WAIT_EXTERNAL,
          "another PR did not wait on its timer after the prior deadline expired")
    check(result.retry_after_seconds == 30,
          "another PR did not retain its provider timer after the prior deadline expired")
    check(result.state.external_backoff_until == expected_deadline,
          "another PR retained the expired session backoff")
    check(result.state.external_backoff_timer_id != first.state.external_backoff_timer_id,
          "another PR reused the expired session timer identity")
    check(result.state.external_backoff_pr == 189,
          "another PR did not become the owner after the prior deadline expired")


def test_session_timer_retries_at_exact_reentry_deadline() -> None:
    first_now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    failure = MODULE.classify("retry after 90 seconds", now=first_now)
    first = MODULE.transition(failure, now=first_now, pr_number=188)
    deadline = first_now.replace(minute=1, second=30)
    result = MODULE.transition(failure, now=deadline, state=first.state, pr_number=188)
    check(result.action == MODULE.RETRY_EXTERNAL,
          "re-entry at the session deadline did not retry")
    check(result.retry_after_seconds == 0,
          "exact-deadline re-entry retained a stale timer delay")
    check(result.state.external_backoff_until == deadline,
          "exact-deadline re-entry changed the stored deadline")
    check(result.state.external_backoff_timer_id == first.state.external_backoff_timer_id,
          "exact-deadline re-entry changed the session timer identity")


def test_cli_preserves_typed_timer_across_reentry() -> None:
    first_now = "2026-07-25T12:00:00+00:00"
    deadline = "2026-07-25T12:01:30+00:00"
    failure = run_cli(
        "classify",
        "--message", "rate limited; retry after 90 seconds",
        "--now", first_now,
    )
    check(failure["kind"] == MODULE.TIMER, "CLI classifier did not return a timer failure")
    check(failure["retry_at"] == deadline, "CLI classifier changed the provider deadline")
    timer_id = failure["timer_identity"]
    check(isinstance(timer_id, str) and timer_id, "CLI classifier lost the timer identity")

    with tempfile.TemporaryDirectory(prefix="reviewer backoff ") as directory:
        failure_file = Path(directory) / "failure.json"
        failure_file.write_text(json.dumps(failure), encoding="utf-8")
        waiting = run_cli(
            "decide",
            "--failure-file", str(failure_file),
            "--now", first_now,
            "--backoff-until", deadline,
            "--backoff-timer-id", timer_id,
            "--backoff-pr", "188",
            "--pr", "188",
        )
        check(waiting["action"] == MODULE.WAIT_EXTERNAL,
              "CLI typed timer did not wait before its deadline")
        check(waiting["state"]["external_backoff_timer_id"] == timer_id,
              "CLI wait dropped the timer identity")
        check(waiting["state"]["external_backoff_pr"] == 188,
              "CLI wait dropped the timer PR owner")

        raw_reentry = subprocess.run(
            [sys.executable, str(OWNER), "decide",
             "--message", "rate limited; retry after 90 seconds",
             "--now", deadline, "--backoff-until", deadline,
             "--backoff-timer-id", timer_id, "--backoff-pr", "188", "--pr", "188"],
            capture_output=True,
            text=True,
            check=False,
        )
        check(raw_reentry.returncode != 0 and "--failure-file" in raw_reentry.stderr,
              "CLI allowed raw-message re-entry to reclassify the timer")

        reentry = run_cli(
            "decide",
            "--failure-file", str(failure_file),
            "--now", deadline,
            "--backoff-until", deadline,
            "--backoff-timer-id", timer_id,
            "--backoff-pr", "188",
            "--pr", "188",
        )
    check(reentry["action"] == MODULE.RETRY_EXTERNAL,
          "CLI exact-deadline re-entry did not retry immediately")
    check(reentry["retry_after_seconds"] == 0,
          "CLI exact-deadline re-entry retained a stale timer delay")
    check(reentry["retry_at"] == deadline,
          "CLI exact-deadline re-entry changed the typed provider deadline")
    check(reentry["state"]["external_backoff_timer_id"] == timer_id,
          "CLI exact-deadline re-entry changed the timer identity")


def test_cli_invalid_utf8_message_file_falls_back() -> None:
    with tempfile.TemporaryDirectory(prefix="reviewer backoff ") as directory:
        message_file = Path(directory) / "message.bin"
        message_file.write_bytes(b"rate limited; retry after 90 seconds \xff")
        completed = subprocess.run(
            [sys.executable, str(OWNER), "decide", "--message-file", str(message_file),
             "--now", "2026-07-25T12:00:00+00:00"],
            capture_output=True,
            text=True,
            check=False,
        )
    check(completed.returncode == 0,
          f"invalid UTF-8 message file was not handled: {completed.stderr!r}")
    result = json.loads(completed.stdout)
    check(result["kind"] == MODULE.PERMANENT,
          "invalid UTF-8 message file was not classified as permanent")
    check(result["action"] == MODULE.FALLBACK_NATIVE,
          "invalid UTF-8 message file did not fall back")
    check(result["external_disabled"],
          "invalid UTF-8 message file did not disable the external route")
    check("Traceback" not in completed.stderr,
          "invalid UTF-8 message file emitted a traceback")


def test_identical_text_new_timer_replaces_expired_session_deadline() -> None:
    first_now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    first = MODULE.decide("retry after 90 seconds", now=first_now, pr_number=188)
    deadline = first_now.replace(minute=1, second=30)
    result = MODULE.decide(
        "retry after 90 seconds",
        now=deadline,
        state=first.state,
        pr_number=188,
    )
    expected_deadline = datetime(2026, 7, 25, 12, 3, tzinfo=timezone.utc)
    check(result.action == MODULE.WAIT_EXTERNAL,
          "identical-text new timer at the exact old deadline did not wait")
    check(result.retry_after_seconds == 90,
          "identical-text new timer did not use its new delay")
    check(result.state.external_backoff_until == expected_deadline,
          "identical-text new timer did not replace the expired session deadline")


def test_new_timer_replaces_expired_session_deadline() -> None:
    first_now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    first = MODULE.decide("retry after 90 seconds", now=first_now, pr_number=188)
    deadline = first_now.replace(minute=1, second=30)
    result = MODULE.decide(
        "retry after 45 seconds",
        now=deadline,
        state=first.state,
        pr_number=188,
    )
    expected_deadline = datetime(2026, 7, 25, 12, 2, 15, tzinfo=timezone.utc)
    check(result.action == MODULE.WAIT_EXTERNAL,
          "new timer at the exact old deadline did not wait")
    check(result.retry_after_seconds == 45,
          "new timer at the exact old deadline was discarded")
    check(result.state.external_backoff_until == expected_deadline,
          "new timer did not replace the expired session deadline")
    check(result.state.external_backoff_timer_id != first.state.external_backoff_timer_id,
          "new timer reused the expired session timer identity")


def test_timer_retries_at_deadline() -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    deadline = datetime(2026, 7, 25, 12, 1, 30, tzinfo=timezone.utc)
    failure = MODULE.classify("retry after 90 seconds", now=now)
    result = MODULE.transition(failure, now=deadline,
                               state=MODULE.SessionState(external_backoff_until=deadline,
                                                          external_backoff_pr=188),
                               pr_number=188)
    check(result.action == MODULE.RETRY_EXTERNAL, "timer did not retry at the exact deadline")


def test_transient_retries_immediately() -> None:
    result = MODULE.decide("upstream timeout")
    check(result.kind == MODULE.TRANSIENT, "timeout was not transient")
    check(result.action == MODULE.RETRY_EXTERNAL, "transient failure did not retry")


def test_standalone_availability_timer_waits() -> None:
    result = MODULE.decide(
        "available in 90 seconds",
        now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
    )
    check(result.kind == MODULE.TIMER, "standalone availability was not classified as a timer")
    check(result.action == MODULE.WAIT_EXTERNAL, "standalone availability did not wait")
    check(result.retry_after_seconds == 90, "standalone availability delay changed")


def test_transient_wrapped_availability_timer_waits() -> None:
    result = MODULE.decide(
        "temporarily unavailable; available in 90 seconds",
        now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
    )
    check(result.kind == MODULE.TIMER,
          "transient-wrapped availability was not classified as a timer")
    check(result.action == MODULE.WAIT_EXTERNAL,
          "transient-wrapped availability retried immediately")
    check(result.retry_after_seconds == 90,
          "transient-wrapped availability delay changed")


def test_malformed_availability_timer_fails_closed() -> None:
    result = MODULE.decide(
        "temporarily unavailable; available in 90 bananas",
        now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
    )
    check(result.kind == MODULE.PERMANENT,
          "malformed availability timer was accepted")
    check(result.action == MODULE.FALLBACK_NATIVE,
          "malformed availability timer retried")
    check(result.state.external_disabled,
          "malformed availability timer did not disable the session route")


def test_permanent_marker_precedes_transient_marker() -> None:
    result = MODULE.classify("permission denied after upstream timeout")
    check(result.kind == MODULE.PERMANENT,
          "permanent marker lost to a transient marker")


def test_oversized_relative_timer_fails_closed() -> None:
    result = MODULE.classify("rate limit; retry after " + "9" * 400 + " seconds")
    check(result.kind == MODULE.PERMANENT,
          "oversized relative timer did not fail closed")


def test_malformed_timer_text_fails_closed() -> None:
    result = MODULE.classify("rate limit; retry after never seconds")
    check(result.kind == MODULE.PERMANENT,
          "malformed timer text selected an immediate retry")


def test_unknown_timer_zone_fails_closed() -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    result = MODULE.classify("rate limit; resets Jul 27, 9pm (Unknown/Zone)", now)
    check(result.kind == MODULE.PERMANENT,
          "unknown timer zone selected an immediate retry")


def test_unrepresentable_absolute_timer_disables_external_route() -> None:
    now = datetime(9999, 12, 30, 0, 0, tzinfo=timezone.utc)
    result = MODULE.decide("rate limit; resets Dec 31, 11pm (Pacific/Pago_Pago)", now=now)
    check(result.kind == MODULE.PERMANENT,
          "unrepresentable absolute deadline was not permanent")
    check(result.action == MODULE.FALLBACK_NATIVE,
          "unrepresentable absolute deadline did not fall back")
    check(result.state.external_disabled,
          "unrepresentable absolute deadline did not disable the session route")


def test_fractional_timer_fails_closed() -> None:
    result = MODULE.classify("rate limit; retry after 1.5 seconds")
    check(result.kind == MODULE.PERMANENT,
          "fractional timer was rounded into a retry delay")


def test_numeric_transient_marker_requires_complete_token() -> None:
    result = MODULE.decide(
        "opaque provider error 1429",
        now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
    )
    check(result.kind == MODULE.PERMANENT,
          "numeric substring was classified as transient")
    check(result.action == MODULE.FALLBACK_NATIVE,
          "numeric substring selected an external retry")
    check(result.state.external_disabled,
          "numeric substring did not disable the session route")


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


def test_malformed_typed_timer_fails_closed() -> None:
    deadline = datetime(2026, 7, 25, 12, 1, 30, tzinfo=timezone.utc)
    failure = MODULE.Failure(MODULE.TIMER, -1, deadline, "malformed timer")
    result = MODULE.transition(failure, now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc))
    check(result.kind == MODULE.PERMANENT, "malformed typed timer was not normalized")
    check(result.action == MODULE.FALLBACK_NATIVE, "malformed typed timer was retried")
    check(result.state.external_disabled, "malformed typed timer did not disable the session route")


def test_malformed_typed_state_fails_closed() -> None:
    state = MODULE.SessionState(external_disabled="false", external_backoff_until="not-a-timestamp")
    failure = MODULE.Failure(MODULE.TRANSIENT, None, None, "temporary failure")
    result = MODULE.transition(
        failure,
        now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
        state=state,
        pr_number=188,
    )
    check(result.kind == MODULE.PERMANENT, "malformed typed state was not normalized")
    check(result.action == MODULE.FALLBACK_NATIVE, "malformed typed state was retried")
    check(result.state.external_disabled, "malformed typed state did not disable the session route")


def test_disabled_state_is_session_only_and_blocks_new_launches() -> None:
    state = MODULE.SessionState(external_disabled=True)
    result = MODULE.decide("temporary failure", now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
                           state=state, pr_number=188)
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


def test_active_timer_without_pr_fails_closed() -> None:
    first_now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    first = MODULE.decide("retry after 90 seconds", now=first_now, pr_number=188)
    result = MODULE.decide(
        "retry after 2 minutes",
        now=first_now.replace(second=10),
        state=first.state,
    )
    check(result.kind == MODULE.PERMANENT,
          "session transition without a PR number was not malformed")
    check(result.action == MODULE.FALLBACK_NATIVE,
          "session transition without a PR number did not fall back natively")
    check(result.external_disabled,
          "session transition without a PR number did not disable the external route")
    check(result.state.external_backoff_pr == 188,
          "malformed session transition lost the existing timer owner")


def test_zero_delay_timer_retries_immediately() -> None:
    result = MODULE.decide("retry-after: 0", now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc))
    check(result.kind == MODULE.TIMER and result.action == MODULE.RETRY_EXTERNAL,
          "zero-delay timer was not an immediate retry")


CASES = [
    ("absolute-provider-timezone", "absolute reset uses provider timezone", test_absolute_timer_uses_provider_timezone),
    ("absolute-dst-elapsed-time", "absolute deadline uses elapsed time across DST", test_absolute_timer_uses_elapsed_time_across_dst),
    ("timer-wait-dst-elapsed-time", "timer wait uses elapsed time across DST", test_timer_wait_uses_elapsed_time_across_dst),
    ("absolute-dst-gap", "DST gap fails closed", test_absolute_timer_dst_gap_fails_closed),
    ("absolute-dst-fold", "DST fold fails closed without an offset", test_absolute_timer_dst_fold_fails_closed),
    ("absolute-dst-explicit-offset", "explicit offset resolves a DST fold", test_absolute_timer_explicit_offset_resolves_dst_fold),
    ("absolute-dst-both-explicit-offsets", "both valid DST fold offsets are accepted", test_absolute_timer_explicit_offset_resolves_both_dst_fold_offsets),
    ("absolute-contradictory-offset", "contradictory named-zone offsets fail closed", test_absolute_timer_rejects_contradictory_named_zone_offset),
    ("relative-exact-wait", "relative delay is preserved exactly", test_relative_timer_waits_exactly),
    ("unitless-relative-timer", "unitless timers default to seconds", test_unitless_relative_timer_defaults_to_seconds),
    ("comma-grouped-relative-timer", "comma-grouped timers fail closed", test_comma_grouped_relative_timer_fails_closed),
    ("unsupported-relative-unit", "unsupported timer units fail closed", test_unsupported_relative_timer_unit_fails_closed),
    ("unsupported-relative-punctuation", "unsupported timer punctuation fails closed", test_unsupported_relative_timer_punctuation_fails_closed),
    ("incomplete-absolute-reset", "incomplete absolute reset fails closed", test_incomplete_absolute_reset_fails_closed),
    ("absolute-timer-suffix", "absolute timer suffix fails closed", test_absolute_timer_suffix_fails_closed),
    ("malformed-before-valid-timer", "malformed timer before valid timer fails closed", test_malformed_timer_before_valid_timer_fails_closed),
    ("malformed-reset-timer", "malformed reset timer fails closed", test_malformed_reset_timer_fails_closed),
    ("session-deadline-reentry", "active session deadline is preserved on re-entry", test_session_timer_deadline_is_not_extended_on_reentry),
    ("active-identical-timer", "active identical fresh timer replaces deadline", test_active_identical_new_timer_replaces_deadline),
    ("active-later-deadline", "active later timer replaces deadline and identity", test_active_later_timer_replaces_deadline_and_identity),
    ("active-earlier-same-pr", "active earlier timer replaces deadline for the same PR", test_active_earlier_timer_replaces_deadline_for_same_pr),
    ("active-timer-other-pr", "active timer for another PR falls back and retains ownership", test_active_timer_for_another_pr_falls_back_and_retains_owner),
    ("expired-timer-other-pr", "expired timer for another PR replaces deadline and waits", test_expired_timer_for_another_pr_replaces_deadline_and_waits),
    ("session-deadline-exact-reentry", "exact deadline re-entry retries without extension", test_session_timer_retries_at_exact_reentry_deadline),
    ("cli-typed-reentry", "CLI preserves typed timer re-entry", test_cli_preserves_typed_timer_across_reentry),
    ("cli-invalid-utf8", "CLI invalid UTF-8 message file falls back", test_cli_invalid_utf8_message_file_falls_back),
    ("expired-deadline-identical-timer", "identical-text new timer replaces an expired session deadline", test_identical_text_new_timer_replaces_expired_session_deadline),
    ("expired-deadline-new-timer", "new timer replaces an expired session deadline", test_new_timer_replaces_expired_session_deadline),
    ("timer-deadline-retry", "timer retries at its exact deadline", test_timer_retries_at_deadline),
    ("transient-immediate-retry", "transient failure retries immediately", test_transient_retries_immediately),
    ("standalone-availability", "standalone availability timer waits", test_standalone_availability_timer_waits),
    ("wrapped-availability", "transient-wrapped availability timer waits", test_transient_wrapped_availability_timer_waits),
    ("malformed-availability", "malformed availability timer fails closed", test_malformed_availability_timer_fails_closed),
    ("permanent-marker-precedence", "permanent marker precedes transient marker", test_permanent_marker_precedes_transient_marker),
    ("oversized-relative-timer", "oversized timer fails closed", test_oversized_relative_timer_fails_closed),
    ("malformed-timer", "malformed timer fails closed", test_malformed_timer_text_fails_closed),
    ("unknown-timer-zone", "unknown timer zone fails closed", test_unknown_timer_zone_fails_closed),
    ("unrepresentable-absolute-timer", "unrepresentable absolute timer disables the session route", test_unrepresentable_absolute_timer_disables_external_route),
    ("fractional-timer", "fractional timer fails closed", test_fractional_timer_fails_closed),
    ("numeric-marker-token", "numeric transient markers require complete tokens", test_numeric_transient_marker_requires_complete_token),
    ("permanent-session-disable", "permanent failure disables only this session", test_permanent_disables_external_route_for_session),
    ("unknown-permanent", "unknown failure fails closed", test_unknown_fails_closed_as_permanent),
    ("unknown-typed-permanent", "unknown typed failure fails closed", test_unrecognized_typed_failure_fails_closed),
    ("malformed-typed-timer", "malformed typed timer fails closed", test_malformed_typed_timer_fails_closed),
    ("malformed-typed-state", "malformed typed state fails closed", test_malformed_typed_state_fails_closed),
    ("session-state", "disabled state blocks later launches in this session", test_disabled_state_is_session_only_and_blocks_new_launches),
    ("spent-timer-fallback", "spent timer falls back and retains session backoff", test_timer_after_retry_falls_back_but_keeps_timer),
    ("active-timer-missing-pr", "active timer without a PR number fails closed", test_active_timer_without_pr_fails_closed),
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
