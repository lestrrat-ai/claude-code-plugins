#!/usr/bin/env python3
# ci: pyright
"""Fixtures for `limit-retry.py` — the wait before the one external-reviewer retry.

They live in a SIBLING file, and `limit-retry.py self-test` FAILS LOUDLY if it cannot load them.

EVERY FIXTURE PINS A RULE WITH TEETH: it asserts the outcome on one side of a boundary AND the
opposite outcome on the other, so an implementation that returned a constant would go red.

Several of these are REGRESSIONS, not new rules — each pins a defect a reviewer actually found in a
predecessor of this file: `unquotable` and `quotation mark` reading as a limit, a combining mark or a
zero-width format character defeating the word anchor, a plural `quotas` no longer matching, a plural
`s` appended to a marker that already ended in one, prose delay text producing a wait nobody stated,
and a documented call that omitted the attempt count. Do not relax one.

One fixture pins the opposite: a DISCLOSED residual (`dotless-i`) that the file keeps deliberately, so
that a later round cannot quietly convert it into a rewrite the file's Non-goals refuse.

Two more pin DELETIONS rather than defects — `as-captured` and `one-class` hold the Non-goals the
owner states, so re-adding a reading layer over the message, a scan bound, a seventh phrase or a
second failure class goes red here. Each names its own residual: an example pins the wordings it
spells and nothing wider, and only the `Decision` field set pins a second class structurally.

Every non-ASCII character in a fixture is written as an ESCAPE, never as a literal: a literal
combining mark or zero-width character is invisible in a diff, and one normalisation pass over this
file would silently rewrite it into a fixture that pins nothing. Every such character has a named
constant below, and every fixture using one asserts the character survived.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from _gauntlet.modules import load_module_from_path
from _gauntlet.testing import capture_cli

OWNER = Path(__file__).resolve().parent / "limit-retry.py"


def _load_owner():
    # `register=True`: the owner's frozen dataclass resolves its own annotations through
    # `sys.modules`, so an unregistered module cannot build it.
    module = load_module_from_path("limit_retry_owner", OWNER, register=True)
    if module is None:
        raise RuntimeError(f"cannot load the limit-retry owner at {OWNER}")
    return module


L = _load_owner()

#: One ordinary limit message, used wherever the fixture is about something other than the marker.
LIMIT = "codex: usage limit reached for this account"
#: U+0301 COMBINING ACUTE ACCENT, as an escape. Never spell it as a literal — see the module docstring.
COMBINING_ACUTE = "\u0301"
#: Unicode FORMAT characters (general category `Cf`), as escapes. Each of the first four renders
#: `quota` + itself + `tion` as the single displayed word `quotation`, so an unfolded one satisfies
#: the trailing word anchor instead of closing it. `BYTE_ORDER_MARK` is the same category in the
#: OPPOSITE position: it opens a BOM-prefixed capture rather than continuing a word.
ZERO_WIDTH_JOINER = "\u200d"
ZERO_WIDTH_NON_JOINER = "\u200c"
WORD_JOINER = "\u2060"
SOFT_HYPHEN = "\u00ad"
BYTE_ORDER_MARK = "\ufeff"
#: U+0131 LATIN SMALL LETTER DOTLESS I and U+00E9 LATIN SMALL LETTER E WITH ACUTE, as escapes. The
#: first is one of the three non-ASCII characters `re.IGNORECASE` folds into a marker's letters; the
#: second is an ordinary letter that `re.ASCII` would stop counting as one.
DOTLESS_I = "\u0131"
E_ACUTE = "\u00e9"
#: U+0130 LATIN CAPITAL LETTER I WITH DOT ABOVE and U+017F LATIN SMALL LETTER LONG S, as escapes.
#: They are the other two characters of the same disclosed `re.IGNORECASE` tail that `DOTLESS_I`
#: belongs to, named separately because a folding pass over the MESSAGE removes them one at a time.
DOTTED_CAPITAL_I = "\u0130"
LONG_S = "\u017f"
#: FULLWIDTH letter and digit spellings of two markers, as escapes. Each is a COMPATIBILITY spelling
#: of one of the six phrases rather than the phrase itself, so it is a limit only once something
#: normalises the message.
FULLWIDTH_QUOTA = "\uff51\uff55\uff4f\uff54\uff41"    # FULLWIDTH q, u, o, t, a
FULLWIDTH_429 = "\uff14\uff12\uff19"                  # FULLWIDTH four, two, nine
#: Non-ASCII decimal digits, as escapes. Python's `\d` matches every one of these and `int()` converts
#: them, so each is a spelling that `\d` would read as RFC 9110's ASCII-only delta-seconds.
ARABIC_INDIC_60 = "\u0666\u0660"          # ARABIC-INDIC SIX, ZERO
FULLWIDTH_900 = "\uff19\uff10\uff10"      # FULLWIDTH NINE, ZERO, ZERO
MIXED_SCRIPT_60 = "6\u0660"               # ASCII SIX then ARABIC-INDIC ZERO


class FixtureFailure(AssertionError):
    """One pinned rule no longer holds."""


def check(condition: object, message: str) -> None:
    if not condition:
        raise FixtureFailure(message)


def action(message: object, attempts_spent: object) -> str:
    return L.decide(message, attempts_spent).action


def wait(message: object, attempts_spent: object) -> int:
    return L.decide(message, attempts_spent).wait_seconds


def t_a_marker_matches_whole_words_only() -> None:
    """REGRESSION. `unquotable` and `quotation mark` are ordinary words that happen to contain
    `quota`; both classified a usage limit and waited minutes for a limit nobody hit."""

    check(not L.is_limit("provider response is unquotable"),
          "`quota` matched inside `unquotable` — the word anchor is gone")
    check(not L.is_limit("bash: unterminated quotation mark in the prompt"),
          "`quota` matched inside `quotation` — the word anchor is gone")
    check(not L.is_limit("build 1.429 failed"),
          "`429` matched inside a version number — the digit anchor is gone")
    # The other side of that anchor: the markers themselves must still match, plural included.
    check(L.is_limit("monthly quotas exceeded for this org"),
          "`quotas` no longer matches — the marker's optional plural is gone")
    check(L.is_limit(LIMIT), "`usage limit` no longer matches at all")
    check(L.is_limit("HTTP 429 returned by the provider"), "`429` no longer matches on its own")
    check(L.is_limit("too many\nrequests from this client"),
          "a marker split across a wrapped line no longer matches")
    check(not L.is_limit("no external reviewer is configured"),
          "an unrelated failure classified as a limit — every message is now a limit")


def t_a_combining_mark_cannot_defeat_the_word_anchor() -> None:
    """REGRESSION. A combining mark is not `\\w`, so an unfolded one SATISFIES the trailing word
    anchor instead of closing it: `quota` + U+0301 + `tion` claimed a usage limit on text that is one
    ordinary word."""

    marked = "quota" + COMBINING_ACUTE + "tion marks were unbalanced"
    check(not marked.isascii(), "the fixture lost its combining mark and now pins nothing")
    check(not L.is_limit(marked),
          "a combining mark defeated the word anchor — the mark fold is gone")
    # The other side: the same letters with a real word break ARE the marker.
    check(L.is_limit("quota tion"), "`quota` no longer matches as a standalone word")


def t_a_format_character_cannot_defeat_the_word_anchor() -> None:
    """REGRESSION. A Unicode format character is not `\\w` either, so an unfolded one BETWEEN two
    letters satisfies the trailing word anchor exactly as a combining mark did: `quota` + U+200D +
    `tion` claimed a usage limit on text that displays as the single word `quotation`."""

    for name, character in (("U+200D ZERO WIDTH JOINER", ZERO_WIDTH_JOINER),
                            ("U+200C ZERO WIDTH NON-JOINER", ZERO_WIDTH_NON_JOINER),
                            ("U+2060 WORD JOINER", WORD_JOINER),
                            ("U+00AD SOFT HYPHEN", SOFT_HYPHEN)):
        joined = "quota" + character + "tion marks were unbalanced"
        check(not joined.isascii(), f"the {name} fixture lost its character and now pins nothing")
        check(not L.is_limit(joined),
              f"{name} defeated the word anchor — the format-character fold is gone")
    # The other side, and the reason that fold is FLANKED rather than unconditional: U+FEFF is a
    # format character too, but a BOM-prefixed capture OPENS with it, where it precedes the first word
    # instead of continuing one. Folding every format character puts a `\w` character in front of
    # `quota` and this plainly stated limit stops matching.
    prefixed = BYTE_ORDER_MARK + "quota exceeded for this account"
    check(not prefixed.isascii(), "the BOM fixture lost its byte order mark and now pins nothing")
    check(L.is_limit(prefixed),
          "a BOM-prefixed limit no longer matches — the format fold became unconditional")


def t_the_plural_suffix_skips_a_marker_already_ending_in_s() -> None:
    """REGRESSION. The optional trailing `s` was appended to every non-digit marker, including the one
    of the six that already ends in `s`, so `too many requestss` — neither one of the six phrases nor
    a plural of one — claimed a usage limit."""

    check(not L.is_limit("too many requestss were queued"),
          "`too many requestss` matched — the plural `s` is back on a marker that ends in `s`")
    # Both other sides: that marker still matches itself, and the plural still covers the markers it
    # is a real spelling for.
    check(L.is_limit("the provider reported too many requests"),
          "`too many requests` no longer matches at all")
    check(L.is_limit("HTTP 429 Too Many Requests"),
          "the documented provider line no longer matches")
    check(L.is_limit("monthly quotas exceeded for this org"),
          "the optional plural is gone from the markers that do not end in `s`")


def t_the_case_fold_keeps_its_disclosed_non_ascii_tail() -> None:
    """A DISCLOSED RESIDUAL, pinned so a later round cannot quietly rewrite it. `re.IGNORECASE` folds
    Unicode, and across all of it exactly three non-ASCII characters fold into the markers' letters
    (`_marker_pattern` states which), so `rate l` + U+0131 + `m` + U+0131 + `t` classifies as a limit.
    It STAYS: it is a case fold rather than a homoglyph one, so the matched text still reads as the
    marker itself as a whole word, and the whole cost is one bounded wait."""

    dotless = "rate l" + DOTLESS_I + "m" + DOTLESS_I + "t reached for this account"
    check(not dotless.isascii(), "the fixture lost its dotless i and now pins nothing")
    check(L.is_limit(dotless),
          "the disclosed non-ASCII case fold was removed — see `_marker_pattern` for why it stays")
    # The other side, and what each ASCII-only rewrite would cost: `re.ASCII` stops counting an
    # ordinary accented letter as a word character, so the word anchor opens and `quota` matches
    # inside an unrelated word again — the exact over-match the anchors and the mark fold exist to
    # stop.
    unrelated = "quota" + E_ACUTE + "ion marks were unbalanced"
    check(not unrelated.isascii(), "the fixture lost its accented letter and now pins nothing")
    check(not L.is_limit(unrelated),
          "`quota` matched inside an unrelated accented word — the word anchor is ASCII-only now")


def t_the_message_is_read_as_captured() -> None:
    """NO reading layer runs over the captured text before the markers are matched, and NO bound
    truncates it. Both are Non-goals the owner states, and neither was reachable from a fixture.

    A COMPATIBILITY fold (`NFKC`, `NFKD`) rewrites a fullwidth spelling into one of the six ASCII
    phrases, so text that is not a marker starts claiming a limit. A `casefold()` pass is the same
    extra layer from the other side, and it SUBTRACTS: of the three characters the disclosed
    `re.IGNORECASE` tail matches it removes only U+0130, so a fixture naming one of the other two
    never sees it — all three are pinned here for that reason. A scan bound drops a marker past it,
    which is why `is_limit` says it is deliberately not length-bounded.

    RESIDUAL: this pins the SPELLINGS it names. A normalising pass whose effect falls outside them
    stays unpinnable by example — `t_the_class_is_the_six_phrases_and_nothing_else` carries the one
    structural half this suite has."""

    for name, spelled in (("quota", FULLWIDTH_QUOTA), ("429", FULLWIDTH_429)):
        wide = spelled + " exceeded for this account"
        check(not wide.isascii(), f"the fullwidth `{name}` fixture lost its spelling, pinning nothing")
        check(not L.is_limit(wide),
              f"fullwidth `{name}` claimed a limit — a normalising pass over the message is back")
    for name, folded in (("U+0130", "rate l" + DOTTED_CAPITAL_I + "mit reached for this account"),
                         ("U+0131", "rate l" + DOTLESS_I + "mit reached for this account"),
                         ("U+017F", "u" + LONG_S + "age limit reached for this account")):
        check(not folded.isascii(), f"the {name} fixture lost its character and now pins nothing")
        check(L.is_limit(folded),
              f"{name} stopped folding into a marker — a folding pass over the message is back")
    check(L.is_limit("x" * 20000 + " usage limit reached"),
          "a marker 20000 characters in was missed — the scanned message length is bounded now")
    # The other side of all three: the ASCII spellings, which ARE the phrases, still classify.
    check(L.is_limit("quota exceeded for this account"),
          "ASCII `quota` no longer matches, so the fullwidth case above pins nothing")
    check(L.is_limit("HTTP 429 returned by the provider"),
          "ASCII `429` no longer matches, so the fullwidth case above pins nothing")


def t_the_class_is_the_six_phrases_and_nothing_else() -> None:
    """ONE class, SIX phrases. A limit worded outside them is simply not a limit here, and no OTHER
    failure earns handling of its own: a second class needs an ordering rule between the classes, and
    that ordering is the grammar two predecessors died of.

    RESIDUAL, and the reason the last check is not about wording: the loop pins the five wordings it
    spells, so a seventh phrase or a second class worded outside them escapes it. The field set of
    `Decision` is the STRUCTURAL half — a second class that reports which class it matched goes red by
    SHAPE rather than by vocabulary."""

    for text in ("the account is out of credits",
                 "capacity exceeded for this deployment",
                 "503 service unavailable",
                 "the model is overloaded right now",
                 "the server is busy, try another region"):
        decision = L.decide(text, 0)
        check(decision.limit is False, f"{text!r} was classified — a seventh phrase joined the six")
        check(decision.action == L.FALLBACK_NATIVE,
              f"{text!r} did not fall back — a second failure class is back")
        check(decision.wait_seconds == 0,
              f"{text!r} produced a wait — a second failure class is back")
    # The other side: the six still claim the class, and the decision still reports exactly one.
    check(L.decide(LIMIT, 0).action == L.WAIT_EXTERNAL,
          "one of the six no longer waits, so the wordings above pin nothing")
    check(set(vars(L.decide(LIMIT, 0))) == {"action", "limit", "wait_seconds", "attempts_spent",
                                            "attempts_cap", "reason"},
          "the decision gained or lost a field — a second class is being reported")


def t_retry_after_is_read_exactly() -> None:
    """The one delay spelling this file reads: RFC 9110's unitless `Retry-After: <delta-seconds>`."""

    check(L.retry_after_seconds("rate limit exceeded\nRetry-After: 7200\n") == 7200,
          "`Retry-After: 7200` was not read as 7200")
    check(L.retry_after_seconds("  retry-after:45") == 45,
          "an indented, lower-case, tight-spaced header was not read")
    check(L.retry_after_seconds("Retry-After: 60\r\n") == 60, "a CRLF header line was not read")
    check(wait(LIMIT + "\nRetry-After: 60", 0) == 60, "a readable header did not become the wait")
    check(wait(LIMIT + "\nRetry-After: 61", 0) == 61,
          "the wait is a constant, not the stated header value")


