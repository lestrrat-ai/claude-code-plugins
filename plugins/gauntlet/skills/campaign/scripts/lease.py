#!/usr/bin/env python3
"""Schema-owning accessor for the campaign run lease (lease.json).

The lease is what stops two agents driving one run — `run-identity-and-lease.md` calls that "the bug this
guards against". It was also the ONLY durable store in the campaign with no schema owner: `state.jsonl` has
`ledger.py`, `followups.jsonl` has `followups.py`, the review artifacts have `review-pass.py`, CI snapshots
have `ci-snapshot.py`. The lease had four reference docs and an `echo`.

And it is not a value-write — it is a CHECK-AND-SET: take a lock, read, decide, write, read back, unlock,
plus a stale-lock sweep and a staleness rule. Hand-rolled from prose, on every heartbeat, by a fresh agent
instance that remembers nothing. In run `g260717-1748-d76f0acb` a driver hand-rolled it with
`mkdir`/`echo`/`rmdir`, eyeballed the read-back instead of comparing tokens, and skipped the
stale-lock sweep entirely. It was safe only because the lease was absent and nobody else was driving.

Four refusals this file exists to make, each one a thing the prose left undefined or a well-meaning driver
would otherwise do:

1. **A MALFORMED lease is `corrupt`, never `adopt`.** The prose once said only adopt when the lease is
   "absent or stale", leaving unparseable undefined. A reader that lets "cannot parse" fall through to
   "absent" ADOPTS A LIVE RUN. The asymmetry decides it: wrongly adopting gives two drivers on one ledger;
   wrongly refusing an orphan stalls a run, which staleness and a human both recover.
2. **`release` refuses unless the token matches.** The prose once said "delete lease.json on normal
   exit" — a superseded driver following that literally deletes the LIVE OWNER'S lease.
3. **No `--heartbeat-id`, no lease.** The caller must hand over its proof that it has ALREADY armed the
   heartbeat. This tool never inspects the proof and takes the caller's word for it — which is exactly why
   it must name something already done. Arming was step 6 of the heartbeat skeleton, after all the interesting
   work, when an agent believes it is finished; it was skipped for an entire session and nothing noticed.
   Requiring it here moves the failure from "forgot at the end", which nothing catches, to "cannot start".
4. **No `--token`, no lease** — and `acquire` never mints one. The heartbeat carries `--token`, so a caller
   without one demonstrably never armed a heartbeat that identifies it: its proof cannot be real.

A future `updated` (clock skew) reads as FRESH, not stale — fail closed, same direction as (1).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

from _gauntlet.atomic import replace_text
from _gauntlet.modules import load_module_from_path
from _gauntlet.testing import capture_cli

DESCRIPTION = "Schema-owning accessor for the campaign run lease (lease.json)."

SIBLING = Path(__file__).resolve().parent / "lease-test.py"

# --- scope: a single shared, roughly-monotonic system clock -------------------
#
# This lease assumes ONE system clock, shared by every driver of a given run. Concurrent drivers are
# same-machine — two sessions, or a resume of the same run — reading the same `time.time()`. That is the
# whole threat model, and the user has ruled it so.
#
# A cross-host deployment where two INDEPENDENT, unsynchronized clocks disagree by more than the staleness
# window is OUT OF SCOPE, and cannot be made sound here: the claim lock is a bare `mkdir` with no PID (kept
# that way for interop, see `claim_lock`), so mtime is the ONLY cross-driver liveness signal, and a live
# owner whose clock is behind is indistinguishable ON DISK from a dead one. No threshold separates them. It
# is a declared non-goal, not a defect to fix — do NOT add clock-skew heuristics chasing it.
#
# On the ONE shared clock, the residual is BOUNDED but not zero. `is_stale`'s signed `>` reads a backward
# step / future `updated` as FRESH (never manufacturing an adoption), and `cmd_acquire` samples the clock
# exactly once so a step mid-decision cannot split one record two ways. What remains is a large FORWARD step
# on that shared clock landing between an owner's refreshes (or inside a claim's sub-second critical
# section): CLAIM_LOCK_STALE_AFTER's 30-min scale means only an implausibly large forward step could sweep a
# live claim. This tool does NOT claim complete mutual exclusion under an arbitrarily misbehaving clock — it
# bounds the shared-clock hazards it can, and names the cross-host one it cannot.
#
# --- constants: ONE defining site ---------------------------------------------
#
# `references/run-identity-and-lease.md` NAMES `LEASE_STALE_AFTER` instead of restating the number, and
# `references/critical-rules.md` defers to it. Keep it that way: a prose copy of the value is a second
# source of truth for one constant, which is what this repo's own rule forbids.
#
#   LEASE_STALE_AFTER      a lease older than this has a DEAD driver, so the run may be adopted. Long
#                          enough that a busy driver is never mistaken for a dead one.
#   CLAIM_LOCK_STALE_AFTER a claim.lock older than this is swept as abandoned by a process that died
#                          mid-claim. The prose said "a few minutes", which is not a number and cannot be
#                          implemented — but the number matters more than it looks, because mtime is the
#                          ONLY cross-driver signal here. The lock is a bare `mkdir claim.lock` kept
#                          DELIBERATELY (see `claim_lock`) for interop with drivers that hand-roll it from
#                          prose, and a hand-rolled lock carries no PID — so a liveness/PID check, the only
#                          COMPLETE fix for a stale-sweep racing a live claim, is unavailable. The
#                          threshold is the only lever left.
#
#                          If the system clock jumps FORWARD by more than this while a live driver is
#                          inside its sub-second critical section (a read plus a write), a second driver
#                          would sweep the LIVE lock and enter too, and the read-back does NOT catch a
#                          serialized both-succeeded pair — TWO owners of one run. Setting this to the
#                          lease-staleness SCALE (30 min, same magnitude as LEASE_STALE_AFTER, deliberately
#                          — they are independent facts that happen to share it, NOT one aliased to the
#                          other) means only an implausibly large forward clock jump could sweep a live
#                          claim, while a genuinely crashed claim is still recovered on the same timescale
#                          the lease already tolerates a dead driver. This MITIGATES the clock-skew
#                          two-driver hole; it does NOT eliminate it. The residual risk is a forward clock
#                          jump larger than 30 min landing inside a sub-second window — small, not zero.
LEASE_STALE_AFTER = 30 * 60
CLAIM_LOCK_STALE_AFTER = 30 * 60

LOCK_NAME = "claim.lock"
FIELDS = ("agent", "heartbeat", "updated")

EXIT_OK = 0
EXIT_REFUSED = 1


def fail(msg: str) -> NoReturn:
    print(msg, file=sys.stderr)
    raise SystemExit(EXIT_REFUSED)


def now() -> int:
    return int(time.time())


def mint_token() -> str:
    return secrets.token_hex(4)


# --- the store ----------------------------------------------------------------

class Corrupt(Exception):
    """The lease exists and cannot be trusted. NEVER treated as absent."""


def _reject_duplicate_keys(pairs: "list[tuple[str, object]]") -> dict:
    """`object_pairs_hook` that raises on a repeated member name, at any object depth.

    `json.loads` silently keeps the LAST of duplicate keys, so `{"agent":"other","agent":"mine"}` resolves
    to `mine` — while a driver hand-rolling a parser that keeps the FIRST reads `other`. That cross-parser
    disagreement is a two-driver seam, so a duplicate-keyed lease is Corrupt, not a value to guess at.
    """
    seen: set = set()
    for key, _val in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r}")
        seen.add(key)
    return dict(pairs)


def _reject_non_finite(constant: str) -> NoReturn:
    """`parse_constant` hook: reject the non-finite constants `json.loads` accepts by DEFAULT.

    `json.loads` parses `NaN`, `Infinity`, and `-Infinity` anywhere a number may appear — a preserved
    unknown field, a nested object, anything past the per-field checks. They are NOT strict JSON: a driver
    hand-rolling a parser may reject them, and `json.dumps` cannot even re-serialize them without
    `allow_nan=True`, so such a value round-trips to disk as INVALID JSON. A lease we cannot round-trip
    cannot be trusted, so we refuse it at the PARSE boundary — the same fail-closed direction as bad bytes
    or bad JSON, closing the whole "unparseable" class rather than the two example inputs.
    """
    raise ValueError(f"non-finite JSON constant {constant!r}")


def read_lease(path: Path) -> "dict | None":
    """Return the lease, or None if ABSENT. Raise Corrupt if present and untrustworthy.

    Absent and corrupt are DIFFERENT ANSWERS and must never collapse: absent means nobody is driving,
    corrupt means we cannot tell. Only the first permits adoption.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        # A `UnicodeDecodeError` is a `ValueError` subclass, NOT an `OSError`, so it would otherwise escape
        # the `except OSError` below and crash `read`/`acquire` with a raw traceback instead of failing
        # closed. Undecodable bytes are the same fail-closed direction as bad JSON: a lease we cannot even
        # decode cannot tell us who is driving, so it is Corrupt, never 'absent'.
        raise Corrupt(f"{path} is not valid UTF-8 ({exc}) — a lease we cannot decode cannot tell us who is "
                      f"driving; refusing to read that as 'absent'") from exc
    except OSError as exc:
        raise Corrupt(f"cannot read {path}: {exc}") from exc
    if not raw.strip():
        raise Corrupt(f"{path} is empty — a driver may hold this run; refusing to read that as 'absent'")
    try:
        rec = json.loads(raw, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_non_finite)
    except json.JSONDecodeError as exc:
        raise Corrupt(f"{path} is not valid JSON ({exc}) — refusing to read that as 'absent'") from exc
    except ValueError as exc:
        # Both `_reject_duplicate_keys` (a repeated member name) and `_reject_non_finite`
        # (NaN/Infinity/-Infinity anywhere) raise a plain ValueError; JSONDecodeError, a ValueError
        # subclass, is handled above. Either way the bytes are not a strict JSON object we can trust: a
        # lease two parsers could read differently — or that is not strict JSON at all — cannot tell us
        # who is driving.
        raise Corrupt(f"{path} has a {exc} — a lease that two parsers could read differently, or that is "
                      f"not strict JSON, cannot tell us who is driving; refusing to read that as 'absent'"
                      ) from exc
    if not isinstance(rec, dict):
        raise Corrupt(f"{path} holds {type(rec).__name__}, not a JSON object")
    agent = rec.get("agent")
    if not isinstance(agent, str) or not agent.strip():
        raise Corrupt(f"{path} has no usable `agent` — cannot tell who is driving this run")
    updated = rec.get("updated")
    # This int-only check is what keeps a huge-exponent float out of every DECISION. `parse_constant` above
    # catches the NaN/Infinity/-Infinity TOKENS, but not `1e10000`, which `parse_float` silently turns into
    # `inf`. `updated` is the ONLY numeric field any decision reads, and it refuses every float here (finite
    # or `inf`), so no non-finite value can reach a staleness decision. A `1e10000` can still SURVIVE in an
    # opaque field (`heartbeat`, recorded verbatim and never inspected) and, if the owner later `refresh`es
    # (which preserves unknown fields), reach `write_lease`, where `json.dumps(allow_nan=False)` RAISES —
    # the refresh aborts non-zero, but the atomic temp-write never lands, so the on-disk lease is left
    # intact. That noisy abort is NOT guarded to a clean refusal ON PURPOSE: `lease.json` is git-ignored
    # bookkeeping only this run's own driver writes, so a `1e10000` there is the single user's own foot — a
    # documented non-threat (`intent`/fu35), not a malformed lease this tool undertakes to reject cleanly.
    if isinstance(updated, bool) or not isinstance(updated, int):
        raise Corrupt(f"{path} has a non-integer `updated` ({updated!r}) — cannot tell if it is stale")
    return rec


