#!/usr/bin/env python3
"""Derive a PR's CI status: FETCH, PROMOTE, VERIFY, DECIDE — one command, one machine-readable verdict.

THE BUG THIS EXISTS TO KILL. `stage-2-ci.md` already forbids deriving CI from `gh pr checks`, already
specifies a SHA-pinned snapshot of BOTH check families, and `ci-snapshot.py` already executes every rule
that reads one. And yet, on a live run in this repo, the driver ran `gh pr checks <pr>`, read back a line
saying no checks were reported for that branch, and wrote `ci = green` into the ledger. ZERO EVIDENCE IS
NOT GREEN. Every rule was right and none of them ran, because the step that DERIVES the status was the one
step still performed by a model READING TERMINAL OUTPUT AND JUDGING IT BY EYE.

That is the gap this file closes, and it is a gap in MECHANISM, not in rules: the producer of the snapshot
was a shell block in a document. A block a human-shaped reader must transcribe — three `gh` calls, three
`jq` filters, a temp file, an atomic rename — is a block that reader will shortcut under load, and the
shortcut LOOKS like an answer. **The fix is that deriving CI is now a COMMAND, and eyeballing is not one of
the things it can do.**

    ci-status.py derive --pr 31 --head-sha <40-hex> --rundir <rundir> --required-set <spec>

prints a verdict as JSON and exits 0 ONLY on green. `gh pr checks` is never read: its `--json` surface
carries exactly ONE field (`bucket`) — no sha, no name, no conclusion — so it can never say WHICH COMMIT it
describes and can never be evidence. Use it to WAIT, never to decide.

WHAT WAS SUPPOSED TO BE THERE IS NOT A QUESTION THE EVIDENCE CAN ANSWER — SO IT IS AN INPUT (`--required-set`).
Every rule below quantifies over the rows we GOT. A REQUIRED CHECK THAT HAS NOT REGISTERED IS NO ROW AT ALL,
so no count, no marker and no cross-check can see it: they all agree, correctly, about a set that is missing
the one member that matters. The base branch's REQUIRED SET is the other half of the question, it is read
from branch protection AND rulesets (`stage-2-ci.md`, "WHAT WERE WE EXPECTING TO SEE?"), it is stored per ROW
(its base is per-row, so its required set is too — the header value is only the legacy fallback), and it is
passed in here — NAMED, with NO DEFAULT, because a caller who forgot must never be handed the permissive answer. `unknown` (the read FAILED) is a PENDING outcome that escalates; it can
NEVER go green. `ci-snapshot.py` owns the rule, this tool's job is to HAND IT THE SET — see `derive()`.

SCOPE — WHY THIS IS A SEPARATE FILE FROM `ci-snapshot.py`, AND NOT A SUBCOMMAND OF IT.
The split is PRODUCER vs VERIFIER, and it is the same two-independent-sources principle the snapshot
contract is built on:

  * `ci-snapshot.py` is a PURE, NETWORKLESS function from BYTES ON DISK to a verdict. That purity is what
    lets every one of its rules be pinned by an offline fixture and mutated by `mutate-ci-snapshot.py`.
  * A VERIFIER MUST NOT TRUST ITS PRODUCER, and the surest way to make it trust one is to make them the
    same function. This repo has already shipped the shape of that bug: a SHA check built out of the SHA we
    had stamped ourselves, which matched by construction and COULD NEVER FAIL.

So this file PRODUCES the artifact and then hands it to `ci-snapshot.py` — as a file, on disk, through the
exact same `evaluate()` a reviewer would run by hand. **NOT ONE CLASSIFICATION RULE, AND NOT ONE LINE OF
THE DECIDE ORDER, IS RE-IMPLEMENTED HERE.** They are IMPORTED. A second copy of `FAIL_CONCLUSIONS` in this
file would be a second owner of the rule, and the day they disagreed the tool would be lying in whichever
direction the reader did not check.

EVIDENCE WE KNOW IS INCOMPLETE IS NOT EVIDENCE. NOTHING HERE DISCLOSES A GAP AND GREENS ANYWAY.
This is the same false green as the one above, one level in: not "no evidence, called green", but "evidence
GitHub ITSELF told us was short, called green". Each of these now FAILS CLOSED:

  * **A SHORT READ.** Both REST families return GitHub's OWN `total_count` for the commit — the number of
    rows it holds, ACROSS PAGES. If the paginated read collected FEWER, a row GitHub holds is NOT IN OUR
    HANDS, and the row that is missing could be the FAILING one. `read_pages()` refuses. A count we cannot
    READ (absent, or not an integer) is refused for the same reason `headRefOid` is: a fail-closed rule that
    cannot fire is not a rule.

  * **A FIELD THAT IS NOT THERE IS NOT A FIELD THAT IS EMPTY.** `page.get(rows_key) or []`: delete a page's
    `statuses` member, leave `total_count: 0`, and every rule agreed — the count matched the rows collected,
    containment held — GREEN. Every field of a GitHub response comes through `field()`, which takes a SHAPE
    and never a default. `x // []` and `x or []` are the same bug in two languages: they erase the
    difference between "GitHub says there are none" and "GitHub said nothing at all".

  * **A ROLLUP `StatusContext`.** The rollup returns two entry types; this tool kept `CheckRun` and DROPPED
    the rest ON THE FLOOR. A `StatusContext` in state `EXPECTED` is *a REQUIRED status check that has not
    been posted yet* — and no VERDICT source can see it: the REST commit-status API has no `EXPECTED` state,
    so the family that carries status verdicts cannot express it, by construction. `build_snapshot()` now
    requires every rollup `StatusContext` to be VISIBLE in the REST status family, and refuses when one is
    not. An entry of a `__typename` we do not know is refused too, and so is a `StatusContext` whose `state`
    is not in the `StatusState` enum — that value NEVER ENTERS THE ARTIFACT, so no rule downstream can ever
    refuse it: it is refused HERE or it is accepted for good.

    **AND THAT COVERAGE RULE IS NOT WHAT CLOSES THE `EXPECTED` FALSE GREEN. IT CANNOT BE.** It quantifies
    over the `StatusContext` entries THE ROLLUP RETURNED, and the rollup carries NO total: unlike both REST
    families it cannot be proven complete (`ci-derivation-spec.md`, "Honest limits"). Delete the one `EXPECTED` entry
    from a rollup response and the guard has NOTHING TO CHECK, and the PR — blocked on a check nobody has run
    — goes GREEN. **A GUARD WHOSE INPUT CAN BE ABSENT NEVER FIRES.** The closure is the REQUIRED SET, above:
    it is DECLARED BY THE BASE BRANCH, so what must be present does not depend on what showed up. What the
    coverage rule is still FOR is stated at its own site (`build_snapshot`) — it is a CROSS-SOURCE
    consistency check, not the registration gap's closure, and it must never again be sold as one.

  * **AND TWO SOURCES THAT DISAGREE.** Every rule above asks whether a check EXISTS in the evidence. NONE of
    them asked whether the two sources SAY THE SAME THING ABOUT IT — so the tool believed whichever one it
    could parse, and it was GREEN for a PR the rollup was calling FAILED. THE REST FAMILIES ARE FETCHED
    BEFORE THE ROLLUP, so a check that flips to failure between the calls makes two HONEST sources contradict
    each other with the head never moving — no moved-head rule can fire on it. `build_snapshot()` REFUSES a
    conflict (`agree_or_refuse`), and it does NOT resolve it: not by preferring REST (it is first, not
    right), not by preferring the rollup (no oid — never a verdict), and never by taking the kinder of the
    two. Compared as BUCKETS, so `success` vs `SUCCESS` is not a conflict and `pending` vs `EXPECTED` is not
    either — see `status_bucket` / `checkrun_bucket`.

**THERE IS NO `notes` CHANNEL, ON PURPOSE.** A field that says "this evidence may be incomplete" BESIDE a
green verdict is the trapdoor, not the disclosure — it was read by nobody, and it let the tool ship the one
thing it exists to prevent. Every gap the tool can DETECT is a REFUSAL. What CANNOT be known is stated where
it belongs (`ci-derivation-spec.md`, "Honest limits"), never emitted as reassurance beside a verdict.

EVIDENCE ABOUT A COMMIT THAT IS NO LONGER THE HEAD IS NOT EVIDENCE ABOUT THE PR. The fetch is pinned to the
LEDGER's `head_sha`, and a push can land at any time — including WHILE this tool is fetching. So the tool
also reads the PR's CURRENT head, LAST (after both evidence families), and if it has MOVED the verdict is
`unusable`, NEVER green and never red: green would merge a PR on checks that never ran against its head, and
red would be a claim about the wrong commit too. The complete old-head artifact was already promoted and
stays on disk for audit, but the final result carries no current-PR fingerprint or buckets. `ci = pending`,
and the reason NAMES the new head so the driver re-derives against it rather than guessing. See `derive()`;
`stage-2-ci.md`, "A MOVED HEAD FAILS CLOSED", owns the result contract.

WHAT IT DOES NOT DECIDE, ON PURPOSE. `derive` answers **what the evidence says**, and **whether that
evidence is about this PR at all**. It does NOT answer **what the driver should DO** — dispatch a CI fix,
or prompt the user when a PR parks. Those ACTIONS live in `stage-2-ci.md`. ONE ROW OF THAT BOUNDARY MOVED
DELIBERATELY, and only that row: the watch policy ("WATCH ONLY WHAT CAN MOVE") reduces mechanically to a
single fact — IS A WATCH WARRANTED? — so `liveness` now EMITS that fact as `watch_warranted` instead of
leaving the driver to read the table by hand. What stays the driver's is the ACTION it triggers: LAUNCHING
the watch task, ensuring one is alive, relaunching an exited one. Encoding those actions here would create
a SECOND owner of a rule that is moving under it.

THE DOC AND THIS TOOL CANNOT SILENTLY DISAGREE — `doc-check` is what makes that true.
The enums, the CLASSIFY buckets and the DECIDE order are stated in `ci-derivation-spec.md` as prose AND encoded in
`ci-snapshot.py` as Python. NOTHING compared them, so they could drift, and the drifted copy would be the
one a reader believed. `doc-check` PARSES the doc's own enum block, its two CLASSIFY tables and its DECIDE
bullet order, and asserts they agree with the sets `ci-snapshot.py` actually classifies with — and that the
classification is TOTAL over the enums the doc declares. It also checks every copy of a `gh` command the doc
prints against the argv this code really issues, every copy of the derive command for `--required-set`, and
the moved-head owner block's retained-artifact/trust/result contract. Drift is a RED BUILD, not a discovery.

**A check that finds nothing MUST NOT PASS.** If the doc cannot be found, or a block cannot be parsed, or
zero rules are extracted, `doc-check` FAILS. An extractor that silently matches nothing and reports success
is the false green of this whole story, one level up, in the tool written to prevent it.

(The doc's three `gh … | jq` snapshot filters are EXECUTED by `ci-snapshot.py` over recorded, multi-page
API payloads. The required-set reads are production functions in this file and its sibling suite drives
them over their recorded API payloads.)

  required-set  read branch protection and rulesets, then persist their complete union in the ledger
  derive     fetch a PR's checks, promote the snapshot, verify it, and print the verdict as JSON
  doc-check  assert the CI docs (ci-derivation-spec.md + stage-2-ci.md) agree with the code that runs
  self-test  run every fixture, assert its verdict AND the rule that produced it, then run doc-check

THE FIXTURE SUITE IS THE SIBLING `ci-status-test.py`, and it is this tool's EXECUTABLE CONTRACT — every rule
below is pinned by a RECORDED API RESPONSE driven through the REAL producer, and most of them by a FALSE
GREEN this tool actually shipped. `self-test` loads it (by a `__file__`-relative path, never the cwd) and
FAILS LOUDLY if it is not there: a self-test that passes because it found no tests is this file's founding
defect, committed by this file.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NamedTuple, NoReturn
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
SNAPSHOT_PY = HERE / "ci-snapshot.py"
LEDGER_PY = HERE / "ledger.py"
TEST_PY = HERE / "ci-status-test.py"     # the fixture suite — this tool's executable contract
# The CI docs, split by audience: the SPEC (what the tools implement — enums, CLASSIFY, DECIDE, the
# fetch commands) and the DRIVER doc (the commands a heartbeat runs, the caps, the fingerprint block).
# `doc-check` reads BOTH; each element is parsed from the file that owns it.
SPEC_DOC = HERE.parent / "references" / "ci-derivation-spec.md"
DRIVER_DOC = HERE.parent / "references" / "stage-2-ci.md"
FIXTURES = HERE / "fixtures" / "ci-status"

# A git object id, as GitHub returns it: 40 LOWERCASE hex. Same rule, same reason, as `ci-snapshot.py` —
# a `--head-sha` of any other shape makes every comparison downstream unfalsifiable, so it is an OPERATOR
# ERROR (exit 2), never a verdict.
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# "this source's response carried no commit oid" — the artifact's word for it, never a sha we made up.
NO_OID = "-"


def load_snapshot_module():
    """Import `ci-snapshot.py`. Its name has a hyphen, so it is not importable as a module path.

    THE VERDICT RULES HAVE EXACTLY ONE OWNER AND IT IS THAT FILE. Everything this script knows about what
    counts as a pass, a failure, a running check, an unrecognised value, or the order those are tested in,
    it knows by CALLING that module. If this import is ever replaced by a local copy of its constants, the
    two will drift and the tool will be confidently wrong.
    """
    spec = importlib.util.spec_from_file_location("ci_snapshot", SNAPSHOT_PY)
    if spec is None or spec.loader is None:  # pragma: no cover - a broken checkout, not a verdict
        fail(f"cannot load {SNAPSHOT_PY} — the verdict rules live there; refusing to guess them")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SNAP = load_snapshot_module()


def load_ledger_module():
    """Import the schema-owning ledger accessor instead of copying its file format or write rules."""
    spec = importlib.util.spec_from_file_location("campaign_ledger_for_ci", LEDGER_PY)
    if spec is None or spec.loader is None:  # pragma: no cover - a broken checkout, not a verdict
        fail(f"cannot load {LEDGER_PY} — required-set persistence belongs to that accessor")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LEDGER = load_ledger_module()

# `StatusState`, as the ROLLUP hands it to us — and it is NOT a fourth copy of those values, it is the UNION
# OF THE THREE BUCKETS `ci-snapshot.py` CLASSIFIES WITH. That file owns the enum; this file only asks whether
# a value it was handed is IN it. `doc-check` asserts that same union equals the `StatusState` enum the doc
# declares (`StatusState is TOTAL`), so all three copies are held together mechanically and a value GitHub
# adds tomorrow is OUTSIDE this set — which is the point: it is REFUSED, never quietly accepted.
STATUS_STATES = SNAP.STATUS_PASS | SNAP.STATUS_RUNNING | SNAP.STATUS_FAIL

# --- THE BUCKETS, and they are NOT a fourth copy of the vocabulary either -------------------------
#
# THE TWO SOURCES SPELL THE SAME FACT DIFFERENTLY. REST returns `success` / `failure` / `pending`; the rollup
# returns `SUCCESS` / `FAILURE` / `PENDING` / `EXPECTED` / `ERROR`, and for a check run it returns a `status`
# AND a `conclusion` where REST returns a lowercase pair of the same two. A cross-source comparison written
# on the RAW VALUES would therefore report a DISAGREEMENT for every honest PR on Earth (`success` != `SUCCESS`),
# and a rule that wedges honest input gets deleted by the next person in a hurry.
#
# So both sides are mapped THROUGH THE BUCKETS `ci-snapshot.py` ALREADY OWNS — the same sets `decide()`
# classifies with, imported, never restated. `up()` has already removed the spelling difference; these
# functions remove the VOCABULARY difference (REST `pending` and rollup `EXPECTED` are both RUNNING, and a
# check run's two fields collapse to the one bucket `decide()` would put it in).
#
# **`UNKNOWN_VALUE` IS A BUCKET, NOT AN ERROR** — and that is the load-bearing design choice here. A value
# NEITHER source's enum contains is a value nobody has classified; two sources are IN AGREEMENT about it only
# if BOTH of them said it, and then `ci-snapshot.py`'s catch-all owns it downstream (it escalates, and it
# CANNOT go green). What must never happen is one source saying a value we KNOW (`success`) while the other
# says one we do NOT — that is a bucket difference, it is REFUSED, and it is exactly the shape a reviewer
# demonstrated with an invented `BRAND_NEW_FAILURE`.
PASS, RUNNING, FAIL, UNKNOWN_VALUE = "PASS", "RUNNING", "FAIL", "an UNRECOGNISED value"

# The JSON spelling of each bucket — `UNKNOWN_VALUE`'s prose spelling ("an UNRECOGNISED value") is built
# for `reason` strings, not for a key a driver programs against.
BUCKET_KEYS = {PASS: "PASS", RUNNING: "RUNNING", FAIL: "FAIL", UNKNOWN_VALUE: "UNKNOWN_VALUE"}

# --- the liveness caps -----------------------------------------------------------------------------
#
# Each VALUE's one defining site is `stage-2-ci.md` — "SETTLED" (the derivation block) for the STRIKE CAP,
# "UNUSABLE — the refetch is BOUNDED" for the REFETCH CAP, "THE CI STALL CAP = 6h" for the stall bound —
# and `doc-check` compares these constants against those sites, so the doc and the tool cannot drift.
# `liveness()` below is what fires them.
STRIKE_CAP = 2
REFETCH_CAP = 3
STALL_CAP_HOURS = 6


def status_bucket(state: object) -> str:
    """A commit status's `state` -> the bucket `ci-snapshot.decide()` would put it in."""
    if state in SNAP.STATUS_PASS:
        return PASS
    if state in SNAP.STATUS_RUNNING:
        return RUNNING
    if state in SNAP.STATUS_FAIL:
        return FAIL
    return UNKNOWN_VALUE


def checkrun_bucket(status: object, conclusion: object) -> str:
    """A check run's (`status`, `conclusion`) -> the same three buckets, by `ci-snapshot.decide()`'s rule:
    a run that is not COMPLETED is judged on its STATUS, and a COMPLETED one on its CONCLUSION.

    **A FIELD THE ROLLUP DID NOT SEND LANDS HERE AS `UNKNOWN_VALUE`, ON PURPOSE.** `gh pr view --json
    statusCheckRollup` returns `status` and `conclusion` on every `CheckRun` entry (verified live), so their
    ABSENCE is a response we do not understand — and absence must never be the thing that quietly switches a
    guard off. It is not read as "no opinion"; it is a value no bucket holds, and it CONTRADICTS a REST twin
    that classifies. A guard whose input can go missing is a guard that never fires.
    """
    if status in SNAP.RUNNING_STATUSES:
        return RUNNING
    if status == SNAP.TERMINAL_STATUS:
        if conclusion in SNAP.PASS_CONCLUSIONS:
            return PASS
        if conclusion in SNAP.FAIL_CONCLUSIONS:
            return FAIL
    return UNKNOWN_VALUE


def bucket_counts(rows: list[dict]) -> dict[str, int]:
    """Every EVIDENCE row's CLASSIFY bucket, tallied — all four keys always present.

    This is what lets the driver and `liveness()` answer "can any row still move?" (`RUNNING > 0` — the
    watch policy and the SETTLED/RUNNING-STALL split both read exactly that) WITHOUT hand-classifying
    snapshot rows, which is the by-eye judgment this tool exists to remove.
    """
    counts = {key: 0 for key in BUCKET_KEYS.values()}
    for row in rows:
        if row["row"] == "checkrun":
            counts[BUCKET_KEYS[checkrun_bucket(row["status"], row["conclusion"])]] += 1
        elif row["row"] == "status":
            counts[BUCKET_KEYS[status_bucket(row["state"])]] += 1
    return counts