def t_only_the_ascii_colon_adjacent_header_is_read() -> None:
    """REGRESSION, both halves. The pattern spelled the digits `\\d` and allowed whitespace in front of
    the colon, so two spellings RFC 9110 does not define read as the header and produced a wait the
    provider never asked for.

    `\\d` matches every Unicode decimal digit and `int()` converts it, so `Retry-After:` followed by
    Arabic-Indic, fullwidth or mixed-script digits answered byte-identically to the ASCII spelling.
    RFC 9110 delta-seconds is `1*DIGIT`, ASCII only. RFC 9112 section 5.1 separately allows no
    whitespace between a field name and its colon, so a space in front of the colon is not this header
    either. Both take the default, like every other unread spelling.

    The LINE-LEADING anchor is the third spelling pinned here, and the owner refuses it for its own
    reason: without that anchor this stops being a header read and becomes a scan, so an inline
    mention or a JSON body starts producing a wait. Dropping only the leading anchor leaves every
    other case in this loop passing, which is why both inline shapes are named."""

    for text in (f"Retry-After: {ARABIC_INDIC_60}",
                 f"Retry-After: {FULLWIDTH_900}",
                 f"Retry-After: {MIXED_SCRIPT_60}",
                 "Retry-After : 60",
                 "Retry-After  :  60",
                 "retry-after\t: 60",
                 "error: retry-after: 60",
                 '{"error": "rate limit", "retry-after": 60}'):
        check(not text.isascii() or " :" in text or "\t:" in text
              or text.startswith("error:") or text.startswith("{"),
              f"the fixture case {text!r} lost the spelling it pins")
        message = LIMIT + "\n" + text
        check(L.retry_after_seconds(message) is None, f"{text!r} was read as a delay")
        check(wait(message, 0) == L.DEFAULT_WAIT_SECONDS,
              f"{text!r} did not take the default wait — it is being read as the header")
    # The other side of both anchors: ASCII digits with a colon-adjacent field name ARE the header, and
    # the same numbers in that spelling produce their own waits rather than the default.
    check(L.retry_after_seconds(LIMIT + "\nRetry-After: 60") == 60,
          "the ASCII, colon-adjacent header is no longer read at all")
    check(wait(LIMIT + "\nRetry-After: 61", 0) == 61,
          "the ASCII, colon-adjacent header no longer becomes the wait")


