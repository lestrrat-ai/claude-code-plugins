#!/usr/bin/env python3
# ci: pyright
"""Fixtures for `lease.py` — the check-and-set, the heartbeat precondition, and the refusals.

They live in a SIBLING file, and `lease.py self-test` FAILS LOUDLY if it cannot load them.

EVERY FIXTURE MUST PIN A RULE — it must go red if its rule is deleted or weakened. The rules worth the
most here are the ones the PROSE never defined, because nobody was watching them: a malformed lease read
as "absent" (which ADOPTS A LIVE RUN), a `release` that deletes someone else's lease, and clock skew
manufacturing an adoption. Each of those is a two-driver bug, and each was reachable from the instructions
as written.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

from _gauntlet.modules import load_module_from_path

OWNER = Path(__file__).resolve().parent / "lease.py"


def _load_owner():
    mod = load_module_from_path("lease_owner", OWNER)
    if mod is None:
        raise RuntimeError(f"cannot load the lease accessor at {OWNER}")
    return mod


L = _load_owner()


# --- helpers ------------------------------------------------------------------

def lease_path(work: Path) -> Path:
    return work / "lease.json"


def put(work: Path, rec, *, raw: "str | None" = None) -> Path:
    p = lease_path(work)
    p.write_text(raw if raw is not None else json.dumps(rec) + "\n", encoding="utf-8")
    return p


def acquire(work: Path, **kw):
    argv = ["--file", str(lease_path(work)), "acquire"]
    for flag, val in (("--token", kw.get("token")), ("--heartbeat-id", kw.get("heartbeat"))):
        if val is not None:
            argv += [flag, val]
    if kw.get("takeover"):
        argv.append("--allow-takeover")
    return L.run(argv)


def run_before_lease_access(argv: "list[str]"):
    """A missing precondition must refuse before both the claim lock and lease read."""
    original_lock = L.claim_lock
    original_read = L.read_lease

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a precondition refusal reached the claim lock or lease read")

    setattr(L, "claim_lock", forbidden)
    setattr(L, "read_lease", forbidden)
    try:
        return L.run(argv)
    finally:
        setattr(L, "claim_lock", original_lock)
        setattr(L, "read_lease", original_read)


def assert_precondition_refusal(code: int, out: str, err: str, path: Path, before: bytes,
                                command: str) -> None:
    """Pin the common no-read/no-write contract without replacing command-specific guidance checks."""
    L.check(code == L.EXIT_REFUSED,
            f"{command} with a missing precondition must exit {L.EXIT_REFUSED}, got {code}")
    L.check(out == "", f"{command}'s precondition refusal must print no stdout, got {out!r}")
    L.check("ownership was NOT checked" in err,
            f"{command}'s precondition refusal must say it made no ownership decision")
    L.check("UNDRIVEN" not in err,
            f"{command}'s precondition refusal must not invent an ownership state")
    L.check(path.read_bytes() == before,
            f"{command}'s precondition refusal must leave the lease BYTE-UNCHANGED")
    L.check(not (path.parent / L.LOCK_NAME).exists(),
            f"{command}'s precondition refusal must stop before taking the claim lock")


def verdict_of(out: str) -> str:
    return json.loads(out)["verdict"]


# --- the heartbeat precondition -----------------------------------------------

def t_no_heartbeat_refuses(work: Path) -> None:
    """No proof stops before ownership is checked, even when the caller already owns a live lease."""
    p = put(work, {"agent": "t1", "heartbeat": "w0", "updated": L.now()})
    before = p.read_bytes()
    code, out, err = run_before_lease_access(
        ["--file", str(p), "acquire", "--token", "t1"])
    assert_precondition_refusal(code, out, err, p, before, "acquire without --heartbeat-id")
    L.check("--heartbeat-id" in err and "ALREADY armed" in err,
            "the refusal must say the proof names something ALREADY armed, not something intended")
    L.check("IN THIS ORDER" in err, "the refusal must teach the order (arm, THEN acquire) — an "
                                    "instruction, not a diagnosis")
    L.check("keep the token you already have" in err and
            "acquire --token <tok> --heartbeat-id <proof>" in err,
            "the missing-proof refusal must preserve the existing token and name the exact acquire retry")
    L.check("runtime-adapter" in err, "the refusal must point at the doc that owns the host mapping")
    for host_specific in ("ScheduleWakeup", "CronCreate", "bounded wait"):
        L.check(host_specific not in err,
                f"the refusal must name NO host mechanism ({host_specific!r} leaked) — that is the "
                f"cross-host violation AGENTS.md forbids")


def t_no_heartbeat_refuses_on_an_absent_lease(work: Path) -> None:
    """The easy way to reintroduce the bug: check the proof only on the `owned` path.

    A fresh run is the case with NO heartbeat yet — exactly the one that must not sail through.
    """
    L.check(not lease_path(work).exists(), "fixture precondition: no lease yet")
    code, _out, _err = acquire(work, token="t1")
    L.check(code != 0, "an ABSENT lease must refuse without a proof too — a fresh run is the case with no "
                       "heartbeat armed yet, so checking only the `owned` path defeats the whole door")


def t_empty_heartbeat_is_not_a_proof(work: Path) -> None:
    """Not trimmed into existence — same identifier discipline as review-pass.py."""
    for blank in ("", "   ", "\t\n"):
        code, _out, _err = acquire(work, token="t1", heartbeat=blank)
        L.check(code != 0, f"a whitespace-only proof ({blank!r}) must REFUSE, never be trimmed into a value")
        L.check(not lease_path(work).exists(), "a refused acquire must write NOTHING")


def t_argparse_must_not_steal_the_refusal(work: Path) -> None:
    """The message IS the mechanism, so argparse must not answer first.

    If --heartbeat-id were argparse-`required`, the caller would get "the following arguments are required"
    — the right exit code and NONE of the instruction. The mechanism would be gone while the tests that
    only assert `code != 0` stayed green.
    """
    _code, _out, err = acquire(work, token="t1")
    L.check("the following arguments are required" not in err,
            "argparse must NOT own this refusal — the instruction is the mechanism, not the exit code")
    L.check(err.startswith("lease: REFUSED"), "the refusal must be OUR message")


def t_no_acquire_preconditions_give_complete_recovery(work: Path) -> None:
    """Neither acquire input still yields complete recovery in one refusal."""
    p = put(work, {"agent": "t1", "heartbeat": "w0", "updated": L.now()})
    before = p.read_bytes()
    code, out, err = run_before_lease_access(["--file", str(p), "acquire"])
    assert_precondition_refusal(code, out, err, p, before,
                                "acquire without --token or --heartbeat-id")
    L.check("requires both --token and --heartbeat-id" in err,
            "the combined refusal must identify both missing inputs")
    L.check("if YOU already hold this run" in err and "recover YOUR OWN token" in err and
            "Do NOT mint a replacement" in err,
            "a current owner must recover its own token without minting a replacement")
    L.check("NEVER from `lease.json` or `lease.py read`" in err,
            "the combined refusal must reject run-scoped token sources")
    L.check("including when adopting an absent" in err and "or stale run" in err and
            "lease.py mint" in err,
            "an absent or stale-run adopter must mint its token")
    L.check("Arm a new heartbeat for this run with that token" in err and
            "runtime-adapter.md" in err and "record the id for that arming as the proof" in err,
            "every caller must be told how to obtain a newly armed heartbeat proof")
    L.check("acquire --token <tok> --heartbeat-id <proof>" in err,
            "the combined refusal must name the exact retry with both values")


def t_no_token_refuses_with_caller_scoped_recovery(work: Path) -> None:
    """A missing token must distinguish owner recovery from stale-run adoption."""
    p = put(work, {
        "agent": "previous-owner-token",
        "heartbeat": "w0",
        "updated": L.now() - L.LEASE_STALE_AFTER - 1,
    })
    before = p.read_bytes()
    code, out, err = run_before_lease_access(
        ["--file", str(p), "acquire", "--heartbeat-id", "hb-1"])
    assert_precondition_refusal(code, out, err, p, before, "acquire without --token")
    L.check("if YOU already hold this run" in err and "YOUR OWN token" in err,
            "owner recovery must be scoped to the caller, not a token found in run state")
    L.check("session or from the heartbeat prompt" in err,
            "owner recovery must name caller-scoped sources for the token")
    L.check("NEVER from `lease.json` or `lease.py read`" in err,
            "owner recovery must reject run-scoped sources that may identify the previous holder")
    L.check("adopting an absent or stale run" in err and "lease.py mint" in err,
            "a stale-run adopter without its own token must be routed through mint")
    L.check("do NOT mint a replacement" in err,
            "a current owner must be told to recover its own token, never mint a replacement")
    L.check("acquire --token <tok> --heartbeat-id <proof>" in err,
            "the acquire refusal must name the exact retry")


def t_refresh_without_token_refuses_before_ownership_check(work: Path) -> None:
    """Refresh recovers the heartbeat's owner token; it never mints or changes commands."""
    p = put(work, {"agent": "t1", "heartbeat": "w0", "updated": L.now()})
    before = p.read_bytes()
    code, out, err = run_before_lease_access(["--file", str(p), "refresh"])
    assert_precondition_refusal(code, out, err, p, before, "refresh without --token")
    L.check("refresh --token <tok>" in err and "heartbeat or owner session" in err,
            "refresh must say where to recover the token and name the exact retry")
    L.check("Do NOT mint a replacement or switch to acquire" in err,
            "refresh must not redirect a missing-token owner into a different ownership path")