# The LEDGER's `ci` column is a THREE-VALUE enum (`green`/`red`/`pending` — `files-and-ledger.md`), while
# DECIDE has SIX outcomes. So the mapping is LOSSY, and that is exactly why this tool emits BOTH: `ci` (what
# the driver writes to the ledger) and `verdict` (what the evidence actually said). Collapsing them into one
# field is how "unusable" and "an enum value nobody has ever classified" would come to be recorded as the
# same bland `pending` and lose the thing that made them worth escalating.
#
# EVERY NON-GREEN, NON-RED OUTCOME IS `pending`, and the doc says so outcome by outcome: UNUSABLE ->
# "`ci = pending`, refetch"; containment unprovable -> "`ci = pending`"; UNKNOWN_VALUE -> escalate, and an
# unrecognised value is by definition neither a pass nor a failure, so of the three legal column values only
# `pending` is left. NEVER let an unmapped verdict fall through to a default here: an outcome this table does
# not name is one nobody has thought about, and guessing `pending` for it is the same "close enough" that
# greened a commit with no checks on it. It is an OPERATOR ERROR, loudly (`ledger_ci`).
LEDGER_CI = {
    SNAP.GREEN: "green",
    SNAP.RED: "red",
    SNAP.PENDING: "pending",
    SNAP.UNUSABLE: "pending",
    SNAP.UNVERIFIABLE: "pending",
    SNAP.UNCLASSIFIED: "pending",
}


def ledger_ci(verdict: str) -> str | None:
    """The ledger column this verdict maps to — or None, which `result()` refuses rather than guesses."""
    return LEDGER_CI.get(verdict)


# The DECIDE order, as a NAME PER BULLET, in the order `ci-derivation-spec.md` evaluates them. This is a THIRD
# statement of an order that is already owned twice (the doc's bullets; `ci-snapshot.decide()`'s branches),
# and it is only allowed to exist because it is MECHANICALLY CHECKED AGAINST BOTH:
#
#   * against the DOC, textually — `doc-check` parses the DECIDE bullets and asserts this exact sequence;
#   * against the CODE, BEHAVIOURALLY — the `*-outranks-*` fixtures drive real evidence through the real
#     `ci-snapshot.decide()` and assert the precedence holds at run time, which no amount of reading either
#     copy could establish.
#
# A copy that is checked against both owners is a PIVOT. A copy that is merely written down beside them is
# the stale restatement this repo keeps killing. Delete either check and this becomes the latter.
#
# THE TWO REQUIRED-SET BULLETS ARE THE LAST TWO BEFORE `green`, AND THEIR POSITION IS THE POINT: they are
# the questions no ROW can answer, so they are asked once every row has already passed. `required-set-unreadable.json`
# and `required-check-absent.json` drive them behaviourally; this line is what pins that the DOC still
# evaluates them in that order.
DECIDE_ORDER = ("UNUSABLE", "red", "UNKNOWN_VALUE", "pending", "pending (nothing registered)",
                "pending (required set unreadable)", "pending (required check missing)", "green")


class FetchError(Exception):
    """A source could not be read. The snapshot is NOT promoted and there is NO verdict from evidence."""


def fail(msg: str) -> NoReturn:
    print(f"ci-status: {msg}", file=sys.stderr)
    raise SystemExit(2)


# --- the OPERATOR-ERROR guards: functions, so that ONE owner is both CALLED by `main` and DRIVEN by the
# suite. Inline in `main()` they were reachable ONLY through the CLI — which no fixture goes through — so
# both were pinned by NOTHING. `seam_cases` drives them.

def check_head_sha(head_sha: str) -> str:
    """An OPERATOR ERROR is not a verdict about the PR. A `--head-sha` that is not a git object id makes
    every comparison downstream unfalsifiable, and blaming the EVIDENCE for the caller's mistake is how a
    tool reports a defect that is not there. Exit 2: no verdict at all beats a verdict about the wrong
    question."""
    if not SHA_RE.match(head_sha):
        fail(f"--head-sha {head_sha!r} is not a git object id (40 LOWERCASE hex) — refusing to derive")
    return head_sha


def check_rundir(rundir: Path) -> Path:
    """`promote()` writes the artifact INTO <rundir>; a rundir that does not exist is a caller mistake, and
    it must be named as one BEFORE any fetch — not surface later as a crash in the middle of promotion,
    where it would look like a defect in the evidence."""
    if not rundir.is_dir():
        fail(f"--rundir {rundir} is not a directory")
    return rundir


def check_required_set(spec: str):
    """The base branch's required set, as the ledger holds it (`declared:<json>` | `none` | `unknown`).

    ONE PARSER, AND IT IS `ci-snapshot.py`'s — the same object `decide()` reads, so this tool cannot hold a
    second opinion about what "required" means. It is called from `main` BEFORE the fetch, with the other
    operator-error guards: a spec we cannot read is the CALLER'S mistake, and naming it as one beats
    surfacing it later as a crash, or (far worse) as a verdict about the PR.

    A SPEC WE CANNOT PARSE IS EXIT 2, NEVER `none`. Degrading it would announce "the base branch requires
    nothing" on the strength of a value we just failed to read — rebuilding the exact false green the
    required set exists to remove, one layer down.
    """
    try:
        return SNAP.parse_required_set(spec)
    except SNAP.SpecError as exc:
        fail(
            f"--required-set {spec!r} cannot be read ({exc}) — and a spec we cannot read is NOT `none`. "
            f"It is the row's `effective_required_set` (its explicit `required_set`, else the legacy header "
            f"value), and guessing at it would say 'the base branch requires nothing' on the strength of a "
            f"value we failed to parse."
        )


def resolve_required_for_derive(ledger_file: str, pr: str, required_set_arg: "str | None"):
    """Resolve the required set `derive` decides under from the ledger ROW, not the header.

    The set is a property of the base, and the base is per-ROW state, so the value is the selected row's
    `effective_required_set` — its explicit `required_set`, else the legacy header value, through the ONE
    schema-owned accessor (never a second copy of that fallback). Any `--required-set` given ALONGSIDE
    `--ledger` becomes an ASSERTION that must equal it — the same "argument is an assertion, not a second
    source of truth" contract base-preflight uses for `--base`. Fails CLOSED on a missing row or a malformed
    value; `unknown` parses through as its fail-closed self (it can NEVER green — a `pending` bullet in DECIDE).
    """
    header, rows = LEDGER.load(Path(ledger_file))
    row = LEDGER.find_row(rows, str(pr))
    if row is None:
        fail(f"derive: no ledger row for pr {pr} — its required set cannot be resolved")
    spec = LEDGER.effective_required_set(header, row)
    if required_set_arg is not None and required_set_arg != spec:
        fail(
            f"derive: --required-set {required_set_arg!r} disagrees with pr {pr}'s effective required set "
            f"{spec!r} — with --ledger, --required-set is an ASSERTION, not a second source of truth"
        )
    return check_required_set(spec)


# --- REQUIRED SET -------------------------------------------------------------------------------

def required_check(source: str, value: object, app_key: str) -> tuple[str, str]:
    """Parse one GitHub required-check declaration without turning absence into an empty value."""
    context = field(source, value, "context", str)
    app = field(source, value, app_key, int, NULL, ABSENT)
    if app is None:
        app = SNAP.ANY_APP
    else:
        app = str(app)
    return context, app


def classic_required_set(payload: object) -> list[tuple[str, str]]:
    """Read classic branch protection from the complete branch object."""
    source = "required-set-classic"
    protection = field(source, payload, "protection", dict)
    enabled = field(source, protection, "enabled", bool)
    if not enabled:
        return []
    status_checks = field(source, protection, "required_status_checks", dict)
    checks = field(source, status_checks, "checks", list)
    return [required_check(source, check, "app_id") for check in checks]


def ruleset_required_set(payload: object) -> list[tuple[str, str]]:
    """Read every page returned by `gh api --paginate --slurp` for rules on the base branch."""
    source = "required-set-ruleset"
    if not isinstance(payload, list):
        raise FetchError(f"{source}: the paginated response is not a list of pages")
    checks: list[tuple[str, str]] = []
    for page in payload:
        if not isinstance(page, list):
            raise FetchError(f"{source}: a page is not a list of rules: {page!r}")
        for rule in page:
            rule_type = field(source, rule, "type", str)
            if rule_type != "required_status_checks":
                continue
            parameters = field(source, rule, "parameters", dict)
            declared = field(source, parameters, "required_status_checks", list)
            checks.extend(required_check(source, check, "integration_id") for check in declared)
    return checks


def canonical_required_set(checks: list[tuple[str, str]]) -> str:
    """Return the one ledger spelling for a complete union of both declaration sources."""
    unique = sorted(set(checks))
    if not unique:
        return SNAP.NONE_DECLARED
    payload = [{"context": context, "app": app} for context, app in unique]
    spec = SNAP.DECLARED_PREFIX + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    SNAP.parse_required_set(spec)  # the verifier's parser has the final word on what may be persisted
    return spec


def fetch_required_set(fetch: Fetch, repo: str, base_branch: str) -> tuple[str, str]:
    """Fetch both mandatory declaration sources. Any incomplete read stays `unknown`."""
    scoped = repo_scoped(fetch, repo)
    encoded = quote(base_branch, safe="")
    try:
        classic = scoped(
            "required-set-classic",
            ["gh", "api", f"repos/{REPO_SLOT}/branches/{encoded}"],
        )
        rules = scoped(
            "required-set-ruleset",
            ["gh", "api", "--paginate", "--slurp", f"repos/{REPO_SLOT}/rules/branches/{encoded}"],
        )
        value = canonical_required_set(classic_required_set(classic) + ruleset_required_set(rules))
        return value, "both required-check sources were read completely"
    except (FetchError, SNAP.SpecError) as exc:
        return SNAP.CANNOT_READ, str(exc)


# The two TERMINAL row statuses — ledger.py's park guard names them (`merged`/`aborted`). A terminal row's
# required set is frozen with its campaign record and is never re-read; the grouped refresh below quantifies
# over NONTERMINAL rows only.
TERMINAL_STATUSES = ("merged", "aborted")


def _read_needed(spec: str) -> bool:
    """Does this required-set spec still need a (re)read? `unknown` does, and so does a value we cannot even
    parse (forced to re-read rather than trusted — the same fail-closed stance the header path always took).
    `declared:<json>` and `none` are SETTLED reads and are never re-read: the head-SHA liveness checks are
    the freshness boundary, NOT a policy-revision field (stage-2-ci.md, "WHAT WERE WE EXPECTING TO SEE?")."""
    try:
        return SNAP.parse_required_set(spec).state == SNAP.CANNOT_READ
    except SNAP.SpecError:
        return True


def refresh_required_set(fetch: Fetch, ledger_path: Path, repo: str | None = None) -> dict:
    """Settle the required-check set PER BASE, retrying only the groups whose value is still `unknown`.

    Required checks are a property of the base, and the base is per-ROW state, so the required set is too.
    This GROUPS the nonterminal rows by `effective_base`, reads each DISTINCT base's requirements ONCE from
    GitHub, and writes the canonical result to that base's rows STILL NEEDING one (`ledger.py`'s writer,
    never a second copy of the file format). A row whose stored value is already settled is NEVER
    overwritten — by a failed read or a fresh one; an unsettled row joining a settled base adopts the
    group's settled value with no new read. A read that fails leaves ONLY that base's unsettled rows
    `unknown` (fail-closed, never `none`); other groups settle independently. Legacy `-` rows carry no explicit base or set: they inherit the HEADER
    through `effective_required_set`, which this settles as its own (legacy) channel — so an old single-base
    ledger, down to a zero-row one, keeps working exactly as before and NO inherited row value is materialized.
    """
    header, rows = LEDGER.load(ledger_path)
    nonterminal = [r for r in rows if r.get("status") not in TERMINAL_STATUSES]

    # The header MUST parse before anything overwrites it — a malformed value is a hand-edit to fail on, not
    # to clobber (preserves the old header-path refusal).
    header_spec = header["required_set"]
    try:
        SNAP.parse_required_set(header_spec)
    except SNAP.SpecError as exc:
        fail(f"ledger required_set {header_spec!r} is malformed ({exc}); refusing to overwrite it")

    # Explicit-base rows STORE their own `required_set`; group them by that base. Legacy `-` rows inherit the
    # header, so they are NOT grouped here — the header channel below is their storage.
    explicit_groups: "dict[str, list[dict]]" = {}
    has_legacy = False
    for row in nonterminal:
        if row.get("base_branch", LEDGER.ROW_DEFAULTS["base_branch"]) == "-":
            has_legacy = True
            continue
        explicit_groups.setdefault(LEDGER.effective_base(header, row), []).append(row)

    # One GitHub read per DISTINCT base, memoized so a base shared by the header channel and an explicit group
    # is read at most once. Repo is resolved LAZILY — only if some group actually needs a read.
    reads: "dict[str, tuple[str, str]]" = {}
    state: dict = {"repo": repo}

    def read_base(base: str) -> "tuple[str, str]":
        if base in reads:
            return reads[base]
        if state["repo"] is None:
            try:
                state["repo"] = resolve_repo(fetch)
            except FetchError as exc:
                fail(f"cannot determine the repo ({exc}) — pass --repo owner/name")
        value = fetch_required_set(fetch, state["repo"], base)
        reads[base] = value
        return value

    groups_out: list[dict] = []
    dirty = False
    header_base = header.get("base_branch", LEDGER.HEADER_DEFAULTS["base_branch"])

    # THE HEADER / LEGACY CHANNEL. Settle the header when a legacy `-` row inherits it (or there are no rows at
    # all — the compatibility case of a header-only ledger) and its value is still `unknown`. A new run's
    # header base is `-`, so this is skipped and the row groups own the read.
    if (has_legacy or not nonterminal) and _read_needed(header_spec):
        if not header_base or header_base == "-":
            fail("ledger base_branch is not set; required checks cannot be read without it")
        value, reason = read_base(header_base)
        header["required_set"] = value
        dirty = True
        parsed = SNAP.parse_required_set(value)
        groups_out.append({"base": header_base, "storage": "header", "required_set": value,
                           "state": parsed.state, "settled": parsed.state != SNAP.CANNOT_READ,
                           "reason": reason})

    # EXPLICIT-BASE ROW GROUPS. Act on a group when any row's OWN stored `required_set` is unsettled — the
    # row STORES its base's set (an explicit `-` row has simply not had ITS base read yet: `_read_needed("-")`
    # is True). A SETTLED value for the group's base is NEVER overwritten — not by a failed read (which would
    # clobber it with `unknown`) and not by a fresh read (settled reads are never re-read: `_read_needed`). So
    # when the group's base already holds exactly one settled value, the unsettled rows ADOPT it with no
    # GitHub read; only a group with no settled value (or with disagreeing settled values — a hand-edit this
    # never papers over) reads GitHub, and the result lands ONLY on the rows that needed it. A base that is
    # unusable (blank/`-`) cannot be read — leave the group `unknown` rather than fabricate a permissive answer.
    #
    # "THE SETTLED SET FOR BASE B" HAS TWO STORAGE CHANNELS, and they are ONE fact: each group row's OWN
    # settled value, AND the header value WHEN B IS the header base and the header is itself settled. The
    # header describes base B only for B == header base (a legacy single-base run, or a migrated one whose
    # header settled before a new same-base row joined), so it is unioned in ONLY then — never for a DIFFERENT
    # base, which would settle a base off another base's set (a false green). Folding it in is what makes a
    # `-` row on the header base ADOPT the header's settled value with no read, and what stops a FAILED read
    # from clobbering a value already settled through the header channel — the round-1 row-adoption rule,
    # extended to the header's copy of the same fact. `effective_required_set` (ledger.py) draws the SAME
    # base-agreement line for the single-row accessor, so the two never disagree.
    for base, group_rows in explicit_groups.items():
        unsettled = []
        settled_values = set()
        for r in group_rows:
            spec = r.get("required_set", LEDGER.ROW_DEFAULTS["required_set"])
            if _read_needed(spec):
                unsettled.append(r)
            else:
                settled_values.add(spec)
        if base == header_base and not _read_needed(header_spec):
            settled_values.add(header_spec)
        if not unsettled:
            continue
        prs = [r["pr"] for r in group_rows]
        if not base or base == "-":
            groups_out.append({"base": base, "storage": "rows", "prs": prs,
                               "required_set": SNAP.CANNOT_READ, "state": SNAP.CANNOT_READ,
                               "settled": False, "reason": f"pr(s) {prs} have no usable base to read"})
            continue
        if len(settled_values) == 1:
            value = next(iter(settled_values))
            reason = "adopted the group's settled value; settled reads are never re-read"
        else:
            value, reason = read_base(base)
        for r in unsettled:
            r["required_set"] = value
        dirty = True
        parsed = SNAP.parse_required_set(value)
        groups_out.append({"base": base, "storage": "rows", "prs": prs, "required_set": value,
                           "state": parsed.state, "settled": parsed.state != SNAP.CANNOT_READ,
                           "reason": reason})

    if dirty:
        LEDGER.dump(ledger_path, header, rows)

    settled = all(g["settled"] for g in groups_out)
    result = {
        "repo": state["repo"],
        "base_branch": header_base,
        "required_set": header["required_set"],
        "groups": groups_out,
        "settled": settled,
        "reason": ("no group needed a read; every nonterminal row's required set is already settled"
                   if not groups_out
                   else f"settled {sum(g['settled'] for g in groups_out)} of {len(groups_out)} base group(s)"),
    }

    # SINGLE-BASE TOP-LEVEL CONTRACT. When the whole run resolves to ONE effective base, restore the pre-PR
    # top-level summary: `base_branch`/`required_set`/`state` describe that one base (the settled value, not the
    # header's stale `unknown`), and the `state` key is present. This is the promise "single-base runs stay
    # behaviorally unchanged" — it must hold for a NEW explicit-base row too, not only legacy `-` rows. A
    # MIXED-base run has no single base to summarize, so it keeps `groups` as the signal and omits `state`.
    effective_bases = set(explicit_groups)
    if has_legacy or not nonterminal:
        effective_bases.add(header_base)
    if len(effective_bases) == 1:
        base = next(iter(effective_bases))
        acted = next((g for g in groups_out if g["base"] == base), None)
        if acted is not None:
            value, base_state = acted["required_set"], acted["state"]
        else:
            # Fully settled already (no read this call): read the settled value from its storage — the explicit
            # rows' own value, or the header for a legacy / row-less ledger.
            group_rows = explicit_groups.get(base)
            value = group_rows[0]["required_set"] if group_rows else header["required_set"]
            base_state = SNAP.parse_required_set(value).state
        result["base_branch"] = base
        result["required_set"] = value
        result["state"] = base_state

    return result


# --- FETCH ---------------------------------------------------------------------------------------
#
# Every fetch goes through ONE seam, `Fetch = (source, argv) -> parsed JSON`, so the fixtures can drive the
# whole producer with RECORDED API RESPONSES and no network. The seam is the source of the fixtures' power:
# the code under test below is the SAME code that runs against GitHub, not a re-implementation of it.

Fetch = Callable[[str, "list[str]"], object]