def _current_umask() -> int:
    """Read the process umask. There is no read-only call, so set-to-0 then restore."""
    mask = os.umask(0)
    os.umask(mask)
    return mask


def write_lease(path: Path, rec: dict) -> None:
    """Write the lease atomically, preserving any field this version does not know about.

    Two guards ride on the atomic replace:

    - `allow_nan=False`, so this tool can NEVER itself put a non-finite number (NaN/Infinity) on disk.
      `read_lease` refuses to read one back, so writing one would strand the run behind a corrupt lease of
      our own making. If `rec` ever held one, `json.dumps` raises before anything is written.
    - PERMISSION preservation. `replace_text` writes through a private 0600 `mkstemp` temp, so without this
      every refresh/acquire would NARROW an existing lease's mode (e.g. 0664 -> 0600). An EXISTING lease
      keeps its prior mode; a BRAND-NEW lease gets the umask-adjusted permissions an ordinary create would
      produce. `mode=` chmods the temp BEFORE the rename, so the lease never appears on disk mis-permissioned.
    """
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o666 & ~_current_umask()
    replace_text(path, json.dumps(rec, allow_nan=False) + "\n",
                 temp_prefix=".lease.", encoding="utf-8", mode=mode)


def age_of(rec: dict, now_ts: "int | None" = None) -> int:
    """Seconds since the lease was refreshed, floored at 0 so a skewed clock cannot report a negative age.

    Pass `now_ts` to measure against a clock value sampled ONCE by the caller; omit it for a fresh read.
    A single decision (see `cmd_acquire`) MUST pass one sampled value, so a clock step mid-decision cannot
    split one record into two verdicts.

    The floor is PRESENTATION ONLY — it is `is_stale`'s signed comparison that makes a future `updated`
    read fresh, not this clamp. Do not credit it with more than it does.
    """
    ref = now() if now_ts is None else now_ts
    return max(0, ref - int(rec["updated"]))


