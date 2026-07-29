#!/usr/bin/env python3
"""The nudge printer — sticky-note reminders the campaign orchestrator prints at the top of every heartbeat.

Every heartbeat is a fresh agent instance, and the campaign's obligations live in prose it re-derives by hand.
So it forgets: it forgets to arm the fallback heartbeat, it drives off a stale ledger instead of fanning
out, it lets a review agent die unnoticed, it forgets to swap a PR's labels as the gate moves. This tool
reads the durable state and PRINTS what the orchestrator should CHECK — the same audit the heartbeat skeleton
describes in prose, computed from disk so an amnesiac heartbeat is handed it rather than told to remember it.

**It is a REMINDER, not a supervisor.** Every rule is a CHEAP read (ledger fields, an open-follow-up
count, whether a `<rundir>` file exists) → a short "check X" string. It NEVER derives CI, counts verdicts,
decides merge-readiness, evaluates a liveness cap, or judges whether a review agent is actually alive —
those tools already exist and it only reminds the orchestrator to run them. It decides nothing about
whether a PR may merge; it is branch-owned, dogfoodable, and always exits 0. A reminder you can ignore is
the point: its value is being DELIVERED at the heartbeat, computed and concrete, not forcing anything.

**It also DELIVERS the durable notes file** `<project_root>/.gauntlet/nudges.md` — standing lessons the
campaign has learned about its own process, which every computed rule above is by construction unable to
hold: a rule is code, and a lesson is not shipped with the plugin. Each usable line becomes one reminder,
appended AFTER the computed ones so live state stays where it is read first. Nothing here WRITES that
file; the driver or the user appends to it by hand. Its path is DERIVED from `--followups` (its sibling
in the same durable `.gauntlet/` tier, `references/files-and-ledger.md`), never a flag of its own — a
flag is one more thing the canonical invocation can be missing, and a delivery channel that arrives
unpassed is a channel that silently never fires.

The design and the full obligation inventory it was trimmed from live in `.gauntlet/DESIGN-nudge-printer.md`.
"""

from __future__ import annotations

import argparse
import io
import os
import stat
import sys
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _gauntlet.modules import load_sibling
from _gauntlet.testing import run_sibling_suite

DESCRIPTION = "Print per-heartbeat reminders for the campaign orchestrator (sticky notes, not a supervisor)."

_HERE = Path(__file__).resolve().parent
SIBLING = _HERE / "nudge-test.py"


L = load_sibling("nudge_ledger", _HERE, "ledger.py")
F = load_sibling("nudge_followups", _HERE, "followups.py")

# A held PR is FROZEN — it fires ONLY its held reminder, never review/CI/merge nudges. The enumeration
# lives in ledger.py; never retype it (a new held status inherits the freeze with no edit here).
HELD = L.HELD_STATUSES
REPAIRING = L.REPAIR_STATUS
TERMINAL = ("merged", "aborted")
OPEN_FOLLOWUP_HIDDEN = F.TABLE_HIDDEN_STATES  # a follow-up in one of these is closed, not open work

# The PARKED-on-a-human statuses — HELD minus `repairing` (which waits on the reassessment pass, not the
# user). DERIVED from HELD so a new held status cannot silently join or miss this set. The quiet-run rule
# treats these specially: a run idle only because every open PR is parked is idle BECAUSE it waits on YOU.
PARKED = tuple(s for s in HELD if s != REPAIRING)

# How long a run may show NO ledger activity before the quiet-run sweep fires. Derived, not guessed: two
# normal ~15-min heartbeat intervals (so a single slow heartbeat never trips it) plus ~5 min of slack.
QUIET_AFTER = timedelta(minutes=35)

# --- the durable notes file ---------------------------------------------------
# `<project_root>/.gauntlet/nudges.md` — standing lessons, hand-appended, delivered verbatim. It sits in
# the DURABLE tier next to `followups.jsonl` and `history/` (`references/files-and-ledger.md`), never
# under `.gauntlet/tmp/**` which is wiped between runs: a lesson has to outlive the run that learned it.
NOTES_NAME = "nudges.md"

# Every rendered note line starts with this, so standing advice is never mistaken for a LIVE condition
# computed off this run's ledger. The three note-family lines (a note, the not-read disclosure, the
# truncation disclosure) all lead with it, which is also what makes them greppable as one family.
NOTES_MARK = "standing note"

