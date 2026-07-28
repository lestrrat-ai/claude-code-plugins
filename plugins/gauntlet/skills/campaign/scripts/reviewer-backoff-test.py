#!/usr/bin/env python3
# ci: pyright
"""Fixtures for `reviewer-backoff.py` — the rough external-reviewer backoff guess.

They live in a SIBLING file, and `reviewer-backoff.py self-test` FAILS LOUDLY if it cannot load them.

EVERY FIXTURE PINS A RULE WITH TEETH: it asserts the outcome on one side of a boundary AND the
opposite outcome on the other, so an implementation that returned a constant would go red.

The suite's own shape enforces the design's central promise. `t_no_message_disables_the_route` runs
every fixture message plus the ten real provider messages that defeated the predecessor's exact
parser, and requires that not one of them ends the external route beyond the current pass. That is
the rule the old design broke, so it is checked over the whole corpus rather than case by case.
"""

from __future__ import annotations

import json
from pathlib import Path

from _gauntlet.modules import load_module_from_path
from _gauntlet.testing import capture_cli

OWNER = Path(__file__).resolve().parent / "reviewer-backoff.py"


def _load_owner():
    # `register=True`: the owner's frozen dataclasses resolve their own annotations through
    # `sys.modules`, so an unregistered module cannot build them.
    module = load_module_from_path("reviewer_backoff_owner", OWNER, register=True)
    if module is None:
        raise RuntimeError(f"cannot load the reviewer-backoff owner at {OWNER}")
    return module


B = _load_owner()


class FixtureFailure(AssertionError):
    """One pinned rule no longer holds."""


def check(condition: object, message: str) -> None:
    if not condition:
        raise FixtureFailure(message)


def action(message: object, attempts_spent: object = 0) -> str:
    return B.decide(message, attempts_spent).action


def wait(message: object, attempts_spent: object = 0) -> int:
    return B.decide(message, attempts_spent).wait_seconds


def kind(message: object) -> str:
    return B.classify(message).kind


# The ten real provider messages that defeated the predecessor's exact-parsing classifier. Each is
# paired with the action and wait this design gives it, so a regression in either is visible.
PROVIDER_MESSAGES = (
    ("retry after 60 seconds (attempt 2 of 3)", B.WAIT_EXTERNAL, 60),
    ("retry after 1 minute 30 seconds", B.WAIT_EXTERNAL, 60),
    ("usage limit reached; resets at 3pm", B.WAIT_EXTERNAL, 300),
    ("your limit resets in 2 hours 30 minutes", B.FALLBACK_NATIVE, 0),
    ("retry after ~60 seconds", B.WAIT_EXTERNAL, 60),
    ("retry in about 60 seconds", B.WAIT_EXTERNAL, 60),
    ("you have exceeded your rate limit. try again at 1:30pm.", B.WAIT_EXTERNAL, 300),
    ("please wait 30-60 seconds before retrying", B.WAIT_EXTERNAL, 60),
    ("rate limit exceeded, retry after 2026-07-25T13:00:00Z", B.WAIT_EXTERNAL, 300),
    ("codex: stream disconnected before completion; retrying 1/5 in 214 ms",
     B.WAIT_EXTERNAL, B.MIN_WAIT_SECONDS),
)

OTHER_MESSAGES = (
    "",
    "upstream timeout",
    "HTTP 503 from the provider",
    "authentication failed: invalid api key",
    "codex: command not found",
    "usage: codex exec [OPTIONS] [PROMPT]",
    "token usage: 41234 of 200000",
    "the connection was refused by the upstream proxy",
    "ECONNREFUSED 127.0.0.1:443",
    "opaque provider failure",
    "reviewed 40 files in 12 seconds and then died",
)


# --- the promise the predecessor broke ---------------------------------------