def is_stale(rec: dict, now_ts: "int | None" = None) -> bool:
    """A lease is stale only once it is OLDER than the window.

    Pass `now_ts` to decide against a clock value sampled ONCE; omit it for a fresh read. See `age_of`.

    The comparison is deliberately SIGNED and deliberately `>`: a future `updated` (clock skew) yields a
    negative age, which is not greater than the window, so it reads FRESH. That is the fail-closed
    direction and it is the rule doing the work — never `abs()` this, and never widen `>` to `>=`.
    Manufacturing an adoption out of a skewed clock is the one error that puts two drivers on one run.
    """
    return age_of(rec, now_ts) > LEASE_STALE_AFTER


@contextmanager
def claim_lock(path: Path):
    """Serialize the check-and-set, sweeping a lock a crashed claim left behind.

    Kept as `mkdir` DELIBERATELY. Other drivers still hand-roll this lock from the prose (a Codex session
    on a different installed version, an older cache). A tool that locked with O_EXCL or flock would not
    mutually exclude a prose-following driver: both would "win", and this file would CREATE the double-drive
    it exists to prevent while looking more rigorous than what it replaced.
    """
    lock = path.parent / LOCK_NAME
    try:
        if time.time() - lock.stat().st_mtime > CLAIM_LOCK_STALE_AFTER:
            lock.rmdir()  # abandoned by a process that died mid-claim
    except (FileNotFoundError, OSError):
        pass
    try:
        lock.mkdir(parents=False)
    except FileExistsError:
        fail(f"lease: {lock} is held — another agent is claiming this run right now. Retry shortly.")
    except OSError as exc:
        fail(f"lease: cannot create {lock}: {exc}")
    # Record the identity of the EXACT directory this call just created. If the stale-sweep of a LATER
    # process removes our lock and re-creates its own (the documented forward-skew entry hole), the dir
    # named `claim.lock` at cleanup time is a DIFFERENT one — a foreign process's live lock. Removing it
    # would cascade a third driver in. So cleanup rmdirs only when the inode still matches ours. Inode
    # identity needs no marker file inside the dir, so a hand-rolled `rmdir claim.lock` still interoperates.
    try:
        st = os.stat(lock)
        mine = (st.st_dev, st.st_ino)
    except OSError:
        mine = None
    try:
        yield
    finally:
        try:
            st = os.stat(lock)
            if mine is not None and (st.st_dev, st.st_ino) == mine:
                lock.rmdir()
        except OSError:
            pass