def t_release_without_token_refuses_before_ownership_check(work: Path) -> None:
    """Release recovers the exact owner token or leaves the lease untouched."""
    p = put(work, {"agent": "t1", "heartbeat": "w0", "updated": L.now()})
    before = p.read_bytes()
    code, out, err = run_before_lease_access(["--file", str(p), "release"])
    assert_precondition_refusal(code, out, err, p, before, "release without --token")
    L.check("release --token <tok>" in err and "your own token from your session" in err,
            "release must name the caller-scoped token source and the exact retry")
    L.check("do NOT delete or alter the lease" in err,
            "release without the owner token must say to leave the lease untouched")


# --- the proof is recorded, never interpreted ---------------------------------

def t_proof_is_stored_verbatim(work: Path) -> None:
    """The tool never parses, normalizes, or validates the proof's content."""
    hostile = "  heartbeat id/with spaces --and-dashes é中 $(echo no) `echo no`  "
    code, out, _err = acquire(work, token="t1", heartbeat=hostile)
    L.check(code == 0, "a proof with odd bytes is still a proof — the tool does not inspect it")
    L.check(json.loads(out)["heartbeat"] == hostile, "the proof must round-trip VERBATIM")
    L.check(json.loads(lease_path(work).read_text())["heartbeat"] == hostile,
            "the proof must be stored VERBATIM on disk")


def t_a_new_proof_is_not_a_generation_mismatch(work: Path) -> None:
    """Pins the REJECTED design out: `heartbeat` is a record, not a generation to match.

    An earlier draft compared the presented proof to the stored one and stood down on a mismatch. It could
    never have worked: a heartbeat has two things to say ("I am H1" / "I armed H2") and one flag to say them in.
    """
    acquire(work, token="t1", heartbeat="hb-1")
    code, out, _err = acquire(work, token="t1", heartbeat="hb-2")
    L.check(code == 0, "re-acquiring with a DIFFERENT proof is normal — each heartbeat arms its own successor")
    L.check(verdict_of(out) == "owned", "same token on a fresh lease is `owned`, whatever the proof says")
    L.check(json.loads(lease_path(work).read_text())["heartbeat"] == "hb-2",
            "the lease records the LATEST proof")


# --- ownership: the decision table --------------------------------------------