def t_no_message_disables_the_route() -> None:
    # The whole corpus, in one assertion: an unreadable or hostile message costs at most this pass's
    # external attempts. There is no session-wide disable to reach, and the returned action set is
    # closed, so no message can invent one.
    allowed = {B.WAIT_EXTERNAL, B.FALLBACK_NATIVE, B.STOP_AND_ASK}
    corpus = [text for text, _action, _wait in PROVIDER_MESSAGES] + list(OTHER_MESSAGES)
    for text in corpus:
        for spent in (0, 1, 2):
            decision = B.decide(text, spent)
            check(decision.action in allowed,
                  f"{text!r} at {spent} attempts produced the unknown action {decision.action!r}")
            check(decision.attempts_cap == B.MAX_EXTERNAL_ATTEMPTS,
                  f"{text!r} reported a cap other than {B.MAX_EXTERNAL_ATTEMPTS}")
    # And the fields a durable disable would need do not exist at all.
    for field in ("external_disabled", "retry_at", "external_backoff_until"):
        check(not hasattr(B.decide("upstream timeout", 0), field),
              f"the decision carries {field!r} — session-disabling state is back")


def t_ten_provider_messages_land_where_documented() -> None:
    for text, expected_action, expected_wait in PROVIDER_MESSAGES:
        decision = B.decide(text, 0)
        check(decision.action == expected_action,
              f"{text!r} gave {decision.action!r}, not {expected_action!r}")
        check(decision.wait_seconds == expected_wait,
              f"{text!r} waited {decision.wait_seconds}s, not {expected_wait}s")
        check(decision.action != B.STOP_AND_ASK,
              f"{text!r} stopped the campaign for a delay message")


# --- refusal: the one failure that never recovers on its own ------------------

def t_refusal_stops_and_asks() -> None:
    for text in ("I'm sorry, but I can't help with that.",
                 "I am unable to assist with this request.",
                 "This request violates the content policy.",
                 "I refused to review this diff."):
        check(action(text) == B.STOP_AND_ASK, f"a reviewer refusal ({text!r}) did not stop and ask")
    # A spent retry budget must not downgrade a refusal into a same-engine native pass.
    check(action("I'm sorry, but I can't help with that.", B.MAX_EXTERNAL_ATTEMPTS)
          == B.STOP_AND_ASK, "a spent budget downgraded a refusal to native fallback")
    # A refusal that ends with the protocol's own deferral line is still a refusal. The prompt ASKS a
    # stopping reviewer for that line, so `reviewer.md` routes it to this helper before any retry.
    check(action("This request violates the content policy.\n"
                 "VERDICT: DEFERRED — cannot review this request.") == B.STOP_AND_ASK,
          "a refusal carrying a terminal DEFERRED line stopped reaching the operator")
    # And refusal outranks a transient marker in the same text.
    mixed = "the request timed out, and I cannot help with that anyway"
    check(kind(mixed) == B.REFUSAL, "a transient marker outranked a refusal in the same message")
    check(action(mixed) == B.STOP_AND_ASK, "a mixed refusal did not stop and ask")


def t_refused_to_needs_the_infinitive() -> None:
    # `refused to` / `refusal to` and not the bare stems: transport wording, in either the verb or
    # the noun spelling, must not manufacture a stop-and-ask.
    for text in ("the connection was refused by the upstream proxy",
                 "ECONNREFUSED 127.0.0.1:443",
                 "the merge was refused by the branch ruleset",
                 "connection refusal by the upstream"):
        check(kind(text) != B.REFUSAL, f"transport wording {text!r} was read as a reviewer refusal")
        check(action(text) != B.STOP_AND_ASK,
              f"transport wording {text!r} stopped the campaign for the operator")
    check(kind("the reviewer refused to continue") == B.REFUSAL,
          "the agent sense of `refused to` stopped being a refusal")
    check(kind("refusal to review this diff on policy grounds") == B.REFUSAL,
          "the agent sense of `refusal to` stopped being a refusal")


# --- the classes act differently ---------------------------------------------