def emit(verdict: str, rec: "dict | None", **extra) -> None:
    out: "dict[str, object]" = {"verdict": verdict}
    if rec is not None:
        out.update({
            "agent": rec.get("agent"),
            "heartbeat": rec.get("heartbeat"),
            "updated": rec.get("updated"),
            "age_seconds": age_of(rec) if isinstance(rec.get("updated"), int) else None,
        })
    out["stale_after"] = LEASE_STALE_AFTER
    out.update(extra)
    print(json.dumps(out))


# --- the refusal that carries the contract ------------------------------------
#
# The proof cannot be verified, so the ENFORCEMENT is this refusal and its value is the INSTRUCTION it
# carries. Modelled on `ledger.py verdict` at a review-loop cap, the strongest rule in the campaign: it
# exits non-zero and its stderr says what happened, WHAT STATE CHANGED, what NOT to do, and what to do
# instead. It names no host mechanism — `runtime-adapter.md` owns that mapping — and it offers NO
# alternative, because the moment it names a way to proceed without a proof, that way becomes the default.
#
# This message concerns the ACQUIRE-TIME heartbeat proof (`--heartbeat-id` presented here to take the
# lease), NOT the same-session wake-prompt SHAPE (lean prompt vs. full invocation) owned by
# runtime-adapter.md "Background work and heartbeats". Evidence it does not restate that boundary: the
# text is unchanged by the wake-shape PR (not in its diff), it delegates the host mechanism to
# runtime-adapter.md by name rather than reconstructing it, and heartbeat.py callback — not this string —
# still prints the wake prompt. A driver following this message consults that owner for the shape.