def t_absent_is_adopted(work: Path) -> None:
    code, out, _err = acquire(work, token="t1", heartbeat="w1")
    L.check(code == 0 and verdict_of(out) == "adopted", "an absent lease is adoptable")
    L.check(json.loads(lease_path(work).read_text())["agent"] == "t1", "the token must land in the lease")


def t_fresh_and_mine_is_owned(work: Path) -> None:
    put(work, {"agent": "t1", "heartbeat": "w0", "updated": L.now()})
    code, out, _err = acquire(work, token="t1", heartbeat="w1")
    L.check(code == 0 and verdict_of(out) == "owned", "my own fresh lease is `owned`, and refreshes")


def t_fresh_and_theirs_is_superseded(work: Path) -> None:
    rec = {"agent": "other", "heartbeat": "w0", "updated": L.now()}
    p = put(work, rec)
    before = p.read_bytes()
    code, out, err = acquire(work, token="mine", heartbeat="w1")
    L.check(code != 0 and verdict_of(out) == "superseded", "a live lease held by ANOTHER agent refuses")
    L.check(p.read_bytes() == before, "a superseded acquire must leave the lease BYTE-UNCHANGED")
    L.check("not the driver" in err, "the refusal must tell the loser to stand down")


def t_takeover_is_explicit(work: Path) -> None:
    put(work, {"agent": "other", "heartbeat": "w0", "updated": L.now()})
    code, out, _err = acquire(work, token="mine", heartbeat="w1", takeover=True)
    L.check(code == 0 and verdict_of(out) == "adopted", "--allow-takeover adopts a live run")
    L.check(json.loads(lease_path(work).read_text())["agent"] == "mine", "takeover writes the new token")


def t_stale_is_adopted(work: Path) -> None:
    put(work, {"agent": "dead", "heartbeat": "w0", "updated": L.now() - L.LEASE_STALE_AFTER - 1})
    code, out, _err = acquire(work, token="mine", heartbeat="w1")
    L.check(code == 0 and verdict_of(out) == "adopted", "a lease past LEASE_STALE_AFTER has a dead driver")


def t_just_inside_the_window_is_not_stale(work: Path) -> None:
    put(work, {"agent": "busy", "heartbeat": "w0", "updated": L.now() - L.LEASE_STALE_AFTER + 5})
    code, out, _err = acquire(work, token="mine", heartbeat="w1")
    L.check(code != 0 and verdict_of(out) == "superseded",
            "a lease INSIDE the window is a BUSY driver, not a dead one — staleness must not be off by a "
            "boundary and steal a live run")


def t_future_updated_is_fresh_not_stale(work: Path) -> None:
    """Clock skew must never manufacture an adoption — that is the two-driver bug."""
    put(work, {"agent": "other", "heartbeat": "w0", "updated": L.now() + 10_000})
    code, out, _err = acquire(work, token="mine", heartbeat="w1")
    L.check(code != 0 and verdict_of(out) == "superseded",
            "a FUTURE `updated` must read FRESH — a skewed clock must not hand us someone's live run")


def t_exact_boundary_is_not_stale(work: Path) -> None:
    """A lease at age EXACTLY LEASE_STALE_AFTER is NOT stale — the boundary is `>`, never `>=`.

    `>` and `>=` differ ONLY at exact equality, which wall-clock timing can never hit deterministically, so
    a frozen clock is the only way to pin it. `is_stale`'s docstring says "never widen `>` to `>=`"; this
    makes that regression go red. At the boundary the owner is BUSY, not dead, so a different token must be
    superseded — widening to `>=` would adopt a live run.
    """
    frozen = 2_000_000_000
    put(work, {"agent": "other", "heartbeat": "w0", "updated": frozen - L.LEASE_STALE_AFTER})
    p = lease_path(work)
    before = p.read_bytes()
    original = L.now
    setattr(L, "now", lambda: frozen)  # freeze the clock so age is EXACTLY the window
    try:
        code, out, _err = acquire(work, token="mine", heartbeat="w1")
    finally:
        setattr(L, "now", original)
    L.check(code != 0 and verdict_of(out) == "superseded",
            "age == LEASE_STALE_AFTER is a BUSY owner, not a dead one — `>=` would flip it to `adopted`")
    L.check(p.read_bytes() == before, "a superseded acquire at the boundary must leave the lease unchanged")


# --- corrupt is not absent ----------------------------------------------------

def t_malformed_is_corrupt_never_adopted(work: Path) -> None:
    """The prose said "absent or stale". It never said what an unparseable lease is.

    A reader that lets "cannot parse" fall through to "absent" ADOPTS A LIVE RUN.
    """
    bad = [
        ("empty", ""),
        ("blank", "   \n"),
        ("truncated", '{"agent": "t1", "upda'),
        ("array", '["agent", "t1"]'),
        ("string", '"just a string"'),
        ("no-agent", '{"heartbeat": "w0", "updated": 1}'),
        ("blank-agent", '{"agent": "   ", "updated": 1}'),
        ("agent-not-string", '{"agent": 7, "updated": 1}'),
        ("updated-string", '{"agent": "t1", "updated": "recently"}'),
        ("updated-bool", '{"agent": "t1", "updated": true}'),
        ("no-updated", '{"agent": "t1", "heartbeat": "w0"}'),
    ]
    for name, raw in bad:
        p = put(work, None, raw=raw)
        before = p.read_bytes()
        code, _out, err = acquire(work, token="mine", heartbeat="w1")
        L.check(code != 0, f"a {name} lease must REFUSE — it is CORRUPT, and corrupt is not absent")
        L.check(p.read_bytes() == before, f"a {name} lease must not be overwritten")
        L.check("two agents" in err or "cannot" in err.lower(),
                f"the {name} refusal must explain why we will not guess")