def t_cannot_run_classes_skip_the_wait() -> None:
    for text, expected in (("codex: command not found", B.NOT_FOUND),
                           ("bash: no such file or directory", B.NOT_FOUND),
                           ("error: unknown option `--nope`", B.NOT_FOUND),
                           ("authentication failed: invalid api key", B.AUTH),
                           ("401 unauthorized", B.AUTH),
                           ("permission denied", B.AUTH)):
        check(kind(text) == expected, f"{text!r} classified {kind(text)!r}, not {expected!r}")
        check(action(text) == B.FALLBACK_NATIVE, f"{text!r} did not fall back immediately")
        check(wait(text) == 0, f"{text!r} scheduled a wait a retry could never use")


def t_usage_limit_and_transient_defaults_differ() -> None:
    # Different markers, different guesses: a usage limit is minutes, a network blip is seconds.
    check(wait("usage limit reached") == B.DEFAULT_WAIT_SECONDS[B.USAGE_LIMIT],
          "an unreadable usage limit did not take the usage-limit default")
    check(wait("upstream timeout") == B.DEFAULT_WAIT_SECONDS[B.TRANSIENT],
          "an unreadable transient failure did not take the transient default")
    check(wait("opaque provider failure") == B.DEFAULT_WAIT_SECONDS[B.UNKNOWN],
          "an unreadable unknown failure did not take the unknown default")
    check(B.DEFAULT_WAIT_SECONDS[B.USAGE_LIMIT] != B.DEFAULT_WAIT_SECONDS[B.TRANSIENT],
          "the usage-limit and transient defaults collapsed into one value")
    for value in B.DEFAULT_WAIT_SECONDS.values():
        check(value <= B.MAX_WAIT_SECONDS, "a class default exceeds the wait cap it must stay under")


def t_line_anchored_usage_banner() -> None:
    # `usage:` opening a line is a CLI help dump; mid-line it is telemetry prose.
    check(kind("usage: codex exec [OPTIONS] [PROMPT]") == B.NOT_FOUND,
          "a CLI help dump was not read as an unrunnable tool")
    check(kind("token usage: 41234 of 200000") != B.NOT_FOUND,
          "telemetry prose `token usage:` was read as a CLI help dump")
    check(kind("  usage: codex\nmemory usage: 82%") == B.NOT_FOUND,
          "an indented help banner on a later line stopped matching")
    # …and the banner is the WEAK half of not-found: alongside a real limit message, the limit wins,
    # so the case this helper exists for keeps its wait and its one external retry.
    riding_along = "usage: rate limit exceeded; retry after 60 seconds"
    check(kind(riding_along) == B.USAGE_LIMIT,
          "a banner riding along with a real limit message claimed not-found")
    check(action(riding_along) == B.WAIT_EXTERNAL,
          "a banner riding along with a real limit message dropped the wait and fell back")


# --- the guess is approximate, and unreadable is normal -----------------------

def t_delay_needs_a_retry_word_nearby() -> None:
    check(B.guess_delay_seconds("retry after 60 seconds") == 60,
          "a delay next to a retry word was not read")
    check(B.guess_delay_seconds("reviewed 40 files in 12 seconds") is None,
          "a number with no retry word near it was read as a delay")
    far = "retry" + " padding" * 12 + " 60 seconds"
    check(B.guess_delay_seconds(far) is None,
          "a number far past the trigger window was still read as that trigger's delay")
    # The trigger must START a word, not merely sit inside one: telemetry that only CONTAINS the
    # letters of a retry word supplies no timer, and `unavailable` is not `available`.
    for text in ("retrieved 999 files in 3 seconds",
                 "retrieval finished in 3 hours",
                 "service unavailable after 12 seconds"):
        check(B.guess_delay_seconds(text) is None,
              f"telemetry {text!r} supplied a delay from a word that merely contains a retry word")


def t_unreadable_delay_shapes_are_not_errors() -> None:
    for text in ("retry after 2026-07-25T13:00:00Z",
                 "retry at 3pm",
                 "try again at 1:30pm",
                 "retry after 1.5 seconds",
                 "retry after a while",
                 "retry after 90 bananas",
                 "retry after seconds.5"):
        check(B.guess_delay_seconds(text) is None, f"{text!r} produced a delay it cannot support")
        check(action(text) == B.WAIT_EXTERNAL,
              f"{text!r} was punished for being unreadable instead of taking the default")
        check(wait(text) == B.DEFAULT_WAIT_SECONDS[kind(text)],
              f"{text!r} did not take its class default")