# --- WHICH REPOSITORY IS THIS PR IN? ONE ANSWER, ONE PLACE, AND EVERY FETCH GOES THROUGH IT --------
#
# **A FLAG THE PARSER ACCEPTS AND THE WORK IGNORES IS A LIE.** `--repo` was honoured by the two REST fetches
# (which interpolate it into the URL) and SILENTLY DROPPED by the rollup, which ran `gh pr view <pr>` — a
# command that resolves the PR IN THE CURRENT CHECKOUT. So the tool worked only against the repository you
# happened to be standing in: `derive --repo cli/cli --pr 13842` came back `unusable` ("Could not resolve to
# a PullRequest"), and the flag that said otherwise was a lie. It failed CLOSED, which is why it was never a
# false green — but a tool must USE what it ACCEPTS, or REFUSE it.
#
# Fixing the one argv would fix the INSTANCE. This fixes the CLASS: **no fetcher is handed the repo at all.**
# A fetcher writes `REPO_SLOT` where the repository goes, and `repo_scoped()` — the ONE wrapper every fetch
# passes through — substitutes it and then REFUSES any argv that does not name the repo. A new fetcher that
# forgets is not "unlikely to happen"; it CANNOT reach GitHub, because the seam it must go through will not
# let an unscoped command past.
REPO_SLOT = "{repo}"


def is_endpoint(repo: str, arg: str) -> bool:
    """Does THIS ONE ARGUMENT — already known to be the endpoint POSITION — name `repo`? A PATH, or the full
    URL of one.

    It is a string test, and a string test is exactly what was spoofable. What makes it sound is that it is
    asked of ONE argument, the one `gh api` will actually use as the path (`api_endpoint`), and of no other.
    Ask it of `any(...)` over the argv and it is the spoof again: see `require_repo_scoped`.
    """
    return (arg.startswith(f"repos/{repo}/") or arg.startswith(f"/repos/{repo}/")
            or f"//api.github.com/repos/{repo}/" in arg)


# The ONLY words this tool may write between `api` and the ENDPOINT — and BOTH TAKE NO VALUE, which is the
# entire reason a fixed set is safe. A flag that takes a value (`-H`, `-f`, `-F`, `-q`/`--jq`, `--template`,
# `-X`) puts a word after itself that is NOT the endpoint, and a parser that has to guess which flags do that
# is a parser that can be handed a repo-shaped value where the path belongs. So this one does not guess: any
# other word before the endpoint means the endpoint's position CANNOT BE IDENTIFIED, and that is a REFUSAL.
API_VALUELESS_FLAGS = ("--paginate", "--slurp")


def api_endpoint(argv: list[str]) -> str | None:
    """THE ENDPOINT OF A `gh api` CALL, BY POSITION — the operand `gh` will use as the path.

    `gh api [flags] <endpoint> [flags]`: the endpoint is the FIRST OPERAND, i.e. the first word after `api`
    that is not one of the valueless flags this tool writes ahead of it (`API_VALUELESS_FLAGS`).

    It returns None — and None REFUSES, upstream — in every case where that position is not identifiable:

      * the command is not `gh api` at all (that repository is named by `--repo`, not by a path);
      * there is no operand after `api`;
      * a word before the endpoint is a flag this tool does not write. It MIGHT take a value, and then the
        word after it is that value and not the path — so the parse FAILS CLOSED rather than picking one.
    """
    if argv[:2] != ["gh", "api"]:
        return None
    for word in argv[2:]:
        if word in API_VALUELESS_FLAGS:
            continue
        return None if word.startswith("-") else word
    return None


def require_repo_scoped(source: str, repo: str, argv: list[str]) -> list[str]:
    """EVERY GitHub call NAMES THE REPOSITORY IT IS ABOUT, in one of the only two ways `gh` has of saying it:

      * `gh api` — the repository is the ENDPOINT (`repos/<owner>/<name>/…`, or the full URL of it);
      * every other subcommand — the repository is the value of `--repo`, i.e. the word RIGHT AFTER it.

    **AND WHICH OF THE TWO IS LEGAL IS DECIDED BY THE SUBCOMMAND, NOT BY WHERE A STRING TURNS UP.** The
    subcommand picks the form, and the OTHER form is then no defence: a `gh api` whose endpoint names the
    wrong repository is refused even if `--repo <right>` is somewhere in the argv (`gh api` does not have a
    `--repo`; it would be a flag the command ignores, which is this file's oldest defect wearing a hat).

    **A GUARD THAT ACCEPTS A STRING WHERE IT MEANS A POSITION CAN BE FED THE STRING.** This guard has been
    that guard twice: it asked whether the repo's name appeared ANYWHERE in the argv, so
    `gh pr view 35 --template repos/o/r/x` SATISFIED it — the repository was named, in a flag that scopes
    NOTHING, and the command still resolved against whatever checkout the process was standing in. BOTH
    halves name a POSITION now: the endpoint `gh` will actually request (`api_endpoint`), and the word right
    after `--repo`. A repo-shaped string anywhere else — `--jq`, `-f`, `-F`, a header, a template — scopes
    nothing and satisfies nothing.
    """
    if argv[:2] == ["gh", "api"]:
        endpoint = api_endpoint(argv)  # the POSITION `gh` will request
        scoped = endpoint is not None and is_endpoint(repo, endpoint)
    else:
        scoped = any(flag == "--repo" and name == repo for flag, name in zip(argv, argv[1:]))
    if not scoped:
        raise FetchError(
            f"{source}: `{' '.join(argv)}` is NOT scoped to {repo!r} — it would resolve against whatever "
            f"checkout this process is standing in, which is not the repository the caller named. A `gh api` "
            f"call carries the repo in the ENDPOINT it requests (`repos/{repo}/…`, the first operand after "
            f"`api`); every other `gh` subcommand carries it as `--repo {repo}`, in the word right after the "
            f"flag. NOWHERE ELSE COUNTS: a repo-shaped value in a `--template`, a `--jq`, a field or a header "
            f"scopes nothing, and the command still asks the wrong repository. A fetch that cannot say WHICH "
            f"repository it is about is not evidence about this PR — and a flag this tool ACCEPTS it must "
            f"USE, or REFUSE."
        )
    return argv


def repo_scoped(fetch: Fetch, repo: str) -> Fetch:
    """THE ONE PLACE THE REPOSITORY ENTERS A `gh` COMMAND. Wrapped ONCE, in `build_snapshot`, around the seam
    every fetcher already goes through — so a fetcher cannot opt out of it, and cannot be handed the repo by
    any other route: none of them takes it as an argument any more.
    """
    def scoped(source: str, argv: list[str]) -> object:
        return fetch(source, require_repo_scoped(source, repo, [a.replace(REPO_SLOT, repo) for a in argv]))
    return scoped


