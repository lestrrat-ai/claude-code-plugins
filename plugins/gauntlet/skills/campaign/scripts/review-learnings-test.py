#!/usr/bin/env python3
"""THE DURABLE REVIEW-LEARNINGS STORE'S CONTRACT, EXECUTED — the fixtures for `review-learnings.py`.

RUN IT THROUGH THE SCRIPT IT TESTS: `python3 review-learnings.py self-test`. That is what CI invokes, and
it owns the exit code. This module is loaded from there, BY PATH.

EVERY FIXTURE MUST PIN A RULE — it must go red if its rule is deleted or weakened. The rendering fixtures
lean on the LEDGER's oracle (`ledger-test.py`'s `grid`/`notices`/`hostile`), because this store prints
through the same shared table renderer, so its output MUST be parsed back by the same parser — not a
friendlier copy.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ledger  # noqa: E402
from _gauntlet.modules import load_module_from_path  # noqa: E402
from _gauntlet.table import escape_cell, hidden_notice  # noqa: E402
from _gauntlet.testing import capture_cli  # noqa: E402


def _load(name: str, filename: str):
    """Load a sibling script whose hyphenated filename is not an importable module name."""
    module = load_module_from_path(name, HERE / filename)
    if module is None:
        raise RuntimeError(f"cannot load {HERE / filename}")
    return module


# The ledger's test ORACLE. Both stores render through the same table code, so this store's output is
# parsed back by the ledger's own parser — not a friendlier copy.
ledger_test = _load("ledger_test", "ledger-test.py")
SelfTestFailure = ledger_test.SelfTestFailure
check = ledger_test.check

# The accessor under test, and its schema constants bound as locals for the fixtures.
SCRIPT = HERE / "review-learnings.py"
A = _load("review_learnings_owner_for_test", "review-learnings.py")
DEFAULTS, EDITABLE, ENTRY_TYPE, FIELDS, INITIAL = A.DEFAULTS, A.EDITABLE, A.ENTRY_TYPE, A.FIELDS, A.INITIAL
INTAKE, PLACEHOLDER, REQUIRED, SEQ_TYPE, STATES = A.INTAKE, A.PLACEHOLDER, A.REQUIRED, A.SEQ_TYPE, A.STATES
TABLE_ALL_HIDDEN_MARKER, TABLE_EMPTY_MARKER = A.TABLE_ALL_HIDDEN_MARKER, A.TABLE_EMPTY_MARKER
TABLE_HIDDEN_STATES, TABLE_MARKERS, TERMINAL = A.TABLE_HIDDEN_STATES, A.TABLE_MARKERS, A.TERMINAL
TIERS, TRANSITIONS, USER_RULINGS, WRITES = A.TIERS, A.TRANSITIONS, A.USER_RULINGS, A.WRITES
find, is_blank, load = A.find, A.is_blank, A.load

# EVERY SPELLING OF "THIS VALUE CARRIES NOTHING" that `is_blank()` recognises — spelled once, here, so a
# fixture that loops over BLANKS picks up a spelling added tomorrow with no edit.
BLANKS = ("", "   ", "\t", PLACEHOLDER, f" {PLACEHOLDER} ")


def run(argv: "list[str]") -> "tuple[int, str, str]":
    """Drive the REAL CLI in-process and capture (exit code, stdout, stderr)."""
    return capture_cli(A.main, argv)


def entry_line(**over: object) -> str:
    """A raw store line for a COMPLETE entry — every field carrying something, `**over` on top."""
    rec = {"type": ENTRY_TYPE, **DEFAULTS,
           **{f: f"<{f}>" for f in FIELDS if f not in ("id", "state")}, **over}
    return json.dumps(rec)


def raw_line(field: str, raw: str, **over: object) -> str:
    """An entry line whose `field` carries RAW JSON TEXT (`null`, `123`) — shapes no write door produces."""
    rec = json.loads(entry_line(**over))
    rec[field] = "@@RAW@@"
    return json.dumps(rec).replace('"@@RAW@@"', raw)


def write_lines(path: Path, *lines: str) -> Path:
    """Write a store RAW — bypassing dump(), so a fixture can hold what dump() would never emit."""
    path.write_text("".join(line + "\n" for line in lines))
    return path


def seeded(field: str, i: int = 0) -> str:
    return f"{field}-{i}"


def record_argv(i: int = 0) -> "list[str]":
    """A LEGAL `record` — every REQUIRED field carrying something. Derived from `REQUIRED`, never retyped,
    so a field added to REQUIRED tomorrow is passed by every fixture that makes a learning, with no edit."""
    return ["record", *(a for f in REQUIRED for a in (INTAKE["record"][f], seeded(f, i)))]


# Per-path seed cursor: each `seed(path)` continues that store's content index, so repeated calls on ONE
# store make DISTINCT classes (the store keeps one live record per claim+anchor, so a re-seed of the same
# content is refused — that is the guard, not a fixture bug). The FIRST entry on a fresh store is still
# index 0, so fixtures that match on `seeded(...,0)` are unaffected.
_SEED_CURSOR: "dict[str, int]" = {}


def seed(path: Path, n: int = 1) -> "list[str]":
    ids = []
    start = _SEED_CURSOR.get(str(path), 0)
    for i in range(start, start + n):
        code, out, err = run(["--file", str(path), *record_argv(i)])
        check(code == 0, f"record exited {code}: {err!r}")
        ids.append(json.loads(out)["id"])
    _SEED_CURSOR[str(path)] = start + n
    return ids


def transition_args(cmd: str) -> "list[str]":
    """The flags a transition REQUIRES — derived from `WRITES`/`INTAKE`, never retyped. `promote` gets a
    VALID tier; an evidence edge gets some text."""
    argv: list[str] = []
    for field in WRITES[cmd]:
        flag = INTAKE[cmd][field]
        value = TIERS[0] if field == "promoted" else f"{cmd}:{field}"
        argv += [flag, value]
    return argv


def drive_to(path: Path, lid: str, target: str) -> None:
    """Move a recorded (`active`) learning to `target` along the GRAPH — shortest legal path, derived."""
    paths: "dict[str, list[str]]" = {INITIAL: []}
    queue = [INITIAL]
    while queue:
        state = queue.pop(0)
        for cmd, (frm, to) in TRANSITIONS.items():
            if state in frm and to not in paths:
                paths[to] = paths[state] + [cmd]
                queue.append(to)
    check(target in paths, f"no legal path reaches {target!r} — the graph cannot produce this fixture")
    for cmd in paths[target]:
        code, _, err = run(["--file", str(path), cmd, "--id", lid, *transition_args(cmd)])
        check(code == 0, f"driving to {target!r}: `{cmd}` exited {code}: {err!r}")


# --- fixtures -----------------------------------------------------------------

def t_record_is_the_only_way_in(tmp: Path) -> None:
    """`record` creates an ACTIVE learning, and refuses one missing or blank in ANY required field.

    A learning missing its claim/justification/anchor/falsifiability/provenance is a RUMOR a future reviewer
    cannot audit — the one thing this store must never accumulate. Both loops run over `REQUIRED`, so a
    field added there tomorrow is enforced here that day.
    """
    path = tmp / "r.jsonl"
    for missing in REQUIRED:
        argv = ["--file", str(path), "record"]
        for f in REQUIRED:
            if f != missing:
                argv += [INTAKE["record"][f], "x"]
        code, _, err = run(argv)
        check(code == 2, f"record without --{missing} was ACCEPTED (exit {code}): {err!r}")
    for blanked in REQUIRED:
        for blank in BLANKS:
            argv = ["--file", str(path), "record"]
            for f in REQUIRED:
                argv += [INTAKE["record"][f], blank if f == blanked else "x"]
            code, _, err = run(argv)
            check(code == 1, f"record with a BLANK --{blanked} ({blank!r}) was ACCEPTED (exit {code})")
            check("RUMOR" in err, f"record failed for the wrong reason: {err!r}")
    check(load(path) == [], "a REFUSED record still wrote an entry")
    code, out, err = run(["--file", str(path), *record_argv()])
    check(code == 0, f"a legal record was REFUSED (exit {code}): {err!r}")
    entry = json.loads(out)
    check(entry["state"] == INITIAL, f"a fresh learning is not '{INITIAL}': {entry['state']!r}")
    check(not is_blank(entry["recorded"]), "a fresh learning has no `recorded` stamp")
    # …and a non-ASCII value is a value like any other.
    code, out, _ = run(["--file", str(path), "record", "--claim", "日本語", "--justification", "réf",
                        "--anchor", "x", "--falsifiability", "y", "--provenance", "z"])
    check(code == 0 and json.loads(out)["claim"] == "日本語", f"a non-ASCII learning was mangled: {out!r}")


def t_store_never_deletes(tmp: Path) -> None:
    """THE STORE ACCUMULATES — no transition removes an entry, and a revoked one is still THERE.

    A learning is a standing fact, not work that completes, so it must never be discharged the way the
    follow-up QUEUE deletes a landed entry. Proved two ways: no edge targets removal (there is no state
    outside `STATES` that a transition lands on), and a fully-retired learning still loads.
    """
    for _cmd, (_frm, to) in TRANSITIONS.items():
        check(to in STATES, f"`{_cmd}` lands on {to!r}, which is not a state — this store must not delete")
    path = tmp / "keep.jsonl"
    (lid,) = seed(path)
    drive_to(path, lid, "revoked")
    entry = find(load(path), lid)
    check(entry is not None, "a REVOKED learning was removed — the store must keep it for audit")
    assert entry is not None
    check(entry["state"] == "revoked", f"the kept entry is not revoked: {entry['state']!r}")
    check(not is_blank(entry["claim"]), "the kept entry lost its claim")


def t_legacy_demote_entry_remains_readable(tmp: Path) -> None:
    """An entry produced by a legacy DEMOTE remains active and readable without a store migration."""
    path = write_lines(
        tmp / "legacy-demote.jsonl",
        entry_line(
            id="rl7",
            claim="legacy accepted residual",
            anchor="legacy/guard.py",
            provenance="repair-7-1.md DECISION: demote",
        ),
    )
    code, out, err = run(["--file", str(path), "get", "--id", "rl7"])
    check(code == 0, f"a legacy DEMOTE learning was unreadable (exit {code}): {err!r}")
    entry = json.loads(out)
    check(entry["state"] == "active", f"the legacy learning changed state: {entry['state']!r}")
    check(entry["provenance"] == "repair-7-1.md DECISION: demote",
          f"the legacy provenance changed: {entry['provenance']!r}")
    code, out, err = run([
        "--file", str(path), "table", "--fields", "id,state,claim,anchor,provenance",
    ])
    check(code == 0 and "DECISION: demote" in out,
          f"the legacy learning disappeared from consultation output (exit {code}): {err!r}")


def t_user_ruling_is_unskippable(tmp: Path) -> None:
    """NO DRIVER-ONLY PATH REACHES `revoked` OR WRITES `promoted` — both are the USER's, proved on the graph.

    Retiring a learning and widening its reach are the two acts that are the user's alone. If a driver step
    could reach `revoked`, a bad learning could be silently retired; if one could write `promoted`, the
    driver could launder its own call into the user's consent. Derived from the graph and `USER_RULINGS`.
    """
    driver = tuple(c for c in TRANSITIONS if c not in USER_RULINGS)
    # `revoked` is reachable ONLY through a user ruling.
    into_revoked = [c for c, (_frm, to) in TRANSITIONS.items() if to == "revoked"]
    check(into_revoked and all(c in USER_RULINGS for c in into_revoked),
          f"`revoked` has a non-user in-edge {into_revoked!r} — a driver could retire a learning")
    # `promoted` is WRITTEN only by a user ruling.
    writes_promoted = [c for c, fields in WRITES.items() if "promoted" in fields]
    check(writes_promoted and all(c in USER_RULINGS for c in writes_promoted),
          f"`promoted` is written by a non-user edge {writes_promoted!r} — promotion must be the user's")
    # And no driver step, from any state, moves toward those.
    for c in driver:
        _frm, to = TRANSITIONS[c]
        check(to != "revoked" and "promoted" not in WRITES.get(c, ()),
              f"driver step `{c}` reaches a user-only outcome")
    # Behaviourally: drive to active, then a driver step never produces a revoked/promoted entry.
    path = tmp / "u.jsonl"
    (lid,) = seed(path)
    for c in driver:
        frm, _to = TRANSITIONS[c]
        if INITIAL in frm:
            run(["--file", str(path), c, "--id", lid, *transition_args(c)])
    entry = find(load(path), lid)
    assert entry is not None
    check(entry["state"] != "revoked" and is_blank(entry["promoted"]) and is_blank(entry["decided"]),
          f"a driver-only sequence produced a user outcome: {entry!r}")
    # …and a driver `set` cannot UNDO or LAUNDER the ruling either. Once the user revokes it, the
    # class-defining claim+anchor are FROZEN, so `set` cannot walk the entry off the identity the ruling
    # named — the write door does not become the hole the transition graph closed.
    run(["--file", str(path), "revoke", "--id", lid, "--reason", "user overturned"])
    for flag in ("--claim", "--anchor"):
        code, _, err = run(["--file", str(path), "set", "--id", lid, flag, "moved"])
        check(code == 1, f"`set {flag}` laundered a revoked ruling (exit {code}): {err!r}")
    after = find(load(path), lid)
    assert after is not None
    check(after["state"] == "revoked", f"`set` walked a revoked entry out of revoked: {after['state']!r}")


def t_ruling_is_recorded(tmp: Path) -> None:
    """The USER's ruling is STAMPED durably (`decided`), and NOTHING the driver does alone stamps it.

    A later heartbeat is a fresh agent that never saw the conversation; it must not re-ask a question the
    user answered. `revoke`/`promote` stamp `decided`; `record`/`stale`/`reaffirm` never do.
    """
    path = tmp / "d.jsonl"
    (a,) = seed(path)
    run(["--file", str(path), "stale", "--id", a, *transition_args("stale")])
    run(["--file", str(path), "reaffirm", "--id", a, *transition_args("reaffirm")])
    entry = find(load(path), a)
    assert entry is not None
    check(is_blank(entry["decided"]), f"a driver step stamped `decided`: {entry['decided']!r}")
    run(["--file", str(path), "promote", "--id", a, *transition_args("promote")])
    entry = find(load(path), a)
    assert entry is not None
    check(not is_blank(entry["decided"]), "promote did not stamp the user's `decided`")
    check(entry["promoted"].startswith(TIERS[0] + "@"), f"promoted is not tier@stamp: {entry['promoted']!r}")


def t_promote_tier_is_validated(tmp: Path) -> None:
    """`promote` demands a REAL tier, and leaves the learning `active` and locally consulted.

    Promotion widens reach; it does not retire the gauntlet-local learning. A bogus tier is refused rather
    than written as if it named somewhere.
    """
    path = tmp / "p.jsonl"
    (a,) = seed(path)
    code, _, err = run(["--file", str(path), "promote", "--id", a, "--tier", "planet"])
    check(code == 1 and "tier" in err, f"a bogus tier was ACCEPTED (exit {code}): {err!r}")
    for blank in BLANKS:
        code, _, _ = run(["--file", str(path), "promote", "--id", a, "--tier", blank])
        check(code == 1, f"promote --tier {blank!r} (blank) was ACCEPTED")
    for tier in TIERS:
        (b,) = seed(path)
        code, out, err = run(["--file", str(path), "promote", "--id", b, "--tier", tier])
        check(code == 0, f"promote --tier {tier} was REFUSED: {err!r}")
        entry = json.loads(out)
        check(entry["state"] == "active", f"promote retired the learning: {entry['state']!r}")
        check(entry["promoted"].startswith(tier + "@"), f"promoted not stamped: {entry['promoted']!r}")


def t_record_refuses_revoked_twin(tmp: Path) -> None:
    """`record` REFUSES to resurrect a class the USER REVOKED — same claim+anchor, revoked, is never
    silently re-recorded.

    A `revoke` is the USER's ruling; `record` is an AUTONOMOUS driver action. Without the guard, re-running
    `record` after `revoke rlN` appends a fresh ACTIVE twin and the driver consults the class again — an
    autonomous path silently undoing a user ruling. `revoked` is terminal (no un-revoke), so the guard
    fails closed, naming the revoked id, and only the user can decide the class holds again.
    """
    path = tmp / "rr.jsonl"
    (a,) = seed(path)                 # rl1: active, claim=claim-0, anchor=anchor-0
    drive_to(path, a, "revoked")
    # a record with the SAME claim+anchor is refused, naming the revoked id, and adds no row.
    code, _, err = run(["--file", str(path), *record_argv(0)])
    check(code == 1, f"record of a REVOKED twin was ACCEPTED (exit {code}): {err!r}")
    check(a in err, f"the refusal does not name the revoked id {a!r}: {err!r}")
    entries = load(path)
    check(len(entries) == 1 and entries[0]["state"] == "revoked",
          f"a refused record still changed the store: {[(e['id'], e['state']) for e in entries]!r}")
    # a DIFFERENT claim+anchor still records — the guard is CONTENT-scoped, not a global block.
    code, out, err = run(["--file", str(path), *record_argv(1)])
    check(code == 0, f"a distinct-content record was refused after a revoke: {err!r}")
    check(json.loads(out)["state"] == INITIAL, f"the distinct record is not active: {out!r}")


def t_record_is_one_live_per_class(tmp: Path) -> None:
    """`record` keeps ONE LIVE record per class — an active OR stale twin (same claim+anchor) is refused.

    Two rows for one class would split it across ids and let the driver consult a stale copy while a fresh
    one exists. `record` refuses the twin naming the live id, and adds no row — the revoked-twin refusal is
    the same guard for the terminal state.
    """
    path = tmp / "ol.jsonl"
    (a,) = seed(path)                      # rl1: active, claim-0/anchor-0
    code, _, err = run(["--file", str(path), *record_argv(0)])
    check(code == 1, f"a second ACTIVE twin was recorded (exit {code}): {err!r}")
    check(a in err, f"the refusal does not name the live id {a!r}: {err!r}")
    # a STALE twin is refused too — stale is still a live record of the class.
    run(["--file", str(path), "stale", "--id", a, "--reason", "anchor moved"])
    code, _, err = run(["--file", str(path), *record_argv(0)])
    check(code == 1, f"a twin of a STALE entry was recorded (exit {code}): {err!r}")
    check(a in err, f"the refusal does not name the stale id {a!r}: {err!r}")
    entries = load(path)
    check(len(entries) == 1, f"a refused record still added a row: {[(e['id'], e['state']) for e in entries]!r}")
    # distinct content still records — the guard is CONTENT-scoped.
    code, _, err = run(["--file", str(path), *record_argv(1)])
    check(code == 0, f"a distinct-content record was refused: {err!r}")


def t_set_cannot_launder_a_ruling(tmp: Path) -> None:
    """`set` cannot edit ANY field of a learning under a durable USER ruling — a revoked one, or a
    promoted/consented one — so no driver `set` rewrites what the user ruled on.

    Without the freeze, `set --anchor other` on a revoked entry moves the retired class onto a new pair,
    silently undoing the revoke; `set --justification` rewrites the reasoning the ruling rested on (the tier
    the user consented to depended on it); on a promoted entry either rewrites what the user consented to
    while keeping the consent stamp. A ruling freezes the entry's WHOLE editable content — every EDITABLE
    field — and a genuine post-ruling change is a fresh USER ruling, not a driver `set`. The revoke also
    HOLDS across a set attempt: the class the user retired can still not be re-recorded.
    """
    # revoked: every editable field is frozen — claim, anchor, and the prose alike.
    rpath = tmp / "sr.jsonl"
    (a,) = seed(rpath)                     # rl1: claim-0/anchor-0
    drive_to(rpath, a, "revoked")
    for field in EDITABLE:
        code, _, err = run(["--file", str(rpath), "set", "--id", a, A.flag_of(field), "moved"])
        check(code == 1, f"set --{field} on a revoked entry was ACCEPTED (exit {code}): {err!r}")
        check(a in err and "revoked" in err, f"the refusal does not name id+state: {err!r}")
    # the revoke HOLDS: the class still cannot be re-recorded after the frozen set attempts.
    code, _, err = run(["--file", str(rpath), *record_argv(0)])
    check(code == 1 and a in err, f"the revoke did not hold after a set attempt: {err!r}")
    # promoted/consented: every editable field is frozen too.
    ppath = tmp / "sp.jsonl"
    (b,) = seed(ppath)
    run(["--file", str(ppath), "promote", "--id", b, "--tier", "repo"])   # active, promoted + decided
    for field in EDITABLE:
        code, _, err = run(["--file", str(ppath), "set", "--id", b, A.flag_of(field), "moved"])
        check(code == 1, f"set --{field} on a promoted entry was ACCEPTED (exit {code}): {err!r}")
        check(b in err, f"the refusal does not name the promoted id {b!r}: {err!r}")


def t_set_cannot_forge_a_revoked_twin(tmp: Path) -> None:
    """`set` cannot edit an active learning ONTO the claim+anchor of a REVOKED one — that would resurrect
    the user-retired class under a fresh id, the same undo `record` refuses, reached through `set`.

    rl2 shares rl1's claim but not its anchor (so `record` allowed it); `set --anchor <rl1's>` would make
    it an exact twin of the revoked rl1, and is refused naming the revoked id. A non-colliding set still
    works — the guard is CONTENT-scoped, not a block on `set`.
    """
    path = tmp / "sf.jsonl"
    (a,) = seed(path)                      # rl1: claim-0/anchor-0
    drive_to(path, a, "revoked")
    # rl2: SAME claim as rl1, DIFFERENT anchor — not an exact twin, so record accepts it.
    code, out, err = run(["--file", str(path), "record", "--claim", seeded("claim", 0),
                          "--justification", "j", "--anchor", "a-different-anchor",
                          "--falsifiability", "f", "--provenance", "p"])
    check(code == 0, f"seeding a same-claim/diff-anchor rl2 failed: {err!r}")
    b = json.loads(out)["id"]
    # editing rl2's anchor onto rl1's makes it an exact twin of the revoked class — refused.
    code, _, err = run(["--file", str(path), "set", "--id", b, "--anchor", seeded("anchor", 0)])
    check(code == 1, f"set forged a twin of a REVOKED class (exit {code}): {err!r}")
    check(a in err, f"the refusal does not name the revoked id {a!r}: {err!r}")
    after = find(load(path), b)
    assert after is not None
    check(after["anchor"] == "a-different-anchor", f"a refused set changed the anchor: {after['anchor']!r}")
    # a non-colliding set still works.
    code, _, err = run(["--file", str(path), "set", "--id", b, "--anchor", "somewhere-else"])
    check(code == 0, f"a non-colliding set was refused: {err!r}")


def t_set_is_one_live_per_class(tmp: Path) -> None:
    """`set` cannot edit an entry's claim+anchor onto ANOTHER LIVE entry's class — an active OR a stale twin
    is refused, so no `set` leaves two records of one claim+anchor.

    `record` already keeps one live record per class; `set` is the other write door, and without the same
    guard `set --anchor <rl1's>` on rl2 forges a second live row of rl1's class, which the driver would then
    consult split across two ids. Staling rl1 first does not license it — a stale entry is still a live
    record, so a twin of it is refused too. (The revoked case is `set-cannot-forge-revoked-twin`.)
    """
    path = tmp / "sol.jsonl"
    (a,) = seed(path)                      # rl1: claim-0/anchor-0
    # rl2: SAME claim as rl1, DIFFERENT anchor — not a twin yet, so record accepts it.
    code, out, err = run(["--file", str(path), "record", "--claim", seeded("claim", 0),
                          "--justification", "j", "--anchor", "other-anchor",
                          "--falsifiability", "f", "--provenance", "p"])
    check(code == 0, f"seeding a same-claim/diff-anchor rl2 failed: {err!r}")
    b = json.loads(out)["id"]
    # editing rl2's anchor onto rl1's makes it an exact twin of an ACTIVE entry — refused, naming the id.
    code, _, err = run(["--file", str(path), "set", "--id", b, "--anchor", seeded("anchor", 0)])
    check(code == 1, f"set forged an ACTIVE twin (exit {code}): {err!r}")
    check(a in err, f"the refusal does not name the live id {a!r}: {err!r}")
    after = find(load(path), b)
    assert after is not None
    check(after["anchor"] == "other-anchor", f"a refused set changed the anchor: {after['anchor']!r}")
    # staling rl1 does NOT license forging a twin of it — a stale entry is still a live record.
    run(["--file", str(path), "stale", "--id", a, "--reason", "anchor moved"])
    code, _, err = run(["--file", str(path), "set", "--id", b, "--anchor", seeded("anchor", 0)])
    check(code == 1, f"set forged a STALE twin (exit {code}): {err!r}")
    check(a in err, f"the refusal does not name the stale id {a!r}: {err!r}")
    # so no two records ever share a claim+anchor, whatever their states.
    classes = [(e["claim"], e["anchor"]) for e in load(path)]
    check(len(set(classes)) == len(classes), f"two entries share a claim+anchor: {classes!r}")
    # a non-colliding set still works — the guard is CONTENT-scoped, not a block on `set`.
    code, _, err = run(["--file", str(path), "set", "--id", b, "--anchor", "somewhere-unique"])
    check(code == 0, f"a non-colliding set was refused: {err!r}")


def t_promote_is_monotone(tmp: Path) -> None:
    """`promote` only WIDENS reach — it never demotes; a monotone-up promotion still succeeds.

    TIERS is ascending, and the doc promises a promotion "only widens its reach". Without the guard,
    `promote --tier account` then `promote --tier repo` overwrites the higher stamp with a lower one and
    re-stamps `decided` — a command named `promote` moving the tier BACKWARD. The refusal happens BEFORE
    any write, so the existing `promoted` stamp and `decided` are untouched.
    """
    path = tmp / "pm.jsonl"
    # monotone UP: repo then account succeeds and widens.
    (a,) = seed(path)
    code, _, err = run(["--file", str(path), "promote", "--id", a, "--tier", "repo"])
    check(code == 0, f"promote --tier repo was refused: {err!r}")
    code, out, err = run(["--file", str(path), "promote", "--id", a, "--tier", "account"])
    check(code == 0, f"a monotone-up promote (repo -> account) was refused: {err!r}")
    check(json.loads(out)["promoted"].startswith("account@"), f"promote did not widen to account: {out!r}")
    # DEMOTE: account then repo is refused, and neither `promoted` nor `decided` moves.
    (b,) = seed(path)
    run(["--file", str(path), "promote", "--id", b, "--tier", "account", "--at", "2026-01-01T00:00:00Z"])
    before = find(load(path), b)
    assert before is not None
    code, _, err = run(["--file", str(path), "promote", "--id", b, "--tier", "repo",
                        "--at", "2026-01-02T00:00:00Z"])
    check(code == 1, f"a DEMOTING promote (account -> repo) was ACCEPTED (exit {code}): {err!r}")
    check("account" in err, f"the refusal does not name the current tier: {err!r}")
    after = find(load(path), b)
    assert after is not None
    check(after["promoted"] == before["promoted"], f"a refused demote changed `promoted`: {after['promoted']!r}")
    check(after["decided"] == before["decided"], f"a refused demote re-stamped `decided`: {after['decided']!r}")


def t_staleness_is_the_expiry(tmp: Path) -> None:
    """A learning EXPIRES to `stale` and returns only by `reaffirm`; the evidence log APPENDS, never clobbers.

    "Falsifiable/expiring" is the gate-safety guardrail: when the anchored code changes, the learning is set
    aside pending re-evaluation, and a fresh investigation must reaffirm it. Its history of going stale and
    being reaffirmed is the audit trail — a second entry never erases the first.
    """
    path = tmp / "s.jsonl"
    (a,) = seed(path)
    run(["--file", str(path), "stale", "--id", a, "--reason", "anchor rewritten"])
    entry = find(load(path), a)
    assert entry is not None
    check(entry["state"] == "stale", f"stale did not set the state: {entry['state']!r}")
    check("[stale " in entry["evidence"] and "anchor rewritten" in entry["evidence"], entry["evidence"])
    # reaffirm only from stale, and it appends.
    (b,) = seed(path)
    code, _, err = run(["--file", str(path), "reaffirm", "--id", b, "--finding", "x"])
    check(code == 1, f"reaffirm from `active` was ACCEPTED (exit {code}): {err!r}")
    run(["--file", str(path), "reaffirm", "--id", a, "--finding", "re-checked: still holds"])
    entry = find(load(path), a)
    assert entry is not None
    check(entry["state"] == "active", "reaffirm did not return the learning to active")
    check(entry["evidence"].count("\n") == 1 and "still holds" in entry["evidence"],
          f"reaffirm did not APPEND beside the stale record: {entry['evidence']!r}")
    # the evidence flag is refused blank at every evidence-writing door.
    for cmd, extra in (("stale", []), ("revoke", [])):
        (c,) = seed(path)
        for _s in extra:
            pass
        for blank in BLANKS:
            code, _, _ = run(["--file", str(path), cmd, "--id", c, INTAKE[cmd]["evidence"], blank])
            check(code == 1, f"{cmd} with a BLANK reason ({blank!r}) was ACCEPTED")


def t_state_and_evidence_are_not_settable(tmp: Path) -> None:
    """`set` edits the PROSE and NOTHING ELSE — not `state`, not the lifecycle records a transition leaves.

    Were `state` settable, `set --state active` would walk a revoked learning back into the consulted set
    without the user; were `provenance`/`decided`/`promoted` settable, the record of WHO settled or ruled
    could be rewritten after the fact. Only `EDITABLE` prose can change, and never to blank.
    """
    path = tmp / "e.jsonl"
    (a,) = seed(path)
    parser = A.build_parser()
    set_flags = {act.dest for act in parser._subparsers._group_actions[0].choices["set"]._actions  # type: ignore[attr-defined]
                 if act.dest not in ("help", "id")}
    check(set_flags == set(EDITABLE), f"`set` offers {set_flags!r}, not exactly EDITABLE {set(EDITABLE)!r}")
    for guarded in ("state", "provenance", "decided", "promoted", "recorded", "evidence"):
        code, _, err = run(["--file", str(path), "set", "--id", a, f"--{guarded}", "x"])
        check(code == 2, f"`set --{guarded}` was accepted — that field must not be settable: {err!r}")
    for blank in BLANKS:
        code, _, _ = run(["--file", str(path), "set", "--id", a, "--claim", blank])
        check(code == 1, f"`set --claim {blank!r}` blanked a REQUIRED field")
    code, out, err = run(["--file", str(path), "set", "--id", a, "--claim", "sharpened"])
    check(code == 0 and json.loads(out)["claim"] == "sharpened", f"a legal set was refused: {err!r}")


def t_transition_graph(tmp: Path) -> None:
    """Every transition is checked against its FROM-set; the graph is the guard, not a settable string."""
    # STATES lists `active` twice (INITIAL, and the state reaffirm/promote return to), so the file name is
    # keyed by the loop INDEX — a per-(cmd,start) fresh store, never one a prior iteration already retired.
    for cmd, (frm, to) in TRANSITIONS.items():
        for si, start in enumerate(STATES):
            path = tmp / f"g-{cmd}-{si}-{start}.jsonl"
            (a,) = seed(path)
            if start != INITIAL:
                drive_to(path, a, start)
            code, _, err = run(["--file", str(path), cmd, "--id", a, *transition_args(cmd)])
            if start in frm:
                check(code == 0, f"`{cmd}` from legal state {start!r} was refused: {err!r}")
                check(find(load(path), a)["state"] == to,  # type: ignore[index]
                      f"`{cmd}` from {start!r} did not land on {to!r}")
            else:
                check(code == 1, f"`{cmd}` from ILLEGAL state {start!r} was ACCEPTED (exit {code})")


def t_ids_are_assigned_and_never_reused(tmp: Path) -> None:
    """Ids are assigned by the store, sequential (`rl<N>`), and NEVER reused — the high-water mark persists."""
    path = tmp / "i.jsonl"
    ids = seed(path, 3)
    check(ids == ["rl1", "rl2", "rl3"], f"ids are not sequential from rl1: {ids!r}")
    # the mark rides in the store; a fresh process keeps counting past it.
    _, out, _ = run(["--file", str(path), *record_argv(9)])
    check(json.loads(out)["id"] == "rl4", f"the next id is not rl4: {out!r}")
    text = path.read_text()
    check(f'"type": "{SEQ_TYPE}"' in text and '"high": 4' in text, f"the high-water mark is not persisted: {text!r}")


def t_store_is_validated(tmp: Path) -> None:
    """A corrupt store is REJECTED naming its line, never silently repaired or skipped; a missing one is empty."""
    check(load(tmp / "missing.jsonl") == [], "a missing store is not read as empty")
    bad = {
        "not-json": "{ this is not json",
        "not-object": "[1, 2, 3]",
        "unknown-type": json.dumps({"type": "nope"}),
        "unknown-state": entry_line(id="rl1", state="zombie"),
        "unknown-key": json.dumps({"type": ENTRY_TYPE, **json.loads(entry_line(id="rl1")), "extra": "x"}),
        "dup-id": None,  # two lines, handled below
        "blank-required": entry_line(id="rl1", claim=""),
    }
    for name, line in bad.items():
        if line is None:
            continue
        p = write_lines(tmp / f"{name}.jsonl", line)
        code, _, err = run(["--file", str(p), "list"])
        check(code == 1, f"a {name} store LOADED (exit {code})")
        check("line 1" in err or "line" in err, f"the {name} refusal does not name the line: {err!r}")
    p = write_lines(tmp / "dup.jsonl", entry_line(id="rl1"), entry_line(id="rl1"))
    code, _, err = run(["--file", str(p), "list"])
    check(code == 1 and "duplicate" in err, f"a duplicate id was not rejected: {err!r}")


def t_defaults_backfill(tmp: Path) -> None:
    """An entry written before an OPTIONAL field existed reads back complete — an absent optional key
    defaults, so the schema can grow without migrating a store that cannot be rebuilt."""
    rec = json.loads(entry_line(id="rl1"))
    del rec["promoted"]
    del rec["decided"]
    p = write_lines(tmp / "b.jsonl", json.dumps(rec))
    code, out, err = run(["--file", str(p), "get", "--id", "rl1"])
    check(code == 0, f"an entry omitting an optional field was refused: {err!r}")
    got = json.loads(out)
    check(got["promoted"] == PLACEHOLDER and got["decided"] == PLACEHOLDER,
          f"an absent optional field did not backfill to the placeholder: {got!r}")


def t_every_value_is_a_string(tmp: Path) -> None:
    """EVERY VALUE IS A STRING — a `null` or a number is refused, never coerced, and a refused store is not
    rewritten by the next write door."""
    for field in FIELDS:
        for raw in ("null", "123", "1.5", "true", "[]", "{}"):
            p = write_lines(tmp / f"{field}-{raw}.jsonl", raw_line(field, raw, id="rl1"))
            before = p.read_text()
            code, _, err = run(["--file", str(p), "list"])
            check(code == 1, f"a {raw} in {field!r} LOADED (exit {code}) — every value is a STRING")
            check(field in err, f"the refusal of a {raw} {field!r} does not name the field: {err!r}")
            run(["--file", str(p), "set", "--id", "rl1", "--claim", "new"])
            check(p.read_text() == before, f"a REFUSED store was REWRITTEN by the next write: {field}={raw}")


def t_concurrent_writers_lose_nothing(tmp: Path) -> None:
    """CONCURRENT RUNS MUST NOT LOSE A LEARNING — the store is locked, and this proves it.

    Every concurrent campaign run writes THIS file, and a read-modify-write race silently drops entries with
    no other copy anywhere. Real processes, real contention; drop the `flock` in `locked()` and this reds.
    """
    path = tmp / "race.jsonl"
    writers, adds = 8, 4
    # Each record is a DISTINCT class (claim+anchor keyed by pid and iteration), because the store keeps
    # one live record per class — so this proves the LOCK, not a coincidence of identical content. The
    # flags are derived from `REQUIRED`, so a new required field is still passed by every writer.
    script = (
        "import importlib.util, os, sys;"
        "spec = importlib.util.spec_from_file_location('review_learnings', %r);"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
        "pid = os.getpid();"
        "[m.main(['--file', %r, 'record'] + "
        "[x for f in m.REQUIRED for x in (m.INTAKE['record'][f], f + '-' + str(pid) + '-' + str(k))]) "
        "for k in range(%d)]"
        % (str(SCRIPT), str(path), adds)
    )
    procs = [subprocess.Popen([sys.executable, "-c", script],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for _ in range(writers)]
    for p in procs:
        _, err = p.communicate(timeout=120)
        check(p.returncode == 0, f"a concurrent writer failed ({p.returncode}): {err.decode()!r}")
    entries = load(path)
    ids = [e["id"] for e in entries]
    check(len(entries) == writers * adds,
          f"{writers}×{adds} adds left {len(entries)} entries — {writers * adds - len(entries)} LOST to a race")
    check(len(set(ids)) == len(ids), f"an id was handed out twice under concurrency: {ids!r}")


def t_write_is_atomic_and_private(tmp: Path) -> None:
    """A failed replacement leaves the old store intact; a successful one keeps the store private (0600)."""
    # A plain ACTIVE learning under no ruling — `set --claim` edits its prose, so this exercises the
    # atomic-replace path (a ruled entry would freeze its whole content and never reach the write).
    path = write_lines(tmp / "atomic.jsonl", entry_line(id="rl1", promoted=PLACEHOLDER, decided=PLACEHOLDER))
    path.chmod(0o644)
    before = path.read_bytes()
    real_replace = os.replace

    def dying_replace(a: str, b: str) -> None:
        raise OSError("the machine died between the write and the rename")

    os.replace = dying_replace  # type: ignore[assignment]
    try:
        code, _, err = run(["--file", str(path), "set", "--id", "rl1", "--claim", "changed"])
    finally:
        os.replace = real_replace
    check(code == 1, f"a failed replacement exited {code}, not the CLI's refusal code")
    check("cannot write the store to" in err and "Nothing was touched." in err,
          f"the replacement failure escaped clean I/O: {err!r}")
    check(path.read_bytes() == before, "a failed replacement changed the store")
    check(path.stat().st_mode & 0o777 == 0o644, "a failed replacement changed the old store's permissions")
    check(not sorted(path.parent.glob(".review-learnings-*.tmp")), "the failed replacement left temp files")

    old_mask = os.umask(0o022)
    try:
        code, _, err = run(["--file", str(path), "set", "--id", "rl1", "--claim", "changed"])
    finally:
        os.umask(old_mask)
    check(code == 0, f"the unsabotaged write exited {code}: {err!r}")
    check(path.stat().st_mode & 0o777 == 0o600, f"the write left the store {path.stat().st_mode & 0o777:o}, not 600")


def t_table_hides_revoked(tmp: Path) -> None:
    """The default view hides ONLY `revoked`; `active`/`stale` (open, still-consultable) always show.

    A hidden state must be one the graph cannot leave (hidden ⊆ TERMINAL), so the view never buries an
    entry somebody can still act on. `--all` shows every entry and claims nothing hidden.
    """
    path = tmp / "t.jsonl"
    write_lines(path, *(entry_line(id=f"rl{i + 1}", state=s) for i, s in enumerate(STATES)))
    code, out, err = run(["--file", str(path), "table", "--fields", "id,state"])
    check(code == 0, f"table exited {code}: {err!r}")
    _, _, cells = ledger_test.grid(ledger, out, ("id", "state"), ("store", "rule"), TABLE_MARKERS)
    shown = [c[1] for c in cells]
    check(shown == [s for s in STATES if s not in TABLE_HIDDEN_STATES],
          f"the default view hid something other than the closed states — it shows {shown!r}\n{out}")
    check("active" in shown and "stale" in shown, f"an open learning was hidden: {shown!r}")
    check(set(TABLE_HIDDEN_STATES) <= set(TERMINAL),
          f"{sorted(set(TABLE_HIDDEN_STATES) - set(TERMINAL))!r} is HIDDEN and yet still has a move left")
    code, out, err = run(["--file", str(path), "table", "--all", "--fields", "id,state"])
    _, _, cells = ledger_test.grid(ledger, out, ("id", "state"), ("store", "rule"), TABLE_MARKERS)
    check([c[1] for c in cells] == list(STATES), f"--all did not show every entry: {cells!r}")
    check(ledger_test.notices(out) == [], f"--all hid nothing, so it must claim nothing was hidden\n{out}")


def t_table_omission_is_never_silent(tmp: Path) -> None:
    """THE OMISSION IS STATED, the COUNT is correct, and an all-revoked store never reads as an empty one."""
    empty = write_lines(tmp / "empty.jsonl")
    closed = write_lines(tmp / "closed.jsonl", entry_line(id="rl1", state="revoked"),
                         entry_line(id="rl2", state="revoked"))
    _, blank, _ = run(["--file", str(empty), "table"])
    check(ledger_test.notices(blank) == [TABLE_EMPTY_MARKER],
          f"an empty store must say exactly {TABLE_EMPTY_MARKER!r}: {ledger_test.notices(blank)!r}")
    _, out, _ = run(["--file", str(closed), "table"])
    check(ledger_test.notices(out) == [TABLE_ALL_HIDDEN_MARKER, hidden_notice(2, TABLE_HIDDEN_STATES)],
          f"an all-revoked store must say it is NOT empty and how many it hid: {ledger_test.notices(out)!r}")
    check(out != blank, "an all-revoked store renders exactly what an empty one renders")


def t_table_grid_integrity(tmp: Path) -> None:
    """NO VALUE CAN FORGE THE LAYOUT — every hostile value, in every column, parsed back by the ledger oracle.

    A learning's `claim`/`justification`/`evidence` are free text derived from a reviewer's output. This
    store prints through the shared `escape_cell()`/`grid_lines()` and is checked by the SAME parser.
    """
    for name, hostile in ledger_test.hostile(A).items():
        cells = {f: hostile for f in ("claim", "justification", "anchor")}
        if is_blank(hostile):
            cells = {f: f"x{hostile}x" for f in cells}
        path = write_lines(tmp / f"g-{name}.jsonl",
                           entry_line(id="rl1", **cells), entry_line(id="rl2", claim="benign"))
        for fields in (("id", "claim", "state"), ("claim",), ("anchor", "id")):
            code, out, err = run(["--file", str(path), "table", "--fields", ",".join(fields)])
            check(code == 0, f"[{name}] table exited {code}: {err!r}")
            _, _, got = ledger_test.grid(ledger, out, fields, ("store", "rule"), TABLE_MARKERS)
            check(len(got) == 2, f"[{name}] the value forged an ENTRY: {len(got)} rows, not 2\n{out}")
            check(got[0] == [escape_cell({**cells, "id": "rl1", "state": "active"}[f]) for f in fields],
                  f"[{name}] the printed row is not the escaped row: {got[0]!r}\n{out}")
            check(ledger_test.notices(out) == [],
                  f"[{name}] a VISIBLE row forged an out-of-band line: {ledger_test.notices(out)!r}")


def t_fields_and_lookup(tmp: Path) -> None:
    """Read BY FIELD NAME: `get --field`, `list --where`. An unknown or EMPTY field is REJECTED."""
    path = tmp / "f.jsonl"
    (a,) = seed(path)
    code, out, _ = run(["--file", str(path), "get", "--id", a, "--field", "claim"])
    check(code == 0 and out.strip() == seeded("claim"), f"get --field claim returned {out!r}")
    for bad in ("", "nope"):
        code, _, err = run(["--file", str(path), "get", "--id", a, "--field", bad])
        check(code in (1, 2), f"get --field {bad!r} was accepted")
    _, out, _ = run(["--file", str(path), "list", "--where", f"provenance={seeded('provenance')}"])
    check(out.strip() == a, f"--where could not match a value on disk: {out!r}")
    code, _, err = run(["--file", str(path), "list", "--where", "nope=x"])
    check(code == 1, f"--where on an unknown field was accepted: {err!r}")


CASES = [
    ("record-is-the-way-in", "record makes an ACTIVE learning and refuses a rumor — every required field", t_record_is_the_only_way_in),
    ("store-never-deletes", "the store ACCUMULATES — no edge removes an entry; a revoked learning is kept", t_store_never_deletes),
    ("legacy-demote-readable", "legacy DEMOTE learnings remain readable without migration", t_legacy_demote_entry_remains_readable),
    ("user-ruling-unskippable", "no driver-only path reaches `revoked` or writes `promoted` — proved on the graph", t_user_ruling_is_unskippable),
    ("ruling-recorded", "the USER's ruling is stamped durably; nothing the driver does alone stamps it", t_ruling_is_recorded),
    ("promote-tier-validated", "promotion names a real tier and keeps the learning locally consulted", t_promote_tier_is_validated),
    ("record-refuses-revoked-twin", "record refuses to resurrect a user-REVOKED class — same claim+anchor", t_record_refuses_revoked_twin),
    ("record-one-live-per-class", "record refuses an active OR stale twin — one live record per class", t_record_is_one_live_per_class),
    ("set-cannot-launder-a-ruling", "set cannot edit ANY field under a revoke or a promote — no laundering", t_set_cannot_launder_a_ruling),
    ("set-cannot-forge-revoked-twin", "set cannot edit an active entry onto a revoked class's claim+anchor", t_set_cannot_forge_a_revoked_twin),
    ("set-one-live-per-class", "set cannot edit onto an active OR stale entry's class — one live per class", t_set_is_one_live_per_class),
    ("promote-is-monotone", "promote only widens reach — a demoting --tier is refused, nothing moves", t_promote_is_monotone),
    ("staleness-is-expiry", "a learning EXPIRES to stale and returns only by reaffirm; evidence appends", t_staleness_is_the_expiry),
    ("state-not-settable", "`set` edits prose only — never state, provenance, or a lifecycle record", t_state_and_evidence_are_not_settable),
    ("transition-graph", "every transition is checked against its FROM-set; nothing else moves state", t_transition_graph),
    ("ids-never-reused", "ids are store-assigned, sequential, and never reused — the mark persists", t_ids_are_assigned_and_never_reused),
    ("store-validated", "a corrupt store is rejected NAMING its line; a missing one is empty", t_store_is_validated),
    ("defaults-backfill", "an entry written before an optional field existed reads back complete", t_defaults_backfill),
    ("values-are-strings", "every value is a STRING — a null or a number is refused, never coerced", t_every_value_is_a_string),
    ("concurrent-writers", "concurrent runs lose NOTHING — the read-modify-write is locked", t_concurrent_writers_lose_nothing),
    ("write-atomic-private", "replacement failure preserves the old store; successful writes stay 0600", t_write_is_atomic_and_private),
    ("table-hides-revoked", "the default view hides only revoked; open learnings always show", t_table_hides_revoked),
    ("table-omission-loud", "the omission is never silent, and an all-revoked store never reads as empty", t_table_omission_is_never_silent),
    ("table-grid-integrity", "no hostile value forges a column, an entry, or an out-of-band line", t_table_grid_integrity),
    ("fields-and-lookup", "read by FIELD NAME; an unknown or empty field is rejected", t_fields_and_lookup),
]


def self_test() -> int:
    """Run every fixture. Return 0 iff every rule the store claims to enforce actually holds."""
    if not CASES:
        print("CASES is EMPTY — this suite has not tested anything, and that is not a pass.")
        return 1
    failures = 0
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, (name, rule, fn) in enumerate(CASES):
            work = Path(tmpdir) / f"{i:02d}-{name}"
            work.mkdir(parents=True)
            try:
                fn(work)
            except SelfTestFailure as exc:
                print(f"FAIL     {name:24} -> {rule}\n         {exc}")
                failures += 1
                continue
            except SystemExit as exc:
                print(f"FAIL     {name:24} -> {rule}\n         the accessor REFUSED the store (exit "
                      f"{exc.code}) inside the fixture — its message is on stderr, above")
                failures += 1
                continue
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL     {name:24} -> {rule}\n         raised {type(exc).__name__}: {exc}")
                failures += 1
                continue
            print(f"ok       {name:24} -> {rule}")
    print()
    if failures:
        print(f"{failures} check(s) FAILED — the review-learnings store's contract is broken.")
        return 1
    print(f"all {len(CASES)} fixtures hold — the review-learnings store's contract is intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
