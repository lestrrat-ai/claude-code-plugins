#!/usr/bin/env python3
"""Schema-owning accessor for the FOLLOW-UP ledger (.gauntlet/followups.jsonl).

A follow-up is work the campaign DISCOVERED and deliberately did not do: a defect out of scope for the
PR in hand, a pre-existing bug a fix subagent declined to touch, a refinement a review exposed. Left in
the driver's prose it dies with the driver's context — the same defect the CI-liveness work exists to
fix, one layer up: a counter that dies with the context never reaches its cap, and a follow-up that
lives only in the driver's head is a follow-up that is lost.

THREE PROPERTIES SEPARATE THIS STORE FROM `state.jsonl`, and each one drives the design:

  * It OUTLIVES ITS RUN. A follow-up found by run A is promoted by run C. So it is NOT run-scoped: it
    lives at `.gauntlet/followups.jsonl`, a sibling of `history/` — never under `.gauntlet/tmp/**`,
    which is disposable.
  * It IS THE SOURCE OF TRUTH, not a cache. `state.jsonl` is a hint reconciled against GitHub every
    wake, so a lost row heals itself. NOTHING can rebuild a lost follow-up: it exists nowhere else, by
    design (see below). A lost entry is lost forever — which is why every write goes through this
    accessor, under a lock, atomically.
  * It has MANY WRITERS. One lease makes `state.jsonl` single-writer; every concurrent run writes THIS
    file. A read-modify-write race would silently drop an entry, and nothing downstream would ever know.

EVERY ENTRY IS A CANDIDATE, NEVER AN ISSUE. These are things the DRIVER noticed — claims, not facts, and
the repo already holds that a driver's own diagnosis is a claim needing corroboration (`CLAUDE.md`, "Your
OWN diagnosis is a claim too"). So the store is LOCAL and stays local, and the one thing the driver may
NEVER do on its own is PUBLISH one — filing an issue would launder an unvalidated self-diagnosis into a
public statement of fact, made in the user's name.

WHAT THE DRIVER MAY DO WITHOUT ASKING is the THREE-TIER AUTONOMY THRESHOLD, and `references/followups.md`
OWNS it — the tiers, the conditions, and what each one costs. DO NOT RESTATE THEM HERE. This file enforces
what is STRUCTURAL about them, and the enforcement is the graph:

  * AN INVESTIGATION NEEDS NO PERMISSION. It is read-only, its product is EVIDENCE, and its outcome —
    `corroborated` or `refuted` — is recorded here. A REFUTATION IS RECORDED, NEVER DROPPED: it is the
    driver's own uncorroborated claim about its own uncorroborated claim, so it stays in the store, with
    its evidence, VISIBLE, and the user can still overturn it.
  * THE DRIVER MUST NOT ACTION AN UNCORROBORATED CLAIM. That — not the user's signature — is the real
    guarantee. So the autonomous edge that takes a follow-up up for work (`take-up`) leaves ONLY from
    `corroborated`, and it must RECORD ITS EVIDENCE for every ACT condition or it is refused. It lands in
    `self-accepted`, which is a DIFFERENT STATE from `accepted`: a follow-up the USER agreed to and one
    the DRIVER took up on its own are different things, forever, and the table says which at a glance.
  * PUBLICATION STAYS THE USER'S. `publish` leaves ONLY from `accepted`, and the only edge into `accepted`
    is `accept`. No sequence of driver-only transitions reaches either the state or the step — proved on
    the graph itself, not on one lucky path (`t_user_ruling_is_unskippable`).

WHAT THIS CANNOT DO is verify that the user really agreed (no local file can): `accept` is a promise the
driver makes. It makes skipping the user a DELIBERATE LIE rather than an oversight — a footgun guard, NOT
a security boundary. But the guard must hold against a driver that writes the JSONL BY HAND, because THAT
IS THE DRIVER IT DEFENDS AGAINST: so the invariants are checked where the DATA enters (`load()`), not only
where the COMMANDS do. An entry no legal sequence of transitions could have produced is CORRUPT, and it is
refused — loudly, never silently repaired and never skipped.

THE STORE IS A WORK QUEUE, NOT AN ARCHIVE — and THE PRINCIPLE OF ITS LIFETIME IS: DELETE ONCE A DURABLE
RECORD EXISTS ELSEWHERE; KEEP WHAT PREVENTS REPEATED WORK. It is LOCAL and GIT-IGNORED — it does not
survive a fresh clone and nobody else can see it — so it is a poor archive and a fine queue, and an
archive nobody reads is just a file that grows.

  * DELETED once the record lives SOMEWHERE ELSE, and the entry names WHERE (`DURABLE_RECORD`): the PR
    that addresses it MERGED (the PR is on GitHub, reviewable, and is where anyone actually looks for "why
    did we do this"), or it was PUBLISHED as an issue (the issue is then the record).
  * NEVER DELETED ON TAKE-UP. An entry deleted when work STARTS is an entry a closed, abandoned or
    rejected PR takes down with it — the work still undone, and nothing left to remember it. That is the
    exact permanent loss this store exists to prevent, moved later in time. So while a PR is OPEN the entry
    STAYS and records which PR is addressing it (`in-pr`), and a PR CLOSED WITHOUT MERGING returns it to
    OPEN WORK (`reopened`) — never a silent vanish, never stuck in "being worked on" forever.
  * REJECTIONS STAY. A rejection is worth remembering PRECISELY so it is not re-raised: delete it and the
    next run rediscovers the same thing, records it again, and asks the user again. It is hidden from the
    default view; it is not deleted. (A published one CAN be deleted for the same test: the ISSUE is the
    external record that stops the re-raise. A rejection has no external record — that is the whole
    asymmetry.)

The store is plaintext JSONL, one JSON object per line, cat/grep/jq-able. This script owns the schema
ONCE (the field list and the transition graph below) so callers read/write BY FIELD NAME.
"""

from __future__ import annotations

import argparse
import fcntl
import io
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

DESCRIPTION = "Schema-owning accessor for the follow-up ledger (.gauntlet/followups.jsonl)."

# The grid is NOT reimplemented here. `escape_cell()` is security-shaped — it is what stops a value from
# forging a column, a row, or an out-of-band line — and a second copy of it would be a second definition
# of the same guarantee, free to rot away from the one the fixtures pin. So the escaping, the layout and
# the omission notice are IMPORTED from the ledger, which owns them; this file owns only what is its own:
# the schema, the lifecycle, and the store's lifetime.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger  # noqa: E402
from ledger import (  # noqa: E402
    SelfTestFailure, check, config_lines, escape_cell, grid_lines, hidden_notice,
)