# At most this many note lines are RENDERED. A file that grows without bound would drown the computed
# reminders — the live state is the part that goes stale, so it is the part that must stay at the top and
# stay findable. Ten is the order of magnitude of a small run's computed list, so the notes sit at parity
# with live state rather than burying it. Over the cap the OMISSION IS DISCLOSED, never silent.
NOTES_CAP = 10

# The notes file is read WHOLE into memory, so its size is bounded before the read rather than after.
# 64 KiB is far past any hand-written file and far short of anything that could hurt; over it the printer
# DISCLOSES and reads nothing rather than risking a `MemoryError` in a tool that must always exit 0.
NOTES_MAX_BYTES = 64 * 1024

# A leading markdown bullet is STRIPPED so a file written as a bullet list and one written as plain lines
# both read correctly. Exactly these two markers, each with its trailing space, so `-x` stays the word it
# is; a line that is ONLY a marker is an empty bullet and carries no note, so it is dropped.
NOTES_BULLETS = ("- ", "* ")

# The non-regular entry types the classification NAMES when it refuses one. Naming the type is the whole
# value of the disclosure: "not a regular file" tells the user nothing they can act on, "a FIFO" tells
# them exactly what is sitting at the path. Order does not matter — the predicates are disjoint.
NOTES_ENTRY_KINDS = ((stat.S_ISDIR, "a directory"), (stat.S_ISFIFO, "a FIFO"), (stat.S_ISSOCK, "a socket"),
                     (stat.S_ISBLK, "a block device"), (stat.S_ISCHR, "a character device"))


def entry_kind(mode: int) -> str:
    """What the entry at the notes path IS, in words, for a disclosure that refuses to read it."""
    for is_kind, name in NOTES_ENTRY_KINDS:
        if is_kind(mode):
            return name
    return "of an unknown type"


