#!/usr/bin/env python3
# ci: pyright
"""Fixtures for `nudge.py` — the sticky-note reminder rules.

They live in a SIBLING file, and `nudge.py --self-test` FAILS LOUDLY if it cannot load them.

EVERY FIXTURE MUST PIN A RULE with TEETH: it asserts the reminder fires when its condition holds AND is
ABSENT when it does not. A rule that only ever checked "the line is present" would pass against a printer
that emits every line unconditionally — which is no printer at all.
"""

from __future__ import annotations

import io
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _gauntlet.modules import load_sibling
from _gauntlet.testing import checker

OWNER = Path(__file__).resolve().parent / "nudge.py"


N = load_sibling("nudge_owner", OWNER.parent, OWNER.name)
L = N.L


def header(**kw) -> dict:
    return dict(L.HEADER_DEFAULTS, **{k: str(v) for k, v in kw.items()})


def row(pr, status, **kw) -> dict:
    r = dict(L.ROW_DEFAULTS, pr=str(pr), status=status)
    r.update({k: str(v) for k, v in kw.items()})
    r["id"] = f"pr{pr}"
    return r


def fire(rows, *, hdr=None, n_followups: "int | None" = 0, rundir=None, now=None,
         followups_path: "Path | None" = None, notes: "list | None" = None,
         notes_unread: "str | None" = None) -> list:
    return N.reminders(hdr or header(run_id="g1"), rows, n_followups, rundir, now, followups_path,
                       notes, notes_unread)


# A fixed "now" and two stamps around it, so the quiet-run rule is DETERMINISTIC: one older than
# QUIET_AFTER (fires) and one comfortably inside it (silent).
NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)