NO_HEARTBEAT = """\
lease: REFUSED — the lease was NOT taken. Nothing was written, and this run is still UNDRIVEN.
lease: acquire requires --heartbeat-id: your PROOF that you have ALREADY armed the heartbeat for this
       run. This tool never inspects it and takes your word for it — which is exactly why it must be
       something you already did, not something you intend to do.
lease: DO THIS, IN THIS ORDER: (1) arm the heartbeat for this run — schedule its wake via your host's
       mechanism (`runtime-adapter.md` owns it, and `heartbeat.py callback` prints the exact wake prompt). The
       wake carries ONLY --run <id> --token <tok>; a resuming heartbeat REFRESHES and needs no proof,
       so --heartbeat-id is NOT part of it. (2) Re-run THIS command with --heartbeat-id <proof> — the id you
       recorded for that arming, presented to acquire here, never carried in the wake.
lease: DO NOT take the lease first and arm afterwards. An arm at the end of a heartbeat is the step that gets
       forgotten — that is the entire reason this door refuses."""

NO_TOKEN = """\
lease: REFUSED — the lease was NOT taken. Nothing was written, and this run is still UNDRIVEN.
lease: acquire requires --token, and it does NOT mint one for you. The heartbeat carries `--token <tok>`, so
       the token must exist BEFORE you arm: a caller without one cannot have armed a heartbeat that identifies
       it, which means its --heartbeat-id cannot name a real heartbeat.
lease: DO THIS: run `lease.py mint` for a token, arm the heartbeat with it, then acquire with both."""


