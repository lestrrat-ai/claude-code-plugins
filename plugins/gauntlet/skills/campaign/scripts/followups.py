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
OWN diagnosis is a claim too"). So the store is LOCAL and stays local: NOTHING in it may be published — a
GitHub issue, a PR — without the USER's agreement ON THAT SPECIFIC ITEM. Filing one unilaterally would
launder an unvalidated self-diagnosis into a public statement of fact.

The lifecycle enforces that structurally: `publish` and `done` are reachable ONLY from `accepted`, and
the ONLY way into `accepted` is the `accept` transition, which exists to record the user's agreement. An
autonomous driver cannot get from `candidate` to `published` — there is no edge. What this CANNOT do is
verify that the user really agreed (no local file can): `accept` is a promise the driver makes. It makes
skipping the user a DELIBERATE LIE rather than an oversight, and that is the whole claim — it is a
footgun guard, NOT a security boundary.

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

# --- schema (owned here, once) ------------------------------------------------

FIELDS = (
    "id", "title", "evidence", "deferred_why", "state", "found_run", "found", "decided", "published",
)
DEFAULTS = {
    "id": "-", "title": "-", "evidence": "-", "deferred_why": "-", "state": "candidate",
    "found_run": "-", "found": "-", "decided": "-", "published": "-",
}

# Fields a caller may EDIT after the fact. `state` is deliberately ABSENT: it moves only through the
# transitions below, which check where it is coming FROM. Were it settable, `set --state published` would
# walk straight past the user's agreement — the one thing this store exists to make unskippable. `id`,
# `found*`, `decided` and `published` are records of what happened, not opinions to revise.
EDITABLE = ("title", "evidence", "deferred_why")

# Fields a follow-up cannot be WITHOUT. A follow-up with no evidence is a RUMOR — a claim nobody can
# check, which is the one thing this store must never accumulate; and one with no `deferred_why` makes
# the next run re-litigate a scoping decision it cannot see.
REQUIRED = ("title", "evidence", "deferred_why")

# --- the lifecycle (owned here, once) -----------------------------------------
#
# THE GRAPH IS THE ENFORCEMENT. `accepted` is a cut vertex: every path from `candidate` to `published` or
# `done` runs through it, and the only edge into it is `accept` — the transition whose entire purpose is
# to record that the USER agreed to THIS item. Remove that edge and there is no route to publication at
# all. This is why the lifecycle is a graph and not a settable string.
#
# `<subcommand>: (states it may be applied FROM, the state it moves TO)`.
TRANSITIONS = {
    "accept":  (("candidate",), "accepted"),
    "reject":  (("candidate", "accepted"), "rejected"),
    "publish": (("accepted",), "published"),
    "done":    (("accepted", "published"), "done"),
}

# The transitions that are the USER'S RULING, not the driver's bookkeeping — they are the ones that stamp
# `decided`. Derived from that fact, never listed twice.
USER_RULINGS = ("accept", "reject")

STATES = ("candidate",) + tuple(dict.fromkeys(to for _, to in TRANSITIONS.values()))

# What the DEFAULT view hides: the CLOSED entries — the ones NOBODY has anything left to do about. A
# `done` follow-up shipped; a `rejected` one the user ruled against. Everything else is somebody's open
# obligation: a `candidate` needs the user's ruling, an `accepted` one needs the work, a `published` one
# needs that work to land. This is the same line `ledger.py`'s TABLE_HIDDEN_STATUSES draws, applied to a
# different store — NOT "terminal" (there, `aborted` is terminal and stays visible precisely because a
# human may still act on it).
TABLE_HIDDEN_STATES = ("rejected", "done")

TABLE_DEFAULT_FIELDS = ("id", "state", "found", "title", "published")

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

def load(path: Path) -> "list[dict]":
    """Return the entries. A missing file is an EMPTY store — a first follow-up, not an error.

    Every record must be `{"type": "followup", …}`; an unknown type is REJECTED, never skipped (a silently
    dropped entry is exactly the loss this store exists to prevent). Values are coerced to `str`, so an
    on-disk JSON number compares as the string key the rest of the accessor uses. `id` and `state` are
    validated: a malformed id could never be addressed again, and an unrecognised state would sit in the
    table as something no transition can move.
    """
    entries: list[dict] = []
    if not path.exists():
        return entries
    seen: set[str] = set()
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            fail(f"malformed JSON on line {n}: {e}")
        if not isinstance(rec, dict):
            fail(f"line {n}: record is not a JSON object")
        if rec.get("type") != "followup":
            fail(f"line {n}: missing or unknown record type {rec.get('type')!r}")
        entry = {f: str(rec.get(f, DEFAULTS[f])) for f in FIELDS}
        if not ID_RE.match(entry["id"]):
            fail(f"line {n}: malformed id {entry['id']!r} (expected fu<N>)")
        if entry["state"] not in STATES:
            fail(f"line {n}: unknown state {entry['state']!r}; valid: {', '.join(STATES)}")
        if entry["id"] in seen:
            fail(f"line {n}: duplicate entry for {entry['id']}")
        seen.add(entry["id"])
        entries.append(entry)
    return entries