def t_no_other_delay_spelling_is_read() -> None:
    """REGRESSION, and the rule the two predecessors died on. Anything that is not the header's
    delta-seconds spelling — PROSE ABOVE ALL — reads as no delay and takes the default. A grammar
    added here reads `retry after 60 seconds` and relaunches 60s into a limit hours long.

    A grammar is written one WORDING at a time, so pinning one wording pins one grammar. The two the
    owner's Non-goal names by hand are here beside it: each states a delay far past the wait cap, so
    reading either flips this from the bounded wait to the native fallback."""

    for text in ("Retry-After: soon",
                 "Retry-After: -5",
                 "Retry-After: 72.5",
                 "Retry-After: 7200 ms",
                 "Retry-After: Wed, 21 Oct 2015 07:28:00 GMT",
                 "retry after 60 seconds",
                 "try again in 3 hours",
                 "your quota replenishes in 90 minutes",
                 "your limit resets in 2 hours",
                 "please wait 30-60 seconds before retrying"):
        message = LIMIT + "\n" + text
        check(L.retry_after_seconds(message) is None, f"{text!r} was read as a delay")
        check(action(message, 0) == L.WAIT_EXTERNAL, f"{text!r} did not take the ordinary wait")
        check(wait(message, 0) == L.DEFAULT_WAIT_SECONDS,
              f"{text!r} did not take the default wait — a delay grammar is back")
    # The other side: the header spelling still yields something other than the default.
    check(wait(LIMIT + "\nRetry-After: 61", 0) != L.DEFAULT_WAIT_SECONDS,
          "every message now takes the default — the header is never read")


