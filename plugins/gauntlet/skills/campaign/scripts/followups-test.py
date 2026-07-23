#!/usr/bin/env python3
# ci: pyright
"""THE FOLLOW-UP STORE'S CONTRACT, EXECUTED — the fixtures for `followups.py`.

RUN IT THROUGH THE SCRIPT IT TESTS: `python3 followups.py self-test`. That is what CI invokes, and it is
the entry point that owns the exit code. This module is loaded from there, BY PATH.

EVERY FIXTURE MUST PIN A RULE — it must go red if its rule is deleted or weakened. A fixture that would
still pass with its rule gone tests nothing and manufactures false confidence.

The rendering fixtures lean on the LEDGER's oracle (`ledger-test.py`'s `grid`) and its hostile corpus on
purpose: both stores print through the shared table renderer, so both must be checked by the same parser.
A second, weaker copy of the oracle would be free to bless output the real one rejects.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger  # noqa: E402
from _gauntlet.table import escape_cell, hidden_notice  # noqa: E402
from _gauntlet.testing import capture_cli  # noqa: E402

# The ledger's test ORACLE — `grid`, `notices`, `check`, `SelfTestFailure`, and the hostile corpus — lives
# in the ledger's SIBLING suite, `ledger-test.py`, loaded BY PATH (its name is not a legal module name).
# The follow-up store and ledger print through the same renderer, so follow-up output MUST be parsed back
# by the ledger test's oracle — not a friendlier copy. `grid` takes the store's config-field names and
# markers; the ledger module is handed in as `L` for the compatibility exports used by that oracle.
_ledger_test_path = Path(__file__).resolve().parent / "ledger-test.py"
_spec = importlib.util.spec_from_file_location("ledger_test", _ledger_test_path)
if _spec is None or _spec.loader is None:  # a broken install — never an input error
    raise SystemExit(f"followups-test: cannot load the ledger's oracle at {_ledger_test_path}")
ledger_test = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ledger_test)
SelfTestFailure = ledger_test.SelfTestFailure
check = ledger_test.check

# The module under test. `followups.py` registers itself under this name before it loads these fixtures, so
# this resolves to the module that is actually RUNNING — not a second copy of the same file.
import followups  # noqa: E402
from followups import (  # noqa: E402
    ACT_CMD, ACT_CONDITIONS, ACT_FLAGS, ACT_WITNESSES, BLANK_WHY, DEFAULTS, DELETED, DELETING,
    DRIVER_STEPS, DURABLE_RECORD, EDITABLE, ENTRY_TYPE, EVIDENCE_FIELDS, FIELDS, FLAG, INTAKE,
    INVESTIGATION, OPTIONAL, PLACEHOLDER, REQUIRED, SEQ_TYPE, STATES, TABLE_ALL_HIDDEN_MARKER,
    TABLE_DEFAULT_FIELDS, TABLE_EMPTY_MARKER, TABLE_HIDDEN_STATES, TABLE_MARKERS, TERMINAL, TRANSITIONS,
    USER_RULINGS, WRITE_CMDS, WRITES, build_parser, deletable, find, is_blank, load,
)


def run(argv: "list[str]") -> "tuple[int, str, str]":
    """Drive the REAL CLI in-process and capture (exit code, stdout, stderr)."""
    return capture_cli(followups.main, argv)


def entry_line(**over: object) -> str:
    """A raw store line for a COMPLETE entry — every field carrying something, `**over` on top.

    Every fixture that hand-writes a store builds its lines here, so what a legal line looks like is spelled
    once. A fixture that wants a DEFECTIVE entry blanks a field on purpose — that is what `**over` is for.
    """
    rec = {"type": ENTRY_TYPE, **DEFAULTS,
           **{f: f"<{f}>" for f in FIELDS if f not in ("id", "state")}, **over}
    return json.dumps(rec)


def raw_line(field: str, raw: str, **over: object) -> str:
    """An entry line whose `field` carries RAW JSON TEXT (`null`, `123`) — shapes no write door can produce.

    A `str` is the only thing `argparse` can hand a door, so these are the shapes that say "this line did
    not come from the CLI". They cannot be spelled through `json.dumps` from a fixture's dict, so the token
    is spliced in as text.
    """
    rec = json.loads(entry_line(**over))
    rec[field] = "@@RAW@@"
    return json.dumps(rec).replace('"@@RAW@@"', raw)


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


# EVERY SPELLING OF "THIS VALUE CARRIES NOTHING" that `is_blank()` recognises — including the PLACEHOLDER,
# and the placeholder with whitespace around it. Spelled once, here: a fixture carrying its own private list
# of blanks is how `-` slipped past three doors at once while every one of them looked tested, so every
# fixture that loops over BLANKS picks up a spelling added tomorrow with no edit.
BLANKS = ("", "   ", "\t", PLACEHOLDER, f" {PLACEHOLDER} ")


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


def seeded(field: str, i: int = 0) -> str:
    """The value `seed()` puts in `field`. ONE convention — a fixture that hard-codes `'e0'` silently stops
    checking the field it names if `seed()` ever writes something else."""
    return f"{field}-{i}"


def add_argv(i: int = 0) -> "list[str]":
    """A LEGAL `add` — every REQUIRED field, carrying something. Derived from `REQUIRED`, never retyped.

    So a field added to REQUIRED tomorrow is passed by every fixture that makes an entry, with no edit: the
    suite goes on testing the RULES instead of going red because it no longer knows how to build a follow-up.

    THE FLAG COMES FROM `INTAKE`, NOT FROM `flag_of()`. Most fields' flag IS their name, and assuming that is
    how a fixture ends up typing a flag the CLI does not offer: `found_run` is taken by `--run`.
    """
    return ["add", *(a for f in REQUIRED for a in (INTAKE["add"][f], seeded(f, i)))]


def seed(path: Path, n: int = 1) -> "list[str]":
    ids = []
    for i in range(n):
        code, out, err = run(["--file", str(path), *add_argv(i)])
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


def write_argv(cmd: str, fid: str, field: str, value: str) -> "list[str]":
    """The argv for a WRITE door with ONE field set to `value` and every other value LEGAL.

    Derived from INTAKE (which flags the door takes) and WRITES/REQUIRED (which of them it cannot be
    without), so a fixture that loops over the write doors covers a new door, or a new field on an old
    one, the day it is added — with no edit here.
    """
    argv = [cmd] + ([] if cmd == "add" else ["--id", fid])
    for f, flag in INTAKE[cmd].items():
        if f == field:
            argv += [flag, value]
        elif cmd == "add" and f in REQUIRED:
            argv += [flag, f"add:{f}"]                      # the door cannot be without these
        elif cmd in TRANSITIONS and f in WRITES[cmd] and f not in OPTIONAL:
            argv += [flag, f"{cmd}:{f}"]                    # nor these — the edge's own witnesses
    return argv


def t_user_ruling_is_unskippable(tmp: Path) -> None:
    """THE DRIVER CAN NEVER REACH `accepted`, NOR RUN `publish`, ON ITS OWN — proved ON THE GRAPH, not on
    one lucky path.

    THE load-bearing rule of this store, and the ONE that neither the ACT tier nor DELETION was allowed to
    break. The driver may investigate freely, and it may TAKE UP a corroborated follow-up for work — but
    publication is a claim made in the USER's name, so `publish` leaves only from `accepted`, `accepted`
    has exactly one in-edge, and that edge is the user's.

    `publish` DELETES the entry rather than parking it in a `published` state, and that changes NOTHING
    here: the guarantee was never about the state it landed in, it was about which states the STEP may
    leave FROM.

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
    straight past `accept` — `set --state accepted` — and the whole graph would be decoration.

    THE SAME IS TRUE OF EVERY WITNESS. A `set --act-reversible x` would let the driver assert the ACT
    conditions, take the work up, and then rewrite the grounds it acted on. So the rule is not "state is
    frozen" but "NOTHING A TRANSITION WROTE IS EDITABLE", and it is derived from `EVIDENCE_FIELDS`: a new
    witness is covered here the day the graph gains it.
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

    A ruling that is not durable gets re-asked by the next heartbeat — a fresh agent never saw the
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
    # into the user's agreement.
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
    case is how the rule rots: with only `evidence` blank-tested, the blank check could be narrowed to
    `evidence` alone — dropping `title` and `deferred_why` outright — and this suite stayed GREEN through
    it. A field added to REQUIRED tomorrow is pinned here with no edit.
    """
    path = tmp / "f.jsonl"
    for missing in REQUIRED:
        argv = ["--file", str(path), "add"]
        for f in REQUIRED:
            if f != missing:
                argv += [INTAKE["add"][f], "x"]
        code, _, err = run(argv)
        check(code == 2, f"add without --{missing} was ACCEPTED (exit {code}): {err!r}")
    for blanked in REQUIRED:
        for blank in BLANKS:
            argv = ["--file", str(path), "add"]
            for f in REQUIRED:
                argv += [INTAKE["add"][f], blank if f == blanked else "x"]
            code, _, err = run(argv)
            check(code == 1,
                  f"add with a BLANK --{blanked} ({blank!r}) was ACCEPTED (exit {code}) — the field is "
                  f"REQUIRED, so a value made only of whitespace is not a value")
            check("rumor" in err, f"add failed for the wrong reason: {err!r}")
    check(load(path) == [], "a REFUSED add still wrote an entry to the store")

    # …and a legitimate NON-ASCII title is a value like any other. The blank rule asks whether a value is
    # whitespace, not whether it is ASCII: a follow-up raised against a Japanese identifier, or a name with
    # an accent in it, must go in.
    code, out, err = run(["--file", str(path), "add", "--title", "日本語のタイトル",
                          "--evidence", "réf: PR #12", "--deferred-why", "out of scope"])
    check(code == 0, f"a non-ASCII follow-up was REFUSED (exit {code}): {err!r}")
    check(json.loads(out)["title"] == "日本語のタイトル", f"the title came back changed: {out!r}")


def t_required_cannot_be_edited_away(tmp: Path) -> None:
    """A REQUIRED FIELD IS REQUIRED AT EVERY DOOR AN ENTRY CAN CHANGE — not only at the one that made it.

    `add` refusing a blank `evidence` guards NOTHING if `set --evidence '   '` hollows the entry out an hour
    later. The result is the same rumor the store exists to refuse, and a worse one: this claim the store
    has already VOUCHED for, because it was checked once, at a door it is no longer standing at.

    THE LOOP RUNS OVER `REQUIRED` × `BLANKS` — never a hand-list. A field added to REQUIRED tomorrow is
    pinned at this door with no edit here, and every spelling of blank is tried, INCLUDING the placeholder.
    Delete the guard in `taken()` and this goes red for `title` first.
    """
    path = tmp / "f.jsonl"
    (fid,) = seed(path)
    before = json.loads(run(["--file", str(path), "get", "--id", fid])[1])
    for field in REQUIRED:
        if field not in EDITABLE:
            continue  # not settable at all — a different door, pinned by `state-not-settable`
        flag = INTAKE["set"][field]
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


def t_every_value_the_cli_takes_is_validated(tmp: Path) -> None:
    """EVERY VALUE THE CLI TAKES IN PASSES `is_blank()` — AT EVERY DOOR, FOR EVERY FIELD, BY CONSTRUCTION.

    The bug this pins had one shape, six times over: the predicate was right, and ONE door did not call it.
    The last of them was `--at` — `accept --id fu1 --at -` exited 0 and wrote an `accepted` entry whose
    `decided` stamp carried nothing, so the store recorded a ruling the user never made. Patching the door a
    reviewer happened to find leaves the next one, so the doors do not check for themselves anymore: they
    all read `taken()`, which loops over `INTAKE`.

    Three checks, all derived — none of them a list of what exists today:

      1. THE PARSER IS READ BACK. Every value-taking flag of every write door must be registered in
         `INTAKE`, with `dest` = the field it writes, and INTAKE must hold nothing the door does not offer.
         `INTAKE` is what `taken()` loops over, so REGISTERED IS VALIDATED — and a flag added to a write
         door tomorrow and not registered goes RED here, on the day it is added, before it can take a blank.
      2. EVERY INTAKE FIELD IS A REAL FIELD, and has a REASON a blank is refused (`BLANK_WHY`) — the reason
         is what the caller is told, and a field nobody could write a reason for is one nobody thought about.
      3. THE BEHAVIOR, end to end: every write door × every field it takes × every spelling of BLANKS is
         REFUSED (exit 1), the entry is UNCHANGED, and the store still LOADS afterwards.

    And the CONVERSE, or a door that refused everything would pass all of it: every edge is driven for real,
    with legal arguments, and the store must load and still be writable afterwards.
    """
    for action in build_parser()._actions:  # noqa: SLF001 — argparse exposes no public accessor
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            subs = action.choices
            break
    else:
        raise SelfTestFailure("the parser has no subcommands — the CLI is not what this file thinks it is")

    for cmd in WRITE_CMDS:
        check(cmd in subs, f"`{cmd}` is a WRITE door with no subcommand — INTAKE and the CLI disagree")
        for action in subs[cmd]._actions:  # noqa: SLF001 — the flags the door ACTUALLY offers
            if not action.option_strings or action.dest in ("help", "id"):
                continue  # `--id` names an entry; it never becomes a value IN one
            check(action.dest in INTAKE[cmd],
                  f"`{cmd} {'/'.join(action.option_strings)}` takes a value into the store and is NOT in "
                  f"INTAKE — so it is not read through `taken()`, so NOTHING checks it for a blank. "
                  f"Register it in INTAKE and it is validated by construction")
            check(action.option_strings == [INTAKE[cmd][action.dest]],
                  f"`{cmd}` offers {action.option_strings!r} for {action.dest!r}, but INTAKE says "
                  f"{INTAKE[cmd][action.dest]!r} — the schema and the CLI have drifted")
        offered = {a.dest for a in subs[cmd]._actions  # noqa: SLF001
                   if a.option_strings and a.dest not in ("help", "id")}
        check(offered == set(INTAKE[cmd]),
              f"INTAKE[{cmd!r}] holds {sorted(INTAKE[cmd])} and `{cmd}` offers {sorted(offered)} — the loops "
              f"below would 'cover' a door by trying fields it does not take")

    for cmd, table in INTAKE.items():
        for field in table:
            check(field in FIELDS, f"INTAKE[{cmd!r}] takes {field!r}, which is not a FIELD of the store")
            check(field in BLANK_WHY,
                  f"{field!r} is taken in by `{cmd}` with no BLANK_WHY — a field whose refusal nobody can "
                  f"explain is a field nobody decided to check")

    for cmd in WRITE_CMDS:
        for field in INTAKE[cmd]:
            path = tmp / f"{cmd}-{field}.jsonl"
            (fid,) = seed(path)
            if cmd in TRANSITIONS:
                drive_to(path, fid, TRANSITIONS[cmd][0][0])
            before = json.loads(run(["--file", str(path), "get", "--id", fid])[1])
            for blank in BLANKS:
                code, out, err = run(["--file", str(path), *write_argv(cmd, fid, field, blank)])
                check(code == 1,
                      f"`{cmd}` with a BLANK {INTAKE[cmd][field]} ({blank!r}) was ACCEPTED (exit {code}) — "
                      f"a value that carries nothing is not a value:\n{out}")
                code, _, err = run(["--file", str(path), "list"])
                check(code == 0,
                      f"after `{cmd} {INTAKE[cmd][field]} {blank!r}` the STORE ITSELF NO LONGER LOADS (exit "
                      f"{code}) — a write door wrote what `load()` refuses, and there is no other copy of "
                      f"these follow-ups anywhere: {err!r}")
                now = json.loads(run(["--file", str(path), "get", "--id", fid])[1])
                check(now == before, f"a REFUSED `{cmd} {INTAKE[cmd][field]} {blank!r}` changed the entry "
                                     f"anyway: {now!r}")
            check(len(load(path)) == 1, f"a REFUSED `{cmd}` added or removed an entry")

    # THE CONVERSE: every edge, driven for real, from a legal state — the store must LOAD and still be
    # WRITABLE afterwards. A door that refused everything would pass every check above.
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
        code, _, err = run(["--file", str(path), *add_argv()])
        check(code == 0, f"after a LEGAL `{cmd}` the store can no longer be ADDED to: {err!r}")

    # …and an omitted optional stamp defaults, while a supplied one is kept.
    path = tmp / "stamp.jsonl"
    (fid,) = seed(path)
    code, out, err = run(["--file", str(path), "accept", "--id", fid])
    check(code == 0, f"`accept` with no --at was refused: {err!r}")
    check(not is_blank(json.loads(out)["decided"]), f"`accept` left no stamp at all: {out!r}")
    (fid,) = seed(path)
    code, out, err = run(["--file", str(path), "accept", "--id", fid, "--at", "2026-01-01T00:00:00Z"])
    check(code == 0, f"`accept --at <stamp>` was refused: {err!r}")
    check(json.loads(out)["decided"] == "2026-01-01T00:00:00Z", f"the stamp was not written: {out!r}")


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
    check(first["evidence"] == seeded("evidence"),
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
    """
    path = tmp / "f.jsonl"
    (fid,) = seed(path)
    run(["--file", str(path), "refute", "--id", fid, "--finding", "cannot reproduce on main"])
    check(state_of(path, fid) == "refuted", "refute did not reach `refuted`")

    code, out, _ = run(["--file", str(path), "list"])
    check(out == f"{fid}\n", f"a refuted follow-up was DROPPED from the store: {out!r}")
    code, out, err = run(["--file", str(path), "table", "--fields", "id,state"])
    check(code == 0, f"table exited {code}: {err!r}")
    _, _, cells = ledger_test.grid(ledger, out, ("id", "state"), ("store", "rule"), TABLE_MARKERS)
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
    gone. So the entry survives take-up, survives the PR opening, and dies only on the MERGE.

    Checked STRUCTURALLY first, on the graph: for every deleting edge, either the edge ITSELF writes a
    durable record (`publish` writes `published`), or EVERY way into the state it leaves from wrote one
    (`in-pr` is reachable only through `open-pr`, which writes `pr`). Nothing ever clears a field, so that
    is what makes the record certain to still be there. A deleting edge added tomorrow from a state with no
    durable record goes red here rather than shipping.
    """
    check(DELETING, "NOTHING deletes an entry — the store is an archive again, and it only grows")
    for cmd in DELETING:
        frm, _ = TRANSITIONS[cmd]
        if set(WRITES[cmd]) & set(DURABLE_RECORD):
            continue  # the deleting step itself records where the follow-up now lives
        for state in frm:
            ins = [c for c, (_, to) in TRANSITIONS.items() if to == state]
            check(ins, f"`{cmd}` deletes a {state!r} entry, and NOTHING reaches {state!r} — an entry that "
                       f"is there was hand-written, and carries no record of anything")
            for into in ins:
                check(set(WRITES[into]) & set(DURABLE_RECORD),
                      f"`{cmd}` deletes a {state!r} entry, and `{into}` reaches {state!r} writing only "
                      f"{sorted(WRITES[into])!r} — NONE of {list(DURABLE_RECORD)}. The work would be gone "
                      f"with nothing, anywhere, to remember it.")

    # …and the DOOR asks the same question, for the day the graph stops answering it. `deletable()` is what
    # `cmd_transition` calls before it destroys anything, and it is exercised DIRECTLY — because the check
    # above is precisely the proof that no CLI sequence can reach it. An untested fail-safe is not a
    # fail-safe.
    for f in DURABLE_RECORD:
        check(deletable({**DEFAULTS, f: "#123"}), f"an entry naming its record in {f!r} is NOT deletable")
    check(not deletable(dict(DEFAULTS)),
          "an entry naming NO durable record is DELETABLE — the work would be destroyed with nothing, "
          "anywhere, left to remember it")
    for blank in BLANKS:
        check(not deletable({**DEFAULTS, **{f: blank for f in DURABLE_RECORD}}),
              f"an entry whose record is {blank!r} is DELETABLE — a record that carries nothing names "
              f"nothing, and the blank predicate is ONE (`is_blank`)")

    # …and end-to-end, an entry that names NO durable record at all SURVIVES the attempt to delete it.
    # EVERY field of `DURABLE_RECORD` is blanked, derived — blank only the `pr` and the entry still names
    # the issue, so `merged` would legitimately succeed and this fixture would pass on the wrong reason.
    path = write_lines(tmp / "no-record.jsonl",
                       entry_line(id="fu1", state="in-pr", **{f: PLACEHOLDER for f in DURABLE_RECORD}))
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
    _, _, cells = ledger_test.grid(ledger, shown, ("id", "state"), ("store", "rule"), TABLE_MARKERS)
    check([c[0] for c in cells] == [b], f"the default view did not hide the rejected entry: {cells!r}")
    code, everything, _ = run(["--file", str(path), "table", "--all", "--fields", "id,state"])
    _, _, cells = ledger_test.grid(ledger, everything, ("id", "state"), ("store", "rule"), TABLE_MARKERS)
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
        _, _, cells = ledger_test.grid(ledger, out, ("id", "state"), ("store", "rule"), TABLE_MARKERS)
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
          f"user's ruling")
    check(not is_blank(u["decided"]), f"a USER-accepted follow-up lost its ruling: {u!r}")
    for witness in ACT_FLAGS:
        check(not is_blank(d[witness]), f"the ACT grounds vanished when the work started: {d!r}")
        check(is_blank(u[witness]),
              f"a USER-accepted follow-up carries ACT grounds ({witness}) it never needed: {u!r}")


def t_the_doc_and_the_code_agree(tmp: Path) -> None:
    """THE THRESHOLD THE DRIVER READS AND THE THRESHOLD THE CODE ENFORCES ARE THE SAME FOUR CONDITIONS.

    The ACT conditions necessarily exist twice: as prose in `references/followups.md`, which is where the
    driver reads the RULE, and as `ACT_CONDITIONS` in the script, which is what REFUSES the step. Two copies
    of one definition is the shape this repo has been bitten by over and over — a summary that has drifted
    from its definition is worse than no summary, because it is the version people actually read.

    So the two are not merely both maintained; their AGREEMENT is executed. Add a fifth condition to the
    code and the doc goes stale — red. Delete one from the code and the doc now documents a condition
    nothing enforces — red.
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
              f"ACT condition '{label}' is enforced by the store and appears NOWHERE in {doc.name} — the "
              f"driver is refused a step it was never told the rule for")
    check(ACT_CMD in text, f"`{ACT_CMD}` — the autonomous edge itself — is not documented in {doc.name}")

    # …and EVERY step is named there, not just the ACT one. Derived from TRANSITIONS: an edge added to the
    # graph — a deleting one above all — that the driver is never told about is an edge it never takes, and
    # the entry it should have ended sits in the queue forever.
    for cmd in TRANSITIONS:
        check(cmd in text,
              f"`{cmd}` is a step the store enforces and {doc.name} never names — the driver cannot take a "
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
    code, _, _ = run(["--file", str(path), *add_argv(), "--id", "fu99"])
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
    """A CORRUPT STORE IS REJECTED, AND THE REFUSAL NAMES THE LINE — never silently repaired or skipped.

    This is the contract `ledger.py` already keeps, applied to this store: a skipped line is a follow-up
    nothing reads, in the one store that has no other copy to heal from. An unrecognised `state` is rejected
    for the same reason it must not be settable: it would sit in the table as something no transition can
    move. That is also what refuses a TOMBSTONE — `DELETED` is a sentinel, NOT a state, so an entry carrying
    it is one that was supposed to be GONE.

    Every case here also asserts the LINE NUMBER, because "the store is corrupt" without one is a message
    that cannot be acted on.
    """
    mark = json.dumps({"type": SEQ_TYPE, "high": 3})
    ok = entry_line(id="fu1")
    for name, lines, needle in (
        ("bad-json", (ok, "{not json"), "followups: malformed JSON on line 2"),
        ("not-object", (ok, '["followup"]'), "followups: line 2: record is not a JSON object"),
        ("unknown-type", (ok, json.dumps({"type": "note", "id": "fu2"})), "line 2: missing or unknown"),
        ("no-type", (ok, json.dumps({"id": "fu2"})), "line 2: missing or unknown"),
        ("unknown-state", (ok, entry_line(id="fu2", state="approved")), "line 2: unknown state"),
        ("deleted-tombstone", (ok, entry_line(id="fu2", state=DELETED)), "line 2: unknown state"),
        ("unknown-key", (ok, json.dumps({**json.loads(ok), "id": "fu2", "pwned": "x"})),
         "line 2: unknown key"),
        ("duplicate-id", (ok, entry_line(id="fu1")), "line 2: duplicate entry for fu1"),
        ("two-marks", (mark, mark, ok), "line 2: a second"),
        ("mark-not-a-number", (json.dumps({"type": SEQ_TYPE, "high": "lots"}),), "line 1: "),
    ):
        p = write_lines(tmp / f"{name}.jsonl", *lines)
        code, _, err = run(["--file", str(p), "list"])
        check(code == 1, f"[{name}] a corrupt store was ACCEPTED (exit {code})")
        check(needle in err, f"[{name}] failed for the wrong reason: {err!r}")

    # A MISSING file, though, is an empty store — the first follow-up must not need a bootstrap step.
    code, out, err = run(["--file", str(tmp / "nope.jsonl"), "list"])
    check((code, out) == (0, ""), f"a missing store is not an empty one: {code} {out!r} {err!r}")


def t_defaults_backfill(tmp: Path) -> None:
    """AN OPTIONAL FIELD BACKFILLS. A REQUIRED ONE DOES NOT — AND ITS ABSENCE REFUSES THE ENTRY.

    THE BACKFILL IS WHY A FIELD CAN BE ADDED TO THE SCHEMA AT ALL. This store cannot be rebuilt, so an entry
    raised before `finding` and the ACT witnesses existed must still load: it is a `candidate`, and every
    field it never heard of reads as the placeholder.

    AND THAT IS EXACTLY AS FAR AS IT MAY GO. Backfill a REQUIRED field and the store MANUFACTURES a
    follow-up out of a fragment: `evidence` and `deferred_why` would read as `-`, which is what an UNSET
    field holds — so the entry looks complete to everything and carries nothing. `list` would load it,
    `accept` would take it, and `publish` would file it as an issue with no evidence at all. So a required
    field that is absent is MISSING, missing reads as blank, and blank is refused.
    """
    # An entry from before the optional fields existed: it carries the REQUIRED fields (it always had to —
    # `add` demanded them) and NOTHING else. It must still load, and read back complete.
    old = {"type": ENTRY_TYPE, "id": "fu1", **{f: f"old-{f}" for f in REQUIRED}}
    p = write_lines(tmp / "old.jsonl", json.dumps(old))
    code, out, err = run(["--file", str(p), "get", "--id", "fu1"])
    check(code == 0, f"get on a pre-schema entry exited {code}: {err!r}")
    entry = json.loads(out)
    check(set(entry) == set(FIELDS), f"get did not project onto FIELDS: {sorted(entry)}")
    check(entry["state"] == DEFAULTS["state"] == "candidate",
          f"a state-less entry did not default to a CANDIDATE (it defaulted to {entry['state']!r}) — an "
          f"entry whose state is unknown must be the one that needs the user, never one that skipped them")
    for f in REQUIRED:
        check(entry[f] == f"old-{f}", f"the {f!r} the entry DID carry was overwritten by a default")
    for f in FIELDS:  # every OPTIONAL field it never heard of — derived, so a new one is covered that day
        if f not in REQUIRED and f not in ("id", "state"):
            check(entry[f] == PLACEHOLDER, f"a pre-schema entry's optional {f!r} did not default")

    # …AND THE OTHER HALF: a REQUIRED field is never invented. Absent is BLANK, and blank is REFUSED.
    for absent in REQUIRED:
        rec = {"type": ENTRY_TYPE, "id": "fu1",
               **{f: f"old-{f}" for f in REQUIRED if f != absent}}
        p = write_lines(tmp / f"no-{absent}.jsonl", json.dumps(rec))
        code, out, err = run(["--file", str(p), "list"])
        check(code == 1,
              f"an entry with NO {absent!r} at all LOADED (exit {code}) — the load door BACKFILLED a "
              f"REQUIRED field the write door refuses, and manufactured a follow-up out of a fragment:\n{out}")
        check(absent in err and "carries no" in err,
              f"the refusal does not name the missing field {absent!r}: {err!r}")


def t_every_value_is_a_string(tmp: Path) -> None:
    """EVERY VALUE IN A FOLLOW-UP IS A STRING — a `null` and a number are not, and neither loads.

    `argparse` can hand a write door NOTHING BUT A `str`, so a non-string on a line is a record this
    accessor did not write. It is REFUSED rather than coerced, because coercing it INVENTS a value:
    `str(None)` is `"None"` and `str(123)` is `"123"` — both non-blank, so both would read as evidence
    somebody wrote, in a store whose whole job is to hold claims a human can audit.

    Every field, every shape, derived from `FIELDS` — a field added tomorrow is covered tomorrow.
    """
    for field in FIELDS:
        for raw in ("null", "123", "1.5", "true", "[]", "{}"):
            p = write_lines(tmp / f"{field}-{raw}.jsonl", raw_line(field, raw, id="fu1"))
            before = p.read_text()
            code, out, err = run(["--file", str(p), "list"])
            check(code == 1,
                  f"a {raw} in {field!r} LOADED (exit {code}) — every value in a follow-up is a STRING, and "
                  f"reading this one as its own text would invent a value nobody wrote:\n{out}")
            check(field in err, f"the refusal of a {raw} {field!r} does not name the field: {err!r}")
            # …and the store it is in is NOT rewritten by the next write door either.
            run(["--file", str(p), "set", "--id", "fu1", "--title", "new"])
            check(p.read_text() == before, f"a REFUSED store was REWRITTEN by the next write: {field}={raw}")

    # THE CONVERSE. A door that refused every value would satisfy everything above.
    p = write_lines(tmp / "s.jsonl", entry_line(id="fu1", found_run="260714"))
    code, out, err = run(["--file", str(p), "get", "--id", "fu1"])
    check(code == 0, f"get exited {code}: {err!r}")
    check(all(isinstance(v, str) for v in json.loads(out).values()), f"a non-string survived load(): {out!r}")
    code, out, _ = run(["--file", str(p), "list", "--where", "found_run=260714"])
    check(out == "fu1\n", f"--where could not match a string value on disk: {out!r}")


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
    script = (  # the `add` is DERIVED (`add_argv`), so a new REQUIRED field races here too, with no edit
        "import sys; sys.path.insert(0, %r); import followups as f;"
        "[f.main(['--file', %r] + %r) for _ in range(%d)]"
        % (str(Path(__file__).resolve().parent), str(path), add_argv(), adds)
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


def t_write_is_atomic_and_private(tmp: Path) -> None:
    """A failed replacement leaves the old store intact; a successful one keeps the store private."""
    path = write_lines(tmp / "atomic.jsonl", entry_line(id="fu1"))
    path.chmod(0o644)
    before = path.read_bytes()

    fsyncs = 0
    src: "Path | None" = None
    dst: "Path | None" = None
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd: int) -> None:
        nonlocal fsyncs
        fsyncs += 1
        real_fsync(fd)

    def dying_replace(a: str, b: str) -> None:
        nonlocal src, dst
        src, dst = Path(a), Path(b)
        raise OSError("the machine died between the write and the rename")

    os.fsync, os.replace = spy_fsync, dying_replace
    try:
        code, _, err = run(["--file", str(path), "set", "--id", "fu1", "--title", "changed"])
    finally:
        os.fsync, os.replace = real_fsync, real_replace

    check(code == 1, f"a failed replacement exited {code}, not the follow-up CLI's refusal code")
    check("cannot write the store to" in err and "Nothing was touched." in err,
          f"the replacement failure escaped the follow-up CLI's clean I/O error: {err!r}")
    check(path.read_bytes() == before, "a failed replacement changed the follow-up store")
    check(path.stat().st_mode & 0o777 == 0o644,
          "a failed replacement changed the old store's permissions")
    check(src is not None and src.parent == path.parent,
          f"the replacement staged bytes outside the store directory: {src}")
    check(dst == path, f"the replacement targeted {dst}, not {path}")
    check(fsyncs >= 1, "the replacement reached rename without fsyncing its bytes")
    temps = sorted(p.name for p in path.parent.glob(".followups-*.tmp"))
    check(not temps, f"the failed replacement left temporary files behind: {temps}")

    old_mask = os.umask(0o022)
    try:
        code, _, err = run(["--file", str(path), "set", "--id", "fu1", "--title", "changed"])
    finally:
        os.umask(old_mask)
    check(code == 0, f"the unsabotaged follow-up write exited {code}: {err!r}")
    check(path.stat().st_mode & 0o777 == 0o600,
          f"the replacement made the follow-up store {path.stat().st_mode & 0o777:o}, not 600")
    temps = sorted(p.name for p in path.parent.glob(".followups-*.tmp"))
    check(not temps, f"the successful replacement left temporary files behind: {temps}")


def t_table_hides_closed(tmp: Path) -> None:
    """The default view hides ONLY the CLOSED entries; everything still owed to someone stays visible.

    A `candidate` is the whole point of the store — it is waiting on the USER — so hiding one would bury
    the exact thing the view exists to surface. `--all` shows every entry.
    """
    path = tmp / "f.jsonl"
    write_lines(path, *(entry_line(id=f"fu{i + 1}", state=s) for i, s in enumerate(STATES)))
    code, out, err = run(["--file", str(path), "table", "--fields", "id,state"])
    check(code == 0, f"table exited {code}: {err!r}")
    _, _, cells = ledger_test.grid(ledger, out, ("id", "state"), ("store", "rule"), TABLE_MARKERS)
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
    _, _, cells = ledger_test.grid(ledger, out, ("id", "state"), ("store", "rule"), TABLE_MARKERS)
    check([c[1] for c in cells] == list(STATES), f"--all did not show every entry: {cells!r}\n{out}")
    check(ledger_test.notices(out) == [], f"--all hid nothing, so it must claim nothing was hidden\n{out}")


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
            _, _, cells = ledger_test.grid(ledger, out, TABLE_DEFAULT_FIELDS, ("store", "rule"), TABLE_MARKERS)
            check(len(cells) == live, f"[{closed}/{live}] {len(cells)} rows shown, not {live}\n{out}")
            said = [n for n in ledger_test.notices(out) if n not in TABLE_MARKERS]
            if not closed:
                check(said == [], f"[{closed}/{live}] nothing was hidden, yet the table says {said!r}")
                continue
            check(said == [hidden_notice(closed, TABLE_HIDDEN_STATES)],
                  f"[{closed}/{live}] the table hid {closed} and reported {said!r}\n{out}")
            # …and the count is what `--all` reveals — derived from the OUTPUT, not the fixture's arithmetic
            _, allout, _ = run(["--file", str(path), "table", "--all"])
            _, _, allcells = ledger_test.grid(ledger, allout, TABLE_DEFAULT_FIELDS, ("store", "rule"), TABLE_MARKERS)
            check(len(allcells) - len(cells) == closed,
                  f"[{closed}/{live}] the notice claims {closed} hidden, --all reveals "
                  f"{len(allcells) - len(cells)} more")

    empty = write_lines(tmp / "empty.jsonl")
    closed_only = write_lines(tmp / "closed.jsonl", entry_line(id="fu1", state="rejected"),
                              entry_line(id="fu2", state="rejected"))
    code, blank, _ = run(["--file", str(empty), "table"])
    check(ledger_test.notices(blank) == [TABLE_EMPTY_MARKER],
          f"an empty store must say exactly {TABLE_EMPTY_MARKER!r}: {ledger_test.notices(blank)!r}")
    code, out, _ = run(["--file", str(closed_only), "table"])
    check(ledger_test.notices(out) == [TABLE_ALL_HIDDEN_MARKER, hidden_notice(2, TABLE_HIDDEN_STATES)],
          f"an all-closed store must say it is NOT empty, and how many it hid: {ledger_test.notices(out)!r}")
    check(out != blank,
          f"an ALL-CLOSED store renders EXACTLY what an EMPTY one renders — 'every follow-up resolved' "
          f"and 'no follow-up ever found' are indistinguishable:\n{out}")
    check(TABLE_EMPTY_MARKER not in out.split("\n"), f"an all-closed store printed the EMPTY marker:\n{out}")


def t_table_grid_integrity(tmp: Path) -> None:
    """NO VALUE CAN FORGE THE LAYOUT — every hostile value, in every column, parsed back mechanically.

    A follow-up's `title` and `evidence` are free text written from a REVIEWER'S OUTPUT. Rendered raw, one
    carrying a `|` fabricates a column, a newline fabricates an entry, a leading `#` fabricates the rule
    line or the omission notice. This store prints through the shared `escape_cell()`/`grid_lines()`, and
    it is checked by the LEDGER'S OWN ORACLE — the same parser, not a friendlier copy of it.

    A BLANK hostile (`''`, `'   '`) is put in NO column at all, and that is not a gap: no column of this
    store can HOLD a blank. A REQUIRED one carries something or the entry is refused, and an OPTIONAL one
    carries the PLACEHOLDER or something non-blank. The hostile CHARACTERS still go through the grid,
    wrapped in visible ones; what is dropped is only the claim that a cell can be empty. Derived from
    `is_blank()`, so a spelling of blank added tomorrow is handled here with no edit.
    """
    for name, hostile in ledger_test.hostile(followups).items():
        cells = {f: hostile for f in ("title", "evidence", "published")}
        if is_blank(hostile):  # …then NO column may hold it: keep the characters, drop the blankness
            cells = {f: f"x{hostile}x" for f in cells}
        path = write_lines(tmp / f"g-{name}.jsonl",
                           entry_line(id="fu1", **cells), entry_line(id="fu2", title="benign"))
        for fields in (("id", "title", "state"), ("title",), ("published", "id")):
            code, out, err = run(["--file", str(path), "table", "--fields", ",".join(fields)])
            check(code == 0, f"[{name}] table exited {code}: {err!r}")
            _, _, got = ledger_test.grid(ledger, out, fields, ("store", "rule"), TABLE_MARKERS)
            check(len(got) == 2, f"[{name}] the value forged an ENTRY: {len(got)} rows, not 2\n{out}")
            check(got[0] == [escape_cell({**cells, "id": "fu1", "state": "candidate"}[f]) for f in fields],
                  f"[{name}] the printed row is not the escaped row: {got[0]!r}\n{out}")
            check(ledger_test.notices(out) == [],
                  f"[{name}] a VISIBLE row forged an out-of-band line: {ledger_test.notices(out)!r}\n{out}")


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
    ("every-value-validated", "EVERY value the CLI takes, at EVERY write door, is refused if it carries nothing — a flag that skips the check cannot exist", t_every_value_the_cli_takes_is_validated),
    ("ids-never-reused", "ids are assigned by the store, sequential, and NEVER reused", t_ids_are_assigned_and_never_reused),
    ("store-validated", "a corrupt store is rejected NAMING ITS LINE, never silently repaired; a missing one is empty", t_store_is_validated),
    ("defaults-backfill", "an entry written before a field existed reads back complete — as a CANDIDATE", t_defaults_backfill),
    ("values-are-strings", "every value in a follow-up is a STRING — a `null` or a number is refused, never coerced", t_every_value_is_a_string),
    ("concurrent-writers", "concurrent runs lose NOTHING — the read-modify-write is locked", t_concurrent_writers_lose_nothing),
    ("write-atomic-private", "replacement failure preserves the old store; successful writes stay mode 0600", t_write_is_atomic_and_private),
    ("table-hides-closed", "the default view hides only CLOSED entries; a candidate always shows", t_table_hides_closed),
    ("table-omission-loud", "the omission is never silent, and an all-closed store never reads as empty", t_table_omission_is_never_silent),
    ("table-grid-integrity", "no hostile title/evidence forges a column, an entry, or an out-of-band line", t_table_grid_integrity),
    ("fields-and-lookup", "read by FIELD NAME; an unknown or empty field is rejected", t_fields_and_lookup),
]


def self_test() -> int:
    """Run every fixture. Return 0 iff every rule the store claims to enforce actually holds.

    A SUITE THAT RAN NOTHING IS NOT A PASS: an empty `CASES` fails here rather than printing an all-clear.
    CI runs `followups.py self-test` and trusts the exit code, so the one thing this must never do is report
    success over a suite that checked nothing.
    """
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
                # A fixture called an accessor DIRECTLY (`load()`, not through `run()`) and it REFUSED —
                # `fail()` raises SystemExit, a BaseException that would otherwise take the whole run down,
                # printing no verdict and naming no rule.
                print(f"FAIL     {name:24} -> {rule}\n         the accessor REFUSED the store (exit "
                      f"{exc.code}) inside the fixture — its message is on stderr, above")
                failures += 1
                continue
            except Exception as exc:  # noqa: BLE001 — a fixture that CRASHES has not passed
                print(f"FAIL     {name:24} -> {rule}\n         raised {type(exc).__name__}: {exc}")
                failures += 1
                continue
            print(f"ok       {name:24} -> {rule}")
    print()
    if failures:
        print(f"{failures} check(s) FAILED — the follow-up store's contract is broken.")
        return 1
    print(f"all {len(CASES)} fixtures hold — the follow-up store's contract is intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