# --- the ACT conditions (owned here, once) ------------------------------------
#
# The FOUR conditions that let the driver take a follow-up up FOR WORK with no user ruling. The tier and
# its rationale are owned by `references/followups.md`; what lives here is the one thing code can enforce:
# EVERY CONDITION MUST BE WITNESSED BY EVIDENCE IN THE ENTRY, or the take-up is refused. A condition
# ASSERTED but not EVIDENCED is not a condition — it is a bypass with a nicer name, and the whole tier
# collapses into "the driver decided it was fine".
#
# `(condition, the FIELD that witnesses it, what that evidence must actually say)`. Everything else about
# the ACT edge is DERIVED from this tuple — the CLI flags, what `take-up` writes, and what `load()` demands
# of a `self-accepted` entry — so a fifth condition is enforced end-to-end the day it is added here, and a
# stale restatement of the list cannot exist because there is no second list.
ACT_CONDITIONS = (
    ("corroborated", "finding",
     "An INVESTIGATION corroborated it — a reviewer confirmed it, or the driver reproduced the failure. "
     "This one is enforced by the GRAPH (`take-up` leaves only from `corroborated`) and its witness is the "
     "investigation's own `finding`, so it takes no flag: the evidence is already in the entry."),
    ("not-gate-machinery", "act_not_gate",
     "It does not decide whether a PR may merge (`CLAUDE.md` defines the gate). WHEN UNCLEAR IT IS GATE "
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
DEFAULTS = {**{f: PLACEHOLDER for f in FIELDS}, "state": "candidate"}

# The fields that name a record OUTSIDE this store — the PR that addresses it, or the issue it was
# published as. THEY ARE WHAT MAKES DELETION SAFE, and the rule is enforced, not assumed: an entry is
# removed only when one of these carries something (`cmd_transition`), and no deleting edge may leave from
# a state whose legal histories could have written NEITHER (`t_deletion_needs_a_durable_record`). Delete an
# entry with no durable record and the work is gone with nothing left to remember it — which is the one
# thing this store exists to prevent.
DURABLE_RECORD = ("pr", "published")

# Fields a follow-up cannot be WITHOUT. A follow-up with no evidence is a RUMOR — a claim nobody can
# check, which is the one thing this store must never accumulate; and one with no `deferred_why` makes
# the next run re-litigate a scoping decision it cannot see.
REQUIRED = ("title", "evidence", "deferred_why")

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
# The EVIDENCE a transition is required to leave behind. This is the other half of the enforcement, and it
# is what makes the graph survive a driver that writes the JSONL by hand: `load()` derives from this table
# (below) what a legal history must have left in an entry, and REFUSES one that could not have arisen.
#
# ONE OWNER. `build_parser()` wires the CLI from it, `cmd_transition()` writes from it, `load()` validates
# from it, and the fixtures derive their argv from it — so an evidence-bearing edge cannot be added with a
# flag the fixtures do not know to pass, or a witness `load()` does not know to demand.
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

# The flag that carries each evidence field in. `decided` is the one that may be OMITTED — a timestamp
# defaults to now; EVIDENCE never defaults to anything.
FLAG = {"finding": "--finding", "published": "--ref", "decided": "--at", "pr": "--pr",
        **{f: "--" + f.replace("_", "-") for f in ACT_FLAGS}}
OPTIONAL = ("decided",)

# Why a blank value is refused, per field. An evidence field that may be blank is not evidence.
BLANK_WHY = {
    "finding": "an investigation that shows no work is a rumor about a rumor",
    "published": "a published follow-up must name WHERE it was published",
    "pr": "the PR is the DURABLE RECORD this entry's deletion will rest on — an entry that says one is "
          "addressing it must name WHICH",
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
# transitions above, which check where it is coming FROM. Were it settable, `set --state published` would
# walk straight past the user's agreement — the one thing this store exists to make unskippable. So are
# every EVIDENCE_FIELD (see above), and `id`/`found*`, which are records of what happened, not opinions to
# revise. What is left is the PROSE of the original claim, which a later run may legitimately sharpen.
EDITABLE = ("title", "evidence", "deferred_why")


def witness_alternatives() -> "dict[str, tuple[frozenset, ...]]":
    """For each state, the ALTERNATIVE evidence sets that a LEGAL history would have left in the entry.

    THIS IS THE LOAD-TIME GUARD, and it is derived from TRANSITIONS + WRITES by a fixpoint — never
    hand-listed. Walk every path from `candidate`, accumulating what each edge is required to write.
    Nothing ever erases a witness (no transition clears a field, and none of them is EDITABLE), so an entry
    in state S is legal only if it carries EVERY field of AT LEAST ONE alternative for S.

    Alternatives, not one set, because a state can be reached more than one way and the entry must satisfy
    the way it actually came: `in-pr` is legal with the user's `decided` stamp (it came through `accept`)
    OR with the full ACT witness set (it came through `take-up`) — but NOT with neither, which is what a
    hand-written `in-pr` would be. Only the MINIMAL alternatives are kept: a superset can never make an
    entry legal that its subset would not.

    A DELETING edge is skipped: there is no entry at the other end of it, so there is nothing to witness.

    This is what closes the gap the transitions alone leave open. `publish` checking that the entry is
    `accepted` guards nothing against a driver that simply WRITES `"state": "accepted"` into the file — and
    that driver is the one this store exists to defend against. With this, `accepted` without a `decided`
    stamp is not an entry the accessor argues with; it is an entry no legal history could have produced,
    and it does not load at all.
    """
    alts: "dict[str, set]" = {s: set() for s in STATES}
    alts["candidate"] = {frozenset()}
    changed = True
    while changed:
        changed = False
        for cmd, (frm, to) in TRANSITIONS.items():
            if to == DELETED:
                continue  # a DELETING edge leaves no entry behind, so there is no state to witness
            for prev in frm:
                for base in tuple(alts[prev]):
                    reached = base | frozenset(WRITES[cmd])
                    if reached not in alts[to]:
                        alts[to].add(reached)
                        changed = True
    return {
        s: tuple(sorted((a for a in sets if not any(b < a for b in sets)), key=lambda a: (len(a), sorted(a))))
        for s, sets in alts.items()
    }


WITNESS = witness_alternatives()


def is_blank(value: str) -> bool:
    """A field carries nothing: empty, whitespace, or the placeholder an unset field defaults to.

    THE ONE BLANK PREDICATE — every door uses THIS, and none of them re-spells it. A door that tested
    `value.strip()` instead would disagree with this one about the PLACEHOLDER, and the two doors of a
    store must never disagree about what "carries nothing" means: `load()` reads `-` as blank, so a write
    door that ACCEPTS `-` writes an entry that reads back EMPTY — and, for a witness, one `load()` then
    rejects as an illegal history, leaving a store its own accessor can no longer open.
    """
    return value.strip() in ("", PLACEHOLDER)

# What the DEFAULT view hides: the CLOSED entries — the ones NOBODY has anything left to do about. Work
# that FINISHED is not here to be hidden: it is DELETED (the merged PR, or the issue, is the record). What
# is left to hide is the entry that is closed and yet KEPT — the `rejected` one, kept precisely so the next
# run does not re-raise what the user already ruled against. Everything else is somebody's open obligation:
# a `candidate` needs the user's ruling, an `accepted` one needs the work, an `in-pr` one needs that PR to
# land, a `reopened` one is work whose PR died. This is the same line `ledger.py`'s TABLE_HIDDEN_STATUSES
# draws, applied to a different store.
TABLE_HIDDEN_STATES = ("rejected",)

# `pr` — not `published`: a published entry is DELETED (the issue is the record), so that column could only
# ever be blank. WHICH PR IS ADDRESSING IT is the one thing a reader of the open queue actually needs.
TABLE_DEFAULT_FIELDS = ("id", "state", "found", "title", "pr")

# The out-of-band lines, in the `#` namespace `escape_cell()` proves no cell can enter (a leading `#` is
# escaped, and a body line always opens with its first cell). The two EMPTY-GRID markers are DIFFERENT
# LINES because they are different facts: a store that holds nothing has never raised a follow-up, while
# an all-hidden one has closed every follow-up it holds. Printing the same line for both would tell a
# reader "nothing was ever found" at the exact moment everything was resolved.
TABLE_EMPTY_MARKER = "# (no follow-ups)"
TABLE_ALL_HIDDEN_MARKER = (
    "# (no follow-ups shown — the store is NOT empty; every entry it holds is closed and hidden)"
)
TABLE_MARKERS = (TABLE_EMPTY_MARKER, TABLE_ALL_HIDDEN_MARKER)

# Printed above the grid, in that same namespace. The rule rides on the view itself because the view is
# what an agent actually reads — a store of unvalidated claims that does not say so on sight is one
# `gh issue create` away from publishing them.
TABLE_RULE = "CANDIDATES, not issues — LOCAL. NEVER publish one without the user's agreement on it"

ID_RE = re.compile(r"^fu[1-9][0-9]*$")


def fail(msg: str) -> NoReturn:
    print(f"followups: {msg}", file=sys.stderr)
    raise SystemExit(1)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- parse / serialize --------------------------------------------------------

def illegal_history(entry: dict) -> "str | None":
    """Why this entry could NOT have been produced by any legal sequence of transitions — or None.

    THE INVARIANT IS CHECKED WHERE THE DATA ENTERS, not only where the COMMANDS do. `publish` refusing to
    run on a non-`accepted` entry guards nothing against a driver that hand-writes `"state": "accepted"`
    into the JSONL — and a driver that would skip the user is exactly the one that would do that. So an
    entry must carry the evidence a legal path to its state was REQUIRED to leave behind (`WITNESS`, which
    is derived from the graph itself): an `accepted` with no `decided` stamp, an `in-pr` naming no PR, a
    `self-accepted` missing any ACT condition's evidence — none of these is an entry to argue with. It is
    an entry that cannot exist, and it does not load.

    The message names EVERY missing field of the CLOSEST alternative, so a corrupt store says what is
    wrong with it rather than merely refusing.
    """
    alts = WITNESS[entry["state"]]
    missing = min((sorted(a for a in alt if is_blank(entry[a])) for alt in alts), key=len, default=[])
    if not missing:
        return None
    ways = " or ".join("/".join(sorted(alt)) for alt in alts)
    return (
        f"state {entry['state']!r} with no {', '.join(missing)} — no legal history produces that. "
        f"Reaching {entry['state']!r} requires: {ways}. The entry was hand-written, or written by "
        f"something that is not this accessor."
    )


def read_store(path: Path) -> "tuple[list[dict], int]":
    """Return the entries AND the id high-water mark. A missing file is an EMPTY store — not an error.

    Every record must be `{"type": "followup", …}` or the ONE meta record (`{"type": "followup-seq"}`, the
    high-water mark below); an unknown type is REJECTED, never skipped (a silently dropped entry is exactly
    the loss this store exists to prevent). Values are coerced to `str`, so an on-disk JSON number compares
    as the string key the rest of the accessor uses. `id` and `state` are validated: a malformed id could
    never be addressed again, and an unrecognised state would sit in the table as something no transition
    can move — which is also what refuses a `DELETED` tombstone, since deletion leaves no entry at all.

    And the STATE ITSELF IS VALIDATED AGAINST ITS OWN HISTORY (`illegal_history()`) — the transitions
    cannot be the only guard, because they only ever see entries this accessor wrote.

    THE HIGH-WATER MARK IS WHY DELETION DOES NOT REUSE AN ID. `next_id()` counts past the highest id EVER
    HANDED OUT, and once entries can be DELETED the surviving entries no longer remember what that was:
    delete `fu7` of seven and the highest id present is `fu6`, so the next `add` would hand out `fu7` a
    SECOND time — silently re-pointing every reference to the old one (a merged PR body, the user's own
    note) at a different follow-up. So the mark is persisted, ONE line, not one per deletion: the deleted
    entry is really gone, and its id is still never reused.

    A store with no mark (one written before it existed — the live 7-entry store is exactly that) is not
    corrupt: the mark is BACKFILLED from the highest id present, which is what it would have been.
    """
    entries: list[dict] = []
    high = 0
    if not path.exists():
        return entries, high
    seen: set[str] = set()
    marked = False
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            fail(f"malformed JSON on line {n}: {e}")
        if not isinstance(rec, dict):
            fail(f"line {n}: record is not a JSON object")
        if rec.get("type") == SEQ_TYPE:
            if marked:
                fail(f"line {n}: a second {SEQ_TYPE} record — the store holds ONE high-water mark")
            marked = True
            try:
                high = int(rec.get("high", 0))
            except (TypeError, ValueError):
                fail(f"line {n}: {SEQ_TYPE} carries a non-numeric high-water mark {rec.get('high')!r}")
            if high < 0:
                fail(f"line {n}: {SEQ_TYPE} carries a negative high-water mark {high}")
            continue
        if rec.get("type") != ENTRY_TYPE:
            fail(f"line {n}: missing or unknown record type {rec.get('type')!r}")
        entry = {f: str(rec.get(f, DEFAULTS[f])) for f in FIELDS}
        if not ID_RE.match(entry["id"]):
            fail(f"line {n}: malformed id {entry['id']!r} (expected fu<N>)")
        if entry["state"] not in STATES:
            fail(f"line {n}: unknown state {entry['state']!r}; valid: {', '.join(STATES)}")
        if entry["id"] in seen:
            fail(f"line {n}: duplicate entry for {entry['id']}")
        why = illegal_history(entry)
        if why is not None:
            fail(f"line {n}: {entry['id']} is {why}")
        seen.add(entry["id"])
        entries.append(entry)
    return entries, high_water(entries, high)


def high_water(entries: "list[dict]", high: int) -> int:
    """The highest id ever handed out: the mark on disk, or the highest id present if it is higher.

    ONE definition, used on the way IN (a store with no mark, or one an older version wrote) and on the way
    OUT (`dump`) — so a mark can never be written that is LOWER than an id already in the store, which is
    the only way this could hand the same id out twice.
    """
    return max([high] + [int(e["id"][2:]) for e in entries])


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
    path.parent.mkdir(parents=True, exist_ok=True)
    high = high_water(entries, high)
    body = json.dumps({"type": SEQ_TYPE, "high": high}) + "\n" if high else ""
    body += "".join(
        json.dumps({"type": ENTRY_TYPE, **{f: str(e.get(f, DEFAULTS[f])) for f in FIELDS}}) + "\n"
        for e in entries
    )
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
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    with open(lock, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def deletable(entry: dict) -> bool:
    """May this entry be REMOVED? Only if it names a record that outlives it (`DURABLE_RECORD`).

    THE ONE QUESTION DELETION TURNS ON, spelled once. It is the SECOND lock on that door, and it is
    honest about being one: TODAY the graph alone already makes an undeletable entry unreachable — every
    legal history that arrives at a deleting edge has written the PR or the issue first, and
    `t_deletion_needs_a_durable_record` PROVES that on the graph rather than hoping for it, which is also
    why no CLI sequence can make this refuse. So this is what still holds if a future edge, state or
    witness change quietly stops being true — the check is here, at the moment the data is destroyed, and
    the fixture calls it directly rather than pretending a store can be driven into it.
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

def cmd_add(path: Path, args) -> int:
    with locked(path):
        entries, high = read_store(path)
        entry = dict(DEFAULTS)
        for f in REQUIRED:
            value = getattr(args, f)
            if is_blank(value):  # THE one predicate — `-` is what an UNSET field holds, so it is not a value
                fail(f"--{f.replace('_', '-')} must not be empty — a follow-up without it is a rumor")
            entry[f] = value
        entry["id"] = next_id(high)   # assigned here; never caller-set, never reused — not even after a
        entry["state"] = "candidate"  # DELETION took the highest id out of the store (see `read_store`)
        entry["found_run"] = args.run or "-"
        entry["found"] = args.found or now_iso()
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

    The check is DERIVED from REQUIRED. Hand-listing the fields here is how it rots: the next field added
    to REQUIRED would be pinned at `add` and blankable through this door on the day it is added.
    """
    with locked(path):
        entries, high = read_store(path)
        entry = find(entries, args.id)
        if entry is None:
            fail(f"no follow-up {args.id}")
        updates = {f: getattr(args, f) for f in EDITABLE if getattr(args, f) is not None}
        if not updates:
            fail(f"set requires at least one --<field> <value>; editable: {', '.join(EDITABLE)}")
        for f in REQUIRED:  # DERIVED from REQUIRED — never a list of field names retyped here
            if f in updates and is_blank(updates[f]):
                fail(f"--{f.replace('_', '-')} must not be EMPTIED — a follow-up without it is a rumor, "
                     f"and this one the store already vouched for")
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
    this store (`DURABLE_RECORD` — the PR that merged, the issue it was published as). An entry deleted
    with neither is work that exists nowhere. The step still PRINTS the removed entry, in full: that record
    is the driver's handoff, and it names where the follow-up now lives.
    """
    cmd = args.cmd
    frm, to = TRANSITIONS[cmd]
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
        # The user's ruling is DURABLE DATA, exactly like the ledger's `api_approval`: a later run — or a
        # fresh agent that never saw the conversation — reads it and does not re-ask.
        stamp = getattr(args, "at", None) or now_iso()
        for field in WRITES[cmd]:
            if field in OPTIONAL:
                entry[field] = stamp
                continue
            value = getattr(args, field)
            if is_blank(value):  # THE one predicate — see `is_blank()`. A `-` witness writes an ILLEGAL
                fail(f"{FLAG[field]} must not be empty — {BLANK_WHY[field]}")  # history the store cannot open
            entry[field] = (append_finding(entry[field], to, stamp, value) if field == "finding"
                            else value)
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


# --- self-test: the fixtures ARE the contract ---------------------------------
#
# EVERY FIXTURE MUST PIN A RULE — it must go red if its rule is deleted or weakened. A fixture that would
# still pass with its rule gone tests nothing and manufactures false confidence.
#
# The rendering fixtures lean on the LEDGER's oracle (`ledger.grid`) and its HOSTILE corpus on purpose:
# this store prints through the ledger's `escape_cell()`/`grid_lines()`, so it must be checked by the same
# parser that checks the ledger — a second, weaker copy of the oracle would be free to bless output the
# real one rejects.


def run(argv: "list[str]") -> "tuple[int, str, str]":
    """Drive the REAL CLI in-process and capture (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
    except SystemExit as exc:  # fail() -> 1; argparse -> 2
        code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def entry_line(**over: str) -> str:
    """A raw store line for an entry in some state — with the evidence that state REQUIRES filled in.

    Derived from `WITNESS`, so a fixture asking for a `self-accepted` entry gets a LEGAL one without
    restating what a self-acceptance must carry (and a fixture that wants an ILLEGAL one blanks a witness
    on purpose — see `t_load_rejects_an_illegal_history`). A new state, or a new witness on an existing
    one, is filled here the day the graph gains it, with no fixture edit.
    """
    state = over.get("state", DEFAULTS["state"])
    witness = min(WITNESS[state], key=len) if WITNESS.get(state) else frozenset()
    return json.dumps({"type": "followup", **DEFAULTS,
                       **{f: f"<{f}>" for f in witness}, **over})


def transition_args(cmd: str) -> "list[str]":
    """The flags a transition REQUIRES — derived from `WRITES`, never retyped.

    So every graph fixture exercises a new evidence-bearing edge the day it is added: forget to pass a
    required flag and argparse exits 2, which is a fixture failure, not a silent skip.
    """
    argv: list[str] = []
    for field in WRITES[cmd]:
        if field in OPTIONAL:
            continue
        argv += [FLAG[field], f"{cmd}:{field}"]
    return argv


# EVERY spelling of "carries nothing" that `is_blank()` recognises — the vocabulary EVERY door must
# refuse, used by every fixture that tests a blank. PLACEHOLDER is IN IT, and that is the whole point: it
# is what an UNSET field holds, so a door that accepts it writes an entry that reads back EMPTY. Spelled
# once, here: a fixture carrying its own private list of blanks is how `-` slipped past three doors at
# once while every one of them looked tested.
BLANKS = ("", "   ", "\t", PLACEHOLDER)


def write_lines(path: Path, *lines: str) -> Path:
    """Write a store RAW — bypassing dump(), so a fixture can hold what dump() would never emit."""
    path.write_text("".join(line + "\n" for line in lines))
    return path


def drive_to(path: Path, fid: str, target: str) -> None:
    """Move an entry to `target` along the GRAPH — the shortest legal path, derived from TRANSITIONS.

    So a fixture that needs an entry in some state does not restate how one gets there; add an edge and the
    path is re-derived. A BFS, because the shortest legal route is the one with the fewest witnesses to
    invent along the way.
    """
    paths = {"candidate": []}
    queue = ["candidate"]
    while queue:
        state = queue.pop(0)
        for cmd, (frm, to) in TRANSITIONS.items():
            if to == DELETED:
                continue  # there is no entry at the other end of a deleting edge to drive anywhere
            if state in frm and to not in paths:
                paths[to] = paths[state] + [cmd]
                queue.append(to)
    check(target in paths, f"no legal path reaches {target!r} — the graph cannot produce this fixture")
    for cmd in paths[target]:
        code, _, err = run(["--file", str(path), cmd, "--id", fid, *transition_args(cmd)])
        check(code == 0, f"driving to {target!r}: `{cmd}` exited {code}: {err!r}")


def seed(path: Path, n: int = 1) -> "list[str]":
    ids = []
    for i in range(n):
        code, out, err = run(["--file", str(path), "add", "--title", f"t{i}",
                              "--evidence", f"e{i}", "--deferred-why", f"w{i}"])
        check(code == 0, f"add exited {code}: {err!r}")
        ids.append(json.loads(out)["id"])
    return ids


def state_of(path: Path, fid: str) -> str:
    code, out, err = run(["--file", str(path), "get", "--id", fid, "--field", "state"])
    check(code == 0, f"get exited {code}: {err!r}")
    return out.strip()


def closure(start: "tuple[str, ...]", cmds: "tuple[str, ...]") -> "set[str]":
    """Every STATE reachable from `start` using ONLY the named transitions. A fixpoint over the graph.

    A deleting edge contributes no state — the entry is gone, and `DELETED` is not somewhere to be.
    """
    seen = set(start)
    changed = True
    while changed:
        changed = False
        for cmd in cmds:
            frm, to = TRANSITIONS[cmd]
            if to != DELETED and to not in seen and any(f in seen for f in frm):
                seen.add(to)
                changed = True
    return seen


def t_user_ruling_is_unskippable(tmp: Path) -> None:
    """THE DRIVER CAN NEVER REACH `accepted`, NOR RUN `publish`, ON ITS OWN — proved ON THE GRAPH, not on
    one lucky path.

    THE load-bearing rule of this store, and the ONE that neither the ACT tier nor DELETION was allowed to
    break. The driver may investigate freely, and it may TAKE UP a corroborated follow-up for work — but
    publication is a claim made in the USER's name, so `publish` leaves only from `accepted`, `accepted`
    has exactly one in-edge, and that edge is the user's.

    `publish` now DELETES the entry rather than parking it in a `published` state, and that changes NOTHING
    here: the guarantee was never about the state it landed in, it was about which states the STEP may
    leave FROM. So the property is stated on the step — every state `publish` leaves from must be
    unreachable without the user — which is the same claim before and after, and is what this checks.

    Checked three ways, every one of them DERIVED from TRANSITIONS — so an edge added tomorrow that routes
    around the user goes red here rather than shipping:

      1. the in-edges. `accepted` has exactly one and it is `accept`, a USER ruling.
      2. the CLOSURE over every driver-only step (everything that is not a user ruling): from `candidate`
         it reaches the investigation outcomes and the ACT state — and NEITHER `accepted` NOR any state
         `publish` may leave from. This is the property in full: not "there is no direct edge", but "there
         is no PATH".
      3. the same closure from `self-accepted` — the autonomous state, the one a bypass would be built out
         of. It reaches work (a PR, a merge); it never reaches publication.

    Then the same thing END-TO-END through the real CLI, because a graph that is right and an accessor that
    does not enforce it is a comment.
    """
    # The states the USER's ruling gates: `accepted` itself, and whatever `publish` may leave FROM — derived
    # from the graph, so moving the publish edge cannot quietly move the guarantee.
    gated = {"accepted"} | set(TRANSITIONS["publish"][0])

    # 1. the in-edges.
    ins = [c for c, (_, to) in TRANSITIONS.items() if to == "accepted"]
    check(ins == ["accept"], f"`accepted` has in-edges {ins!r} — `accept` must be the ONLY one")
    check("accept" in USER_RULINGS, "`accept` is not a USER ruling — the gate would be the driver's own")
    check(TRANSITIONS["publish"][0] == ("accepted",),
          f"`publish` leaves from {TRANSITIONS['publish'][0]!r}, not from `accepted` alone")

    # 2. + 3. the closure over EVERY driver-only step, from the start and from the ACT state.
    for start in ("candidate", TRANSITIONS[ACT_CMD][1]):
        reach = closure((start,), DRIVER_STEPS)
        check(not (reach & gated),
              f"a driver with NO user ruling reaches {sorted(reach & gated)!r} from {start!r} — the user "
              f"is BYPASSABLE (reachable: {sorted(reach)})")
    autonomous = closure(("candidate",), DRIVER_STEPS)
    # …and the tier is REAL, not decorative: the investigation outcomes and the ACT state ARE reachable.
    expected = {to for c, (_, to) in TRANSITIONS.items()
                if (c in INVESTIGATION or c == ACT_CMD) and to != DELETED}
    check(expected <= autonomous,
          f"the driver cannot reach {sorted(expected - autonomous)!r} on its own — the INVESTIGATE/ACT "
          f"tiers do not exist")

    # …and end-to-end: EVERY state the driver can reach alone is refused publication — including the ones
    # it reaches by taking work up and opening a PR on it. Derived from the closure, not hand-listed.
    for state in sorted(autonomous):
        path = tmp / f"start-{state}.jsonl"
        (fid,) = seed(path)
        drive_to(path, fid, state)
        code, out, err = run(["--file", str(path), "publish", "--id", fid, "--ref", "#123"])
        check(code == 1,
              f"`publish` was ACCEPTED on a {state!r} follow-up (exit {code}) — the user was skipped:\n{out}")
        check("applies only to" in err, f"publish failed for the wrong reason: {err!r}")
        check(state_of(path, fid) == state, f"a refused publish still moved {state!r}")

    # …and the ONLY route through: accept (the user agreed), and only then publish — which DELETES it,
    # because the issue is now the record.
    path = tmp / "route.jsonl"
    (fid,) = seed(path)
    check(run(["--file", str(path), "accept", "--id", fid])[0] == 0, "accept must succeed on a candidate")
    check(state_of(path, fid) == "accepted", "accept did not reach `accepted`")
    code, out, err = run(["--file", str(path), "publish", "--id", fid, "--ref", "#123"])
    check(code == 0, f"publish must succeed on an ACCEPTED follow-up: {err!r}")
    check(json.loads(out)["published"] == "#123", f"the deletion record does not name the issue: {out!r}")
    check(load(path) == [], "the published follow-up was KEPT — the issue is the record now")


def t_state_and_evidence_are_not_settable(tmp: Path) -> None:
    """`set` CANNOT WRITE `state`, AND IT CANNOT WRITE ANY EVIDENCE FIELD — not by flag, not by name.

    The transitions check where a follow-up is coming FROM; `set` does not. A settable `state` would walk
    straight past `accept` — `set --state published` — and the whole graph would be decoration.

    THE SAME IS TRUE OF EVERY WITNESS. `load()` now admits a `self-accepted` entry only because it carries
    the evidence for each ACT condition; a `set --act-reversible ''` would let the driver assert the
    conditions, take the work up, and then rewrite or hollow out the grounds it acted on — and the entry
    would still load, because the state was legal WHEN IT WAS WRITTEN. So the rule is not "state is frozen"
    but "NOTHING A TRANSITION WROTE IS EDITABLE", and it is derived from `EVIDENCE_FIELDS`: a new witness
    is covered here the day the graph gains it.
    """
    path = tmp / "f.jsonl"
    (fid,) = seed(path)
    # (`id` is absent: it is `set`'s KEY, not one of its fields.)
    for field in ("state", "found", "found_run", *EVIDENCE_FIELDS):
        flag = "--" + field.replace("_", "-")
        code, _, err = run(["--file", str(path), "set", "--id", fid, flag, "x"])
        check(code == 2, f"`set {flag}` was ACCEPTED (exit {code}) — it must not be a flag at all: {err!r}")
        check(field not in EDITABLE,
              f"{field!r} is EDITABLE — a transition wrote it, and `set` could now rewrite the record of "
              f"what happened")
    check(state_of(path, fid) == "candidate", "a rejected `set` moved the state anyway")

    # …and the prose fields ARE editable (the rule is targeted, not a blanket freeze).
    code, out, err = run(["--file", str(path), "set", "--id", fid, "--evidence", "PR #9, review 2"])
    check(code == 0, f"set --evidence exited {code}: {err!r}")
    check(json.loads(out)["evidence"] == "PR #9, review 2", f"set did not write the field: {out!r}")


def t_transition_graph(tmp: Path) -> None:
    """EVERY transition is checked against the graph — DERIVED from TRANSITIONS, never a retyped list.

    For each (command, state) pair the graph does not allow, the command must be REFUSED and the state
    must not move. A new state or edge is covered the moment it is added to `TRANSITIONS`: this fixture
    reads the graph rather than restating it, so it cannot go stale behind it.

    A DELETING edge is checked the same way, on its own terms: from an allowed state the entry is GONE (and
    the store still LOADS); from a forbidden one it is untouched. Nothing else may remove an entry.
    """
    for cmd, (frm, to) in TRANSITIONS.items():
        for state in STATES:
            path = tmp / f"{cmd}-{state}.jsonl"
            write_lines(path, entry_line(id="fu1", state=state))
            code, _, err = run(["--file", str(path), cmd, "--id", "fu1", *transition_args(cmd)])
            if state in frm:
                check(code == 0, f"`{cmd}` was refused from the ALLOWED state {state!r}: {err!r}")
                if to == DELETED:
                    code, out, err = run(["--file", str(path), "list"])
                    check(code == 0, f"after `{cmd}` from {state!r} the STORE NO LONGER LOADS: {err!r}")
                    check(out == "", f"`{cmd}` from {state!r} left the entry in the store: {out!r}")
                else:
                    check(state_of(path, "fu1") == to, f"`{cmd}` from {state!r} did not reach {to!r}")
            else:
                check(code == 1, f"`{cmd}` was ACCEPTED from {state!r}, which the graph forbids (exit {code})")
                check(state_of(path, "fu1") == state,
                      f"a refused `{cmd}` still moved {state!r} to {state_of(path, 'fu1')!r}")

    # AN INVESTIGATION OUTCOME IS NEVER TERMINAL, and that is a rule, not an accident. A refutation is the
    # driver's own uncorroborated claim ABOUT its own uncorroborated claim. If `refuted` had no way out,
    # the driver could CLOSE a follow-up by investigating it badly — the mirror image of publishing one
    # unilaterally, and just as unappealable. So the user can always overturn it, and so can a better
    # investigation. (Terminality itself needs no separate check: the loop above already proves that every
    # command is refused from every state the graph does not allow it from.)
    for cmd in INVESTIGATION:
        outcome = TRANSITIONS[cmd][1]
        out_edges = [c for c, (frm, _) in TRANSITIONS.items() if outcome in frm]
        check(outcome not in TERMINAL,
              f"{outcome!r} is TERMINAL — an investigation could close a follow-up with no user ruling")
        check("accept" in out_edges,
              f"the USER cannot `accept` a {outcome!r} follow-up — the driver's own investigation is final")
        check(any(c in INVESTIGATION for c in out_edges),
              f"no further investigation can leave {outcome!r} — the FIRST investigation is authoritative, "
              f"and a driver that got it wrong can never correct itself")


def t_ruling_is_recorded(tmp: Path) -> None:
    """The USER'S RULING is stamped into `decided`; the driver's own bookkeeping is NOT.

    A ruling that is not durable gets re-asked by the next wake — a fresh agent never saw the
    conversation. It is the same reason the ledger's `api_approval` records `approved@<iso>` rather than
    living in the driver's head. `publish`/`open-pr` are the driver's own steps and must NOT stamp it: a
    `decided` written by anything but the user would launder the driver's action into the user's consent.
    """
    path = tmp / "f.jsonl"
    a, b = seed(path, 2)
    check(state_of(path, a) == "candidate" and json.loads(run(
        ["--file", str(path), "get", "--id", a])[1])["decided"] == "-",
        "a fresh candidate must carry NO ruling")

    run(["--file", str(path), "accept", "--id", a, "--at", "2026-07-14T09:00:00Z"])
    got = json.loads(run(["--file", str(path), "get", "--id", a])[1])
    check(got["decided"] == "2026-07-14T09:00:00Z", f"accept did not record the ruling: {got!r}")

    run(["--file", str(path), "reject", "--id", b, "--at", "2026-07-14T10:00:00Z"])
    check(json.loads(run(["--file", str(path), "get", "--id", b])[1])["decided"] == "2026-07-14T10:00:00Z",
          "reject did not record the ruling")

    # …and a driver-only transition leaves the user's ruling exactly as the user left it.
    run(["--file", str(path), "open-pr", "--id", a, "--pr", "#77"])
    after = json.loads(run(["--file", str(path), "get", "--id", a])[1])
    check(after["decided"] == "2026-07-14T09:00:00Z",
          f"`open-pr` overwrote the USER's ruling timestamp: {after!r}")
    check(after["pr"] == "#77", f"open-pr did not record WHICH PR is addressing it: {after!r}")

    # …and so does the step that DELETES it: the record it prints is the handoff, and it still says the
    # user ruled, and when.
    code, out, err = run(["--file", str(path), "reject", "--id", a, "--at", "2026-07-14T11:00:00Z"])
    check(code == 0, f"reject exited {code}: {err!r}")  # the user changed their mind while the PR was open
    (b2,) = seed(path)
    run(["--file", str(path), "accept", "--id", b2, "--at", "2026-07-14T12:00:00Z"])
    code, out, _ = run(["--file", str(path), "publish", "--id", b2, "--ref", "#88"])
    gone = json.loads(out)
    check(gone["decided"] == "2026-07-14T12:00:00Z",
          f"the deletion record lost the USER's ruling — nothing says the publish was theirs: {gone!r}")
    check(gone["published"] == "#88", f"publish did not record WHERE: {gone!r}")

    # …and NOTHING THE DRIVER DOES ALONE STAMPS IT. An investigation is evidence, not consent; a take-up is
    # the driver's own call, not the user's. A `decided` written by either would launder the driver's action
    # into the user's agreement — and `decided` is exactly what `load()` demands of an `accepted` entry, so
    # a driver that could stamp it could forge one.
    stampers = {c for c, w in WRITES.items() if "decided" in w}
    check(stampers == set(USER_RULINGS),
          f"`decided` is stamped by {sorted(stampers)!r} — it must be stamped by the USER's rulings "
          f"({sorted(USER_RULINGS)}) and by NOTHING else")
    path = tmp / "driver.jsonl"
    (c,) = seed(path)
    for cmd in ("corroborate", ACT_CMD):
        code, _, err = run(["--file", str(path), cmd, "--id", c, *transition_args(cmd)])
        check(code == 0, f"`{cmd}` exited {code}: {err!r}")
        got = json.loads(run(["--file", str(path), "get", "--id", c])[1])
        check(got["decided"] == PLACEHOLDER,
              f"`{cmd}` stamped `decided` ({got['decided']!r}) — the DRIVER's own step was recorded as the "
              f"USER's ruling")


def t_publish_needs_a_ref(tmp: Path) -> None:
    """`publish` must name WHERE it published. A published follow-up with no reference is unfindable —
    and the next run, seeing no link, raises it a second time."""
    path = tmp / "f.jsonl"
    (fid,) = seed(path)
    run(["--file", str(path), "accept", "--id", fid])
    code, _, err = run(["--file", str(path), "publish", "--id", fid])
    check(code == 2, f"publish without --ref was accepted (exit {code}): {err!r}")
    code, _, err = run(["--file", str(path), "publish", "--id", fid, "--ref", "  "])
    check(code == 1, f"publish with a BLANK --ref was accepted (exit {code})")
    check(state_of(path, fid) == "accepted", "a refused publish moved the state anyway")


def t_evidence_is_required(tmp: Path) -> None:
    """A FOLLOW-UP WITH NO EVIDENCE IS A RUMOR — `add` refuses one, and refuses a blank one.

    A store of unfalsifiable claims is worse than no store: nobody can audit an entry that says only "the
    merge logic looks wrong". `deferred_why` is required on the same terms — without it the next run
    cannot see why the finding was not simply folded into the PR that found it, and re-litigates it.

    BOTH LOOPS RUN OVER `REQUIRED` — the missing-flag case AND the blank-value case. Hand-listing the blank
    case is how the rule rots: with only `evidence` blank-tested, `cmd_add`'s blank check could be narrowed
    to `evidence` alone — dropping `title` and `deferred_why` outright — and this suite stayed GREEN through
    it. A field added to REQUIRED tomorrow is pinned here with no edit.
    """
    path = tmp / "f.jsonl"
    for missing in REQUIRED:
        argv = ["--file", str(path), "add"]
        for f in REQUIRED:
            if f != missing:
                argv += [f"--{f.replace('_', '-')}", "x"]
        code, _, err = run(argv)
        check(code == 2, f"add without --{missing} was ACCEPTED (exit {code}): {err!r}")
    for blanked in REQUIRED:
        for blank in BLANKS:
            argv = ["--file", str(path), "add"]
            for f in REQUIRED:
                argv += [f"--{f.replace('_', '-')}", blank if f == blanked else "x"]
            code, _, err = run(argv)
            check(code == 1,
                  f"add with a BLANK --{blanked} ({blank!r}) was ACCEPTED (exit {code}) — the field is "
                  f"REQUIRED, so a value made only of whitespace is not a value")
            check("rumor" in err, f"add failed for the wrong reason: {err!r}")
    check(load(path) == [], "a REFUSED add still wrote an entry to the store")


def t_required_cannot_be_edited_away(tmp: Path) -> None:
    """A REQUIRED FIELD IS REQUIRED AT EVERY DOOR AN ENTRY CAN CHANGE — not only at the one that made it.

    `add` refusing a blank `evidence` guards NOTHING if `set --evidence '   '` hollows the entry out an hour
    later. The result is the same rumor the store exists to refuse, and a worse one: this claim the store
    has already VOUCHED for, because it was checked once, at a door it is no longer standing at. The rule
    was enforced where an entry is CREATED and not where it is CHANGED.

    THE LOOP RUNS OVER `REQUIRED` × `BLANKS` — never a hand-list. A field added to REQUIRED tomorrow is
    pinned at this door with no edit here, and every spelling of blank is tried, INCLUDING the placeholder.
    Delete the guard in `cmd_set` and this goes red for `title` first.
    """
    path = tmp / "f.jsonl"
    (fid,) = seed(path)
    before = json.loads(run(["--file", str(path), "get", "--id", fid])[1])
    for field in REQUIRED:
        if field not in EDITABLE:
            continue  # not settable at all — a different door, pinned by `state-not-settable`
        flag = f"--{field.replace('_', '-')}"
        for blank in BLANKS:
            code, _, err = run(["--file", str(path), "set", "--id", fid, flag, blank])
            check(code == 1,
                  f"`set {flag} {blank!r}` was ACCEPTED (exit {code}) — {field!r} is REQUIRED, and a value "
                  f"made only of whitespace (or the placeholder an UNSET field holds) is not a value")
            check("rumor" in err, f"`set {flag}` failed for the wrong reason: {err!r}")
            # …and the REFUSAL is total: the field it could not blank still holds what it held.
            now = json.loads(run(["--file", str(path), "get", "--id", fid])[1])
            check(now == before, f"a REFUSED `set {flag} {blank!r}` changed the entry anyway: {now!r}")

    # …and a real edit still lands: the rule refuses an EMPTYING, not an edit (see `state-not-settable`).
    code, out, err = run(["--file", str(path), "set", "--id", fid, "--evidence", "reproduced at line 488"])
    check(code == 0, f"a NON-blank set exited {code}: {err!r}")
    check(json.loads(out)["evidence"] == "reproduced at line 488", f"set did not write the field: {out!r}")


def t_no_door_writes_a_store_that_will_not_load(tmp: Path) -> None:
    """WHATEVER A WRITE DOOR ACCEPTS, `load()` MUST ACCEPT BACK. The blank predicate is ONE: `is_blank()`.

    The doors used to test `value.strip()` while `load()` tested `is_blank()` — which ALSO reads the
    PLACEHOLDER `-` (what an UNSET field holds) as carrying nothing. So `-` passed the WRITE check and
    failed the READ check, and the two doors of one store disagreed about what "carries nothing" means.

    That is not a cosmetic gap. `take-up --act-not-gate - --act-behavior - --act-reversible -` exited 0 and
    wrote the entry — and the store then WOULD NOT LOAD. Not the entry: the STORE. Every later command
    (`table`, `get`, `list`, `add`, every transition) exits 1 on an illegal history, and these follow-ups
    have NO OTHER COPY ANYWHERE. A door that can write a store its own accessor cannot open is the worst
    failure this file has, and it was reachable through the ordinary CLI with nothing hand-edited.

    Derived from WRITES/FLAG/BLANKS, so a new evidence-bearing edge is pinned the day it is added. Restore
    either `.strip()` check in `cmd_transition` and this goes red — on the `-` case, and on the STORE.
    """
    for cmd, fields in WRITES.items():
        required = [f for f in fields if f not in OPTIONAL]
        for field in required:
            frm, _ = TRANSITIONS[cmd]
            path = tmp / f"{cmd}-{field}.jsonl"
            (fid,) = seed(path)
            drive_to(path, fid, frm[0])
            was = state_of(path, fid)
            for blank in BLANKS:
                values = [a for f in required for a in (FLAG[f], blank if f == field else "x")]
                code, _, err = run(["--file", str(path), cmd, "--id", fid, *values])
                check(code == 1,
                      f"`{cmd}` with a BLANK {FLAG[field]} ({blank!r}) was ACCEPTED (exit {code}) — a "
                      f"witness that carries nothing is not a witness")
                # THE POINT: the refusal is what keeps the store READABLE. Had it been written, this fails.
                code, _, err = run(["--file", str(path), "list"])
                check(code == 0,
                      f"after `{cmd} {FLAG[field]} {blank!r}` the STORE ITSELF NO LONGER LOADS (exit "
                      f"{code}) — a write door wrote a history `load()` calls illegal: {err!r}")
            check(state_of(path, fid) == was, f"a refused `{cmd}` moved the state anyway")

    # …AND WHAT A DOOR ACCEPTS IS READ BACK. The half above proves the REFUSALS keep the store readable; a
    # store that refused everything would pass it. So every edge is also driven for real — with legal
    # arguments, from a legal state — and the store must LOAD AFTERWARDS. This is what a DELETING edge and
    # the id high-water mark it leaves behind are pinned by: they are the newest thing a write door emits,
    # and the file they emit it into is the one file nothing can rebuild.
    for cmd, (frm, _) in TRANSITIONS.items():
        path = tmp / f"legal-{cmd}.jsonl"
        (fid,) = seed(path)
        drive_to(path, fid, frm[0])
        code, _, err = run(["--file", str(path), cmd, "--id", fid, *transition_args(cmd)])
        check(code == 0, f"`{cmd}` was refused from the legal state {frm[0]!r}: {err!r}")
        code, _, err = run(["--file", str(path), "list"])
        check(code == 0,
              f"after a LEGAL `{cmd}` the STORE ITSELF NO LONGER LOADS (exit {code}) — the door wrote "
              f"something its own `load()` refuses, and these follow-ups have no other copy: {err!r}")
        code, _, err = run(["--file", str(path), "add", "--title", "t", "--evidence", "e",
                            "--deferred-why", "w"])
        check(code == 0, f"after a LEGAL `{cmd}` the store can no longer be ADDED to: {err!r}")


def t_investigation_shows_its_work(tmp: Path) -> None:
    """AN INVESTIGATION MUST SHOW ITS WORK — `--finding` is required, non-blank, and APPENDED, never
    clobbered.

    An investigation is the one thing the driver may do with NO permission at all, so its only cost is that
    it produces EVIDENCE. Without that, `corroborated` is the driver marking its own homework — and a blank
    `refuted` is worse: it is the driver telling the user "there is nothing here", with nothing to check.

    APPEND, never clobber, and never into `evidence`. The claim's `evidence` (why the driver raised it) and
    the investigation's `finding` (what happened when somebody looked) are DIFFERENT THINGS, and a second
    investigation that overturns the first must leave the first STANDING — the record of the driver
    changing its mind IS the audit trail.
    """
    for cmd in INVESTIGATION:
        path = tmp / f"{cmd}.jsonl"
        (fid,) = seed(path)
        code, _, err = run(["--file", str(path), cmd, "--id", fid])
        check(code == 2, f"`{cmd}` without --finding was ACCEPTED (exit {code}): {err!r}")
        for blank in BLANKS:
            code, _, err = run(["--file", str(path), cmd, "--id", fid, "--finding", blank])
            check(code == 1, f"`{cmd}` with a BLANK finding ({blank!r}) was ACCEPTED (exit {code})")
            check("rumor" in err, f"`{cmd}` failed for the wrong reason: {err!r}")
        check(state_of(path, fid) == "candidate", f"a refused `{cmd}` moved the state anyway")

    # …and the record ACCUMULATES: a later investigation never erases an earlier one.
    path = tmp / "append.jsonl"
    (fid,) = seed(path)
    run(["--file", str(path), "refute", "--id", fid, "--finding", "no input reaches the branch",
         "--at", "2026-07-14T09:00:00Z"])
    first = json.loads(run(["--file", str(path), "get", "--id", fid])[1])
    check("no input reaches the branch" in first["finding"], f"the finding was not recorded: {first!r}")
    check("refuted" in first["finding"] and "2026-07-14T09:00:00Z" in first["finding"],
          f"the finding does not say WHICH outcome it produced, or WHEN: {first['finding']!r}")
    check(first["evidence"] == "e0",
          f"the investigation CLOBBERED the claim's own evidence: {first['evidence']!r}")

    run(["--file", str(path), "corroborate", "--id", fid, "--finding", "reproduced: blank ref, exit 0",
         "--at", "2026-07-14T10:00:00Z"])
    second = json.loads(run(["--file", str(path), "get", "--id", fid])[1])
    check("no input reaches the branch" in second["finding"],
          f"the SECOND investigation erased the first — the refutation the driver reversed is GONE, and "
          f"nobody can audit the reversal: {second['finding']!r}")
    check("reproduced: blank ref, exit 0" in second["finding"],
          f"the second finding was not recorded: {second['finding']!r}")
    check(second["state"] == "corroborated", "a later investigation could not overturn an earlier one")


def t_refutation_stays_in_the_store(tmp: Path) -> None:
    """A REFUTED FOLLOW-UP STAYS — IN THE STORE, IN THE VIEW, AND THE USER'S TO OVERTURN.

    The driver refuting its OWN claim is precisely the case the user must be able to audit. Refuting is not
    deleting and it is not closing: if a refuted entry vanished from the store — or merely from the default
    table — a driver could bury a real defect by investigating it badly, silently, with nothing left behind.
    This repo has already burned four review rounds on a bug that was never real; the OPPOSITE mistake,
    refuting one that IS, must cost nothing to catch.
    """
    path = tmp / "f.jsonl"
    (fid,) = seed(path)
    run(["--file", str(path), "refute", "--id", fid, "--finding", "cannot reproduce on main"])
    check(state_of(path, fid) == "refuted", "refute did not reach `refuted`")

    code, out, _ = run(["--file", str(path), "list"])
    check(out == f"{fid}\n", f"a refuted follow-up was DROPPED from the store: {out!r}")
    code, out, err = run(["--file", str(path), "table", "--fields", "id,state"])
    check(code == 0, f"table exited {code}: {err!r}")
    _, _, cells = ledger.grid(out, ("id", "state"), ("store", "rule"), TABLE_MARKERS)
    check(cells == [[fid, "refuted"]],
          f"a refuted follow-up is HIDDEN by the default view — the driver's own refutation is unauditable "
          f"unless the user already knows to look: {cells!r}\n{out}")
    check("refuted" not in TABLE_HIDDEN_STATES, "`refuted` is hidden by default")

    # …and the USER can overturn it, with the refutation's evidence still there to be judged.
    check(run(["--file", str(path), "accept", "--id", fid])[0] == 0,
          "the USER cannot accept what the DRIVER refuted — the driver's investigation is unappealable")
    after = json.loads(run(["--file", str(path), "get", "--id", fid])[1])
    check(after["state"] == "accepted", f"accept from `refuted` did not reach `accepted`: {after!r}")
    check("cannot reproduce on main" in after["finding"],
          f"the refutation's evidence was destroyed when the user overturned it: {after!r}")


def t_deletion_needs_a_durable_record(tmp: Path) -> None:
    """AN ENTRY IS DELETED ONLY ONCE ITS RECORD LIVES SOMEWHERE ELSE — and NEVER WHEN THE WORK MERELY STARTS.

    The store is a WORK QUEUE, not an archive: it is local and git-ignored, so it is a poor archive and a
    fine queue. But the DELETION IS THE DANGEROUS HALF, and the whole safety of it is one question — is
    there a record ELSEWHERE? The merged PR is on GitHub, reviewable, and is where anyone actually looks
    for "why did we do this". The issue is the same. The entry can go, because the fact did not.

    DELETE IT ON TAKE-UP INSTEAD AND THE STORE LOSES THE ONE THING IT EXISTS TO KEEP. A PR can be closed,
    abandoned, or rejected in review; the work is then still undone, and the entry that remembered it is
    gone. That is the exact permanent loss this store was built to prevent, moved later in time — so the
    entry survives take-up, survives the PR opening, and dies only on the MERGE.

    Checked STRUCTURALLY first, on the graph — for every deleting edge, EVERY legal history that could
    reach it must have left a durable record in the entry — so a deleting edge added tomorrow from some
    state that has none goes red here rather than shipping.
    """
    check(DELETING, "NOTHING deletes an entry — the store is an archive again, and it only grows")
    for cmd in DELETING:
        frm, _ = TRANSITIONS[cmd]
        for state in frm:
            for alt in WITNESS[state]:
                carried = set(alt) | set(WRITES[cmd])
                check(carried & set(DURABLE_RECORD),
                      f"`{cmd}` deletes a {state!r} entry — and a legal history reaches {state!r} carrying "
                      f"only {sorted(alt)!r}, NONE of {list(DURABLE_RECORD)}. The work would be gone with "
                      f"nothing, anywhere, to remember it.")

    # …and the DOOR asks the same question, for the day the graph stops answering it. `deletable()` is what
    # `cmd_transition` calls before it destroys anything, and it is exercised DIRECTLY — because the check
    # above is precisely the proof that no CLI sequence can reach it (an entry with no durable record does
    # not even LOAD). An untested fail-safe is not a fail-safe; a fail-safe pretended to be reachable is a
    # lie about the fixture.
    for f in DURABLE_RECORD:
        check(deletable({**DEFAULTS, f: "#123"}), f"an entry naming its record in {f!r} is NOT deletable")
    check(not deletable(dict(DEFAULTS)),
          "an entry naming NO durable record is DELETABLE — the work would be destroyed with nothing, "
          "anywhere, left to remember it")
    for blank in BLANKS:
        check(not deletable({**DEFAULTS, **{f: blank for f in DURABLE_RECORD}}),
              f"an entry whose record is {blank!r} is DELETABLE — a record that carries nothing names "
              f"nothing, and the blank predicate is ONE (`is_blank`)")

    # …and end-to-end, the entry that cannot be deleted SURVIVES the attempt (here `load()` is what refuses:
    # an `in-pr` with no PR is not a legal history at all).
    path = write_lines(tmp / "no-record.jsonl", entry_line(id="fu1", state="in-pr", pr=PLACEHOLDER))
    code, out, _ = run(["--file", str(path), "merged", "--id", "fu1"])
    check(code == 1, f"an entry naming NO durable record was DELETED (exit {code}):\n{out}")
    check("fu1" in path.read_text(), "a REFUSED deletion removed the entry anyway")

    # NOT ON TAKE-UP: the driver takes the work up, and the follow-up STAYS.
    path = tmp / "lifecycle.jsonl"
    (fid,) = seed(path)
    run(["--file", str(path), "corroborate", "--id", fid, "--finding", "reproduced"])
    code, _, err = run(["--file", str(path), ACT_CMD, "--id", fid, *transition_args(ACT_CMD)])
    check(code == 0, f"`{ACT_CMD}` exited {code}: {err!r}")
    check(find(load(path), fid) is not None,
          f"`{ACT_CMD}` DELETED the follow-up — the work has only just STARTED, the PR that would record it "
          f"does not exist yet, and a PR that never lands would take the entry with it")

    # NOR WHEN THE PR MERELY OPENS: the entry stays, and it names WHICH PR is addressing it.
    code, _, err = run(["--file", str(path), "open-pr", "--id", fid, "--pr", "#123"])
    check(code == 0, f"open-pr exited {code}: {err!r}")
    entry = json.loads(run(["--file", str(path), "get", "--id", fid])[1])
    check(entry["state"] == "in-pr", f"an open PR did not put the entry in `in-pr`: {entry!r}")
    check(entry["pr"] == "#123", f"the entry does not say WHICH PR is addressing it: {entry!r}")

    # ON THE MERGE it goes — and it is REALLY gone: not hidden, not tombstoned. Gone from the file.
    code, out, err = run(["--file", str(path), "merged", "--id", fid])
    check(code == 0, f"merged exited {code}: {err!r}")
    check(json.loads(out)["pr"] == "#123",
          f"the deletion record does not name the PR that is now the record: {out!r}")
    check(load(path) == [], "a MERGED follow-up was KEPT — the queue is an archive, and it only grows")
    check(fid not in path.read_text(),
          f"{fid} is still in the file — a deleted follow-up is DELETED, not hidden and not tombstoned")
    code, out, err = run(["--file", str(path), "list"])
    check((code, out) == (0, ""), f"the store does not LOAD after a deletion: {code} {err!r}")


def t_a_closed_pr_returns_the_entry_to_open_work(tmp: Path) -> None:
    """A PR CLOSED WITHOUT MERGING RETURNS THE ENTRY TO OPEN WORK — it does not vanish, and it does not sit
    in `in-pr` forever.

    This is what buys the right to delete on the merge. A PR can be closed, abandoned or rejected in review,
    and the work is then exactly as undone as it was before anyone touched it — so the entry goes back to
    being work, with its history (the finding, the ACT grounds or the user's ruling, and the PR that DIED)
    intact. Two failure modes are both refused here: the entry silently VANISHING with the PR, and the entry
    STUCK in "being worked on" with no way out.
    """
    reopened = TRANSITIONS["closed-unmerged"][1]
    check(reopened != DELETED,
          "a PR closed WITHOUT merging DELETES the follow-up — the work is undone and nothing remembers it")
    check("in-pr" not in TERMINAL,
          "`in-pr` is TERMINAL — an entry whose PR dies is stuck in `being worked on`, forever")
    check(reopened not in TERMINAL,
          f"{reopened!r} is TERMINAL — work whose PR died could never be picked up again")
    check(reopened not in TABLE_HIDDEN_STATES,
          f"{reopened!r} is HIDDEN by the default view — work whose PR died is invisible, which is the same "
          f"as losing it")

    for lineage, setup in (("user", ["accept"]), ("driver", ["corroborate", ACT_CMD])):
        path = tmp / f"{lineage}.jsonl"
        (fid,) = seed(path)
        for cmd in setup:
            code, _, err = run(["--file", str(path), cmd, "--id", fid, *transition_args(cmd)])
            check(code == 0, f"[{lineage}] setup `{cmd}` exited {code}: {err!r}")
        run(["--file", str(path), "open-pr", "--id", fid, "--pr", "#9"])
        code, _, err = run(["--file", str(path), "closed-unmerged", "--id", fid])
        check(code == 0, f"[{lineage}] `closed-unmerged` exited {code}: {err!r}")

        entry = json.loads(run(["--file", str(path), "get", "--id", fid])[1])
        check(entry["state"] == reopened,
              f"[{lineage}] a PR closed without merging left the entry in {entry['state']!r}: {entry!r}")
        check(entry["pr"] == "#9", f"[{lineage}] the PR that died was forgotten: {entry!r}")
        code, out, err = run(["--file", str(path), "list"])
        check((code, out) == (0, f"{fid}\n"),
              f"[{lineage}] the entry left the store when its PR was closed — the work is undone and there "
              f"is now NOTHING that remembers it: {code} {out!r} {err!r}")

        # …and it is REACHABLE work: a second PR can be opened on it, and THAT one deletes it when it lands.
        code, _, err = run(["--file", str(path), "open-pr", "--id", fid, "--pr", "#10"])
        check(code == 0, f"[{lineage}] a reopened follow-up cannot be worked again: {err!r}")
        code, _, err = run(["--file", str(path), "merged", "--id", fid])
        check(code == 0, f"[{lineage}] `merged` exited {code}: {err!r}")
        check(load(path) == [], f"[{lineage}] the second PR merged and the entry was kept anyway")


def t_a_rejection_is_never_deleted(tmp: Path) -> None:
    """A REJECTED FOLLOW-UP STAYS IN THE STORE — hidden from the default view, never deleted.

    This is the other half of the lifetime principle, and the half that is NOT about durability: KEEP WHAT
    PREVENTS REPEATED WORK. Delete a rejection and the next run rediscovers the same thing, records it
    again, and asks the user the same question — and the run after that does it again.

    The asymmetry with a PUBLISHED entry is not an exception to the rule, it IS the rule, applied. Ask the
    same question of both: is there a record ELSEWHERE? A published follow-up has one — the issue, on
    GitHub, which is what stops the re-raise. A rejection has NONE: nothing was filed and nothing merged,
    and this local store is the only place the user's `no` exists at all.
    """
    check(TRANSITIONS["reject"][1] != DELETED,
          "`reject` DELETES the follow-up — the next run rediscovers it and asks the user all over again")
    check(not any("rejected" in TRANSITIONS[c][0] for c in DELETING),
          f"a deleting edge ({', '.join(DELETING)}) leaves from `rejected` — the user's `no` can be erased, "
          f"and then re-asked")

    path = tmp / "f.jsonl"
    a, b = seed(path, 2)
    run(["--file", str(path), "reject", "--id", a, "--at", "2026-07-14T09:00:00Z"])
    check(state_of(path, a) == "rejected", "reject did not reach `rejected`")
    code, out, err = run(["--file", str(path), "list"])
    check((code, sorted(out.split())) == (0, sorted([a, b])),
          f"a REJECTED follow-up was dropped from the store — the next run will raise it again: {out!r}")
    entry = json.loads(run(["--file", str(path), "get", "--id", a])[1])
    check(entry["decided"] == "2026-07-14T09:00:00Z", f"the user's `no` lost its stamp: {entry!r}")

    # …HIDDEN, not gone: the default view leaves it out (nobody has anything left to do about it), `--all`
    # shows it, and it is still there after the store is rewritten by the next write.
    code, shown, err = run(["--file", str(path), "table", "--fields", "id,state"])
    check(code == 0, f"table exited {code}: {err!r}")
    _, _, cells = ledger.grid(shown, ("id", "state"), ("store", "rule"), TABLE_MARKERS)
    check([c[0] for c in cells] == [b], f"the default view did not hide the rejected entry: {cells!r}")
    code, everything, _ = run(["--file", str(path), "table", "--all", "--fields", "id,state"])
    _, _, cells = ledger.grid(everything, ("id", "state"), ("store", "rule"), TABLE_MARKERS)
    check([c[0] for c in cells] == [a, b], f"`--all` does not reveal the rejected entry: {cells!r}")

    seed(path)  # any write rewrites the WHOLE store — the rejection must come through it
    check(state_of(path, a) == "rejected",
          "the rejection did not survive the next write of the store")


def t_act_edge_needs_every_condition(tmp: Path) -> None:
    """THE AUTONOMOUS EDGE IS EVIDENCE-BEARING, OR IT IS A BYPASS WITH A NICER NAME.

    `take-up` is the one step where the driver commits the repo to work with NO user ruling. What makes
    that safe is not the driver's good intentions — it is that EVERY ACT condition must be witnessed IN THE
    ENTRY, and the accessor refuses the step otherwise, exactly as `add` refuses a blank `evidence`.

    Condition 1 (CORROBORATED) is enforced by the GRAPH, not by a flag: `take-up` leaves only from
    `corroborated`, so a claim nobody investigated cannot be taken up at all. The rest are flags, and each
    is REQUIRED and must be non-blank. Every check below is derived from ACT_CONDITIONS — a fifth condition
    is pinned here the day it is added.
    """
    # Condition 1: the graph. An UNINVESTIGATED claim cannot be taken up — and neither can a REFUTED one.
    check(TRANSITIONS[ACT_CMD][0] == ("corroborated",),
          f"`{ACT_CMD}` leaves from {TRANSITIONS[ACT_CMD][0]!r} — it must leave ONLY from `corroborated`, "
          f"which is what makes CORROBORATION structural rather than a claim the driver types in")
    for setup in ([], ["refute"]):
        path = tmp / ("cond1-" + "-".join(setup or ["raw"]) + ".jsonl")
        (fid,) = seed(path)
        for cmd in setup:
            run(["--file", str(path), cmd, "--id", fid, *transition_args(cmd)])
        was = state_of(path, fid)
        code, _, err = run(["--file", str(path), ACT_CMD, "--id", fid, *transition_args(ACT_CMD)])
        check(code == 1, f"`{ACT_CMD}` was ACCEPTED on a {was!r} follow-up (exit {code}) — the driver took "
                         f"up an UNCORROBORATED claim")
        check(state_of(path, fid) == was, f"a refused `{ACT_CMD}` moved {was!r} anyway")

    # Conditions 2..N: each is a REQUIRED flag, and each refuses a blank.
    for witness in ACT_FLAGS:
        path = tmp / f"cond-{witness}.jsonl"
        (fid,) = seed(path)
        run(["--file", str(path), "corroborate", "--id", fid, "--finding", "reproduced"])

        argv = ["--file", str(path), ACT_CMD, "--id", fid]
        omitted = [a for f in ACT_FLAGS if f != witness for a in (FLAG[f], "x")]
        code, _, err = run([*argv, *omitted])
        check(code == 2, f"`{ACT_CMD}` without {FLAG[witness]} was ACCEPTED (exit {code}): {err!r}")
        for blank in BLANKS:
            values = [a for f in ACT_FLAGS for a in (FLAG[f], blank if f == witness else "x")]
            code, _, err = run([*argv, *values])
            check(code == 1,
                  f"`{ACT_CMD}` with a BLANK {FLAG[witness]} ({blank!r}) was ACCEPTED (exit {code}) — a "
                  f"condition asserted with no evidence is not a condition")
            check("bypass" in err, f"`{ACT_CMD}` failed for the wrong reason: {err!r}")
        check(state_of(path, fid) == "corroborated", f"a refused `{ACT_CMD}` moved the state anyway")

    # …and with every condition evidenced, it goes through — and the evidence is IN the entry.
    path = tmp / "ok.jsonl"
    (fid,) = seed(path)
    run(["--file", str(path), "corroborate", "--id", fid, "--finding", "reproduced at followups.py:1"])
    code, _, err = run(["--file", str(path), ACT_CMD, "--id", fid, *transition_args(ACT_CMD)])
    check(code == 0, f"`{ACT_CMD}` exited {code} with every condition evidenced: {err!r}")
    entry = json.loads(run(["--file", str(path), "get", "--id", fid])[1])
    check(entry["state"] == TRANSITIONS[ACT_CMD][1], f"`{ACT_CMD}` did not reach its state: {entry!r}")
    for witness in ACT_WITNESSES:
        check(not is_blank(entry[witness]),
              f"the entry was taken up with NO evidence for the condition witnessed by {witness!r}: "
              f"{entry!r}")


def t_self_accepted_is_never_mistaken_for_accepted(tmp: Path) -> None:
    """A follow-up the USER agreed to and one the DRIVER took up are DIFFERENT THINGS — forever, and at a
    glance.

    They are modelled as different STATES rather than one state with a flag, for two reasons that a field
    could not buy:

      * the STATE is what the default table shows, so the difference is visible without asking for it;
      * and the graph can then make `publish` reachable only from the USER's `accepted` — publication is
        a claim in the user's name, so no autonomous path may reach it. A `who_decided` field could not
        enforce that; an edge can, and `t_user_ruling_is_unskippable` proves it does.

    And the distinction SURVIVES the work starting: a self-accepted lineage carries the ACT witnesses and NO
    `decided` stamp; a user-accepted one carries `decided` and no ACT witnesses. Even in `in-pr` — the one
    state both lineages reach — the entry still says which happened.
    """
    self_state = TRANSITIONS[ACT_CMD][1]
    check(self_state != "accepted", "the driver's own acceptance IS `accepted` — it is indistinguishable")
    check(self_state in STATES and "accepted" in STATES, "both acceptances must be real states")

    driven = tmp / "driver.jsonl"
    (a,) = seed(driven)
    run(["--file", str(driven), "corroborate", "--id", a, "--finding", "reproduced"])
    run(["--file", str(driven), ACT_CMD, "--id", a, *transition_args(ACT_CMD)])
    ruled = tmp / "user.jsonl"
    (b,) = seed(ruled)
    run(["--file", str(ruled), "accept", "--id", b, "--at", "2026-07-14T09:00:00Z"])

    for path, fid in ((driven, a), (ruled, b)):
        code, out, err = run(["--file", str(path), "table", "--fields", "id,state"])
        check(code == 0, f"table exited {code}: {err!r}")
        _, _, cells = ledger.grid(out, ("id", "state"), ("store", "rule"), TABLE_MARKERS)
        check(cells and cells[0][1] == state_of(path, fid),
              f"the default view does not show WHO accepted it: {cells!r}\n{out}")
    check(state_of(driven, a) != state_of(ruled, b),
          "a self-accepted and a user-accepted follow-up show the SAME state in the table")

    # …and once both are IN A PR — the one state both lineages reach — the entries still say which is which.
    shared = TRANSITIONS["open-pr"][1]
    run(["--file", str(driven), "open-pr", "--id", a, "--pr", "#1"])
    run(["--file", str(ruled), "open-pr", "--id", b, "--pr", "#2"])
    d = json.loads(run(["--file", str(driven), "get", "--id", a])[1])
    u = json.loads(run(["--file", str(ruled), "get", "--id", b])[1])
    check(state_of(driven, a) == state_of(ruled, b) == shared, "both lineages must be able to open a PR")
    check(is_blank(d["decided"]),
          f"a DRIVER-accepted follow-up carries a `decided` stamp ({d['decided']!r}) — it reads as the "
          f"user's ruling, and `load()` would accept it as one")
    check(not is_blank(u["decided"]), f"a USER-accepted follow-up lost its ruling: {u!r}")
    for witness in ACT_FLAGS:
        check(not is_blank(d[witness]), f"the ACT grounds vanished when the work started: {d!r}")
        check(is_blank(u[witness]),
              f"a USER-accepted follow-up carries ACT grounds ({witness}) it never needed: {u!r}")


def t_load_rejects_an_illegal_history(tmp: Path) -> None:
    """AN ENTRY NO LEGAL HISTORY COULD PRODUCE DOES NOT LOAD — the guard holds against a HAND-WRITTEN store.

    The transitions only ever see entries this accessor wrote. A driver that means to skip the user does not
    call `accept` and lie; it writes `"state": "accepted"` into the JSONL and calls `publish` — and every
    from-state check in the file waves it through, because by then the entry IS accepted. THAT DRIVER IS THE
    ONE THIS STORE DEFENDS AGAINST. So the invariant is enforced where the DATA enters.

    Derived from `WITNESS`: for every state, blank each field a legal path to it must have written, and the
    store must REFUSE to load. A new state, a new edge, or a new witness is covered with no fixture edit —
    which is the only way a rule of this kind stays true.
    """
    for state in STATES:
        for alt in WITNESS[state]:
            for missing in sorted(alt):
                # Build the entry from THE ALTERNATIVE UNDER TEST, minus one of its witnesses — not from
                # the store's minimal one. `in-pr` is reachable two ways (the user's `decided` stamp, or the
                # full ACT witness set), and an entry that satisfies EITHER is legal: strip a field from the
                # one it did not come by and nothing is wrong with it. What must be refused is an entry that
                # satisfies NO alternative, and only this construction produces one. (It cannot accidentally
                # satisfy another: the alternatives are minimal, so none is a subset of another.)
                carried = {f: f"<{f}>" for f in alt if f != missing}
                path = write_lines(tmp / f"{state}-{'+'.join(sorted(alt))}-no-{missing}.jsonl",
                                   json.dumps({"type": "followup", **DEFAULTS, "id": "fu1",
                                               "state": state, **carried}))
                code, out, err = run(["--file", str(path), "list"])
                check(code == 1,
                      f"an entry in state {state!r} carrying {sorted(carried)!r} but NO {missing!r} LOADED "
                      f"(exit {code}) — no legal sequence of transitions produces that entry:\n{out}")
                check("no legal history" in err, f"[{state}/{missing}] failed for the wrong reason: {err!r}")
        # …and the LEGAL entry in that state loads.
        path = write_lines(tmp / f"{state}-ok.jsonl", entry_line(id="fu1", state=state))
        code, out, err = run(["--file", str(path), "list"])
        check((code, out) == (0, "fu1\n"), f"a LEGAL {state!r} entry did not load: {code} {err!r}")

    # THE REVIEWER'S EXACT CASE, end to end: hand-write `accepted` with no user ruling, then publish.
    path = write_lines(tmp / "forged.jsonl",
                       entry_line(id="fu1", state="accepted", decided=PLACEHOLDER))
    code, out, err = run(["--file", str(path), "publish", "--id", "fu1", "--ref", "#666"])
    check(code == 1,
          f"a HAND-WRITTEN `accepted` entry with NO user ruling was PUBLISHED (exit {code}) — the cut "
          f"vertex is enforced only against a driver that cooperates with it:\n{out}")
    check("decided" in err, f"the refusal does not name what is missing: {err!r}")
    # (the REF, not the word "publish" — every entry carries a `published` FIELD, so that would match on
    # a store nothing had touched.)
    check("#666" not in path.read_text(), "the store was written despite the refusal")

    # …and the same for the ACT edge: a hand-written self-acceptance missing a condition's evidence.
    for witness in ACT_WITNESSES:
        path = write_lines(tmp / f"forged-act-{witness}.jsonl",
                           entry_line(id="fu1", state=TRANSITIONS[ACT_CMD][1], **{witness: PLACEHOLDER}))
        code, out, err = run(["--file", str(path), "open-pr", "--id", "fu1", "--pr", "#1"])
        check(code == 1,
              f"a HAND-WRITTEN self-acceptance with no evidence for {witness!r} was TAKEN INTO A PR (exit "
              f"{code}) — the ACT conditions are enforced only on the way IN:\n{out}")


def t_the_doc_and_the_code_agree(tmp: Path) -> None:
    """THE THRESHOLD THE DRIVER READS AND THE THRESHOLD THE CODE ENFORCES ARE THE SAME FOUR CONDITIONS.

    The ACT conditions necessarily exist twice: as prose in `references/followups.md`, which is where the
    driver reads the RULE, and as `ACT_CONDITIONS` here, which is what REFUSES the step. Two copies of one
    definition is the shape this repo has been bitten by over and over — a summary that has drifted from
    its definition is worse than no summary, because it is the version people actually read.

    So the two are not merely both maintained; their AGREEMENT is executed. Add a fifth condition to the
    code and the doc goes stale — red. Delete one from the code and the doc now documents a condition
    nothing enforces — red. This is the check `fu5` in the live store asks for, applied to the one
    definition in this file that could not be given a single owner.
    """
    doc = Path(__file__).resolve().parent.parent / "references" / "followups.md"
    check(doc.exists(), f"the threshold's prose is missing entirely: {doc}")
    text = doc.read_text()

    documented = set(re.findall(r"--act-[a-z-]+", text))
    enforced = {FLAG[w] for w in ACT_FLAGS}
    check(documented == enforced,
          f"the doc and the code disagree about the ACT conditions.\n"
          f"  documented but NOT enforced: {sorted(documented - enforced)}\n"
          f"  enforced but NOT documented: {sorted(enforced - documented)}\n"
          f"{doc}")
    for label, _, _ in ACT_CONDITIONS:
        check(label in text,
              f"ACT condition '{label}' is enforced by this file and appears NOWHERE in {doc.name} — the "
              f"driver is refused a step it was never told the rule for")
    check(ACT_CMD in text, f"`{ACT_CMD}` — the autonomous edge itself — is not documented in {doc.name}")

    # …and EVERY step is named there, not just the ACT one. Derived from TRANSITIONS: an edge added to the
    # graph — a deleting one above all — that the driver is never told about is an edge it never takes, and
    # the entry it should have ended sits in the queue forever.
    for cmd in TRANSITIONS:
        check(cmd in text,
              f"`{cmd}` is a step this file enforces and {doc.name} never names — the driver cannot take a "
              f"step it has not been told exists")


def t_ids_are_assigned_and_never_reused(tmp: Path) -> None:
    """`id` is assigned by the STORE (`fu<N>`, one past the highest EVER HANDED OUT) — never by the caller,
    and NEVER REUSED, not even after the entry that held it was DELETED.

    A reused id silently re-points every reference to the old entry — an audit file, a MERGED PR's body,
    the user's own note — at a DIFFERENT follow-up. And deletion is precisely what makes that reachable:
    the surviving entries do not remember an id that is gone, so `merged` on the HIGHEST one would hand
    that id straight back out. The high-water mark is what stops it (`read_store`), and it is ONE line, so
    the deleted entry is really gone and its id is still spent forever.
    """
    path = tmp / "f.jsonl"
    check(seed(path, 3) == ["fu1", "fu2", "fu3"], "ids are not sequential fu<N>")

    write_lines(path, entry_line(id="fu1"), entry_line(id="fu3"))  # fu2 deleted by hand
    check(seed(path)[0] == "fu4", "an id was REUSED after an entry was removed")

    # …and the id the STORE ITSELF deletes — the HIGHEST one, through the ordinary CLI — is spent too.
    drive_to(path, "fu4", "in-pr")
    code, _, err = run(["--file", str(path), "merged", "--id", "fu4"])
    check(code == 0, f"merged exited {code}: {err!r}")
    check(find(load(path), "fu4") is None, "the merged follow-up is still in the store")
    check(seed(path)[0] == "fu5",
          "the id of a DELETED follow-up was handed out again — every reference to it (the merged PR that "
          "closed it, the user's note) now points at a DIFFERENT follow-up")

    # …the caller cannot set one (there is no flag), and a malformed/duplicate id on disk is REJECTED.
    code, _, _ = run(["--file", str(path), "add", "--title", "t", "--evidence", "e",
                      "--deferred-why", "w", "--id", "fu99"])
    check(code == 2, "add --id was accepted — the caller must not choose the id")
    for name, bad in (("malformed", entry_line(id="pwned")), ("zero", entry_line(id="fu0")),
                      ("empty", entry_line(id=""))):
        p = write_lines(tmp / f"{name}.jsonl", bad)
        code, _, err = run(["--file", str(p), "list"])
        check(code == 1, f"a {name} id was ACCEPTED (exit {code})")
        check("malformed id" in err, f"failed for the wrong reason: {err!r}")
    p = write_lines(tmp / "dup.jsonl", entry_line(id="fu1"), entry_line(id="fu1"))
    code, _, err = run(["--file", str(p), "list"])
    check(code == 1, "a DUPLICATE entry was accepted — `find` would read the first and `set` update it "
                     "while the second stayed behind, disagreeing, forever")
    check("duplicate entry" in err, f"failed for the wrong reason: {err!r}")


def t_store_is_validated(tmp: Path) -> None:
    """A corrupt store is REJECTED, never silently repaired — and an unknown record type is never SKIPPED.

    A skipped line is a follow-up nothing reads, in the one store that has no other copy to heal from.
    An unrecognised `state` is rejected for the same reason it must not be settable: it would sit in the
    table as something no transition can move, and no reader could tell what it means. That is also what
    refuses a TOMBSTONE — `DELETED` is a sentinel, NOT a state, so an entry carrying it is one that was
    supposed to be GONE, and a store holding one is a store something wrote wrong.

    The id high-water mark is data, so it is checked where data enters, on exactly those terms: a SECOND
    mark, or one that is not a number, is a store this accessor did not write.
    """
    mark = json.dumps({"type": SEQ_TYPE, "high": 3})
    for name, lines, needle in (
        ("bad-json", ("{not json",), "malformed JSON"),
        ("not-object", ('["followup"]',), "not a JSON object"),
        ("unknown-type", (json.dumps({"type": "note", "id": "fu1"}),), "unknown record type"),
        ("unknown-state", (entry_line(id="fu1", state="approved"),), "unknown state"),
        ("deleted-tombstone", (entry_line(id="fu1", state=DELETED),), "unknown state"),
        ("two-marks", (mark, mark, entry_line(id="fu1")), "ONE high-water mark"),
        ("mark-not-a-number", (json.dumps({"type": SEQ_TYPE, "high": "lots"}),), "non-numeric"),
        ("mark-negative", (json.dumps({"type": SEQ_TYPE, "high": -1}),), "negative"),
    ):
        p = write_lines(tmp / f"{name}.jsonl", *lines)
        code, _, err = run(["--file", str(p), "list"])
        check(code == 1, f"[{name}] a corrupt store was ACCEPTED (exit {code})")
        check(needle in err, f"[{name}] failed for the wrong reason: {err!r}")

    # A MISSING file, though, is an empty store — the first follow-up must not need a bootstrap step.
    code, out, err = run(["--file", str(tmp / "nope.jsonl"), "list"])
    check((code, out) == (0, ""), f"a missing store is not an empty one: {code} {out!r} {err!r}")


def t_defaults_backfill(tmp: Path) -> None:
    """An entry written BEFORE a field existed still reads back complete — every absent field defaults.

    This is what lets a field be added to the schema without migrating a store that CANNOT BE REBUILT, and
    it is why the investigation/ACT fields could be added to a live store at all: an entry raised before
    they existed is a `candidate`, a `candidate` is required to witness nothing, and so it stays legal.
    That is not luck — it is the same rule that makes an unknown state default to the one that still needs
    the user.
    """
    p = write_lines(tmp / "old.jsonl", json.dumps({"type": "followup", "id": "fu1", "title": "old"}))
    code, out, err = run(["--file", str(p), "get", "--id", "fu1"])
    check(code == 0, f"get on a pre-schema entry exited {code}: {err!r}")
    entry = json.loads(out)
    check(set(entry) == set(FIELDS), f"get did not project onto FIELDS: {sorted(entry)}")
    check(entry["state"] == DEFAULTS["state"] == "candidate",
          f"a state-less entry did not default to a CANDIDATE (it defaulted to {entry['state']!r}) — an "
          f"entry whose state is unknown must be the one that needs the user, never one that skipped them")
    check(entry["title"] == "old", "the field the entry DID carry was overwritten by its default")
    check(WITNESS["candidate"] == (frozenset(),),
          f"a `candidate` is required to witness {WITNESS['candidate']!r} — every entry raised before "
          f"those fields existed would stop loading, and this store cannot be rebuilt")
    for f in ("finding", *ACT_FLAGS):
        check(entry[f] == PLACEHOLDER, f"a pre-schema entry's {f!r} did not default to a placeholder")


def t_values_are_strings(tmp: Path) -> None:
    """Every ingested value is coerced to `str`, so an on-disk JSON number cannot change a comparison."""
    p = write_lines(tmp / "n.jsonl", json.dumps({"type": "followup", "id": "fu1", "found_run": 260714}))
    code, out, err = run(["--file", str(p), "get", "--id", "fu1"])
    check(code == 0, f"get exited {code}: {err!r}")
    check(all(isinstance(v, str) for v in json.loads(out).values()), f"a non-string survived load(): {out!r}")
    code, out, _ = run(["--file", str(p), "list", "--where", "found_run=260714"])
    check(out == "fu1\n", f"--where could not match a value that was a JSON number on disk: {out!r}")


def t_concurrent_writers_lose_nothing(tmp: Path) -> None:
    """CONCURRENT RUNS MUST NOT LOSE AN ENTRY — the store is locked, and this proves it.

    Every concurrent campaign run writes THIS file, and a read-modify-write race silently drops entries:
    two drivers that both read a 7-entry store and both write an 8-entry one leave 8, not 9. Nothing
    errors, nothing reconciles, and no other copy of the lost follow-up exists anywhere — which is the
    precise failure this whole store was built to prevent, reintroduced one layer down.

    Real processes, real contention: N writers × M adds, all racing on one file. The oracle is exact — the
    store must hold N×M entries with N×M DISTINCT ids. Drop the `flock` in `locked()` and this goes red.
    """
    path = tmp / "race.jsonl"
    writers, adds = 8, 4
    script = (
        "import sys; sys.path.insert(0, %r); import followups as f;"
        "[f.main(['--file', %r, 'add', '--title', 't', '--evidence', 'e', '--deferred-why', 'w'])"
        " for _ in range(%d)]" % (str(Path(__file__).resolve().parent), str(path), adds)
    )
    import subprocess
    procs = [subprocess.Popen([sys.executable, "-c", script],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for _ in range(writers)]
    for p in procs:
        _, err = p.communicate(timeout=120)
        check(p.returncode == 0, f"a concurrent writer failed ({p.returncode}): {err.decode()!r}")

    entries = load(path)  # load() itself rejects a duplicate id, so a collision cannot even be read back
    ids = [e["id"] for e in entries]
    check(len(entries) == writers * adds,
          f"{writers} writers × {adds} adds left {len(entries)} entries, not {writers * adds} — "
          f"{writers * adds - len(entries)} follow-up(s) were LOST to a read-modify-write race")
    check(len(set(ids)) == len(ids), f"an id was handed out twice under concurrency: {ids!r}")


def t_table_hides_closed(tmp: Path) -> None:
    """The default view hides ONLY the CLOSED entries; everything still owed to someone stays visible.

    A `candidate` is the whole point of the store — it is waiting on the USER — so hiding one would bury
    the exact thing the view exists to surface. `--all` shows every entry.
    """
    path = tmp / "f.jsonl"
    write_lines(path, *(entry_line(id=f"fu{i + 1}", state=s) for i, s in enumerate(STATES)))
    code, out, err = run(["--file", str(path), "table", "--fields", "id,state"])
    check(code == 0, f"table exited {code}: {err!r}")
    _, _, cells = ledger.grid(out, ("id", "state"), ("store", "rule"), TABLE_MARKERS)
    shown = [c[1] for c in cells]
    check(shown == [s for s in STATES if s not in TABLE_HIDDEN_STATES],
          f"the default view hid something other than the closed states — it shows {shown!r}\n{out}")
    check("candidate" in shown, "a CANDIDATE was hidden — the entry that is waiting on the user is invisible")
    # NOTHING SOMEBODY CAN STILL ACT ON IS EVER HIDDEN. A hidden state must be one the graph cannot leave —
    # otherwise the view buries an entry that still has a move left in it, which is how a driver would make
    # its own refutation, or its own self-acceptance, quietly disappear. Derived: hidden ⊆ TERMINAL.
    check(set(TABLE_HIDDEN_STATES) <= set(TERMINAL),
          f"{sorted(set(TABLE_HIDDEN_STATES) - set(TERMINAL))!r} is HIDDEN by default and yet a transition "
          f"still applies to it — the view hides an entry somebody can still act on")

    code, out, err = run(["--file", str(path), "table", "--all", "--fields", "id,state"])
    check(code == 0, f"table --all exited {code}: {err!r}")
    _, _, cells = ledger.grid(out, ("id", "state"), ("store", "rule"), TABLE_MARKERS)
    check([c[1] for c in cells] == list(STATES), f"--all did not show every entry: {cells!r}\n{out}")
    check(ledger.notices(out) == [], f"--all hid nothing, so it must claim nothing was hidden\n{out}")


def t_table_omission_is_never_silent(tmp: Path) -> None:
    """THE OMISSION IS STATED, AND THE COUNT IS CORRECT — and an all-hidden store never reads as an empty one.

    A filtered view that does not say what it hid is a lie by omission. And the two empty grids are
    OPPOSITE facts: a store that never held a follow-up, versus one whose every follow-up is resolved.
    Printing the same marker for both tells a reader "nothing was ever found" at the moment everything was
    settled.
    """
    for closed in range(0, 4):
        for live in (0, 2):
            path = write_lines(
                tmp / f"n{closed}-{live}.jsonl",
                *(entry_line(id=f"fu{i}", state=TABLE_HIDDEN_STATES[0]) for i in range(1, closed + 1)),
                *(entry_line(id=f"fu{100 + i}") for i in range(live)),
            )
            code, out, err = run(["--file", str(path), "table"])
            check(code == 0, f"table exited {code}: {err!r}")
            _, _, cells = ledger.grid(out, TABLE_DEFAULT_FIELDS, ("store", "rule"), TABLE_MARKERS)
            check(len(cells) == live, f"[{closed}/{live}] {len(cells)} rows shown, not {live}\n{out}")
            said = [n for n in ledger.notices(out) if n not in TABLE_MARKERS]
            if not closed:
                check(said == [], f"[{closed}/{live}] nothing was hidden, yet the table says {said!r}")
                continue
            check(said == [hidden_notice(closed, TABLE_HIDDEN_STATES)],
                  f"[{closed}/{live}] the table hid {closed} and reported {said!r}\n{out}")
            # …and the count is what `--all` reveals — derived from the OUTPUT, not the fixture's arithmetic
            _, allout, _ = run(["--file", str(path), "table", "--all"])
            _, _, allcells = ledger.grid(allout, TABLE_DEFAULT_FIELDS, ("store", "rule"), TABLE_MARKERS)
            check(len(allcells) - len(cells) == closed,
                  f"[{closed}/{live}] the notice claims {closed} hidden, --all reveals "
                  f"{len(allcells) - len(cells)} more")

    empty = write_lines(tmp / "empty.jsonl")
    closed_only = write_lines(tmp / "closed.jsonl", entry_line(id="fu1", state="rejected"),
                              entry_line(id="fu2", state="rejected"))
    code, blank, _ = run(["--file", str(empty), "table"])
    check(ledger.notices(blank) == [TABLE_EMPTY_MARKER],
          f"an empty store must say exactly {TABLE_EMPTY_MARKER!r}: {ledger.notices(blank)!r}")
    code, out, _ = run(["--file", str(closed_only), "table"])
    check(ledger.notices(out) == [TABLE_ALL_HIDDEN_MARKER, hidden_notice(2, TABLE_HIDDEN_STATES)],
          f"an all-closed store must say it is NOT empty, and how many it hid: {ledger.notices(out)!r}")
    check(out != blank,
          f"an ALL-CLOSED store renders EXACTLY what an EMPTY one renders — 'every follow-up resolved' "
          f"and 'no follow-up ever found' are indistinguishable:\n{out}")
    check(TABLE_EMPTY_MARKER not in out.split("\n"), f"an all-closed store printed the EMPTY marker:\n{out}")


def t_table_grid_integrity(tmp: Path) -> None:
    """NO VALUE CAN FORGE THE LAYOUT — every hostile value, in every column, parsed back mechanically.

    A follow-up's `title` and `evidence` are free text written from a REVIEWER'S OUTPUT — the most
    attacker-shaped input in the whole campaign. Rendered raw, one carrying a `|` fabricates a column, a
    newline fabricates an entry, a leading `#` fabricates the rule line or the omission notice. This store
    prints through the ledger's `escape_cell()`/`grid_lines()`, and it is checked by the LEDGER'S OWN
    ORACLE — the same parser, not a friendlier copy of it.
    """
    for name, hostile in ledger.HOSTILE.items():
        path = write_lines(tmp / f"g-{name}.jsonl",
                           entry_line(id="fu1", title=hostile, evidence=hostile, published=hostile),
                           entry_line(id="fu2", title="benign"))
        for fields in (("id", "title", "state"), ("title",), ("published", "id")):
            code, out, err = run(["--file", str(path), "table", "--fields", ",".join(fields)])
            check(code == 0, f"[{name}] table exited {code}: {err!r}")
            _, _, cells = ledger.grid(out, fields, ("store", "rule"), TABLE_MARKERS)
            check(len(cells) == 2, f"[{name}] the value forged an ENTRY: {len(cells)} rows, not 2\n{out}")
            check(cells[0] == [escape_cell({"id": "fu1", "title": hostile, "state": "candidate",
                                            "evidence": hostile, "published": hostile}[f]) for f in fields],
                  f"[{name}] the printed row is not the escaped row: {cells[0]!r}\n{out}")
            check(ledger.notices(out) == [],
                  f"[{name}] a VISIBLE row forged an out-of-band line: {ledger.notices(out)!r}\n{out}")


def t_fields_and_lookup(tmp: Path) -> None:
    """Read BY FIELD NAME: `get --field`, `list --where`. An unknown or EMPTY field is REJECTED.

    `args.fields is not None` is the rule: falsiness would read `--fields ''` as "give me the defaults"
    and print a table nobody asked for.
    """
    path = tmp / "f.jsonl"
    (fid,) = seed(path)
    for argv, needle in (
        (["table", "--fields", ""], "unknown field ''"),
        (["table", "--fields", "nope"], "unknown field 'nope'"),
        (["get", "--id", fid, "--field", "nope"], "unknown field 'nope'"),
        (["list", "--where", "nope=1"], "unknown field 'nope'"),
        (["list", "--where", "bare"], "--where must be"),
        (["get", "--id", "fu99"], "no follow-up fu99"),
    ):
        code, out, err = run(["--file", str(path), *argv])
        check(code == 1, f"{argv!r} exited {code}, not 1 — it was ACCEPTED:\n{out}")
        check(needle in err, f"{argv!r} failed with {err!r}, which does not mention {needle!r}")

    run(["--file", str(path), "accept", "--id", fid])
    code, out, _ = run(["--file", str(path), "list", "--where", "state=accepted"])
    check(out == f"{fid}\n", f"--where state=accepted returned {out!r}")
    code, out, _ = run(["--file", str(path), "list", "--where", "state=candidate"])
    check(out == "", f"--where matched a state the entry has left: {out!r}")


CASES = [
    ("user-step-unskippable", "no driver-only path reaches `accepted`, nor any state `publish` leaves from — proved on the graph", t_user_ruling_is_unskippable),
    ("illegal-history", "an entry no legal history produces does NOT LOAD — the guard holds against a hand-written store", t_load_rejects_an_illegal_history),
    ("delete-needs-a-record", "an entry is deleted only once a DURABLE RECORD exists elsewhere — never on take-up", t_deletion_needs_a_durable_record),
    ("closed-pr-reopens", "a PR closed WITHOUT merging returns the entry to open work — it never vanishes with it", t_a_closed_pr_returns_the_entry_to_open_work),
    ("rejection-kept", "a REJECTED follow-up is kept — deleting it is how the next run re-raises it", t_a_rejection_is_never_deleted),
    ("act-needs-conditions", "the autonomous ACT edge must EVIDENCE every condition, or it is refused", t_act_edge_needs_every_condition),
    ("self-accept-distinct", "a DRIVER-accepted follow-up is never mistaken for a USER-accepted one", t_self_accepted_is_never_mistaken_for_accepted),
    ("doc-and-code-agree", "the ACT conditions the driver READS are the ones the code ENFORCES", t_the_doc_and_the_code_agree),
    ("investigation-evidence", "an investigation shows its work; the finding APPENDS and never clobbers", t_investigation_shows_its_work),
    ("refutation-stays", "a refuted follow-up stays in the store, stays visible, and stays overturnable", t_refutation_stays_in_the_store),
    ("state-not-settable", "`set` writes neither `state` nor any evidence a transition left behind", t_state_and_evidence_are_not_settable),
    ("transition-graph", "every transition is checked against TRANSITIONS; an investigation outcome is never terminal", t_transition_graph),
    ("ruling-recorded", "the USER's ruling is stamped durably; NOTHING the driver does alone stamps it", t_ruling_is_recorded),
    ("publish-needs-ref", "a published follow-up must name WHERE", t_publish_needs_a_ref),
    ("evidence-required", "a follow-up with no evidence is a RUMOR — `add` refuses it, for EVERY required field", t_evidence_is_required),
    ("required-not-editable-away", "a REQUIRED field cannot be BLANKED through `set` — the rule holds where an entry CHANGES, not only where it was made", t_required_cannot_be_edited_away),
    ("no-unreadable-store", "no write door can write a store `load()` refuses — every door shares ONE blank predicate", t_no_door_writes_a_store_that_will_not_load),
    ("ids-never-reused", "ids are assigned by the store, sequential, and NEVER reused", t_ids_are_assigned_and_never_reused),
    ("store-validated", "a corrupt store is rejected, never silently repaired; a missing one is empty", t_store_is_validated),
    ("defaults-backfill", "an entry written before a field existed reads back complete — as a CANDIDATE", t_defaults_backfill),
    ("values-are-strings", "every ingested value is coerced to str", t_values_are_strings),
    ("concurrent-writers", "concurrent runs lose NOTHING — the read-modify-write is locked", t_concurrent_writers_lose_nothing),
    ("table-hides-closed", "the default view hides only CLOSED entries; a candidate always shows", t_table_hides_closed),
    ("table-omission-loud", "the omission is never silent, and an all-closed store never reads as empty", t_table_omission_is_never_silent),
    ("table-grid-integrity", "no hostile title/evidence forges a column, an entry, or an out-of-band line", t_table_grid_integrity),
    ("fields-and-lookup", "read by FIELD NAME; an unknown or empty field is rejected", t_fields_and_lookup),
]


def self_test() -> int:
    """Run every fixture. Exit 0 iff every rule this file claims to enforce actually holds."""
    failures = 0
    with tempfile.TemporaryDirectory() as tmpdir:
        for name, rule, fn in CASES:
            work = Path(tmpdir) / name
            work.mkdir()
            try:
                fn(work)
            except SelfTestFailure as exc:
                print(f"FAIL     {name:24} -> {rule}\n         {exc}")
                failures += 1
            except SystemExit as exc:
                # A fixture called an accessor DIRECTLY (`load()`, not through `run()`) and it REFUSED —
                # `fail()` raises SystemExit, which is a BaseException and would otherwise escape every
                # handler here: the suite would die on the FIRST such fixture, printing no verdict, naming
                # no rule, and running none of the others. The refusal is usually the very thing under test
                # (a store that will not load), so it must be reported AS a failure of the fixture that
                # provoked it, not as the end of the run.
                print(f"FAIL     {name:24} -> {rule}\n         the accessor REFUSED the store (exit "
                      f"{exc.code}) inside the fixture — its message is on stderr, above")
                failures += 1
            except Exception as exc:  # noqa: BLE001 — a fixture that CRASHES has not passed
                print(f"FAIL     {name:24} -> {rule}\n         raised {type(exc).__name__}: {exc}")
                failures += 1
            else:
                print(f"ok       {name:24} -> {rule}")
    print()
    if failures:
        print(f"{failures} check(s) FAILED — the follow-up store's contract is broken.")
        return 1
    print(f"all {len(CASES)} fixtures hold — the follow-up store's contract is intact.")
    return 0


# --- cli ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    # NOT `required=True`: `self-test` reads no store at all. Every OTHER subcommand does, and main()
    # enforces that through `parser.error` — the same message, usage line and exit 2 argparse would give.
    parser.add_argument("--file", help="path to the store (.gauntlet/followups.jsonl)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="raise a new follow-up CANDIDATE (the only way in)")
    for f in REQUIRED:
        a.add_argument(f"--{f.replace('_', '-')}", dest=f, required=True,
                       help=f"'{f}' (required — a follow-up without it is a rumor)")
    a.add_argument("--run", help="the run-id that found it")
    a.add_argument("--found", help="ISO timestamp it was found (default: now)")

    s = sub.add_parser("set", help=f"edit an existing follow-up's prose ({', '.join(EDITABLE)})")
    s.add_argument("--id", required=True)
    for f in EDITABLE:  # `state` is NOT here, and that is the point: see EDITABLE.
        s.add_argument(f"--{f.replace('_', '-')}", dest=f, help=f"field '{f}'")

    # The transitions — the ONLY things that move `state`. Each validates the state it comes FROM, so the
    # user's ruling cannot be routed around. Their FLAGS are derived from `WRITES`: every evidence field a
    # transition must leave behind is a REQUIRED flag, so an edge cannot be added that writes a witness the
    # CLI never asks for (and that `load()` would then reject as an illegal history).
    for cmd, (frm, to) in TRANSITIONS.items():
        t = sub.add_parser(cmd, help=f"{role(cmd)}: {'/'.join(frm)} -> {to}")
        t.add_argument("--id", required=True)
        t.add_argument("--at", help="ISO timestamp of this step (default: now)")
        for field in WRITES[cmd]:
            if field in OPTIONAL:
                continue  # a TIMESTAMP may default; evidence never may
            t.add_argument(FLAG[field], dest=field, required=True, help=FLAG_HELP[field])

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