def t_wait_is_clamped_at_both_ends() -> None:
    """A floor, so a sub-second hint never spins; a cap, past which the pass reviews natively NOW."""

    check(wait(LIMIT + "\nRetry-After: 0", 0) == L.MIN_WAIT_SECONDS,
          "a zero-second header did not take the floor")
    check(action(LIMIT + "\nRetry-After: 900", 0) == L.WAIT_EXTERNAL,
          "a header exactly at the cap fell back instead of waiting")
    check(wait(LIMIT + "\nRetry-After: 900", 0) == L.MAX_WAIT_SECONDS,
          "a header exactly at the cap did not wait its stated time")
    check(action(LIMIT + "\nRetry-After: 901", 0) == L.FALLBACK_NATIVE,
          "a header one second past the cap still waited")
    check(action(LIMIT + "\nRetry-After: 7200", 0) == L.FALLBACK_NATIVE,
          "a two-hour header still waited")
    # An absurdly wide digit run answers just past the cap rather than converting an unbounded number.
    huge = LIMIT + "\nRetry-After: " + "9" * 40
    check(L.retry_after_seconds(huge) == L.MAX_WAIT_SECONDS + 1,
          "an over-wide digit run was not answered just past the cap")
    check(action(huge, 0) == L.FALLBACK_NATIVE, "an absurdly large header still waited")
    # REGRESSION. That width shortcut reads width as a proxy for MAGNITUDE, and zero padding breaks the
    # proxy: a padded value must be read as the value, never as its width. `MAX_READABLE_DIGITS + 1`
    # zeroes stated zero seconds and fell back as though the header had asked for over the cap.
    padded_zero = LIMIT + "\nRetry-After: " + "0" * (L.MAX_READABLE_DIGITS + 1)
    check(L.retry_after_seconds(padded_zero) == 0,
          "a zero-padded header was answered by its width instead of its value")
    check(action(padded_zero, 0) == L.WAIT_EXTERNAL,
          "a zero-padded zero fell back instead of waiting")
    check(wait(padded_zero, 0) == L.MIN_WAIT_SECONDS,
          "a zero-padded zero did not take the same floor as the unpadded one")
    check(wait(padded_zero, 0) == wait(LIMIT + "\nRetry-After: 0", 0),
          "padding changed the answer for a value that did not change")
    padded_under_cap = LIMIT + "\nRetry-After: " + "0" * 8 + "60"
    check(wait(padded_under_cap, 0) == 60,
          "a zero-padded 60 was not read as 60 once it exceeded the width bound")
    # The other side of the shortcut: the SAME width with a significant leading digit is genuinely
    # over the cap and must still fall back, so stripping zeroes did not disarm the width bound.
    over_cap_same_width = LIMIT + "\nRetry-After: 1000000000"
    check(len("1000000000") == L.MAX_READABLE_DIGITS + 1 == len("0" * 10),
          "the padded and over-cap cases no longer share a digit width, so neither isolates padding")
    check(L.retry_after_seconds(over_cap_same_width) == L.MAX_WAIT_SECONDS + 1,
          "a genuinely over-wide value was converted instead of answered just past the cap")
    check(action(over_cap_same_width, 0) == L.FALLBACK_NATIVE,
          "a ten-digit value above the cap still waited")