def t_first_readable_pair_wins() -> None:
    check(B.guess_delay_seconds("retry after 1 minute 30 seconds") == 60,
          "a compound delay stopped guessing from its first readable pair")
    check(B.guess_delay_seconds("please wait 30-60 seconds") == 60,
          "a range stopped guessing from its first fully readable pair")
    check(B.guess_delay_seconds("retry after ~60 seconds") == 60,
          "a tilde-prefixed delay stopped being readable")
    check(B.guess_delay_seconds("retrying in 214 ms") == 1,
          "a sub-second delay stopped being readable")
    check(B.guess_delay_seconds("retry after 2 hours") == 7200,
          "an hours delay stopped being readable")
    # …and telemetry riding along inside a real limit message must not beat the provider's own
    # delay to the first-pair rule, which would relaunch seconds into a 45-minute limit and burn
    # the pass's last external attempt.
    hijack = ("usage limit reached: retrieved 12 files in 2 seconds before the cap; "
              "try again in 45 minutes")
    check(B.guess_delay_seconds(hijack) == 2700,
          "telemetry beat the provider's real delay to the first readable pair")
    check(action(hijack) == B.FALLBACK_NATIVE and wait(hijack) != B.MIN_WAIT_SECONDS,
          "a 45-minute limit relaunched the external reviewer instead of reviewing natively")


def t_wait_is_clamped_at_both_ends() -> None:
    check(wait("retrying in 214 ms") == B.MIN_WAIT_SECONDS,
          "a sub-second hint produced a spin instead of the floor")
    check(wait("retry after 1 second") == B.MIN_WAIT_SECONDS,
          "a 1s hint produced a spin instead of the floor")
    over = f"retry after {B.MAX_WAIT_SECONDS + 1} seconds"
    check(action(over) == B.FALLBACK_NATIVE,
          "a delay past the cap stalled the pass instead of reviewing natively")
    under = f"retry after {B.MAX_WAIT_SECONDS} seconds"
    check(action(under) == B.WAIT_EXTERNAL and wait(under) == B.MAX_WAIT_SECONDS,
          "a delay exactly at the cap stopped being waitable")
    # Width is not a second, silent cap. A stated over-cap delay must fall back at EVERY width — the
    # widest run the guess converts, one digit past it, and a run past CPython's int-conversion
    # limit, which must answer rather than raise.
    widest = "retry after " + "9" * B.MAX_READABLE_DIGITS + " seconds"
    check(action(widest) == B.FALLBACK_NATIVE,
          "the widest convertible over-cap delay stopped reviewing natively")
    over_width = "retry after 1" + "0" * B.MAX_READABLE_DIGITS + " seconds"
    check(action(over_width) == B.FALLBACK_NATIVE,
          "one digit past the convertible width read as unreadable instead of over-cap")
    unbounded = "retry after " + "9" * 5000 + " seconds"
    check(action(unbounded) == B.FALLBACK_NATIVE,
          "an unbounded digit run stopped reviewing natively")


# --- the cap ------------------------------------------------------------------

def t_attempts_are_capped() -> None:
    text = "usage limit reached; retry after 30 seconds"
    for spent in range(B.MAX_EXTERNAL_ATTEMPTS):
        check(action(text, spent) == B.WAIT_EXTERNAL,
              f"a retryable failure with {spent} attempts spent stopped waiting")
    check(action(text, B.MAX_EXTERNAL_ATTEMPTS) == B.FALLBACK_NATIVE,
          "the external route kept retrying past its cap")
    check(action(text, B.MAX_EXTERNAL_ATTEMPTS + 5) == B.FALLBACK_NATIVE,
          "an over-cap attempt count kept retrying")
    check(B.MAX_EXTERNAL_ATTEMPTS == 2,
          "the cap drifted from the attempt budget runtime-adapter.md allocates (1 external, "
          "1 external retry, then native attempt 3)")


