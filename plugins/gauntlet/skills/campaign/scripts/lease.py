#!/usr/bin/env python3
"""Schema-owning accessor for the campaign run lease (lease.json).

The lease is what stops two agents driving one run — `run-identity-and-lease.md` calls that "the bug this
guards against". It was also the ONLY durable store in the campaign with no schema owner: `state.jsonl` has
`ledger.py`, `followups.jsonl` has `followups.py`, the review artifacts have `review-pass.py`, CI snapshots
have `ci-snapshot.py`. The lease had four reference docs and an `echo`.

And it is not a value-write — it is a CHECK-AND-SET: take a lock, read, decide, write, read back, unlock,
plus a stale-lock sweep and a staleness rule. Hand-rolled from prose, on every wake, by a fresh agent
instance that remembers nothing. In run `g260717-1748-d76f0acb` a driver hand-rolled it with
`mkdir`/`openssl`/`echo`/`rmdir`, eyeballed the read-back instead of comparing tokens, and skipped the
stale-lock sweep entirely. It was safe only because the lease was absent and nobody else was driving.

Four refusals this file exists to make, each one a thing the prose left undefined or a well-meaning driver
would otherwise do:

1. **A MALFORMED lease is `corrupt`, never `adopt`.** The prose says adopt when the lease is "absent or
   stale" and says nothing about unparseable. A reader that lets "cannot parse" fall through to "absent"
   ADOPTS A LIVE RUN. The asymmetry decides it: wrongly adopting gives two drivers on one ledger; wrongly
   refusing an orphan stalls a run, which staleness and a human both recover.
2. **`release` refuses unless the token matches.** The prose says "delete lease.json on normal exit" — a
   superseded driver following that literally deletes the LIVE OWNER'S lease.
3. **No `--heartbeat-id`, no lease.** The caller must hand over its proof that it has ALREADY armed the
   heartbeat. This tool never inspects the proof and takes the caller's word for it — which is exactly why
   it must name something already done. Arming was step 6 of the wake skeleton, after all the interesting
   work, when an agent believes it is finished; it was skipped for an entire session and nothing noticed.
   Requiring it here moves the failure from "forgot at the end", which nothing catches, to "cannot start".
4. **No `--token`, no lease** — and `acquire` never mints one. The wake carries `--token`, so a caller
   without one demonstrably never armed a wake that identifies it: its proof cannot be real.

A future `updated` (clock skew) reads as FRESH, not stale — fail closed, same direction as (1).
"""

from __future__ import annotations

import argparse
import json
import secrets
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

# --- constants: ONE defining site ---------------------------------------------
#
# `references/run-identity-and-lease.md` and `references/critical-rules.md` each state "~30 min" in prose
# today — two sources of truth for one constant, which is what this repo's own rule forbids. Landing this
# file means those sites NAME this constant instead of restating the number.
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


def read_lease(path: Path) -> "dict | None":
    """Return the lease, or None if ABSENT. Raise Corrupt if present and untrustworthy.

    Absent and corrupt are DIFFERENT ANSWERS and must never collapse: absent means nobody is driving,
    corrupt means we cannot tell. Only the first permits adoption.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Corrupt(f"cannot read {path}: {exc}") from exc
    if not raw.strip():
        raise Corrupt(f"{path} is empty — a driver may hold this run; refusing to read that as 'absent'")
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Corrupt(f"{path} is not valid JSON ({exc}) — refusing to read that as 'absent'") from exc
    if not isinstance(rec, dict):
        raise Corrupt(f"{path} holds {type(rec).__name__}, not a JSON object")
    agent = rec.get("agent")
    if not isinstance(agent, str) or not agent.strip():
        raise Corrupt(f"{path} has no usable `agent` — cannot tell who is driving this run")
    updated = rec.get("updated")
    if isinstance(updated, bool) or not isinstance(updated, int):
        raise Corrupt(f"{path} has a non-integer `updated` ({updated!r}) — cannot tell if it is stale")
    return rec


def write_lease(path: Path, rec: dict) -> None:
    """Write the lease atomically, preserving any field this version does not know about."""
    replace_text(path, json.dumps(rec) + "\n", temp_prefix=".lease.", encoding="utf-8")


def age_of(rec: dict) -> int:
    """Seconds since the lease was refreshed, floored at 0 so a skewed clock cannot report a negative age.

    The floor is PRESENTATION ONLY — it is `is_stale`'s signed comparison that makes a future `updated`
    read fresh, not this clamp. Do not credit it with more than it does.
    """
    return max(0, now() - int(rec["updated"]))


def is_stale(rec: dict) -> bool:
    """A lease is stale only once it is OLDER than the window.

    The comparison is deliberately SIGNED and deliberately `>`: a future `updated` (clock skew) yields a
    negative age, which is not greater than the window, so it reads FRESH. That is the fail-closed
    direction and it is the rule doing the work — never `abs()` this, and never widen `>` to `>=`.
    Manufacturing an adoption out of a skewed clock is the one error that puts two drivers on one run.
    """
    return age_of(rec) > LEASE_STALE_AFTER


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
    try:
        yield
    finally:
        try:
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

NO_HEARTBEAT = """\
lease: REFUSED — the lease was NOT taken. Nothing was written, and this run is still UNDRIVEN.
lease: acquire requires --heartbeat-id: your PROOF that you have ALREADY armed the heartbeat for this
       run. This tool never inspects it and takes your word for it — which is exactly why it must be
       something you already did, not something you intend to do.
