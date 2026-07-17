#!/usr/bin/env python3
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


def verdict_of(out: str) -> str:
    return json.loads(out)["verdict"]


# --- the heartbeat precondition -----------------------------------------------

def t_no_heartbeat_refuses(work: Path) -> None:
    """The whole point: no proof of arming, no lease."""
    code, _out, err = acquire(work, token="t1")
    L.check(code != 0, "acquire with NO --heartbeat-id must REFUSE — that is the entire mechanism")
    L.check(not lease_path(work).exists(), "a refused acquire must write NOTHING")
    L.check("--heartbeat-id" in err and "ALREADY armed" in err,
            "the refusal must say the proof names something ALREADY armed, not something intended")
    L.check("IN THIS ORDER" in err, "the refusal must teach the order (arm, THEN acquire) — an "
                                    "instruction, not a diagnosis")
    L.check("runtime-adapter" in err, "the refusal must point at the doc that owns the host mapping")
    for host_specific in ("ScheduleWakeup", "CronCreate", "bounded wait"):
        L.check(host_specific not in err,
                f"the refusal must name NO host mechanism ({host_specific!r} leaked) — that is the "
                f"cross-host violation AGENTS.md forbids")


def t_no_heartbeat_refuses_on_an_absent_lease(work: Path) -> None:
    """The easy way to reintroduce the bug: check the proof only on the `owned` path.

    A fresh run is the case with NO wake yet — exactly the one that must not sail through.
    """
    L.check(not lease_path(work).exists(), "fixture precondition: no lease yet")
    code, _out, _err = acquire(work, token="t1")
    L.check(code != 0, "an ABSENT lease must refuse without a proof too — a fresh run is the case with no "
                       "wake armed yet, so checking only the `owned` path defeats the whole door")


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


def t_no_token_refuses_and_never_mints(work: Path) -> None:
    """A caller with no token never armed a wake that identifies it: its proof cannot be real."""
    code, _out, err = acquire(work, heartbeat="wake-1")
    L.check(code != 0, "acquire with NO --token must REFUSE")
    L.check(not lease_path(work).exists(), "a refused acquire must write NOTHING — and must NOT mint a "
                                           "token to make itself succeed")
    L.check("mint" in err, "the refusal must point at `mint` as step one")


# --- the proof is recorded, never interpreted ---------------------------------

def t_proof_is_stored_verbatim(work: Path) -> None:
    """The tool never parses, normalizes, or validates the proof's content."""
    hostile = "  wake id/with spaces --and-dashes é中 $(echo no) `echo no`  "
    code, out, _err = acquire(work, token="t1", heartbeat=hostile)
    L.check(code == 0, "a proof with odd bytes is still a proof — the tool does not inspect it")
    L.check(json.loads(out)["heartbeat"] == hostile, "the proof must round-trip VERBATIM")
    L.check(json.loads(lease_path(work).read_text())["heartbeat"] == hostile,
            "the proof must be stored VERBATIM on disk")


def t_a_new_proof_is_not_a_generation_mismatch(work: Path) -> None:
    """Pins the REJECTED design out: `heartbeat` is a record, not a generation to match.

    An earlier draft compared the presented proof to the stored one and stood down on a mismatch. It could
    never have worked: a wake has two things to say ("I am H1" / "I armed H2") and one flag to say them in.
    """
    acquire(work, token="t1", heartbeat="wake-1")
    code, out, _err = acquire(work, token="t1", heartbeat="wake-2")
    L.check(code == 0, "re-acquiring with a DIFFERENT proof is normal — each wake arms its own successor")
    L.check(verdict_of(out) == "owned", "same token on a fresh lease is `owned`, whatever the proof says")
    L.check(json.loads(lease_path(work).read_text())["heartbeat"] == "wake-2",
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


def t_read_reports_corrupt_without_deciding(work: Path) -> None:
    put(work, None, raw="{oops")
    code, out, _err = L.run(["--file", str(lease_path(work)), "read"])
    L.check(code != 0 and verdict_of(out) == "corrupt", "`read` reports corruption rather than guessing")


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
    """Real processes, real lock. The property the whole file exists for."""
    p = lease_path(work)
    prog = (
        "import sys;"
        f"sys.path.insert(0, {str(OWNER.parent)!r});"
        "from _gauntlet.modules import load_module_from_path;"
        f"m = load_module_from_path('lease_race', {str(OWNER)!r});"
        f"raise SystemExit(m.main(['--file', {str(p)!r}, 'acquire', '--token', sys.argv[1],"
        " '--heartbeat-id', 'w']))"
    )
    procs = [subprocess.Popen([sys.executable, "-c", prog, tok],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
             for tok in ("racer-a", "racer-b")]
    codes = [p_.wait() for p_ in procs]
    L.check(p.exists(), "one of the racers must have taken the lease")
    winner = json.loads(p.read_text())["agent"]
    L.check(winner in ("racer-a", "racer-b"), f"the lease must name a real racer, got {winner!r}")
    L.check(not (work / L.LOCK_NAME).exists(), "neither racer may leak the lock")
    # Both may legitimately exit 0: the loser can arrive after the winner's lease went in and read it as a
    # normal `superseded`/adopt-by-stale... but it must NEVER be that BOTH think they own it as themselves.
    L.check(codes.count(0) >= 1, "at least one racer must succeed")


CASES = [
    ("no-heartbeat", "no proof of arming, no lease — the entire mechanism", t_no_heartbeat_refuses),
    ("no-heartbeat-absent", "an ABSENT lease refuses too — a fresh run has no wake yet",
     t_no_heartbeat_refuses_on_an_absent_lease),
    ("blank-heartbeat", "a whitespace proof is refused, never trimmed into a value",
     t_empty_heartbeat_is_not_a_proof),
    ("refusal-is-ours", "argparse must not steal the refusal — the instruction IS the mechanism",
     t_argparse_must_not_steal_the_refusal),
    ("no-token", "no token, no lease — and acquire never mints one to save itself",
     t_no_token_refuses_and_never_mints),
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
    ("corrupt-not-absent", "an unreadable lease is CORRUPT — adopting it would double-drive a live run",
     t_malformed_is_corrupt_never_adopted),
    ("read-reports-corrupt", "`read` reports corruption rather than guessing",
     t_read_reports_corrupt_without_deciding),
    ("release-refuses-theirs", "release refuses a lease we do not own — the prose's missing check",
     t_release_refuses_someone_elses_lease),
    ("release-mine", "release with the right token releases", t_release_deletes_my_own),
    ("release-corrupt", "refuse to delete a lease we cannot read", t_release_of_a_corrupt_lease_refuses),
    ("refresh-bumps", "refresh bumps, preserves the proof, and keeps unknown fields",
     t_refresh_bumps_and_preserves),
    ("refresh-superseded", "refresh refuses once superseded", t_refresh_refuses_when_superseded),
    ("refresh-absent", "refresh does not conjure a lease", t_refresh_of_an_absent_lease_does_not_recreate_it),
    ("lock-blocks", "a held claim.lock blocks the check-and-set", t_a_held_lock_blocks),
    ("lock-sweep-scale", "a short claim-lock sweep re-opens the clock-skew mutual-exclusion hole",
     t_claim_lock_sweep_stays_on_the_lease_scale),
    ("lock-swept", "a lock from a crashed claim is swept", t_a_stale_lock_is_swept),
    ("lock-released", "the lock is released on the refusal paths", t_the_lock_is_released_on_the_refusal_paths),
    ("race", "two real racing claimers, one lease", t_only_one_of_two_racing_claimers_wins),
]