def t_attempts_are_capped() -> None:
    """The retries are BOUNDED. Two external launches, then the native fallback — no third."""

    check(action(LIMIT, 0) == L.WAIT_EXTERNAL, "the first failure did not wait and retry")
    check(action(LIMIT, 1) == L.WAIT_EXTERNAL, "the one allowed retry was refused")
    check(action(LIMIT, L.MAX_EXTERNAL_ATTEMPTS) == L.FALLBACK_NATIVE,
          "the attempt cap was reached and the external route was retried anyway")
    check(action(LIMIT, L.MAX_EXTERNAL_ATTEMPTS + 1) == L.FALLBACK_NATIVE,
          "an over-cap attempt count still retried the external route")
    check(wait(LIMIT, L.MAX_EXTERNAL_ATTEMPTS) == 0, "an exhausted route still asked for a wait")


def t_a_malformed_attempt_count_reads_as_exhausted() -> None:
    """A caller bug costs one native pass, never an unbounded retry loop. `True` is `int`-like in
    Python and must NOT read as attempt 1."""

    for value in (None, -1, "1", 1.0, True, [0]):
        decision = L.decide(LIMIT, value)
        check(decision.action == L.FALLBACK_NATIVE, f"attempts_spent={value!r} was not exhausted")
        check(decision.attempts_spent == L.MAX_EXTERNAL_ATTEMPTS,
              f"attempts_spent={value!r} was not reported as exhausted")
    check(action(LIMIT, 0) == L.WAIT_EXTERNAL, "a valid count 0 is now exhausted too")