lease: DO THIS, IN THIS ORDER: (1) arm the heartbeat for this run. `runtime-adapter.md` owns your host's
       mechanism and tells you what your proof is; the wake's own invocation must carry
       --run <id> --token <tok> --heartbeat-id <proof> so it can present the proof it was armed with.
       (2) Re-run this command with that proof.
lease: DO NOT take the lease first and arm afterwards. An arm at the end of a wake is the step that gets
       forgotten — that is the entire reason this door refuses."""

NO_TOKEN = """\
lease: REFUSED — the lease was NOT taken. Nothing was written, and this run is still UNDRIVEN.
lease: acquire requires --token, and it does NOT mint one for you. The wake carries `--token <tok>`, so
       the token must exist BEFORE you arm: a caller without one cannot have armed a wake that identifies
       it, which means its --heartbeat-id cannot name a real wake.
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
        try:
            rec = read_lease(path)
        except Corrupt as exc:
            fail(f"lease: REFUSED — {exc}\n"
                 f"lease: A lease that cannot be parsed is NOT an absent lease. Adopting it could put two "
                 f"agents on one run; refusing only stalls this one. Nothing was written.\n"
                 f"lease: Inspect {path} and, if the run is genuinely orphaned, remove it by hand.")
        if rec is not None and not is_stale(rec) and rec["agent"] != token:
            if not args.allow_takeover:
                emit("superseded", rec, held_by=rec["agent"])
                print(f"lease: REFUSED — a DIFFERENT agent holds this run (lease age {age_of(rec)}s). "
                      f"You are not the driver: do not review, fix, merge, or relabel anything.\n"
                      f"lease: If a human has agreed this run should be taken over, re-run with "
                      f"--allow-takeover.", file=sys.stderr)
                return EXIT_REFUSED
            verdict = "adopted"
        elif rec is None or is_stale(rec):
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

    The prose says "delete lease.json". A superseded driver that follows that literally deletes the LIVE
    owner's lease and hands the run to anyone.
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
            # Release is idempotent: an already-absent lease is release's goal state, so there is no token
            # to match and nothing to undo. The token check below guards a PRESENT live lease from deletion
            # by a non-owner; absent is terminal cleanup already complete, reported, not refused.
            emit("absent", None)
            return EXIT_OK
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
                                "the heartbeat, because the wake carries it")

    # NOTE: --token and --heartbeat-id are deliberately NOT argparse-`required`. They ARE required, and
    # this file refuses without them — but argparse's "the following arguments are required:
    # --heartbeat-id" is a DIAGNOSIS, and the whole mechanism here is the INSTRUCTION the refusal carries
    # (NO_HEARTBEAT / NO_TOKEN). Letting argparse win the race would keep the exit code and throw away the
    # only part that teaches the caller what to do. `lease-test.py` pins this.
    a = sub.add_parser("acquire", help="take or refresh ownership of this run — the door every wake goes "
                                       "through first")
    a.add_argument("--token", help="REQUIRED. Your agent token (from `mint`). NEVER minted here: a caller "
                                   "without one cannot have armed a wake that names it")
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