def gh_fetch(source: str, argv: list[str]) -> object:
    """Run `gh` and parse its stdout as JSON.

    A NON-ZERO EXIT IS A FAILED FETCH, FULL STOP. The doc's shell version needs `set -o pipefail` to learn
    this, because a dead `gh` piped into `jq` yields an EMPTY stdin, and `jq` then prints nothing and exits
    0 — the fetch failed and the shell called it success. There is no pipeline here, and the exit status is
    checked directly, which is the same rule with nothing left to forget.

    **THIS FUNCTION IS THE ONE SEAM THE FIXTURES REPLACE, WHICH IS WHY BOTH ITS RULES WERE PINNED BY
    NOTHING.** Every fixture drives the producer through `fixture_fetch`, so nothing in the suite ever
    executed these two lines. They are driven now, by `seam_cases`, against a LOCAL PROCESS (no network): a
    command that prints valid JSON and exits 1, and one that prints garbage and exits 0. A rule on the only
    code path that talks to GitHub is the last rule that may go untested.
    """
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
    if proc.returncode != 0:
        raise FetchError(f"{source}: `{' '.join(argv[:3])} …` exited {proc.returncode}: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise FetchError(f"{source}: response is not JSON ({exc})") from exc


def s(value: object) -> object:
    """Every field in the artifact's table is a STRING. Numbers become strings; ANYTHING ELSE IS LEFT AS IT
    IS, ON PURPOSE.

    The temptation is to coerce whatever GitHub sent into `str()` so the file always parses. **That is the
    bug, not the fix.** A `None` where a check-run NAME should be is a response we do not understand, and
    `str(None)` would launder it into the perfectly parseable string `"None"` — evidence we INVENTED, sitting
    in the file, describing a check that does not exist. Left alone it lands in the artifact as `null`,
    `ci-snapshot.py` rejects the row, and the snapshot comes back UNUSABLE. Failing closed on a response we
    cannot read is the whole point; a producer that can always produce a parseable file has only moved the
    lie somewhere the verifier cannot see it.
    """
    return str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value


# --- ONE DOOR FOR EVERY FIELD READ OFF A GITHUB RESPONSE -------------------------------------------
#
# **`x or []` — THE IDIOM THAT ERASES THE DIFFERENCE BETWEEN "GITHUB SAID THERE ARE NONE" AND "GITHUB SAID
# NOTHING AT ALL".** `read_pages` flattened its rows with `page.get(rows_key) or []`. Delete the `statuses`
# member from an otherwise-green response, leave `total_count: 0`, and every rule in this file agrees: the
# count matches the rows collected (zero of them), containment holds — **GREEN**. The tool ALREADY KNEW this
# rule (`total_count` absent is refused; a MISSING `statusCheckRollup` is refused) and the seam reintroduced
# it anyway, because a field read that DECLARES NOTHING cannot refuse ANYTHING.
#
# So the class is closed the way `repo_scoped()` closed its own: **not by fixing the one line, but by making
# the mistake unreachable.** Every field read off a GitHub response goes through `field()`, and `field()`
# takes no default — it takes a DECLARED SHAPE, and refuses anything else. A key that may be absent, or null,
# says so (`ABSENT`, `NULL`); a key that may not, cannot become one by accident.
#
# **A DECLARED TOLERANCE IS NOT A COERCION**, and the difference is the whole point. `conclusion` is null on a
# running check and `app` is null on a check run that came from no app — those reads declare `NULL`/`ABSENT`
# and the value lands in the artifact as `-`, which is a FACT GitHub stated. What is forbidden is the read
# that does not SAY what it expects, because that is the one that cannot tell a fact from a silence.

ABSENT = "may be ABSENT"   # the key may be MISSING from the object entirely
NULL = "may be NULL"       # the key may be present and explicitly null

# What a shape READS AS, in the refusal. `list` -> "a list", so a message says "…is null, NOT A LIST", which
# is the sentence a reader needs — never `<class 'list'>`.
SHAPE_NAMES = {list: "a list", dict: "an object", str: "a string", int: "an integer", bool: "a boolean"}


def shape_problem(source: str, obj: object, key: str, shape: tuple) -> str | None:
    """Does `obj[key]` have the shape the caller DECLARED? Returns the COMPLAINT, or None if it does.

    It NEVER returns the value. Reading is `field()`'s job and there is exactly ONE door — a second function
    that could hand back a value would be a second door, and the next `or []` would go through it.
    """
    kinds = tuple(k for k in shape if isinstance(k, type))
    want = " or ".join([*(SHAPE_NAMES.get(k, k.__name__) for k in kinds),
                        *(k for k in shape if not isinstance(k, type))]) or "NOTHING AT ALL"
    if not kinds:
        return (f"{source}: the read of {key!r} DECLARES NO SHAPE — a read that does not say what it expects "
                f"cannot REFUSE anything, so a MISSING value silently becomes an empty one. That is the bug "
                f"this seam exists to make unwritable.")
    if not isinstance(obj, dict):
        return (f"{source}: {key!r} cannot be read off {SHAPE_NAMES.get(type(obj), type(obj).__name__)} — "
                f"GitHub returns an object here, and a response we cannot read is not a response with "
                f"nothing in it.")
    if key not in obj:
        return None if ABSENT in shape else (
            f"{source}: the response STATES NO {key!r} — it is ABSENT, not {want}. AN ABSENT VALUE IS NOT AN "
            f"EMPTY ONE: 'GitHub says there are none' is a FACT, 'GitHub said nothing at all' is a response "
            f"we cannot read, and defaulting the second to the first is how a read with a HOLE in it reports "
            f"a complete one.")
    value = obj[key]
    if value is None:
        return None if NULL in shape else (
            f"{source}: {key!r} is null, not {want} — and a null we did not DECLARE is a value we do not "
            f"understand, never a benign one.")
    if isinstance(value, bool) and bool not in kinds:
        return f"{source}: {key!r} is a boolean ({value!r}), not {want}."
    if not isinstance(value, kinds):
        return (f"{source}: {key!r} is {SHAPE_NAMES.get(type(value), type(value).__name__)} ({value!r}), not "
                f"{want} — a value of a shape we did not declare is a response we cannot read.")
    return None


def field(source: str, obj: object, key: str, *shape: object, why: str = "") -> object:
    """**THE ONLY WAY A FIELD OF A GITHUB RESPONSE ENTERS THIS TOOL.**

    `field("status", page, "statuses", list)` — the caller SAYS WHAT IT EXPECTS, and anything else is a
    `FetchError`. There is no `default` parameter and there will never be one: a default is a legal-looking
    value handed to whoever did not think about the illegal case, and every false green in this file is one
    of those. `ABSENT` / `NULL` in the shape are how a caller declares a value GitHub legitimately may not
    send (a running check has no `conclusion`); anything NOT declared is REFUSED, loudly, here.

    `why` is the caller's reason — appended to the refusal, so the message says what the SHAPE means as well
    as what it is.
    """
    problem = shape_problem(source, obj, key, shape)
    if problem:
        raise FetchError(f"{problem}{' ' + why if why else ''}")
    return obj.get(key) if isinstance(obj, dict) else None


# --- WHAT A FETCHER HANDS TO `build_snapshot`: TYPED VALUES IT BUILT, NEVER A RESPONSE ------------
#
# `build_snapshot` ORCHESTRATES; it does not read responses. The cross-source facts leave a fetcher as TYPED
# values — attributes, not field reads — so the orchestrator never has to reach into a row (or, worse, into
# a response a fetcher handed back) to reconcile two sources.

class RestStatus(NamedTuple):
    """One commit status as the REST family reported it: its context, and the bucket it classifies to."""
    context: object
    bucket: str


class RestRun(NamedTuple):
    """One check run as the REST family reported it. `id` is the detailsUrl — the CROSS-SOURCE identity,
    the same one `ci-snapshot.check_containment` compares on."""
    id: object
    bucket: str


class RollupStatus(NamedTuple):
    """One `StatusContext` from the rollup: its context, the state GitHub spelled, and its bucket."""
    context: object
    state: object
    bucket: str


class RollupRun(NamedTuple):
    """One `CheckRun` from the rollup: its cross-source id, its name, the state it spelled, its bucket."""
    id: object
    name: object
    state: str
    bucket: str


class Snapshot(NamedTuple):
    """What `build_snapshot` hands `derive`. `rows` are the ARTIFACT'S LINES — dicts this file built, on
    their way to `promote()`, never read again by anything here."""
    rows: list
    head_sha_now: object
    evidence: dict


def read_pages(fetch: Fetch, source: str, argv: list[str], rows_key: str) -> tuple[list[dict], dict]:
    """**EVERY PAGINATED READ IN THIS TOOL ENTERS HERE, AND THERE IS NO OTHER DOOR.**

    A fetcher gets its rows from this function or it gets no rows at all — which is what makes the page rules
    impossible to forget rather than merely easy to remember. It returns the rows collected across ALL pages
    (`--slurp` hands us an ARRAY OF PAGES), and the FIRST page, from which a caller reads the per-commit facts
    GitHub repeats on every page (the status family's `.sha`).

    THE COMPLETENESS TEST LIVES HERE TOO, and it REPLACED A NOTE. The tool used to record `total_count=3 but
    collected 2` in a `notes` list and return GREEN anyway — a verdict computed from evidence it had just
    finished proving incomplete, with the proof printed politely underneath. Disclosure is not a substitute
    for refusal. **The only honest thing to do with evidence you KNOW is missing a row is to REFUSE TO DERIVE
    A VERDICT FROM IT.**

    `total_count` is GitHub's own count of the rows it holds FOR THE COMMIT — not for the page — and it is
    read off EVERY page and each value compared against the rows collected across ALL of them, which is
    exactly what `--slurp` gives us. Reading only the first page's count was itself a false green: GitHub
    RECOMPUTES the count per request, so a check registered mid-fetch makes a later page report MORE than the
    first, and a row we never received slips through. **The honest limit** (`ci-derivation-spec.md`, "Honest limits"):
    this catches a read that came up SHORT of what GitHub said it holds, or pages that DISAGREE about it. It
    does not, and cannot, prove that GitHub told us the truth, and `/check-runs` is capped at the 1000 most
    recent check suites regardless.
    """
    pages = fetch(source, argv)
    # A `--slurp` that did not yield an ARRAY is a response we cannot read — and the row loop below would
    # then iterate an object's KEYS and blow up on the first read, which is a CRASH where a verdict was
    # owed. An EMPTY array is refused for the same reason a missing `total_count` is: `--paginate --slurp`
    # over a real commit returns at least one page, so zero pages is a response we do not understand, and it
    # would leave every page rule below quantifying over the empty set — vacuously true, which is the shape
    # of every defect in this file.
    if not isinstance(pages, list) or not pages:
        raise FetchError(
            f"{source}: expected a NON-EMPTY array of pages from --slurp, got "
            f"{type(pages).__name__}{'(empty)' if isinstance(pages, list) else ''} — a response we cannot "
            f"read is not a response with nothing in it."
        )
    # AND EVERY PAGE IS AN OBJECT. A page that is not one used to reach `.get` and raise AttributeError — a
    # CRASH, which is not a refusal and not a verdict: the tool simply had no opinion where one was owed.
    if not all(isinstance(page, dict) for page in pages):
        raise FetchError(
            f"{source}: page(s) "
            + ", ".join(str(i + 1) for i, p in enumerate(pages) if not isinstance(p, dict))
            + f" of {len(pages)} are not objects — GitHub returns one object per page, and a page we cannot "
            f"read carries facts we cannot check. It is refused, never skipped: skipping it is how a page "
            f"that says 'there are more rows' stops being read."
        )

    # **AND EVERY PAGE CARRIES ITS ROW ARRAY — AS AN ARRAY.** The bug this replaced is `x or []`: the idiom
    # that erases the difference between "GitHub says this commit has no statuses" (a FACT, and
    # `total_count: 0` confirms it) and "the response did not contain a `statuses` member at all" (a response
    # we cannot read). Delete that member from an otherwise-green fixture and the count still matched the rows
    # collected — zero — containment still held, and `derive()` returned **GREEN**. A page missing the key, or
    # carrying anything but a LIST there, is REFUSED — never defaulted, never coerced.
    rows = [row for page in pages for row in field(source, page, rows_key, list, why=(
        f"Every page of this response carries its rows under {rows_key!r}; a page that does not is not a "
        f"page with NO rows, and the row it is hiding could be the FAILING one."))]

    first = next(iter(pages))
    # A COUNT WE CANNOT READ IS REFUSED, for the same reason `headRefOid` is: the completeness test below
    # cannot be MADE without it, and a fail-closed rule that cannot fire is not a rule — it is a hole with a
    # comment above it. An absent count is NOT a count of zero.
    #
    # AND IT IS READ OFF EVERY PAGE, NOT JUST THE FIRST — that was a false green (a `network`-writer finding
    # on a paginated read). `total_count` is GitHub's per-commit count, RECOMPUTED per request, so a check
    # that registers BETWEEN two page fetches makes a LATER page report a HIGHER count than the first: page
    # one says 31, we collect 31, page two says 32, and a rule that trusted page one alone waves the missing
    # 32nd row (perhaps the FAILING one) through as green. This is the same between-calls race the moved-head
    # and cross-source rules already fail closed on. So the count is read off every page (`field` refuses an
    # absent/non-integer count on ANY page, not only the first), and the collected rows are checked against
    # ALL of them.
    totals = [field(source, page, "total_count", int, why=(
        "That is GitHub's own count of the rows it holds for this commit, and it is the ONLY thing we can "
        "check our read against: without it we cannot tell a complete read from a truncated one, and "
        "cannot-tell is not a green. It is read off EVERY page — a later page reporting a different count is "
        "a check that registered mid-fetch, and the row it added could be the failing one.")) for page in pages]

    # WHAT WE COLLECTED MUST BE WHAT GITHUB SAYS IT HOLDS, ON EVERY PAGE. A page whose count disagrees with
    # the rows we collected is a short read (or a response whose pages contradict each other), and a hole we
    # KNOW about is never green. (This is not the marker's `count` rule, which asks a DIFFERENT question,
    # downstream: "did every row this fetch produced survive into the file?")
    off = sorted({t for t in totals if t != len(rows)})
    if off:
        raise FetchError(
            f"{source}: GitHub reported total_count={' and '.join(str(t) for t in off)} but the paginated "
            f"read collected {len(rows)} row(s) — EVIDENCE IS MISSING. A row GitHub holds for this commit is "
            f"not in our hands, and it could be the FAILING one. total_count is read off EVERY page: a check "
            f"registered between two page fetches makes a LATER page report more than the first, and a read "
            f"that trusted page one would miss it. No verdict is derived from a read we KNOW is short, or a "
            f"response whose pages DISAGREE about what the commit holds. (/check-runs is also capped at the "
            f"1000 most recent check suites; --paginate defeats page-size truncation, and this count defeats "
            f"a short read — neither proves completeness at that scale.)"
        )
    return rows, first


def fetch_check_runs(fetch: Fetch, head_sha: str) -> tuple[list[dict], dict, list[RestRun]]:
    """(1) CHECK RUNS — pinned to <head_sha> BY THE URL. Identity AND verdict in one row.

    `--paginate` is MANDATORY (`/check-runs` pages at 30) and `--slurp` collects every page into ONE array,
    which is what lets the marker's `count` be the total ACROSS pages rather than the last page's.

    THE REPOSITORY IS `REPO_SLOT`, NOT AN ARGUMENT — see `repo_scoped`. No fetcher is trusted to remember it.

    AND THE PAGES ARE NOT PARSED HERE EITHER — see `read_pages`, the one door a paginated read comes through.
    """
    runs, _first = read_pages(fetch, "check-runs", [
        "gh", "api", "--paginate", "--slurp", f"repos/{REPO_SLOT}/commits/{head_sha}/check-runs",
    ], "check_runs")

    rows = []
    seen: list[RestRun] = []
    for r in runs:
        # THE SHAPES ARE DECLARED, one read at a time (`field`). `app` is NULL on a check run that came from
        # no app and `conclusion` is NULL on one still running — both are things GitHub legitimately sends,
        # so both are DECLARED, and they land in the artifact as `-`, which is a fact and not a default. What
        # a read may never do is stay SILENT about what it expects: that is the read that cannot refuse.
        app = field("check-runs", r, "app", dict, NULL, ABSENT)
        app_id = field("check-runs", app, "id", int, NULL, ABSENT) if isinstance(app, dict) else None
        # GITHUB'S OWN `.head_sha`, off the row it sits on — NEVER the `head_sha` we asked for. The whole
        # force of the verify rule downstream comes from these two being INDEPENDENT: the header carries
        # ours, the rows carry GitHub's, so they CAN disagree, and on a snapshot fetched for a superseded
        # commit they WILL. Interpolate our own literal here and the comparison is a copy against its own
        # source: it matches BY CONSTRUCTION, it can never fail, and the verification is deleted rather
        # than implemented. That bug has already shipped in this repo once.
        sha = field("check-runs", r, "head_sha", str)
        status = up(field("check-runs", r, "status", str))
        conclusion = up(field("check-runs", r, "conclusion", str, NULL, ABSENT) or NO_OID)
        run_id = field("check-runs", r, "details_url", str, NULL, ABSENT) or NO_OID
        rows.append({
            "row": "checkrun",
            "sha": sha,
            "name": field("check-runs", r, "name", str),
            "app_id": s(app_id) if app_id is not None else NO_OID,
            "status": status,
            "conclusion": conclusion,
            "id": run_id,
        })
        # AND THE SAME VERDICT, TYPED, FOR THE CROSS-SOURCE TEST. `build_snapshot` reconciles this family
        # against the rollup. The bucket is computed HERE, where the response is already in hand and already
        # declared, and what leaves this fetcher is a value with no fields to read.
        seen.append(RestRun(id=run_id, bucket=checkrun_bucket(status, conclusion)))

    # The commit oid lives ONLY on the rows here, so a fetch that returned ZERO rows has NO oid to carry and
    # its marker's sha is `-`. Inventing one would be the fabrication the contract forbids outright.
    marker_sha = field("check-runs", next(iter(runs)), "head_sha", str) if runs else NO_OID
    marker = {"row": "source", "source": "check-runs", "sha": marker_sha, "count": str(len(rows))}
    return rows, marker, seen


def fetch_statuses(fetch: Fetch, head_sha: str) -> tuple[list[dict], dict, list[RestStatus]]:
    """(2) COMMIT STATUSES — the legacy family, which (1) CANNOT SEE.

    A failing Jenkins/CircleCI commit status is genuinely INVISIBLE to `/check-runs`. Read only one family
    and the other's failures are simply ABSENT from the evidence — and an absence parses as "nothing wrong".

    The response carries the commit ONCE PER PAGE, at the TOP LEVEL, and carries it EVEN WHEN `.statuses` IS
    EMPTY. That is what lets the marker PROVE a zero-status commit: `{"source":"status","sha":"<GitHub's>",
    "count":"0"}` says *we asked THIS COMMIT, and it has none* — a FACT, where an absent section says
    nothing at all.

    **NEVER read this response's own `.state` as a verdict.** A commit carrying ZERO statuses reports
    `{"state":"pending","total_count":0}` — verified live against this repo on a commit whose checks had all
    passed. An absence read as a verdict is a lie in both directions, so `.state` is not read here at all.
    """
    statuses, first = read_pages(fetch, "status", [
        "gh", "api", "--paginate", "--slurp", f"repos/{REPO_SLOT}/commits/{head_sha}/status",
    ], "statuses")

    # GITHUB'S OWN — never the sha we asked for. The whole force of the verify rule downstream comes from the
    # two being INDEPENDENT: the header carries ours, the rows carry GitHub's, so they CAN disagree, and on a
    # response fetched for a superseded commit they WILL. It is the response's own top-level `.sha`, and it
    # is present even at zero statuses, which is what makes the marker able to prove we asked.
    #
    # READ OFF THE FIRST PAGE, AND THAT IS CORRECT — unlike `total_count` (read off every page in
    # `read_pages`, because GitHub RECOMPUTES it per request and a later page can report more). `.sha` is not
    # a volatile count; it is the commit the endpoint's ref resolves to, and this endpoint —
    # `commits/<head_sha>/status`, the COMBINED status — is pinned to an IMMUTABLE 40-hex oid (`check_head_sha`).
    # An immutable commit always resolves to itself, so GitHub returns that SAME `.sha` on every page of the
    # SAME request. Verified live: `gh api repos/<repo>/commits/89739ae/status` returns `.sha` =
    # `89739ae4ba3a95…` (the ref echoed back). A page carrying a DIFFERENT `.sha` is therefore not a response
    # this endpoint produces — it would require the commit id to change mid-pagination, which cannot happen —
    # so cross-page sha agreement is a guard against an input the real API cannot emit, and is not added.
    sha = s(field("status", first, "sha", str))
    rows = []
    seen: list[RestStatus] = []
    for st in statuses:
        context = field("status", st, "context", str)
        state = up(field("status", st, "state", str))
        rows.append({"row": "status", "sha": sha, "context": context, "state": state})
        # TYPED, for the cross-source test — see `RestRun` in the check-run family, same rule, same reason:
        # `build_snapshot` must not have to subscript a row to reconcile it.
        seen.append(RestStatus(context=context, bucket=status_bucket(state)))
    # ALWAYS GitHub's, never `-`: a `-` here did not come from the response, and a marker whose sha is not
    # GitHub's cannot disagree with the ledger — so it could never fail. That is a rubber stamp.
    return rows, {"row": "source", "source": "status", "sha": sha, "count": str(len(rows))}, seen


def fetch_rollup(fetch: Fetch, pr: str) -> tuple[list[dict], dict, object, list[RollupStatus],
                                                 list[RollupRun]]:
    """(3) ROLLUP — its ROWS are WITNESSES ONLY (identity, no verdict), for the containment test.

    THAT IS A RULE ABOUT THE ARTIFACT, AND IT IS NOT A LICENCE TO LEAVE THE ROLLUP'S OWN VERDICT UNREAD.
    "The rollup may never BE a verdict" was read, for two rules running, as "the rollup's verdict may be
    ignored" — and the verdict it states was thrown away twice over (the `StatusContext`'s `state`, then
    every `CheckRun`'s `status`/`conclusion`), each time producing a green for a PR the rollup was calling
    FAILED. It is read here, RECONCILED in `build_snapshot`, and never written down.

    IT RETURNS TWO KINDS OF ENTRY, AND THIS FUNCTION USED TO KEEP ONE AND DROP THE OTHER ON THE FLOOR.
    `statusCheckRollup` holds `CheckRun` entries AND `StatusContext` entries (verified live: a Prow PR whose
    rollup is `[{__typename: StatusContext, context: "tide", state: "PENDING"}, {…"EasyCLA", "SUCCESS"}]`),
    and a `select(__typename == "CheckRun")` threw the second kind away WITHOUT A WORD.

    **WHAT IT DROPPED IS THE ONE THING NOTHING ELSE CAN SEE.** A `StatusContext` in state `EXPECTED` is a
    REQUIRED status check that HAS NOT BEEN POSTED YET — the PR is blocked on it. The REST commit-status API
    does not have an `EXPECTED` state AT ALL, so the family that carries status VERDICTS **cannot express
    it**: the rollup is the ONLY place it appears. Drop it, and a PR blocked on a check that has never run
    reports a snapshot with zero status rows, every check run passing — **GREEN**.

    So the entries are PARTITIONED, and nothing is discarded in silence:

      * `CheckRun`      -> witness rows (identity only; the containment test) — AND its `status` /
        `conclusion` are carried out to `build_snapshot` to be RECONCILED against the REST row for the same
        run. They are NOT written into the artifact: the rollup may never be a verdict. But a verdict we
        were HANDED and did not so much as LOOK AT is the same silent drop as the `StatusContext` below.
      * `StatusContext` -> its `state` is CHECKED AGAINST THE `StatusState` ENUM (an unknown value is a hard
        FetchError), and it is then returned to `build_snapshot`, which requires each one to be VISIBLE in
        the REST status family and REFUSES when one is not. They do NOT enter the artifact: the rollup
        carries no commit oid and no app id, so it can never be read as a verdict.
      * anything else   -> a hard FetchError. A `__typename` we do not know is a row we cannot read, and a
        row we cannot read is never a row we may drop: that is how the `StatusContext` got lost.

    `headRefOid` — the PR's head AS OF THIS CALL — rides along on the SAME call (no extra request), and it
    is the LAST thing this tool asks GitHub. It NEVER ENTERS THE ARTIFACT and NO RULE IN `ci-snapshot.py`
    READS IT: a snapshot is about the commit it was PINNED to. What it decides is one level up, in
    `derive()`, and it is not what the evidence SAYS but whether that evidence is ABOUT THIS PR AT ALL.

    **`--repo` IS NOT OPTIONAL HERE, AND ITS ABSENCE IS THE DEFECT THIS FUNCTION SHIPPED.** `gh pr view <pr>`
    with no repository resolves the PR IN THE CURRENT CHECKOUT. It is `REPO_SLOT` now, like every other
    fetch, and `repo_scoped` REFUSES an argv that does not name the repository.
    """
    data = fetch("rollup", [
        "gh", "pr", "view", pr, "--repo", REPO_SLOT, "--json", "statusCheckRollup,headRefOid",
    ])
    # `gh pr view --json` returns an OBJECT. Anything else is a response we cannot read, and reading
    # `.get("statusCheckRollup")` off it would crash — no verdict at all, which is the one outcome this
    # vocabulary has no word for.
    if not isinstance(data, dict):
        raise FetchError(f"rollup: expected an object, got {type(data).__name__}")

    # "THE ROLLUP WAS EMPTY" AND "WE DID NOT GET THE ROLLUP" ARE DIFFERENT ANSWERS, and `or []` gave them the
    # SAME one. An empty LIST is a FACT — GitHub says this head has no checks in the rollup, and a PR whose
    # suites are all dynamic-event legitimately looks like that. A MISSING (or non-list) key is a response we
    # did not understand, and reading it as "no witnesses" makes CONTAINMENT VACUOUS: "REST saw everything
    # the rollup saw" is then a claim about the empty set, and it passes trivially.
    entries = field("rollup", data, "statusCheckRollup", list, why=(
        "That is not an EMPTY rollup (a fact GitHub can state), it is a response we cannot read. Treating "
        "it as 'no witnesses' would make the containment test a claim about the empty set, which passes "
        "TRIVIALLY: an absence read as 'nothing wrong'."))

    witnesses: list[dict] = []
    status_rollup: list[RollupStatus] = []
    run_rollup: list[RollupRun] = []
    for entry in entries:
        kind = field("rollup", entry, "__typename", str, NULL, ABSENT)
        if kind == "CheckRun":
            witnesses.append(entry)
            # AND ITS VERDICT IS KEPT — NOT to be believed, but to be RECONCILED. The rollup returns each
            # CheckRun's `status` and `conclusion` (verified live), and this function used to read the NAME
            # and the URL and DROP THEM — the same silent drop the `StatusContext` suffered, in the family
            # that carries most of the verdicts. A reviewer's `FAILURE` in the rollup beside a `success` in
            # REST for the SAME run returned GREEN.
            #
            # `status` and `conclusion` DECLARE that the rollup may not send them (`NULL`, `ABSENT`) — and
            # that is NOT a coercion, because nothing here reads their absence as "no opinion". A field the
            # rollup did not send buckets as `UNKNOWN_VALUE` (see `checkrun_bucket`), which CONTRADICTS any
            # REST twin that classifies, and `agree_or_refuse` then refuses the pair.
            run_status = field("rollup", entry, "status", str, NULL, ABSENT)
            run_conclusion = field("rollup", entry, "conclusion", str, NULL, ABSENT) or NO_OID
            run_rollup.append(RollupRun(
                id=field("rollup", entry, "detailsUrl", str, NULL, ABSENT) or NO_OID,
                name=field("rollup", entry, "name", str),
                state=f"{up(run_status)}/{up(run_conclusion)}",
                bucket=checkrun_bucket(up(run_status), up(run_conclusion)),
            ))
            continue
        if kind == "StatusContext":
            # THE STATE IS AN ENUM GITHUB HANDS US, AND AN ENUM VALUE WE DO NOT RECOGNISE IS NOT A BENIGN
            # ONE. This value was ACCEPTED, unread, for as long as the REST status family happened to report
            # the same context — and then the coverage rule below passed, and the PR went GREEN. A reviewer
            # put `BRAND_NEW_FAILURE` into a rollup `StatusContext` beside a `SUCCESS` REST row and watched
            # `derive()` return green: the two sources DISAGREED about a context and the tool believed the
            # one it could parse.
            #
            # It is NOT covered by the row-level catch-all in `ci-snapshot.py` (which is what refuses an
            # unknown REST `.state`): a rollup `StatusContext` NEVER ENTERS THE ARTIFACT, by design, so no
            # rule downstream can ever see it. The value dies here or it dies nowhere.
            #
            # THE ENUM HAS ONE OWNER and it is `ci-snapshot.py` — this is the union of the three buckets it
            # CLASSIFIES with, never a fourth copy of the values. `doc-check` asserts that union IS the
            # `StatusState` enum the doc declares, so a value GitHub adds tomorrow lands outside it and is
            # REFUSED.
            context = field("rollup", entry, "context", str)
            state = up(field("rollup", entry, "state", str, NULL, ABSENT))
            if state not in STATUS_STATES:
                raise FetchError(
                    f"rollup: StatusContext {context!r} is in an UNRECOGNISED state "
                    f"{state!r} — StatusState is {' / '.join(sorted(STATUS_STATES))}, and a state we cannot "
                    f"read is NOT a state we may drop. It never enters the artifact, so NOTHING downstream "
                    f"can refuse it: accepted here, it is accepted for good, and this PR would go GREEN on a "
                    f"context whose state nobody has ever classified — which is exactly what a reviewer "
                    f"demonstrated. If GitHub has added a state, TEACH `ci-snapshot.py` ABOUT IT (its "
                    f"CLASSIFY buckets, and the doc's enum block) — do not let it fall on the floor here."
                )
            status_rollup.append(RollupStatus(context=context, state=state, bucket=status_bucket(state)))
            continue
        # The original bug was: keep what we recognise, drop the rest, say nothing. It is how the
        # `StatusContext` — and with it every required-but-unposted check — became invisible, and it is how a
        # `__typename` GitHub adds tomorrow would become invisible next.
        raise FetchError(
            f"rollup: entry of an UNRECOGNISED __typename {kind!r} — the rollup returns CheckRun and "
            f"StatusContext, and a kind we do not know is a kind we cannot read. A row we cannot read is "
            f"NOT a row we may ignore: dropping one is how a required check that had never run reported "
            f"GREEN. If GitHub has added a type, TEACH THIS TOOL ABOUT IT — do not let it fall on the floor."
        )

    rows = [{"row": "witness", "name": field("rollup", w, "name", str),
             "id": field("rollup", w, "detailsUrl", str, NULL, ABSENT) or NO_OID}
            for w in witnesses]
    # The rollup carries NO app id and NO commit oid, so it can never be read as a verdict — and its marker's
    # sha is therefore `-`, ALWAYS. A sha there would be one WE invented.
    marker = {"row": "source", "source": "rollup", "sha": NO_OID, "count": str(len(rows))}
    # THE HEAD MUST BE KNOWN, or the fail-closed rule below is a rule that cannot fire. A response with no
    # `headRefOid` leaves us unable to say whether this evidence is about the PR's head or about a commit it
    # has moved past — and "we cannot tell" has exactly one safe answer, which is not green. Left as `None`
    # it would sail straight through `head_moved()` as "not moved" — a fail-closed check that fails OPEN on
    # the one input it cannot read, which is the whole family of bug this file is about.
    #
    # THE READ DECLARES THAT GITHUB MAY NOT SEND IT, AND THE RULE BELOW SAYS WHAT WE DO ABOUT THAT — which
    # is REFUSE. That split is deliberate: `field()` says which shapes may ARRIVE, the rule says which ones
    # we can DERIVE FROM. An EMPTY string is refused here too — it is no more a commit than an absent one is.
    head_now = field("rollup", data, "headRefOid", str, NULL, ABSENT)
    if not isinstance(head_now, str) or not head_now:
        raise FetchError(
            "rollup: the response carries no headRefOid — WE CANNOT TELL which commit is the PR's head, so "
            "we cannot tell whether this evidence describes it. That is not a green; it is a fetch we "
            "cannot use."
        )
    return rows, marker, head_now, status_rollup, run_rollup


def up(value: object) -> object:
    """REST returns `status`/`conclusion`/`state` in lowercase; the enums are UPPERCASE."""
    return value.upper() if isinstance(value, str) else value


def build_snapshot(fetch: Fetch, repo: str, pr: str, head_sha: str) -> Snapshot:
    """The artifact, in order: header, then each source's rows FOLLOWED BY ITS OWN MARKER.

    THE HEADER'S SHA IS THE ONE ROW WHOSE SHA IS OURS — it records the commit we ASKED FOR (the ledger's
    `head_sha`). Every EVIDENCE row and every MARKER carries what GITHUB said. Two independent sources, which
    is the ONLY reason comparing them can tell you anything: build the check out of your own input and it
    passes by construction, including on a snapshot fetched for the wrong commit.

    AND THE REPOSITORY IS BOUND ONCE, HERE, TO THE SEAM ITSELF — never handed to a fetcher (`repo_scoped`).
    A fetcher that forgot it ran against the CURRENT CHECKOUT and nothing noticed; now there is nothing to
    forget, and an argv that does not name the repository cannot get past this line.
    """
    fetch = repo_scoped(fetch, repo)
    rows: list[dict] = [{"row": "header", "sha": head_sha}]

    # THE ORDER OF THESE THREE FETCHES IS ITSELF A RULE: **THE EVIDENCE FIRST, THE PR'S CURRENT HEAD LAST.**
    # No snapshot of a moving thing is atomic — a push can land BETWEEN two of these calls — so the question
    # is never "can the head move under us" (it can) but "can a head we read still be the head those checks
    # were fetched under". Read the head LAST and the answer is yes for every push up to that instant: any
    # push that could have staled the evidence happened BEFORE the head read, so `headRefOid` shows it and
    # `derive()` fails closed. (A push AFTER the last call is invisible to any tool at any ordering, and it
    # is harmless: it moves the ledger's head_sha, and the next derivation is pinned to the new one.)
    #
    # READ IT FIRST and the race is wide open, silently: a push landing between the head read and the
    # check-runs fetch leaves `headRefOid` EQUAL to the sha we asked for, so nothing fails closed, while the
    # check-runs call — pinned BY URL to the ledger's now-superseded sha — happily returns the OLD head's
    # green runs. Two snapshots of two different moments, spliced into one GREEN verdict about a commit that
    # is no longer the PR's head. `head-moves-mid-fetch.json` is that push, recorded.
    (runs, cr_marker, rest_runs), (statuses, st_marker, rest_statuses), (
        witnesses, ru_marker, head_now, status_rollup, run_rollup) = (
        fetch_check_runs(fetch, head_sha),
        fetch_statuses(fetch, head_sha),
        fetch_rollup(fetch, pr),
    )
    rows += runs + [cr_marker] + statuses + [st_marker] + witnesses + [ru_marker]

    evidence = {"checkrun": len(runs), "status": len(statuses), "witness": len(witnesses)}

    # THE ROLLUP'S STATUS CONTEXTS MUST BE VISIBLE IN THE FAMILY THAT CARRIES STATUS VERDICTS — or we do not
    # derive a verdict. This is CONTAINMENT, for the OTHER family: the witnesses prove REST saw every check
    # RUN the rollup saw, and this proves REST saw every commit STATUS the rollup saw.
    #
    # **READ THIS BEFORE YOU LEAN ON IT: THIS RULE IS *NOT* WHAT CLOSES THE REGISTRATION GAP, AND IT NEVER
    # COULD HAVE BEEN.** It was WRITTEN as that closure — "a required check that has not been posted appears
    # in the rollup and NOWHERE ELSE, so we check the rollup" — and the claim was FALSE, provably: it
    # quantifies over the `StatusContext` entries THE ROLLUP RETURNED, and the rollup carries no total, is a
    # single un-paginated page, and CANNOT BE PROVEN COMPLETE. Take the one `EXPECTED` entry out of the
    # response — a rollup that is merely SHORT — and `uncovered` is EMPTY, this rule has nothing to check,
    # and the PR goes GREEN while blocked on a check nobody has run. **A GUARD WHOSE INPUT CAN BE ABSENT
    # NEVER FIRES.** The closure is the REQUIRED SET, which is DECLARED by the base branch and therefore does
    # not depend on the rollup showing up: `required-check-absent.json` is that case, and what catches it is
    # `decide()`'s required-check rule (see `derive()`), not this line.
    #
    # SO WHAT IS IT STILL FOR? It is KEPT, and it is not redundant — but its job is the one it can actually
    # do: **the two sources DISAGREE ABOUT WHAT EXISTS.** The rollup names a commit status; the REST status
    # family, whose read is PROVEN COMPLETE against GitHub's own `total_count`, does not report it at all.
    # That is not "a check is missing" (the required set owns that question) — it is EVIDENCE WE CANNOT
    # RECONCILE. Two live cases reach it, and neither is hypothetical:
    #   * an `EXPECTED` context the required set does NOT declare — a ruleset changed after the run read it
    #     (the read is once-per-run and SETTLED, `stage-2-ci.md`), or the set read `none`. The rollup is then
    #     the ONLY source that knows, and this is what refuses;
    #   * a POSTED context (`SUCCESS`/`FAILURE`/…) present in the rollup and absent from `/status` — which
    #     should be impossible (verified live against a Prow PR: every rollup context appeared in `/status`),
    #     and if it ever happens it means our status read is wrong in a way `total_count` did not catch.
    # A context the REST family DOES report needs nothing further: that row carries identity AND verdict, so
    # it is already in the evidence and already decided — which is why this is a coverage test and not a
    # fail-closed-on-any-StatusContext rule. Refusing every `StatusContext` would WEDGE every Jenkins/Prow
    # repo forever, and a rule that wedges honest input gets deleted by the next person in a hurry.
    #
    # A MOVED HEAD IS NOT A COVERAGE FAILURE, and must not be reported as one: the rollup then describes the
    # NEW head while the REST families describe the old one, so of course its contexts are not in ours. That
    # is the HEAD MOVED rule's business (`derive()`), it is the better diagnosis, and it is the one that
    # carries `head_sha_now` back to the driver so the refetch is PINNED rather than guessed.
    contexts = {st.context for st in rest_statuses}
    uncovered = [w for w in status_rollup if w.context not in contexts]
    if uncovered and not head_moved(head_sha, head_now):
        raise FetchError(
            "rollup: the PR's status rollup lists "
            + ", ".join(f"{w.context!r} ({w.state})" for w in uncovered)
            + " — and the REST commit-status family, which is where a status VERDICT comes from, does not "
            "report it at all, on a read PROVEN COMPLETE against GitHub's own total_count. THE TWO SOURCES "
            "DISAGREE ABOUT WHAT EXISTS, and a snapshot built from evidence we cannot reconcile is not "
            "evidence: not a green, and not a red. An EXPECTED context is a required status nobody has "
            "posted (REST has no EXPECTED state to report it with), so derive again once it has been "
            "posted. NOTE: this rule can only see contexts the ROLLUP RETURNED, and the rollup cannot be "
            "proven complete — it is NOT what proves a required check registered. The REQUIRED SET is "
            "(--required-set)."
        )

    # AND EXISTING IN BOTH IS NOT THE SAME AS SAYING THE SAME THING. The rule above asks "does the REST
    # family REPORT this context AT ALL?" — and it was the ONLY question asked, so it answered YES and the
    # tool then believed whichever source it could parse. A reviewer shaped `rollup-status-posted.json` so
    # that REST said `success` and the ROLLUP said `FAILURE` for THE SAME CONTEXT, and `derive()` returned
    # **GREEN**. The check-run family had the identical hole and nobody had looked: the rollup hands us each
    # `CheckRun`'s status and conclusion, and this tool read the NAME and threw the VERDICT AWAY.
    #
    # **THIS IS REACHABLE WITHOUT ANYBODY LYING.** The REST families are fetched BEFORE the rollup. A check
    # that flips to failure BETWEEN those calls — the head never moving, so the moved-head rule cannot fire —
    # lands here exactly. The two sources are then both HONEST and they CONTRADICT each other, which is the
    # one thing this tool must never average out.
    #
    # SO: WHEN TWO SOURCES DISAGREE ABOUT ONE FACT, THE EVIDENCE IS NOT TRUSTWORTHY, AND UNTRUSTWORTHY
    # EVIDENCE IS NOT GREEN. The conflict is NOT resolved. Not by preferring REST (it is only "right" here by
    # the accident of being fetched first, and on the race above it is the STALE one); not by preferring the
    # rollup (it carries no oid — it may never be a verdict); and NEVER by taking the more favourable of the
    # two, which is how a tool ends up optimistic on exactly the evidence it should refuse.
    #
    # DISAGREE MEANS A DIFFERENT BUCKET, NOT A DIFFERENT SPELLING — see `status_bucket` / `checkrun_bucket`.
    # REST `success` vs rollup `SUCCESS` is one fact in two vocabularies and it MUST NOT refuse anything;
    # REST `pending` vs rollup `EXPECTED` are both RUNNING and agree. What does not agree is PASS vs FAIL,
    # and a value NOBODY has classified (`UNKNOWN_VALUE`) vs one we know.
    #
    # A ROLLUP ENTRY WITH NO REST TWIN IS NOT THIS RULE'S BUSINESS, and reporting it here would be the worse
    # diagnosis: for a `StatusContext` the coverage rule above already refused it, and for a `CheckRun` the
    # containment test does (`ci-snapshot.check_containment`, on the same `id`). Each rule keeps the case it
    # can name precisely.
    #
    # A MOVED HEAD IS NOT A DISAGREEMENT EITHER — the rollup then describes the NEW head and the REST
    # families the old one, so of course they differ. Same exemption, same reason, as the coverage rule.
    if not head_moved(head_sha, head_now):
        agree_or_refuse("commit status", [
            (w.context, w.bucket, w.state, twins)
            for w in status_rollup
            if (twins := {st.bucket for st in rest_statuses if st.context == w.context})
            and w.bucket not in twins
        ])

        agree_or_refuse("check run", [
            (w.name, w.bucket, w.state, twins)
            for w in run_rollup
            if (twins := {r.bucket for r in rest_runs if r.id == w.id}) and w.bucket not in twins
        ])
    return Snapshot(rows=rows, head_sha_now=head_now, evidence=evidence)


def agree_or_refuse(kind: str, conflicts: list[tuple]) -> None:
    """THE RULE BODY. Called ONCE PER FAMILY — a body no family invokes is not a rule.
    `rollup-status-conflict.json` drives one application, `rollup-checkrun-conflict.json` the other, and
    NEITHER can stand in for the other.
    """
    if conflicts:
        raise FetchError(
            f"rollup: THE TWO SOURCES DISAGREE ABOUT WHAT A {kind.upper()} SAYS — "
            + "; ".join(
                f"{name!r}: the rollup says {raw} ({bucket}), the REST family says "
                f"{' + '.join(sorted(rest))}"
                for name, bucket, raw, rest in conflicts
            )
            + ". They describe the SAME check on the SAME commit and they cannot both be right, so this "
            "evidence is not trustworthy — and untrustworthy evidence is NOT GREEN. The conflict is NOT "
            "resolved here: preferring the REST row would prefer the source we merely happened to fetch "
            "FIRST (a check that flips to failure between the two calls makes it the STALE one), and "
            "preferring the rollup would let a source that carries no commit oid decide a verdict. Neither "
            "is evidence about this PR. Derive again — a settled check reports the same thing to both."
        )


def promote(rows: list[dict], rundir: Path, pr: str, head_sha: str) -> Path:
    """Write to a temp file INSIDE <rundir> (same filesystem => `os.replace` is an atomic rename), then
    promote to `ci-<pr>-<head_sha>.txt` — the artifact's exact name, which `ci-snapshot.py` VERIFIES.

    Nothing is promoted unless every fetch SUCCEEDED: this is only ever called after `build_snapshot`
    returns, and any FetchError above aborts before this line. A partial artifact is not a snapshot, and a
    half-written one left in <rundir> is worse than none — a later heartbeat would read it as evidence.
    """
    tmp = rundir / f".ci-{pr}.{os.getpid()}"
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    final = rundir / f"ci-{pr}-{head_sha}.txt"
    os.replace(tmp, final)
    return final


def head_moved(head_sha: str, head_now: object) -> bool:
    """Is the commit we pinned this fetch to no longer the PR's head?

    ONE OWNER for this predicate, because it is stated twice — the FAIL-CLOSED rule in `derive()` reads it,
    and the `head_moved` field the driver reads is it. Two copies of a comparison is two chances to write
    `==` for `!=`, and the day they disagreed the JSON would say `head_moved: true` beside a green verdict.

    A head we did not get is `None` — and that CANNOT REACH HERE from a promoted snapshot: `fetch_rollup`
    refuses a response with no `headRefOid` outright. It is handled anyway, because a predicate that is
    correct only while some other function stays correct is a predicate with a landmine under it.
    """
    return bool(head_now) and head_now != head_sha


def derive(fetch: Fetch, repo: str, pr: str, head_sha: str, rundir: Path, required) -> dict:
    """FETCH -> PROMOTE -> VERIFY -> DECIDE, and then: IS THIS EVIDENCE EVEN ABOUT THIS PR?

    The verdict comes from the BYTES, never from the fetch. That is the whole architecture in one line. It
    would be trivial — and wrong — to decide from the row dicts still in memory: they are what we THINK we
    fetched. Writing them out and reading them back through `ci-snapshot.evaluate()` means the verdict is
    computed from THE ARTIFACT THAT WILL BE AUDITED, by the same code any reviewer runs against it. A tool
    whose answer cannot be reproduced from the evidence it leaves behind is asking to be trusted, which is
    the thing this repo does not do.

    AND THEN ONE QUESTION THE BYTES CANNOT ANSWER, WHICH IS WHY IT IS ASKED HERE AND NOT IN `ci-snapshot.py`.
    The artifact is a report about the commit it was PINNED to, and it is checked to death against that
    commit — but NOTHING IN IT KNOWS WHETHER THAT COMMIT IS STILL THE PR'S HEAD. `ci-snapshot.py` is a pure
    function from bytes on disk to a verdict; it is handed no PR, it makes no network call, and the day it
    did it would stop being independently auditable. Only the PRODUCER sees the head. So only the producer
    can refuse, and it MUST: a perfectly green snapshot of a commit the PR has moved past is a TRUE report
    about the WRONG THING, and merging on it merges code whose checks never ran.

    AND ONE QUESTION THE EVIDENCE CANNOT ANSWER EITHER, WHICH IS WHY IT ARRIVES AS AN ARGUMENT. `required` is
    what the BASE BRANCH declared (the row's `effective_required_set`, resolved from `--ledger`). Every rule that reads the
    artifact quantifies over the rows that ARE in it, and a required check that has not registered is NO ROW:
    invisible to the counts, to containment, and to the rollup cross-check alike — all three agree, correctly,
    about a set that is missing the one member that decides the merge. Only a DECLARED set can see it,
    because only a declaration is independent of what showed up. This tool does not re-implement that rule
    (`ci-snapshot.decide()` owns it); its whole job here is to HAND THE SET OVER.
    """
    try:
        rows, head_now, evidence = build_snapshot(fetch, repo, pr, head_sha)
    except FetchError as exc:
        # A source that could not be read promotes and reports NO artifact. An older same-head artifact may
        # remain in the persistent rundir, but this result does not name it as current evidence. The
        # `promote` below is NEVER reached, and no verdict is derived from a fetch we know to be incomplete.
        return result(pr, head_sha, SNAP.UNUSABLE, f"FETCH FAILED — {exc}", None, {}, None, required,
                      None, None)

    path = promote(rows, rundir, pr, head_sha)

    # THE SET THE VERDICT IS DECIDED UNDER IS THE SET THE RESULT REPORTS — one value, used twice, so the
    # answer and the account of how it was reached can never disagree.
    decided_under = required

    verdict, reason = SNAP.evaluate(path, head_sha, required=decided_under, expect_filename_sha=True)

    # FAIL CLOSED ON A MOVED HEAD — and note WHICH verdicts this overrides: ALL OF THEM, `red` included.
    # Green is obvious (it would merge a PR on checks that never ran against its head). RED IS THE ONE
    # WORTH SAYING OUT LOUD: it looks safe — it blocks the merge either way — but it is still a claim about
    # a commit that is not the PR's, and recording it as this PR's `ci` red would blame the new head for the
    # old one's failure and send a fix subagent at a bug the push may already have fixed. The evidence does
    # not describe the thing being judged; the only honest thing to report is that we do not yet know.
    #
    # `unusable`, not `pending`, and the distinction is the DRIVER'S TO USE: `pending` means "this PR's
    # checks have not started" (see zero-checks.json), and a driver that reads a moved head as that would
    # sit and WAIT for checks on a commit nobody is going to check. `unusable` means "the snapshot cannot be
    # trusted — REFETCH" (`stage-2-ci.md`), and the reason NAMES the new head so the refetch is pinned to it
    # instead of guessed. Both map to ledger `ci = pending` (LEDGER_CI is lossy, and that is why `verdict` is
    # emitted BESIDE `ci` and not collapsed into it).
    if head_moved(head_sha, head_now):
        verdict, reason = SNAP.UNUSABLE, (
            f"HEAD MOVED — this evidence was fetched for {head_sha}, but the PR's head is NOW {head_now}. "
            f"It describes a commit that is no longer this PR's head, so it is not evidence about this PR "
            f"at all: NOT green (the evidence is stale), and NOT red (that would be a claim about the wrong "
            f"commit). Re-derive with --head-sha {head_now} once the ledger holds it. "
            f"(what the stale snapshot said, for the record: {verdict} — {reason})"
        )

    # THE FINGERPRINT IS COMPUTED HERE, NEVER BY THE DRIVER — a hash a driver reassembles by hand from the
    # doc's spec is a hash that drifts, and every drifted byte reads as "CI moved", which resets the very
    # counters the fingerprint exists to feed. Only a final derivation trusted for the PR's current head has
    # one: `unusable`/`unverifiable` evidence is not trusted (the moved-head override above lands here too),
    # and hashing rejected or stale evidence would let a strike accrue against rows nobody believed about
    # this PR. A moved-head artifact remains on disk for audit, but produces no digest or tally. Trusted rows
    # come off the PROMOTED ARTIFACT, like the verdict — never from the dicts still in memory.
    fp = None
    buckets = None
    if verdict not in (SNAP.UNUSABLE, SNAP.UNVERIFIABLE):
        trusted = SNAP.parse(path)
        fp = SNAP.fingerprint(trusted, head_sha)
        buckets = bucket_counts(trusted)

    return result(pr, head_sha, verdict, reason, path, evidence, head_now, decided_under, fp, buckets)


def result(pr: str, head_sha: str, verdict: str, reason: str, path: Path | None,
           evidence: dict, head_now: object, required, fingerprint: str | None,
           buckets: "dict[str, int] | None") -> dict:
    """The machine-readable verdict — everything the driver needs, and NOTHING it has to interpret.

    `ci` is what goes into the ledger. `verdict` is what the evidence said. They are separate because the
    mapping is lossy (LEDGER_CI above), and the lossy one must never be the only one recorded.
    """
    # NO FIXTURE CAN REACH THIS: `LEDGER_CI` is total over the six verdicts `ci-snapshot.py` can return
    # today, so nothing this suite can construct lands in this branch — it fires only if that file grows a
    # SEVENTH verdict. The guard EARNS its place anyway: an unmapped verdict must not become a silent
    # `pending`, and the day that seventh verdict is added, this is what refuses to guess.
    column = ledger_ci(verdict)
    if column is None:
        fail(
            f"DECIDE returned {verdict!r}, which this tool has no ledger mapping for. That is an outcome "
            f"nobody has thought about, and guessing `pending` for it is the same 'close enough' that "
            f"greened a commit with no checks on it. Refusing to emit a verdict."
        )
    return {
        "pr": pr,
        "head_sha": head_sha,          # the commit we PINNED to (the ledger's) — what the fetch asked for
        "verdict": verdict,            # the DECIDE outcome, in full
        "ci": column,                  # the LEDGER value — recorded by `liveness`, which takes this JSON
        "reason": reason,              # WHICH rule fired and WHICH row made it fire
        "snapshot": str(path) if path else None,   # the evidence, on disk, re-verifiable by hand
        "evidence": evidence,          # counts per row type — `{}` when nothing was ever fetched
        # The PR's head as of the LAST call this tool made. It NEVER enters the artifact (`ci-snapshot.py`
        # reads no such thing), and it decides EXACTLY ONE question, in `derive()`: is the evidence about
        # this PR's head at all? If it is not, the verdict is `unusable` and `head_moved` is true — which is
        # what tells the driver to re-derive against THIS sha instead of waiting for checks that will never
        # arrive on the old one.
        "head_sha_now": head_now,
        "head_moved": head_moved(head_sha, head_now),
        # THE SET THIS VERDICT WAS DECIDED UNDER — `declared` / `none` / `unknown`, the row's
        # `effective_required_set`. It is an INPUT, recorded so the answer can be reproduced, and it is NOT a caveat
        # channel: `unknown` NEVER accompanies a green (it is a `pending` bullet in `decide()`), so this can
        # never become "green, but note that we could not read what was required".
        "required_set": required.state,
        # THE LIVENESS DIGEST of the trusted current-head evidence rows (`ci-snapshot.py fingerprint()`;
        # the spec is `stage-2-ci.md`, "SETTLED"). `liveness` compares it to the ledger's `ci_fingerprint`
        # and applies the SETTLED/RUNNING-STALL rules — nobody recomputes the hash by hand. `null`
        # exactly when the final derivation has no trusted evidence for the PR's current head (fetch failed,
        # unusable, unverifiable, or head moved). A moved-head artifact can remain on disk for audit, but
        # stale evidence has no fingerprint. A derivation that got none touches no liveness counter but
        # `unusable_refetches`.
        "fingerprint": fingerprint,
        # THE CLASSIFY TALLY of the trusted current-head evidence rows —
        # {"PASS","RUNNING","FAIL","UNKNOWN_VALUE"},
        # every key always present. `RUNNING > 0` is the ONE fact the watch policy ("WATCH ONLY WHAT CAN
        # MOVE") and `liveness`'s SETTLED/RUNNING-STALL split need, and emitting it is what spares the
        # driver from ever classifying snapshot rows by eye. `null` exactly when `fingerprint` is: an
        # derivation with no trusted current-head evidence has no rows to tally.
        "buckets": buckets,
        # THERE IS NO `notes` FIELD, and its absence is a RULE, not an oversight. It used to carry "the
        # evidence may be incomplete" NEXT TO A GREEN VERDICT — a disclosure nobody read, attached to the
        # one answer it contradicted. Every gap we can DETECT is now a REFUSAL (`read_pages`, the rollup
        # coverage rule, the moved head), so nothing is left to footnote; and what we CANNOT detect belongs
        # in `stage-2-ci.md`, stated once, not re-emitted as reassurance beside each verdict.
        # NEVER re-add a channel that can print a caveat beside a green: fail closed instead.
    }


# --- liveness: the SETTLED / RUNNING-STALL / UNUSABLE bookkeeping, as a command -------------------

MACHINE_ACTIONS = ("due", "in-flight", "none")
LEDGER_CI_VALUES = ("green", "red", "pending")


def check_now(source: str, value: str) -> datetime:
    """An ISO-8601 timestamp WITH a timezone — `ci_stalled_since` is UTC by contract, and subtracting a
    naive timestamp from an aware one raises, which would be a crash where a bound was owed."""
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        fail(f"{source}: {value!r} is not an ISO-8601 timestamp")
    if ts.tzinfo is None:
        fail(f"{source}: {value!r} carries no timezone — the stall clock is UTC ISO-8601 "
             f"(stage-2-ci.md, 'RUNNING-STALL'), and a naive value cannot be compared to it")
    return ts


def derive_output(raw: object) -> dict:
    """Validate the derive JSON handed to `liveness` — the UNEDITED output of `derive`, nothing else.

    The same fail-closed posture as every other input: a field missing or of the wrong shape is refused
    at the door, because a liveness pass run on a hand-assembled approximation of `derive`'s output is
    the hand-run bookkeeping this command exists to remove, wearing the command as a costume.
    """
    if not isinstance(raw, dict):
        fail("liveness: the derive JSON is not an object — hand this command the JSON `derive` printed, "
             "unedited")
    out: dict = {}
    for key in ("head_sha", "verdict", "ci", "reason"):
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            fail(f"liveness: the derive JSON carries no usable {key!r} — it is not `derive`'s output")
        out[key] = value
    if out["ci"] not in LEDGER_CI_VALUES:
        fail(f"liveness: derive JSON `ci` is {out['ci']!r}, not one of {'/'.join(LEDGER_CI_VALUES)}")
    fp, buckets = raw.get("fingerprint"), raw.get("buckets")
    trusted_current_head = out["verdict"] not in (SNAP.UNUSABLE, SNAP.UNVERIFIABLE)
    if trusted_current_head:
        if not isinstance(fp, str) or not re.fullmatch(r"[0-9a-f]{64}", fp):
            fail(f"liveness: a derivation with trusted current-head evidence must carry a 64-hex "
                 f"`fingerprint`; got {fp!r}")
        if (not isinstance(buckets, dict) or set(buckets) != set(BUCKET_KEYS.values())
                or any(not isinstance(v, int) or v < 0 for v in buckets.values())):
            fail(f"liveness: a derivation with trusted current-head evidence must carry the four-key "
                 f"`buckets` tally; got {buckets!r}")
    elif fp is not None or buckets is not None:
        fail(f"liveness: verdict {out['verdict']!r} has no trusted current-head evidence, yet the JSON "
             f"carries fingerprint={fp!r} buckets={buckets!r} — that is not `derive`'s output")
    out["fingerprint"], out["buckets"] = fp, buckets
    out["trusted_current_head"] = trusted_current_head
    return out


def liveness(ledger_path: Path, pr: str, derived: dict, machine_action: str, now: datetime) -> dict:
    """Apply the liveness rules to one derivation and write the row — ONE atomic row update.

    THE SPEC IS THE DERIVATION BLOCK IN `stage-2-ci.md` ("SETTLED", "RUNNING-STALL", "UNUSABLE — the
    refetch is BOUNDED"). What stays the CALLER's: `--machine-action` — "is work that can move this PR's
    `head_sha` due or in flight?" is a property judgment about the run's dispatch state, which no ledger
    field records; the caller asserts it, this command does every piece of arithmetic that follows from it.

    Two rules here are not in the doc's pseudocode lines and are stated in its prose instead:

      * A STALE DERIVATION IS REFUSED, NOT RECORDED. `derive` was pinned to a `head_sha` the row no longer
        holds — the site that moved it already reset the counters, so recording anything from the old
        head's evidence would poison the new head's budget. Exit 2, write nothing, re-derive.
      * A HELD ROW IS OBSERVED, NEVER STRUCK. Observation is not mutation (`ledger.py`, HELD_STATUSES), so
        `ci` is still recorded — but a parked row's `ci_reason` is the OPEN QUESTION a human is being
        asked, and its counters are cleared at unpark anyway: bounds neither accrue nor fire, and this
        command never overwrites an open park with a new one.
    """
    header, rows = LEDGER.load(ledger_path)
    row = LEDGER.find_row(rows, pr)
    if row is None:
        fail(f"liveness: no ledger row for PR {pr}")
    if derived["head_sha"] != row["head_sha"]:
        fail(f"liveness: this derivation is pinned to {derived['head_sha']} but the row now holds "
             f"{row['head_sha']} — a head that moved past the derivation. Nothing is recorded: re-derive "
             f"against the ledger's head (the site that moved it already reset the liveness counters)")

    held = row["status"] in LEDGER.HELD_STATUSES
    wrote: dict = {}

    def put(field: str, value: object) -> None:
        text = str(value)
        if row[field] != text:
            row[field] = text
            wrote[field] = text

    put("ci", derived["ci"])
    escalate_reason = None

    if held:
        state = "held"
    elif not derived["trusted_current_head"]:
        refetches = LEDGER.counter(row, "unusable_refetches") + 1
        put("unusable_refetches", refetches)
        state = "unusable"
        if refetches >= REFETCH_CAP:
            escalate_reason = (f"UNUSABLE at the REFETCH CAP — {refetches} consecutive derivations at "
                               f"head {row['head_sha']} yielded no trusted current-head evidence. "
                               f"Last refusal: "
                               f"{derived['reason']}")
    else:
        put("unusable_refetches", 0)
        if derived["fingerprint"] != row["ci_fingerprint"]:
            put("ci_fingerprint", derived["fingerprint"])
            put("settled_strikes", 0)
            put("ci_stalled_since", "-")
            state = "moving"
        elif machine_action in ("due", "in-flight"):
            put("ci_stalled_since", "-")
            state = "machine-action"
        elif derived["buckets"]["RUNNING"] == 0:
            state = "settled"
            if derived["ci"] in ("red", "pending"):
                strikes = LEDGER.counter(row, "settled_strikes") + 1
                put("settled_strikes", strikes)
                if strikes >= STRIKE_CAP:
                    escalate_reason = (f"SETTLED at the STRIKE CAP — {strikes} derivations saw this exact "
                                       f"check set, nothing RUNNING, and ci={derived['ci']}; nobody is "
                                       f"coming. Last DECIDE reason: {derived['reason']}")
        else:
            state = "running-stall"
            if row["ci_stalled_since"] == "-":
                put("ci_stalled_since", now.isoformat(timespec="seconds"))
            else:
                since = check_now("ledger ci_stalled_since", row["ci_stalled_since"])
                stalled_hours = (now - since).total_seconds() / 3600
                if stalled_hours >= STALL_CAP_HOURS:
                    escalate_reason = (f"RUNNING-STALL at the CI STALL CAP — a row still claims RUNNING "
                                       f"and NOT ONE row in the check set has changed state since "
                                       f"{row['ci_stalled_since']} ({stalled_hours:.1f}h). Last DECIDE "
                                       f"reason: {derived['reason']}")

    # UNKNOWN_VALUE parks ON THIS DERIVATION, not at a cap: `decide()` only returns `unclassified` when no
    # FAIL outranked the value (red would have won), and the doc's rule is "escalate, NEVER guess" — an
    # immediate park, which outranks a strike-cap reason because the unknown value IS the blocker.
    if not held and derived["verdict"] == SNAP.UNCLASSIFIED:
        state = "unknown-value"
        escalate_reason = (f"UNKNOWN VALUE — an evidence row carries a value nobody has classified; "
                           f"guessing a bucket for it is how a hole becomes a wedge or a false green. "
                           f"{derived['reason']}")

    if escalate_reason is not None:
        # ESCALATE (stage-2-ci.md): park, name the blocker, void any previous ruling — ONE row write, so
        # no heartbeat can observe a freshly parked row still carrying the previous park's answer.
        put("status", "awaiting-user")
        put("ci_reason", escalate_reason)
        put("blocker_ruling", "-")

    LEDGER.dump(ledger_path, header, rows)

    # --- watch_warranted: the WHOLE of "WATCH ONLY WHAT CAN MOVE", reduced to one fact ---------------
    #
    # The watch policy's table (`stage-2-ci.md`, "WATCH ONLY WHAT CAN MOVE") collapses to ONE predicate,
    # EMITTED here so the driver ACTS on a field instead of reading the table by eye — the same by-eye
    # judgment the whole tool exists to remove. What stays the driver's is the ACTION: LAUNCHING the watch
    # task, ensuring one is alive, relaunching an exited one. Deciding WHETHER a watch is warranted is this:
    #
    #     watch_warranted = TRUSTED_CURRENT_HEAD AND verdict != UNCLASSIFIED AND buckets["RUNNING"] > 0
    #
    # It reads ONLY `derived` — NEVER the row's `status` — so it is UNAFFECTED by held/parked status: a
    # park neither stops a warranted watch nor starts an unwarranted one (`stage-2-ci.md`, "WATCH ONLY WHAT
    # CAN MOVE"; the CI-watch park exception in `loop-control.md` step 3 and `critical-rules.md`).
    #
    # THE COLLAPSE, one WATCH-table row at a time — this is the proof, not a summary of it:
    #   * green                    -> every evidence row PASS, so RUNNING == 0 -> false.
    #   * pending (a row RUNNING)  -> RUNNING > 0 -> TRUE.
    #   * pending (nothing registered) -> zero evidence rows -> RUNNING == 0 -> false.
    #   * pending (required set unreadable) / (required check missing) -> NO row is RUNNING BY DECIDE
    #     CONSTRUCTION: plain `pending` (a RUNNING row) OUTRANKS both required-set bullets, so had a row
    #     been RUNNING the verdict would be plain `pending`, not either of these -> RUNNING == 0 -> false.
    #   * red + a row still RUNNING -> RUNNING > 0 -> TRUE.   red, every row terminal -> RUNNING == 0 -> false.
    #   * unusable / unverifiable  -> no trusted current-head rows to tally -> false.
    #   * UNKNOWN_VALUE            -> false, AND THIS IS THE ONE ROW A BARE `RUNNING > 0` GETS WRONG.
    #     `ci-snapshot.decide()` ranks UNCLASSIFIED ABOVE plain `pending`, so an unclassified verdict CAN
    #     carry a still-RUNNING row (buckets == {RUNNING: 1, UNKNOWN_VALUE: 1} is reachable, and pinned by
    #     a fixture). The table says NO watch anyway: `liveness` has ALREADY PARKED the PR, and the human
    #     ruling — not a check finishing — is the exit; a running check completing cannot answer the open
    #     question the unknown value raised. So the predicate EXCLUDES UNCLASSIFIED explicitly. Drop that
    #     term and it would warrant a pointless watch on a parked PR — the counterexample this field exists
    #     to get right.
    running = derived["buckets"]["RUNNING"] if derived["trusted_current_head"] else 0
    if not derived["trusted_current_head"]:
        watch_warranted, watch_reason = False, "no trusted current-head evidence"
    elif derived["verdict"] == SNAP.UNCLASSIFIED:
        watch_warranted, watch_reason = False, "unknown value parked — the park is the resolution, not a watch"
    elif running > 0:
        watch_warranted = True
        watch_reason = f"{running} row{'s' if running != 1 else ''} still RUNNING"
    else:
        watch_warranted, watch_reason = False, "nothing can move"

    return {
        "pr": pr,
        "head_sha": row["head_sha"],
        "ci": derived["ci"],
        "verdict": derived["verdict"],
        "state": state,             # moving | settled | running-stall | machine-action | unusable | held
        "held": held,
        "machine_action": machine_action,
        "wrote": wrote,             # exactly the fields this call changed, with the values written
        "escalated": escalate_reason is not None,
        "ci_reason": escalate_reason if escalate_reason is not None else "-",
        "settled_strikes": row["settled_strikes"],
        "unusable_refetches": row["unusable_refetches"],
        "ci_stalled_since": row["ci_stalled_since"],
        # WHETHER A WATCH IS WARRANTED — the whole of "WATCH ONLY WHAT CAN MOVE", decided above so the
        # driver never reads that table by hand. The driver ACTS on it: watch_warranted AND no live watch
        # -> ensure/relaunch one; false -> never launch or relaunch. `watch_reason` names the deciding fact.
        "watch_warranted": watch_warranted,
        "watch_reason": watch_reason,
    }


# --- doc-check: the doc and the code cannot silently disagree ------------------------------------

class DocError(Exception):
    """The doc could not be read the way this check needs to read it. NEVER a pass."""


def fenced_blocks(text: str) -> list[str]:
    return re.findall(r"^```[a-z]*\n(.*?)^```", text, re.MULTILINE | re.DOTALL)


def parse_enums(blocks: list[str]) -> dict[str, set[str]]:
    """The doc's own enum block — the three GitHub enums, introspected from the schema and written down.

    Continuation lines matter: `CheckConclusionState` wraps, and its last two values (`STARTUP_FAILURE`,
    `STALE`) live on the WRAPPED line. A parser that read only the first line would drop the two FAILURE
    values whose omission the doc explicitly calls a false green — so it would "agree" with a rule set that
    had lost them.
    """
    for block in blocks:
        if "CheckStatusState" not in block:
            continue
        enums: dict[str, set[str]] = {}
        current = None
        for line in block.splitlines():
            if not line.strip():
                continue
            head = re.match(r"^(?P<name>[A-Za-z]+)\s+(?P<vals>[A-Z_ ]+)$", line)
            if head:
                current = head.group("name")
                enums[current] = set(head.group("vals").split())
            elif re.match(r"^\s+[A-Z_ ]+$", line) and current:
                enums[current] |= set(line.split())
            else:
                raise DocError(f"the enum block holds a line this check cannot read: {line!r}")
        if not enums:
            raise DocError("the enum block parsed to ZERO enums")
        return enums
    raise DocError("no fenced block naming CheckStatusState — the doc's enum block is GONE or renamed")


def parse_enum_comment(source: str) -> dict[str, set[str]]:
    """The SAME enum table, restated a THIRD time as a COMMENT in `ci-snapshot.py`.

    A comment cannot be executed, so nothing has ever checked this copy — which makes it the one most likely
    to rot unnoticed, and the one a reader of that file actually believes. It is held to the same standard as
    the doc: parsed, and compared against the sets the code classifies with.
    """
    lines = source.splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if "CheckStatusState" in ln and ln.lstrip().startswith("#")), None)
    if start is None:
        raise DocError(
            f"{SNAPSHOT_PY.name} no longer restates the enums in a comment — this check has NO SUBJECT, "
            f"and a check with no subject must never report success"
        )
    block = []
    for ln in lines[start:]:
        stripped = ln.lstrip()
        if not stripped.startswith("#") or not stripped[1:].strip():
            break
        block.append(stripped[1:])
    return parse_enums([textwrap.dedent("\n".join(block)) + "\n"])


