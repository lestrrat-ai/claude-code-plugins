#!/usr/bin/env python3
# ci: pyright
"""THE DURABLE REVIEW-LEARNINGS STORE — schema-owning accessor for `.gauntlet/review-learnings.jsonl`.

WHY THIS EXISTS, AND WHY IT IS NOT `followups.jsonl`. When the campaign settles a question — a finding
REFUTED because its mechanism cannot occur, or completed under a legacy DEMOTE as a true-but-immaterial
accepted residual — that answer is worth remembering, so a fresh, stochastic reviewer does not make the
campaign re-litigate the same class PR after PR. The follow-up store cannot hold it: `followups.md` is a
work QUEUE whose entries are DELETED the moment the work lands elsewhere ("delete once a durable record
exists ELSEWHERE"). A learning has no "elsewhere" and no completion — it is a standing fact about a CLASS
of finding — so a queue would discharge and lose it. This store is the opposite: it ACCUMULATES and it
NEVER auto-deletes. An entry leaves the consulted set only when the user REVOKES it or the anchored code
changes enough to make it STALE; either way the record is KEPT for audit.

WHAT IT IS TIED TO, AND WHAT IT OUTLIVES. It is the **gauntlet tier** — a git-ignored file under
`.gauntlet/`, a sibling of `followups.jsonl` and `history/`, NEVER under `.gauntlet/tmp/**` (that tree is
wiped). Git-ignored means local-per-machine: it survives every run on this machine but not a fresh clone.
That is the deliberate boundary between the three tiers, and promotion across them is the USER'S call:
gauntlet-local learning (here, autonomous) → repo-tier calibration (`AGENTS.md` "SINGLE-USER, advisory
workflow", committed, survives clones — the user's `promote --tier repo` records consent) → account-tier
principle (the user's out-of-checkout global memory, only on explicit request). See `review-learnings.md`.

THE GATE-SAFETY BOUNDARY. A learning is consulted ONLY by the DRIVER — when it authors a new PR's intent
Non-goals (`pr-adoption.md`) and as non-dispositional precedent for the finding audit (`finding-audit.md`).
It is NEVER injected into a review pass to tell a reviewer to stand down: that would BLIND the gate. A
learning never changes a verdict; the driver and user apply the relevance, falsifiability, and
never-suppress-a-real-guarantee rules before using it to author intent. So this store is not gate
machinery; it feeds the driver's own decision.

Like the follow-up store this file has MANY writers (every concurrent run) and NO other copy of its data,
so the read-modify-write is LOCKED and a corrupt line is REFUSED (naming the line), never skipped. Every
value is a STRING. This accessor is the ONLY door: read and write BY FIELD NAME, never hand-edit the JSONL.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _gauntlet.atomic import replace_text  # noqa: E402
from _gauntlet.jsonl import JsonlError, object_lines  # noqa: E402
from _gauntlet.modules import load_module_from_path  # noqa: E402
from _gauntlet.table import config_lines, grid_lines, hidden_notice  # noqa: E402

DESCRIPTION = (
    "The durable review-learnings store (.gauntlet/review-learnings.jsonl): refuted and legacy-demoted finding "
    "CLASSES the DRIVER consults when authoring intent, so a settled question is not re-litigated. It "
    "ACCUMULATES and never auto-deletes; promotion to a higher tier is the USER's."
)

# --- schema (owned here, once) ------------------------------------------------

PLACEHOLDER = "-"  # what an unset field holds, and what `is_blank()` reads as "carries nothing"
MISSING = ""       # what an ABSENT required key reads as — blank, which `entry_error()` then refuses

# The record types: a learning, and the ONE non-learning line — the id high-water mark (`read_store()`),
# which is what stops a REVOKED-then-superseded id from ever being handed out twice. Anything else on a
# line is a corrupt store, refused; never skipped.
ENTRY_TYPE = "learning"
SEQ_TYPE = "learning-seq"

# The learning's fields. `claim`..`provenance` are the durable content; `state` is the lifecycle;
# `recorded` stamps creation; `evidence` is the APPEND-only log of every stale/reaffirm/revoke; `decided`
# stamps the user's ruling (revoke/promote); `promoted` records the tier the user consented to.
FIELDS = ("id", "claim", "justification", "anchor", "falsifiability", "provenance",
          "state", "recorded", "evidence", "decided", "promoted")

# Fields a learning cannot be WITHOUT, enforced at EVERY door (derived, never hand-listed):
#   claim         — the settled finding CLASS, one line
#   justification — why it is not a real defect / is an accepted residual under the calibration
#   anchor        — the files/area/finding-class it applies to (what a consulting driver matches on)
#   falsifiability— the code change that would make it a REAL defect again (so a stale learning is
#                   re-judged, never trusted forever) — the expiry condition, in prose
#   provenance    — which run / PR / audit settled it, so it can be audited and revoked
# A learning missing any of these is a RUMOR a future reviewer cannot audit — the one thing this store
# must never accumulate.
REQUIRED = ("claim", "justification", "anchor", "falsifiability", "provenance")

# A non-REQUIRED field absent from a line gets a DEFAULT (so the schema can grow without migrating a store
# that cannot be rebuilt); a REQUIRED one absent reads as MISSING (blank), which `entry_error()` refuses.
DEFAULTS = {**{f: PLACEHOLDER for f in FIELDS if f not in REQUIRED}, "state": "active"}

RECORD_KEYS = frozenset(FIELDS) | {"type"}
SEQ_KEYS = frozenset({"type", "high"})

# The tiers a learning may be PROMOTED to — always the user's consent (`promote`). `gauntlet` is not here:
# it is where every learning already lives (this store), so it is the default, not a promotion target.
TIERS = ("repo", "account")


def project(rec: "dict", where: str = "") -> "dict[str, str]":
    """A record — off disk, or out of a door — as THE FIELDS. ONE definition, used both ways.

    EVERY VALUE IS A STRING: `argparse` has nothing but a `str` to hand a write door, so a non-string on a
    line is a record this accessor did not write, and it is REFUSED rather than coerced (`str(None)` is
    `"None"`, `str(123)` is `"123"` — both non-blank, both then indistinguishable from real content).
    """
    unknown = sorted(set(rec) - RECORD_KEYS)
    if unknown:
        fail(f"{where}unknown key(s) {', '.join(repr(k) for k in unknown)} — a learning carries "
             f"{len(FIELDS)} declared fields and nothing else. Valid: {', '.join(sorted(RECORD_KEYS))}.")
    entry: "dict[str, str]" = {}
    for f in FIELDS:
        raw = rec[f] if f in rec else DEFAULTS.get(f, MISSING)
        if not isinstance(raw, str):
            fail(f"{where}{f} is {raw!r} — every value in a learning is a STRING. To leave {f} unset, "
                 f"OMIT THE KEY; an unset field reads back as {PLACEHOLDER!r}.")
        entry[f] = raw
    return entry


# --- the lifecycle (owned here, once) -----------------------------------------
#
# THE STORE NEVER DELETES. A learning is not work that completes; it is a standing fact. It ACCUMULATES,
# and it leaves the consulted set only by moving state — never by removal — so nothing is ever lost:
#
#   * `stale`    — the anchored code changed materially, so the falsifiability condition MAY now be met.
#                  The learning is set aside pending re-evaluation; it is NOT consulted while stale.
#   * `reaffirm` — a fresh investigation confirmed the stale learning still holds; back to `active`.
#   * `revoke`   — THE USER overturned it (or a later investigation showed the harm is now real). KEPT for
#                  audit, never consulted again, never silently re-recorded.
#   * `promote`  — THE USER consented to widen the learning's reach to a higher tier (`repo`/`account`).
#                  It stays `active` and consulted locally; `promoted` records the consent, durably, so a
#                  fresh agent does not re-ask.
#
# `<subcommand>: (states it may be applied FROM, the state it moves TO)`.
INITIAL = "active"
TRANSITIONS = {
    "stale":    (("active",), "stale"),
    "reaffirm": (("stale",), "active"),
    "revoke":   (("active", "stale"), "revoked"),
    "promote":  (("active",), "active"),
}

# The transitions that are the USER'S RULING — the ONLY ones that stamp `decided`. A `decided` written by
# anything else would launder a driver step into the user's consent. `revoke` retires a learning; `promote`
# widens it — both are the user's, and NO sequence of driver-only steps reaches `revoked` or writes
# `promoted` (proved on the graph by the fixtures).
USER_RULINGS = ("revoke", "promote")

# Everything else is the DRIVER's, plus `record` (creation). Derived, never listed.
DRIVER_STEPS = tuple(c for c in TRANSITIONS if c not in USER_RULINGS)

STATES = (INITIAL,) + tuple(dict.fromkeys(to for _, to in TRANSITIONS.values()))

# The states nothing leaves. DERIVED — a state is terminal because no transition applies to it. `revoked`
# is terminal (kept, never consulted, never resumed); `active`/`stale` are not.
TERMINAL = tuple(s for s in STATES if not any(s in frm for frm, _ in TRANSITIONS.values()))

# What each transition MUST write besides the state move. `stale`/`reaffirm`/`revoke` append to the
# `evidence` log; `promote` writes the tier into `promoted`. ONE owner: the CLI, the writer and the
# fixtures all read this, so an evidence-bearing edge cannot be added with a flag the fixtures do not pass.
WRITES = {
    "stale":    ("evidence",),
    "reaffirm": ("evidence",),
    "revoke":   ("evidence",),
    "promote":  ("promoted",),
}


def flag_of(field: str) -> str:
    return "--" + field.replace("_", "-")


# Fields a caller may EDIT after the fact — the PROSE of the claim a later run may legitimately sharpen
# WHILE THE ENTRY IS UNRULED. A durable USER ruling (a `revoke`, or the user's `promote`/`decided`) FREEZES
# ALL of these: `set` refuses to touch ANY of them on a ruled entry, so a driver `set` cannot rewrite what
# the user ruled on — the tier the user consented to rested on the `justification`, and a revoked record is
# KEPT for audit. `provenance` is deliberately ABSENT (it records what happened, not an opinion to revise),
# and so are `state`/`recorded`/`evidence`/`decided`/`promoted` — records of the lifecycle, not settable
# strings. This exhausts the settable content: `INTAKE["set"]` wires exactly these flags and nothing else.
EDITABLE = ("claim", "justification", "anchor", "falsifiability")

# The subset of the content that DEFINES the class — the pair `record` and the twin guards match on. The
# class-protection invariants (revoked-twin refusal, promote consent, one live per class) key on THIS pair.
# The ruling-freeze (in `cmd_set`) covers all of EDITABLE, so editing `claim`/`anchor` on a ruled entry is
# refused by that freeze before the twin check is ever reached — this pair is the class identity, not the
# freeze key.
CLASS_FIELDS = ("claim", "anchor")

# --- intake: EVERY value the CLI takes IN (owned here, once) -------------------
#
# Per WRITE subcommand, the store field each caller flag lands in. THIS TABLE IS THE WRITE DOOR'S SCHEMA:
# `build_parser()` wires the flags from it (`dest` = THE FIELD), and `taken()` — the ONE door every caller
# value enters through — loops over it and refuses a blank. A value is validated BECAUSE IT IS IN THE
# SCHEMA, never because a door remembered to ask. `--reason`/`--finding` both land in the same append-only
# `evidence` log; only the caller's word for the step differs.
INTAKE = {
    "record":   {f: flag_of(f) for f in REQUIRED},
    "set":      {f: flag_of(f) for f in EDITABLE},
    "stale":    {"evidence": "--reason"},
    "reaffirm": {"evidence": "--finding"},
    "revoke":   {"evidence": "--reason"},
    "promote":  {"promoted": "--tier"},
}
WRITE_CMDS = tuple(INTAKE)

# Why a blank value is refused, per field — the reason the CALLER is told. One reason per INTAKE field; a
# fixture pins that a new writable field carries one.
BLANK_WHY = {
    **{f: "a review-learning without it is a RUMOR a future reviewer cannot audit" for f in REQUIRED},
    "evidence": "a state change with no reason is an unaudited edit to a store that has no other copy",
    "promoted": f"promotion must name the tier the user consented to ({'/'.join(TIERS)})",
}

INTAKE_HELP = {
    "claim": "the settled finding CLASS, one line — what a fresh reviewer must not re-litigate",
    "justification": "why it is not a real defect / is an accepted residual under the calibration",
    "anchor": "the files/area/finding-class it applies to — what a consulting driver matches on",
    "falsifiability": "the code change that would make it a REAL defect again (the expiry condition)",
    "provenance": "which run / PR / audit settled it — so it can be audited and revoked",
    "evidence": f"why this step was taken — APPENDED to the log, never clobbering: {BLANK_WHY['evidence']}",
    "promoted": f"the tier ({'/'.join(TIERS)}) — {BLANK_WHY['promoted']}",
}

ID_RE = re.compile(r"^rl[1-9][0-9]*$")

# What the DEFAULT table view hides: `revoked` — closed, kept for audit, nothing left to do about it.
# `stale` is NOT hidden: it is open work (a learning awaiting re-evaluation).
TABLE_HIDDEN_STATES = ("revoked",)
TABLE_DEFAULT_FIELDS = ("id", "state", "recorded", "claim", "anchor")
TABLE_EMPTY_MARKER = "# (no review-learnings)"
TABLE_ALL_HIDDEN_MARKER = (
    "# (no review-learnings shown — the store is NOT empty; every entry it holds is revoked and hidden)"
)
TABLE_MARKERS = (TABLE_EMPTY_MARKER, TABLE_ALL_HIDDEN_MARKER)
TABLE_RULE = "DRIVER-CONSULTED, never injected into a review pass. Promotion beyond gauntlet-local is the USER's"


def fail(msg: str) -> NoReturn:
    print(f"review-learnings: {msg}", file=sys.stderr)
    raise SystemExit(1)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_blank(value: str) -> bool:
    """A field carries nothing: whitespace, or the placeholder an unset field holds. THE ONE predicate."""
    return value.strip() in ("", PLACEHOLDER)


# --- parse / serialize --------------------------------------------------------

def entry_error(entry: "dict[str, str]") -> "str | None":
    """Why this entry cannot be in the store — or None. The STRUCTURAL check every line on disk is put to."""
    if not ID_RE.match(entry["id"]):
        return f"malformed id {entry['id']!r} (expected rl<N>)"
    if entry["state"] not in STATES:
        return f"unknown state {entry['state']!r}; valid: {', '.join(STATES)}"
    empty = [f for f in REQUIRED if is_blank(entry[f])]
    if empty:
        return f"{entry['id']} carries no {', '.join(empty)} — {BLANK_WHY[empty[0]]}"
    return None


@contextmanager
def clean_io(what: str, path: Path) -> "Iterator[None]":
    """Turn an `OSError` into a REFUSAL rather than a traceback — a bad path is not a corrupt store."""
    try:
        yield
    except OSError as e:
        fail(f"cannot {what} {str(path)!r}: {e.strerror or e}. Nothing was touched.")


def read_store(path: Path) -> "tuple[list[dict[str, str]], int]":
    """Return the entries AND the id high-water mark. A missing file is an EMPTY store — not an error.

    Every record is `{"type": "learning", …}` or the ONE meta record (`{"type": "learning-seq"}`); an
    unknown type is REJECTED with the line it is on, never skipped — a skipped line is a learning nothing
    reads, in a store that has no other copy. The high-water mark is why a retired id is never reused.
    """
    entries: "list[dict[str, str]]" = []
    high = 0
    seen: "set[str]" = set()
    marked = False
    if not path.exists():
        return entries, high
    with clean_io("read the store at", path):
        text = path.read_text()
    try:
        for n, rec in object_lines(text):
            if rec.get("type") == SEQ_TYPE:
                if marked:
                    fail(f"line {n}: a second {SEQ_TYPE} record — the store holds ONE high-water mark")
                marked = True
                unknown = sorted(set(rec) - SEQ_KEYS)
                if unknown:
                    fail(f"line {n}: {SEQ_TYPE} carries unknown key(s) "
                         f"{', '.join(repr(k) for k in unknown)} — it holds {', '.join(sorted(SEQ_KEYS))} "
                         f"and nothing else.")
                mark = rec.get("high")
                if isinstance(mark, bool) or not isinstance(mark, int):
                    fail(f"line {n}: {SEQ_TYPE} carries a non-numeric high-water mark {mark!r}")
                high = mark
                continue
            if rec.get("type") != ENTRY_TYPE:
                fail(f"line {n}: missing or unknown record type {rec.get('type')!r}")
            entry = project(rec, f"line {n}: ")
            why = entry_error(entry)
            if why is not None:
                fail(f"line {n}: {why}")
            if entry["id"] in seen:
                fail(f"line {n}: duplicate entry for {entry['id']}")
            seen.add(entry["id"])
            entries.append(entry)
    except JsonlError as exc:
        fail(str(exc))
    return entries, high_water(entries, high)


def high_water(entries: "list[dict[str, str]]", high: int) -> int:
    """The highest id ever handed out: the mark on disk, or the highest id present if it is higher."""
    return max([0, high] + [int(e["id"][2:]) for e in entries])


def load(path: Path) -> "list[dict[str, str]]":
    return read_store(path)[0]


def dump(path: Path, entries: "list[dict[str, str]]", high: int) -> None:
    """Write the whole store ATOMICALLY. The high-water mark rides in the same write, so a crash can never
    leave the store holding an id the mark has forgotten."""
    records = [project(e) for e in entries]
    high = high_water(entries, high)
    body = json.dumps({"type": SEQ_TYPE, "high": high}) + "\n" if high else ""
    body += "".join(json.dumps({"type": ENTRY_TYPE, **record}) + "\n" for record in records)
    with clean_io("write the store to", path):
        path.parent.mkdir(parents=True, exist_ok=True)
        replace_text(path, body, temp_prefix=".review-learnings-")


@contextmanager
def locked(path: Path) -> "Iterator[None]":
    """Serialize the read-modify-write. THIS IS NOT OPTIONAL — every concurrent run writes this one file,
    and an unlocked race silently drops entries with no other copy anywhere."""
    lock = path.with_name(path.name + ".lock")
    with clean_io("lock the store at", lock):
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock, "a+")  # noqa: SIM115 — closed below; `with` cannot span the yield and the cleanup
    with fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def find(entries: "list[dict[str, str]]", lid: str) -> "dict[str, str] | None":
    for e in entries:
        if e["id"] == lid:
            return e
    return None


def twin(entries: "list[dict[str, str]]", claim: str, anchor: str,
         *, exclude_id: "str | None" = None) -> "dict[str, str] | None":
    """The existing entry that shares this class (exact `claim`+`anchor`), or None. The one predicate the
    write doors match on, so `record` and `set` cannot drift apart on what "the same class" means."""
    for e in entries:
        if e["id"] != exclude_id and e["claim"] == claim and e["anchor"] == anchor:
            return e
    return None


def next_id(high: int) -> str:
    """`rl<N>`, one past the highest N EVER HANDED OUT — assigned HERE, never by the caller."""
    return f"rl{high + 1}"


def check_field(name: str, valid: "tuple[str, ...]") -> None:
    if name not in valid:
        fail(f"unknown field '{name}'; valid: {', '.join(valid)}")


def stamp(args: argparse.Namespace) -> str:
    """The ISO timestamp of this step: `--at` if supplied (validated non-blank), else now."""
    at = getattr(args, "at", None)
    if at is None:
        return now_iso()
    if is_blank(at):
        fail("--at must not be empty — a supplied stamp that shows nothing is refused like any value")
    return at


def append_evidence(existing: str, outcome: str, at: str, text: str) -> str:
    """APPEND a stamped `[outcome at] text` record — NEVER clobber the log. A learning's history of going
    stale, being reaffirmed, and being revoked is the audit trail, not clutter to tidy away."""
    record = f"[{outcome} {at}] {text}"
    return record if is_blank(existing) else existing + "\n" + record


# --- subcommands --------------------------------------------------------------

def taken(cmd: str, args: argparse.Namespace) -> "dict[str, str]":
    """EVERY value the caller supplied, VALIDATED — THE ONE DOOR into the store, for every write command."""
    values: "dict[str, str]" = {}
    for field, flag in INTAKE[cmd].items():
        value = getattr(args, field, None)
        if value is None:
            continue
        if is_blank(value):
            fail(f"{flag} must not be empty — {BLANK_WHY[field]}")
        values[field] = value
    return values


def cmd_record(path: Path, args: argparse.Namespace) -> int:
    """Record a settled refutation or completed legacy demotion as an ACTIVE learning. The only way in.

    RECORDING NEVER DISCHARGES A FINDING. A learning is written only AFTER a finding is already settled —
    refuted-and-dropped, or completed under an existing legacy DEMOTE — and its `provenance` names that
    settled event. This store is a MEMORY of decisions the gate already made, never a shortcut around one.
    """
    values = taken("record", args)
    at = stamp(args)
    with locked(path):
        entries, high = read_store(path)
        # ONE LIVE record per class (exact `claim`+`anchor`). A REVOKED twin is the USER's ruling and
        # `record` is an AUTONOMOUS driver action, so re-recording it would silently undo that ruling —
        # the store's own `revoke` contract promises it is "never silently re-recorded"; `revoked` is
        # terminal (no un-revoke), so refuse fail-closed and let only the user decide it holds again. An
        # active/stale twin is the same class already on file: a second row would split one class across
        # two ids and let the driver consult a stale copy. Either way, refuse before writing.
        clash = twin(entries, values["claim"], values["anchor"])
        if clash is not None:
            if clash["state"] == "revoked":
                fail(f"{clash['id']} is a REVOKED learning with this exact claim and anchor — a revoked "
                     f"learning is KEPT for audit and never silently re-recorded. Only the USER can decide "
                     f"the class holds again (there is no un-revoke). Nothing was recorded.")
            fail(f"{clash['id']} is already a '{clash['state']}' learning with this exact claim and anchor "
                 f"— this store keeps ONE LIVE record per class. Sharpen it with `set`, or `revoke` it, "
                 f"instead of recording a twin. Nothing was recorded.")
        entry = {**DEFAULTS, **values, "id": next_id(high), "state": INITIAL, "recorded": at}
        entries.append(entry)
        dump(path, entries, high)
    print(json.dumps(entry))
    return 0


def cmd_set(path: Path, args: argparse.Namespace) -> int:
    """Edit the claim's PROSE — and NEVER edit it AWAY, nor edit AROUND a user ruling. A REQUIRED field is
    required at every door an entry can change, so `set --claim '   '` cannot hollow a vouched-for learning
    out (the check is in `taken()`). And the class-protection invariants that `record`/`promote` enforce
    hold HERE too: `set` is a write door, so it must not be the one that routes around them."""
    updates = taken("set", args)
    with locked(path):
        entries, high = read_store(path)
        entry = find(entries, args.id)
        if entry is None:
            fail(f"no learning {args.id}")
        if not updates:
            fail(f"set requires at least one --<field> <value>; editable: {', '.join(EDITABLE)}")
        # A durable USER ruling FREEZES the entry's WHOLE editable content, not just its class-defining
        # pair. On a revoked entry any edit would alter what the user retired; on a promoted/consented entry
        # it would rewrite what the user agreed to while keeping the consent stamp — and the tier was
        # consented to on the strength of the `justification`, so leaving `justification`/`falsifiability`
        # editable would let a driver `set` change the very reasoning the ruling rested on. So refuse ANY
        # editable field on a ruled entry, naming id+state. A genuine post-ruling change is a fresh USER
        # ruling, never a driver `set`.
        edited = [f for f in EDITABLE if f in updates]
        if edited:
            frozen_by = None
            if entry["state"] == "revoked":
                frozen_by = "is revoked — a USER ruling, KEPT for audit"
            elif not is_blank(entry["promoted"]):
                frozen_by = f"is promoted to '{entry['promoted']}' — a USER ruling"
            elif not is_blank(entry["decided"]):
                frozen_by = "carries the USER's `decided` ruling"
            if frozen_by is not None:
                fail(f"{args.id} {frozen_by}; its {', '.join(edited)} are what the ruling was made about "
                     f"and are FROZEN. Ask the user for a fresh ruling. Nothing changed.")
        # `set` must enforce the SAME one-live-per-class invariant `record` does, at THIS door too: editing
        # an entry's claim+anchor onto ANY OTHER entry's pair — active, stale, or revoked — leaves two
        # records of one class, no two of which may ever share a claim+anchor. A revoked twin is the sharper
        # case (it silently resurrects a class the USER retired, under a fresh id), so it keeps its own
        # message; any active/stale twin splits one class across two ids and lets the driver consult the
        # wrong copy. Either way, refuse before writing. (A RULED entry cannot reach here with a claim/anchor
        # edit at all: the freeze above already refused ANY editable field on it — so this door only ever
        # sees a claim/anchor edit on an UNRULED entry, and the twin key is just the class-defining pair.)
        touched = [f for f in CLASS_FIELDS if f in updates]
        if touched:
            proposed_claim = updates.get("claim", entry["claim"])
            proposed_anchor = updates.get("anchor", entry["anchor"])
            clash = twin(entries, proposed_claim, proposed_anchor, exclude_id=entry["id"])
            if clash is not None:
                if clash["state"] == "revoked":
                    fail(f"this edit would make {args.id} a claim+anchor twin of {clash['id']}, which the "
                         f"USER REVOKED — a revoked class is never silently resurrected. Nothing changed.")
                fail(f"this edit would make {args.id} a claim+anchor twin of {clash['id']} (a "
                     f"'{clash['state']}' learning) — this store keeps ONE LIVE record per class. Sharpen "
                     f"that entry, or revoke it, instead of forging a twin. Nothing changed.")
        entry.update(updates)  # by NAME — never by position. `state` is NOT here: see EDITABLE.
        dump(path, entries, high)
    print(json.dumps(entry))
    return 0


def cmd_transition(path: Path, args: argparse.Namespace) -> int:
    """The ONLY things that move `state` — each validates the state it comes FROM, so the user's ruling
    (`revoke`/`promote`) cannot be routed around by a driver step."""
    cmd = args.cmd
    frm, to = TRANSITIONS[cmd]
    values = taken(cmd, args)
    with locked(path):
        entries, high = read_store(path)
        entry = find(entries, args.id)
        if entry is None:
            fail(f"no learning {args.id}")
        if entry["state"] not in frm:
            fail(f"{args.id} is '{entry['state']}' — `{cmd}` applies only to: {', '.join(frm)}. "
                 f"A learning reaches '{to}' only along the transition graph; nothing else moves `state`.")
        at = stamp(args)
        for field in WRITES[cmd]:
            if field == "evidence":
                entry["evidence"] = append_evidence(entry["evidence"], to, at, values["evidence"])
            elif field == "promoted":
                tier = values["promoted"]
                if tier not in TIERS:
                    fail(f"--tier {tier!r} is not a tier; valid: {', '.join(TIERS)}")
                # `promote` only WIDENS reach (TIERS is ascending). If already promoted, refuse a --tier
                # that does not rank strictly higher — BEFORE any write, so the existing `promoted` stamp
                # and `decided` are untouched on refusal. This makes the command honor its name.
                current = entry["promoted"]
                if not is_blank(current):
                    current_tier = current.split("@", 1)[0]
                    if current_tier in TIERS and TIERS.index(tier) <= TIERS.index(current_tier):
                        fail(f"{args.id} is already promoted to '{current_tier}' — `promote` only widens "
                             f"reach (order: {' -> '.join(TIERS)}); --tier {tier!r} does not rank higher. "
                             f"Nothing was changed.")
                entry["promoted"] = f"{tier}@{at}"
        if cmd in USER_RULINGS:
            entry["decided"] = at
        entry["state"] = to
        dump(path, entries, high)
    print(json.dumps(entry))
    return 0


def cmd_get(path: Path, args: argparse.Namespace) -> int:
    entry = find(load(path), args.id)
    if entry is None:
        fail(f"no learning {args.id}")
    if args.field is not None:
        check_field(args.field, FIELDS)
        print(entry[args.field])
    else:
        print(json.dumps({f: entry[f] for f in FIELDS}))
    return 0


def cmd_list(path: Path, args: argparse.Namespace) -> int:
    entries = load(path)
    if args.where is not None:
        if "=" not in args.where:
            fail("--where must be <field>=<value>")
        field, _, value = args.where.partition("=")
        check_field(field, FIELDS)
        entries = [e for e in entries if e[field] == value]
    for e in entries:
        print(e["id"])
    return 0


def cmd_table(path: Path, args: argparse.Namespace) -> int:
    entries = load(path)
    if args.fields is not None:
        fields = tuple(f.strip() for f in args.fields.split(","))
        for f in fields:
            check_field(f, FIELDS)
    else:
        fields = TABLE_DEFAULT_FIELDS
    shown = entries if args.show_all else [e for e in entries if e["state"] not in TABLE_HIDDEN_STATES]
    hidden = len(entries) - len(shown)
    for line in config_lines([("store", str(path)), ("rule", TABLE_RULE)]):
        print(line)
    print()
    for line in grid_lines(fields, [[e[f] for f in fields] for e in shown]):
        print(line)
    if not shown:
        print(TABLE_EMPTY_MARKER if not entries else TABLE_ALL_HIDDEN_MARKER)
    if hidden:
        print(hidden_notice(hidden, TABLE_HIDDEN_STATES))
    return 0


# --- self-test ----------------------------------------------------------------

TESTS = Path(__file__).resolve().parent / "review-learnings-test.py"


def self_test() -> int:
    """Run the fixtures in `review-learnings-test.py`, loaded BY PATH from this script's own directory.

    A MISSING SIBLING IS A LOUD FAILURE, NEVER A PASS: a self-test that reports success because it found no
    tests certifies a contract that nothing checked.
    """
    if not TESTS.exists():
        fail(f"the fixtures are GONE — {TESTS} does not exist. `self-test` verifies NOTHING without them.")
    module = load_module_from_path("review_learnings_test", TESTS, register=True)
    if module is None:
        fail(f"cannot load the fixtures at {TESTS}")
    return module.self_test()


# --- cli ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--file", help="path to the store (.gauntlet/review-learnings.jsonl)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser(
        "record",
        help="record a settled refutation or completed legacy demotion as an ACTIVE learning "
             "(the only way in)",
    )
    for field, flag in INTAKE["record"].items():
        r.add_argument(flag, dest=field, required=True, help=INTAKE_HELP[field])
    r.add_argument("--at", help="ISO timestamp it was recorded (default: now)")

    s = sub.add_parser("set", help=f"edit a learning's prose ({', '.join(EDITABLE)})")
    s.add_argument("--id", required=True)
    for field, flag in INTAKE["set"].items():
        s.add_argument(flag, dest=field, help=INTAKE_HELP[field])

    for cmd, (frm, to) in TRANSITIONS.items():
        who = "THE USER rules" if cmd in USER_RULINGS else "the driver records"
        t = sub.add_parser(cmd, help=f"{who}: {'/'.join(frm)} -> {to}")
        t.add_argument("--id", required=True)
        for field, flag in INTAKE[cmd].items():
            t.add_argument(flag, dest=field, required=True, help=INTAKE_HELP[field])
        t.add_argument("--at", help="ISO timestamp of this step (default: now)")

    g = sub.add_parser("get", help="print a learning as JSON, or one field")
    g.add_argument("--id", required=True)
    g.add_argument("--field", help="print only this field")

    ls = sub.add_parser("list", help="print matching learnings' ids")
    ls.add_argument("--where", help="filter as <field>=<value>")

    t = sub.add_parser("table", help="print learnings as an aligned table — active + stale by default "
                                     "(revoked hidden); only ACTIVE ones are consulted (read-only)")
    t.add_argument("--fields", help=f"comma-separated fields (default: {','.join(TABLE_DEFAULT_FIELDS)})")
    t.add_argument("--all", dest="show_all", action="store_true",
                   help=f"show every learning (the default hides state={'/'.join(TABLE_HIDDEN_STATES)})")

    sub.add_parser("self-test", help="run every fixture and assert the rules this file enforces still hold")
    return parser


def main(argv: "list[str]") -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "self-test":
        return self_test()
    if args.file is None:
        parser.error("the following arguments are required: --file")
    path = Path(args.file)
    if args.cmd in TRANSITIONS:
        return cmd_transition(path, args)
    handlers = {"record": cmd_record, "set": cmd_set, "get": cmd_get, "list": cmd_list, "table": cmd_table}
    return handlers[args.cmd](path, args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