def t_legacy_lease_with_no_heartbeat_is_accepted(work: Path) -> None:
    """A prose-era lease with NO `heartbeat` field is a VALID held lease, never corrupt — interop.

    `read_lease` requires only `agent` and `updated`; `heartbeat` is OPTIONAL. A driver that hand-rolled
    its lease from the prose (an older cache, a Codex session) wrote no `heartbeat`. Reading such a lease
    as corrupt would refuse to see a LIVE legacy driver and let this tool adopt on top of it — the exact
    double-drive this tool exists to prevent, in reverse. Contrast `t_malformed_is_corrupt`'s `no-updated`
    case: a missing `updated` IS corrupt (we cannot tell staleness); a missing `heartbeat` is NOT.

    The clock is FROZEN to `updated` so the legacy lease reads FRESH regardless of the wall-clock date —
    the outcome is anchored to the frozen clock, never to a stamp being in the future.

    TEETH: add a "reject a lease with no heartbeat" check to read_lease and this goes red — read_lease
    raises Corrupt, and the same-token acquire/refresh stop returning `owned`.
    """
    frozen = 2_000_000_000
    rec = {"agent": "legacy-driver", "updated": frozen}
    p = put(work, rec)
    got = L.read_lease(p)
    L.check(got is not None and got.get("agent") == "legacy-driver",
            "a legacy lease with no `heartbeat` must read as a VALID held lease, never None/Corrupt")
    L.check(got is not None and "heartbeat" not in got, "read_lease must not invent a `heartbeat` the legacy lease never had")
    original = L.now
    setattr(L, "now", lambda: frozen)  # freeze the clock so the legacy lease reads FRESH (age 0), independent of wall-clock date
    try:
        code, out, _err = acquire(work, token="legacy-driver", heartbeat="w1")
        L.check(code == 0 and verdict_of(out) == "owned",
                "a same-token acquire over a heartbeat-less legacy lease must be `owned`, not refused")
        rcode, rout, _ = L.run(["--file", str(p), "refresh", "--token", "legacy-driver"])
        L.check(rcode == 0 and verdict_of(rout) == "owned",
                "a plain refresh of a heartbeat-less legacy lease by its own token must be `owned`")
    finally:
        setattr(L, "now", original)


def t_read_reports_corrupt_without_deciding(work: Path) -> None:
    put(work, None, raw="{oops")
    code, out, _err = L.run(["--file", str(lease_path(work)), "read"])
    L.check(code != 0 and verdict_of(out) == "corrupt", "`read` reports corruption rather than guessing")


def t_duplicate_json_key_is_corrupt(work: Path) -> None:
    """A lease that repeats a key reads two ways across parsers — reject it, never guess.

    `json.loads` keeps the LAST duplicate, so `{"agent":"other","agent":"mine"}` resolves to `mine`; a
    driver whose parser keeps the FIRST reads `other`. That cross-parser disagreement over WHO is driving
    is a two-driver seam, so a duplicate key is `corrupt`, the same fail-closed direction as every other
    unreadable lease. If this regresses (the hook dropped), the last-wins `agent` matches and the lease is
    silently adopted / released.
    """
    for name, raw in (
        ("dup-agent", '{"agent": "other", "agent": "mine", "heartbeat": "w0", "updated": 1}'),
        ("dup-updated", '{"agent": "t1", "updated": 1, "updated": 2}'),
    ):
        p = put(work, None, raw=raw)
        before = p.read_bytes()
        code, _out, err = acquire(work, token="mine", heartbeat="w1")
        L.check(code != 0, f"a {name} lease must REFUSE — a duplicate key is corrupt, not a value to guess")
        L.check(p.read_bytes() == before, f"a {name} lease must not be overwritten")
        L.check("cannot" in err.lower() or "two" in err.lower(),
                f"the {name} refusal must explain why we will not guess who is driving")
        rcode, rout, _ = L.run(["--file", str(p), "read"])
        L.check(rcode != 0 and verdict_of(rout) == "corrupt", f"`read` must report a {name} lease corrupt")
        dcode, _dout, _derr = L.run(["--file", str(p), "release", "--token", "mine"])
        L.check(dcode != 0 and p.exists(), f"release must not delete a {name} lease it cannot trust")


# --- corrupt at the DECODE/PARSE boundary, not just per-field -----------------

def t_invalid_utf8_is_corrupt(work: Path) -> None:
    """Undecodable bytes must FAIL CLOSED, not crash with a traceback.

    `read_text` raises `UnicodeDecodeError`, a `ValueError` subclass and NOT an `OSError`, so before the
    decode-boundary catch it escaped `except OSError` and `read` printed a raw traceback while `acquire`
    skipped its Corrupt refusal entirely. A lease we cannot even decode is corrupt, never absent.
    """
    p = lease_path(work)
    p.write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    before = p.read_bytes()
    code, _out, err = acquire(work, token="mine", heartbeat="w1")
    L.check(code != 0, "a lease with invalid UTF-8 must REFUSE — undecodable bytes are corrupt, not absent")
    L.check(p.read_bytes() == before, "an undecodable lease must not be overwritten")
    L.check(err.startswith("lease: REFUSED"),
            "acquire must emit its own REFUSED message, not let a raw UnicodeDecodeError escape")
    L.check("Traceback" not in err and "UnicodeDecodeError" not in err,
            "the tool must fail closed on undecodable bytes, never crash with a traceback")
    rcode, rout, _ = L.run(["--file", str(p), "read"])
    L.check(rcode != 0 and verdict_of(rout) == "corrupt", "`read` must report undecodable bytes as corrupt")


def t_non_finite_is_corrupt(work: Path) -> None:
    """`json.loads` accepts NaN/Infinity/-Infinity by default; the parse boundary must reject them.

    The per-field checks only catch a non-finite `agent`/`updated`. Anywhere else — a preserved unknown
    field, `heartbeat`, a nested object — a non-finite value slips past, reads `held`/acquires `owned`, and
    round-trips to disk as INVALID strict JSON. Each `agent`/`updated` here is well-formed, so ONLY the
    `parse_constant` reject makes these corrupt; without it acquire would adopt the stale lease (exit 0).
    """
    cases = [
        ("heartbeat-nan", '{"agent": "t1", "heartbeat": NaN, "updated": 1}'),
        ("unknown-infinity", '{"agent": "t1", "updated": 1, "extra": Infinity}'),
        ("nested-neg-infinity", '{"agent": "t1", "updated": 1, "meta": {"drift": -Infinity}}'),
    ]
    for name, raw in cases:
        p = put(work, None, raw=raw)
        before = p.read_bytes()
        code, _out, _err = acquire(work, token="mine", heartbeat="w1")
        L.check(code != 0,
                f"a {name} lease must REFUSE — a non-finite JSON constant is not strict JSON, so corrupt")
        L.check(p.read_bytes() == before, f"a {name} lease must not be overwritten")
        rcode, rout, _ = L.run(["--file", str(p), "read"])
        L.check(rcode != 0 and verdict_of(rout) == "corrupt", f"`read` must report a {name} lease corrupt")


