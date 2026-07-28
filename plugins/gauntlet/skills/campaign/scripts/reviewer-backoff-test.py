#!/usr/bin/env python3
# ci: pyright
"""Fixtures for `reviewer-backoff.py` — the rough external-reviewer backoff guess.

They live in a SIBLING file, and `reviewer-backoff.py self-test` FAILS LOUDLY if it cannot load them.

EVERY FIXTURE PINS A RULE WITH TEETH: it asserts the outcome on one side of a boundary AND the
opposite outcome on the other, so an implementation that returned a constant would go red.

The suite's own shape enforces the design's central promise. `t_no_message_disables_the_route` runs
BOTH corpora this file declares — the ten real provider messages that defeated the predecessor's
exact parser, and the other shapes these fixtures pin — and requires that not one of them ends the
external route beyond the current pass. That is the rule the old design broke, so it is checked over
a corpus rather than case by case. Add a message shape to a fixture, add it to a corpus too.
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
    # The shapes that reached the wrong class before the matching layer was made uniform. They are
    # listed here as well as in their own fixtures so the route-disable promise below covers them.
    "provider response is unquotable",
    "bash: unterminated quotation mark in the prompt",
    "I’m sorry, but I can’t help with that.",
    "I am sorry, but I can't\nhelp with that request.",
    "retry after -2 hours",
    "retry after 1,800 seconds",
    # The Unicode spellings of those same shapes, and the two word anchors that were a version apart.
    "I canʻt help with this review",
    "quota\u0301tion is ordinary prose",
    "retry after −2 seconds",
    "retry after 1٫5 seconds",
    "The waiter was quoted at 900 seconds",
    "resettlement of 300 seconds of logs",
    "the error is retryable; 800 seconds of telemetry followed",
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
    # The plural spelling of a policy marker is the same refusal. These three are the only markers
    # whose plural is not a superstring of the singular, so each is pinned.
    for text in ("Your request is forbidden by our content policies.",
                 "blocked by our safety policies",
                 "your prompt violates our usage policies"):
        check(action(text) == B.STOP_AND_ASK,
              f"a policy refusal in the plural ({text!r}) did not reach the operator")
    # …and the other side of that boundary: an over-broad policy marker must not swallow the auth
    # class, so a bare credential failure with no policy wording still falls back natively.
    check(kind("403 Forbidden") == B.AUTH,
          "a bare credential failure was read as a reviewer refusal")
    check(action("403 Forbidden") == B.FALLBACK_NATIVE,
          "a bare credential failure stopped the campaign for the operator")
    # And refusal outranks a transient marker in the same text.
    mixed = "the request timed out, and I cannot help with that anyway"
    check(kind(mixed) == B.REFUSAL, "a transient marker outranked a refusal in the same message")
    check(action(mixed) == B.STOP_AND_ASK, "a mixed refusal did not stop and ask")
    # …but only a marker that carries a refusal SENSE may outrank. A bare component noun does not:
    # status prose about a filtering service reports infrastructure, so the transient marker in the
    # same text wins and the pass waits and retries instead of parking the PR for the operator.
    status = "content filter service temporarily unavailable"
    check(kind(status) == B.TRANSIENT,
          "infrastructure status prose naming a filtering component was read as a refusal")
    check(action(status) == B.WAIT_EXTERNAL,
          "infrastructure status prose stopped the campaign for the operator")
    # The disclosed trade of dropping that component noun, pinned so re-adding it goes red: a
    # refusal phrased ONLY as the component's name is no longer recognised. It takes the safe
    # `unknown` landing — a wait, then the bounded native fallback — never a manufactured stop.
    only_component = "blocked by the content filter"
    check(kind(only_component) == B.UNKNOWN,
          "the component noun is back as a refusal marker; status prose parks PRs again")
    check(action(only_component) != B.STOP_AND_ASK,
          "an unrecognised refusal shape escalated instead of taking the safe unknown landing")


def t_a_marker_matches_whole_words_only() -> None:
    # The anchor is UNIFORM: every marker matches as a whole word, so none of them can claim a class
    # from inside an unrelated word. Both of these read `usage-limit` when `quota` matched as a bare
    # substring — one waiting five minutes for a limit nobody hit.
    for text in ("provider response is unquotable",
                 "bash: unterminated quotation mark in the prompt"):
        check(kind(text) == B.UNKNOWN,
              f"{text!r} claimed a class from a marker buried inside an unrelated word")
        check(wait(text) == B.DEFAULT_WAIT_SECONDS[B.UNKNOWN],
              f"{text!r} took another class's default delay")
    # …and the other side of that boundary: the anchor must not cost the plain and PLURAL wordings a
    # provider actually uses. A trailing `\b` right after the marker loses every one of these, which
    # is why `_marker_pattern` admits one trailing `s` inside the anchor.
    for text, expected in (("quota exceeded for this org", B.USAGE_LIMIT),
                           ("monthly quotas exceeded for this org", B.USAGE_LIMIT),
                           ("you have exceeded your rate limits", B.USAGE_LIMIT),
                           ("your limit resets in 2 hours 30 minutes", B.USAGE_LIMIT),
                           ("two connection resets in a row", B.TRANSIENT),
                           ("error: unknown options `--nope` `--nah`", B.NOT_FOUND)):
        check(kind(text) == expected, f"{text!r} classified {kind(text)!r}, not {expected!r}")
    # The `-ies` plural is the shape the trailing `s` does NOT cover, so those markers keep their own
    # entry. Both spellings must still reach the operator.
    for text in ("This request violates the content policy.",
                 "Your request is forbidden by our content policies."):
        check(kind(text) == B.REFUSAL, f"a policy refusal ({text!r}) stopped being a refusal")
    # The SAME rule beyond ASCII, which is where the anchor alone did not carry it. A combining mark
    # continues the word its base letter starts, but it is not `\w`, so an unfolded one SATISFIED the
    # closing anchor instead of shutting it: `quota` + U+0301 + `tion` is one ordinary word that
    # claimed a usage limit and waited five minutes for a limit nobody hit. The ASCII spellings of
    # that hazard are pinned above; a rule that holds in ASCII and not in Unicode is the defect, so
    # the mark spellings are pinned beside them — including the one at end of text, where there is no
    # following letter to close the anchor at all.
    #
    # Every mark below is written as an ESCAPE rather than as the character, on purpose. A decomposed
    # mark is invisible in a diff, and one NFC pass over this file — an editor setting, a formatter —
    # rewrites `quota` + U+0301 + `tion` into a precomposed `á`, which is a DIFFERENT string that
    # passes these checks without exercising anything. An escape cannot be normalized away.
    for text in ("quota\u0301tion is ordinary prose",
                 "hit the quota\u0301"):
        check(kind(text) == B.UNKNOWN,
              f"{text!r} claimed a class from a marker a combining mark had glued into a word")
        check(wait(text) == B.DEFAULT_WAIT_SECONDS[B.UNKNOWN],
              f"{text!r} took another class's default delay")
    # …and the other side of that boundary, twice: folding a mark must not LOSE a marker that really
    # is a whole word, and it must not GLUE one together where none was written.
    check(kind("quota exceeded for thi\u0301s org") == B.USAGE_LIMIT,
          "a combining mark elsewhere in the message cost a marker standing as its own word")
    check(kind("I can't help with thi\u0301s review") == B.REFUSAL,
          "a combining mark elsewhere in the message lost a genuine refusal")
    check(kind("token usage\u0301 limit of 200000") != B.USAGE_LIMIT,
          "folding a mark manufactured a marker out of two words that were never one")


def t_a_refusal_survives_typography_and_wrapping() -> None:
    # A refusal that fails to reach the operator is the WORST outcome this file has: it falls back to
    # the orchestrator's own engine and silently drops the gate's engine diversity. So one refusal is
    # pinned in every spelling a provider actually emits it in.
    #
    # The apostrophe variants, none of which NFKC would fold to ASCII:
    for text in ("I’m sorry, but I can’t help with that.",       # U+2019
                 "I‘m sorry, but I can‘t help with that.",       # U+2018
                 "I canʼt help with that.",                      # U+02BC
                 "I canʹt help with that.",                      # U+02B9
                 "I can＇t help with that.",                      # U+FF07
                 "I can′t help with that.",                      # U+2032
                 "I can´t help with that.",                      # U+00B4
                 "I can`t help with that.",                      # U+0060
                 "I can't help with that.",                      # the ASCII original
                 # …and the wrapping captured stderr does to any of them at the terminal width.
                 "I am sorry, but I can't\nhelp with that request.",
                 "I can't  help with that.",
                 "I can’t\thelp with that.",
                 "I can’t\n  help with that."):
        check(kind(text) == B.REFUSAL, f"a genuine refusal ({text!r}) was lost to {kind(text)!r}")
        check(action(text) == B.STOP_AND_ASK,
              f"a genuine refusal ({text!r}) fell back to a same-engine reviewer")
    # …and the other side of that boundary, twice over. Whitespace inside a marker absorbs a WRAP,
    # never arbitrary words: the marker's words must still be adjacent.
    for text in ("I can't decide whether to help with that",
                 "I can't, on reflection, help with that"):
        check(kind(text) != B.REFUSAL,
              f"{text!r} matched a marker across words that are not adjacent")
    # The MODIFIER LETTER range U+02B9-U+02BF is folded WHOLE, and this loop is mechanical for that
    # reason: a second hand-written list is what went one member short the first time. `ʹ` (U+02B9)
    # and `ʼ` (U+02BC) were folded while `ʻ` (U+02BB) sitting between them was not, so a genuine
    # `I canʻt help with this review` classified `unknown`, waited, and was then answered by this
    # campaign's own engine. Every member of the range is a LETTER to `\w`, so an unfolded one glues
    # `can` and `t` into a single word that `can't help` can never match, at any attempt count.
    for code in range(0x02B9, 0x02C0):
        spelling = f"I can{chr(code)}t help with this review"
        check(kind(spelling) == B.REFUSAL,
              f"a refusal spelled with U+{code:04X} was lost to {kind(spelling)!r}")
        # Both ends of the retry budget: a spent budget is what turns a lost refusal into the
        # same-engine native pass, so the fresh count alone would not pin the outcome that matters.
        for spent in (0, B.MAX_EXTERNAL_ATTEMPTS):
            check(action(spelling, spent) == B.STOP_AND_ASK,
                  f"a refusal spelled with U+{code:04X} did not reach the operator at {spent} "
                  f"attempts spent")
    # And folding an apostrophe manufactures no marker where none was written.
    check(kind("the model can t help itself to more tokens") != B.REFUSAL,
          "a message with no apostrophe at all was folded into a refusal")


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
    # `decline` takes the same anchor for the same reason: the bare stem reads a REQUEST that was
    # declined — a credential, billing, or gateway outcome — as an agent refusing the task. A
    # credential failure must reach its own class and fall back natively, not park the PR.
    credential = "authentication failed: your request was declined"
    check(kind(credential) == B.AUTH,
          "a credential failure carrying `declined` was read as a reviewer refusal")
    check(action(credential) == B.FALLBACK_NATIVE,
          "a credential failure carrying `declined` stopped the campaign for the operator")
    check(kind("the payment method was declined") != B.REFUSAL,
          "billing wording was read as a reviewer refusal")
    # …and the other side: all four infinitive spellings still reach the operator.
    for text in ("I decline to review this diff.",
                 "the reviewer declines to answer on policy grounds",
                 "the model declined to continue",
                 "declining to proceed with this request"):
        check(kind(text) == B.REFUSAL, f"the agent sense of {text!r} stopped being a refusal")
        check(action(text) == B.STOP_AND_ASK, f"a reviewer refusal ({text!r}) did not stop and ask")


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
    # A banner that opens a LATER line is the case that makes the line structure load-bearing. It is
    # why `_normalize` folds apostrophes but never collapses newlines: rewrite the message's
    # whitespace to "complete" that fix and this banner is mid-line prose and stops matching.
    check(kind("codex exec failed\nusage: codex exec [OPTIONS] [PROMPT]") == B.NOT_FOUND,
          "a help banner opening a later line stopped matching; the message's lines were collapsed")
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
    # The same window bounds the before-trigger pass, so a pair far in FRONT of a trigger is not that
    # trigger's delay either.
    far_before = "60 seconds" + " padding" * 12 + " retry"
    check(B.guess_delay_seconds(far_before) is None,
          "a number far in front of the trigger window was still read as that trigger's delay")
    # The trigger must START a word, not merely sit inside one: telemetry that only CONTAINS the
    # letters of a retry word supplies no timer, and `unavailable` is not `available`.
    for text in ("retrieved 999 files in 3 seconds",
                 "retrieval finished in 3 hours",
                 "service unavailable after 12 seconds"):
        check(B.guess_delay_seconds(text) is None,
              f"telemetry {text!r} supplied a delay from a word that merely contains a retry word")


def t_a_trigger_matches_whole_words_only() -> None:
    # The trigger scan takes the SAME whole-word rule every marker takes. Anchored at the front only,
    # a trigger matched INSIDE a longer word, so ordinary prose stating no wait at all produced a
    # real one: the first of these guessed 900s, the wait cap exactly.
    for text in ("The waiter was quoted at 900 seconds",
                 "resettlement of 300 seconds of logs",
                 "the error is retryable; 800 seconds of telemetry followed",
                 "the waitlist cleared 120 seconds later",
                 "availableness was measured over 240 seconds"):
        check(B.guess_delay_seconds(text) is None,
              f"ordinary prose {text!r} supplied a delay from a trigger buried inside a word")
        check(wait(text) == B.DEFAULT_WAIT_SECONDS[kind(text)],
              f"{text!r} waited on a delay nobody stated")
    # …and the other side of that boundary, which is the whole difficulty and the ONLY reason the
    # alternation spells out inflections instead of stems: a bare stem with `\b` after it loses every
    # one of these, and each is a wording providers actually use to state a real delay.
    for text, expected in (("retry after 30 seconds", 30),
                           ("retries in 30 seconds", 30),
                           ("retrying in 25 seconds", 25),
                           ("retried after 25 seconds", 25),
                           ("try again in 45 seconds", 45),
                           ("wait 10 seconds", 10),
                           ("waits 10 seconds", 10),
                           ("waiting 30 seconds for the window", 30),
                           ("waited 10 seconds", 10),
                           ("backoff of 15 seconds", 15),
                           ("back off for 15 seconds", 15),
                           ("backoffs of 15 seconds", 15),
                           ("reset in 40 seconds", 40),
                           ("resets in 45 seconds", 45),
                           ("resetting in 40 seconds", 40),
                           ("available in 50 seconds", 50),
                           ("resume in 20 seconds", 20),
                           ("resumes in 20 seconds", 20),
                           ("resumed after 20 seconds", 20),
                           ("resuming in 20 seconds", 20),
                           ("cooldown of 15 seconds", 15),
                           ("cool down for 15 seconds", 15)):
        check(B.guess_delay_seconds(text) == expected,
              f"{text!r} guessed {B.guess_delay_seconds(text)!r}, not {expected}")


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
    # A pair stated BEFORE the retry word is still that trigger's delay: it is read, and here it is
    # over the cap, so the pass reviews natively instead of waiting out the usage-limit default.
    before = "Rate limit reached. 2 hours until retry."
    check(B.guess_delay_seconds(before) == 7200,
          "a delay stated in front of the retry word was not read at all")
    check(action(before) == B.FALLBACK_NATIVE,
          "a 2-hour limit stated before the retry word waited instead of reviewing natively")
    # …and telemetry riding along inside a real limit message must not beat the provider's own
    # delay to the first-pair rule, which would relaunch seconds into a 45-minute limit and burn
    # the pass's last external attempt. This is the other side of the boundary above, and it is why
    # the two passes are ORDERED: a both-sided scan reads this telemetry as the delay.
    hijack = ("usage limit reached: retrieved 12 files in 2 seconds before the cap; "
              "try again in 45 minutes")
    check(B.guess_delay_seconds(hijack) == 2700,
          "telemetry beat the provider's real delay to the first readable pair")
    check(action(hijack) == B.FALLBACK_NATIVE and wait(hijack) != B.MIN_WAIT_SECONDS,
          "a 45-minute limit relaunched the external reviewer instead of reviewing natively")


def t_the_scan_never_starts_inside_a_number() -> None:
    # One rule, every shape: the scan must not re-anchor in the MIDDLE of a number a provider wrote,
    # whatever character precedes the digits it lands on. Each of these answered a number nobody
    # stated — `-2 hours` as 7200, `1,800 seconds` as 800, and `10,000 seconds` as 0, which waited
    # five seconds and relaunched the external reviewer into a limit hours long.
    for text in ("retry after -2 hours", "retry after +2 hours", "retry in -30 seconds",
                 "retry after 1,800 seconds", "retry after 10,000 seconds",
                 "retry after 1.5 seconds"):
        check(B.guess_delay_seconds(text) is None,
              f"{text!r} started reading inside a written number")
        check(action(text) == B.WAIT_EXTERNAL,
              f"{text!r} escalated instead of taking its class default")
        check(wait(text) == B.DEFAULT_WAIT_SECONDS[kind(text)],
              f"{text!r} did not take its class default")
    # …and the other side of that boundary, which is the whole difficulty: a character that sits
    # BETWEEN two numbers is not part of either. The `-` in a RANGE separates two numbers instead of
    # signing one, and a range keeps guessing its high end; a `,` followed by a space punctuates a
    # sentence instead of grouping digits. A bare `[-+]` in the lookbehind passes every check above
    # and breaks the first two of these.
    for text, expected in (("try again in 30-60 seconds", 60),
                           ("please wait 30-60 seconds before retrying", 60),
                           ("wait, 60 seconds", 60),
                           ("retry after 2 hours", 7200),
                           ("retrying in 214 ms", 1)):
        check(B.guess_delay_seconds(text) == expected,
              f"{text!r} guessed {B.guess_delay_seconds(text)!r}, not {expected}")
    # THE SAME RULE BEYOND ASCII, which is where naming the forbidden characters instead of the
    # allowed ones failed. Once the ASCII sign and separators were named, every character NOT named
    # was still a way in: `−2 seconds` (U+2212) read as 2 and `1٫5 seconds` (U+066B) read as 5, each
    # taking a magnitude and dropping the sign or the integer part beside it, then waiting five
    # seconds on a number nobody stated. Their ASCII spellings are pinned above, so a rule that holds
    # in ASCII and not in Unicode is the defect. These four are illustrations of a class, NOT a list
    # to complete: the scan starts only where a lead-in below says a number begins, so a spelling
    # nobody thought of stays unread rather than half-read.
    for text in ("retry after −2 seconds",       # U+2212 MINUS SIGN
                 "retry after －2 hours",          # U+FF0D FULLWIDTH HYPHEN-MINUS
                 "retry after 1٫5 seconds",       # U+066B ARABIC DECIMAL SEPARATOR
                 "retry after 1٬800 seconds"):    # U+066C ARABIC THOUSANDS SEPARATOR
        check(B.guess_delay_seconds(text) is None,
              f"{text!r} started reading inside a written number")
        check(action(text) == B.WAIT_EXTERNAL,
              f"{text!r} escalated instead of taking its class default")
        check(wait(text) == B.DEFAULT_WAIT_SECONDS[kind(text)],
              f"{text!r} did not take its class default")
    # …and the other side of THAT boundary: the lead-in whitelist is what decides where a number may
    # begin, so every lead-in it admits has to keep working. The start of the message is one of them
    # and has no other fixture, so dropping it would otherwise go unnoticed.
    for text, expected in (("60 seconds until retry", 60),
                           ("retry after ~60 seconds", 60),
                           ("retry after (60 seconds)", 60),
                           ("retry after [60 seconds]", 60),
                           ("retry after\n60 seconds", 60)):
        check(B.guess_delay_seconds(text) == expected,
              f"{text!r} guessed {B.guess_delay_seconds(text)!r}, not {expected}")


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


def t_the_guess_is_bounded_and_the_marker_scan_is_not() -> None:
    # The guess's two passes MATERIALIZE every match before choosing one, so an unbounded scan costs
    # a multiple of the message. A runaway CLI's stderr capture is unbounded input, and a decision
    # that was never produced is not a bounded fallback — it is the absence of a decision.
    oversized = "retry after 1s " * (B.MAX_SCANNED_CHARS // 4)
    check(len(oversized) > B.MAX_SCANNED_CHARS, "the oversized fixture no longer exceeds the bound")
    decision = B.decide(oversized, 1)
    check(decision.action in {B.WAIT_EXTERNAL, B.FALLBACK_NATIVE, B.STOP_AND_ASK},
          f"an oversized capture produced the unknown action {decision.action!r}")
    check(0 <= decision.wait_seconds <= B.MAX_WAIT_SECONDS,
          "an oversized capture produced a wait outside the schedule")
    # The bound is real, and this is its disclosed consequence: a delay stated PAST it is not read at
    # all, so the failure takes its class default. Unbounded, this pair is read and the check fails.
    past = "usage limit reached " + "pad " * B.MAX_SCANNED_CHARS + " retry after 30 seconds"
    check(B.guess_delay_seconds(past) is None,
          "a delay past the scan bound was still read, so the guess is not bounded")
    check(wait(past) == B.DEFAULT_WAIT_SECONDS[B.USAGE_LIMIT],
          "a delay past the scan bound did not fall through to its class default")
    # …and the other side of that boundary: the bound belongs to the GUESS alone. A refusal marker
    # sitting past it must still reach the operator, so bounding `classify()` too goes red here.
    far_refusal = "x" * (B.MAX_SCANNED_CHARS * 2) + " I cannot help with that request."
    check(kind(far_refusal) == B.REFUSAL,
          "a refusal marker past the guess's bound stopped being found")
    check(action(far_refusal) == B.STOP_AND_ASK,
          "a refusal past the guess's bound silently fell back to a same-engine reviewer")
    # A readable delay in FRONT of the bound is still read, so the bound is not a blanket refusal.
    head = "usage limit reached; retry after 30 seconds " + "pad " * B.MAX_SCANNED_CHARS
    check(B.guess_delay_seconds(head) == 30,
          "a delay stated before the scan bound stopped being read")


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
    code, _out, err = capture_cli(B.main, ["decide", "--message", "x", "--message-file", "y",
                                           "--attempts-spent", "0"])
    check(code != 0 and "exactly one" in err, "the CLI accepted two message sources")
    code, _out, err = capture_cli(B.main, ["decide", "--attempts-spent", "0"])
    check(code != 0 and "exactly one" in err, "the CLI accepted no message source at all")
    # The attempt count is the ONLY thing that reaches the cap, so the CLI must refuse to guess it.
    # Defaulted to 0, a caller that follows the prose and omits it waits and retries forever.
    code, _out, err = capture_cli(B.main, ["decide", "--message", "usage limit reached"])
    check(code != 0 and "attempts-spent" in err,
          "the CLI ran `decide` without the attempt count and defaulted the retry budget")
    # …and the other side: the count it IS given is honoured at both ends of the cap.
    for spent, expected in ((1, B.WAIT_EXTERNAL), (B.MAX_EXTERNAL_ATTEMPTS, B.FALLBACK_NATIVE)):
        code, out, _err = capture_cli(B.main, ["decide", "--message", "usage limit reached",
                                               "--attempts-spent", str(spent)])
        check(code == 0 and json.loads(out)["action"] == expected,
              f"the CLI at {spent} spent attempts did not return {expected!r}")


CASES = [
    ("no-route-disable", "no message can disable the external route beyond this pass",
     t_no_message_disables_the_route),
    ("provider-messages", "the ten real provider messages land where documented",
     t_ten_provider_messages_land_where_documented),
    ("refusal-stops", "a reviewer refusal reaches the operator, never a same-engine fallback",
     t_refusal_stops_and_asks),
    ("marker-word-anchor", "a marker claims a class as a whole word, plural included, never as a "
     "substring inside another word", t_a_marker_matches_whole_words_only),
    ("refusal-typography", "a refusal reaches the operator in every apostrophe and line-wrap "
     "spelling a provider emits it in", t_a_refusal_survives_typography_and_wrapping),
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
    ("trigger-word-anchor", "a retry trigger introduces a delay as a whole word, every inflection "
     "included, never as a substring inside another word", t_a_trigger_matches_whole_words_only),
    ("unreadable-delay", "unreadable delay text takes the default instead of escalating",
     t_unreadable_delay_shapes_are_not_errors),
    ("first-pair", "the guess reads the first readable number-and-unit pair", t_first_readable_pair_wins),
    ("number-anchored", "the scan never starts inside a written number, while a range still guesses "
     "its high end", t_the_scan_never_starts_inside_a_number),
    ("wait-clamped", "the wait has a floor, and past the cap the pass reviews natively",
     t_wait_is_clamped_at_both_ends),
    ("scan-bounded", "the delay guess reads a bounded slice, while the marker scan reads it all",
     t_the_guess_is_bounded_and_the_marker_scan_is_not),
    ("attempt-cap", "the external route retries a fixed, capped number of times", t_attempts_are_capped),
    ("malformed-count", "a malformed attempt count reads as exhausted", t_malformed_attempt_count_is_exhausted),
    ("non-text", "non-text input is unknown, not a verdict", t_non_text_input_is_unknown_not_a_verdict),
    ("cli", "the CLI reports the decision as JSON and refuses ambiguous input",
     t_cli_reports_the_decision_as_json),
]