def stamp(minutes_ago: int) -> str:
    """A last_activity value `minutes_ago` before NOW, in the ledger's UTC ISO-8601 second-precision form."""
    return (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def has(lines, substr) -> bool:
    return any(substr in line for line in lines)


check = checker(N.SelfTestFailure)


# --- always-fire floor --------------------------------------------------------

def t_heartbeat_always_fires():
    for rows in ([], [row(1, "in_review")], [row(1, "merged")]):
        lines = fire(rows)
        pointers = [l for l in lines if "Primary continuity" in l]
        check(len(pointers) == 1,
              "exactly one Primary continuity pointer must fire EVERY heartbeat, whatever the ledger holds")
        check(not has(lines, "check the heartbeat is armed") and not has(lines, "confirm the next heartbeat"),
              "the reminder must NEVER tell the driver to check or confirm armed state — the re-arm is "
              "unconditional, never gated on inspected state (loop-control.md, Primary continuity)")


def t_header_reread_always_fires():
    check(has(fire([]), "re-read the ledger header"),
          "the header-reread reminder must fire every heartbeat")


# --- PR labels ----------------------------------------------------------------

def t_labels_fire_only_with_an_active_pr():
    check(has(fire([row(1, "in_review")]), "labels match its gate state"),
          "the labels reminder must fire when a PR is active")
    check(not has(fire([]), "labels match"),
          "the labels reminder must NOT fire with no PRs — nothing to relabel")
    check(not has(fire([row(1, "merged"), row(2, "aborted")]), "labels match"),
          "the labels reminder must NOT fire when every PR is terminal")


# --- run-level ----------------------------------------------------------------

def t_required_set_unknown():
    # An active row whose EFFECTIVE required set is unknown (here inherited from the legacy header) nudges
    # to run the grouped read, NAMING the base it blocks.
    hdr = header(run_id="g1", base_branch="main", required_set="unknown")
    lines = fire([row(1, "in_review")], hdr=hdr)  # row base/required_set `-` → inherit header
    check(has(lines, "required set unknown for base main"),
          "an unknown effective required set must nudge to run the grouped read, naming the base")
    # A row with its OWN settled required set does NOT fire — even when the header fallback is unknown.
    check(not has(fire([row(1, "in_review", required_set="none")], hdr=hdr), "required set unknown"),
          "a row with an explicit, settled required set must NOT fire the derive nudge")
    # No active PR → there is no per-base required set to derive, whatever the header holds.
    check(not has(fire([], hdr=hdr), "required set unknown"),
          "with no active PR there is no per-base required set to derive")


def t_required_set_unknown_is_per_base():
    # A mixed-base run: one base is settled, one is still unknown. Only the unsettled base is reminded,
    # named with its PR(s) — the grouped read is per distinct effective base.
    hdr = header(run_id="g1", base_branch="-", required_set="unknown")
    rows = [
        row(1, "in_review", base_branch="v3", required_set="declared:[]"),  # settled for v3
        row(2, "in_review", base_branch="main", required_set="unknown"),    # unsettled for main
    ]
    lines = fire(rows, hdr=hdr)
    check(has(lines, "required set unknown for base main (PR(s) 2)"),
          "only the unsettled base is reminded, naming its PR(s)")
    check(not has(lines, "required set unknown for base v3"),
          "a base whose required set is already settled must NOT fire the unknown reminder")
    check(has(lines, "base v3 (PR(s) 1): required set declared:[]."),
          "a settled base is REPORTED with its effective base and required set instead")


def t_settled_base_report():
    # The intent's purpose bullet: nudge NAMES each active row's effective base and required set. A settled
    # single-base ledger (legacy channel: header base + required set, rows inheriting) must report both
    # values — not only the generic re-resolve instruction; an explicit row value reports the same way.
    hdr = header(run_id="g1", base_branch="release/v3", required_set="none")
    lines = fire([row(1, "in_review")], hdr=hdr)  # row base/required_set `-` → inherit header
    check(has(lines, "base release/v3 (PR(s) 1): required set none."),
          "a settled base must be reported naming its effective base and required set")
    lines = fire([row(2, "in_review", base_branch="v3", required_set="declared:[]")], hdr=hdr)
    check(has(lines, "base v3 (PR(s) 2): required set declared:[]."),
          "an explicit row base/required set must drive the report (row-owned, not the header)")
    # Teeth: the report is computed from ACTIVE rows — no active row, no report line.
    check(not has(fire([], hdr=hdr), "required set"),
          "with no active PR there is no per-base report")
    check(not has(fire([row(1, "merged")], hdr=hdr), "base release/v3"),
          "a terminal row must not be reported")


def t_fanout_fires_only_with_open_work():
    check(has(fire([row(1, "in_review")]), "fan out work"),
          "an open PR must nudge to fan out")
    check(not has(fire([row(1, "merged")]), "fan out work"),
          "no open PR must NOT nudge to fan out")


def t_followups_fire_only_when_open():
    check(has(fire([], n_followups=3), "3 open follow-up"),
          "open follow-ups must nudge, with the count")
    check(not has(fire([], n_followups=0), "open follow-up"),
          "zero open follow-ups must NOT nudge")


def t_unread_followup_store_is_disclosed():
    """An UNREAD follow-up store must SAY SO, naming why — never fall silent. Silence is what a caller
    reads as "this run has no open follow-ups", and the store goes unread on two ordinary invocations:
    `--followups` omitted, and a path that is not there. The store path is not derivable from `--rundir`
    (`.gauntlet/` is a project-root concern, not a run-directory one) and the printer always exits 0, so
    DISCLOSURE — not a default and not a refusal — is the whole fix."""
    # unit level: not-read (None) discloses; a successful read never does.
    absent = fire([], n_followups=None)
    check(has(absent, "follow-up store NOT READ (no --followups given)"),
          "an omitted --followups must disclose that the store was not read, and why")
    missing = fire([], n_followups=None, followups_path=Path("/nonexistent/typo.jsonl"))
    check(has(missing, "follow-up store NOT READ (no file at /nonexistent/typo.jsonl)"),
          "a --followups path that is not there must disclose it, NAMING the path it tried")
    # Teeth: a store that WAS read never claims it was not — neither with open entries nor empty.
    check(not has(fire([], n_followups=3), "NOT READ"),
          "a store that was read must report its count, never the not-read disclosure")
    check(not has(fire([], n_followups=0), "NOT READ"),
          "an EMPTY store that was read is genuinely zero — it must stay silent, not disclose")
    # main() plumbing: the same two invocations end to end, plus a real store proving the count wins.
    with tempfile.TemporaryDirectory() as d:
        rd = Path(d)
        led = rd / "state.jsonl"
        led.write_text('{"type": "header", "run_id": "g1", "required_set": "none"}\n', encoding="utf-8")
        check("follow-up store NOT READ (no --followups given)" in _run_main(["--file", str(led)]),
              "a --file-only invocation must DISCLOSE the unread store — the silent-misuse case")
        typo = rd / "typo.jsonl"
        check(f"follow-up store NOT READ (no file at {typo})"
              in _run_main(["--file", str(led), "--followups", str(typo)]),
              "a mistyped --followups path must DISCLOSE it end to end, naming the path")
        # Teeth end to end: a REAL store with an open entry prints the count and no disclosure. Built
        # through followups.py's own writer, so this fixture never restates that store's schema.
        store = rd / "followups.jsonl"
        N.F.dump(store, [{"id": "fu1", "title": "t", "evidence": "e", "deferred_why": "w"}], 0)
        out = _run_main(["--file", str(led), "--followups", str(store)])
        check("1 open follow-up(s) — start any you can." in out and "NOT READ" not in out,
              "a store that exists must yield the COUNT and no disclosure — the disclosure must "
              "discriminate on whether the store was actually read")


# --- standing notes -----------------------------------------------------------
# `<project_root>/.gauntlet/nudges.md` — hand-written standing lessons, delivered verbatim after every
# computed reminder. Its path is DERIVED from `--followups`, never a flag, so these fixtures pin the
# derivation as hard as the content rules: a wrong path reads the wrong file, or none, and says nothing.

HEADER_ONLY = '{"type": "header", "run_id": "g1", "required_set": "none"}\n'


def _laid_out(d: str) -> "tuple[Path, Path, Path]":
    """A realistic layout — `(ledger, followups store, notes file)` — with `<rundir>` and `.gauntlet/`
    as SEPARATE directories, so a fixture that passes only because the two happen to coincide cannot."""
    root = Path(d)
    rundir = root / "tmp" / "run"
    rundir.mkdir(parents=True)
    gauntlet = root
    ledger = rundir / "state.jsonl"
    ledger.write_text(HEADER_ONLY, encoding="utf-8")
    return ledger, gauntlet / "followups.jsonl", gauntlet / "nudges.md"


def t_notes_path_is_derived_from_the_followups_path():
    """The notes file is `--followups`'s SIBLING `nudges.md` and NOTHING ELSE. That derivation is why
    there is no `--notes` flag at all: a flag has to be added to the canonical invocation
    (`loop-control.md`, Step 1), and a delivery channel whose flag was never added there never fires —
    silently, forever. So the derivation is load-bearing, and it is pinned here.

    Teeth: a `nudges.md` sitting in the RUN DIRECTORY — the other directory nudge already knows — must
    not be read, and no `--followups` must yield no path at all rather than some guessed one.
    """
    check(N.notes_path(Path("/p/.gauntlet/followups.jsonl")) == Path("/p/.gauntlet/nudges.md"),
          "the notes path must be the --followups path's sibling nudges.md")
    check(N.notes_path(None) is None,
          "with no --followups there is NO notes path — never a guessed fallback directory")
    with tempfile.TemporaryDirectory() as d:
        led, store, notes = _laid_out(d)
        notes.write_text("the sibling note\n", encoding="utf-8")
        (led.parent / "nudges.md").write_text("the rundir note\n", encoding="utf-8")
        out = _run_main(["--file", str(led), "--followups", str(store), "--rundir", str(led.parent)])
        check("standing note: the sibling note" in out,
              "the notes file beside the follow-up store must be READ end to end")
        check("the rundir note" not in out,
              "a nudges.md in the RUN directory must NOT be read — the path derives from --followups only")


def t_absent_notes_file_says_nothing():
    """A missing notes file is the NORMAL state — the file is optional, hand-written user data — so the
    look that finds nothing is SILENT. This is the one place the notes deliberately split from the
    follow-up store, whose typed path treats "not there" as not-read.

    Teeth: write one line at the same derived path and the SAME invocation must speak. A fixture that
    only checked the silence would pass against a printer that never reads the file at all.
    """
    with tempfile.TemporaryDirectory() as d:
        led, store, notes = _laid_out(d)
        quiet = _run_main(["--file", str(led), "--followups", str(store)])
        check("standing note" not in quiet,
              "no notes file at the derived path must produce NO note line and NO disclosure")
        notes.write_text("- keep the ledger honest\n", encoding="utf-8")
        loud = _run_main(["--file", str(led), "--followups", str(store)])
        check("standing note: keep the ledger honest" in loud,
              "the same invocation must deliver the note once the file exists — the silence was a real "
              "read, not an unread file")


def t_unread_notes_are_disclosed():
    """NOT READ is never silence. Three ways the notes cannot be read, each DISCLOSED and each naming
    why, and every one of them still exits 0: no `--followups` (so `.gauntlet/` is unknown), something
    unreadable in the file's place, and bytes that are not UTF-8.
    """
    # No --followups: the project root is genuinely unknown and nothing may be guessed.
    absent = fire([], notes_unread=None, notes=None)
    check(not has(absent, "standing note"), "the default (read, nothing found) must stay silent")
    with tempfile.TemporaryDirectory() as d:
        led, store, notes = _laid_out(d)
        out = _run_main(["--file", str(led)])
        check("standing notes NOT READ (no --followups given, so the .gauntlet/ directory is unknown)" in out,
              "with no --followups the notes must be DISCLOSED unread, naming why — never reported absent")
        check("standing notes are UNKNOWN, not absent." in out,
              "the disclosure must say the notes are UNKNOWN, not that there are none")
        # A DIRECTORY where the file belongs: it stats fine and fails on read.
        notes.mkdir()
        out = _run_main(["--file", str(led), "--followups", str(store)])
        check(f"standing notes NOT READ ({notes} could not be read (IsADirectoryError))" in out,
              "an unreadable notes path must be DISCLOSED, naming the path and the failure — never raise")
        notes.rmdir()
        # Bytes that are not UTF-8 are disclosed, never silently mangled by a lenient decode.
        notes.write_bytes(b"fine\n\xff\xfe not utf-8\n")
        out = _run_main(["--file", str(led), "--followups", str(store)])
        check(f"standing notes NOT READ ({notes} could not be read (UnicodeDecodeError))" in out,
              "an undecodable notes file must be DISCLOSED, never partially delivered")
        check("standing note: fine" not in out,
              "an undecodable file must deliver NO line — a half-read file is not a read file")
        # Teeth: a file that reads fine never claims it was not read.
        notes.write_bytes(b"fine\n")
        out = _run_main(["--file", str(led), "--followups", str(store)])
        check("standing note: fine" in out and "standing notes NOT READ" not in out,
              "a readable notes file must deliver its line and NO not-read disclosure — the disclosure "
              "must discriminate on whether the file was actually read")


def t_oversized_notes_file_is_disclosed():
    """A file too large to hold in memory is DISCLOSED and not read, rather than risking the one thing
    this printer may never do — fail. Teeth: a file one byte under the cap is read normally."""
    with tempfile.TemporaryDirectory() as d:
        led, store, notes = _laid_out(d)
        notes.write_bytes(b"x" * (N.NOTES_MAX_BYTES + 1))
        out = _run_main(["--file", str(led), "--followups", str(store)])
        check(f"over the {N.NOTES_MAX_BYTES}-byte cap" in out and "standing notes NOT READ" in out,
              "a notes file over the byte cap must be DISCLOSED unread, naming the cap")
        under = b"under the cap\n" + b"# pad\n" * 100
        check(len(under) <= N.NOTES_MAX_BYTES, "the fixture's 'under' file must actually be under the cap")
        notes.write_bytes(under)
        out = _run_main(["--file", str(led), "--followups", str(store)])
        check("standing note: under the cap" in out and "standing notes NOT READ" not in out,
              "a file under the byte cap must be read normally — the guard must be a cap, not a refusal")


def t_notes_content_model():
    """One reminder per line, VERBATIM — minus the shapes that are not reminders. Blank lines and `#`
    lines are skipped so the file can carry markdown headings and comments, and a leading `- `/`* ` is
    stripped so a bullet list and a plain list both work. One equality pins every one of those rules:
    drop any single rule and this list changes.
    """
    parsed = N.parse_notes(
        "# a heading\n"
        "\n"
        "- a bullet note\n"
        "* a star note\n"
        "a plain note\n"
        "   \n"
        "   # an indented comment\n"
        "  - an indented bullet  \n"
        "-\n"
        "a note with a # inside it\n"
        "-not-a-bullet\n"
    )
    check(parsed == ["a bullet note", "a star note", "a plain note", "an indented bullet",
                     "a note with a # inside it", "-not-a-bullet"],
          f"the notes content model must skip blanks and # lines, strip a leading bullet marker, and "
          f"otherwise take the line verbatim — got {parsed!r}")
    check(N.parse_notes("") == [] and N.parse_notes("# only a comment\n") == [],
          "a file with no usable line must yield no note — and, having been READ, no disclosure either")
    check(has(fire([], notes=["delivered as written"]), "standing note: delivered as written"),
          "a parsed note must reach the reminder list under the standing-note mark")


def t_notes_come_after_every_computed_reminder():
    """Notes go LAST, always. The computed reminders are the live state — the part that goes stale — so
    they keep the top of the list where they are read first. Teeth: the computed lines must all still be
    there, and no note may appear before any of them.
    """
    lines = fire([row(1, "in_review", ci="pending")], notes=["standing advice"])
    marked = [i for i, l in enumerate(lines) if l.startswith(N.NOTES_MARK)]
    computed = [i for i, l in enumerate(lines) if not l.startswith(N.NOTES_MARK)]
    check(bool(marked) and bool(computed), "the fixture must produce BOTH computed reminders and notes")
    check(min(marked) > max(computed),
          f"every standing note must follow every computed reminder — got {lines!r}")
    check(has(lines, "PR 1: CI pending") and has(lines, "fan out work"),
          "appending notes must not displace any computed reminder")


def t_notes_cap_truncates_and_discloses_the_hidden_count():
    """The cap bounds how many note lines are RENDERED, so a file that grew without bound cannot drown
    the computed reminders — and when it bites, THE OMISSION IS NEVER SILENT: the output says how many
    lines it is hiding and where they are. Teeth: exactly at the cap nothing is hidden and nothing is
    disclosed, so the disclosure cannot be an always-on line.
    """
    cap = N.NOTES_CAP
    over = [f"note {i}" for i in range(cap + 3)]
    lines = fire([], notes=over, followups_path=Path("/p/.gauntlet/followups.jsonl"))
    shown = [l for l in lines if l.startswith(f"{N.NOTES_MARK}: ")]
    check(len(shown) == cap, f"at most {cap} note lines may be rendered — got {len(shown)}")
    check(has(lines, f"standing notes TRUNCATED — 3 of {cap + 3} line(s)"),
          "the truncation must disclose how many lines were hidden, out of how many")
    check(has(lines, f"(cap {cap}); trim the file.") and has(lines, "/p/.gauntlet/nudges.md"),
          "the truncation disclosure must name the cap and the file the hidden lines are in")
    at = fire([], notes=[f"note {i}" for i in range(cap)])
    check(len([l for l in at if l.startswith(f"{N.NOTES_MARK}: ")]) == cap and not has(at, "TRUNCATED"),
          "exactly at the cap nothing is hidden, so nothing may be disclosed")


# --- held PRs short-circuit ---------------------------------------------------

def t_parked_pr_fires_only_its_own_reminder():
    lines = fire([row(7, "awaiting-user", ci_reason="settled but not green")])
    check(has(lines, "PR 7: parked"), "a parked PR must nudge that it is parked")
    check(has(lines, "settled but not green"), "the park reminder must carry ci_reason")
    check(not has(lines, "PR 7: CI pending") and not has(lines, "PR 7: mergeable")
          and not has(lines, "PR 7: work due"),
          "a HELD PR must fire ONLY its held reminder — never review/CI/merge nudges")


def t_repairing_splits_on_decision():
    no_dec = fire([row(7, "repairing", repair_decision="-")])
    check(has(no_dec, "repairing, no decision"), "repairing + no decision → reassess nudge")
    check(has(no_dec, "repair-pass.py bundle"), "reassessment nudge must name the executable bundle door")
    check(not has(no_dec, "dispatch decision"), "no-decision repairing must not say dispatch")
    # A legacy DEMOTE row is deliberate: nudge remains a generic consumer of durable decisions.
    with_dec = fire([row(7, "repairing", repair_decision="demote@2026-01-01T00:00:00Z")])
    check(has(with_dec, "prepare then dispatch decision"), "repairing + decision → preparation/dispatch nudge")
    check(not has(with_dec, "no decision"), "decided repairing must not say NO decision")


# --- per-PR in-flight ---------------------------------------------------------

def t_intent_missing_fires_only_without_the_file():
    with tempfile.TemporaryDirectory() as d:
        rd = Path(d)
        r = [row(9, "in_review", reviews_ok=0, tier="HIGH")]
        check(has(fire(r, rundir=rd), "no intent-9.md"),
              "a review-due PR with no intent file must nudge to write it")
        (rd / "intent-9.md").write_text("x", encoding="utf-8")
        check(not has(fire(r, rundir=rd), "no intent-9.md"),
              "once the intent file exists the nudge must STOP")


def t_ci_pending_fires():
    check(has(fire([row(9, "in_review", ci="pending")]), "PR 9: CI pending"),
          "a pending-CI PR must nudge to re-derive")
    check(not has(fire([row(9, "in_review", ci="green")]), "PR 9: CI pending"),
          "a green PR must NOT fire the CI-pending nudge")


def t_work_due_fires_whenever_review_is_due():
    """The work-due reminder fires whenever a PR needs review and isn't blocked — NOT keyed to any
    progress file. This pins fu25: a first review (review_rounds=0) must fire, and it must not claim to
    know the work is a 'review' specifically (an audit or fix may be what's live)."""
    with tempfile.TemporaryDirectory() as d:
        rd = Path(d)
        (rd / "intent-9.md").write_text("x", encoding="utf-8")  # keep the intent nudge quiet
        # review_rounds=0, no progress file at all → the OLD rule missed this; the new one must fire.
        r = [row(9, "in_review", reviews_ok=0, tier="HIGH", ci="green", review_rounds=0)]
        check(has(fire(r, rundir=rd), "work due — make sure a dispatched review/audit/fix is live"),
              "a first review (review_rounds=0) must fire the work-due nudge — the fu25 miss")
        # not review-due → silent: enough verdicts (mergeable), or CI red.
        check(not has(fire([row(9, "in_review", reviews_ok=2, tier="HIGH", ci="green")]), "work due"),
              "a PR with its verdicts is NOT work-due — no work-due nudge")
        check(not has(fire([row(9, "in_review", reviews_ok=0, tier="HIGH", ci="red")]), "work due"),
              "a red-CI PR is not review-due — fix CI first, no work-due nudge")


def t_mergeable_fires_when_counters_are_met():
    check(has(fire([row(9, "in_review", reviews_ok=2, tier="HIGH", ci="green")]), "mergeable"),
          "reviews_ok >= required and green → nudge to check merge-readiness")
    check(not has(fire([row(9, "in_review", reviews_ok=1, tier="HIGH", ci="green")]), "mergeable"),
          "short of required verdicts → NOT mergeable, no merge nudge")
    # TRIVIAL needs only 1
    check(has(fire([row(9, "in_review", reviews_ok=1, tier="TRIVIAL", ci="green")]), "mergeable"),
          "a TRIVIAL PR needs only 1 verdict to read mergeable")


def t_terminal_pr_fires_no_per_pr_line():
    lines = fire([row(9, "merged"), row(10, "aborted")])
    check(not has(lines, "PR 9:") and not has(lines, "PR 10:"),
          "a terminal PR must produce NO per-PR reminder")


# --- quiet-run sweep ----------------------------------------------------------
# Boundary stamps are derived FROM N.QUIET_AFTER, not hard-coded minutes, so a re-tuned constant still
# leaves one stamp comfortably over the threshold and one comfortably under it.
_QUIET_MIN = int(N.QUIET_AFTER.total_seconds() // 60)


def t_quiet_run_fires_when_stale():
    """A ledger quiet longer than QUIET_AFTER with an open PR fires the whole sweep — and a FRESH ledger
    fires none of it (the teeth: an always-on sweep would be no sweep)."""
    stale = fire([row(1, "in_review", ci="green")], hdr=header(run_id="g1", last_activity=stamp(_QUIET_MIN + 5)), now=NOW)
    check(has(stale, "QUIET"), "a run quiet past QUIET_AFTER must fire the sweep")
    check(has(stale, "review-pass.py status --run <rundir> --verify"),
          "the sweep must remind to run the review-pass status check with --verify")
    check(has(stale, "re-derive CI"), "the sweep must remind to re-derive CI for rows that can move")
    fresh = fire([row(1, "in_review", ci="green")], hdr=header(run_id="g1", last_activity=stamp(_QUIET_MIN - 5)), now=NOW)
    check(not has(fresh, "QUIET"), "a run with recent activity must NOT fire the quiet sweep")


def t_quiet_run_silent_when_absent():
    """An absent/`-` last_activity (an old ledger that predates the sensor) is NOT a stall — stay silent."""
    check(not has(fire([row(1, "in_review")], hdr=header(run_id="g1"), now=NOW), "QUIET"),
          "a default `-` last_activity must NOT fire the quiet sweep")
    check(not has(fire([row(1, "in_review")], hdr=header(run_id="g1", last_activity=""), now=NOW), "QUIET"),
          "an empty last_activity must NOT fire the quiet sweep")
    check(not has(fire([row(1, "in_review")], hdr=header(run_id="g1", last_activity="not-a-date"), now=NOW), "QUIET"),
          "an unparseable last_activity must NOT fire the quiet sweep — advisory, never a crash")


def t_quiet_run_needs_an_open_pr():
    """A quiet ledger with NOTHING open has nothing to sweep — the rule stays silent."""
    check(not has(fire([row(1, "merged")], hdr=header(run_id="g1", last_activity=stamp(_QUIET_MIN + 5)), now=NOW), "QUIET"),
          "a quiet run whose only rows are terminal must NOT fire the sweep")


def t_quiet_run_names_the_park():
    """When PARKED rows are the ONLY open rows, the sweep says the run is waiting on the USER and surfaces
    the unanswered question — the park is the thing to act on, not a stall to chase."""
    lines = fire([row(7, "awaiting-user", ci_reason="needs your approval")],
                 hdr=header(run_id="g1", last_activity=stamp(_QUIET_MIN + 5)), now=NOW)
    check(has(lines, "waiting on YOU"), "a parked-only quiet run must say it is waiting on the user")
    check(has(lines, "PR 7: parked — LEAD") and has(lines, "needs your approval"),
          "the sweep must surface the parked PR's question, led with how long it has waited")
    # a MIXED run (an in-flight row alongside the parked one) is NOT parked-only — it must not claim so.
    mixed = fire([row(7, "awaiting-user", ci_reason="needs your approval"), row(8, "in_review")],
                 hdr=header(run_id="g1", last_activity=stamp(_QUIET_MIN + 5)), now=NOW)
    check(has(mixed, "QUIET") and not has(mixed, "waiting on YOU"),
          "a run with an in-flight PR is not idle-on-user — it must not claim every open PR is parked")


# --- watchdog-due reminder ----------------------------------------------------
# Stamps built around NOW so the rule is DETERMINISTIC: a future deadline (ok → silent) and a past one
# (due → fires), plus the `-`/malformed/naive spellings that read unset/invalid.

def _future(minutes: int) -> str:
    return (NOW + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _past(minutes: int) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def t_watchdog_due_fires_on_unset_overdue_invalid_with_open_work():
    """With an OPEN PR, the reminder fires when watchdog_due is unset (`-`), overdue (a past deadline), or
    invalid (malformed or naive) — and stays SILENT when it is `ok` (a future deadline). The teeth: an
    always-on reminder would be no sensor, so the `ok` case must be silent."""
    open_row = [row(1, "in_review")]
    for label, wd in (("unset (`-`)", "-"),
                      ("overdue", _past(10)),
                      ("malformed", "not-a-date"),
                      ("naive", "2026-07-19T11:00:00")):  # no tzinfo
        lines = fire(open_row, hdr=header(run_id="g1", watchdog_due=wd), now=NOW)
        check(has(lines, "watchdog due — run the health pass"),
              f"a {label} watchdog_due with open work must fire the watchdog-due reminder")
    ok = fire(open_row, hdr=header(run_id="g1", watchdog_due=_future(30)), now=NOW)
    check(not has(ok, "watchdog due"),
          "a future (ok) watchdog deadline must NOT fire the reminder — the run does not owe a health pass yet")


def t_watchdog_due_silent_without_open_work():
    """No non-terminal row → nothing to health-check, so the reminder stays silent even when unset/overdue —
    both an empty ledger and a terminal-only one."""
    for rows in ([], [row(1, "merged"), row(2, "aborted")]):
        for wd in ("-", _past(10), "not-a-date"):
            lines = fire(rows, hdr=header(run_id="g1", watchdog_due=wd), now=NOW)
            check(not has(lines, "watchdog due"),
                  f"a run with no open work must NOT fire the watchdog-due reminder (rows={rows}, wd={wd!r})")


def _run_main(argv) -> str:
    """main()'s printed output, so a fixture can pin the FLAG PLUMBING and not only `reminders()`."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = N.main(argv)
    check(code == 0, f"main({argv!r}) must exit 0 — a nudge never blocks")
    return buf.getvalue()


def t_rundir_defaults_to_the_ledger_directory():
    """`--file` alone must NOT report every intent file missing. The ledger IS `<rundir>/state.jsonl`, so
    the run directory is its parent and nudge derives it. Before this, a `--file`-only call passed
    rundir=None, `rundir_has` read that as "absent", and every review-due PR got a confident, wrong
    "no intent-<N>.md" line with no error and no warning."""
    with tempfile.TemporaryDirectory() as d:
        rd = Path(d)
        led = rd / "state.jsonl"
        led.write_text('{"type": "header", "run_id": "g1", "required_set": "none"}\n'
                       '{"type": "row", "id": "pr9", "pr": "9", "status": "in_review", "tier": "HIGH",'
                       ' "reviews_ok": "0", "ci": "green"}\n', encoding="utf-8")
        (rd / "intent-9.md").write_text("x", encoding="utf-8")
        check("no intent-9.md" not in _run_main(["--file", str(led)]),
              "an intent file NEXT TO the ledger must silence the nudge with NO --rundir passed — the run "
              "directory is the ledger's parent, never unknown")
        # Teeth: the derived rundir is really being READ, not just suppressing the rule. Remove the file and
        # the same --file-only invocation must fire again.
        (rd / "intent-9.md").unlink()
        check("no intent-9.md" in _run_main(["--file", str(led)]),
              "with the intent file gone the same --file-only invocation must fire the nudge — the derived "
              "run directory is read, not ignored")
        # An explicit --rundir still WINS over the derived default.
        other = rd / "elsewhere"
        other.mkdir()
        (other / "intent-9.md").write_text("x", encoding="utf-8")
        check("no intent-9.md" not in _run_main(["--file", str(led), "--rundir", str(other)]),
              "an explicit --rundir must override the derived default")


def t_a_nudge_never_blocks():
    # main() over a real ledger file exits 0 no matter what it prints.
    with tempfile.TemporaryDirectory() as d:
        led = Path(d) / "state.jsonl"
        led.write_text('{"type": "header", "run_id": "g1", "required_set": "unknown"}\n', encoding="utf-8")
        code = N.main(["--file", str(led)])
        check(code == 0, "the nudge printer must ALWAYS exit 0 — it reminds, it never blocks")


CASES = [
    ("heartbeat-always", "the heartbeat reminder fires every heartbeat", t_heartbeat_always_fires),
    ("header-always", "the header-reread reminder fires every heartbeat", t_header_reread_always_fires),
    ("labels-active-only", "the labels reminder fires only with an active PR", t_labels_fire_only_with_an_active_pr),
    ("required-set-unknown", "an unknown effective required set nudges to run the grouped read",
     t_required_set_unknown),
    ("required-set-per-base", "the unknown-required-set nudge is per distinct effective base",
     t_required_set_unknown_is_per_base),
    ("settled-base-report", "each active base is reported with its effective base and required set",
     t_settled_base_report),
    ("fanout-open-only", "fan-out nudges only with open work", t_fanout_fires_only_with_open_work),
    ("followups-open-only", "follow-ups nudge only when open, with the count", t_followups_fire_only_when_open),
    ("followups-unread-disclosed", "an unread follow-up store SAYS so, naming why — never silence",
     t_unread_followup_store_is_disclosed),
    ("notes-path-derived", "the notes file is --followups's sibling nudges.md and nothing else",
     t_notes_path_is_derived_from_the_followups_path),
    ("notes-absent-silent", "a missing notes file is the normal case and says nothing",
     t_absent_notes_file_says_nothing),
    ("notes-unread-disclosed", "notes that cannot be read SAY so, naming why — never silence",
     t_unread_notes_are_disclosed),
    ("notes-oversized-disclosed", "a notes file over the byte cap is disclosed unread, never a crash",
     t_oversized_notes_file_is_disclosed),
    ("notes-content-model", "blanks and # lines skip, a leading bullet strips, the rest is verbatim",
     t_notes_content_model),
    ("notes-appended-last", "standing notes follow every computed reminder",
     t_notes_come_after_every_computed_reminder),
    ("notes-cap-disclosed", "the note cap truncates and discloses the hidden count",
     t_notes_cap_truncates_and_discloses_the_hidden_count),
    ("parked-short-circuits", "a parked PR fires only its held reminder", t_parked_pr_fires_only_its_own_reminder),
    ("repairing-splits", "repairing splits on whether a decision is recorded", t_repairing_splits_on_decision),
    ("intent-missing", "intent nudge fires only without the file", t_intent_missing_fires_only_without_the_file),
    ("ci-pending", "pending CI nudges to re-derive", t_ci_pending_fires),
    ("work-due", "the work-due nudge fires whenever review is due (fu25)", t_work_due_fires_whenever_review_is_due),
    ("mergeable", "mergeable nudge respects required(tier)", t_mergeable_fires_when_counters_are_met),
    ("terminal-quiet", "a terminal PR fires no per-PR line", t_terminal_pr_fires_no_per_pr_line),
    ("quiet-fires-when-stale", "a run quiet past QUIET_AFTER with an open PR fires the sweep; fresh does not", t_quiet_run_fires_when_stale),
    ("quiet-silent-when-absent", "an absent/`-`/unparseable last_activity never fires the sweep", t_quiet_run_silent_when_absent),
    ("quiet-needs-open-pr", "a quiet run with nothing open has nothing to sweep", t_quiet_run_needs_an_open_pr),
    ("quiet-names-the-park", "a parked-only quiet run says it waits on the user and surfaces the question", t_quiet_run_names_the_park),
    ("watchdog-due-fires", "watchdog-due reminder fires on unset/overdue/invalid with open work, silent when ok", t_watchdog_due_fires_on_unset_overdue_invalid_with_open_work),
    ("watchdog-due-needs-open-work", "watchdog-due reminder is silent with no open/terminal-only rows", t_watchdog_due_silent_without_open_work),
    ("rundir-defaults-to-ledger-dir", "--file alone derives the run directory from the ledger's parent",
     t_rundir_defaults_to_the_ledger_directory),
    ("never-blocks", "a nudge always exits 0", t_a_nudge_never_blocks),
]