def t_a_non_limit_falls_back_without_waiting() -> None:
    """This file has ONE class. Everything else is somebody else's problem: fall back, do not wait."""

    for text in ("bash: codex: command not found", "authentication failed",
                 "connection reset by peer", "", "   ", None, 7):
        decision = L.decide(text, 0)
        check(decision.action == L.FALLBACK_NATIVE, f"{text!r} was treated as a provider limit")
        check(decision.limit is False, f"{text!r} was reported as a limit")
        check(decision.wait_seconds == 0, f"{text!r} produced a wait")
    check(L.decide(LIMIT, 0).limit is True, "a real limit is no longer reported as one")


def t_the_cli_requires_the_attempt_count() -> None:
    """The documented call passes `--attempts-spent`, and omitting it FAILS LOUDLY. A default of 0
    reads every failure as the pass's first, so the cap is never reached — the defect this pins."""

    code, _, _ = capture_cli(L.main, ["decide", "--message", LIMIT])
    check(code != 0, "`decide` without --attempts-spent succeeded — a silent default is back")
    code, out, _ = capture_cli(L.main, ["decide", "--message", LIMIT, "--attempts-spent", "0"])
    check(code == 0, "the documented `decide` call failed")
    payload = json.loads(out)
    check(payload["action"] == L.WAIT_EXTERNAL, "the CLI reported the wrong action")
    check(payload["wait_seconds"] == L.DEFAULT_WAIT_SECONDS, "the CLI reported the wrong wait")
    check(payload["attempts_cap"] == L.MAX_EXTERNAL_ATTEMPTS, "the CLI reported the wrong cap")
    code, _, _ = capture_cli(L.main, ["decide", "--attempts-spent", "0"])
    check(code != 0, "`decide` with no message at all succeeded")
    with tempfile.TemporaryDirectory() as directory:
        capture = Path(directory) / "capture.txt"
        capture.write_text(LIMIT + "\nRetry-After: 60\n", encoding="utf-8")
        code, out, _ = capture_cli(
            L.main, ["decide", "--message-file", str(capture), "--attempts-spent", "1"])
        check(code == 0, "the documented --message-file call failed")
        check(json.loads(out)["wait_seconds"] == 60, "the --message-file capture was not read")
        code, _, _ = capture_cli(
            L.main, ["decide", "--message", LIMIT, "--message-file", str(capture),
                     "--attempts-spent", "0"])
        check(code != 0, "both message inputs at once were accepted")