def dump(path: Path, entries: "list[dict]") -> None:
    """Write the whole store ATOMICALLY — a temp file in the same directory, then `os.replace()`.

    A partial write here is not a corrupt cache that the next wake heals: it is data that exists NOWHERE
    else. `os.replace()` is atomic on POSIX, so a reader (or a crash) sees either the old store or the
    new one, never half of one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(
        json.dumps({"type": "followup", **{f: str(e.get(f, DEFAULTS[f])) for f in FIELDS}}) + "\n"
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


def find(entries: "list[dict]", fid: str) -> "dict | None":
    for e in entries:
        if e["id"] == fid:
            return e
    return None


def next_id(entries: "list[dict]") -> str:
    """`fu<N>`, one past the highest N in the store — assigned HERE, never by the caller.

    It counts past the highest id rather than the entry count, so an id is never REUSED: reusing one
    would silently re-point every reference to the old entry (an audit file, a PR body, the user's own
    note) at a different follow-up.
    """
    used = [int(e["id"][2:]) for e in entries]
    return f"fu{max(used, default=0) + 1}"


def check_field(name: str, valid: "tuple[str, ...]") -> None:
    if name not in valid:
        fail(f"unknown field '{name}'; valid: {', '.join(valid)}")


# --- subcommands --------------------------------------------------------------

def cmd_add(path: Path, args) -> int:
    with locked(path):
        entries = load(path)
        entry = dict(DEFAULTS)
        for f in REQUIRED:
            value = getattr(args, f)
            if not value.strip():
                fail(f"--{f.replace('_', '-')} must not be empty — a follow-up without it is a rumor")
            entry[f] = value
        entry["id"] = next_id(entries)  # assigned here; never caller-set, never reused
        entry["state"] = "candidate"    # every follow-up starts as a CLAIM
        entry["found_run"] = args.run or "-"
        entry["found"] = args.found or now_iso()
        entries.append(entry)
        dump(path, entries)
    print(json.dumps(entry))
    return 0


def cmd_set(path: Path, args) -> int:
    with locked(path):
        entries = load(path)
        entry = find(entries, args.id)
        if entry is None:
            fail(f"no follow-up {args.id}")
        updates = {f: getattr(args, f) for f in EDITABLE if getattr(args, f) is not None}
        if not updates:
            fail(f"set requires at least one --<field> <value>; editable: {', '.join(EDITABLE)}")
        entry.update(updates)  # by NAME — never by position. `state` is NOT here: see EDITABLE.
        dump(path, entries)
    print(json.dumps(entry))
    return 0


def cmd_transition(path: Path, args) -> int:
    """`accept` / `reject` / `publish` / `done` — the ONLY things that move `state`.

    Each checks the state it is coming FROM against the graph, so the user's agreement cannot be routed
    around: there is no edge from `candidate` to `published` or to `done`.
    """
    frm, to = TRANSITIONS[args.cmd]
    with locked(path):
        entries = load(path)
        entry = find(entries, args.id)
        if entry is None:
            fail(f"no follow-up {args.id}")
        if entry["state"] not in frm:
            fail(
                f"{args.id} is '{entry['state']}' — `{args.cmd}` applies only to: {', '.join(frm)}. "
                f"A follow-up reaches '{to}' only along the transition graph; nothing else moves `state`."
            )
        if args.cmd == "publish":
            if not args.ref.strip():
                fail("--ref must not be empty — a published follow-up must name WHERE it was published")
            entry["published"] = args.ref
        entry["state"] = to
        if args.cmd in USER_RULINGS:
            # The user's ruling is DURABLE DATA, exactly like the ledger's `api_approval`: a later run —
            # or a fresh agent that never saw the conversation — reads it and does not re-ask.
            entry["decided"] = args.at or now_iso()
        dump(path, entries)
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
    return json.dumps({"type": "followup", **DEFAULTS, **over})


def write_lines(path: Path, *lines: str) -> Path:
    """Write a store RAW — bypassing dump(), so a fixture can hold what dump() would never emit."""
    path.write_text("".join(line + "\n" for line in lines))
    return path


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


def t_user_step_is_unskippable(tmp: Path) -> None:
    """A CANDIDATE CAN NEVER BE PUBLISHED OR CLOSED — the user's agreement has no bypass.

    THE load-bearing rule of this store. `accepted` is a cut vertex: every path to `published`/`done` runs
    through it, and the only edge into it is `accept`, which exists to record the user's ruling. Delete
    that check — let `publish` apply to a `candidate` — and an autonomous driver files a GitHub issue for
    an unvalidated self-diagnosis, which is the exact thing this store was built to prevent.
    """
    path = tmp / "f.jsonl"
    (fid,) = seed(path)
    for cmd, extra in (("publish", ["--ref", "#123"]), ("done", [])):
        code, out, err = run(["--file", str(path), cmd, "--id", fid, *extra])
        check(code == 1,
              f"`{cmd}` was ACCEPTED on a CANDIDATE (exit {code}) — the user's agreement was skipped:\n{out}")
        check("applies only to" in err, f"`{cmd}` failed for the wrong reason: {err!r}")
        check(state_of(path, fid) == "candidate",
              f"a refused `{cmd}` still moved the state to {state_of(path, fid)!r}")

    # …and the ONLY route through: accept (the user agreed), and only then publish.
    check(run(["--file", str(path), "accept", "--id", fid])[0] == 0, "accept must succeed on a candidate")
    check(state_of(path, fid) == "accepted", "accept did not reach `accepted`")
    check(run(["--file", str(path), "publish", "--id", fid, "--ref", "#123"])[0] == 0,
          "publish must succeed on an ACCEPTED follow-up")
    check(state_of(path, fid) == "published", "publish did not reach `published`")


def t_state_is_not_settable(tmp: Path) -> None:
    """`set` CANNOT WRITE `state` — not by flag, not by field name.

    The transitions check where a follow-up is coming FROM; `set` does not. A settable `state` would walk
    straight past `accept` — `set --state published` — and the whole graph would be decoration. So `state`
    is absent from EDITABLE, and the flag does not exist at all (argparse rejects it, exit 2).
    """
    path = tmp / "f.jsonl"
    (fid,) = seed(path)
    for flag in ("--state", "--decided", "--published", "--id-", "--found"):
        code, _, err = run(["--file", str(path), "set", "--id", fid, flag, "published"])
        check(code == 2, f"`set {flag}` was ACCEPTED (exit {code}) — it must not be a flag at all: {err!r}")
    check(state_of(path, fid) == "candidate", "a rejected `set` moved the state anyway")
    check("state" not in EDITABLE, "`state` is EDITABLE — `set --state published` would skip the user")

    # …and the prose fields ARE editable (the rule is targeted, not a blanket freeze).
    code, out, err = run(["--file", str(path), "set", "--id", fid, "--evidence", "PR #9, review 2"])
    check(code == 0, f"set --evidence exited {code}: {err!r}")
    check(json.loads(out)["evidence"] == "PR #9, review 2", f"set did not write the field: {out!r}")


def t_transition_graph(tmp: Path) -> None:
    """EVERY transition is checked against the graph — DERIVED from TRANSITIONS, never a retyped list.

    For each (command, state) pair the graph does not allow, the command must be REFUSED and the state
    must not move. A new state or edge is covered the moment it is added to `TRANSITIONS`: this fixture
    reads the graph rather than restating it, so it cannot go stale behind it.
    """
    for cmd, (frm, to) in TRANSITIONS.items():
        for state in STATES:
            path = tmp / f"{cmd}-{state}.jsonl"
            write_lines(path, entry_line(id="fu1", state=state))
            extra = ["--ref", "#1"] if cmd == "publish" else []
            code, _, err = run(["--file", str(path), cmd, "--id", "fu1", *extra])
            if state in frm:
                check(code == 0, f"`{cmd}` was refused from the ALLOWED state {state!r}: {err!r}")
                check(state_of(path, "fu1") == to, f"`{cmd}` from {state!r} did not reach {to!r}")
            else:
                check(code == 1, f"`{cmd}` was ACCEPTED from {state!r}, which the graph forbids (exit {code})")
                check(state_of(path, "fu1") == state,
                      f"a refused `{cmd}` still moved {state!r} to {state_of(path, 'fu1')!r}")

    # A TERMINAL state is terminal: nothing in the graph leaves `rejected` or `done`.
    for state in ("rejected", "done"):
        check(not any(state in frm for frm, _ in TRANSITIONS.values()),
              f"{state!r} is not terminal — some transition applies to it")


def t_ruling_is_recorded(tmp: Path) -> None:
    """The USER'S RULING is stamped into `decided`; the driver's own bookkeeping is NOT.

    A ruling that is not durable gets re-asked by the next wake — a fresh agent never saw the
    conversation. It is the same reason the ledger's `api_approval` records `approved@<iso>` rather than
    living in the driver's head. `publish`/`done` are the driver's own steps and must NOT stamp it: a
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
    run(["--file", str(path), "publish", "--id", a, "--ref", "#77"])
    after = json.loads(run(["--file", str(path), "get", "--id", a])[1])
    check(after["decided"] == "2026-07-14T09:00:00Z",
          f"`publish` overwrote the USER's ruling timestamp: {after!r}")
    check(after["published"] == "#77", f"publish did not record WHERE: {after!r}")
    check(set(USER_RULINGS) == {"accept", "reject"},
          "USER_RULINGS changed — the transitions that stamp `decided` must be the user's, and only those")


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
    """
    path = tmp / "f.jsonl"
    for missing in REQUIRED:
        argv = ["--file", str(path), "add"]
        for f in REQUIRED:
            if f != missing:
                argv += [f"--{f.replace('_', '-')}", "x"]
        code, _, err = run(argv)
        check(code == 2, f"add without --{missing} was ACCEPTED (exit {code}): {err!r}")
    for blank in ("", "   ", "\t"):
        code, _, err = run(["--file", str(path), "add", "--title", "t", "--evidence", blank,
                            "--deferred-why", "w"])
        check(code == 1, f"add with a BLANK evidence ({blank!r}) was ACCEPTED (exit {code})")
        check("rumor" in err, f"add failed for the wrong reason: {err!r}")
    check(load(path) == [], "a REFUSED add still wrote an entry to the store")


def t_ids_are_assigned_and_never_reused(tmp: Path) -> None:
    """`id` is assigned by the STORE (`fu<N>`, one past the highest) — never by the caller, never reused.

    A reused id silently re-points every reference to the old entry — an audit file, a PR body, the user's
    own note — at a DIFFERENT follow-up. So it counts past the highest id, not the entry count: delete
    `fu2` of three and the next add must still be `fu4`.
    """
    path = tmp / "f.jsonl"
    check(seed(path, 3) == ["fu1", "fu2", "fu3"], "ids are not sequential fu<N>")

    write_lines(path, entry_line(id="fu1"), entry_line(id="fu3"))  # fu2 deleted by hand
    check(seed(path)[0] == "fu4", "an id was REUSED after an entry was removed")

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
    table as something no transition can move, and no reader could tell what it means.
    """
    for name, line, needle in (
        ("bad-json", "{not json", "malformed JSON"),
        ("not-object", '["followup"]', "not a JSON object"),
        ("unknown-type", json.dumps({"type": "note", "id": "fu1"}), "unknown record type"),
        ("unknown-state", entry_line(id="fu1", state="approved"), "unknown state"),
    ):
        p = write_lines(tmp / f"{name}.jsonl", line)
        code, _, err = run(["--file", str(p), "list"])
        check(code == 1, f"[{name}] a corrupt store was ACCEPTED (exit {code})")
        check(needle in err, f"[{name}] failed for the wrong reason: {err!r}")

    # A MISSING file, though, is an empty store — the first follow-up must not need a bootstrap step.
    code, out, err = run(["--file", str(tmp / "nope.jsonl"), "list"])
    check((code, out) == (0, ""), f"a missing store is not an empty one: {code} {out!r} {err!r}")