# --- commands -----------------------------------------------------------------

def cmd_mint(_path, _args) -> int:
    print(mint_token())
    return EXIT_OK


def cmd_acquire(path: Path, args) -> int:
    if not (args.token or "").strip():
        fail(NO_TOKEN)
    if not (args.heartbeat_id or "").strip():
        fail(NO_HEARTBEAT)
    token = args.token
    with claim_lock(path):
        # Sample the wall clock EXACTLY ONCE for this whole decision. Reading it again mid-decision let a
        # backward step classify one record as both stale and fresh and fall through to `owned`, overwriting
        # a lease a DIFFERENT token holds. Every staleness branch below reads this one value.
        #
        # No fixture pins this single sample, and none can, because in this single-branch table it changes no
        # (exit, verdict, token) outcome: `owned` is reachable ONLY through the `rec["agent"] == token`
        # equality below, so the feared "fall through to `owned` overwriting a different token" is
        # unreachable however many times the clock is read. Reverting to per-call reads alters only the
        # cosmetic age printed in the `superseded` message — a display number, no behavioral surface. The
        # single sample is cleanliness, not a pinnable rule; check that claim by reverting it and running
        # self-test, which stays all-green.
        acquired_at = now()
        try:
            rec = read_lease(path)
        except Corrupt as exc:
            fail(f"lease: REFUSED — {exc}\n"
                 f"lease: A lease that cannot be parsed is NOT an absent lease. Adopting it could put two "
                 f"agents on one run; refusing only stalls this one. Nothing was written.\n"
                 f"lease: Inspect {path} and, if the run is genuinely orphaned, remove it by hand.")
        # One staleness verdict, computed once off the single `acquired_at` sample, drives every branch.
        # `owned` is therefore reachable ONLY when the record's token is ours; a different-token record is
        # always `superseded` or (with --allow-takeover / staleness) `adopted`, never `owned`.
        if rec is None or is_stale(rec, acquired_at):
            verdict = "adopted"
        elif rec["agent"] != token:
            if not args.allow_takeover:
                emit("superseded", rec, held_by=rec["agent"])
                print(f"lease: REFUSED — a DIFFERENT agent holds this run (lease age "
                      f"{age_of(rec, acquired_at)}s). You are not the driver: do not review, fix, merge, or "
                      f"relabel anything.\n"
                      f"lease: If a human has agreed this run should be taken over, re-run with "
                      f"--allow-takeover.", file=sys.stderr)
                return EXIT_REFUSED
            verdict = "adopted"
        else:
            verdict = "owned"

        fresh = dict(rec) if rec is not None else {}
        fresh.update({"agent": token, "heartbeat": args.heartbeat_id, "updated": now()})
        write_lease(path, fresh)

        # Read back: the write is not the proof. A concurrent claimer may have won.
        try:
            back = read_lease(path)
        except Corrupt as exc:
            fail(f"lease: wrote the lease and could not read it back: {exc}")
        if back is None or back["agent"] != token:
            emit("lost-race", back, expected=token)
            print("lease: REFUSED — another agent's token is in the lease after our write. You lost the "
                  "race and are NOT the driver. Stand down.", file=sys.stderr)
            return EXIT_REFUSED
        emit(verdict, back, token=token)
        return EXIT_OK