CASES = [
    ("marker-word-anchor", "a marker claims a limit as a whole word, plural included, never as a "
     "substring inside another word", t_a_marker_matches_whole_words_only),
    ("combining-mark", "a combining mark cannot defeat the word anchor",
     t_a_combining_mark_cannot_defeat_the_word_anchor),
    ("format-character", "a format character between two letters cannot defeat the word anchor, "
     "while a leading byte order mark still leaves the first word matchable",
     t_a_format_character_cannot_defeat_the_word_anchor),
    ("plural-suffix", "the optional plural `s` is skipped for the marker that already ends in one",
     t_the_plural_suffix_skips_a_marker_already_ending_in_s),
    ("dotless-i", "the case fold keeps its disclosed non-ASCII tail rather than an ASCII-only rewrite",
     t_the_case_fold_keeps_its_disclosed_non_ascii_tail),
    ("as-captured", "the message is matched as captured — no normalising pass, no scan bound",
     t_the_message_is_read_as_captured),
    ("one-class", "the class is the six phrases and nothing else; there is no second class",
     t_the_class_is_the_six_phrases_and_nothing_else),
    ("retry-after", "the `Retry-After` delta-seconds header is read exactly",
     t_retry_after_is_read_exactly),
    ("header-spelling", "only the ASCII delta-seconds spelling with a colon-adjacent field name is "
     "read; a Unicode digit or a space before the colon takes the default",
     t_only_the_ascii_colon_adjacent_header_is_read),
    ("no-delay-grammar", "no other delay spelling is read; prose takes the default",
     t_no_other_delay_spelling_is_read),
    ("wait-clamped", "the wait has a floor, and past the cap the pass reviews natively",
     t_wait_is_clamped_at_both_ends),
    ("attempt-cap", "the external route retries a fixed, capped number of times",
     t_attempts_are_capped),
    ("malformed-count", "a malformed attempt count reads as exhausted",
     t_a_malformed_attempt_count_reads_as_exhausted),
    ("non-limit", "anything that is not a limit falls back without waiting",
     t_a_non_limit_falls_back_without_waiting),
    ("cli", "the CLI requires --attempts-spent and reports the decision as JSON",
     t_the_cli_requires_the_attempt_count),
]