def t_lease_path_is_a_directory_is_corrupt(work: Path) -> None:
    """An OS-level read failure must FAIL CLOSED as Corrupt, never fall open to absent/None.

    Make the lease path unreadable in the most direct way a real filesystem allows: create a DIRECTORY
    where `lease.json` belongs. `path.read_text` then raises `IsADirectoryError` — an `OSError`, so it is
    NOT `FileNotFoundError` (absent) and NOT `UnicodeDecodeError` (the decode boundary). It lands in
    read_lease's generic `except OSError` branch, which must raise Corrupt. If that branch is changed to
    return None (fail OPEN), a live run gets adopted on top of an OS-level read failure.

    TEETH: change read_lease's `except OSError` branch to return None and this goes red — read_lease
    stops raising Corrupt, `read` reports absent, and acquire adopts instead of refusing.
    """
    p = lease_path(work)
    p.mkdir()  # a directory at the lease path -> read_text raises IsADirectoryError (an OSError)
    raised = False
    try:
        L.read_lease(p)
    except L.Corrupt:
        raised = True
    L.check(raised, "read_lease must raise Corrupt on an OS-level read error (a directory at the lease "
                    "path), never return None/absent through the generic `except OSError` branch")
    rcode, rout, _ = L.run(["--file", str(p), "read"])
    L.check(rcode != 0 and verdict_of(rout) == "corrupt",
            "`read` must report an unreadable lease path as corrupt, not absent")
    code, _out, _err = acquire(work, token="mine", heartbeat="w1")
    L.check(code != 0, "acquire must REFUSE when the lease path cannot be read, never adopt on top of it")


def t_write_lease_never_writes_non_finite(work: Path) -> None:
    """What we write must be STRICT JSON, and `write_lease` must refuse to serialize a non-finite value.

    `allow_nan=False` is the guard that stops the tool putting on disk exactly what `read_lease` now
    refuses to read back — a lease that would strand the run behind a corruption of our own making.
    """
    code, _out, _err = acquire(work, token="t1", heartbeat="w1")
    L.check(code == 0, "precondition: a normal acquire succeeds")
    content = lease_path(work).read_text()

    def _boom(v):
        raise AssertionError(f"the lease on disk holds a non-finite constant {v!r} — not strict JSON")

    json.loads(content, parse_constant=_boom)  # raises if disk holds NaN/Infinity/-Infinity
    L.check("NaN" not in content and "Infinity" not in content,
            "the lease written to disk must be strict JSON — no NaN/Infinity tokens")

    raised = False
    try:
        L.write_lease(lease_path(work),
                      {"agent": "t1", "heartbeat": "w1", "updated": L.now(), "drift": float("inf")})
    except ValueError:
        raised = True
    L.check(raised, "write_lease must REFUSE to serialize a non-finite number (allow_nan=False) — the tool "
                    "must never itself put NaN/Infinity on disk")


# --- mode: refresh/acquire must not narrow the lease's permissions ------------

def t_refresh_preserves_file_mode(work: Path) -> None:
    """`replace_text` writes through a 0600 mkstemp temp, so a rewrite would NARROW 0664 -> 0600.

    write_lease preserves an existing lease's prior mode. Without it, every refresh silently tightens the
    lease's permissions away from what the run dir was set up with.
    """
    p = put(work, {"agent": "t1", "heartbeat": "w0", "updated": L.now() - 100})
    os.chmod(p, 0o664)
    code, _out, _err = L.run(["--file", str(p), "refresh", "--token", "t1"])
    L.check(code == 0, "refreshing my own lease succeeds")
    mode = stat.S_IMODE(p.stat().st_mode)
    L.check(mode == 0o664,
            f"refresh must PRESERVE the lease's prior mode (0664), got {oct(mode)} — a rewrite must not "
            f"narrow it to mkstemp's private 0600")


def t_new_lease_uses_umask_permissions(work: Path) -> None:
    """A brand-new lease gets umask-adjusted create perms, NOT mkstemp's private 0600.

    umask is pinned for the duration so the expected mode is deterministic regardless of the environment.
    """
    old = os.umask(0o022)
    try:
        code, _out, _err = acquire(work, token="t1", heartbeat="w1")
    finally:
        os.umask(old)
    L.check(code == 0, "an absent lease is adoptable")
    mode = stat.S_IMODE(lease_path(work).stat().st_mode)
    L.check(mode == 0o644,
            f"a NEW lease must get umask-adjusted create perms (0644 under umask 022), got {oct(mode)} — "
            f"not mkstemp's private 0600")


# --- release: the token check the prose forgot --------------------------------

def t_release_refuses_someone_elses_lease(work: Path) -> None:
    """"Delete lease.json on normal exit", followed literally by a superseded driver, hands away a live run."""
    p = put(work, {"agent": "other", "heartbeat": "w0", "updated": L.now()})
    before = p.read_bytes()
    code, out, err = L.run(["--file", str(p), "release", "--token", "mine"])
    L.check(code != 0 and verdict_of(out) == "superseded", "release must refuse a lease we do not own")
    L.check(p.exists() and p.read_bytes() == before, "a refused release must leave the lease INTACT")
    L.check("DIFFERENT agent" in err, "the refusal must say whose lease it is")