def t_malformed_attempt_count_is_exhausted() -> None:
    # A caller bug costs one native pass, never an unbounded retry loop.
    for spent in (-1, "2", 1.5, None, True):
        check(action("upstream timeout", spent) == B.FALLBACK_NATIVE,
              f"a malformed attempts_spent ({spent!r}) was treated as a fresh budget")
    check(action("upstream timeout", 0) == B.WAIT_EXTERNAL,
          "a valid zero attempt count was mistaken for a malformed one")


def t_non_text_input_is_unknown_not_a_verdict() -> None:
    for message in (None, b"bytes", 17, ""):
        check(kind(message) == B.UNKNOWN, f"{message!r} was classified instead of left unknown")
        check(action(message) == B.WAIT_EXTERNAL,
              f"{message!r} escalated instead of taking the unknown default")


# --- CLI ----------------------------------------------------------------------

def t_cli_reports_the_decision_as_json() -> None:
    code, out, _err = capture_cli(B.main, ["decide", "--message", "usage limit reached",
                                           "--attempts-spent", "1"])
    check(code == 0, f"`decide` exited {code}")
    payload = json.loads(out)
    check(payload["action"] == B.WAIT_EXTERNAL, "the CLI lost the wait action")
    check(payload["wait_seconds"] == B.DEFAULT_WAIT_SECONDS[B.USAGE_LIMIT],
          "the CLI lost the usage-limit default wait")
    check(payload["attempts_spent"] == 1, "the CLI lost the attempt count it was given")
    code, out, _err = capture_cli(B.main, ["classify", "--message", "codex: command not found"])
    check(code == 0 and json.loads(out)["kind"] == B.NOT_FOUND, "the CLI lost the classify result")
    code, _out, err = capture_cli(B.main, ["decide", "--message", "x", "--message-file", "y"])
    check(code != 0 and "exactly one" in err, "the CLI accepted two message sources")
    code, _out, err = capture_cli(B.main, ["decide"])
    check(code != 0 and "exactly one" in err, "the CLI accepted no message source at all")


CASES = [
    ("no-route-disable", "no message can disable the external route beyond this pass",
     t_no_message_disables_the_route),
    ("provider-messages", "the ten real provider messages land where documented",
     t_ten_provider_messages_land_where_documented),
    ("refusal-stops", "a reviewer refusal reaches the operator, never a same-engine fallback",
     t_refusal_stops_and_asks),
    ("refused-to-anchor",
     "`refused to` / `refusal to` need their infinitive, so transport wording is not a refusal",
     t_refused_to_needs_the_infinitive),
    ("cannot-run", "an unrunnable tool or a bad credential falls back without waiting",
     t_cannot_run_classes_skip_the_wait),
    ("class-defaults", "each marker class guesses its own default delay",
     t_usage_limit_and_transient_defaults_differ),
    ("usage-banner", "`usage:` claims a line-leading help dump, never telemetry prose",
     t_line_anchored_usage_banner),
    ("delay-proximity", "a number is a delay only near a retry word", t_delay_needs_a_retry_word_nearby),
    ("unreadable-delay", "unreadable delay text takes the default instead of escalating",
     t_unreadable_delay_shapes_are_not_errors),
    ("first-pair", "the guess reads the first readable number-and-unit pair", t_first_readable_pair_wins),
    ("wait-clamped", "the wait has a floor, and past the cap the pass reviews natively",
     t_wait_is_clamped_at_both_ends),
    ("attempt-cap", "the external route retries a fixed, capped number of times", t_attempts_are_capped),
    ("malformed-count", "a malformed attempt count reads as exhausted", t_malformed_attempt_count_is_exhausted),
    ("non-text", "non-text input is unknown, not a verdict", t_non_text_input_is_unknown_not_a_verdict),
    ("cli", "the CLI reports the decision as JSON and refuses ambiguous input",
     t_cli_reports_the_decision_as_json),
]