def parse_classify(blocks: list[str]) -> dict[str, set[str]]:
    """The doc's two CLASSIFY tables -> {bucket: values}, with the bucket keyed by the field it reads.

    A rule may WRAP (`.conclusion FAILURE | … | STALE` puts its `-> FAIL` on the next line), so lines are
    accumulated until one carries the arrow.
    """
    out: dict[str, set[str]] = {}
    seen_catch_all = 0
    for block in blocks:
        if "-> RUNNING" not in block and "-> PASS" not in block:
            continue
        field_name = None
        pending = ""
        for line in block.splitlines():
            line = line.split("#")[0].rstrip()
            if not line.strip():
                continue
            pending = f"{pending} {line.strip()}".strip()
            if "->" not in pending:
                continue
            lhs, _, rhs = pending.partition("->")
            pending = ""
            bucket = rhs.strip().split()[0] if rhs.strip() else ""
            lhs = lhs.strip()
            if lhs.startswith("."):
                field_name, _, lhs = lhs.partition(" ")
            if lhs.startswith("ANY OTHER VALUE"):
                if bucket != "UNKNOWN_VALUE":
                    raise DocError(f"the catch-all maps to {bucket!r}, not UNKNOWN_VALUE")
                seen_catch_all += 1
                continue
            values = {v.strip() for v in lhs.split("|") if re.fullmatch(r"[A-Z_]+", v.strip())}
            if not values or bucket not in ("RUNNING", "PASS", "FAIL"):
                continue  # `.status COMPLETED -> classify on .conclusion, below` is a REDIRECT, not a bucket
            key = f"{field_name or '.state'}:{bucket}"
            out.setdefault(key, set())
            out[key] |= values
    if not out:
        raise DocError("no CLASSIFY rules parsed — the tables are GONE, renamed, or reformatted")
    if seen_catch_all < 2:
        raise DocError(
            f"found {seen_catch_all} `ANY OTHER VALUE -> UNKNOWN_VALUE` catch-all(s), expected one per "
            f"CLASSIFY table. The catch-all is what makes classification TOTAL; without it an enum value "
            f"GitHub adds tomorrow falls into a HOLE, matches no branch, and the PR wedges forever."
        )
    return out