def cmd_refresh(path: Path, args) -> int:
    """The heartbeat bump — 'still alive'. It does NOT re-arm, so it takes no proof."""
    if not (args.token or "").strip():
        fail(NO_TOKEN)
    with claim_lock(path):
        try:
            rec = read_lease(path)
        except Corrupt as exc:
            fail(f"lease: REFUSED — {exc}\nlease: Nothing was written.")
        if rec is None:
            fail(f"lease: REFUSED — {path} is GONE. You believed you owned this run and its lease no longer "
                 f"exists; something released or deleted it under you. Nothing was written. Re-acquire "
                 f"explicitly rather than re-creating it here — a silently re-created lease hides that.")
        if rec["agent"] != args.token:
            emit("superseded", rec, held_by=rec["agent"])
            print("lease: REFUSED — a different agent now holds this run. You were superseded while you "
                  "worked. Stand down: do not merge, fix, or relabel anything.", file=sys.stderr)
            return EXIT_REFUSED
        rec["updated"] = now()
        write_lease(path, rec)
        emit("owned", rec)
        return EXIT_OK


def cmd_release(path: Path, args) -> int:
    """Release on normal exit — and ONLY if the token matches.

    The prose once said "delete lease.json". A superseded driver that follows that literally deletes the
    LIVE owner's lease and hands the run to anyone.

    An ABSENT lease fails closed too: with no lease present there is no token to match against, so we
    cannot confirm this caller was ever the owner. Symmetric with `refresh` and with the Purpose line "a
    release refuses unless the token matches".
    """
    if not (args.token or "").strip():
        fail(NO_TOKEN)
    with claim_lock(path):
        try:
            rec = read_lease(path)
        except Corrupt as exc:
            fail(f"lease: REFUSED — {exc}\nlease: Refusing to delete a lease we cannot read. Nothing was "
                 f"written.")
        if rec is None:
            # Release fails closed on an absent lease: with nothing present, no token can be matched.
            emit("absent", None)
            print("lease: REFUSED — there is no lease here to match your token against. Nothing was "
                  "deleted. A release proves ownership by matching the token IN the lease; with no lease "
                  "present there is nothing to match, so it fails closed — the same as refresh.",
                  file=sys.stderr)
            return EXIT_REFUSED
        if rec["agent"] != args.token:
            emit("superseded", rec, held_by=rec["agent"])
            print("lease: REFUSED — this lease belongs to a DIFFERENT agent. Deleting it would hand a live "
                  "run to anyone. Nothing was written.", file=sys.stderr)
            return EXIT_REFUSED
        path.unlink()
        emit("released", rec)
        return EXIT_OK


def cmd_read(path: Path, _args) -> int:
    """ADVISORY status — taken WITHOUT the lock, so it may race a concurrent write.

    Never make an ownership decision from this; that is `acquire`'s job. It exists for cross-run discovery
    bucketing, which reads many leases and decides nothing.
    """
    try:
        rec = read_lease(path)
    except Corrupt as exc:
        emit("corrupt", None, error=str(exc))
        return EXIT_REFUSED
    if rec is None:
        emit("absent", None)
        return EXIT_OK
    emit("stale" if is_stale(rec) else "held", rec)
    return EXIT_OK


# --- self-test ----------------------------------------------------------------

class SelfTestFailure(AssertionError):
    """A rule this file claims to enforce does not hold."""


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise SelfTestFailure(msg)


def run(argv: "list[str]") -> "tuple[int, str, str]":
    """Drive the REAL CLI in-process and capture (exit code, stdout, stderr)."""
    return capture_cli(main, argv)


def sibling_cases() -> list:
    """Load the sibling's fixtures — and FAIL LOUDLY if they are not there.

    A self-test that passes because it found nothing to check reports health while checking nothing.
    """
    if not SIBLING.exists():
        raise SelfTestFailure(
            f"the fixture file {SIBLING} IS MISSING — this suite has no fixtures to run and CANNOT report "
            f"health. Every rule this file enforces is now unpinned."
        )
    mod = load_module_from_path("lease_test", SIBLING, register=True)
    if mod is None:
        raise SelfTestFailure(f"{SIBLING} exists but cannot be loaded as a module")
    cases = getattr(mod, "CASES", None)
    if not cases:
        raise SelfTestFailure(f"{SIBLING} exports no CASES — every rule in this file is unpinned while the "
                              f"suite still exits 0")
    return list(cases)