def classify_entry(path: Path) -> "tuple[os.stat_result | None, str | None]":
    """`(the entry's stat, why it must NOT be opened)` — at most one of the two is ever set.

    Both `None` means there is NO ENTRY at `path`. That is not "readable" and not "refused"; it is
    "nothing was there", and each caller decides what an absence means for its own file. The follow-up
    store treats it as NOT READ, because its path is TYPED and a typo is the likely explanation. The notes
    file treats it as a completed look that found nothing, because its path is DERIVED and its absence is
    the normal state. Deciding that here would take the choice from both.

    CLASSIFY BEFORE OPENING, ALWAYS. `read_text` on a FIFO waits for a writer FOREVER, and this printer
    promises it always exits 0 and never blocks — a heartbeat printer that never returns stalls the
    campaign loop rather than misreporting one line. `exists()` is not that check: it is True for a FIFO,
    a socket, a device and a directory alike, and False for a dangling symlink that plainly IS an entry.

    The returned stat is the one this walk already took, so a caller that needs the size (the notes cap)
    does not stat again — and cannot drift into asking a second, different question.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"{path} could not be checked ({type(exc).__name__})"
    if stat.S_ISLNK(info.st_mode):
        try:
            info = path.stat()
        except FileNotFoundError:
            return None, f"{path} is a symlink whose target is missing — repoint it or remove it"
        except OSError as exc:
            return None, f"{path} could not be checked ({type(exc).__name__})"
    if not stat.S_ISREG(info.st_mode):
        return None, f"{path} is {entry_kind(info.st_mode)}, not a regular file — replace it with one"
    return info, None


def _activity_age(last_activity: str, now: datetime) -> "timedelta | None":
    """How long since the run last did something — or None if that is UNKNOWABLE, in which case the
    quiet-run rule stays silent. This is advisory and NEVER raises: a `-` (an old ledger that predates the
    sensor), an empty value, an unparseable stamp, or a naive one all read as "cannot tell" and skip the
    rule rather than fabricate an age. A stamp is UTC ISO-8601 (ledger.py writes `now_activity()`); a naive
    value cannot be subtracted from an aware `now`, so it is refused here rather than crashing the printer.
    """
    if not last_activity or last_activity == "-":
        return None
    try:
        ts = datetime.fromisoformat(last_activity)
    except ValueError:
        return None
    if ts.tzinfo is None:
        return None
    return now - ts


def required(tier: str) -> int:
    """1 if TRIVIAL, else 2. Untriaged ('-'/'') counts as needs-review (target 2) — never under-remind."""
    return 1 if tier == "TRIVIAL" else 2


def open_followups(followups_path: "Path | None") -> "tuple[int | None, str | None]":
    """`(open count, why it was NOT READ)` — and the second is `None` exactly when the first is a real count.

    A count of `None` is NOT `0`, and keeping them apart is the whole point: an unread store counted as
    zero is INDISTINGUISHABLE in the output from a genuinely empty queue, so an omitted or mistyped flag
    retired the durable follow-up queue with no error, no warning, and a confident header above it. The
    caller DISCLOSES the `None` (`followups_line`); it never invents a count from a store it did not open.

    The reason rides along because `not there` is no longer the only way this fails. `classify_entry`
    refuses a FIFO, a socket, a device, a directory and a dangling symlink BEFORE anything opens the path
    — a FIFO here used to hang the printer forever, which is the one outcome its contract forbids. Those
    all say what they are; only a genuinely absent file still reports as absent.
    """
    if followups_path is None:
        return None, "no --followups given"
    info, refusal = classify_entry(followups_path)
    if refusal is not None:
        return None, refusal
    if info is None:
        return None, f"no file at {followups_path}"
    loader_stderr = io.StringIO()
    try:
        with redirect_stderr(loader_stderr):
            entries = F.load(followups_path)
    except (OSError, ValueError, SystemExit) as exc:
        # The store is malformed or unreadable. It is still NOT READ, which is what the caller discloses;
        # what it must never become is a confident zero, and it never does.
        refusal = loader_stderr.getvalue().strip()
        if refusal:
            return None, f"{followups_path} could not be read ({refusal})"
        return None, f"{followups_path} could not be read ({type(exc).__name__})"
    return sum(1 for e in entries if e.get("state") not in OPEN_FOLLOWUP_HIDDEN), None


def followups_line(n_followups: "int | None", unread: "str | None") -> "str | None":
    """The one follow-up reminder: a COUNT when the store was read, a DISCLOSURE naming why when it was
    not, and silence ONLY for a read that genuinely found nothing open. An unread store is never silent —
    that silence is what a caller reads as "this run has no open follow-ups".

    The reason comes FROM the read (`open_followups`), never re-derived here. Re-deriving it is how a
    refusal that knew exactly what was wrong — a FIFO, a dangling symlink — printed `no file at <path>`
    about a path that plainly had something at it.
    """
    if n_followups is None:
        return f"follow-up store NOT READ ({unread}) — open follow-ups are UNKNOWN, not zero."
    if n_followups:
        return f"{n_followups} open follow-up(s) — start any you can."
    return None


def notes_path(followups_path: "Path | None") -> "Path | None":
    """Where the standing-notes file IS — DERIVED from `--followups`, or `None` when that was not given.

    `nudges.md` and `followups.jsonl` are SIBLINGS in the one durable `.gauntlet/` tier
    (`references/files-and-ledger.md`), so the follow-up store's directory IS the notes file's directory.
    That derivation is the whole reason there is no `--notes` flag: a flag would have to be added to the
    canonical invocation (`loop-control.md`, Step 1), and a delivery channel whose flag is missing there
    never fires at all — silently, forever, with nothing in the output to say so. A path that is already
    passed cannot be forgotten.

    There is NO fallback base directory. Without `--followups` the project root is genuinely unknown, and
    guessing one (the cwd, the run directory) would read some OTHER file, or none, and report that as the
    user's notes. The caller DISCLOSES the `None` instead (`read_notes`).
    """
    if followups_path is None:
        return None
    return followups_path.parent / NOTES_NAME


def parse_notes(text: str) -> list:
    """The usable note lines: one reminder per line, VERBATIM, minus the shapes that are not reminders.

    Blank lines and `#` lines are skipped so the file can carry markdown headings and comments and still
    read well to the human who maintains it; a leading `- `/`* ` bullet is stripped so a bullet list and a
    plain list both work. Everything else is taken exactly as written — this file is the ONE place the
    campaign's own words reach the heartbeat unedited, and a printer that reworded them would be a printer
    the user cannot predict.
    """
    out: list = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for marker in NOTES_BULLETS:
            if line == marker.rstrip():  # an empty bullet — a marker and nothing else
                line = ""
                break
            if line.startswith(marker):
                line = line[len(marker):].strip()
                break
        if line:  # whatever emptied it — a blank, a comment, an empty bullet — is not a note
            out.append(line)
    return out


def read_notes(path: "Path | None") -> "tuple[list, str | None]":
    """`(note lines, why they were NOT READ)` — and the second is `None` exactly when they WERE read.

    This keeps "not read" and "nothing there" apart, the same discipline `open_followups` /
    `followups_line` enforce for the follow-up store: a store nobody opened, counted as empty, is
    INDISTINGUISHABLE in the output from a real zero.

    It splits from that precedent on ONE case, deliberately. `open_followups` treats a `--followups` path
    that is not there as NOT READ, because that path is TYPED and a typo is the likely explanation. This
    path is DERIVED and cannot be mistyped, and the notes file is optional user data whose ABSENCE IS THE
    NORMAL STATE — so a missing file IN A DIRECTORY THAT IS THERE is a completed look that found nothing,
    and it stays SILENT. A disclosure every heartbeat of a run whose user never wrote a note is a false
    alarm, and a disclosure line that is usually noise is one the driver learns to skip past. That
    exemption is the FILE's alone: an unusable containing directory is not an absent file, it is a look
    that never happened, and it discloses with everything else.

    It NEVER raises AND IT NEVER BLOCKS. A permissions error, invalid UTF-8, or a file too large to hold
    all degrade to a named reason — the printer must always exit 0. `classify_entry` owns what is at the
    path and what may not be opened; do not restate its rules here.

    ONE decision is this function's alone, and it is why the classify result is not simply returned. When
    `classify_entry` reports NO ENTRY, this splits on THE CONTAINING DIRECTORY. A directory that IS there
    and holds no `nudges.md` is a look that COMPLETED and found nothing — the normal, silent case. A
    containing directory that is not a directory at all means the look never happened, so it DISCLOSES.
    `errno` cannot draw that line: a missing file inside an existing directory and a missing directory
    both raise `FileNotFoundError` with `ENOENT`. One stat on the parent draws it exactly.

    ACCEPTED RESIDUAL, deliberately not defended against: the entry can change type between the
    classification and the read. Closing it would mean an `os.open` with `O_NONBLOCK` and an `fstat` type
    check on the descriptor actually read; under this repository's single-user calibration a user
    swapping a file for a FIFO underneath their own heartbeat is their own call.
    """
    if path is None:
        return [], "no --followups given, so the .gauntlet/ directory is unknown"
    info, refusal = classify_entry(path)
    if refusal is not None:
        return [], refusal
    if info is None:
        if path.parent.is_dir():
            return [], None  # the look SUCCEEDED and found no file — the normal case, and it says nothing
        # The directory the notes live in is not a directory, so nothing was ever looked at. This is a
        # correctly invoked campaign's impossible state — the run directory itself lives under it — which
        # is exactly why it is worth saying: it fires only for a wrong project root or a deleted
        # `.gauntlet/`, never for the user who simply keeps no notes.
        return [], f"{path.parent} is not a directory, so the notes file could not be looked for"
    size = info.st_size
    if size > NOTES_MAX_BYTES:
        return [], f"{path} is {size} bytes, over the {NOTES_MAX_BYTES}-byte cap — trim it"
    try:
        # UnicodeDecodeError is a ValueError, not an OSError; both are caught, so an undecodable file is
        # disclosed rather than silently mangled by an `errors=` fallback.
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Raced away between the stat and the read; still just an absent file. No parent check here: the
        # `lstat` above already found an entry at this path, so the containing directory WAS a directory.
        return [], None
    except (OSError, ValueError) as exc:
        return [], f"{path} could not be read ({type(exc).__name__})"
    return parse_notes(text), None


def notes_lines(notes: list, notes_unread: "str | None", path: "Path | None") -> list:
    """The note-family reminder lines: the notes themselves, or the DISCLOSURE of why there are none.

    Silence means one thing only — the look COMPLETED and found no usable line. Every other outcome speaks:
    an unread store says so and names why, and a file over `NOTES_CAP` says how many lines it is hiding.
    """
    if notes_unread is not None:
        return [f"{NOTES_MARK}s NOT READ ({notes_unread}) — standing notes are UNKNOWN, not absent."]
    out = [f"{NOTES_MARK}: {n}" for n in notes[:NOTES_CAP]]
    hidden = len(notes) - NOTES_CAP
    if hidden > 0:
        # THE OMISSION IS NEVER SILENT — the count of what was dropped, and where to go read it.
        out.append(f"{NOTES_MARK}s TRUNCATED — {hidden} of {len(notes)} line(s) in {path} not shown "
                   f"(cap {NOTES_CAP}); trim the file.")
    return out


def rundir_has(rundir: "Path | None", name: str) -> bool:
    return rundir is not None and (rundir / name).exists()


def resolve_rundir(file_arg: str, rundir_arg: "str | None") -> Path:
    """The run directory to check `<rundir>` files against — the explicit `--rundir`, else DERIVED from the
    ledger path. The ledger IS `<rundir>/state.jsonl`, so its parent IS the run directory; there is no
    invocation where `--file` is usable and the run directory is unknowable, and `--file` is already
    mandatory. Deriving it is what keeps a `--file`-only call from reporting every intent file MISSING —
    `rundir_has` reads a `None` rundir as "the file is not there", which is indistinguishable in the output
    from a genuinely missing intent file. An explicit `--rundir` still wins, for the caller who really does
    keep the two apart.
    """
    if rundir_arg:
        return Path(rundir_arg)
    return Path(file_arg).resolve().parent


def reminders(header: dict, rows: list, n_followups: "int | None", rundir: "Path | None",
              now: "datetime | None" = None, followups_path: "Path | None" = None,
              notes: "list | None" = None, notes_unread: "str | None" = None,
              followups_unread: "str | None" = None) -> list:
    """Compute the reminder lines. Pure: same inputs → same output. Returns a list of strings.

    `now` is the current UTC time, injectable so the quiet-run rule is testable; it defaults to
    `datetime.now(timezone.utc)` when a caller (main) does not pass one.

    `followups_path` is only what the follow-up store was LOOKED FOR at — it is never read here, and it
    exists so the notes file's path can be named in its own disclosures (`notes_path`).

    `followups_unread` is `open_followups`'s reason, and it is what an unread store discloses. It is
    passed in rather than re-derived from the path, because only the read knows WHY: a path that could not
    be opened at all — a FIFO, a socket, a dangling symlink — is not the same as one with nothing at it,
    and re-deriving turned every one of those into `no file at <path>`.

    `notes` / `notes_unread` are `read_notes`'s result, read by the caller (main) exactly as
    `n_followups` is — the store reads stay in one place and this stays renderable from fixed inputs. The
    default `([], None)` is "read, and there was nothing", which is silent.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    out: list = []
    active = [r for r in rows if r["status"] not in TERMINAL]

    # --- always-fire floor -----------------------------------------------------
    out.append('follow loop-control.md, "Primary continuity", before yielding.')
    out.append("re-read the ledger header (base_branch, reviewer, required_set, skill_version).")
    if active:
        # The base a PR merges into and its required-check set are per-ROW state now (`effective_base` /
        # `effective_required_set`); the header `base_branch`/`required_set` are only the LEGACY FALLBACK a
        # row with none inherits. So the heartbeat re-resolves them PER active PR, not once from the header.
        out.append("check each active PR's labels match its gate state.")
        out.append("re-resolve each active PR's effective base and required set (row-owned; the header "
                   "base_branch/required_set are only the legacy fallback for a row that carries none).")

    # --- run-level -------------------------------------------------------------
    # Required checks are per-BASE row state. Group active rows by (effective base, effective required set)
    # and REPORT every pairing — naming each active row's effective base and required set is the point, not
    # only the blocked ones. A settled pairing is stated verbatim (the raw ledger spelling — nudge never
    # parses a required-set spec); a base whose effective set is still `unknown` (a read failed or never
    # ran) FAILS CLOSED — it cannot go green — and blocks only ITS group, so ITS line also says to run the
    # grouped read. `-` is NOT `unknown`: it means "inherit the header", which `effective_required_set`
    # resolves; a new run's rows read the header's `unknown` until the grouped read (ci-status.py
    # required-set) writes each base's canonical set.
    base_groups: "dict[tuple, list]" = {}
    for r in active:
        key = (L.effective_base(header, r), L.effective_required_set(header, r))
        base_groups.setdefault(key, []).append(r["pr"])
    for base, rset in sorted(base_groups):
        prs = ", ".join(base_groups[(base, rset)])
        if rset == "unknown":
            out.append(f"required set unknown for base {base} (PR(s) {prs}) — run the grouped required-set "
                       f"read (ci-status.py required-set).")
        else:
            out.append(f"base {base} (PR(s) {prs}): required set {rset}.")
    if active:
        out.append(f"{len(active)} PR(s) open — reconcile and fan out work up to caps.")
    fu_line = followups_line(n_followups, followups_unread)
    if fu_line:
        out.append(fu_line)

    # --- watchdog-due reminder -------------------------------------------------
    # The durable long-cadence health-pass deadline (ledger.py's `watchdog_due`). When it is due, never armed,
    # or unreadable AND there is open work, the run owes a deep health pass — the long sensor that catches a
    # run heartbeats keep firing on but never look deeply at. The parse is ledger.py's own `watchdog_state`
    # (never a second copy of it), and like every nudge rule it is a CHEAP read that never raises: a malformed
    # or naive deadline reads `invalid`, which fires the same "re-arm it" reminder rather than crashing. Nudge
    # knows NOTHING about scheduler entries — that is the health pass's adapter business, not a reminder's.
    if active:
        wd_state, _ = L.watchdog_state(header.get("watchdog_due", "-"), now)
        if wd_state in ("due", "unset", "invalid"):
            out.append("watchdog due — run the health pass, then `ledger.py watchdog arm`.")

    # --- quiet-run sweep -------------------------------------------------------
    # A run whose ledger has not moved for QUIET_AFTER is not necessarily healthy — a review may have died,
    # a poll may be stuck, or the user may be sitting on a parked question. Every heartbeat is a fresh
    # context, so "how long has nothing moved?" is read from the durable `last_activity` stamp, not memory.
    # The rule reminds the orchestrator to SWEEP before rescheduling into another silent interval; it decides
    # nothing. It stays silent unless there is BOTH a readable stamp that old AND at least one open PR — an
    # idle run with nothing open has nothing to sweep, and a `-`/absent stamp is an old ledger, not a stall.
    age = _activity_age(header.get("last_activity", "-"), now)
    if age is not None and age >= QUIET_AFTER and active:
        quiet_min = int(age.total_seconds() // 60)
        parked = [r for r in active if r["status"] in PARKED]
        out.append(f"run has been QUIET for ~{quiet_min}m (no ledger activity) — SWEEP before rescheduling.")
        if len(parked) == len(active):
            # The run is not stalled — it is WAITING ON THE USER. The unanswered question is the thing to
            # surface, not a stall to chase; say so explicitly so the sweep is not misread as a fault.
            out.append("every open PR is parked — the run is idle BECAUSE it is waiting on YOU; the "
                       "unanswered question below is the thing to surface, not a stall to fix.")
        out.append("run `review-pass.py status --run <rundir> --verify` — apply the launch-deadline and "
                   "meaningful-progress rules to any in-flight pass.")
        out.append("re-derive CI for every row that can still move.")
        for r in parked:
            why = r.get("ci_reason", "-")
            tail = f": {why}" if why and why != "-" else ""
            out.append(f"PR {r['pr']}: parked — LEAD your next status to the user with this question and how "
                       f"long it has waited (≥ ~{quiet_min}m quiet){tail}.")

    # --- per-PR reminders ------------------------------------------------------
    # A HELD PR (parked or repairing) fires ONLY its held reminder. That exclusion is enforced by the
    # `status == "in_review"` guard on every in-flight rule below — a held PR is never in_review, so it
    # reaches none of them. No explicit short-circuit is needed (and one would be an untestable no-op).
    for r in active:
        pr = r["pr"]
        status = r["status"]
        if status == REPAIRING:
            if r.get("repair_decision", "-") == "-":
                out.append(f"PR {pr}: repairing, no decision — run `repair-pass.py bundle`.")
            else:
                out.append(f"PR {pr}: repairing — prepare then dispatch decision ({r['repair_decision']}), "
                           "nothing else.")
        elif status in HELD:  # awaiting-user / awaiting-api
            why = r.get("ci_reason", "-")
            tail = f" ({why})" if why and why != "-" else ""
            out.append(f"PR {pr}: parked{tail} — surface the question, don't mutate it.")

        # in-flight rules — each gated on in_review, so held PRs above fire none of them
        need = required(r["tier"])
        ok = int(r["reviews_ok"]) if r["reviews_ok"].isdigit() else 0
        ci = r["ci"]
        if status == "in_review" and ok < need and not rundir_has(rundir, f"intent-{pr}.md"):
            out.append(f"PR {pr}: no intent-{pr}.md — write it before reviewing.")
        if status == "in_review" and ci == "pending":
            out.append(f"PR {pr}: CI pending — re-derive it.")
        if status == "in_review" and ok < need and ci != "red":
            # Work is DUE. The nudge cannot tell from disk whether a review/audit/fix is actually running
            # (that liveness lives in the session, not the ledger) or which KIND is running — so it does not
            # try. It reminds to make sure SOMETHING is live, or to launch one. An earlier version probed
            # review-<pr>-<review_rounds>.progress.jsonl and missed a first review (review_rounds=0) — the
            # exact miss dogfooding and the review both caught (fu25).
            out.append(f"PR {pr}: work due — make sure a dispatched review/audit/fix is live, or launch one.")
        if status == "in_review" and ok >= need and ci == "green":
            out.append(f"PR {pr}: mergeable by counters — check merge-readiness.")

    # --- standing notes --------------------------------------------------------
    # LAST, always. Every line above is a LIVE condition computed from this run's durable state and is the
    # part that goes stale, so it keeps the top of the list where it is read first; standing advice is the
    # same at every heartbeat and can wait. The `NOTES_MARK` lead-in is what keeps the two apart on sight.
    out.extend(notes_lines(notes or [], notes_unread, notes_path(followups_path)))

    return out


def render(header: dict, rows: list, n_followups: "int | None", rundir: "Path | None",
           now: "datetime | None" = None, followups_path: "Path | None" = None,
           notes: "list | None" = None, notes_unread: "str | None" = None,
           followups_unread: "str | None" = None) -> str:
    lines = reminders(header, rows, n_followups, rundir, now, followups_path, notes, notes_unread,
                      followups_unread)
    run_id = header.get("run_id", "-")
    head = f"NUDGE (run {run_id}) — {len(lines)} reminder(s):"
    body = "\n".join(f"  - {line}" for line in lines)
    return head + ("\n" + body if body else "")


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--file", help="the run ledger (<rundir>/state.jsonl)")
    parser.add_argument("--followups", help="the follow-up store (<project_root>/.gauntlet/followups.jsonl). "
                                            "Used AS GIVEN — a relative path resolves against the CWD, so "
                                            "pass an absolute one. Omitted, or not found, the reminder SAYS "
                                            "the store was not read; it never reports zero. It ALSO locates "
                                            "the standing-notes file, its sibling <dir>/" + NOTES_NAME + ", "
                                            "whose lines are appended to the reminders. Those ride on the "
                                            "DIRECTORY, so a wrong filename still delivers them; omitted, "
                                            "or a <dir> that is not a directory, and they are reported not "
                                            "read too")
    parser.add_argument("--rundir", help="the run directory, for intent/CI/progress file checks "
                                         "(default: the directory holding --file)")
    parser.add_argument("--self-test", action="store_true", help="run every fixture and assert the rules "
                                                                 "this file enforces still hold")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    if args.file is None:
        parser.error("the following arguments are required: --file")
    header, rows = L.load(Path(args.file))
    followups_path = Path(args.followups) if args.followups else None
    n_followups, followups_unread = open_followups(followups_path)
    notes, notes_unread = read_notes(notes_path(followups_path))
    rundir = resolve_rundir(args.file, args.rundir)
    print(render(header, rows, n_followups, rundir, followups_path=followups_path,
                 notes=notes, notes_unread=notes_unread, followups_unread=followups_unread))
    return 0  # a nudge NEVER blocks — it only reminds


# --- self-test ----------------------------------------------------------------

class SelfTestFailure(AssertionError):
    """A rule this file claims to enforce does not hold."""


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise SelfTestFailure(msg)


def self_test() -> int:
    """Run every fixture in the sibling suite on the shared runner (`_gauntlet/testing.py`)."""
    return run_sibling_suite(SIBLING, "nudge_test", failure=SelfTestFailure,
                             subject="the nudge printer's contract")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