def parse_fingerprint_spec(blocks: list[str]) -> dict[str, tuple[str, ...]]:
    """The doc's FINGERPRINT block -> {row kind: the fields its canonical line carries, in order}.

    The block is the SPEC `ci-snapshot.fingerprint()` implements, and it is executable prose of the same
    kind as the enums: nothing compared it to the code until this parser existed, and a drifted copy is
    the one a reader believes. The line format is `checkrun  ->  "checkrun\\t<name>\\t…"` — the quoted
    template's first token must be the row kind itself, and every later token is one `<field>`.
    """
    for block in blocks:
        if "fingerprint = sha256" not in block:
            continue
        out: dict[str, tuple[str, ...]] = {}
        for kind, quoted in re.findall(r"^(checkrun|status)\s+->\s+\"([^\"]+)\"", block, re.MULTILINE):
            parts = quoted.split("\\t")
            if parts[0] != kind:
                raise DocError(f"the FINGERPRINT line for {kind!r} does not begin with the row kind: "
                               f"{quoted!r} — the line must carry which row it is, or two kinds with the "
                               f"same fields would hash identically")
            fields = []
            for part in parts[1:]:
                m = re.fullmatch(r"<([a-z_]+)>", part)
                if not m:
                    raise DocError(f"the FINGERPRINT line for {kind!r} carries {part!r}, which is not a "
                                   f"`<field>` placeholder — a literal there would be a value the doc "
                                   f"invented, hashed into every fingerprint")
                fields.append(m.group(1))
            out[kind] = tuple(fields)
        if set(out) != {"checkrun", "status"}:
            raise DocError(f"the FINGERPRINT block defines lines for {sorted(out)!r}, expected exactly "
                           f"['checkrun', 'status'] — the two evidence row types and no other")
        return out
    raise DocError("no FINGERPRINT block (`fingerprint = sha256`) — the spec this tool implements is "
                   "not where it was")