def t_defaults_backfill(tmp: Path) -> None:
    """An entry written BEFORE a field existed still reads back complete — every absent field defaults.

    This is what lets a field be added to the schema without migrating a store that cannot be rebuilt.
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
                *(entry_line(id=f"fu{i}", state="done") for i in range(1, closed + 1)),
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
    closed_only = write_lines(tmp / "closed.jsonl", entry_line(id="fu1", state="done"),
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
    ("user-step-unskippable", "a CANDIDATE can never be published or closed — no edge bypasses `accept`", t_user_step_is_unskippable),
    ("state-not-settable", "`set` cannot write `state` — only the transitions move it", t_state_is_not_settable),
    ("transition-graph", "every transition is checked against TRANSITIONS; terminal states are terminal", t_transition_graph),
    ("ruling-recorded", "the USER's ruling is stamped durably; the driver's own steps never stamp it", t_ruling_is_recorded),
    ("publish-needs-ref", "a published follow-up must name WHERE", t_publish_needs_a_ref),
    ("evidence-required", "a follow-up with no evidence is a RUMOR — `add` refuses it", t_evidence_is_required),
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

    # The transitions — the ONLY things that move `state`. Each validates the state it comes FROM, so
    # `accept` (the user's agreement) cannot be routed around.
    for cmd, (frm, to) in TRANSITIONS.items():
        who = "THE USER agrees" if cmd in USER_RULINGS else "the driver records"
        t = sub.add_parser(cmd, help=f"{who}: {'/'.join(frm)} -> {to}")
        t.add_argument("--id", required=True)
        if cmd == "publish":
            t.add_argument("--ref", required=True, help="where it was published (issue/PR ref or URL)")
        if cmd in USER_RULINGS:
            t.add_argument("--at", help="ISO timestamp of the user's ruling (default: now)")

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