def t_release_deletes_my_own(work: Path) -> None:
    p = put(work, {"agent": "mine", "heartbeat": "w0", "updated": L.now()})
    code, out, _err = L.run(["--file", str(p), "release", "--token", "mine"])
    L.check(code == 0 and verdict_of(out) == "released", "release with the right token releases")
    L.check(not p.exists(), "a released lease is gone, so the finished run shows no driver")


def t_release_of_a_corrupt_lease_refuses(work: Path) -> None:
    p = put(work, None, raw="{oops")
    code, _out, _err = L.run(["--file", str(p), "release", "--token", "mine"])
    L.check(code != 0, "refuse to delete a lease we cannot read — it may belong to a live driver")
    L.check(p.exists(), "the unreadable lease must survive for a human to inspect")


def t_release_of_an_absent_lease_refuses(work: Path) -> None:
    """Release fails CLOSED on an absent lease — symmetric with refresh and the Purpose line.

    A release proves ownership by matching the token in the lease. With no lease present there is nothing
    to match, so it cannot confirm this caller ever owned the run: it must refuse, not exit 0. (This
    reverses an earlier idempotency choice.)
    """
    p = lease_path(work)
    L.check(not p.exists(), "fixture precondition: no lease")
    code, out, err = L.run(["--file", str(p), "release", "--token", "mine"])
    L.check(code != 0, "an absent lease must REFUSE release — there is no token to match against")
    L.check(verdict_of(out) == "absent", "the verdict still names the absent state")
    L.check(not p.exists(), "a refused release must conjure nothing")
    L.check("no lease" in err.lower() and "match" in err.lower(),
            "the refusal must say there is no lease to match the token against")


# --- refresh ------------------------------------------------------------------

def t_refresh_bumps_and_preserves(work: Path) -> None:
    old = L.now() - 100
    put(work, {"agent": "t1", "heartbeat": "w0", "updated": old, "note": "unknown-field"})
    code, _out, _err = L.run(["--file", str(lease_path(work)), "refresh", "--token", "t1"])
    L.check(code == 0, "refreshing my own lease succeeds")
    rec = json.loads(lease_path(work).read_text())
    L.check(rec["updated"] > old, "refresh must bump `updated` — it is the liveness proof")
    L.check(rec["heartbeat"] == "w0", "refresh does NOT re-arm, so it must preserve the proof")
    L.check(rec["note"] == "unknown-field", "a field this version does not know must survive the write")


def t_refresh_refuses_when_superseded(work: Path) -> None:
    """The case that matters: the owner changed while this driver was slow."""
    put(work, {"agent": "other", "heartbeat": "w0", "updated": L.now()})
    code, out, err = L.run(["--file", str(lease_path(work)), "refresh", "--token", "mine"])
    L.check(code != 0 and verdict_of(out) == "superseded", "refresh must refuse once superseded")
    L.check("Stand down" in err, "a superseded driver must be told to stop")


def t_refresh_of_an_absent_lease_does_not_recreate_it(work: Path) -> None:
    code, _out, err = L.run(["--file", str(lease_path(work)), "refresh", "--token", "t1"])
    L.check(code != 0, "refreshing a GONE lease must refuse, not silently re-create it")
    L.check(not lease_path(work).exists(), "refresh must not conjure a lease")
    L.check("GONE" in err, "the refusal must say the lease vanished, which is a bug worth seeing")


# --- the lock -----------------------------------------------------------------

def t_a_held_lock_blocks(work: Path) -> None:
    (work / L.LOCK_NAME).mkdir()
    code, _out, err = acquire(work, token="t1", heartbeat="w1")
    L.check(code != 0, "a held claim.lock must block the check-and-set")
    L.check("held" in err, "the refusal must say the lock is held")


def t_lock_name_is_the_literal_claim_lock(work: Path) -> None:
    """The lock name must be the LITERAL string "claim.lock" — the interop contract with prose drivers.

    An older, prose-driven driver serializes the check-and-set by `mkdir claim.lock`. If this tool locked
    under any OTHER name it would NOT mutually exclude that driver: both would "win" and drive one run.
    So the name is asserted as a LITERAL here, deliberately NOT derived from `L.LOCK_NAME` — deriving the
    fixture from the constant is exactly how a rename slips past (both sides move together and the suite
    stays green), which is why the existing `t_a_held_lock_blocks` (built from `L.LOCK_NAME`) cannot catch
    it.

    TEETH: change LOCK_NAME to anything but "claim.lock" and this goes red twice — the literal constant
    check fails, and a hand-rolled `mkdir claim.lock` no longer blocks this tool's acquire.
    """
    L.check(L.LOCK_NAME == "claim.lock",
            f"the lock name must be the literal 'claim.lock' for interop with prose `mkdir claim.lock` "
            f"drivers, got {L.LOCK_NAME!r}")
    (work / "claim.lock").mkdir()  # a prose driver's literal lock — NOT built from L.LOCK_NAME
    code, _out, err = acquire(work, token="t1", heartbeat="w1")
    L.check(code != 0, "a literal `claim.lock` held by a prose driver must block this tool's check-and-set")
    L.check("held" in err, "the refusal must say the lock is held")


def t_claim_lock_sweep_stays_on_the_lease_scale(work: Path) -> None:
    """A short sweep timeout re-opens the clock-skew mutual-exclusion hole.

    The claim.lock is a bare `mkdir` with no PID, so mtime is the ONLY cross-driver signal and the sweep
    threshold is the only lever against a forward clock jump sweeping a LIVE sub-second claim. If a future
    edit lowers it back toward the critical-section scale (the old `5 * 60`), a modest forward jump could
    sweep a live lock and let a second driver in — two owners of one run. Pin it to the lease-staleness
    scale so that regression goes RED here.
    """
    L.check(L.CLAIM_LOCK_STALE_AFTER >= L.LEASE_STALE_AFTER,
            f"CLAIM_LOCK_STALE_AFTER ({L.CLAIM_LOCK_STALE_AFTER}s) must be >= LEASE_STALE_AFTER "
            f"({L.LEASE_STALE_AFTER}s): a shorter claim-lock sweep re-opens the clock-skew two-driver hole")