def parse_moved_head_contract(blocks: list[str]) -> dict[str, str]:
    """The moved-head owner block -> its exact artifact, trust, and result fields.

    Scope this check to the `moved_head.*` block rather than searching the whole doc for reassuring words:
    a stale summary elsewhere cannot satisfy the owner, and prose about an incomplete fetch cannot be
    mistaken for the moved-head rule.
    """
    keys = {"artifact", "trust", "verdict", "ci", "fingerprint", "buckets"}
    for block in blocks:
        if "moved_head.artifact" not in block:
            continue
        out: dict[str, str] = {}
        for line in block.splitlines():
            match = re.fullmatch(r"moved_head\.([a-z_]+) = (.+)", line)
            if not match:
                raise DocError(f"the moved-head owner block holds a line this check cannot read: {line!r}")
            key, value = match.groups()
            if key in out:
                raise DocError(f"the moved-head owner block defines {key!r} twice")
            out[key] = value
        if set(out) != keys:
            raise DocError(f"the moved-head owner block defines {sorted(out)!r}, expected exactly "
                           f"{sorted(keys)!r}")
        return out
    raise DocError("no moved-head owner block (`moved_head.artifact`) — the retained audit artifact and "
                   "its trust boundary are no longer mechanically documented")


def parse_liveness_contract(blocks: list[str]) -> dict[str, str]:
    """The refetch-counter owner block -> final-result trust, actions, head reset, and cap expression."""
    keys = {
        "untrusted_verdicts", "untrusted_action", "trusted_current_head_action",
        "retained_moved_head_artifact", "head_sha_changed_action", "refetch_cap",
    }
    for block in blocks:
        if "liveness.untrusted_verdicts" not in block:
            continue
        out: dict[str, str] = {}
        for line in block.splitlines():
            match = re.fullmatch(r"liveness\.([a-z_]+) = (.+)", line)
            if not match:
                raise DocError(f"the liveness owner block holds a line this check cannot read: {line!r}")
            key, value = match.groups()
            if key in out:
                raise DocError(f"the liveness owner block defines {key!r} twice")
            out[key] = value
        if set(out) != keys:
            raise DocError(f"the liveness owner block defines {sorted(out)!r}, expected exactly "
                           f"{sorted(keys)!r}")
        return out
    raise DocError("no liveness owner block (`liveness.untrusted_verdicts`) — artifact verification and "
                   "trusted current-head derivation are no longer mechanically distinguished")


def parse_caps(text: str) -> dict[str, int]:
    """The three liveness caps, off their ONE defining site each — `settled_strikes >= N` and
    `unusable_refetches >= N` in the derivation blocks, `THE CI STALL CAP = Nh` in prose.

    Every occurrence is collected and must agree: a second, different value anywhere in the doc is
    exactly the retyped-cap drift the "name the cap, never the number" rule forbids, and this is the
    check that makes that rule mechanical.
    """
    out: dict[str, int] = {}
    for name, pattern in (("STRIKE", r"settled_strikes\s*>=\s*(\d+)"),
                          ("REFETCH", r"unusable_refetches\s*>=\s*(\d+)"),
                          ("STALL_HOURS", r"THE CI STALL CAP = (\d+)h")):
        values = {int(v) for v in re.findall(pattern, text)}
        if not values:
            raise DocError(f"the {name} cap's defining site was not found ({pattern!r}) — a cap this "
                           f"tool fires must be declared in the doc, at one site, by value")
        if len(values) > 1:
            raise DocError(f"the {name} cap is stated with TWO different values ({sorted(values)}) — "
                           f"one of them is a retyped copy that has drifted")
        out[name] = values.pop()
    return out


def parse_decide_order(text: str) -> tuple[str, ...]:
    """The DECIDE section's bullets, in the order the doc evaluates them.

    ONLY the bullets that NAME an outcome count. The section is full of prose bullets, and a parser that
    took every bold-led bullet would read the rationale as if it were the rule.
    """
    section = re.search(r"^#### DECIDE.*?\n(.*?)(?=^#### |\Z)", text, re.MULTILINE | re.DOTALL)
    if not section:
        raise DocError("no `#### DECIDE` section — the order this tool pins is not where it was")
    names = "|".join(re.escape(n) for n in sorted(DECIDE_ORDER, key=len, reverse=True))
    found = tuple(
        m.group(1) for m in re.finditer(rf"^- \*\*({names})\*{{0,2}}", section.group(1), re.MULTILINE)
    )
    if not found:
        raise DocError("the DECIDE section lists ZERO outcome bullets — it cannot be checked, so it FAILS")
    return found


# The commit every recorded fixture describes. It lives HERE, not in the test module, because `code_argv()`
# below — which `doc-check` depends on — replays `green.json` to capture the argv the code really issues.
FIXTURE_SHA = "1499c72bf1715e74abb0e28658b515eaa2c0c971"


def fixture_fetch(fx: dict) -> Fetch:
    """A `Fetch` that answers from a recorded fixture instead of GitHub — same seam, same producer, no
    network. The fixture SUITE lives in `ci-status-test.py`; this seam-replacement lives here because
    `doc-check` needs it to replay one response and record the argv (`code_argv`).

    A fixture may also record a PUSH THAT LANDS MID-FETCH:

        "push": {"after": "check-runs", "head": "<the new head sha>"}

    From the moment that source has been read, the rollup answers with the NEW `headRefOid`, exactly as
    GitHub would — before it, with the old one. **A STATIC RECORDING CANNOT TEST AN ORDERING.** It replays
    the same bytes whichever order the sources are read in, so it cannot tell a head read BEFORE the evidence
    from one read AFTER it — and that difference is the whole of the head-read-last rule. A fixture with no
    `push` key is unaffected: nothing moves, and the order cannot matter.
    """
    push = fx.get("push")
    seen: set[str] = set()

    def fetch(source: str, _argv: list[str]) -> object:
        spec = fx["api"].get(source)
        if spec is None:
            raise FetchError(f"{source}: the fixture records no response for this source")
        if "fail" in spec:
            raise FetchError(f"{source}: {spec['fail']}")
        response = spec["response"]
        if push and source == "rollup" and push["after"] in seen:
            response = {**response, "headRefOid": push["head"]}  # the push has landed; GitHub says so
        seen.add(source)
        return response
    return fetch


def code_argv() -> dict[str, list[str]]:
    """The argv the CODE actually hands `gh`, captured through the same `Fetch` seam the fixtures drive.

    Taken from the RUNNING CODE, never written down here: an expected-argv list in this file would be a
    THIRD copy of the command, free to rot exactly like the doc's, and checked by nobody.
    """
    fx = json.loads((FIXTURES / "green.json").read_text(encoding="utf-8"))
    inner, seen = fixture_fetch(fx), {}

    def record(source: str, argv: list[str]) -> object:
        seen[source] = argv
        return inner(source, argv)

    build_snapshot(record, "o/r", fx.get("pr", "35"), fx.get("head_sha", FIXTURE_SHA))
    return seen


def check_gh_invocations(text: str, argv: dict[str, list[str]]) -> list[str]:
    """EVERY RUNNABLE COPY IN THE DOC OF A `gh` COMMAND THIS TOOL ISSUES — against the argv the code really
    issues. A recap is where a flag goes to die, and it has already happened here twice: a copy that dropped
    `,headRefOid` (the moved-head rule's only input), and a rollup fetch with NO REPOSITORY AT ALL, which
    resolves the PR in whatever checkout the reader is standing in. Both are commands a reader can follow and
    get a WRONG ANSWER from, silently.

    The repo appears in a `gh` command in exactly the two ways `require_repo_scoped` accepts, and the doc
    writes them with placeholders (`repos/<owner>/<repo>/…`, `--repo <owner>/<repo>`) — so what is checked
    here is that the command SAYS which repository it is about, not which one it names.

    (`gh api` lines in the doc's PROMOTE recap ELIDE the URL — `".../check-runs"` — because the spec block
    above owns it; they are pointers, and the flags they still spell out are still checked. A `gh pr view`
    is never elided: it is written out in full every time, so `--repo` is required of every copy.)

    **THE SUBJECT IS A COMMAND A READER CAN RUN, AND THAT IS EXACTLY WHAT IS TESTED FOR.** A copy is a line
    (continuations JOINED — see below) that BEGINS with the command. Prose that mentions `gh pr view`
    mid-sentence is not a copy, and must not be treated as one: this doc WARNS about the bad form in prose,
    and a guard that matched the command anywhere in a line would fail the build on the warning — condemning
    correct text, which is how a guard gets deleted by the next person in a hurry.
    """
    problems: list[str] = []
    json_fields = argv["rollup"][argv["rollup"].index("--json") + 1]
    # **A COMMAND IS NOT A LINE, AND THIS GUARD USED TO THINK IT WAS.** A shell invocation WRAPS with a
    # trailing `\`, and every test below is a substring test on one line — so a rollup fetch written across
    # two lines had `--json statusCheckRollup` on the first and its flags on the second, and NONE of the
    # tests fired. Continuations are JOINED first, so the subject of the test is the COMMAND.
    for line in re.sub(r"\\\n\s*", " ", text).splitlines():
        line = line.strip()
        if line.startswith("gh api") and ("/check-runs" in line or "/status" in line):
            for flag in ("--paginate", "--slurp"):
                if flag not in line:
                    problems.append(f"a REST fetch in the doc omits {flag} — `{line[:60]}…`. The code passes "
                                    f"it, and without it you parse page one and call it the whole set.")
        if line.startswith("gh pr view") and "statusCheckRollup" in line:
            got = re.search(r"--json\s+(\S+)", line)
            if not got or got.group(1) != json_fields:
                problems.append(
                    f"a rollup fetch in the doc requests `--json {got.group(1) if got else '?'}` but the code "
                    f"requests `--json {json_fields}` — `{line[:60]}…`. Drop `headRefOid` and the PR's current "
                    f"head is unknown, which is the one input the MOVED-HEAD rule cannot do without."
                )
            if "--repo" not in line:
                problems.append(
                    f"a rollup fetch in the doc names NO REPOSITORY — `{line[:60]}…`. `gh pr view <pr>` with "
                    f"no `--repo` resolves the PR in the CURRENT CHECKOUT, so a reader following this copy "
                    f"derives a verdict about whatever repo they are standing in — or, for a PR that does "
                    f"not exist there, about nothing at all. The code passes `--repo` (`require_repo_scoped` "
                    f"refuses an argv without it); the doc must too, in EVERY copy."
                )
    return problems


def check_derive_copies(root: Path | None = None) -> tuple[list[str], list[str]]:
    """EVERY COPY OF THE DERIVE COMMAND, IN EVERY SKILL DOC — not just the one in the doc under test.

    THE FLAG THAT NAMES THE REQUIRED SET MUST NOT BE DROPPABLE BY A RECAP. The required set is what makes
    `green` mean *the required set passed*, and it is now the ROW's `effective_required_set`: a copy names it
    with `--ledger` (resolve the row's set — the production form) OR with an explicit `--required-set`. A copy
    carrying NEITHER is a reader reconstructing an invocation the tool REFUSES. This repo has already paid for
    the class TWICE: a fourth copy of a canonical command that had gone stale, and a doc recap that dropped
    `,headRefOid` from the rollup fetch.

    A copy is any occurrence that RUNS the command (`ci-status.py derive` carrying `--pr`) — prose that
    merely NAMES the command is not a copy, and is not checked. **THE UNIT IS THE COMMAND, NOT THE LINE**:
    an invocation WRAPS (a shell `\\`, or plain prose reflow), and a line-by-line check would report the
    continuation line as a violation of itself. So each copy is read to the end of its PARAGRAPH.

    FINDING ZERO COPIES IS A FAILURE: the command is prescribed by at least `stage-2-ci.md` and
    `critical-rules.md`, and a check that cannot find its subject never passes.
    """
    problems, copies = [], []
    for md in sorted((root or HERE.parent).rglob("*.md")):
        text = md.read_text(encoding="utf-8")
        for m in re.finditer(r"ci-status\.py derive", text):
            end = text.find("\n\n", m.start())
            command = text[m.start(): end if end > 0 else len(text)]
            if "--pr" not in command:
                continue  # prose that NAMES the command, not a copy of it
            n = text.count("\n", 0, m.start()) + 1
            copies.append(f"{md.name}:{n}")
            if "--ledger" not in command and "--required-set" not in command:
                problems.append(
                    f"{md.name}:{n} runs `ci-status.py derive` WITHOUT `--ledger` OR `--required-set` — the "
                    f"flag that makes `green` mean the REQUIRED SET passed. A reader following this copy "
                    f"issues a command the tool refuses; a reader who 'fixes' it by dropping the set gets a "
                    f"verdict about the rows that showed up, which is the registration gap, reopened by a recap."
                )
    if not copies:
        problems.append(
            "ZERO copies of `ci-status.py derive` were found in the skill's docs — the command is "
            "prescribed by stage-2-ci.md and critical-rules.md, so finding none means this check has lost "
            "its subject, and a check that finds nothing must never pass"
        )
    return problems, copies


def check_liveness_copies(root: Path | None = None) -> tuple[list[str], list[str]]:
    """Every runnable liveness copy carries `--machine-action` — the judgment flag a recap must not drop.

    Same class as `check_derive_copies`: a copy without the flag is a command the tool refuses, and a
    reader who "fixes" it by inventing a default answers the one question the tool deliberately asks.
    """
    problems, copies = [], []
    for md in sorted((root or HERE.parent).rglob("*.md")):
        text = md.read_text(encoding="utf-8")
        for match in re.finditer(r"ci-status\.py liveness", text):
            end = text.find("\n\n", match.start())
            command = text[match.start(): end if end > 0 else len(text)]
            # `--ledger`, not `--pr`, is the runnable-copy gate here: prose about liveness routinely sits
            # in the same paragraph as a `ledger.py … set --pr` command, and `--pr` alone would condemn
            # every such mention as a flagless invocation.
            if "--ledger" not in command:
                continue  # prose that names the subcommand, not a runnable copy
            line = text.count("\n", 0, match.start()) + 1
            copies.append(f"{md.name}:{line}")
            if "--machine-action" not in command:
                problems.append(
                    f"{md.name}:{line} runs `ci-status.py liveness` WITHOUT `--machine-action` — the one "
                    f"judgment the command asks of its caller. The tool refuses the invocation; a reader "
                    f"who drops the flag's question strikes the very PR a fix is about to move."
                )
    if not copies:
        problems.append(
            "ZERO runnable copies of `ci-status.py liveness` were found in the skill's docs — the command "
            "is prescribed by stage-2-ci.md, so finding none means this check has lost its subject"
        )
    return problems, copies


def check_required_set_copies(root: Path | None = None) -> tuple[list[str], list[str]]:
    """Every runnable required-set copy names the ledger whose per-row required sets the command persists."""
    problems, copies = [], []
    for md in sorted((root or HERE.parent).rglob("*.md")):
        text = md.read_text(encoding="utf-8")
        for match in re.finditer(r"ci-status\.py required-set", text):
            end = text.find("\n\n", match.start())
            command = text[match.start(): end if end > 0 else len(text)]
            if "--ledger" not in command:
                continue  # prose that names the subcommand, not a runnable copy
            line = text.count("\n", 0, match.start()) + 1
            copies.append(f"{md.name}:{line}")
            if "state.jsonl" not in command:
                problems.append(
                    f"{md.name}:{line} runs `ci-status.py required-set` without the run ledger's "
                    f"`state.jsonl` — the command must persist the value it read before the value exists"
                )
    if not copies:
        problems.append(
            "ZERO runnable copies of `ci-status.py required-set` were found in the skill's docs — finding "
            "nothing means this check has lost its subject"
        )
    return problems, copies


