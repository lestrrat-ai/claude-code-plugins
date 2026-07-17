#!/usr/bin/env python3
"""Schema-owning accessor for the FOLLOW-UP ledger (.gauntlet/followups.jsonl).

A follow-up is work the campaign DISCOVERED and deliberately did not do: a defect out of scope for the
PR in hand, a pre-existing bug a fix subagent declined to touch, a refinement a review exposed. Left in
the driver's prose it dies with the driver's context — a follow-up that lives only in the driver's head
is a follow-up that is lost.

WHAT THIS STORE IS: a DRIVER-OWNED LOCAL SCRATCH STORE. Its only writer is the campaign driver, through
this script. It is git-ignored, it is never published, and nobody else can see it. So this file is not
hardened against a hostile or hand-edited store file — it is an accessor with a schema, a lifecycle, and
a work queue's lifetime. What it DOES defend is the one thing a driver can get wrong on its own:

  EVERY ENTRY IS A CANDIDATE, NEVER AN ISSUE. These are things the DRIVER noticed — claims, not facts,
  and the repo already holds that a driver's own diagnosis is a claim needing corroboration (`AGENTS.md`
  or `CLAUDE.md`,
  "Your OWN diagnosis is a claim too"). So the store is LOCAL and stays local, and the one thing the
  driver may NEVER do on its own is PUBLISH one: filing an issue would launder an unvalidated
  self-diagnosis into a public statement of fact, made in the user's name.

WHAT THE DRIVER MAY DO WITHOUT ASKING is the THREE-TIER AUTONOMY THRESHOLD, and `references/followups.md`
OWNS it — the tiers, the conditions, and what each one costs. DO NOT RESTATE THEM HERE. This file enforces
what is STRUCTURAL about them, and the enforcement is the graph:

  * AN INVESTIGATION NEEDS NO PERMISSION. It is read-only, its product is EVIDENCE, and its outcome —
    `corroborated` or `refuted` — is recorded here. A REFUTATION IS RECORDED, NEVER DROPPED: it is the
    driver's own uncorroborated claim about its own uncorroborated claim, so it stays in the store, with
    its evidence, VISIBLE, and the user can still overturn it.
  * THE DRIVER MUST NOT ACTION AN UNCORROBORATED CLAIM. So the autonomous edge that takes a follow-up up
    for work (`take-up`) leaves ONLY from `corroborated`, and it must RECORD ITS EVIDENCE for every ACT
    condition or it is refused. It lands in `self-accepted`, which is a DIFFERENT STATE from `accepted`:
    a follow-up the USER agreed to and one the DRIVER took up on its own are different things, forever,
    and the table says which at a glance.
  * PUBLICATION STAYS THE USER'S. `publish` leaves ONLY from `accepted`, and the only edge into `accepted`
    is `accept`. No sequence of driver-only transitions reaches either the state or the step — proved on
    the graph itself, not on one lucky path (`t_user_ruling_is_unskippable`).

WHAT THIS CANNOT DO is verify that the user really agreed (no local file can): `accept` is a promise the
driver makes. The graph makes skipping the user a DELIBERATE act rather than an oversight — a footgun
guard, NOT a security boundary, and it is not pretended to be one.

THE STORE IS A WORK QUEUE, NOT AN ARCHIVE — and THE PRINCIPLE OF ITS LIFETIME IS: DELETE ONCE A DURABLE
RECORD EXISTS ELSEWHERE; KEEP WHAT PREVENTS REPEATED WORK. It is LOCAL and GIT-IGNORED — it does not
survive a fresh clone — so it is a poor archive and a fine queue, and an archive nobody reads is just a
file that grows.

  * DELETED once the record lives SOMEWHERE ELSE, and the entry names WHERE (`DURABLE_RECORD`): the PR
    that addresses it MERGED (the PR is on GitHub, reviewable, and is where anyone actually looks for "why
    did we do this"), or it was PUBLISHED as an issue (the issue is then the record).
  * NEVER DELETED ON TAKE-UP. An entry deleted when work STARTS is an entry a closed, abandoned or
    rejected PR takes down with it — the work still undone, and nothing left to remember it. So while a PR
    is OPEN the entry STAYS and records which PR is addressing it (`in-pr`), and a PR CLOSED WITHOUT
    MERGING returns it to OPEN WORK (`reopened`) — never a silent vanish, never stuck in "being worked on"
    forever.
  * REJECTIONS STAY. A rejection is worth remembering PRECISELY so it is not re-raised: delete it and the
    next run rediscovers the same thing, records it again, and asks the user again. It is hidden from the
    default view; it is not deleted. (A published one CAN be deleted for the same test: the ISSUE is the
    external record that stops the re-raise. A rejection has no external record — that is the asymmetry.)

TWO PROPERTIES ARE STILL LOAD-BEARING AND ARE NOT SIMPLIFIED AWAY:

  * NOTHING CAN REBUILD A LOST FOLLOW-UP. It exists nowhere else, by design — so every write goes through
    this accessor, under a LOCK, ATOMICALLY. Many concurrent runs write this one file (unlike `state.jsonl`,
    which one lease makes single-writer), and a read-modify-write race would silently drop an entry.
  * A CORRUPT STORE IS REFUSED, WITH THE LINE NUMBER, NEVER SILENTLY REPAIRED OR SKIPPED — the same
    contract `ledger.py` keeps. A skipped line is a follow-up nothing reads.

The store is plaintext JSONL, one JSON object per line, cat/grep/jq-able. This script owns the schema
ONCE (the field list and the transition graph below) so callers read/write BY FIELD NAME.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

# The grid is NOT reimplemented here. The private campaign package owns escaping, layout, and omission
# notices; this file owns only the follow-up schema, lifecycle, and store lifetime.
from _gauntlet.jsonl import JsonlError, object_lines
from _gauntlet.table import config_lines, grid_lines, hidden_notice

DESCRIPTION = "Schema-owning accessor for the follow-up ledger (.gauntlet/followups.jsonl)."

# --- the ACT conditions (owned here, once) ------------------------------------
#
# The FOUR conditions that let the driver take a follow-up up FOR WORK with no user ruling. The tier and
# its rationale are owned by `references/followups.md`; what lives here is the one thing code can enforce:
# EVERY CONDITION MUST BE WITNESSED BY EVIDENCE IN THE ENTRY, or the take-up is refused. A condition
# ASSERTED but not EVIDENCED is not a condition — it is a bypass with a nicer name, and the whole tier
# collapses into "the driver decided it was fine".
#
# `(condition, the FIELD that witnesses it, what that evidence must actually say)`. Everything else about
# the ACT edge is DERIVED from this tuple — the CLI flags and what `take-up` writes — so a fifth condition
# is enforced end-to-end the day it is added here, and a stale restatement of the list cannot exist because
# there is no second list.
ACT_CONDITIONS = (
    ("corroborated", "finding",
     "An INVESTIGATION corroborated it — a reviewer confirmed it, or the driver reproduced the failure. "
     "This one is enforced by the GRAPH (`take-up` leaves only from `corroborated`) and its witness is the "
     "investigation's own `finding`, so it takes no flag: the evidence is already in the entry."),
    ("not-gate-machinery", "act_not_gate",
     "It does not decide whether a PR may merge (the active `AGENTS.md` or `CLAUDE.md` defines the gate). WHEN UNCLEAR IT IS GATE "
     "MACHINERY — the ambiguous case resolves toward ASK, never toward act."),
    ("behavior-preserved", "act_behavior",
     "It preserves user-facing behavior. If it HAS a behavioral surface, name the TEST that proves so. If "
     "it has NONE, say so and name why — an assertion of no-behavioral-surface is itself a claim, and it "
     "is recorded like any other."),
    ("reversible", "act_reversible",
     "A revert restores the prior state. A schema migration is not reversible; anything already published "
     "is not."),
)

# Every ACT condition's witness field, and the subset `take-up` must carry in itself. Condition 1's
# witness is the INVESTIGATION's `finding` — already in the entry, because `take-up` leaves only from
# `corroborated` — so it takes no flag. Both are DERIVED from ACT_CONDITIONS; neither is a second list.
ACT_WITNESSES = tuple(w for _, w, _ in ACT_CONDITIONS)
ACT_FLAGS = tuple(w for w in ACT_WITNESSES if w != "finding")

# --- schema (owned here, once) ------------------------------------------------

PLACEHOLDER = "-"  # what an unset field holds, and what `is_blank()` reads as "carries nothing"

# The record types. A follow-up, and the ONE piece of bookkeeping that is not a follow-up: the id
# high-water mark (`read_store()` — it is what stops a DELETED id from being handed out again). Anything
# else on a line is a corrupt store, refused; never skipped.
ENTRY_TYPE = "followup"
SEQ_TYPE = "followup-seq"

FIELDS = (
    "id", "title", "evidence", "deferred_why", "finding", *ACT_FLAGS,
    "state", "found_run", "found", "decided", "pr", "published",
)

# The fields that name a record OUTSIDE this store — the PR that addresses it, or the issue it was
# published as. THEY ARE WHAT MAKES DELETION SAFE, and the rule is enforced, not assumed: an entry is
# removed only when one of these carries something (`deletable()`, asked by `cmd_transition`). Delete an
# entry with no durable record and the work is gone with nothing left to remember it — which is the one
# thing this store exists to prevent.
DURABLE_RECORD = ("pr", "published")

# Fields a follow-up cannot be WITHOUT. A follow-up with no evidence is a RUMOR — a claim nobody can
# check, which is the one thing this store must never accumulate; and one with no `deferred_why` makes
# the next run re-litigate a scoping decision it cannot see.
#
# REQUIRED MEANS REQUIRED AT EVERY DOOR THAT TAKES A VALUE IN, and `taken()` is where that is spelled —
# once, derived from `INTAKE`. Add a field here and `add` demands it, `set` refuses to blank it, and
# `read_store()` refuses an entry that carries none, all on the day it is added.
REQUIRED = ("title", "evidence", "deferred_why")

# What an ABSENT field on disk reads as — AND ABSENT MEANS THE KEY IS NOT THERE. A field OUTSIDE
# `REQUIRED` is genuinely optional, so a line that OMITS its key gets a DEFAULT, and that backfill is what
# lets the schema grow without migrating a store that cannot be rebuilt (an entry raised before `finding`
# existed still loads, as the `candidate` it was).
#
# A REQUIRED FIELD HAS NO DEFAULT, AND THAT ABSENCE IS THE POINT. Defaulting one would backfill it with
# `-`, which is what an UNSET field holds — so it would look like a value to nothing and like an entry to
# everything, manufacturing a complete-looking follow-up out of a fragment. So a required field that is
# absent is MISSING, a missing one reads as blank, and blank is what `entry_error()` refuses.
MISSING = ""
DEFAULTS = {**{f: PLACEHOLDER for f in FIELDS if f not in REQUIRED}, "state": "candidate"}

# The keys a record may carry, one set per record type, both DERIVED from the schema. An unknown key is
# refused rather than carried along: this accessor has no idea what it means or what a transition should
# do to it, and the next write — which rebuilds the record from `FIELDS` — would silently drop it.
RECORD_KEYS = frozenset(FIELDS) | {"type"}
SEQ_KEYS = frozenset({"type", "high"})


def project(rec: "dict", where: str = "") -> "dict[str, str]":
    """A record — off disk, or out of a door — as THE FIELDS. ONE definition, used both ways.

    Used on the way IN (`read_store`) and on the way OUT (`dump`), so the two can never come to disagree
    about what a record IS. A missing OPTIONAL field defaults; a missing REQUIRED one reads as `MISSING`,
    which is blank, which `entry_error()` then refuses.

    EVERY VALUE IN A FOLLOW-UP IS A STRING. That is not a defense against an adversary; it is the schema.
    A write door is fed by `argparse`, which has nothing but a `str` to hand it, so a non-string on a line
    is a record this accessor did not write — and it is refused rather than coerced, because coercion is
    invention: `str(None)` is `"None"` and `str(123)` is `"123"`, both non-blank, both then indistinguishable
    from evidence somebody actually wrote.
    """
    unknown = sorted(set(rec) - RECORD_KEYS)
    if unknown:
        fail(f"{where}unknown key(s) {', '.join(repr(k) for k in unknown)} — a follow-up carries "
             f"{len(FIELDS)} declared fields and nothing else. Valid: {', '.join(sorted(RECORD_KEYS))}.")
    entry: "dict[str, str]" = {}
    for f in FIELDS:
        raw = rec[f] if f in rec else DEFAULTS.get(f, MISSING)
        if not isinstance(raw, str):
            fail(f"{where}{f} is {raw!r} — every value in a follow-up is a STRING. To leave {f} unset, "
                 f"OMIT THE KEY; an unset field reads back as {PLACEHOLDER!r}.")
        entry[f] = raw
    return entry


# --- the lifecycle (owned here, once) -----------------------------------------
#
# THE GRAPH IS THE ENFORCEMENT, and what it enforces is: THE DRIVER MUST NOT ACTION AN UNCORROBORATED
# CLAIM, and PUBLICATION IS THE USER'S. Two structural facts carry all of it, and both are PROVED on the
# graph rather than asserted (`t_user_ruling_is_unskippable`):
#
#   * `accepted` has exactly ONE in-edge, `accept`, and that is the USER's ruling. `publish` leaves only
#     from `accepted`. So NO sequence of driver-only transitions reaches `accepted`, nor any state
#     `publish` may be taken from — the user cannot be routed around, by any path.
#   * The driver's own edge for taking work up, `take-up`, leaves ONLY from `corroborated` and lands in
#     `self-accepted` — a state that is NOT `accepted` and never becomes indistinguishable from it. Its
#     ACT witnesses stay in the entry forever, and `decided` (the user's stamp) stays `-`.
#
# This is why the lifecycle is a graph and not a settable string.
#
# AND THE END OF AN ENTRY'S LIFE IS ON THE GRAPH TOO. `merged` and `publish` do not move the state — they
# DELETE the entry, because by then the record lives somewhere else (the merged PR; the issue). Everything
# in between is a state, and the two that matter most are the ones that keep a started piece of work from
# being lost: `in-pr` (a PR is open on it — the entry STAYS, and names the PR) and `reopened` (that PR was
# closed WITHOUT merging — the work is undone, so the entry is OPEN WORK again, with its history intact).
#
# `<subcommand>: (states it may be applied FROM, the state it moves TO — or DELETED)`.
DELETED = "deleted"  # NOT a state: the entry is REMOVED. It exists only in the CLI's output for that step,
                     # never on disk — `load()` refuses it as an unknown state, so a tombstone cannot linger.

TRANSITIONS = {
    "corroborate":     (("candidate", "refuted", "reopened"), "corroborated"),
    "refute":          (("candidate", "corroborated", "reopened"), "refuted"),
    "take-up":         (("corroborated",), "self-accepted"),
    "accept":          (("candidate", "corroborated", "refuted", "self-accepted", "reopened"), "accepted"),
    "reject":          (("candidate", "corroborated", "refuted", "self-accepted", "accepted", "in-pr",
                        "reopened"), "rejected"),
    "open-pr":         (("accepted", "self-accepted", "reopened"), "in-pr"),
    "closed-unmerged": (("in-pr",), "reopened"),
    "merged":          (("in-pr",), DELETED),
    "publish":         (("accepted",), DELETED),
}

# The transitions that are the USER'S RULING. They are the ones that stamp `decided`, and the ONLY ones —
# a `decided` written by anything else would launder the driver's action into the user's consent.
USER_RULINGS = ("accept", "reject")

# Everything else is the DRIVER's. Derived, never listed: whatever is not the user's ruling is a step the
# driver can take on its own, and the closure over exactly these edges is what must not reach `accepted`,
# nor any state `publish` leaves from. Add an edge tomorrow and it lands in this set automatically —
# including in the fixture that proves the user cannot be skipped.
DRIVER_STEPS = tuple(c for c in TRANSITIONS if c not in USER_RULINGS)

# The read-only investigation, and the ACT edge. Named because they are the two the THRESHOLD speaks of;
# what they may do is still whatever the graph above says.
INVESTIGATION = ("corroborate", "refute")
ACT_CMD = "take-up"

# The edges that END an entry. DERIVED — an edge deletes because its target is DELETED, never because a
# list here says so, so a deleting edge added tomorrow is enforced and pinned the day it is added.
DELETING = tuple(c for c, (_, to) in TRANSITIONS.items() if to == DELETED)

STATES = ("candidate",) + tuple(dict.fromkeys(to for _, to in TRANSITIONS.values() if to != DELETED))

# The states nothing leaves. DERIVED from the graph — a state is terminal because no transition applies to
# it, never because a list here says so. A DELETING edge counts: `in-pr` is not terminal, and neither is
# `accepted`, because there is still something to do about them.
TERMINAL = tuple(s for s in STATES if not any(s in frm for frm, _ in TRANSITIONS.values()))

# --- evidence: what each transition MUST write --------------------------------
#
# The EVIDENCE a transition is required to leave behind. ONE OWNER: `build_parser()` wires the CLI from it,
# `cmd_transition()` writes from it, and the fixtures derive their argv from it — so an evidence-bearing
# edge cannot be added with a flag the fixtures do not know to pass.
WRITES = {
    "corroborate":     ("finding",),
    "refute":          ("finding",),
    ACT_CMD:           ACT_FLAGS,
    "accept":          ("decided",),
    "reject":          ("decided",),
    "open-pr":         ("pr",),
    "closed-unmerged": (),
    "merged":          (),   # the PR it merged is ALREADY in the entry — `open-pr` is what wrote it
    "publish":         ("published",),
}


def flag_of(field: str) -> str:
    """The flag that carries a field whose flag IS its name (`deferred_why` -> `--deferred-why`)."""
    return "--" + field.replace("_", "-")


# The flag that carries each evidence field in. `decided` is the one that may be OMITTED — a timestamp
# defaults to now; EVIDENCE never defaults to anything.
FLAG = {"finding": "--finding", "published": "--ref", "decided": "--at", "pr": "--pr",
        **{f: flag_of(f) for f in ACT_FLAGS}}
OPTIONAL = ("decided",)

# --- the stamp: WHERE `--at` MEANS ANYTHING (owned here, once) -----------------
#
# THE FIELDS A STEP'S TIMESTAMP GOES INTO. `decided` IS the stamp — the USER's ruling, and when they made
# it. `finding` EMBEDS it — an investigation's record opens `[<outcome> <at>]`. A transition that writes
# NEITHER has nowhere to put a timestamp at all.
STAMPED = ("decided", "finding")

# …so those are the ONLY steps that may OFFER `--at`, and which ones they are is DERIVED from what each edge
# WRITES — never listed. `--at` used to be offered by EVERY transition and read by only these:
# `open-pr --at 1999-01-01T00:00:00Z` exited 0, and that timestamp appeared NOWHERE — not in the entry, not
# on stdout. The caller believes they set a value; the tool tells them it worked; the value is gone. On a
# step that stamps nothing, `--at` is therefore not a flag at all, and argparse refuses it.
STAMPS = tuple(c for c in TRANSITIONS if set(WRITES[c]) & set(STAMPED))

# Why a blank value is refused, per field — the reason the CALLER is told. One reason per INTAKE field, and
# a fixture pins that: a new writable field with no reason here is a field somebody was about to let
# through with a shrug.
BLANK_WHY = {
    "finding": "an investigation that shows no work is a rumor about a rumor",
    "published": "a published follow-up must name WHERE it was published",
    "pr": "the PR is the DURABLE RECORD this entry's deletion will rest on — an entry that says one is "
          "addressing it must name WHICH",
    "decided": "a step is stamped with WHEN it was taken — the USER's ruling into `decided`, an "
               "investigation's into its `finding` record. Omit --at and it stamps now",
    "found": "a follow-up records WHEN it was found — omit --found and it stamps now",
    "found_run": "--run names the RUN that found it — omit it and the entry simply carries no run",
    **{f: "a follow-up without it is a rumor — and this one the store may already have VOUCHED for"
       for f in REQUIRED},
    **{w: f"ACT condition '{c}' was ASSERTED but not EVIDENCED — that is not a condition, it is a bypass"
       for c, w, _ in ACT_CONDITIONS if w in ACT_FLAGS},
}

# What each flag is FOR, printed live by `<cmd> --help`. The ACT conditions' help IS their definition,
# quoted from ACT_CONDITIONS — the driver reads the condition at the moment it is asserting it.
FLAG_HELP = {
    "finding": "the EVIDENCE this investigation produced — APPENDED, never clobbering the claim's own "
               "evidence nor an earlier investigation's finding",
    "published": "where it was published (issue ref or URL) — the ISSUE is now the record, so the entry "
                 "is DELETED",
    "pr": "the PR addressing it (#N or URL). The entry STAYS while that PR is open: `merged` then deletes "
          "it (the PR is the record), `closed-unmerged` returns it to open work (nothing recorded it)",
    **{w: f"ACT condition '{c}' — {why}" for c, w, why in ACT_CONDITIONS if w in ACT_FLAGS},
}


def role(cmd: str) -> str:
    """WHO takes this step — the one thing a reader of `--help` must not have to guess."""
    if cmd in USER_RULINGS:
        return "THE USER rules"
    if cmd in INVESTIGATION:
        return "an INVESTIGATION found (autonomous: it is READ-ONLY)"
    if cmd == ACT_CMD:
        return "the DRIVER takes it up for work (autonomous ONLY with every ACT condition EVIDENCED)"
    if cmd in DELETING:
        return "the record now lives ELSEWHERE, so the ENTRY IS DELETED"
    return "the driver records (it is already past the user)"


# Every field some transition writes. NONE of them may be editable: `set` does not check where an entry
# came from, so a settable witness would let the grounds that made a self-acceptance legal be rewritten
# after the fact — or erased.
EVIDENCE_FIELDS = tuple(dict.fromkeys(f for w in WRITES.values() for f in w))

# Fields a caller may EDIT after the fact. `state` is deliberately ABSENT: it moves only through the
# transitions above, which check where it is coming FROM. Were it settable, `set --state accepted` would
# walk straight past the user's agreement — the one thing this store exists to make unskippable. So are
# every EVIDENCE_FIELD (see above), and `id`/`found*`, which are records of what happened, not opinions to
# revise. What is left is the PROSE of the original claim, which a later run may legitimately sharpen.
EDITABLE = ("title", "evidence", "deferred_why")

# --- intake: EVERY value the CLI takes IN (owned here, once) -------------------
#
# Per WRITE subcommand, the store field each caller-supplied flag lands in. THIS TABLE IS THE WRITE DOOR'S
# SCHEMA: `build_parser()` wires the flags from it (with `dest` = THE FIELD), and `taken()` — the ONE door
# every caller value enters the store through — loops over it and refuses a blank. So a value is validated
# BECAUSE IT IS IN THE SCHEMA, never because a door remembered to ask.
#
# THIS IS WHAT `--at` ESCAPED: the flag whose `dest` was NOT its field's name (`args.at` -> `decided`), read
# by hand at the site, checked by nobody. `accept --at -` exited 0 and wrote an `accepted` entry with a
# blank `decided`. Hand-checking each door is what kept leaving one unchecked, so no door hand-checks: they
# read `taken()`.
INTAKE = {
    "add": {**{f: flag_of(f) for f in REQUIRED},
            "found_run": "--run",     # the run-id that found it — an ordinary value, and so an ordinary
            "found": "--found"},      # blank check: the two that were read by hand, next to the one that was
    "set": {f: flag_of(f) for f in EDITABLE},
    # A transition takes whatever evidence its edge must leave behind — and `--at` ONLY IF IT STAMPS
    # SOMETHING (`STAMPS`, derived from `WRITES`). On an edge that stamps neither, `--at` would be a value
    # the door ACCEPTS and THROWS AWAY, so it is not a flag there at all.
    **{cmd: {**{f: FLAG[f] for f in WRITES[cmd]},
             **({"decided": FLAG["decided"]} if cmd in STAMPS else {})} for cmd in TRANSITIONS},
}

# Every door that WRITES. Derived — a subcommand is a write door because it takes values in, not because a
# list here says so, so a new one is covered by the fixtures the day it is added.
WRITE_CMDS = tuple(INTAKE)

# What each intake flag is FOR, printed live by `<cmd> --help`. The evidence fields' help IS their
# definition (`FLAG_HELP`, quoted from ACT_CONDITIONS); the rest is spelled once here.
INTAKE_HELP = {
    **FLAG_HELP,
    **{f: f"'{f}' — required on `add`, editable after, NEVER blankable: {BLANK_WHY[f]}" for f in REQUIRED},
    "decided": "ISO timestamp of this step (default: now)",
    "found": "ISO timestamp it was found (default: now)",
    "found_run": "the run-id that found it",
}


def is_blank(value: str) -> bool:
    """A field carries nothing: it is whitespace, or it is the placeholder an unset field holds.

    THE ONE BLANK PREDICATE — every door uses THIS, and none of them re-spells it. A door that tested
    `value.strip()` alone would disagree with this one about the PLACEHOLDER, and the two doors of a store
    must never disagree about what "carries nothing" means: a write door that ACCEPTS `-` writes an entry
    that reads back EMPTY.
    """
    return value.strip() in ("", PLACEHOLDER)


# What the DEFAULT view hides: the CLOSED entries — the ones NOBODY has anything left to do about. Work
# that FINISHED is not here to be hidden: it is DELETED (the merged PR, or the issue, is the record). What
# is left to hide is the entry that is closed and yet KEPT — the `rejected` one, kept precisely so the next
# run does not re-raise what the user already ruled against. Everything else is somebody's open obligation.
# This is the same line `ledger.py`'s TABLE_HIDDEN_STATUSES draws, applied to a different store.
TABLE_HIDDEN_STATES = ("rejected",)

# `pr` — not `published`: a published entry is DELETED (the issue is the record), so that column could only
# ever be blank. WHICH PR IS ADDRESSING IT is the one thing a reader of the open queue actually needs.
TABLE_DEFAULT_FIELDS = ("id", "state", "found", "title", "pr")

# The out-of-band lines, in the `#` namespace `escape_cell()` keeps no cell can enter. The two EMPTY-GRID
# markers are DIFFERENT LINES because they are different facts: a store that holds nothing has never raised
# a follow-up, while an all-hidden one has closed every follow-up it holds. Printing the same line for both
# would tell a reader "nothing was ever found" at the exact moment everything was resolved.
TABLE_EMPTY_MARKER = "# (no follow-ups)"
TABLE_ALL_HIDDEN_MARKER = (
    "# (no follow-ups shown — the store is NOT empty; every entry it holds is closed and hidden)"
)
TABLE_MARKERS = (TABLE_EMPTY_MARKER, TABLE_ALL_HIDDEN_MARKER)

# Printed above the grid, in that same namespace. The rule rides on the view itself because the view is
# what an agent actually reads — a store of unvalidated claims that does not say so on sight is one
# `gh issue create` away from publishing them.
TABLE_RULE = "CANDIDATES, not issues — LOCAL. NEVER publish one without the user's agreement on it"

# An id is `fu<N>`, N >= 1 — assigned by the store, never by the caller.
ID_RE = re.compile(r"^fu[1-9][0-9]*$")


def fail(msg: str) -> NoReturn:
    print(f"followups: {msg}", file=sys.stderr)
    raise SystemExit(1)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- parse / serialize --------------------------------------------------------

def entry_error(entry: dict) -> "str | None":
    """Why this entry cannot be in the store — or None. The STRUCTURAL check every line on disk is put to.

    An addressable `id`; a `state` the graph knows; and every REQUIRED field actually CARRYING SOMETHING
    (derived from `REQUIRED`, never hand-listed — a field added there tomorrow is enforced here that day).
    A corrupt entry is REFUSED, loudly, naming its line — never silently repaired, and never skipped: a
    skipped line is a follow-up nothing reads, in the one store that has no other copy.
    """
    if not ID_RE.match(entry["id"]):
        return f"malformed id {entry['id']!r} (expected fu<N>)"
    if entry["state"] not in STATES:
        return f"unknown state {entry['state']!r}; valid: {', '.join(STATES)}"
    empty = [f for f in REQUIRED if is_blank(entry[f])]
    if empty:
        return f"{entry['id']} carries no {', '.join(empty)} — {BLANK_WHY[empty[0]]}"
    return None


@contextmanager
def clean_io(what: str, path: Path) -> "Iterator[None]":
    """Turn an `OSError` into a REFUSAL rather than a traceback.

    The store is a path an operator hands in, and a path can be unreadable, gone, or in a directory nobody
    may write. None of those is a bug in this tool and none of them is a corrupt store — but every one of
    them comes out of the syscall the same way, and a stack trace tells the caller nothing about which.
    """
    try:
        yield
    except OSError as e:
        fail(f"cannot {what} {str(path)!r}: {e.strerror or e}. Nothing was touched.")


def read_store(path: Path) -> "tuple[list[dict], int]":
    """Return the entries AND the id high-water mark. A missing file is an EMPTY store — not an error.

    Every record must be `{"type": "followup", …}` or the ONE meta record (`{"type": "followup-seq"}`, the
    high-water mark below); an unknown type is REJECTED, never skipped. Every corruption is reported with
    THE LINE IT IS ON — the same contract `ledger.py` keeps, and for the same reason: a store that cannot
    be opened must at least say where it went wrong.

    THE HIGH-WATER MARK IS WHY DELETION DOES NOT REUSE AN ID. `next_id()` counts past the highest id EVER
    HANDED OUT, and once entries can be DELETED the surviving entries no longer remember what that was:
    delete `fu7` of seven and the highest id present is `fu6`, so the next `add` would hand out `fu7` a
    SECOND time — silently re-pointing every reference to the old one (a merged PR body, the user's own
    note) at a different follow-up. So the mark is persisted, ONE line, not one per deletion: the deleted
    entry is really gone, and its id is still never reused.

    A store with no mark (one written before the mark existed) is not corrupt: the mark is BACKFILLED from
    the highest id present, which is what it would have been.
    """
    entries: list[dict] = []
    high = 0
    seen: set[str] = set()
    marked = False
    if not path.exists():  # a MISSING store is an EMPTY one — the first follow-up needs no bootstrap step
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
                         f"{', '.join(repr(k) for k in unknown)} — it holds {', '.join(sorted(SEQ_KEYS))} and "
                         f"nothing else.")
                mark = rec.get("high")
                if isinstance(mark, bool) or not isinstance(mark, int):
                    fail(f"line {n}: {SEQ_TYPE} carries a non-numeric high-water mark {mark!r} — it is a whole "
                         f"number of follow-ups ever handed out, and this accessor writes it as one.")
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


def high_water(entries: "list[dict]", high: int) -> int:
    """The highest id ever handed out: the mark on disk, or the highest id present if it is higher.

    ONE definition, used on the way IN (a store with no mark, or one an older version wrote) and on the way
    OUT (`dump`) — so a mark can never be written that is LOWER than an id already in the store, which is
    the only way this could hand the same id out twice. The `0` floor is what keeps `next_id()` at `fu1` or
    above for an empty store.
    """
    return max([0, high] + [int(e["id"][2:]) for e in entries])


def load(path: Path) -> "list[dict]":
    """The entries — what every reader wants. The mark is bookkeeping (see `read_store`)."""
    return read_store(path)[0]


def dump(path: Path, entries: "list[dict]", high: int) -> None:
    """Write the whole store ATOMICALLY — a temp file in the same directory, then `os.replace()`.

    A partial write here is not a corrupt cache that the next wake heals: it is data that exists NOWHERE
    else. `os.replace()` is atomic on POSIX, so a reader (or a crash) sees either the old store or the
    new one, never half of one.

    The high-water mark rides in the same atomic write, so a crash can never leave the store holding an id
    the mark has forgotten (which is how a deletion would hand that id out again).
    """
    records = [project(e) for e in entries]  # the SAME projection `read_store()` applies to a line on disk
    high = high_water(entries, high)
    body = json.dumps({"type": SEQ_TYPE, "high": high}) + "\n" if high else ""
    body += "".join(json.dumps({"type": ENTRY_TYPE, **record}) + "\n" for record in records)
    with clean_io("write the store to", path):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".followups-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


@contextmanager
def locked(path: Path):
    """Serialize the read-modify-write. THIS IS NOT OPTIONAL — it is what makes the store durable.

    `state.jsonl` is single-writer by construction (one run, one lease). This file has MANY: every
    concurrent run appends to it. Two drivers that both `load()` a 7-entry store and both `dump()` an
    8-entry one leave 8 entries, not 9 — one follow-up silently gone, with no error, no reconcile, and no
    other copy anywhere. `flock` on a sidecar lock file makes the whole cycle exclusive; the lock file is
    kept (not unlinked) so two processes cannot end up holding flocks on two different inodes.
    """
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


def deletable(entry: dict) -> bool:
    """May this entry be REMOVED? Only if it names a record that outlives it (`DURABLE_RECORD`).

    THE ONE QUESTION DELETION TURNS ON, spelled once. It is the SECOND lock on that door, and it is honest
    about being one: TODAY the graph alone already makes an undeletable entry unreachable — every legal
    history that arrives at a deleting edge has written the PR or the issue first, and
    `t_deletion_needs_a_durable_record` PROVES that on the graph rather than hoping for it. So this is what
    still holds if a future edge or state change quietly stops being true — the check is here, at the moment
    the data is destroyed.
    """
    return any(not is_blank(entry[f]) for f in DURABLE_RECORD)


def find(entries: "list[dict]", fid: str) -> "dict | None":
    for e in entries:
        if e["id"] == fid:
            return e
    return None


def next_id(high: int) -> str:
    """`fu<N>`, one past the highest N EVER HANDED OUT — assigned HERE, never by the caller.

    It counts past the HIGH-WATER MARK, not the entry count and not the highest id still PRESENT — which
    stopped being the same number the day entries could be DELETED. Reusing an id would silently re-point
    every reference to the old entry (an audit file, a merged PR body, the user's own note) at a different
    follow-up.
    """
    return f"fu{high + 1}"


def check_field(name: str, valid: "tuple[str, ...]") -> None:
    if name not in valid:
        fail(f"unknown field '{name}'; valid: {', '.join(valid)}")


# --- subcommands --------------------------------------------------------------

def taken(cmd: str, args) -> "dict[str, str]":
    """EVERY value the caller supplied, VALIDATED — THE ONE DOOR into the store, for every write command.

    No write door reads a caller's value any other way, and that is the whole design: a value is validated
    because it is in `INTAKE`, not because a door remembered to check it. Hand-checking is what kept
    leaving one door unchecked — `accept --at -` wrote a blank `decided` through the last of them.

    A flag not passed is absent from the result: the field keeps its default (`-`, or a stamp of `now`).
    That is UNSET, which is not the same thing as a caller handing in something that carries nothing.
    """
    values: "dict[str, str]" = {}
    for field, flag in INTAKE[cmd].items():
        value = getattr(args, field, None)
        if value is None:
            continue
        if is_blank(value):  # THE one blank predicate — see `is_blank()`. Called HERE, once, for every door.
            fail(f"{flag} must not be empty — {BLANK_WHY[field]}")
        values[field] = value
    return values


def cmd_add(path: Path, args) -> int:
    values = taken("add", args)  # THE one door — every caller value, validated (see `taken()`)
    with locked(path):
        entries, high = read_store(path)
        # The store's own facts LAST: the id is assigned here, never caller-set and never reused — not even
        # after a DELETION took the highest id out of the store (see `read_store`) — and a new follow-up is
        # a CANDIDATE, whatever anybody passed.
        entry = {**DEFAULTS, "found": now_iso(), **values,
                 "id": next_id(high), "state": "candidate"}
        entries.append(entry)
        dump(path, entries, high)  # `dump` raises the mark to the id just handed out
    print(json.dumps(entry))
    return 0


def cmd_set(path: Path, args) -> int:
    """Edit the claim's PROSE — and NEVER edit it AWAY.

    A REQUIRED field is required WHEREVER AN ENTRY CAN CHANGE, not only where `add` happened to create it.
    `add` refusing a blank `evidence` guards nothing if `set --evidence '   '` can hollow the entry out an
    hour later: what is left is the same RUMOR the store exists to refuse — except this one the store has
    already vouched for, because it was checked once, at a door it is no longer standing at.

    The check is not HERE at all, and that is the point: this door reads its values through `taken()`, like
    every other, so the predicate cannot be forgotten at it.
    """
    updates = taken("set", args)  # THE one door — every caller value, validated (see `taken()`)
    with locked(path):
        entries, high = read_store(path)
        entry = find(entries, args.id)
        if entry is None:
            fail(f"no follow-up {args.id}")
        if not updates:
            fail(f"set requires at least one --<field> <value>; editable: {', '.join(EDITABLE)}")
        entry.update(updates)  # by NAME — never by position. `state` is NOT here: see EDITABLE.
        dump(path, entries, high)
    print(json.dumps(entry))
    return 0


def append_finding(existing: str, outcome: str, at: str, text: str) -> str:
    """APPEND the investigation's finding — NEVER clobber what is already there.

    The claim's `evidence` (why the driver raised it) and the investigation's `finding` (what happened when
    somebody actually looked) are DIFFERENT THINGS and both matter — so the finding never touches
    `evidence`, and a SECOND investigation never erases the first. A later run that overturns an earlier
    refutation must leave that refutation standing: the record of the driver changing its mind IS the audit
    trail, and a `finding` that only ever holds the latest verdict is a store that quietly rewrites its own
    history. Each record is stamped with the outcome it produced and when.
    """
    record = f"[{outcome} {at}] {text}"
    return record if is_blank(existing) else existing + "\n" + record


def cmd_transition(path: Path, args) -> int:
    """The ONLY things that move `state` — or END an entry — and every one checks the state it comes FROM.

    So the graph is the guard, not a convention: there is no edge by which a driver reaches `accepted` or
    runs `publish`, and no edge out of `candidate` that skips an investigation on the way to work.

    WHAT EACH TRANSITION MUST WRITE comes from `WRITES`, and a blank value is REFUSED. That is what stops
    the ACT edge from degenerating into a bypass: `take-up` cannot claim a condition it will not evidence.

    AND A DELETING EDGE MUST LEAVE A DURABLE RECORD BEHIND. `merged`/`publish` REMOVE the entry, so the one
    thing that can make that safe is checked at the moment it happens: the entry must name a record OUTSIDE
    this store (`DURABLE_RECORD` — the PR that merged, the issue it was published as). The step still PRINTS
    the removed entry, in full: that record is the driver's handoff, and it names where the follow-up now
    lives.
    """
    cmd = args.cmd
    frm, to = TRANSITIONS[cmd]
    values = taken(cmd, args)  # THE one door — every caller value, validated (see `taken()`)
    with locked(path):
        entries, high = read_store(path)
        entry = find(entries, args.id)
        if entry is None:
            fail(f"no follow-up {args.id}")
        if entry["state"] not in frm:
            fail(
                f"{args.id} is '{entry['state']}' — `{cmd}` applies only to: {', '.join(frm)}. "
                f"A follow-up reaches '{to}' only along the transition graph; nothing else moves `state`."
            )
        # WHEN this step was taken. The user's ruling is DURABLE DATA, exactly like the ledger's
        # `api_approval`: a later run — or a fresh agent that never saw the conversation — reads it and does
        # not re-ask. OMITTED, the stamp defaults to now; SUPPLIED (`--at`), it is a value like any other and
        # `taken()` has already refused a blank. It is offered ONLY where it lands somewhere (`STAMPS`).
        stamp = values.get("decided") or now_iso()
        for field in WRITES[cmd]:
            if field in OPTIONAL:
                entry[field] = stamp
                continue
            entry[field] = (append_finding(entry[field], to, stamp, values[field]) if field == "finding"
                            else values[field])
        if to == DELETED:
            if not deletable(entry):
                fail(f"{args.id} names no durable record ({', '.join(DURABLE_RECORD)}) — deleting it would "
                     f"destroy work that exists NOWHERE else. An entry is deleted only once its record "
                     f"lives elsewhere: the PR that addresses it MERGED, or it was PUBLISHED.")
            # OUT of the store BEFORE `state` is stamped: the sentinel is not a state, and an entry
            # carrying it must never reach `dump()` — `load()` would refuse the whole store.
            entries = [e for e in entries if e["id"] != entry["id"]]
        entry["state"] = to
        dump(path, entries, high)  # the mark outlives the entry, so its id is never handed out again
    print(json.dumps(entry))
    return 0


def cmd_get(path: Path, args) -> int:
    entry = find(load(path), args.id)
    if entry is None:
        fail(f"no follow-up {args.id}")
    if args.field is not None:  # an empty --field is an invalid field, not "omitted"
        check_field(args.field, FIELDS)
        print(entry[args.field])
    else:
        print(json.dumps({f: entry[f] for f in FIELDS}))
    return 0


def cmd_list(path: Path, args) -> int:
    entries = load(path)
    if args.where is not None:  # an empty --where is malformed, not "omitted"
        if "=" not in args.where:
            fail("--where must be <field>=<value>")
        field, _, value = args.where.partition("=")
        check_field(field, FIELDS)
        entries = [e for e in entries if e[field] == value]
    for e in entries:
        print(e["id"])
    return 0


def cmd_table(path: Path, args) -> int:
    entries = load(path)
    if args.fields is not None:  # an empty --fields is malformed, not "omitted"
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
    # ONLY the printed entries become cells — so a hidden one cannot reach the visible output even through
    # the column widths (see the ledger's `hidden-row-inert` fixture; the same property is pinned here).
    for line in grid_lines(fields, [[e[f] for f in fields] for e in shown]):
        print(line)
    if not shown:
        print(TABLE_EMPTY_MARKER if not entries else TABLE_ALL_HIDDEN_MARKER)
    if hidden:
        # THE OMISSION IS NEVER SILENT. Wording derived from TABLE_HIDDEN_STATES, never spelled beside it.
        print(hidden_notice(hidden, TABLE_HIDDEN_STATES))
    return 0


# --- self-test ----------------------------------------------------------------

# THE FIXTURES ARE THE CONTRACT, and they live in a SIBLING module. They are still run from HERE — CI
# invokes `followups.py self-test` and trusts its exit code — but what a reader (or a reviewer) must read to
# understand the STORE is now just the store.
TESTS = Path(__file__).resolve().parent / "followups-test.py"


def self_test() -> int:
    """Run the fixtures in `followups-test.py`.

    Loaded BY PATH, from THIS script's own directory — never by name and never relative to the cwd:
    `followups-test` is not a legal module name, and the driver invokes this from arbitrary directories
    while the script itself lives wherever the plugin is installed. `__file__` is the only thing that knows
    where its sibling is.

    A MISSING SIBLING IS A LOUD FAILURE, NEVER A PASS. A self-test that reports success because it found no
    tests is worse than one that fails: it certifies a contract that nothing checked.
    """
    if not TESTS.exists():
        fail(f"the fixtures are GONE — {TESTS} does not exist. `self-test` verifies NOTHING without them, "
             f"and it must never report success when it has checked nothing.")
    # The fixtures `import followups`. Run as a script, THIS module is `__main__` and is not registered
    # under its own name — so that import would load and execute a SECOND copy of this file, and the
    # fixtures would then be testing that copy instead of the one running. Register this one first.
    sys.modules.setdefault("followups", sys.modules[__name__])
    spec = importlib.util.spec_from_file_location("followups_test", TESTS)
    if spec is None or spec.loader is None:  # a broken install — never an input error
        fail(f"cannot load the fixtures at {TESTS}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.self_test()


# --- cli ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    # NOT `required=True`: `self-test` reads no store at all. Every OTHER subcommand does, and main()
    # enforces that through `parser.error` — the same message, usage line and exit 2 argparse would give.
    parser.add_argument("--file", help="path to the store (.gauntlet/followups.jsonl)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # EVERY WRITE DOOR'S FLAGS COME FROM `INTAKE`, and every one carries `dest` = THE FIELD IT WRITES. Both
    # halves are load-bearing: the flag EXISTS because the table declares it (so it cannot exist without
    # being validated — `taken()` loops over that same table), and its `dest` IS the field (so no door has to
    # translate `args.at` into `decided` by hand, which is precisely how `--at` came to be checked by
    # nobody).
    a = sub.add_parser("add", help="raise a new follow-up CANDIDATE (the only way in)")
    for field, flag in INTAKE["add"].items():
        a.add_argument(flag, dest=field, required=field in REQUIRED, help=INTAKE_HELP[field])

    s = sub.add_parser("set", help=f"edit an existing follow-up's prose ({', '.join(EDITABLE)})")
    s.add_argument("--id", required=True)
    for field, flag in INTAKE["set"].items():  # `state` is NOT here, and that is the point: see EDITABLE.
        s.add_argument(flag, dest=field, help=INTAKE_HELP[field])

    # The transitions — the ONLY things that move `state`. Each validates the state it comes FROM, so the
    # user's ruling cannot be routed around. Their FLAGS are derived from `WRITES` (through `INTAKE`): every
    # evidence field a transition must leave behind is a REQUIRED flag, so an edge cannot be added that
    # writes a witness the CLI never asks for. The one OPTIONAL field is the timestamp: a stamp may default
    # to now; EVIDENCE never defaults.
    for cmd, (frm, to) in TRANSITIONS.items():
        t = sub.add_parser(cmd, help=f"{role(cmd)}: {'/'.join(frm)} -> {to}")
        t.add_argument("--id", required=True)
        for field, flag in INTAKE[cmd].items():
            t.add_argument(flag, dest=field, required=field not in OPTIONAL, help=INTAKE_HELP[field])

    g = sub.add_parser("get", help="print a follow-up as JSON, or one field")
    g.add_argument("--id", required=True)
    g.add_argument("--field", help="print only this field")

    ls = sub.add_parser("list", help="print matching follow-ups' ids")
    ls.add_argument("--where", help="filter as <field>=<value>")

    t = sub.add_parser("table", help="print the open follow-ups as an aligned table (read-only)")
    t.add_argument("--fields", help=f"comma-separated fields to show (default: {','.join(TABLE_DEFAULT_FIELDS)})")
    t.add_argument("--all", dest="show_all", action="store_true",
                   help=f"show every follow-up (the default hides state={'/'.join(TABLE_HIDDEN_STATES)} "
                        f"and reports how many it hid)")

    sub.add_parser("self-test", help="run every fixture and assert the rules this file enforces still hold")
    return parser


def main(argv: "list[str]") -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "self-test":  # stdlib only, no store, no repo checkout, no network
        return self_test()
    if args.file is None:
        parser.error("the following arguments are required: --file")
    path = Path(args.file)
    if args.cmd in TRANSITIONS:
        return cmd_transition(path, args)
    handlers = {"add": cmd_add, "set": cmd_set, "get": cmd_get, "list": cmd_list, "table": cmd_table}
    return handlers[args.cmd](path, args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