def self_test() -> int:
    failures = 0
    try:
        cases = sibling_cases()
    except SelfTestFailure as exc:
        print(f"FAIL     {'sibling-fixtures':26} -> the fixtures in {SIBLING.name} must be RUNNABLE\n"
              f"         {exc}")
        print("\n1 check(s) FAILED — the lease's contract is broken.")
        return 1
    with tempfile.TemporaryDirectory() as tmpdir:
        for name, rule, fn in cases:
            work = Path(tmpdir) / name
            work.mkdir()
            try:
                fn(work)
            except SelfTestFailure as exc:
                print(f"FAIL     {name:26} -> {rule}\n         {exc}")
                failures += 1
            except Exception as exc:  # noqa: BLE001 — a fixture that CRASHES has not passed
                print(f"FAIL     {name:26} -> {rule}\n         raised {type(exc).__name__}: {exc}")
                failures += 1
            else:
                print(f"ok       {name:26} -> {rule}")
    print()
    if failures:
        print(f"{failures} check(s) FAILED — the lease's contract is broken.")
        return 1
    print(f"all {len(cases)} fixtures hold — the lease's contract is intact.")
    return 0


# --- cli ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--file", help="path to the run's lease (<rundir>/lease.json)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("mint", help="print a fresh agent token. Step ONE: the token must exist before you arm "
                                "the heartbeat, because the heartbeat carries it")

    # NOTE: --token and --heartbeat-id are deliberately NOT argparse-`required`. They ARE required, and
    # this file refuses without them — but argparse's "the following arguments are required:
    # --heartbeat-id" is a DIAGNOSIS, and the whole mechanism here is the INSTRUCTION the refusal carries
    # (NO_HEARTBEAT / NO_TOKEN). Letting argparse win the race would keep the exit code and throw away the
    # only part that teaches the caller what to do. `lease-test.py` pins this.
    a = sub.add_parser("acquire", help="take or refresh ownership of this run — the door every heartbeat goes "
                                       "through first")
    a.add_argument("--token", help="REQUIRED. Your agent token (from `mint`). NEVER minted here: a caller "
                                   "without one cannot have armed a heartbeat that names it")
    a.add_argument("--heartbeat-id",
                   help="REQUIRED. Your PROOF that you have ALREADY armed the heartbeat. Never inspected — "
                        "taken on your word, which is why it must name something already done. "
                        "`runtime-adapter.md` tells you what your proof is")
    a.add_argument("--allow-takeover", action="store_true",
                   help="adopt a run a DIFFERENT live agent holds. Only after a human has agreed to it")

    r = sub.add_parser("refresh", help="heartbeat the lease ('still alive'). Does not re-arm, takes no proof")
    r.add_argument("--token", help="REQUIRED. Your agent token")

    d = sub.add_parser("release", help="release on normal exit. Refuses unless the token matches")
    d.add_argument("--token", help="REQUIRED. Your agent token")

    sub.add_parser("read", help="ADVISORY read-only status (no lock — may race a write). For discovery "
                                "bucketing; never decide ownership from it")

    sub.add_parser("self-test", help="run every fixture and assert the rules this file enforces still hold")
    return parser


def dispatch(args) -> int:
    if args.cmd == "self-test":
        return self_test()
    if args.cmd == "mint":
        return cmd_mint(None, args)
    if args.file is None:
        build_parser().error("the following arguments are required: --file")
    path = Path(args.file)
    if not path.parent.is_dir():
        fail(f"lease: {path.parent} does not exist — the run directory must be created before its lease")
    return {
        "acquire": cmd_acquire,
        "refresh": cmd_refresh,
        "release": cmd_release,
        "read": cmd_read,
    }[args.cmd](path, args)


def main(argv: "list[str] | None" = None) -> int:
    return dispatch(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