def doc_check(spec_doc: "Path | None" = None, driver_doc: "Path | None" = None) -> int:
    """Assert the DOC, the CODE, and this tool's DECIDE_ORDER all say the same thing.

    Six things are checked, and the last three are the ones no reader ever does by hand:

      1. the doc's CLASSIFY buckets == the sets `ci-snapshot.py` actually classifies with;
      2. the doc's DECIDE bullet order == DECIDE_ORDER (which the fixtures pin behaviourally);
      3. the doc's FINGERPRINT canonical lines == `ci-snapshot.FINGERPRINT_FIELDS`, the fields the hash
         `derive` emits is actually built from — a drifted line format is a DIFFERENT fingerprint, and a
         reader who trusts the doc's copy sees motion that never happened;
      4. **CLASSIFICATION IS TOTAL over the doc's OWN enums** — every declared value lands in exactly one
         bucket, no bucket holds a value the enum does not declare. A rule set can agree with the doc's
         tables line for line and still leave a HOLE, because the tables and the enum list are two different
         paragraphs. A value in a hole matches NO branch: not green, not red, not pending — the PR can never
         resolve, and it WEDGES. This is the check that catches that, and nothing else in the repo does.
      5. the doc's `gh` INVOCATIONS, in every copy of them, against the argv the code really issues — plus
         every copy of the derive and required-set commands and their required ledger inputs.
      6. the moved-head owner block says the old-head artifact is retained for audit but contributes no
         current-PR verdict, fingerprint, or buckets; and the liveness owner block says that final
         untrusted result increments the refetch counter while trusted current-head evidence resets it.

    (The doc's three snapshot `jq` filters are executed by `ci-snapshot.py` over recorded, multi-page API
    payloads. The required-set reads are production functions here, covered by `ci-status-test.py`.)
    """
    spec_doc = spec_doc or SPEC_DOC
    driver_doc = driver_doc or DRIVER_DOC
    for doc in (spec_doc, driver_doc):
        if not doc.exists():
            print(f"FAIL     the doc is not at {doc} — a check that cannot find its subject NEVER passes")
            return 1

    spec_text = spec_doc.read_text(encoding="utf-8")
    driver_text = driver_doc.read_text(encoding="utf-8")
    # Each element is parsed from the file that OWNS it (enums/CLASSIFY/DECIDE/fetches: the spec;
    # the fingerprint block: the driver doc) — except the caps, parsed over BOTH texts, because a cap
    # RETYPED in the other file is exactly the drift `parse_caps` exists to refuse.
    try:
        enums = parse_enums(fenced_blocks(spec_text))
        classify = parse_classify(fenced_blocks(spec_text))
        order = parse_decide_order(spec_text)
        fp_spec = parse_fingerprint_spec(fenced_blocks(driver_text))
        moved_head = parse_moved_head_contract(fenced_blocks(driver_text))
        liveness_contract = parse_liveness_contract(fenced_blocks(driver_text))
        caps = parse_caps(spec_text + "\n" + driver_text)
    except DocError as exc:
        print(f"FAIL     the CI docs cannot be read: {exc}")
        return 1

    # The doc's enum block is restated a THIRD time, as a comment in `ci-snapshot.py`. A comment cannot be
    # executed, so nothing ever checked it, and it is the copy most likely to rot unnoticed. Parse it and
    # hold it to the same standard.
    try:
        snap_enums = parse_enum_comment(SNAPSHOT_PY.read_text(encoding="utf-8"))
    except DocError as exc:
        print(f"FAIL     {SNAPSHOT_PY.name} cannot be read: {exc}")
        return 1

    checks: list[tuple[str, object, object, str]] = [
        (".status -> RUNNING", classify.get(".status:RUNNING"), SNAP.RUNNING_STATUSES,
         "a NEGATED test (`!= COMPLETED`) here is a catch-all in disguise: it would map tomorrow's enum "
         "value to RUNNING and the PR would wait for it forever"),
        (".conclusion -> PASS", classify.get(".conclusion:PASS"), SNAP.PASS_CONCLUSIONS,
         "drop SKIPPED and a repo with path filters can NEVER go green"),
        (".conclusion -> FAIL", classify.get(".conclusion:FAIL"), SNAP.FAIL_CONCLUSIONS,
         "drop STARTUP_FAILURE or STALE and the tool calls them not-a-failure and MERGES OVER THEM"),
        (".state -> PASS", classify.get(".state:PASS"), SNAP.STATUS_PASS, ""),
        (".state -> RUNNING", classify.get(".state:RUNNING"), SNAP.STATUS_RUNNING, ""),
        (".state -> FAIL", classify.get(".state:FAIL"), SNAP.STATUS_FAIL,
         "ERROR **is** a failure — never shrug it off as a glitch"),
        ("CheckStatusState is TOTAL", enums.get("CheckStatusState"),
         SNAP.RUNNING_STATUSES | {SNAP.TERMINAL_STATUS},
         "a value in neither bucket matches no branch at all — the PR WEDGES"),
        ("CheckConclusionState is TOTAL", enums.get("CheckConclusionState"),
         SNAP.PASS_CONCLUSIONS | SNAP.FAIL_CONCLUSIONS, "same: a hole is a wedge or a false green"),
        ("StatusState is TOTAL", enums.get("StatusState"),
         SNAP.STATUS_PASS | SNAP.STATUS_RUNNING | SNAP.STATUS_FAIL, "same"),
        ("the enum block in ci-snapshot.py", snap_enums.get("CheckStatusState"),
         enums.get("CheckStatusState"), "the doc and the script's own comment disagree"),
        ("DECIDE order", order, DECIDE_ORDER,
         "the doc evaluates the bullets in a different order than this tool pins"),
        ("FINGERPRINT line: checkrun", fp_spec.get("checkrun"), SNAP.FINGERPRINT_FIELDS["checkrun"],
         "the doc's canonical line and the hash `derive` emits disagree — every driver comparing "
         "`fingerprint` against `ci_fingerprint` would see motion that never happened, or none that did"),
        ("FINGERPRINT line: status", fp_spec.get("status"), SNAP.FINGERPRINT_FIELDS["status"],
         "same: a drifted line format is a different fingerprint"),
        ("moved-head artifact", moved_head.get("artifact"), "promoted for requested head_sha",
         "a completed moved-head fetch retains the requested-head artifact for audit"),
        ("moved-head trust", moved_head.get("trust"), "audit only; not current PR evidence",
         "the retained old-head artifact must never authorize a current-PR verdict"),
        ("moved-head verdict", moved_head.get("verdict"), SNAP.UNUSABLE,
         "a moved head fails closed without blaming or approving the new head"),
        ("moved-head ledger ci", moved_head.get("ci"), LEDGER_CI[SNAP.UNUSABLE],
         "the ledger records pending until CI is re-derived for the new head"),
        ("moved-head fingerprint", moved_head.get("fingerprint"), "null",
         "stale evidence contributes no liveness digest"),
        ("moved-head buckets", moved_head.get("buckets"), "null",
         "stale evidence contributes no CLASSIFY tally"),
        ("liveness untrusted verdicts", set(liveness_contract.get("untrusted_verdicts", "").split()),
         {SNAP.UNUSABLE, SNAP.UNVERIFIABLE},
         "every final derivation without trusted current-head evidence must spend the refetch budget"),
        ("liveness untrusted action", liveness_contract.get("untrusted_action"),
         "unusable_refetches += 1",
         "an untrusted final result must increment the refetch counter"),
        ("liveness trusted action", liveness_contract.get("trusted_current_head_action"),
         "unusable_refetches = 0",
         "only a trusted current-head result resets the refetch counter"),
        ("liveness moved-head artifact", liveness_contract.get("retained_moved_head_artifact"),
         "untrusted",
         "verifying and retaining an old-head artifact must not make the final derivation trusted"),
        ("liveness head change", liveness_contract.get("head_sha_changed_action"),
         "reset by ledger accessor",
         "a head change resets the counter at the ledger write door"),
        ("liveness refetch-cap expression", liveness_contract.get("refetch_cap"),
         f"unusable_refetches >= {REFETCH_CAP}",
         "the owner block and the refetch cap used by liveness must agree"),
        ("the STRIKE CAP", caps.get("STRIKE"), STRIKE_CAP,
         "the bound `liveness` fires at and the bound the doc promises are different numbers"),
        ("the REFETCH CAP", caps.get("REFETCH"), REFETCH_CAP,
         "same: the tool escalates at one count while the doc promises another"),
        ("the CI STALL CAP (hours)", caps.get("STALL_HOURS"), STALL_CAP_HOURS,
         "same: a stalled check would park earlier or later than the doc says it will"),
    ]

    failures = 0
    for what, got, want, why in checks:
        if got == want:
            print(f"ok       {what:32} {fmt(want)}")
            continue
        failures += 1
        missing, extra = diff(want, got)
        print(f"FAIL     {what:32} doc/code DISAGREE\n"
              f"         code says: {fmt(want)}\n"
              f"         doc says:  {fmt(got)}\n"
              f"         missing from the doc: {fmt(missing) or '—'}   only in the doc: {fmt(extra) or '—'}"
              + (f"\n         {why}" if why else ""))

    # AND THE `gh` COMMANDS, IN EVERY COPY, IN BOTH DOCS — the class, not the instance.
    held: list[str] = []
    argv = code_argv()
    problems = check_gh_invocations(spec_text + "\n" + driver_text, argv)
    if not problems:
        json_fields = argv["rollup"][argv["rollup"].index("--json") + 1]
        held.append(f"{'the gh invocations':32} every copy in the doc: --paginate --slurp, "
                    f"--json {json_fields}, and REPO-SCOPED")

    # AND EVERY COPY OF THE DERIVE COMMAND ITSELF, ACROSS EVERY SKILL DOC — the class, not the instance.
    derive_problems, derive_copies = check_derive_copies()
    problems += derive_problems
    if not derive_problems:
        held.append(f"{'the derive invocations':32} {len(derive_copies)} copies across the skill's docs, "
                    f"every one naming the required set (--ledger or --required-set)")
    required_problems, required_copies = check_required_set_copies()
    problems += required_problems
    if not required_problems:
        held.append(f"{'the required-set invocations':32} {len(required_copies)} runnable copies, every one "
                    f"persisting to state.jsonl")
    liveness_problems, liveness_copies = check_liveness_copies()
    problems += liveness_problems
    if not liveness_problems:
        held.append(f"{'the liveness invocations':32} {len(liveness_copies)} runnable copies, every one "
                    f"answering --machine-action")
    for line in held:
        print(f"ok       {line}")
    for problem in problems:
        failures += 1
        print(f"FAIL     {problem}")

    print()
    if failures:
        print(f"{failures} disagreement(s) between {doc.name} and the code that runs. "
              f"ONE of them is wrong and a reader will believe the other.")
        return 1
    print(f"{len(checks) + len(held)} checks: {spec_doc.name}, {driver_doc.name}, ci-snapshot.py and "
          f"ci-status.py agree — enums, CLASSIFY buckets, TOTALITY, the DECIDE order, the caps, the "
          f"FINGERPRINT lines, moved-head and liveness contracts, and every copy of every command.")
    return 0


def fmt(v: object) -> str:
    if isinstance(v, (set, frozenset)):
        return " ".join(sorted(v))
    if isinstance(v, tuple):
        return " -> ".join(v)
    return "MISSING" if v is None else str(v)


def diff(want: object, got: object) -> tuple[object, object]:
    if isinstance(want, (set, frozenset)) and isinstance(got, (set, frozenset)):
        return want - got, got - want
    return None, None


# --- self-test: the executable contract lives in the SIBLING module -------------------------------

def load_test_module():
    """Import the sibling `ci-status-test.py` — the fixture suite, which is this tool's EXECUTABLE CONTRACT.

    It is a SIBLING FILE, not an inlined section, because an adversarial reviewer reads this file end to end
    and the suite doubles what they must read to find the producer. The rules stay pinned exactly as they
    were; only the reader's burden changes.

    **RESOLVED RELATIVE TO `__file__`, NEVER THE CWD.** The driver runs these scripts from arbitrary working
    directories, and the INSTALLED plugin cache is a different path from this repo — a cwd-relative load
    would find the suite on a developer's machine and nowhere else. Same rule, same reason, and the same
    mechanism as `load_snapshot_module()`: the hyphen in the filename means neither is importable as a
    module path, so both are loaded from an explicit `HERE`-relative location.

    **A MISSING SUITE IS A LOUD FAILURE, NEVER A QUIET PASS.** A `self-test` that reports success because it
    found no tests is the exact false green this whole tool exists to refuse, committed by the tool itself:
    zero evidence is not green, and that goes for evidence ABOUT THE TOOL as much as about a PR.
    """
    if not TEST_PY.exists():
        fail(
            f"the fixture suite is NOT AT {TEST_PY} — `self-test` has NO SUBJECT, and a check that cannot "
            f"find the thing it tests must FAIL, never pass. Reporting success here would be a green derived "
            f"from zero evidence, which is the bug this script exists to prevent."
        )
    spec = importlib.util.spec_from_file_location("ci_status_test", TEST_PY)
    if spec is None or spec.loader is None:  # pragma: no cover - a broken checkout, not a verdict
        fail(f"cannot load the fixture suite at {TEST_PY} — refusing to report a self-test that never ran")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def self_test() -> int:
    """Run the sibling suite over every fixture, then `doc-check`. Non-zero on any failure."""
    import tempfile
    tests = load_test_module()
    with tempfile.TemporaryDirectory() as tmp:
        return tests.run(sys.modules[__name__], Path(tmp))


def resolve_repo(fetch: Fetch) -> object:
    """WHICH REPOSITORY IS THIS CHECKOUT? The one fetch that happens outside `derive` — and it goes THROUGH
    THE DOOR, like every other read of a GitHub response.

    THIS READ USED TO BE `str(response.get("nameWithOwner"))` — and `str(None)` is the four perfectly
    parseable letters `"None"`, so a response we could not read became the repository `None`, every fetch was
    scoped to `repos/None/…`, and the tool reported a FETCH FAILURE about a repository that does not exist
    instead of the caller's actual problem. It failed closed, and it lied about why. `field()` refuses it,
    and the operator gets the error that is true: we cannot tell which repo this is.
    """
    return field("repo", fetch("repo", ["gh", "repo", "view", "--json", "nameWithOwner"]),
                 "nameWithOwner", str)


def main() -> int:
    p = argparse.ArgumentParser(description=next(iter((__doc__ or "").splitlines()), ""))
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("derive", help="fetch, promote, verify and decide a PR's CI status")
    d.add_argument("--pr", required=True)
    d.add_argument("--head-sha", required=True, help="the LEDGER's head_sha — the commit to pin the fetch to")
    d.add_argument("--rundir", required=True, type=Path, help="where the snapshot is promoted")
    d.add_argument("--repo", help="owner/name (default: the current checkout's, via `gh repo view`)")
    # THE REQUIRED SET IS NAMED, NEVER DEFAULTED — the same rule `ci-snapshot.evaluate()` enforces on ITS
    # callers, and for the same reason: a caller who forgot to say what the base branch requires must not be
    # handed the permissive answer. It is now the selected ROW's `effective_required_set` (the base is
    # per-row, so its required set is too), resolved from the ledger:
    #   --ledger <rundir>/state.jsonl
    # `--required-set` remains for the pure/explicit path (tests, an out-of-run spec): with `--ledger` it is
    # an ASSERTION that must equal the row's effective set; without `--ledger` it IS the set, verbatim. One of
    # the two is REQUIRED. `unknown` is a legal value that can NEVER go green (a `pending` bullet in DECIDE) —
    # exactly what makes a run that never performed the read merge NOTHING, instead of merging everything.
    d.add_argument("--ledger", type=Path,
                   help="the run's state.jsonl — resolve the row's `effective_required_set` (its explicit "
                        "`required_set`, else the legacy header value). The canonical production form")
    d.add_argument("--required-set",
                   help="an explicit required set (`declared:<json>` | `none` | `unknown`); with --ledger it "
                        "is an ASSERTION that must equal the row's effective set, not a second source")

    r = sub.add_parser(
        "required-set",
        help="read each distinct base's required-check sources and persist the canonical union per row",
    )
    r.add_argument("--ledger", required=True, type=Path, help="the run's state.jsonl")
    r.add_argument("--repo", help="owner/name (default: the current checkout's, via `gh repo view`)")

    lv = sub.add_parser(
        "liveness",
        help="apply the SETTLED/RUNNING-STALL/UNUSABLE liveness rules to one derive result: update the "
             "row's counters through the ledger accessor, and escalate (park) at a cap",
    )
    lv.add_argument("--ledger", required=True, type=Path, help="the run's state.jsonl")
    lv.add_argument("--pr", required=True, help="the PR the derivation is about")
    lv.add_argument("--derive-json", required=True,
                    help="path to the JSON `derive` printed, UNEDITED — or `-` to read it from stdin")
    lv.add_argument("--machine-action", required=True, choices=list(MACHINE_ACTIONS),
                    help="is work that can put a new commit on this PR's head due or in flight? "
                         "(stage-2-ci.md, MACHINE ACTION — the one judgment the caller supplies)")
    lv.add_argument("--now", help="ISO-8601 override of the clock (tests and reproduction only)")

    c = sub.add_parser("doc-check", help="assert the CI docs (ci-derivation-spec.md + stage-2-ci.md) "
                                         "agree with the code that runs — enums, CLASSIFY, DECIDE order, "
                                         "caps, fingerprint, moved-head artifact contract, and every copy "
                                         "of every command")
    c.add_argument("--spec-doc", type=Path, default=SPEC_DOC)
    c.add_argument("--driver-doc", type=Path, default=DRIVER_DOC)

    sub.add_parser("self-test", help="run every fixture (ci-status-test.py), then doc-check")

    args = p.parse_args()

    if args.cmd == "doc-check":
        return doc_check(args.spec_doc, args.driver_doc)

    if args.cmd == "self-test":
        return self_test()

    if args.cmd == "required-set":
        out = refresh_required_set(gh_fetch, args.ledger, args.repo)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0 if out["settled"] else 1

    if args.cmd == "liveness":
        if args.derive_json == "-":
            raw_text = sys.stdin.read()
        else:
            try:
                raw_text = Path(args.derive_json).read_text(encoding="utf-8")
            except OSError as exc:
                fail(f"liveness: cannot read --derive-json {args.derive_json!r}: {exc}")
        try:
            raw = json.loads(raw_text)
        except ValueError as exc:
            fail(f"liveness: --derive-json is not JSON ({exc}) — hand it the JSON `derive` printed")
        now = check_now("--now", args.now) if args.now else datetime.now(timezone.utc)
        out = liveness(args.ledger, args.pr, derive_output(raw), args.machine_action, now)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        # 3, not 1: the command DID its job and the answer is "this PR is now parked on you" — the same
        # STOP semantics as `ledger.py dispatch-check` (EXIT_STOP), distinct from an input error's 2.
        return 3 if out["escalated"] else 0

    # EVERY OPERATOR ERROR IS NAMED BEFORE THE FIRST FETCH. A caller's mistake surfacing later — as a crash
    # during promotion, or as a verdict about the PR — is a defect reported against the wrong thing.
    check_head_sha(args.head_sha)
    check_rundir(args.rundir)
    # The required set is the ROW's effective set when a ledger is named (the production form); an explicit
    # `--required-set` is the pure/test path AND, with `--ledger`, an assertion. One of the two is required —
    # a derive with NEITHER would have no set to decide under, and defaulting one is the false green this
    # whole tool exists to kill.
    if args.ledger is not None:
        required = resolve_required_for_derive(str(args.ledger), args.pr, args.required_set)
    elif args.required_set is not None:
        required = check_required_set(args.required_set)
    else:
        fail("derive needs --ledger (resolve the row's effective required set) or an explicit --required-set")

    repo = args.repo
    if not repo:
        try:
            repo = resolve_repo(gh_fetch)
        except FetchError as exc:
            fail(f"cannot determine the repo ({exc}) — pass --repo owner/name")

    out = derive(gh_fetch, repo, args.pr, args.head_sha, args.rundir, required)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    # green is the ONLY exit-0 verdict. Everything else — pending, red, unusable, an unclassified value —
    # is NOT a green, and a caller that checks only the exit status must never be told otherwise.
    return 0 if out["verdict"] == SNAP.GREEN else 1


if __name__ == "__main__":
    raise SystemExit(main())
