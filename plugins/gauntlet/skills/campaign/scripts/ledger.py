#!/usr/bin/env python3
"""Schema-owning accessor for the campaign ledger (state.jsonl).

The store is a plaintext, cat/grep/jq-able JSONL file: one JSON object per line.
Line 1 is the run-config header record (`{"type": "header", ...}`); each following
line is one adopted PR's row record (`{"type": "row", ...}`). This script owns the
schema ONCE (the field lists below) so callers read/write by FIELD NAME and never
by column position — adding a field here can't silently shift every offset in the
skill.
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
from typing import NoReturn

DESCRIPTION = "Schema-owning accessor for the campaign ledger (state.jsonl)."

HERE = Path(__file__).resolve().parent
TEST_PY = HERE / "ledger-test.py"     # the fixture suite — this accessor's executable contract

# --- schema (owned here, once) ------------------------------------------------

HEADER_FIELDS = ("run_id", "base_branch", "api_changes", "reviewer", "required_set", "skill_version")
HEADER_DEFAULTS = {
    "run_id": "-",
    "base_branch": "-",
    "api_changes": "ask",
    "reviewer": "default",
    # WHICH COPY OF THE RULES ACTUALLY GOVERNED THIS RUN — read from the RUNNING plugin's `plugin.json`
    # at startup (SKILL.md), and stated in the final report.
    #
    # It is not bookkeeping. The harness loads this skill from the INSTALLED plugin cache, and a merged,
    # version-bumped rule governs NOTHING until that cache refreshes: a rule that audits findings before
    # fixing them was written, reviewed, merged and bumped — and then did not run, for days, because the
    # installed copy was still the previous version. **No artifact of the run recorded which version it
    # was, so nothing could say so.** A report that says "reviewer: codex" and cannot say "rules: 0.1.2"
    # is a report about a gate whose identity nobody checked.
    #
    # The default is `unknown` for the same reason `required_set`'s is: "I did not look" is a different
    # fact from any version number, and it must never be spelled as one.
    "skill_version": "unknown",
    # What `base_branch` REQUIRES (stage-2-ci.md, "WHAT WERE WE EXPECTING TO SEE?", which owns the three
    # states and the format). A property of the BASE BRANCH, not of a PR, so it lives here, not on the rows.
    #
    #   declared:<json>  the required checks, READ. `ci-snapshot.py --required-set` is the one parser.
    #   none             both reads succeeded and the set is EMPTY — a read FACT: nothing is required.
    #   unknown          a read failed. We do not know what was required.
    #
    # The default is `unknown`, and it is LOAD-BEARING, not a placeholder: `unknown` CANNOT GO GREEN
    # (stage-2-ci.md), so a run that never performed the read merges NOTHING. "I have not looked" and "I
    # looked and there are none" are DIFFERENT facts, and the default is the one that claims nothing —
    # a `none` that really meant "I could not see" is how a green is recorded for a commit whose required
    # check never registered.
    "required_set": "unknown",
}

ROW_FIELDS = (
    "id", "slug", "branch", "worktree", "worktree_owned", "branch_owned", "pr",
    "head_sha", "reviews_ok", "ci", "tier", "attempts", "started",
    "api_approval", "status",
    # Liveness (stage-2-ci.md, "SETTLED" and "UNUSABLE — the refetch is BOUNDED"). A non-green `ci` is
    # not enough to know whether CI is still MOVING or has STOPPED — these carry that, and they must
    # survive a context loss (a wake may be a fresh agent instance), so they live on disk and not in the
    # driver's head. A counter that dies with the context never reaches its cap.
    "ci_fingerprint",     # digest of the last VERIFIED CI snapshot; UNCHANGED + nothing running == SETTLED
    "settled_strikes",    # consecutive derivations seen SETTLED-but-not-green; at the cap -> escalate
    "unusable_refetches", # consecutive UNUSABLE snapshots (they have NO fingerprint); at the cap -> escalate
    # UNCHANGED + a row still RUNNING == RUNNING-STALL: something CLAIMS it can still move, and nothing in
    # the check set has. A TIMESTAMP, not a tally, and that is the point: SLOW and DEAD look identical on a
    # fingerprint, and derivations are driven by wakes whose cadence tracks the RUN'S LOAD, not this PR's
    # CI — so a derivation count would park a healthy 40-minute build on a busy run. Only elapsed TIME
    # tells them apart. On disk so `now - ci_stalled_since` needs nothing but the ledger.
    "ci_stalled_since",   # UTC ISO-8601 of the first derivation that saw this stall; at the cap -> escalate
    # The MACHINE-BLOCKER reason: what campaign cannot get past without a human, in a form they can act
    # on -- the question `blocker_ruling` answers. NOT merely "why `ci` is not green": that is one class
    # of it. The merge-precondition parks (stage-3-merge.md: a draft PR, BLOCKED, an unrecognized
    # mergeStateStatus) write it with `ci` GREEN. The `ci_` prefix understates it; files-and-ledger.md
    # owns the definition.
    "ci_reason",
    # Durable answer to a machine-blocker park: - | retry@<iso> | abort@<iso>. Durable AND spent exactly
    # once: set back to `-` on park ENTRY and on consuming a `retry`, so a ruling can only ever answer the
    # park it was written for (`abort` is terminal and is never cleared). stage-2-ci.md, "THE RULING IS
    # CONSUMED EXACTLY ONCE".
    "blocker_ruling",
    # THE REVIEW LOOP'S MEMORY (stage-2-review-gate.md, "Recording a verdict"). Every other counter in
    # this schema guards CI; the review path had NONE, and that asymmetry was the whole of a spiral that
    # ran one PR through 21 review rounds. `reviews_ok` is a GATE TALLY and is correctly zeroed on every
    # NOT SATISFIED — which means the ledger after 21 rounds is INDISTINGUISHABLE from the ledger after
    # one, and every stopping rule that says "on the second NOT SATISFIED…" is a backstop with no sensor.
    #
    # A wake is a fresh agent instance. A counter that lives in the driver's head does not exist.
    "review_rounds",   # landed verdicts, ever, for this PR. MONOTONE — NEVER reset, by anything.
    "ns_streak",       # consecutive NOT SATISFIED. Reset ONLY by a SATISFIED.
    # WHERE THIS PR'S INTENT CAME FROM — the PROVENANCE of `<rundir>/intent-<pr>.md`:
    #   `-`                not adopted yet
    #   `stated@<iso>`     the PR body already carried a usable intent block; it was COPIED VERBATIM
    #   `authored@<iso>`   the driver INFERRED it from the PR's title, body and diff
    #
    # The block itself is NOT in this store, and that is deliberate: it is many lines of markdown, and this
    # is one JSON object per line that renders as a table. It lives in the run's own dir as
    # `intent-<pr>.md` (files-and-ledger.md) — beside the review artifacts that quote it, readable by the
    # reviewer (which already gets `--add-dir <rundir>`), and by a human. It is DRIVER BOOKKEEPING under
    # `.gauntlet/`: never repo content, never committed, and NEVER written back to the PR.
    #
    # The distinction the two values draw is the honest one, and the report states it: an `authored` intent
    # is **the driver's CLAIM about what the PR is for**, not the author's. It is still far better than the
    # nothing the reviewer was measured against before — but a WRONG intent block silently NARROWS a
    # review, so which kind it is has to be visible rather than buried.
    "intent",
    # WHO AUTHORED THIS PR — it decides which autonomous repairs are permitted (`repair-pass.md`, "The
    # ownership guardrail"). `gauntlet` = this pipeline opened the PR (it carries the `gauntlet-authored`
    # label, applied by gauntlet:review's handoff). `external` = anything else: the user's PR, a
    # teammate's, a PR adopted by number.
    #
    # The default is `external` and it is LOAD-BEARING, not a placeholder: the repairs that REWRITE branch
    # content (RESCOPE, ROOT-CAUSE) are refused on an `external` PR, so a row whose origin was never
    # established can never have its owner's work reshaped. "I do not know who wrote this" and "I wrote
    # this" are different facts, and the default is the one that claims nothing.
    #
    # NOT `worktree_owned`/`branch_owned`: those say whether campaign created the local checkout and
    # branch, which is a CLEANUP question. A PR can have a campaign-created worktree and still belong
    # entirely to someone else.
    "pr_origin",
    # THE REPAIR'S OWN BOUND — the mechanism that fixes non-convergence must not itself fail to converge.
    "repair_count",     # reassessment decisions taken. At REPAIR_CAP the only decision left is ABORT.
    "repair_decision",  # - | <decision>@<iso> — durable, so the wake that DISPATCHES a repair can be a
                        # different agent instance from the one that DECIDED it. RESET to `-` when the row
                        # RE-ENTERS `repairing` (`cmd_verdict` at a cap), scoping a decision to ONE cap: the
                        # next repair must be earned by a fresh `decide` (which spends `repair_count`), so
                        # the budget binds. `abort` is terminal and is never cleared.
)
ROW_DEFAULTS = {
    "id": "-", "slug": "-", "branch": "-", "worktree": "-", "worktree_owned": "-",
    "branch_owned": "-", "pr": "-", "head_sha": "-", "reviews_ok": "0", "ci": "pending",
    "tier": "-", "attempts": "0", "started": "-", "api_approval": "-", "status": "pending",
    "ci_fingerprint": "-", "settled_strikes": "0", "unusable_refetches": "0",
    "ci_stalled_since": "-", "ci_reason": "-", "blocker_ruling": "-",
    "review_rounds": "0", "ns_streak": "0", "intent": "-",
    "pr_origin": "external", "repair_count": "0", "repair_decision": "-",
}

# The two fields `verdict` OWNS — and the ONLY reason they are not settable through `set`/`add-row` is
# that a door which can write them is a door that can RESET them.
#
# `review_rounds` is the loop's only memory across fresh-context wakes, and its whole value is that it is
# MONOTONE. A rule stating "never reset it" is an exhortation; REMOVING THE DOOR is a mechanism. So there
# is no `--review-rounds` flag to type: `verdict` increments them, and nothing else writes them at all.
# (`reviews_ok` is different — a content change legitimately voids the tally, so `set --reviews-ok 0`
# must stay. What `set` may NOT do is RAISE it: see `check_tally`.)
VERDICT_OWNED = ("review_rounds", "ns_streak")

# The repair's own fields, owned by `repair-pass.py` and settable through NO flag — the same mechanism as
# VERDICT_OWNED, for the same reason. `repair_count` is the bound on the repair itself: a door that can
# write it is a door that can zero it, and a driver that could zero its own repair budget could repair
# forever. The tool that decides a repair is the only thing that writes what it spent.
REPAIR_OWNED = ("repair_count", "repair_decision")

SATISFIED, NOT_SATISFIED = "satisfied", "not-satisfied"
VERDICTS = (SATISFIED, NOT_SATISFIED)

# --- the review loop's caps (this file is their ONE defining site) ------------
#
# Numbered here and NOWHERE ELSE. Every reference that needs one names the CAP, never its value — a value
# retyped in prose goes stale the day it is tuned, and the prose is the copy people read.
#
# The numbers are DEFENDED AGAINST THE REAL RECORD (`repair-pass.md`, "The thresholds"), and
# `ledger-test.py` REPLAYS that record so a re-tuned cap states, in its own failure message, what the new
# number would have done to two PRs that really ran:
#
#   ROUND_CAP = 11    PR #43's last GENUINE defect (a false green reachable from a real GitHub response —
#                     the exact thing that PR existed to prevent) landed on its 10th verdict and was
#                     corroborated by its 11th. A cap of 11 is the smallest that lets a PR doing real work
#                     finish it. It then fires on #43's 12th verdict — its FIRST spiral round — and on
#                     #42's 11th of 21.
#   NS_STREAK_CAP = 6 #43 ran FIVE consecutive NOT SATISFIEDs that were still producing genuine, purpose-
#                     serving findings, so 5 must NOT trigger. 6 is the smallest number the record does not
#                     refute. It catches the shape neither PR had — a PR that NEVER scores a SATISFIED —
#                     several rounds before ROUND_CAP would.
#   REPAIR_CAP = 2    A second failed repair aborts rather than looping.
#
# A wrong threshold is SURVIVABLE, and that is what licenses picking one at all: the trigger is a MODE
# SWITCH, not a rejection. Firing early costs one reassessment pass and a re-gate; nothing is merged, and
# nothing is closed. Firing late costs 8.5 hours.
ROUND_CAP = 11
NS_STREAK_CAP = 6
REPAIR_CAP = 2

# Where a PR goes when it reaches a cap. It is NOT a park: a park waits on a HUMAN and only the user's
# answer leaves it, while this waits on the reassessment pass and the driver clears it itself.
REPAIR_STATUS = "repairing"

# HELD — the statuses in which campaign MUST NOT dispatch ordinary gate work on a PR (a review pass, a
# review fix, a CI fix, a merge, a rebase, a relabel — anything that MUTATES it). THE ONE ENUMERATION;
# `dispatch-check` is what makes it a command that can FAIL rather than a rule an attentive agent must
# remember. The members are held for DIFFERENT reasons and cleared by DIFFERENT events — never collapse
# them:
#
#   awaiting-api / awaiting-user   parked on a HUMAN. Only the user's answer unparks.
#   repairing                      at a review-loop cap. The reassessment pass and the repair it decides
#                                  clear it — no human is waited on.
#
# The ONE exception, for every member alike: OBSERVING a PR is not mutating it, so the CI watch follows
# its normal policy, and reconcile still READS a held PR and records what it read.
HELD_STATUSES = ("awaiting-api", "awaiting-user", REPAIR_STATUS)

# `fail()` keeps 1 for "your input was rejected". A HELD/AT-CAP answer is NOT an input error — the command
# did its job and the answer is STOP — so it gets its own code. A driver that proceeds anyway has a FAILED
# COMMAND in its transcript, not a defensible judgment call.
EXIT_STOP = 3

# A git object id, as git writes one: 40 LOWERCASE hex — the same rule `review-pass.py` and
# `ci-snapshot.py` hold a SHA to, and for the same reason. A verdict is recorded AGAINST a commit, so a
# value that is not a commit id makes the "does this verdict still describe the live tip?" comparison
# unfalsifiable. A short SHA has escaped into this repo's real state twice.
SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")

# A counter's on-disk form: a decimal, from zero up. No sign, no padding, no `int()` on raw input (which
# would take `" +2 "` and CRASH on `"-"` — and `-` is this schema's own "not set" spelling, so it is a
# value a counter field can genuinely be handed by a hand-edited store).
COUNT_RE = re.compile(r"^(?:0|[1-9][0-9]*)\Z")


def fail(msg: str) -> NoReturn:
    print(f"ledger: {msg}", file=sys.stderr)
    raise SystemExit(1)


def counter(row: dict, field: str) -> int:
    """One counter field, as a NUMBER — refusing anything that is not one.

    Every value in this store is a string, and these three are ARITHMETIC: `verdict` adds to them. A
    field holding `-` or `two` cannot be added to, and `int()` on it does not return a wrong answer — it
    CRASHES, in the middle of a write, which is the one thing a store accessor must never do. So the
    value is CHECKED, and a store that cannot be counted is refused with a message naming the field.
    """
    value = row.get(field, ROW_DEFAULTS[field])
    if not COUNT_RE.match(value):
        fail(f"row for pr {row.get('pr')}: `{field}` is {value!r}, which is not a count (a decimal from 0 "
             f"up) — this field is ARITHMETIC, and a value nothing can add to is a corrupt store, never a "
             f"number to guess at")
    return int(value)


# --- parse / serialize --------------------------------------------------------

def load(path: Path) -> "tuple[dict, list[dict]]":
    """Return (header, rows). A missing file yields defaults + no rows.

    The store is JSONL: one JSON object per line, routed by its `type` key. The
    header record MUST be the first non-blank line and appear exactly once; each
    following `row` record appends a row. Missing fields fill from the defaults.
    Every ingested field value is coerced to `str` (mirroring how `dump()` writes
    via `str()`), so on-disk JSON numbers/bools compare as the string keys the rest
    of the accessor uses; a row's `id` is always recomputed from its normalized
    `pr` (never trusted from the file). An unknown `type` is rejected, not dropped.
    """
    header = dict(HEADER_DEFAULTS)
    rows: list[dict] = []
    if not path.exists():
        return header, rows
    seen_pr: set[str] = set()
    saw_first = False
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            fail(f"malformed JSON on line {n}: {e}")
        if not isinstance(rec, dict):
            fail(f"line {n}: record is not a JSON object")
        kind = rec.get("type")
        if kind not in ("header", "row"):
            fail(f"line {n}: missing or unknown record type {kind!r}")
        # Header must be the first non-blank record and appear exactly once.
        if not saw_first:
            if kind != "header":
                fail(f"line {n}: first record must be the header")
            saw_first = True
        elif kind == "header":
            fail(f"line {n}: unexpected second/out-of-order header record")
        if kind == "header":
            # coerce every value to str, matching dump()'s write side
            for f in HEADER_FIELDS:
                header[f] = str(rec.get(f, HEADER_DEFAULTS[f]))
        else:  # kind == "row"
            row = dict(ROW_DEFAULTS)
            # coerce every value to str first, so 11 and "11" are one key
            for f in ROW_FIELDS:
                row[f] = str(rec.get(f, ROW_DEFAULTS[f]))
            pr = row["pr"]
            # id is derived, never trusted from the file: recompute from pr
            row["id"] = f"pr{pr}"
            # duplicate detection runs on the normalized (string) pr key
            if pr in seen_pr:
                fail(f"line {n}: duplicate row for pr {pr}")
            seen_pr.add(pr)
            rows.append(row)
    # A present file must carry a header record. A genuinely MISSING file is a
    # fresh start (returned defaults above); a present-but-headerless file (empty,
    # all-blank, or truncated) is corrupt — reject it rather than silently reset to
    # defaults, which would drop the run config and every row without complaint.
    if not saw_first:
        fail("line 1: missing header record")
    return header, rows


def dump(path: Path, header: dict, rows: list[dict]) -> None:
    """Write the WHOLE store — ATOMICALLY. The ledger is never partly written, ever.

    This used to be `path.write_text(...)`, which is a TRUNCATE-then-write: the target is emptied first and
    the bytes go in after. Interrupt it — a crash, a full disk, a killed wake — and what is left on disk is
    a ledger that is empty or cut in half, and `load()` (correctly) refuses a headerless file. The run's
    ONLY memory would be gone, and nothing could tell that from a run that had never started.

    It matters more now than it did: `verdict` bumps `review_rounds`, moves `ns_streak` and applies
    `reviews_ok` in ONE write, and those three are only coherent TOGETHER — `review_rounds` is documented
    as monotone and reset by nothing. A torn write there does not lose a number, it corrupts the gate's
    memory of how many rounds this PR has already burned, which is the one counter that can see a loop.

    The write is therefore: a temp file in the SAME DIRECTORY (a rename is only atomic WITHIN a
    filesystem — a temp in `/tmp` would be a copy, and a copy can tear exactly like the truncate did),
    flushed and `fsync`ed so the bytes are on the platter BEFORE anything points at them, then
    `os.replace()` onto the target, which is atomic: a reader sees the whole old file or the whole new one,
    and never a byte of anything else. A failure at any point leaves the ORIGINAL untouched and takes the
    temp file with it.
    """
    out = [json.dumps({"type": "header", **{f: header.get(f, HEADER_DEFAULTS[f]) for f in HEADER_FIELDS}})]
    for row in rows:
        out.append(json.dumps({"type": "row", **{f: str(row.get(f, ROW_DEFAULTS[f])) for f in ROW_FIELDS}}))
    path.parent.mkdir(parents=True, exist_ok=True)
    # `mkstemp` creates the file 0600. That is the right default for a TEMP file and the wrong one for the
    # store it is about to become: the ledger is `cat`-able bookkeeping, and making it atomic must not
    # quietly re-permission it. So the mode a plain create would have produced is restored below. Reading
    # the umask means setting it, so it is read here — once, before anything exists to leak — and set back
    # on the same breath.
    mask = os.umask(0)
    os.umask(mask)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:  # fdopen OWNS the descriptor: `with` closes it
            fh.write("\n".join(out) + "\n")
            fh.flush()
            os.fsync(fh.fileno())  # the bytes are DURABLE before the rename makes them the ledger
        os.chmod(tmp, 0o644 & ~mask)
        os.replace(tmp, path)      # atomic within the filesystem: the swap is all-or-nothing
    except BaseException:
        # Nothing was replaced, so the ledger on disk is still the last COMPLETE one. Do not leave the
        # half-written temp behind to be mistaken for a store.
        tmp.unlink(missing_ok=True)
        raise


def find_row(rows: list[dict], pr: str) -> "dict | None":
    for row in rows:
        if row.get("pr") == pr:
            return row
    return None


def check_field(name: str, valid: "tuple[str, ...]") -> None:
    if name not in valid:
        fail(f"unknown field '{name}'; valid: {', '.join(valid)}")


# --- subcommands --------------------------------------------------------------

def cmd_header(path: Path, args) -> int:
    header, rows = load(path)
    check_field(args.field, HEADER_FIELDS)
    if args.action == "get":
        if args.value is not None:  # `header get <field>` takes no value; reject a stray extra arg
            fail("header get takes no value")
        print(header.get(args.field, HEADER_DEFAULTS[args.field]))
        return 0
    if args.value is None:
        fail("header set requires a value")
    header[args.field] = args.value
    dump(path, header, rows)
    return 0


def settable(name: str) -> bool:
    """May `set`/`add-row` write this field?

    `pr` is the row key (passed via --pr) and `id` is derived from it. `VERDICT_OWNED` is the new
    exclusion, and it is the mechanism behind "`review_rounds` is NEVER reset": a door that can write a
    counter is a door that can zero it, so those two fields simply have NO flag. `verdict` writes them.
    `REPAIR_OWNED` is the same mechanism for the repair's bound: a driver that could zero `repair_count`
    could repair forever, so only `repair-pass.py decide` writes what a PR has spent.
    """
    return name not in ("pr", "id") and name not in VERDICT_OWNED and name not in REPAIR_OWNED


def _named_field_values(args) -> dict:
    """Collect the --<row-field> options that were actually supplied."""
    values = {}
    for name in ROW_FIELDS:
        if not settable(name):
            continue
        val = getattr(args, name, None)
        if val is not None:
            values[name] = val
    return values


def check_tally(updates: dict, row: dict) -> None:
    """`set` may VOID the tally. It may NEVER RAISE it.

    **`reviews_ok` counts SATISFIED verdicts, and only a verdict may add one.** It stays settable because
    voiding it is a real, frequent, correct event that is NOT a verdict — a fix commit, a rebase that
    resolves conflicts, any PR-content change drops it to 0 (stage-2-review-gate.md, "Status labels mirror
    the review gate", lists every such site). What no caller may do is write a HIGHER number: that is
    manufacturing a verdict that no review pass ever returned, and it would also skip `review_rounds` and
    `ns_streak` — the two counters that exist precisely because the tally alone cannot see a loop.

    So the rule is a FLOOR-ONLY door: `set --reviews-ok <n>` is accepted while `n <= current`, and
    `verdict` is the only way up. "NEVER set `reviews_ok` by hand" is then a mechanism, not an
    exhortation — and the failure mode it guards is the one that cannot be seen from a transcript: a
    driver that raised the tally by hand looks EXACTLY like a driver that earned it.
    """
    if "reviews_ok" not in updates:
        return
    want = updates["reviews_ok"]
    if not COUNT_RE.match(want):
        fail(f"--reviews-ok {want!r} is not a count (a decimal from 0 up)")
    if int(want) > counter(row, "reviews_ok"):
        fail(f"--reviews-ok {want} would RAISE the tally from {row['reviews_ok']} — and a SATISFIED "
             f"verdict is the only thing that may. Record it with `verdict --pr {row['pr']} --head-sha "
             f"<sha> --verdict satisfied`, which bumps `review_rounds` and clears `ns_streak` in the same "
             f"write. `set` may still VOID the tally (a content change drops it to 0); it may never "
             f"manufacture one, because a hand-raised tally is indistinguishable from an earned one")


def cmd_add_row(path: Path, args) -> int:
    header, rows = load(path)
    pr = str(args.pr)
    if find_row(rows, pr) is not None:
        fail(f"a row for pr {pr} already exists; use `set` to update it")
    row = dict(ROW_DEFAULTS)
    updates = _named_field_values(args)  # pr/id/VERDICT_OWNED excluded — they are derived or verdict-owned
    # A NEW row has run no reviews, so its tally is 0 and the floor rule applies from the defaults: a
    # `--reviews-ok 2` at CREATION is the same forged verdict as one at `set`, and it used to be the one
    # door where it went through.
    check_tally(updates, row)
    row.update(updates)
    row["pr"] = pr  # --pr is the row key
    row["id"] = f"pr{pr}"  # id is always derived from pr, never caller-set
    rows.append(row)
    dump(path, header, rows)
    print(json.dumps(row))
    return 0


def cmd_set(path: Path, args) -> int:
    header, rows = load(path)
    pr = str(args.pr)
    row = find_row(rows, pr)
    if row is None:
        fail(f"no row for pr {pr}; use `add-row` to create it")
    updates = _named_field_values(args)
    if not updates:
        fail("set requires at least one --<field> <value>")
    check_tally(updates, row)
    row.update(updates)  # by NAME — never by column position
    dump(path, header, rows)
    print(json.dumps(row))
    return 0


def cmd_verdict(path: Path, args) -> int:
    """Record ONE landed review verdict — the ONLY sanctioned way, and the only door that writes the
    counters (stage-2-review-gate.md, "Recording a verdict").

    It does THREE things in one atomic write, and the whole point is that they cannot be done separately:

      * bumps `review_rounds` — **always, on every verdict, and it is NEVER reset.** This is the loop's
        only memory. Not the fix that follows, not a rebase, not a content change, not a re-triage may
        take it back, because every one of those is a thing that HAPPENED and a round that HAPPENED is a
        round the next wake must be able to see. There is no `--review-rounds` flag anywhere in this
        tool to argue with;
      * applies the TALLY — `satisfied` adds one to `reviews_ok`; `not-satisfied` VOIDS it (the SHA's
        verdicts are worthless the moment one pass says the content is wrong);
      * moves `ns_streak` — up on `not-satisfied`, back to 0 on a `satisfied` and on nothing else.

    **The head SHA is checked against the row, and a mismatch is REFUSED.** A verdict describes the
    content the pass RAN on. If the tip has moved since, that verdict describes content that is no longer
    there — `review-pass.py verify` already refuses such a pass, and this is the same rule at the ledger
    door, so a late verdict from a superseded attempt can never reach the tally through a driver that
    skipped the check.

    **The counters are SENSORS, and this door NEVER WRITES THEM BACKWARDS — but it does READ them, and at
    a cap it STOPS THE LOOP.** The hazard in fusing a reader into a sensor is that the reader comes to
    reset the counter it consumes; that hazard is structurally absent here, because the cap path writes
    `status` and NOTHING ELSE — `review_rounds` stays monotone whatever it decides.

    And the trigger MUST live here, because this is the one door that cannot be skipped. A cap evaluated
    by a separate command is a cap a driver can forget to run — which is precisely the failure this whole
    mechanism exists to end: the skill's "hard backstop" on the 2nd NOT SATISFIED never fired once in 35
    review rounds, because nothing computed it. So at a cap this sets `status = repairing` and **exits
    non-zero** (`EXIT_STOP`), and the driver that ignores it has a failed command in its transcript.

    **The caps are evaluated ONLY on a NOT SATISFIED.** A SATISFIED is the gate MOVING — the PR may be one
    corroborating pass from merging, and tearing it up for a repair would be the mechanism destroying the
    very outcome it exists to protect. (On the real record: PR #42's TENTH landed verdict was a SATISFIED.)
    A PR is interrupted only when a verdict says the content is STILL WRONG *and* the loop's own history
    says it is no longer converging.
    """
    header, rows = load(path)
    pr = str(args.pr)
    row = find_row(rows, pr)
    if row is None:
        fail(f"no row for pr {pr}; use `add-row` to create it")
    if row["status"] in HELD_STATUSES:
        fail(f"pr {pr} is {row['status']} — no review pass should have been running for it, so this "
             f"verdict describes work that was never due ({held_reason(row['status'])})")
    if not SHA_RE.match(args.head_sha):
        fail(f"--head-sha {args.head_sha!r} is not a git object id (40 LOWERCASE hex) — a verdict is "
             f"recorded AGAINST a commit, and a value that cannot be one makes every 'does this verdict "
             f"still describe the tip?' comparison unfalsifiable")
    if row["head_sha"] != args.head_sha:
        fail(f"this verdict ran on {args.head_sha} but pr {pr}'s head is {row['head_sha']} — it describes "
             f"content that is no longer there. A verdict for a superseded SHA never counts: reconcile the "
             f"row against `gh` first, and re-review the live tip")

    rounds = counter(row, "review_rounds") + 1   # MONOTONE. Bumped before anything can go wrong below.
    if args.verdict == SATISFIED:
        tally, streak = counter(row, "reviews_ok") + 1, 0
    else:
        tally, streak = 0, counter(row, "ns_streak") + 1
    row.update({"review_rounds": str(rounds), "reviews_ok": str(tally), "ns_streak": str(streak)})

    at_cap = args.verdict == NOT_SATISFIED and (rounds >= ROUND_CAP or streak >= NS_STREAK_CAP)
    if at_cap:
        row["status"] = REPAIR_STATUS
        # Clear any STALE reassessment decision as the row RE-ENTERS `repairing`. `repair_decision` is
        # written only by `repair-pass.py decide` (which also spends `repair_count`) and by this reset, so a
        # decision left on the row from a PREVIOUS cap would otherwise satisfy `dispatch-check --action
        # repair` at THIS cap with no fresh `decide` — dispatching a repair that spends no budget, and
        # `REPAIR_CAP` would never bind. A verdict only reaches this branch from a non-held (so post-repair
        # `in_review`) row, so the decision it clears is always a spent one. The next repair here MUST be
        # earned by a fresh `decide`, which bumps `repair_count` — the bound the mechanism exists to hold.
        row["repair_decision"] = "-"
    dump(path, header, rows)
    print(json.dumps(row))
    if not at_cap:
        return 0

    which = []
    if rounds >= ROUND_CAP:
        which.append(f"review_rounds={rounds} (cap {ROUND_CAP})")
    if streak >= NS_STREAK_CAP:
        which.append(f"ns_streak={streak} (cap {NS_STREAK_CAP})")
    spent = counter(row, "repair_count") >= REPAIR_CAP
    print(
        f"ledger: pr {pr} is NOT CONVERGING — {', '.join(which)}. The verdict IS recorded, and the row is "
        f"now `{REPAIR_STATUS}`.\n"
        f"ledger: DO NOT dispatch a fix subagent and DO NOT launch another review pass for it. Run the "
        f"REASSESSMENT PASS (`repair-pass.md`): hand a context-isolated agent the WHOLE history at once — "
        f"every round's verdict and finding, the diff-growth curve, the intent, the current diff — and "
        f"record the ONE decision it returns with `repair-pass.py decide`.\n"
        f"ledger: repair_count={counter(row, 'repair_count')} of {REPAIR_CAP}"
        + (" — SPENT: this PR's repair budget is exhausted, so the only permitted decision is `abort`."
           if spent else "."),
        file=sys.stderr,
    )
    return EXIT_STOP


def held_reason(status: str) -> str:
    """Why a held row is held, and WHAT CLEARS IT — never merely that it is on a list.

    A guard that says only "refused" teaches the driver nothing and invites it to route around the guard.
    DERIVED from the status, so a new member of HELD_STATUSES cannot silently inherit a wrong explanation.
    """
    if status == REPAIR_STATUS:
        return ("at a review-loop cap and awaiting its REASSESSMENT PASS — the reassessment's decision and "
                "the repair it dispatches clear it (`repair-pass.md`); NO human is waited on")
    if status == "awaiting-api":
        return "parked for the user to approve an API-changing fix — only the user's answer unparks it"
    if status == "awaiting-user":
        return ("parked for the user to adjudicate a review standoff or a machine blocker — only the "
                "user's answer unparks it")
    return "held"


def cmd_dispatch_check(path: Path, args) -> int:
    """MAY campaign act on this PR? Run it BEFORE every action that MUTATES a PR — and obey a non-zero exit.

    This is the mechanical form of a rule that already existed in prose and was already obeyed only by
    attention: **a HELD PR is FROZEN**. The park has always said so; `repairing` says so too. A rule an
    attentive agent must remember is exactly the kind that failed here.

    The test is the PROPERTY — "does this MUTATE the PR?" — not membership of a list of actions. Review
    pass, review fix, CI fix, copilot-address, precondition fix, merge, rebase, base refresh, push,
    relabel: all mutate, all are `ordinary`, all must check. **OBSERVING is not mutating**: the CI watch
    follows its normal policy, and reconcile still reads a held PR and records what it read.

    **`--action repair` is the ONE kind of work a `repairing` row accepts** — and it is refused until a
    reassessment DECISION is on the row. Without that second half the guard would have a hole exactly
    where it matters: a driver could call its next fix "the repair", dispatch it, and go right on whacking
    moles under a new name. The decision must exist first, and the tool prints WHICH one, so the work that
    follows is the work that was decided.
    """
    _, rows = load(path)
    pr = str(args.pr)
    row = find_row(rows, pr)
    if row is None:
        fail(f"no row for pr {pr}")
    status, decision = row["status"], row["repair_decision"]

    if args.action == "repair":
        if status != REPAIR_STATUS:
            print(f"refused: pr {pr} is {status}, not {REPAIR_STATUS} — it is not at a review-loop cap, so "
                  f"there is no repair to dispatch. Ordinary gate work is what this PR is owed.",
                  file=sys.stderr)
            return EXIT_STOP
        if decision == "-":
            print(f"refused: pr {pr} is {REPAIR_STATUS} but NO REASSESSMENT DECISION is recorded. Run the "
                  f"reassessment pass and record its decision with `repair-pass.py decide` FIRST — a repair "
                  f"dispatched without one is just the next targeted fix wearing a new name, which is the "
                  f"loop this mechanism exists to stop.", file=sys.stderr)
            return EXIT_STOP
        print(f"ok: pr {pr} is {REPAIR_STATUS} with decision {decision} — dispatch THAT repair, and no "
              f"other work")
        return 0

    if status not in HELD_STATUSES:
        print(f"ok: pr {pr} is {status} — campaign may act on it")
        return 0
    print(f"held: pr {pr} is {status} — {held_reason(status)}", file=sys.stderr)
    print(f"held: take NO action that MUTATES this PR (no review pass, review fix, CI fix, merge, rebase, "
          f"push or relabel). Observing it is fine: the CI watch and reconcile are unaffected. Keep "
          f"driving the run's OTHER PRs — a held PR never blocks the loop.", file=sys.stderr)
    return EXIT_STOP


def cmd_get(path: Path, args) -> int:
    _, rows = load(path)
    row = find_row(rows, str(args.pr))
    if row is None:
        fail(f"no row for pr {args.pr}")
    if args.field is not None:  # an empty --field is an invalid field, not "omitted"
        check_field(args.field, ROW_FIELDS)
        print(row[args.field])
    else:
        # project onto ROW_FIELDS so the injected `type` key never leaks out
        print(json.dumps({f: row[f] for f in ROW_FIELDS}))
    return 0


def cmd_list(path: Path, args) -> int:
    _, rows = load(path)
    if args.where is not None:  # an empty --where is malformed, not "omitted"
        if "=" not in args.where:
            fail("--where must be <field>=<value>")
        field, _, value = args.where.partition("=")
        check_field(field, ROW_FIELDS)
        rows = [r for r in rows if r.get(field) == value]
    for row in rows:
        print(row["pr"])
    return 0


# `review_rounds` is in the DEFAULT view, and that is the point of it. `reviews_ok` is a gate tally that
# returns to 0 on every NOT SATISFIED, so a PR twenty rounds deep and a PR on its first one render
# IDENTICALLY — which is exactly what happened: the only reader who could see the spiral was a human who
# happened to hold every round in one context, and stopping it took them. A round count in the status view
# is how the next one is visible at a glance.
TABLE_DEFAULT_FIELDS = ("pr", "slug", "tier", "reviews_ok", "review_rounds", "ci", "attempts", "status",
                        "head_sha")

# Display-only truncation: a 40-char SHA would dominate the table. The full
# value stays on disk and in `get`; the table is a projection, not a source.
TABLE_SHA_WIDTH = 8

# The out-of-band lines the table prints WHERE A ROW WOULD GO — so unlike every other piece of the layout
# they sit in the one region a value also occupies, and position alone cannot tell them apart. They are
# therefore made unforgeable BY CONSTRUCTION rather than by escaping them after the fact: they live in the
# `#` namespace, which `escape_cell()` already proves no cell can enter (a leading `#` is escaped, and a
# body line always opens with its first cell — or with that cell's padding). A bare `(no rows)` was
# forgeable: a row whose only shown field held that literal string rendered a body byte-identical to an
# empty ledger's. That is the SAME CLASS as the header forgery `escape_cell()` exists to kill — an
# out-of-band marker an in-band value can impersonate — so it gets the same answer: put every marker
# somewhere values provably cannot reach. Each new marker below inherits that guarantee for free, and only
# because it opens with `#`.
#
# The two EMPTY-GRID markers are deliberately DIFFERENT LINES, because they are different facts and a
# reader acts differently on each: an empty ledger has adopted nothing, while an all-hidden ledger has
# finished everything. Printing `# (no rows)` for the second would be the exact lie this file exists to
# prevent — a table saying something the ledger never did — and "every PR is merged" is a REAL end-of-run
# state, not a corner case.
TABLE_EMPTY_MARKER = "# (no rows)"
TABLE_ALL_HIDDEN_MARKER = "# (no rows shown — the ledger is NOT empty; every row it holds is hidden)"

# What the DEFAULT view hides. ONLY `merged` — and that is a deliberate call, not a synonym for
# "terminal". A merged PR is DONE: nothing in the run and nobody reading the table has anything left to do
# about it, and in a long run these rows are most of the ledger, crowding out the PRs still in play.
# `aborted` is terminal too, but it is the OPPOSITE kind of terminal — the run GAVE UP on that PR and left
# it open for its owner (`bailout-and-final-report.md`). It is precisely the row a HUMAN may still need to
# act on, so hiding it would bury the run's unfinished business, which is the one thing a status view must
# never do. The line is drawn at "finished successfully", NOT at "reached a terminal state". Everything
# else (in-flight and parked alike) is live work and always shows.
TABLE_HIDDEN_STATUSES = ("merged",)


def _hex_escape(ch: str) -> str:
    """`\\xNN` for a byte-sized code point, `\\uNNNN` above it — the same spelling Python's own repr uses."""
    return f"\\x{ord(ch):02x}" if ord(ch) < 0x100 else f"\\u{ord(ch):04x}"


def escape_cell(value: str) -> str:
    r"""Make a value safe to render inside the grid — no value may forge the layout, and no two
    values may render THE SAME.

    A field value is free text (`slug` is a PR title; `ci`-style reason fields hold
    prose), so it can contain the very characters the table's layout is built from.
    Rendered raw, a value carrying a `|` fabricates extra columns, a newline
    fabricates extra ROWS, and a leading `# ` mimics the run-config header lines
    printed above the grid. The table would then say something the ledger never did.

    ESCAPING, not quoting: quoting every cell would widen every column by two chars
    for the common (harmless) case and STILL need an escape for an embedded quote —
    so it buys nothing. Backslash escapes leave every ordinary value byte-identical
    and give the invariant outright: the returned string contains no BARE `|` (every
    one is escaped to `\|`, and a `|` preceded by a backslash can never spell the
    ` | ` column separator), no line break, no control character, and never starts
    with `#`. Each escape is
    unambiguous (a literal backslash is doubled), so the display stays reversible by
    eye — but it is still a DISPLAY form. Read values back with `get --field`, never
    by parsing this table.

    WHITESPACE IS ESCAPED AT THE EDGES, and that is not cosmetic — it is what makes the
    escaping INJECTIVE ONCE PRINTED. The layout pads each cell with `ljust()` and then
    `rstrip()`s the line, and BOTH of those EAT trailing whitespace: with it left raw,
    `""` and `"   "` printed the same blank cell and `"a"` and `"a "` printed the same
    `a`. The sanitizer was injective and the RENDERING was not — which is the same lie,
    one layer down. So a leading or trailing whitespace run is escaped (`\x20` for a
    space) and it survives display. INTERIOR spaces are left alone: escaping them would
    turn every PR title into `add\x20a\x20table`, and nothing eats them.

    Whitespace that is NOT a plain space is escaped WHEREVER it appears: it is invisible
    or line-break-ish, `str.rstrip()` eats every bit of it (NBSP and NEL included, not
    just ASCII), and nothing ordinary contains it. So the guarantee is flat: the escaped
    text NEVER starts or ends with whitespace, and holds no whitespace but the plain
    interior space — which is exactly what lets the printed cell be read back off the
    line by stripping its padding (see `grid()` in the self-test).

    Callers MUST escape BEFORE computing column widths, so the widths measure what is
    actually printed. (Truncation of `head_sha` happens first, on the raw value, so a
    cut can never land inside an escape sequence.)
    """
    # The RAW value's whitespace edges — `lstrip`/`rstrip` here use exactly the definition of
    # whitespace that `cmd_table`'s `rstrip()` will apply to the printed line, so what is escaped is
    # precisely what the layout would otherwise eat.
    lead = len(value) - len(value.lstrip())
    trail = len(value.rstrip())  # index where the trailing whitespace run starts
    out = []
    for i, ch in enumerate(value):
        if ch == "\\":
            out.append("\\\\")
        elif ch == "|":
            out.append("\\|")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\x{ord(ch):02x}")  # any other control char
        elif ch.isspace() and ch != " ":
            out.append(_hex_escape(ch))  # NBSP, NEL, U+2028, ideographic space…: invisible, and rstrip() eats it
        elif ch == " " and (i < lead or i >= trail):
            out.append("\\x20")  # a LEADING or TRAILING space: the padding would swallow it
        else:
            out.append(ch)
    text = "".join(out)
    # Only a FIRST-column cell can open a line, but escape a leading '#' in every
    # cell regardless: one value then has one rendering, whatever column it lands in.
    # (A leading whitespace run is already escaped above, so a '#' here can only be the
    # value's own first character.)
    if text.startswith("#"):
        text = "\\" + text
    return text


def hidden_notice(n: int) -> str:
    """The line that makes the omission LOUD — printed whenever the default view drops a row.

    A FILTERED VIEW THAT DOES NOT SAY WHAT IT HID IS A LIE BY OMISSION, and it is the same lie as a
    truncated `gh pr list` reporting 30 of 200 PRs without a word: nothing is fabricated, the reader is
    simply never told that what they are looking at is a SUBSET. So the count is stated, and so is the flag
    that reveals the rest — a reader can always get from the filtered view to the whole ledger without
    knowing this file exists.

    The wording is DERIVED from `TABLE_HIDDEN_STATUSES`, never spelled beside it: change what the default
    hides and this line follows by construction, instead of becoming a stale restatement of the rule it
    is supposed to summarise.
    """
    what = "/".join(TABLE_HIDDEN_STATUSES)
    return f"# {n} {what} row{'' if n == 1 else 's'} hidden — pass --all to show every row"


def cmd_table(path: Path, args) -> int:
    header, rows = load(path)
    if args.fields is not None:  # an empty --fields is malformed, not "omitted"
        fields = tuple(f.strip() for f in args.fields.split(","))
        for f in fields:
            check_field(f, ROW_FIELDS)
    else:
        fields = TABLE_DEFAULT_FIELDS
    # The DEFAULT view drops finished work (see TABLE_HIDDEN_STATUSES); --all shows the whole ledger.
    # `--all` composes with `--fields`: one picks the ROWS, the other the COLUMNS, and neither reads the
    # other.
    shown = rows if args.show_all else [r for r in rows if r["status"] not in TABLE_HIDDEN_STATUSES]
    hidden = len(rows) - len(shown)
    # '#' + blank line keep the run-config lines from reading as table columns.
    # Header values are free text too, so they are escaped on the same terms: an
    # un-escaped newline here would inject lines that read as part of the grid.
    for f in HEADER_FIELDS:
        print(f"# {f}: {escape_cell(header[f])}")
    print()
    cells = []
    # ONLY the rows that are actually printed become cells. That is not merely an optimization: it is what
    # keeps a hidden row from reaching the VISIBLE output at all. Build cells from every row and the widths
    # below would still be measured over the hidden ones, so a merged PR with a 200-char slug would silently
    # blow out the columns of a table it does not even appear in — a value nobody printed, changing what
    # the reader sees.
    for row in shown:
        # truncate the RAW sha, then escape — so a cut never splits an escape
        cells.append(tuple(
            escape_cell(row[f][:TABLE_SHA_WIDTH] if f == "head_sha" else row[f])
            for f in fields
        ))
    # widths measure the ESCAPED cells — i.e. exactly the text that gets printed
    widths = [max(len(f), *(len(c[i]) for c in cells)) if cells else len(f)
              for i, f in enumerate(fields)]
    print(" | ".join(f.ljust(w) for f, w in zip(fields, widths)).rstrip())
    print("-+-".join("-" * w for w in widths))
    for c in cells:
        print(" | ".join(v.ljust(w) for v, w in zip(c, widths)).rstrip())
    if not cells:
        # An empty grid is now AMBIGUOUS — nothing adopted, or everything finished — so say WHICH.
        # Both markers live in the '#' namespace: no cell can render a line that impersonates either.
        print(TABLE_EMPTY_MARKER if not rows else TABLE_ALL_HIDDEN_MARKER)
    if hidden:
        print(hidden_notice(hidden))
    return 0


# --- self-test: the fixtures ARE the contract, and they are a SIBLING ---------
#
# THE SUITE LIVES IN `ledger-test.py`, NOT IN THIS FILE. A self-test that ships inside the thing it tests
# is a self-test the thing it tests can quietly disarm — and this repo has watched a reviewer do exactly
# that, twice, by editing a fixture table in memory and watching the suite still report green.
#
# `self-test` loads that sibling by a `__file__`-relative path — never the cwd, which is the reviewer's
# worktree while the skill's scripts live wherever the plugin is installed — and **FAILS LOUDLY IF IT IS
# NOT THERE.** A check that cannot find the thing it checks must FAIL, never pass: reporting success
# because zero fixtures ran is a green derived from zero evidence, which is the exact bug the fixtures on
# the other side of this call exist to prevent.


def load_test_module():
    if not TEST_PY.exists():
        fail(
            f"the fixture suite is NOT AT {TEST_PY} — `self-test` has NO SUBJECT, and a check that cannot "
            f"find the thing it tests must FAIL, never pass. Reporting success here would be a green "
            f"derived from zero evidence, which is the bug this suite exists to prevent"
        )
    spec = importlib.util.spec_from_file_location("ledger_test", TEST_PY)
    if spec is None or spec.loader is None:  # pragma: no cover - a broken checkout, not a verdict
        fail(f"cannot load the fixture suite at {TEST_PY} — refusing to report a self-test that never ran")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def self_test() -> int:
    """Run the sibling suite against THIS module. Exit 0 iff every rule this file claims actually holds."""
    tests = load_test_module()
    with tempfile.TemporaryDirectory() as tmpdir:
        return tests.run(sys.modules[__name__], Path(tmpdir))


# --- cli ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    # NOT `required=True`: `self-test` reads no ledger at all. Every OTHER subcommand does, and main()
    # enforces that through `parser.error` — the same message, usage line and exit 2 argparse itself
    # would have produced, so a forgotten --file fails exactly as loudly as it always did.
    parser.add_argument("--file", help="path to the ledger (state.jsonl)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("header", help="get/set a run-config header field")
    h.add_argument("action", choices=("get", "set"))
    h.add_argument("field")
    h.add_argument("value", nargs="?")

    def add_row_field_opts(p) -> None:
        for name in ROW_FIELDS:
            # pr is the row key (via --pr); id is derived; VERDICT_OWNED has no flag AT ALL, at either
            # door — `review_rounds` is the loop's memory, and the only way to guarantee nothing resets it
            # is to give nothing a way to write it (`settable`).
            if not settable(name):
                continue
            # Canonical flag is dash-form (--reviews-ok); accept the underscore
            # alias too. dest stays the underscore field name so getattr(args,
            # name) in cmd_set/cmd_add_row keeps working.
            opts = [f"--{name.replace('_', '-')}"]
            if "_" in name:
                opts.append(f"--{name}")
            p.add_argument(*opts, dest=name, help=f"row field '{name}'")

    a = sub.add_parser("add-row", help="append a new row for --pr")
    a.add_argument("--pr", required=True, help="PR number (row key)")
    add_row_field_opts(a)

    s = sub.add_parser("set", help="update named fields on the row for --pr")
    s.add_argument("--pr", required=True, help="PR number (row key)")
    add_row_field_opts(s)

    # The ONE door that records a review verdict — and the only writer of `review_rounds`/`ns_streak`.
    v = sub.add_parser("verdict", help="record ONE landed review verdict: bumps review_rounds, applies "
                                       "the tally, moves ns_streak — atomically. NEVER set reviews_ok by hand")
    v.add_argument("--pr", required=True, help="PR number (row key)")
    v.add_argument("--head-sha", dest="head_sha", required=True,
                   help="the SHA the review pass RAN on — must equal the row's head_sha, or the verdict "
                        "describes content that is no longer there and is refused")
    v.add_argument("--verdict", required=True, choices=VERDICTS,
                   help="the reviewer's VERDICT line, as the orchestrator read it")

    d = sub.add_parser("dispatch-check", help="may campaign ACT on this PR? run before every action that "
                                              f"MUTATES a PR; exits {EXIT_STOP} when the row is HELD")
    d.add_argument("--pr", required=True, help="PR number (row key)")
    d.add_argument("--action", choices=("ordinary", "repair"), default="ordinary",
                   help="'ordinary' (default) = any action that mutates the PR — review, fix, merge, "
                        "rebase, relabel. 'repair' = the reassessment's decided repair, the only work a "
                        "`repairing` row accepts (and only once its decision is recorded)")

    g = sub.add_parser("get", help="print the row for --pr as JSON, or one field")
    g.add_argument("--pr", required=True, help="PR number (row key)")
    g.add_argument("--field", help="print only this field")

    ls = sub.add_parser("list", help="print matching rows' pr numbers")
    ls.add_argument("--where", help="filter as <field>=<value>")

    t = sub.add_parser("table", help="print the run header and the live rows as an aligned table")
    t.add_argument("--fields", help=f"comma-separated row fields to show (default: {','.join(TABLE_DEFAULT_FIELDS)})")
    # The default hides rows; --all is how a reader gets the whole ledger back. The help text is derived
    # from TABLE_HIDDEN_STATUSES for the same reason hidden_notice() is: so it cannot drift from it.
    t.add_argument("--all", dest="show_all", action="store_true",
                   help=f"show every row (the default hides status={'/'.join(TABLE_HIDDEN_STATUSES)} "
                        f"and reports how many it hid)")

    sub.add_parser("self-test", help="run every fixture and assert the rules this file enforces still hold")
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "self-test":  # stdlib only, no ledger, no repo checkout, no network
        return self_test()
    if args.file is None:
        parser.error("the following arguments are required: --file")
    path = Path(args.file)
    handlers = {
        "header": cmd_header, "add-row": cmd_add_row, "set": cmd_set, "verdict": cmd_verdict,
        "get": cmd_get, "list": cmd_list, "table": cmd_table,
        "dispatch-check": cmd_dispatch_check,
    }
    return handlers[args.cmd](path, args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