def t_a_stale_lock_is_swept(work: Path) -> None:
    lock = work / L.LOCK_NAME
    lock.mkdir()
    old = time.time() - L.CLAIM_LOCK_STALE_AFTER - 60
    os.utime(lock, (old, old))
    code, _out, _err = acquire(work, token="t1", heartbeat="w1")
    L.check(code == 0, "a claim.lock left by a process that died mid-claim must be swept, or the run "
                       "wedges forever")


def t_the_lock_is_released_on_the_refusal_paths(work: Path) -> None:
    """A lock leaked on an error path wedges the run as surely as never sweeping it."""
    put(work, None, raw="{oops")
    acquire(work, token="t1", heartbeat="w1")  # refuses: corrupt
    L.check(not (work / L.LOCK_NAME).exists(), "the lock must be released after a CORRUPT refusal")
    put(work, {"agent": "other", "heartbeat": "w0", "updated": L.now()})
    acquire(work, token="mine", heartbeat="w1")  # refuses: superseded
    L.check(not (work / L.LOCK_NAME).exists(), "the lock must be released after a SUPERSEDED refusal")


def t_only_one_of_two_racing_claimers_wins(work: Path) -> None:
    """Real processes, real lock. The property the whole file exists for.

    TEETH: this must FAIL if the critical section is unlocked, not merely if nobody wins. Several racers
    are released together against an ABSENT lease by a shared barrier file. Under a real lock they
    SERIALIZE: exactly one reads absent and adopts (owning the run AS ITSELF, exit 0); every other reads
    that fresh lease and stands down (`superseded`/`lost-race`). Unlock the read/decide/write (e.g. move
    `claim_lock`'s rmdir before the yield) and two or more racers read absent before anyone writes, each
    commits its OWN token and reads it back — TWO self-believed owners of one run. The check below counts
    self-owners and demands exactly one, so that regression goes red; several rounds make the interleave
    reliable to catch.
    """
    p = lease_path(work)
    barrier = work / "go"
    prog = (
        "import sys, os, time;"
        f"sys.path.insert(0, {str(OWNER.parent)!r});"
        "from _gauntlet.modules import load_module_from_path;"
        f"m = load_module_from_path('lease_race', {str(OWNER)!r});"
        f"b = {str(barrier)!r};"
        "\nwhile not os.path.exists(b): time.sleep(0.002)\n"
        f"raise SystemExit(m.main(['--file', {str(p)!r}, 'acquire', '--token', sys.argv[1],"
        " '--heartbeat-id', 'w']))"
    )
    tokens = [f"racer-{i}" for i in range(5)]
    for _round in range(4):
        if p.exists():
            p.unlink()
        if barrier.exists():
            barrier.unlink()
        procs = [subprocess.Popen([sys.executable, "-c", prog, tok],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                 for tok in tokens]
        time.sleep(0.15)          # let every racer reach its spin loop before...
        barrier.touch()           # ...releasing them as simultaneously as the OS allows
        outs = [(tok, pr.communicate()[0].decode()) for tok, pr in zip(tokens, procs)]

        L.check(p.exists(), "one of the racers must have taken the lease")
        winner = json.loads(p.read_text())["agent"]
        L.check(winner in tokens, f"the lease must name a real racer, got {winner!r}")
        L.check(not (work / L.LOCK_NAME).exists(), "neither racer may leak the lock")

        self_owners = []
        for tok, out in outs:
            try:
                rec = json.loads(out)
            except json.JSONDecodeError:
                continue
            # "I own this run AS MYSELF" = a fresh acquire that wrote and read back my own token.
            if rec.get("verdict") in ("owned", "adopted") and rec.get("token") == tok:
                self_owners.append(tok)
        L.check(len(self_owners) == 1,
                f"EXACTLY ONE racer may own the run as itself; got {self_owners!r}. Two or more means the "
                f"read/decide/write was NOT serialized — the lock is broken and both drivers believe they "
                f"own the run.")
        L.check(self_owners == [winner],
                f"the self-believed owner {self_owners!r} must be the token actually in the lease {winner!r}")


def t_lost_race_readback_refuses(work: Path) -> None:
    """Post-write read-back: our write landed, but ANOTHER token is on disk when we read back — stand down.

    The `race` fixture above pins the LOCK: real processes serialize through the `mkdir` claim, so the
    post-write read-back always sees our own token there and the lost-race branch is never reached. That
    fixture proves the lock works; it does NOT prove the guard behind it. This one does. It constructs the
    exact state the read-back exists to catch — the cases the `mkdir` lock cannot cover (a forward-skew
    sweep of a live claim, or a hand-rolled non-interoperating lock letting a second writer in): our write
    lands, then a competitor stamps a DIFFERENT token before we read back. `write_lease` is wrapped to do
    that once, so no real concurrency is needed.

    With the read-back/lost-race block present -> `lost-race`, exit 1, we stand down. Delete that block
    (emit our own `fresh` record without reading back) and this goes RED: `acquire` returns `adopted`,
    exit 0, claiming ownership while `other` sits on disk — two self-believed drivers of one run.
    """
    p = lease_path(work)
    L.check(not p.exists(), "fixture precondition: absent lease")
    original_write = L.write_lease

    def racing_write(path, rec):
        original_write(path, rec)  # our real write lands (agent="mine")...
        # ...then a competitor stamps a DIFFERENT token before acquire reads back. Only the first write
        # races; restore immediately so nothing else is disturbed.
        setattr(L, "write_lease", original_write)
        original_write(path, {"agent": "other", "heartbeat": "w0", "updated": L.now()})

    setattr(L, "write_lease", racing_write)
    try:
        code, out, err = acquire(work, token="mine", heartbeat="w1")
    finally:
        setattr(L, "write_lease", original_write)
    L.check(code != 0 and verdict_of(out) == "lost-race",
            "our write landed but the read-back shows ANOTHER token — the guard must return `lost-race` and "
            "refuse, never claim ownership; without the read-back acquire falsely reports `adopted`/exit 0")
    L.check(json.loads(p.read_text())["agent"] == "other",
            "the competitor's token must remain on disk — the loser must not overwrite it")
    L.check("lost the race" in err.lower() and "stand down" in err.lower(),
            "the refusal must tell the loser it lost the race and to stand down")


CASES = [
    ("no-heartbeat", "no proof of arming, no ownership check — the entire mechanism",
     t_no_heartbeat_refuses),
    ("no-heartbeat-absent", "an ABSENT lease refuses too — a fresh run has no heartbeat yet",
     t_no_heartbeat_refuses_on_an_absent_lease),
    ("blank-heartbeat", "a whitespace proof is refused, never trimmed into a value",
     t_empty_heartbeat_is_not_a_proof),
    ("refusal-is-ours", "argparse must not steal the refusal — the instruction IS the mechanism",
     t_argparse_must_not_steal_the_refusal),
    ("no-acquire-preconditions", "neither acquire input gets complete recovery in one refusal",
     t_no_acquire_preconditions_give_complete_recovery),
    ("no-token", "no token, no ownership check — owner recovery and stale adoption stay separate",
     t_no_token_refuses_with_caller_scoped_recovery),
    ("refresh-no-token", "refresh without a token checks no ownership and preserves the lease",
     t_refresh_without_token_refuses_before_ownership_check),
    ("release-no-token", "release without a token checks no ownership and preserves the lease",
     t_release_without_token_refuses_before_ownership_check),
    ("proof-verbatim", "the proof is recorded, never parsed or normalized", t_proof_is_stored_verbatim),
    ("proof-not-a-generation", "a new proof is a record, not a generation mismatch",
     t_a_new_proof_is_not_a_generation_mismatch),
    ("absent-adopted", "an absent lease is adoptable", t_absent_is_adopted),
    ("fresh-mine-owned", "my own fresh lease is owned", t_fresh_and_mine_is_owned),
    ("fresh-theirs-refused", "a live lease held by another agent refuses, byte-unchanged",
     t_fresh_and_theirs_is_superseded),
    ("takeover-explicit", "--allow-takeover adopts a live run, and only then", t_takeover_is_explicit),
    ("stale-adopted", "a lease past the window has a dead driver", t_stale_is_adopted),
    ("inside-window-busy", "a lease inside the window is BUSY, not dead", t_just_inside_the_window_is_not_stale),
    ("future-is-fresh", "clock skew must not manufacture an adoption", t_future_updated_is_fresh_not_stale),
    ("exact-boundary-fresh", "age == LEASE_STALE_AFTER is NOT stale — the boundary is `>`, not `>=`",
     t_exact_boundary_is_not_stale),
    ("corrupt-not-absent", "an unreadable lease is CORRUPT — adopting it would double-drive a live run",
     t_malformed_is_corrupt_never_adopted),
    ("legacy-no-heartbeat", "a prose-era lease with no `heartbeat` field is a VALID held lease — interop",
     t_legacy_lease_with_no_heartbeat_is_accepted),
    ("read-reports-corrupt", "`read` reports corruption rather than guessing",
     t_read_reports_corrupt_without_deciding),
    ("duplicate-key-corrupt", "a duplicate JSON key reads two ways — corrupt, never guessed",
     t_duplicate_json_key_is_corrupt),
    ("invalid-utf8-corrupt", "undecodable bytes fail closed to corrupt, never crash", t_invalid_utf8_is_corrupt),
    ("non-finite-corrupt", "NaN/Infinity anywhere is corrupt, not a value to round-trip",
     t_non_finite_is_corrupt),
    ("lease-is-a-directory-corrupt", "an OS read error (a directory at the lease path) fails closed to "
     "corrupt", t_lease_path_is_a_directory_is_corrupt),
    ("strict-json-on-disk", "write_lease writes strict JSON and refuses a non-finite value",
     t_write_lease_never_writes_non_finite),
    ("refresh-preserves-mode", "refresh preserves the lease's prior mode, never narrows to 0600",
     t_refresh_preserves_file_mode),
    ("new-lease-umask-mode", "a new lease gets umask-adjusted perms, not mkstemp's 0600",
     t_new_lease_uses_umask_permissions),
    ("release-refuses-theirs", "release refuses a lease we do not own — the prose's missing check",
     t_release_refuses_someone_elses_lease),
    ("release-mine", "release with the right token releases", t_release_deletes_my_own),
    ("release-corrupt", "refuse to delete a lease we cannot read", t_release_of_a_corrupt_lease_refuses),
    ("release-absent", "release fails closed on an absent lease — no token to match",
     t_release_of_an_absent_lease_refuses),
    ("refresh-bumps", "refresh bumps, preserves the proof, and keeps unknown fields",
     t_refresh_bumps_and_preserves),
    ("refresh-superseded", "refresh refuses once superseded", t_refresh_refuses_when_superseded),
    ("refresh-absent", "refresh does not conjure a lease", t_refresh_of_an_absent_lease_does_not_recreate_it),
    ("lock-blocks", "a held claim.lock blocks the check-and-set", t_a_held_lock_blocks),
    ("lock-name-literal", "the lock name is the LITERAL 'claim.lock' — interop with prose `mkdir` drivers",
     t_lock_name_is_the_literal_claim_lock),
    ("lock-sweep-scale", "a short claim-lock sweep re-opens the clock-skew mutual-exclusion hole",
     t_claim_lock_sweep_stays_on_the_lease_scale),
    ("lock-swept", "a lock from a crashed claim is swept", t_a_stale_lock_is_swept),
    ("lock-released", "the lock is released on the refusal paths", t_the_lock_is_released_on_the_refusal_paths),
    ("race", "two real racing claimers, one lease", t_only_one_of_two_racing_claimers_wins),
    ("lost-race-readback", "the post-write read-back refuses when a competitor's token won the disk",
     t_lost_race_readback_refuses),
]
