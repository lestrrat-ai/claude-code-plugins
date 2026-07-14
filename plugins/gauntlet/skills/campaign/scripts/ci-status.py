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
shortcut LOOKS like an answer. `ci-snapshot.py` cannot save you here: it verifies an artifact, so it is
only ever as good as the odds that somebody BOTHERED to produce one. **The fix is that deriving CI is now
a COMMAND, and eyeballing is not one of the things it can do.**

    ci-status.py derive --pr 31 --head-sha <40-hex> --rundir <rundir> --required-set <spec>

prints a verdict as JSON and exits 0 ONLY on green. Nothing here is judged by eye, and `gh pr checks` is
never read: its `--json` surface carries exactly ONE field (`bucket`) — no sha, no name, no conclusion —
so it can never say WHICH COMMIT it describes and can never be evidence. Use it to WAIT, never to decide.

WHAT WAS SUPPOSED TO BE THERE IS NOT A QUESTION THE EVIDENCE CAN ANSWER — SO IT IS AN INPUT (`--required-set`).
Every rule below quantifies over the rows we GOT. A REQUIRED CHECK THAT HAS NOT REGISTERED IS NO ROW AT ALL,
so no count, no marker and no cross-check can see it: they all agree, correctly, about a set that is missing
the one member that matters. The base branch's REQUIRED SET is the other half of the question, it is read
from branch protection AND rulesets (`stage-2-ci.md`, "WHAT WERE WE EXPECTING TO SEE?"), it is carried in the
ledger header, and it is passed in here — MANDATORY, with NO DEFAULT, because a caller who forgot must never
be handed the permissive answer. `unknown` (the read FAILED) is a PENDING outcome that escalates; it can
NEVER go green. `ci-snapshot.py` owns the rule, this tool's job is to HAND IT THE SET — see `derive()`.

SCOPE — WHY THIS IS A SEPARATE FILE FROM `ci-snapshot.py`, AND NOT A SUBCOMMAND OF IT.
The split is PRODUCER vs VERIFIER, and it is the same two-independent-sources principle that the snapshot
contract is built on:

  * `ci-snapshot.py` is a PURE, NETWORKLESS function from BYTES ON DISK to a verdict. That purity is what
    lets every one of its rules be pinned by an offline fixture and mutated by `mutate-ci-snapshot.py`.
    Putting a network fetch inside it would plant an un-fixturable, un-mutatable code path in the one file
    whose entire thesis is "every rule is executed, and every rule is pinned".
  * A VERIFIER MUST NOT TRUST ITS PRODUCER, and the surest way to make it trust one is to make them the
    same function. This repo has already shipped the shape of that bug: a SHA check built out of the SHA we
    had stamped ourselves, which matched by construction and COULD NEVER FAIL. One file that fetched and
    verified is one `return` away from handing back what it just fetched, and the fixtures would not notice.

So this file PRODUCES the artifact and then hands it to `ci-snapshot.py` — as a file, on disk, through the
exact same `evaluate()` a reviewer would run by hand. **NOT ONE CLASSIFICATION RULE, AND NOT ONE LINE OF
THE DECIDE ORDER, IS RE-IMPLEMENTED HERE.** They are IMPORTED. A second copy of `FAIL_CONCLUSIONS` in this
file would be a second owner of the rule, and the day they disagreed the tool would be lying in whichever
direction the reader did not check. The verdict you get from this command is, byte for byte, the verdict
`ci-snapshot.py verify` gives for the artifact it leaves behind — and it leaves the artifact behind
precisely so that claim is AUDITABLE and not merely asserted.

EVIDENCE WE KNOW IS INCOMPLETE IS NOT EVIDENCE. NOTHING HERE DISCLOSES A GAP AND GREENS ANYWAY.
This is the same false green as the one above, one level in: not "no evidence, called green", but "evidence
GitHub ITSELF told us was short, called green". Two places could produce it, and both now FAIL CLOSED:

  * **A SHORT READ.** Both REST families return GitHub's OWN `total_count` for the commit — the number of
    rows it holds, ACROSS PAGES (verified: 27 check runs at `per_page=5`, six pages, every page reporting
    `total_count=27`). If the paginated read collected FEWER, a row GitHub holds is NOT IN OUR HANDS, and
    the row that is missing could be the FAILING one. `read_pages()` refuses. It used to write a NOTE
    and return green — a green computed from evidence the tool KNEW had a hole in it, with the hole
    politely printed beside it. A count we cannot READ (`total_count` absent, or not an integer) is refused
    for the same reason `headRefOid` is: a fail-closed rule that cannot fire is not a rule.

  * **AND A RESPONSE THAT CONTRADICTS *ITSELF* ACROSS ITS PAGES.** The rule above was asked of `pages[0]`
    ALONE, which is not the same rule at all: page one can say the read is COMPLETE while page two — of the
    same response, from the same GitHub — says rows are MISSING. A reviewer built exactly that, in both
    families, and `derive()` returned GREEN with the short read printed in the evidence it had just fetched.
    Every per-commit fact GitHub repeats on every page (`total_count`; the status family's `.sha`) is now
    reconciled ACROSS the pages by `agreed()`, and every paginated read goes through the ONE door that does
    it (`read_pages`) — a fetcher cannot parse pages itself, so it cannot forget. **THIS IS THE SAME BUG AS
    THE ONE BELOW, and that is the whole lesson: the tool reconciled REST against the rollup and did not
    reconcile a response against ITSELF.** An absent count is not a count of zero; a page one did not read is
    not a page that agreed.
  * **A ROLLUP `StatusContext`.** The rollup returns two entry types; this tool kept `CheckRun` and DROPPED
    the rest ON THE FLOOR. A `StatusContext` in state `EXPECTED` is *a REQUIRED status check that has not
    been posted yet* — and no VERDICT source can see it: the REST commit-status API has no `EXPECTED` state,
    so the family that carries status verdicts cannot express it, by construction. Dropping it silently
    reported GREEN for a PR that is BLOCKED on a check nobody has run. `build_snapshot()` now requires every
    rollup `StatusContext` to be VISIBLE in the REST status family (posted statuses are — verified live: a
    Prow PR whose rollup contexts `tide`/`EasyCLA` both appear in `/status`), and refuses when one is not. An
    entry of a `__typename` we do not know is refused too: a row we cannot read is not a row we may drop.
    **AND SO IS A `StatusContext` WHOSE `state` IS NOT IN THE `StatusState` ENUM** — that value NEVER ENTERS
    THE ARTIFACT (the rollup may not be a verdict source), so no rule downstream can ever refuse it: it is
    refused HERE or it is accepted for good. It was accepted for good, and a reviewer proved what that costs
    — an invented `BRAND_NEW_FAILURE` beside a `SUCCESS` REST row for the SAME context, and `derive()`
    returned GREEN. AN UNRECOGNISED VALUE IS NOT A BENIGN VALUE, in any field, from any source.

    **AND THAT COVERAGE RULE IS NOT WHAT CLOSES THE `EXPECTED` FALSE GREEN. IT CANNOT BE — READ THIS BEFORE
    YOU TRUST IT.** It quantifies over the `StatusContext` entries THE ROLLUP RETURNED, and the rollup
    carries NO total: unlike both REST families it cannot be proven complete (`stage-2-ci.md`, "Honest
    limits"). Delete the one `EXPECTED` entry from a rollup response and the guard has NOTHING TO CHECK, and
    the PR — blocked on a check nobody has run — goes GREEN. A reviewer did exactly that to a fixture and
    watched the verdict flip. **A GUARD WHOSE INPUT CAN BE ABSENT NEVER FIRES**, and this file has now paid
    for that lesson three times (zero checks; a family never fetched; this). The closure is the REQUIRED SET,
    above: it is DECLARED BY THE BASE BRANCH, so what must be present does not depend on what showed up, and
    a required check missing from the rollup AND from REST is caught by the thing that SAYS IT MUST BE THERE.
    What the coverage rule is still FOR is stated at its own site (`build_snapshot`) — it is a CROSS-SOURCE
    consistency check, not the registration gap's closure, and it must never again be sold as one.

  * **AND TWO SOURCES THAT DISAGREE.** Every rule above asks whether a check EXISTS in the evidence. NONE of
    them asked whether the two sources SAY THE SAME THING ABOUT IT — so the tool believed whichever one it
    could parse, and it was GREEN for a PR the rollup was calling FAILED. Twice: a `StatusContext` REST
    reported as `success` and the rollup as `FAILURE`, and (the half nobody had looked at) a `CheckRun` whose
    `status`/`conclusion` the rollup HANDS US and this file dropped on the floor while keeping its name. THE
    REST FAMILIES ARE FETCHED BEFORE THE ROLLUP, so a check that flips to failure between the calls makes two
    HONEST sources contradict each other with the head never moving — no moved-head rule can fire on it.
    `build_snapshot()` now REFUSES a conflict (`agree_or_refuse`), and it does NOT resolve it: not by
    preferring REST (it is first, not right), not by preferring the rollup (no oid — never a verdict), and
    never by taking the kinder of the two. Compared as BUCKETS, so `success` vs `SUCCESS` is not a conflict
    and `pending` vs `EXPECTED` is not either — see `status_bucket` / `checkrun_bucket`.

**THERE IS NO `notes` CHANNEL, ON PURPOSE.** A field that says "this evidence may be incomplete" BESIDE a
green verdict is the trapdoor, not the disclosure — it was read by nobody, and it let the tool ship the one
thing it exists to prevent. Every known gap is a REFUSAL now, so there is nothing left to disclose next to
a green. What CANNOT be known is stated where it belongs (`stage-2-ci.md`, the FETCH bullets), never
emitted as reassurance beside a verdict.

EVIDENCE ABOUT A COMMIT THAT IS NO LONGER THE HEAD IS NOT EVIDENCE ABOUT THE PR. The fetch is pinned to the
LEDGER's `head_sha`, and a push can land at any time — including WHILE this tool is fetching. So the tool
also reads the PR's CURRENT head, LAST (after both evidence families), and if it has MOVED the verdict is
`unusable`, NEVER green and never red: green would merge a PR on checks that never ran against its head —
the same false green this file exists to kill, one level deeper — and red would be a claim about the wrong
commit too. `ci = pending`, and the reason NAMES the new head so the driver re-derives against it rather
than guessing. See `derive()`.

WHAT IT DOES NOT DECIDE, ON PURPOSE. It answers **what the evidence says**, and **whether that evidence is
about this PR at all**. It does NOT answer **what the driver should do about it** — whether to launch a
watch, dispatch a CI fix, or park the PR. Those rules live in `stage-2-ci.md` and they are being rewritten
as this lands (PR #31: SETTLED, RUNNING-STALL, the bounded refetch, the ESCALATE park). Encoding them here
would create a SECOND owner of a rule that is moving under it — a stale restatement by construction. This
file's output gives the driver everything those rules read (the verdict, the reason, the evidence counts,
the head that superseded ours) and states nothing about what to do with them.

THE DOC AND THIS TOOL CANNOT SILENTLY DISAGREE — `doc-check` is what makes that true.
The enums, the CLASSIFY buckets and the DECIDE order are stated in `stage-2-ci.md` as prose AND encoded in
`ci-snapshot.py` as Python. NOTHING compared them, so they could drift, and the drifted copy would be the
one a reader believed. `doc-check` PARSES the doc's own enum block, its two CLASSIFY tables and its DECIDE
bullet order, and asserts they agree with the sets `ci-snapshot.py` actually classifies with — and that the
classification is TOTAL over the enums the doc declares (every value in exactly one bucket, no value in
two, no bucket holding a value the enum does not have). It runs in CI and in `self-test`. Drift is now a
RED BUILD, not a discovery.

AND IT READS THE FETCH COMMANDS TOO — because the version that did NOT is where the doc actually drifted.
An alarm with a blind spot tells you where the next defect will land, and this one landed there: `doc-check`
compared enums and DECIDE order and NEVER PARSED THE `gh … | jq` BLOCK, so the doc went on saying
`(.statusCheckRollup // [])` — a MISSING rollup laundered into an EMPTY one — while `fetch_rollup` refused
exactly that shape. The doc and the code disagreed about WHAT IS REFUSED, in the one place nothing looked.
So the doc's fetch commands are now EXECUTED (`check_fetch_spec`): its `jq` filters are run over the
fixtures' recorded responses, and for EVERY fixture and EVERY source the doc must give the SAME ANSWER as
this file's producer — the same rows, or the same REFUSAL. Its `gh` invocations are checked against the argv
the code really issues, EVERY COPY OF THEM in the doc (a recap that quietly drops `,headRefOid` is a reader
reconstructing a fetch the moved-head rule cannot fire on). One refusal is CROSS-SOURCE and no single-fetch
filter can express it — the rollup's `StatusContext` coverage — and `doc-check` PRINTS that limit rather
than letting it pass for coverage. Executing the spec needs `jq`; if `jq` is missing, `doc-check` FAILS.

**A check that finds nothing MUST NOT PASS.** If the doc cannot be found, or a block cannot be parsed, or
zero rules are extracted, or zero (fixture, source) pairs are compared, `doc-check` FAILS. An extractor that
silently matches nothing and reports success is the false green of this whole story, one level up, in the
tool written to prevent it.

  derive     fetch a PR's checks, promote the snapshot, verify it, and print the verdict as JSON
  doc-check  assert stage-2-ci.md's enums / CLASSIFY / DECIDE order AND its executed fetch spec agree with
             the code that runs
  self-test  run every fixture, assert its verdict AND the rule that produced it, then run doc-check
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Callable, NamedTuple, NoReturn

HERE = Path(__file__).resolve().parent
SNAPSHOT_PY = HERE / "ci-snapshot.py"
DOC = HERE.parent / "references" / "stage-2-ci.md"
FIXTURES = HERE / "fixtures" / "ci-status"

# `doc-check` EXECUTES the doc's own `jq` filters (see `check_fetch_spec`). Its ABSENCE is a FAILURE, never a
# skip: a check that quietly does not run is the false green this whole file exists to refuse, one level up.
JQ = shutil.which("jq")

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
#
# PAIRS, NOT A DICT, and that is not a style choice: `result()` — which reads this — is INSIDE the producer,
# and the producer may not perform a raw read of ANY kind (`check_field_shapes`). `LEDGER_CI[verdict]` is a
# subscript, and the scan refuses every subscript with no exempt object, because an exempt object is the hole
# the last two bypasses went through. The lookup is a scan of pairs instead, and it costs one line.
LEDGER_CI = (
    (SNAP.GREEN, "green"),
    (SNAP.RED, "red"),
    (SNAP.PENDING, "pending"),
    (SNAP.UNUSABLE, "pending"),
    (SNAP.UNVERIFIABLE, "pending"),
    (SNAP.UNCLASSIFIED, "pending"),
)


def ledger_ci(verdict: str) -> str | None:
    """The ledger column this verdict maps to — or None, which `result()` refuses rather than guesses."""
    return next((column for outcome, column in LEDGER_CI if outcome == verdict), None)

# The DECIDE order, as a NAME PER BULLET, in the order `stage-2-ci.md` evaluates them. This is a THIRD
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
# the questions no ROW can answer, so they are asked once every row has already passed. `pending (required
# set unreadable)` and `pending (required check missing)` are `pending` OUTCOMES — non-merging, bounded and
# escalated — never a caveat under a green. `required-set-unreadable.json` and `required-check-absent.json`
# drive them behaviourally; this line is what pins that the DOC still evaluates them in that order.
DECIDE_ORDER = ("UNUSABLE", "red", "UNKNOWN_VALUE", "pending", "pending (nothing registered)",
                "pending (required set unreadable)", "pending (required check missing)", "green")

# THE RULES THIS SCRIPT OWNS — the inventory `mutants` reconciles against the `# MUTATE:` markers in the
# source, in BOTH directions: a rule named here with no marker is a rule that is never mutated, and a marker
# with no entry here is a rule nobody declared. Both FAIL.
#
# **THE HONEST LIMIT, because this is the half of the coverage question that CANNOT be derived.**
# `mutate-ci-snapshot.py` can AST-scan `ci-snapshot.py` and *discover* its rules, because every rule there
# has one shape: a `raise` or a `return <verdict>`. THE RULES HERE HAVE NO SUCH SHAPE — a producer's rule is
# a CHOICE OF VALUE ("take the sha from the RESPONSE, never from our own literal"), and no AST scan can tell
# that assignment from any other. So this inventory is DECLARED, and what is mechanically checked is that it
# is CONSISTENT (markers <-> names) and that every rule in it is PINNED BY A FIXTURE. What is NOT checked —
# say it plainly rather than dress the gap up — is a rule ADDED to this file and left out of BOTH this dict
# and the markers. Adding a rule here without marking it is the one way to add an untested rule to this
# script, and nothing but review will catch it.
#
# **AND THAT IS NOT HYPOTHETICAL — REVIEW CAUGHT NINE.** The matrix printed "all 18 rules are pinned" and it
# was TRUE and it was NOT ENOUGH: the sentence is about the rules IN THIS DICT, and NINE REAL GUARDS WERE NOT
# IN IT. Deleted one at a time, each left the entire suite AND the entire matrix green — the completeness
# call on the STATUS family (the body was marked, both call sites were not, so either fixture killed the
# marker and neither killed the deletion); three response-SHAPE guards; `gh_fetch`'s two rules (no fixture
# runs it — every fixture REPLACES it); and the two CLI operator-error guards (no fixture calls `main`).
#
# **AND THE NEXT AUDIT CAUGHT TEN MORE — INSIDE `doc-check` ITSELF.** Every guard in the alarm (the doc is
# not there; the enum block is gone; the CLASSIFY tables parse to nothing; the catch-all is missing; the
# DECIDE section is gone or lists no outcomes; a fetch command is MISSING or DUPLICATED; a copy of the derive
# command has dropped `--required-set`; the sweep found no copies at all) was reachable ONLY FROM A BROKEN
# DOC — and every doc in the tree is intact, so no case ever ran one. Nine were message specialisations; ONE
# WAS LOAD-BEARING (`doc-fetch-spec-complete`: without it a doc that had LOST AN ENTIRE `gh` COMMAND passed,
# the spec executed against whatever remained, printing `ok`). They are driven now by BROKEN DOCS BUILT AT
# RUN TIME (`DOC_EXPECT`) — never written into the tree, because a doc that is deliberately wrong is a doc
# somebody reads.
#
# **THE COUNT IS A CLAIM, AND THE CLAIM WAS WRONG TWICE. The method that found that out is the only one that
# works, and it is not reading:** take each rule, DELETE IT ALONE, and run everything. Something must go red.
# If nothing does, the rule is decoration. Do this for every guard in the file — not just the ones already
# listed here, because the ones NOT listed are exactly where the answer will surprise you.
#
# **WHAT IS DELIBERATELY *NOT* IN HERE, AND WHY — the exclusions are named so the next audit starts from a
# list and not from zero.** Each was DELETED ALONE and its consequence MEASURED, not assumed:
#   * `fail()` itself, and `load_snapshot_module`'s refusal — a BROKEN CHECKOUT (no `ci-snapshot.py`), not a
#     claim about a PR. No fixture can construct it without deleting the file the suite imports.
#   * `result()`'s "DECIDE returned a verdict I cannot map" — UNREACHABLE by construction: `LEDGER_CI` is
#     TOTAL over the six verdicts `ci-snapshot.py` can return. It fires only if that file grows a SEVENTH,
#     which is the day it earns its place. Marking it would report it "pinned by NOTHING" forever.
#   * `main()`'s "cannot determine the repo" — a DIAGNOSIS, not a safety rule: delete it and `repo` stays
#     `None`, every fetch URL is malformed, `gh` fails, and the verdict is `unusable`. It still FAILS CLOSED;
#     what is lost is the good error message, and no verdict can pin a message.
#   * `run_fixture`'s "this fixture declares no `required_set`" and `fixture_fetch`'s refusals — HARNESS
#     scaffolding. They guard the SUITE against a malformed fixture, and they are not rules about a PR.
#   * `parse_enums`' two inner refusals (a line it cannot read; a block that parses to ZERO enums) — both sit
#     BEHIND the block-not-found guard that IS pinned, and neither can be reached without a doc whose enum
#     block exists, names the enum, and yet holds no parsable enum line.
#   * `check_fetch_spec`'s `compared == 0` and `JQ is None` — fail-closed backstops whose input the suite
#     cannot construct (green.json must exist for `code_argv`, and `jq` is a hard dependency of the check).
#     They cost nothing and they refuse; a case that could reach them would have to break the harness first.
RULES = {
    "evidence-sha-from-response": "a checkrun row's sha is GITHUB'S `.head_sha`, NEVER the sha we asked for",
    "status-sha-from-response": "a status row's sha is the response's own top-level `.sha`",
    "checkruns-marker-sha": "the check-runs marker's sha is GitHub's, and `-` ONLY when it returned no rows",
    "status-marker-sha": "the status marker's sha is GitHub's own — present even at ZERO statuses",
    "rollup-marker-sha": "the rollup marker's sha is ALWAYS `-`: the rollup carries no commit oid to copy",
    "both-families-checkruns": "the check-run family is FETCHED — a family never read reports nothing, and nothing parses as nothing-wrong",
    "both-families-status": "the commit-status family is FETCHED — /check-runs CANNOT SEE a failing Jenkins status",
    "rollup-witnesses": "the rollup is read for WITNESSES — with none, containment passes TRIVIALLY",
    "rollup-entries-present": "a rollup response with NO entry list FAILS CLOSED — an EMPTY rollup is a fact, a MISSING one makes containment vacuous",
    "rollup-entry-known": "a rollup entry of an UNKNOWN `__typename` FAILS CLOSED — a row we cannot read is not a row we may drop",
    "rollup-status-state-known": "a rollup `StatusContext` in an UNKNOWN `state` FAILS CLOSED — the value never enters the artifact, so NO rule downstream can refuse it: accepted here it is accepted for good, and the PR goes GREEN on a state nobody has classified",
    "rollup-status-covered": "a rollup `StatusContext` the REST status family CANNOT SEE fails closed — the two sources DISAGREE about what exists (NOT the registration gap's closure: see `required-set-is-passed`)",
    # EXISTENCE IS NOT AGREEMENT, and the rule above only ever asked about existence. THE THREE BELOW ARE THE
    # OTHER HALF: the two sources report the same check, and they say DIFFERENT THINGS ABOUT ITS STATE. One
    # rule BODY (`agree_or_refuse`) and TWO APPLICATIONS, marked separately for the reason the two
    # `*-through-the-seam` rules are — a body no family calls is not a rule, and one marker over two call sites
    # reports a rule PINNED while half of what it guards is unguarded.
    "sources-agree": "two sources that CONTRADICT each other about one check's state FAIL CLOSED — untrustworthy evidence is not green, and the conflict is NEVER resolved by preferring the source we happened to read first",
    "status-agrees": "the commit-status family IS SUBJECT to that test — rollup `FAILURE` beside REST `success` for one context was GREEN",
    "checkrun-agrees": "the check-run family IS SUBJECT to it TOO — the rollup carries each `CheckRun`'s status/conclusion, and this tool used to read its NAME and throw its VERDICT away",
    "required-set-is-passed": "the verdict is decided UNDER THE BASE BRANCH'S REQUIRED SET — a required check that never registered is NO ROW, and no rule that reads rows can see it",
    "head-read-last": "the PR's CURRENT head is read AFTER the evidence — a head read FIRST cannot see a push that lands mid-fetch",
    "head-must-be-known": "a rollup response with NO headRefOid is a FAILED fetch — an unknown head makes the fail-closed rule below unable to fire",
    "head-moved-is-not-evidence": "a MOVED head FAILS CLOSED — evidence about a commit that is not the head is not evidence about the PR",
    "fetch-failure-is-not-evidence": "a `gh` call that FAILS yields NO verdict from evidence, and promotes NOTHING",
    "verdict-from-snapshot": "the verdict comes from ci-snapshot.evaluate() over the PROMOTED BYTES — never from what we think we fetched",
    # --- THE PAGES OF ONE RESPONSE ARE TWO SOURCES, AND THEY MUST AGREE (`read_pages` / `agreed`) ---------
    # The FIFTH false green, and the same shape as the other four: two sources that disagree about a fact,
    # and the tool trusts one of them. Here they are two PAGES of one response. Page one said `total_count`
    # matched the rows collected, page two said GitHub held more, and `derive()` returned GREEN — in both
    # families, and again when page two's count was ABSENT, and again when it was a STRING.
    #
    # **THESE FOUR REPLACE FIVE OLDER NAMES, AND NOTHING THEY PINNED HAS BEEN GIVEN UP** — say that out loud,
    # because a rule that vanishes from this dict looks exactly like a rule that was dropped:
    #   * `evidence-count-known` -> `page-fact-known`, which asks it of EVERY page instead of page one, and of
    #     the status `.sha` as well as the count (the sha was read off page one and STAMPED on every row);
    #   * `checkruns-pages-are-an-array` + `status-pages-are-an-array` -> `pages-are-an-array` (ONE body now,
    #     because there is ONE page-reader) + `page-is-an-object`, which is new: a non-object page used to
    #     reach `.get` and CRASH, and a crash is not a refusal;
    #   * `checkruns-complete` + `status-complete` -> `checkruns-through-the-seam` + `status-through-the-seam`.
    #     The per-family application is still ITS OWN RULE with ITS OWN MARKER and ITS OWN FIXTURES — that
    #     lesson (a body no family CALLS is not a rule; two call sites must never share one marker) is not
    #     weakened but STRENGTHENED: each weakening now bypasses the whole page-reader for that family, which
    #     is the code this file actually shipped, and that family's fixtures must catch it alone.
    # --- AND A FIELD THAT IS NOT THERE IS NOT A FIELD THAT IS EMPTY (`field` / `check_field_shapes`) -------
    # The SIXTH false green, in the seam built ONE ROUND AGO to stop the fifth. `page.get(rows_key) or []`:
    # delete a page's `statuses` member, leave `total_count: 0`, and every rule agreed — the count matched
    # the rows collected, the pages agreed, containment held — GREEN. The class is not "that line": it is a
    # field read that DECLARES NOTHING, and so can REFUSE nothing. Every field of a GitHub response now comes
    # through `field()`, which takes a SHAPE and never a default, and a fetcher that reads one any other way
    # is named by an AST scan and the suite goes RED.
    "rows-are-a-list": "EVERY page carries its rows as a LIST — a page MISSING the key is NOT a page with an EMPTY one, and `or []` said it was: an otherwise-green response with its `statuses` member deleted returned GREEN",
    "field-shape-declared": "a field whose DECLARED shape the response does not have is REFUSED — absent, null, or the wrong type, and NEVER defaulted to the benign-looking value the caller forgot to think about",
    # AND THE SCAN THAT PROVES THE DOOR HAS NO WAY AROUND IT — which SHIPPED WITH THREE WAYS AROUND IT. It saw
    # only STRING-LITERAL subscripts (so `pages[0][rows_key]` walked past it); it EXEMPTED an object by the
    # NAME of the local holding it (so `facts = {}` in a reader was exempt); and it scanned only the FETCHERS,
    # so a response HANDED BACK to `build_snapshot` was read there by nobody's rule at all. All three left the
    # suite GREEN. The exemption is DELETED, not narrowed; the check is TOTAL over the forms of a read (every
    # subscript, every dict-reading method — no exempt key, no exempt object); and its REGION is now the whole
    # PRODUCER, not the fetchers. Zero exemptions is the only number that cannot be spoofed, and the region
    # has to be everywhere the response can GET TO or the exemptions come back as geography.
    "field-reads-through-the-seam": "NOTHING in the producer reads a field any other way — a raw `.get()` (or any dict-reading method), ANY raw subscript (literal key, variable key, computed key, on ANY object), a `field()` call with no shape, or a fetch inlined into `build_snapshot` is caught by an AST scan of the code that RUNS, because a door with a way around it is not a door",
    "field-scan-has-a-subject": "the scan DERIVES its readers from the fetch seam, and a source in which NOTHING is handed that seam FAILS — an empty reader set would pass every source on Earth, and a check that finds nothing must never report health",
    # **THE GUARD THAT KEPT ASSERTING SOMETHING NARROWER THAN IT MEANT, AND THE SIXTH TIME THAT SHAPE HAS COST
    # A FALSE GREEN HERE.** It MEANT "no raw GitHub response can be read outside a scanned reader" and it
    # ASSERTED "no fetcher directly returns the seam call or a name bound to it". A fetcher that returned
    # `{"raw": data}` — or `identity(data)`, or `[data]` — handed the response to `build_snapshot`, which the
    # reader set CANNOT contain (it is where the seam is handed out), and the raw subscripts there were
    # scanned by NOBODY. Both were driven through the scan with the suite still green.
    #
    # The fix is NOT another shape in the hand-back rule: that is a race the next reviewer wins (a closure, a
    # comprehension, a default argument). **The READS are removed instead.** Every function that can HOLD a
    # response is scanned — the producer, not the fetchers — and in ALL of it a raw read is refused. A
    # laundered hand-back is then harmless: there is nowhere left to read it.
    "no-raw-read-outside-the-door": "the raw-read refusal covers THE WHOLE PRODUCER, not just the fetchers — `build_snapshot` and every helper it calls read NOTHING raw, so a response HANDED BACK to them (wrapped in a container, laundered through a helper) has nowhere left to be read",
    "fetchers-never-return-a-response": "a fetcher does not hand the seam's own value back BARE — a narrow rule that names the mistake in the fetcher that made it, and NEVER what makes a hand-back safe (see `no-raw-read-outside-the-door`, which is what does)",
    "pages-are-an-array": "a `--slurp` that did not yield a NON-EMPTY ARRAY is a fetch we cannot read — never rows to iterate, and never zero pages to quantify over vacuously",
    "page-is-an-object": "EVERY page is an OBJECT — a page we cannot read used to reach `.get` and CRASH, and a crash is not a refusal: no verdict was reached at all",
    "page-fact-known": "EVERY page must STATE the facts GitHub repeats on all of them (`total_count`; the status `.sha`) — a page that does not is not a page that agrees, and an absent count is NOT a count of zero",
    "pages-agree": "the PAGES OF ONE RESPONSE must not CONTRADICT each other — page 1 saying the read is complete while page 2 says rows are missing was GREEN, and the tool that reconciles REST against the rollup was not reconciling a response against ITSELF",
    "evidence-is-complete": "a read SHORTER than GitHub's own total_count FAILS CLOSED — the row we did not get could be the failing one",
    # THE RULE BODY AND ITS APPLICATION ARE TWO RULES, and the two below are the second kind. A guard is not
    # enforced by EXISTING; it is enforced by being CALLED, once per family — and a call site nothing pins is
    # a call site that can be deleted with the suite still green. It happened: the status family's call was
    # removed and NOTHING went red, because the body's own markers were killed by the OTHER family's fixture.
    # Never let two applications of one rule share one marker: the harness cannot tell them apart, and it
    # will report a rule PINNED while half of what it guards is unguarded.
    "checkruns-through-the-seam": "the check-run family reads its pages THROUGH `read_pages` — the weakening is the raw response, page one believed and the rest unread, which is the code that shipped",
    "status-through-the-seam": "the commit-status family goes through the SAME door — and this is the family that carries the failing Jenkins status",
    "rollup-is-an-object": "`gh pr view --json` returns an OBJECT — anything else is a response we cannot read",
    # ANYTHING THIS TOOL ACCEPTS, IT MUST USE OR REFUSE. `--repo` was accepted and honoured by TWO of the
    # three fetchers: the REST pair put it in the URL, and the rollup ran `gh pr view <pr>` — which resolves
    # the PR in the CURRENT CHECKOUT. The rule is not "the rollup passes --repo" (that is the instance); it
    # is that NO fetcher is handed the repo at all, so a new one CANNOT forget it.
    "every-fetch-is-repo-scoped": "every GitHub call NAMES THE REPOSITORY — an argv that does not is REFUSED, so a fetcher cannot silently run against the checkout it happens to be standing in",
    # AND WHERE IT NAMES IT IS A POSITION, NOT A STRING SOMEWHERE IN THE ARGV. The rule above was fixed once
    # for `gh pr view` (the repo must be the word after `--repo`, not a repo-shaped `--template`) and the
    # `gh api` half was left testing ANY argument — so `gh api repos/wrong/repo/… --template repos/o/r/x`
    # satisfied it and queried `wrong/repo`. A guard that accepts a STRING where it means a POSITION can be
    # fed the string; this one is the position.
    "api-endpoint-is-a-position": "a `gh api` call is scoped by its ENDPOINT — the operand `gh api` will use as the path, never a repo-shaped value in some other flag",
    # THE SEAM ITSELF, and the CLI. `gh_fetch` is the ONLY code path that talks to GitHub, and every fixture
    # REPLACES it — so nothing executed its rules. `seam_cases` drives them against a local process.
    "gh-exit-is-checked": "a NON-ZERO exit from `gh` is a FAILED FETCH — the doc's shell version needs pipefail to learn this",
    "gh-stdout-is-json": "stdout that is not JSON is a FAILED FETCH, not a CRASH — a raise where a verdict was owed is no verdict",
    "cli-head-sha-is-an-oid": "a `--head-sha` that is not a git object id is an OPERATOR ERROR (exit 2), never a verdict about the PR",
    "cli-rundir-exists": "a `--rundir` that is not a directory is an OPERATOR ERROR — named before the fetch, not as a crash during promotion",
    "cli-required-set-readable": "a `--required-set` we cannot PARSE is an OPERATOR ERROR (exit 2) — NEVER degraded to `none`, which would say 'nothing is required' on the strength of a value we failed to read",
    # THE ALARM'S OWN GUARDS. `doc-check` is the thing that stops the doc and the code drifting apart, and
    # ITS subject can go missing too: an extractor that matches NOTHING and reports success is this file's
    # founding defect, one level up, inside the tool written to prevent it. Every one of these was pinned by
    # NOTHING until the audit deleted it alone — and `doc-fetch-spec-complete` was not a message
    # specialisation but LOAD-BEARING: without it a doc that had lost an entire `gh` command passed.
    "doc-has-a-subject": "a doc that is NOT THERE fails the check — it never passes for want of a subject",
    "doc-enum-block-found": "an enum block that is GONE or renamed FAILS — never an empty enum set, which agrees with anything",
    "doc-classify-found": "CLASSIFY tables that parse to NOTHING FAIL — zero rules agree with every rule set",
    "doc-classify-catch-all": "a CLASSIFY table with NO `ANY OTHER VALUE` catch-all FAILS — without it tomorrow's enum value falls in a HOLE and the PR WEDGES",
    "doc-decide-section-found": "a DECIDE section that is GONE FAILS — an empty order agrees with any order",
    "doc-decide-bullets-found": "a DECIDE section listing ZERO outcome bullets FAILS — it cannot be checked, so it does not pass",
    "doc-fetch-spec-complete": "a FETCH command MISSING from the doc FAILS — LOAD-BEARING: without it the spec is executed against the commands that happen to remain, and reports `ok`",
    "doc-fetch-spec-unique": "TWO fetch commands for one source FAILS — with two copies, `doc-check` executes one and the reader may follow the other",
    "doc-rollup-repo-scoped": "a rollup fetch in the doc with NO `--repo` FAILS — `gh pr view <pr>` resolves the PR in the CURRENT CHECKOUT, and a reader following that copy asks the wrong repository",
    "doc-derive-required-set": "a COPY of the derive command that drops `--required-set` FAILS — the recap is where a merge-deciding flag goes to die",
    "doc-derive-copies-found": "finding ZERO copies of the derive command FAILS — the sweep would otherwise pass by having swept nothing",
    "doc-cross-source-stated": "a CROSS-SOURCE rule the fetch spec CANNOT express must be STATED IN THE DOC — `SPEC_CANNOT_EXPRESS` is the only list of rules `doc-check` does not execute, so a rule that drops out of the doc drops out of EVERYTHING",
}


class OperatorError(Exception):
    """The caller asked a question that cannot be answered — never a verdict about the PR."""


class FetchError(Exception):
    """A source could not be read. The snapshot is NOT promoted and there is NO verdict from evidence."""


def fail(msg: str) -> NoReturn:
    print(f"ci-status: {msg}", file=sys.stderr)
    raise SystemExit(2)


# --- the OPERATOR-ERROR guards: functions, so that ONE owner is both CALLED by `main` and DRIVEN by the
# suite. Inline in `main()` they were reachable ONLY through the CLI — which no fixture goes through — so
# both were pinned by NOTHING, and deleting either left the whole matrix green. `seam_cases` drives them.

def check_head_sha(head_sha: str) -> str:
    """An OPERATOR ERROR is not a verdict about the PR. A `--head-sha` that is not a git object id makes
    every comparison downstream unfalsifiable, and blaming the EVIDENCE for the caller's mistake is how a
    tool reports a defect that is not there. Exit 2: no verdict at all beats a verdict about the wrong
    question."""
    # MUTATE:cli-head-sha-is-an-oid:pass
    if not SHA_RE.match(head_sha):
        fail(f"--head-sha {head_sha!r} is not a git object id (40 LOWERCASE hex) — refusing to derive")
    return head_sha


def check_rundir(rundir: Path) -> Path:
    """`promote()` writes the artifact INTO <rundir>; a rundir that does not exist is a caller mistake, and
    it must be named as one BEFORE any fetch — not surface later as a crash in the middle of promotion,
    where it would look like a defect in the evidence."""
    # MUTATE:cli-rundir-exists:pass
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
    required set exists to remove, one layer down. `SpecError` says so loudly; this turns it into an
    operator error and NO verdict at all, which beats a verdict about the wrong question.
    """
    try:
        return SNAP.parse_required_set(spec)
    except SNAP.SpecError as exc:
        # The weakening below is the DEGRADATION itself — "we could not read it, so call it `none`" — which
        # is why the mutant must be killed by the SEAM case (no fixture carries an unreadable spec).
        # MUTATE:cli-required-set-readable:return SNAP.RequiredSet(SNAP.NONE_DECLARED)
        fail(
            f"--required-set {spec!r} cannot be read ({exc}) — and a spec we cannot read is NOT `none`. "
            f"It is the ledger header's `required_set` (`ledger.py … header get required_set`), and "
            f"guessing at it would say 'the base branch requires nothing' on the strength of a value we "
            f"failed to parse."
        )


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
# let an unscoped command past. `check_repo_scoping()` asserts the same thing over the argv the code REALLY
# ISSUES (`code_argv`, derived from the running code — never a hand-written list, which is how a list goes
# stale the day a fetcher is added), and `[argv] a fetcher that forgets the repo` drives the refusal.
REPO_SLOT = "{repo}"


def adjacent(argv: list[str]):
    """Every (flag, the word after it) pair in an argv, WITHOUT INDEXING IT.

    `argv[i + 1]` would be a raw subscript inside a function the field-shape scan reaches, and that scan now
    refuses EVERY subscript in EVERY reader it scans — no exempt key, no exempt object (see
    `check_field_shapes`). The rule is blunt on purpose, and this is the price: two lines, no indexing, and
    one less way for a raw read to hide behind a "but this one is fine".
    """
    after = iter(argv)
    next(after, None)
    return zip(argv, after)


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
# Flags that carry values are written AFTER the endpoint (as `gh` accepts them, and as this tool writes them
# nowhere at all), where nothing consults them.
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

    Written without indexing the argv: this function is reachable from `build_snapshot` through
    `repo_scoped`, so the field-shape scan reaches it, and that scan refuses EVERY subscript (`adjacent`).
    """
    words = iter(argv)
    if next(words, None) != "gh" or next(words, None) != "api":
        return None
    for word in words:
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

    **A GUARD THAT ACCEPTS A STRING WHERE IT MEANS A POSITION CAN BE FED THE STRING** — the same sentence as
    "an exemption by name is a guard asking to be spoofed", one file over. This guard has now been that guard
    TWICE, in the same round:

      * it asked whether the repo's name appeared ANYWHERE in the argv, so `gh pr view 35 --template
        repos/o/r/x` SATISFIED it — the repository was named, in a flag that scopes NOTHING, and the command
        still resolved against whatever checkout the process was standing in;
      * that was fixed FOR `gh pr view` ONLY. The `gh api` branch went on testing ANY argument, so
        `gh api repos/wrong/repo/commits/<sha>/check-runs --template repos/o/r/x` SATISFIED it and would have
        queried **wrong/repo** — the same spoof, in the very guard that had just been "fixed" for the other
        half of itself. The lesson is not "check the template flag"; it is that BOTH halves must name a
        POSITION: the endpoint `gh` will actually request (`api_endpoint`), and the word right after `--repo`
        (`adjacent`). A repo-shaped string anywhere else — `--jq`, `-f`, `-F`, a header, a field value —
        scopes nothing and satisfies nothing.

    Anything else is a command that will resolve against THE CURRENT CHECKOUT, which is not a repository the
    caller ever named. That is not a fetch about this PR; it is a fetch about wherever this process happens
    to be running, and the answer it brings back is about the wrong repository (or, as the rollup's old argv
    did, about no PR at all). Refused — the caller asked about `repo`, and a fetch that cannot ask about
    `repo` derives nothing.
    """
    is_api = next(iter(adjacent(argv)), None) == ("gh", "api")
    # MUTATE:api-endpoint-is-a-position:endpoint = next((a for a in argv if is_endpoint(repo, a)), None)
    endpoint = api_endpoint(argv)  # the POSITION `gh` will request — None unless this argv IS `gh api …`
    if is_api:
        scoped = endpoint is not None and is_endpoint(repo, endpoint)
    else:
        scoped = any(flag == "--repo" and name == repo for flag, name in adjacent(argv))
    # MUTATE:every-fetch-is-repo-scoped:return argv
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


def check_repo_scoping(repo: str = "o/r") -> str:
    """EVERY GitHub call THE CODE ACTUALLY MAKES, asserted repo-scoped — the fetcher list DERIVED FROM THE
    CODE (`code_argv` records the argv of every fetch `build_snapshot` issues), never written down here.

    A hand-written list of fetchers is a list that is complete on the day it is written and wrong on the day
    the next fetcher is added — which is the day it needed to be right. This one covers a new fetcher the
    moment it exists, and it asserts on the ARGV, not on the verdict: a test that reads only the RESULT
    cannot tell "the right repository" from "the repository I happened to be standing in", which is exactly
    how a `--repo` the rollup ignored survived a whole suite of green fixtures.
    """
    argv = code_argv()
    for source, one in argv.items():
        require_repo_scoped(source, repo, one)
    return f"every GitHub call names the repo {repo}: {', '.join(sorted(argv))}"


def gh_fetch(source: str, argv: list[str]) -> object:
    """Run `gh` and parse its stdout as JSON.

    A NON-ZERO EXIT IS A FAILED FETCH, FULL STOP. The doc's shell version needs `set -o pipefail` to learn
    this, because a dead `gh` piped into `jq` yields an EMPTY stdin, and `jq` then prints nothing and exits
    0 — the fetch failed and the shell called it success. There is no pipeline here, and the exit status is
    checked directly, which is the same rule with nothing left to forget.

    **THIS FUNCTION IS THE ONE SEAM THE FIXTURES REPLACE, WHICH IS WHY BOTH ITS RULES WERE PINNED BY
    NOTHING.** Every fixture drives the producer through `fixture_fetch`, so nothing in the suite ever
    executed these two lines: delete either and the whole matrix stayed green — the audit that found the
    completeness call found this too. They are driven now, by `seam_cases`, against a LOCAL PROCESS (no
    network): a command that prints valid JSON and exits 1, and one that prints garbage and exits 0. A rule
    on the only code path that talks to GitHub is the last rule that may go untested.
    """
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
    # MUTATE:gh-exit-is-checked:pass
    if proc.returncode != 0:
        raise FetchError(f"{source}: `{' '.join(argv[:3])} …` exited {proc.returncode}: {proc.stderr.strip()}")
    # MUTATE:gh-stdout-is-json:return json.loads(proc.stdout)
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
# **THE SIXTH FALSE GREEN, AND IT WAS `x or []` — THE IDIOM THAT ERASES THE DIFFERENCE BETWEEN "GITHUB SAID
# THERE ARE NONE" AND "GITHUB SAID NOTHING AT ALL".** `read_pages` — the seam added ONE ROUND AGO precisely
# to make a paginated read safe — flattened its rows with `page.get(rows_key) or []`. Delete the `statuses`
# member from an otherwise-green response, leave `total_count: 0`, and every rule in this file agrees: the
# count matches the rows collected (zero of them), the pages agree, containment holds — **GREEN**. The tool
# ALREADY KNEW this rule (`total_count` absent is refused; a MISSING `statusCheckRollup` is refused) and the
# brand-new seam reintroduced it anyway, because a field read that DECLARES NOTHING cannot refuse ANYTHING.
#
# So the class is closed the way `repo_scoped()` closed its own: **not by fixing the one line, but by making
# the mistake unreachable.** Every field read off a GitHub response goes through `field()`, and `field()`
# takes no default — it takes a DECLARED SHAPE, and refuses anything else. A key that may be absent, or null,
# says so (`ABSENT`, `NULL`); a key that may not, cannot become one by accident. And `check_field_shapes()`
# AST-scans this file: a fetcher that reads a field any other way — a raw `.get()`, a raw subscript, or a
# `field()` call with no shape at all — FAILS THE SUITE BY NAME. Forgetting is not made unlikely; it is made
# impossible to commit quietly.
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
    as what it is (see `rollup-entries-present`, whose fixture pins that sentence).
    """
    problem = shape_problem(source, obj, key, shape)
    # MUTATE:field-shape-declared:problem = None
    if problem:
        raise FetchError(f"{problem}{' ' + why if why else ''}")
    return obj.get(key) if isinstance(obj, dict) else None


# --- THE SCAN THAT PROVES IT, AND THE THREE HOLES IT SHIPPED WITH ----------------------------------
#
# **A CURE THAT IS AN INSTANCE OF THE DISEASE IT WAS WRITTEN TO CURE — THREE TIMES NOW, ON THIS BRANCH.**
# The scan below was added to make "a field read that declares nothing" IMPOSSIBLE. Reviewers drove three
# different bypasses straight through it, and every one left the suite GREEN:
#
#   * IT ONLY SAW ONE SYNTACTIC FORM OF A SUBSCRIPT. It flagged `page["statuses"]` — a STRING LITERAL key —
#     and was BLIND to every other spelling of the same read. `pages[0][rows_key]` inside `read_pages`: the
#     suite stayed GREEN. The key was a variable, so the guard never looked.
#   * AND ITS EXEMPTION WAS BY NAME. One dict a reader may read raw (the seam's own `facts`) was exempted by
#     the NAME of the local holding it — so `facts = {}` followed by `facts["statuses"]`, ANYWHERE in a
#     scanned reader, was exempt. The suite stayed GREEN for that too. **AN EXEMPTION BY NAME IS A GUARD
#     ASKING TO BE SPOOFED**: the thing it trusts is the one thing the forgetful edit can freely choose.
#   * **AND ITS REGION WAS THE FETCHERS, WHICH IS NOT WHERE THE RESPONSE ENDS UP.** This is the one that
#     matters most, and it is the one that survived two rounds of "fixes". The scan covered the functions
#     HANDED the seam — but a fetcher can HAND ITS RESPONSE BACK, and the function it hands it back to
#     (`build_snapshot`) is the one function no reader set can ever contain, BECAUSE it is where the seam is
#     handed out. The hand-back rule that was supposed to close that only knew a BARE return, so a fetcher
#     that returned `{"raw": data}`, or `identity(data)`, or `[data]`, walked past it — and `build_snapshot`
#     then subscripted a raw GitHub response with no rule anywhere to say a word.
#
# THE FIRST TWO ARE FIXED BY DELETING THE EXEMPTION AND BY BEING TOTAL OVER THE FORMS OF A READ: **EVERY
# subscript is refused — literal key, variable key, computed key, on any object whatsoever — and so is every
# dict-reading method call.** The seam-owned dict that needed the exemption is GONE (`read_pages` returns a
# typed `Facts`, read by ATTRIBUTE), the one list index a fetcher needed is `next(iter(…))`, and the scanned
# region needs NO exemption at all. **ZERO is the only number of exemptions that cannot be spoofed.**
#
# **THE THIRD IS FIXED BY ASSERTING THE PROPERTY INSTEAD OF ENUMERATING THE SHAPES — read this before you
# "improve" the hand-back rule.** The guard MEANS *"no raw GitHub response can be READ outside a scanned
# reader"*. Chasing the shapes a response can be smuggled OUT in (a dict, a list, a helper, a closure, a
# comprehension, a default argument) is a race that always has one more move, and each round of it is a
# guard that asserts one spelling of what it means. So the shapes are not enumerated. **The READS are
# removed:** every function that can HOLD a response — the whole PRODUCER, `build_snapshot` and `derive`
# included — is scanned, and in ALL of it a raw read is refused. A hand-back then has nowhere to be read,
# whatever shape it wore, and the cross-source facts a fetcher must pass out travel as TYPED values it
# BUILT (`RestRun`, `RollupStatus`, `Snapshot`) which have no fields to read.
#
# AND THE SUBJECT OF THE SCAN IS DERIVED, NOT LISTED. It used to scan a hand-written tuple of function
# names, so a NEW fetcher nobody added to it was not scanned AT ALL — the hole was disclosed, which is not
# the same as being closed. Both sets are CALL-GRAPH CLOSURES of the running code now: the READERS from the
# functions `build_snapshot` hands the seam to (`scanned_readers`), and the REGION IN WHICH NO RAW READ IS
# ALLOWED from `derive` and `build_snapshot` themselves (`producer`). A fetcher added tomorrow is scanned
# the day it is added, and so is any helper it — or the orchestrator — happens to call.

# THE DOOR. The one function that may touch a response raw — and the ONLY name the scan does not scan.
#
# **THIS IS NOT THE `facts` EXEMPTION UNDER A NEW NAME, and the difference is the whole lesson.** That one
# exempted a LOCAL VARIABLE's name, which any edit inside a reader could choose for itself — spoofable in
# one line. This exempts a FUNCTION DEFINITION: no edit inside a fetcher can make ITS read exempt, because
# the read would still be in the fetcher's body, which is scanned. The raw access has to physically live
# somewhere, and this is where — behind the shape declaration, which is the only reason it is safe.
#
# `shape_problem` is NOT named here and must never be: nothing but `field` calls it, so it never enters the
# closure. And the day a reader calls it directly, it DOES enter — and its raw reads FAIL THE SCAN. Read the
# response through the door, or do not read it.
DOOR = "field"

# The parameter every reader takes the response through. It is what makes "a reader" a question the CODE can
# answer (`scanned_readers`) instead of a list somebody has to remember to update.
SEAM = "fetch"

# **A RAW READ IS NOT ONLY `x[k]` AND `x.get(k)`.** `x.items()`, `x.values()`, `x.pop(k)` reach the same
# value and declare exactly the same nothing. A guard that knows ONE spelling of what it forbids is the
# guard that gets bypassed by moving the read one character sideways — which is precisely what happened
# here. So the CLASS is refused, in every spelling a reader would plausibly reach for.
RAW_READS = ("get", "items", "keys", "values", "pop", "popitem", "setdefault", "__getitem__")


def top_level(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    """The module's TOP-LEVEL functions — the only names a call is resolved against.

    A nested `def fetch(...)` (the fixtures have one) must never be mistaken for the seam, and a nested def
    inside a scanned function is scanned anyway, as part of that function's body.
    """
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def called_by(fn: ast.FunctionDef, funcs: dict[str, ast.FunctionDef], seam_only: bool = False) -> list[str]:
    """The top-level functions THIS function calls — or, with `seam_only`, just the ones it hands the SEAM."""
    return [call.func.id for call in ast.walk(fn)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in funcs
            and (not seam_only
                 or any(isinstance(arg, ast.Name) and arg.id == SEAM for arg in call.args))]


def closure(roots: list[str], funcs: dict[str, ast.FunctionDef]) -> dict[str, ast.FunctionDef]:
    """`roots` and everything they transitively call, MINUS the DOOR (and so minus whatever only it calls)."""
    out: dict[str, ast.FunctionDef] = {}
    queue = list(roots)
    while queue:
        name = queue.pop()
        if name in out or name == DOOR or name not in funcs:
            continue
        out[name] = funcs[name]
        queue += called_by(funcs[name], funcs)
    return out


def scanned_readers(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    """WHICH FUNCTIONS ARE HANDED A GITHUB RESPONSE — DERIVED FROM THE CODE, never a list somebody maintains.

    The ROOTS are the functions `build_snapshot` hands the `fetch` seam to (that is how a response enters
    this tool at all), and the reader set is their TRANSITIVE CALLEES. So a fetcher added to
    `build_snapshot` tomorrow is scanned the moment it exists — the limit the old hand-written tuple had
    (`a NEW fetcher nobody added is not scanned`) is not disclosed, it is gone.

    **THIS SET IS NO LONGER WHAT BOUNDS THE RAW-READ SCAN — `producer()` IS, AND THE DIFFERENCE IS THE WHOLE
    OF THE LAST DEFECT.** What this set is still for is the HAND-BACK rule, whose subject is a FETCHER. The
    reason it cannot bound the read scan is that a response does not stay where it was fetched: hand it back
    inside a dict, or through any helper, and it is somewhere this set never reached.
    """
    funcs = top_level(tree)
    root = funcs.get("build_snapshot")
    if root is None:
        raise FetchError(
            "the field-shape scan cannot find `build_snapshot` — it is where the fetch seam is handed out, "
            "and therefore where the set of readers is DERIVED from. A scan that cannot find its subject "
            "must never report health it did not measure."
        )
    fetchers = called_by(root, funcs, seam_only=True)
    # MUTATE:field-scan-has-a-subject:pass
    if not fetchers:
        raise FetchError(
            f"the field-shape scan found NO FETCHER — nothing in `build_snapshot` is handed the `{SEAM}` "
            f"seam, so the reader set is EMPTY and this scan has NOTHING TO SCAN. It would then pass every "
            f"source on Earth, including one that reads every field raw. A check with no subject FAILS."
        )
    return closure(fetchers, funcs)


# The PRODUCER's entry points. `derive` is FETCH -> PROMOTE -> VERIFY -> DECIDE, `build_snapshot` is where
# the seam is handed out, and `resolve_repo` is the one fetch that happens OUTSIDE `derive` (the CLI asking
# which repository it is standing in). Between them they reach EVERY function that can hold a response or
# anything derived from one. (`build_snapshot` is named as well as `derive` because the scan is driven
# against INVENTED sources that have no `derive` — and because the anchor of the reader set must be an anchor
# here too, or a source could hand the seam out from a function this closure never reaches.)
PRODUCER_ROOTS = ("derive", "build_snapshot", "resolve_repo")

# **AND THE CLI ITSELF, WHOSE BODY IS SCANNED THOUGH ITS CALLEES ARE NOT.** `main` holds the one thing the
# producer hands out — the result dict — and "no UNSCANNED code reads a field off ANYTHING" has to mean
# `main` too, or the boundary is just an exemption with a nicer name. So its body is scanned (it reads the
# verdict through the DOOR, like everything else), and the closure does NOT descend from it: what it calls
# is either the PRODUCER, which is already scanned, or the HARNESS (`self-test`, `doc-check`), which never
# runs on a live derive and reads RECORDED fixtures off disk rather than a live response.
SCANNED_BOUNDARY = ("main",)


def producer(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    """EVERY FUNCTION THAT CAN HOLD A GITHUB RESPONSE — the call-graph closure of `derive` and
    `build_snapshot`, which is the region where NO raw read is permitted AT ALL.

    **THIS IS THE FIX FOR THE GUARD THAT KEPT ASSERTING THE WRONG THING.** The scan MEANT *"no raw GitHub
    response can be read outside a scanned reader"* and ASSERTED *"no fetcher directly returns the seam call
    or a name bound to it"* — so a fetcher that returned `{"raw": data}`, or `identity(data)`, or `[data]`,
    handed the response straight to `build_snapshot`, which the reader set CANNOT contain, and the read
    there was scanned by nobody. Enumerating the shapes a response can be smuggled in is a game with no last
    move: the next one is a closure, a comprehension, a default argument.

    So the shapes are not enumerated. **The READS are removed.** Everything the response could reach is
    scanned, and in all of it a raw read is refused — so a laundered hand-back is HARMLESS, because there is
    nowhere left to read it. `build_snapshot` orchestrates and reads nothing; `derive`, `promote` and
    `result` handle only values this file BUILT (`Snapshot`, `RestRun`, the artifact rows) and read them by
    attribute or not at all.

    The honest limit, stated rather than implied — and it is the same one the whole scan has: it sees the
    code as WRITTEN. A call this file makes into ANOTHER module (`SNAP.evaluate`, `json.dumps`) is not
    followed, and a deliberately obfuscated read (`getattr(page, "g" + "et")`) is not caught. What is made
    impossible is committing the mistake QUIETLY — which is how every one of these shipped.
    """
    funcs = top_level(tree)
    scanned = closure([name for name in PRODUCER_ROOTS if name in funcs], funcs)
    scanned.update({name: funcs[name] for name in SCANNED_BOUNDARY if name in funcs})
    return scanned


def executable(fn: ast.FunctionDef) -> list[ast.AST]:
    """Every node in a function's EXECUTABLE code. **AN ANNOTATION IS NOT CODE.**

    `list[str]` and `tuple[list[dict], Facts]` are `ast.Subscript` nodes, and a scan that refuses EVERY
    subscript would refuse the TYPE HINTS — condemning correct text, which is how a guard gets deleted by
    the next person in a hurry. They read nothing, they run nothing (`from __future__ import annotations`
    leaves them strings), and they are excluded HERE, once, by IDENTITY — never by pattern-matching what an
    annotation "looks like", which would be one more thing a forgetful read could dress up as.
    """
    skip: set[int] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.FunctionDef):
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
                if arg is not None and arg.annotation is not None:
                    skip.update(id(n) for n in ast.walk(arg.annotation))
            if node.returns is not None:
                skip.update(id(n) for n in ast.walk(node.returns))
        elif isinstance(node, ast.AnnAssign):
            skip.update(id(n) for n in ast.walk(node.annotation))
    return [node for node in ast.walk(fn) if id(node) not in skip]


def own_body(fn: ast.FunctionDef) -> list[ast.AST]:
    """Every node in THIS function's own body — NOT descending into a nested def or lambda.

    The hand-back rule below asks what a FETCHER returns TO ITS CALLER, and a nested function returns to the
    fetcher, not past it. The distinction is not cosmetic: `repo_scoped`'s inner `scoped()` returns the seam's
    value BECAUSE IT IS THE SEAM — the wrapper every fetch passes through — and what it hands back goes to a
    FETCHER, which is scanned. Walking into it would condemn the seam for being the seam.

    (Raw READS inside a nested def are still refused: `executable`/`check_field_shapes` walk the whole tree.
    This narrower view is used by the hand-back rule alone.)
    """
    out: list[ast.AST] = []
    stack: list[ast.AST] = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        out.append(node)
        stack += list(ast.iter_child_nodes(node))
    return out


def handed_back(fn: ast.FunctionDef) -> list[ast.expr]:
    """Every value a function RETURNS to its caller — a returned TUPLE unpacked, because that is how every
    fetcher here hands back more than one thing, and a raw response could ride in any slot of it."""
    out: list[ast.expr] = []
    for node in own_body(fn):
        if isinstance(node, ast.Return) and node.value is not None:
            out += node.value.elts if isinstance(node.value, ast.Tuple) else [node.value]
    return out


def seam_bound(fn: ast.FunctionDef) -> set[str]:
    """The locals bound DIRECTLY to what the seam returned: `pages = fetch(source, argv)`."""
    return {t.id for node in own_body(fn)
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name) and node.value.func.id == SEAM
            for t in node.targets if isinstance(t, ast.Name)}


def is_raw_response(value: ast.expr, bound: set[str]) -> bool:
    """Is this expression the SEAM'S OWN VALUE — the response, unread? The call itself, or a name bound to it."""
    return ((isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == SEAM)
            or (isinstance(value, ast.Name) and value.id in bound))


def check_field_shapes(source: str | None = None) -> str:
    """NOTHING OUTSIDE THE DOOR READS A FIELD AT ALL — asserted over the AST of the code that RUNS.

    This is the half `field()` cannot do. A door only helps if there is no way around it, and the way around
    it is one character long: `page.get(rows_key)`. So the source is SCANNED, and any function in the
    PRODUCER that reads a field another way is NAMED and the suite goes RED:

      * a raw `.get(…)` — declares no shape, and turns MISSING into None (then `or []` turns it into empty);
      * ANY raw subscript, and the word ANY is the fix: `page["statuses"]`, `pages[0][rows_key]`,
        `facts[k]` — a literal key, a variable key, a computed key, on ANY object. The version that read
        only STRING-LITERAL keys was bypassed by a variable one, and the version that exempted an object by
        the NAME of the local holding it was bypassed by spoofing that name. There is no exempt key and
        there is no exempt object;
      * any other dict-reading method (`RAW_READS`) — the same read, one synonym along;
      * a `field()` call with NO SHAPE — the read went through the door and still said nothing;
      * and `build_snapshot` CALLING THE SEAM ITSELF — a fetch belongs in a fetcher.

    **AND THE SUBJECT IS THE PRODUCER, NOT THE READER SET — THAT IS THE DEFECT THIS ROUND FIXES.** Scanning
    only the readers asserted *"no FETCHER reads a field raw"*, which is not what the guard means and never
    was: `build_snapshot` is where the seam is handed out, so it can never be IN the reader set, and a
    fetcher had only to hand its response back — wrapped in a dict, laundered through a helper — to have it
    read there, unscanned. Refusing every raw read in EVERY function that can hold a response is what makes
    the hand-back HARMLESS: there is nowhere left to read it (see `producer`).

    The honest limit, stated rather than implied: a static scan sees the code as WRITTEN. It catches every
    ordinary spelling of a forgetful read — which is what a forgetful read IS — and it is not a sandbox
    against a deliberately obfuscated one (`getattr(page, "g" + "et")`). What it makes impossible is
    committing the mistake QUIETLY, which is how all six false greens in this file shipped.
    """
    tree = ast.parse(source if source is not None else Path(__file__).read_text(encoding="utf-8"))
    readers = scanned_readers(tree)
    # THE REGION IN WHICH A RAW READ IS REFUSED. The weakening is the SHIPPED BUG: scan the READERS ONLY, and
    # every function a response can be handed BACK to — `build_snapshot` first among them — is free to read it
    # with nothing watching. That is the hole the wrapped and the helper hand-backs went through.
    # MUTATE:no-raw-read-outside-the-door:scanned = readers
    scanned = producer(tree)
    bad: list[tuple[int, str]] = []
    for name, node in scanned.items():
        for sub in executable(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr in RAW_READS:
                bad.append((sub.lineno, f"{name}:{sub.lineno} reads `{ast.unparse(sub)}` with a raw "
                                        f"`.{sub.func.attr}(…)` — it DECLARES NO SHAPE, so an ABSENT value "
                                        f"becomes None, and `or []` then makes it EMPTY"))
            elif isinstance(sub, ast.Subscript):
                bad.append((sub.lineno, f"{name}:{sub.lineno} reads `{ast.unparse(sub)}` with a raw "
                                        f"SUBSCRIPT — it DECLARES NO SHAPE, and a KeyError is a crash, not a "
                                        f"refusal. EVERY subscript is refused here: a literal key, a "
                                        f"variable key, a computed key, on ANY object. An exemption is a "
                                        f"guard asking to be spoofed."))
            elif (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == DOOR
                  and len(sub.args) < 4):
                bad.append((sub.lineno, f"{name}:{sub.lineno} calls {DOOR}() with NO SHAPE — it went through "
                                        f"the door and still did not say what it expects"))
        # AND NO FETCHER HANDS THE SEAM'S OWN VALUE BACK UNREAD — a NARROW rule, kept, and it is NOT what makes
        # the hand-back safe. Say that plainly, because believing otherwise is what shipped the last defect:
        # this catches the response returned BARE (`return fetch("x", [])`) or under a name bound to it, and it
        # catches NOTHING ELSE — not `{"raw": data}`, not `identity(data)`, not `[data]`. It was sold as the
        # closure of "no raw response is read outside a scanned reader" and it never was, and NO enumeration of
        # container shapes ever could be: the next spelling is always one move away.
        #
        # **WHAT MAKES A HAND-BACK HARMLESS IS THAT THERE IS NOWHERE LEFT TO READ IT** — the raw-read refusal
        # above, over the WHOLE producer (`producer`). This rule survives because it names the mistake early,
        # in the fetcher that made it, with the message that says what to do. NEVER grow it into a taint
        # analysis; if you find yourself adding a shape to it, the read scan is what you actually want.
        bound = seam_bound(node)
        for value in handed_back(node):
            # MUTATE:fetchers-never-return-a-response:continue
            if is_raw_response(value, bound):
                bad.append((value.lineno, f"{name}:{value.lineno} HANDS THE RAW RESPONSE BACK to its caller "
                                          f"— unread and undeclared. Read it HERE, through {DOOR}(), or do "
                                          f"not return it."))
    # AND `build_snapshot` NEVER CALLS THE SEAM. It is scanned like everything else now, so a response read
    # there would be refused anyway — but a fetch inlined into the orchestrator is its own mistake (it puts a
    # response in a function whose job is to hold none), and it gets its own name. It hands the seam to a
    # fetcher; it never calls it. (`scanned_readers` has already refused a source with no `build_snapshot`,
    # so this `next` cannot fail.)
    root = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_snapshot")
    for call in executable(root):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == SEAM:
            bad.append((call.lineno, f"build_snapshot:{call.lineno} calls the `{SEAM}` seam DIRECTLY — the "
                                     f"orchestrator holds no response and reads none. A fetch belongs in a "
                                     f"FETCHER."))
    # MUTATE:field-reads-through-the-seam:pass
    if bad:
        raise FetchError(
            "A FIELD READ THAT FORGETS ITS SHAPE: "
            + "; ".join(msg for _, msg in sorted(bad)) + ". Every field of a GitHub response "
            "is read through `field(source, obj, key, <shape>)`, which REFUSES anything the caller did not "
            "declare. A read that declares nothing cannot refuse anything — and the last one to ship "
            "(`page.get(rows_key) or []`) turned a response that was MISSING its row array into a page with "
            "NO rows, and returned GREEN."
        )
    return (f"every field read declares its shape and goes through {DOOR}(), and NOTHING outside it reads a "
            f"field at all — in every function the code DERIVES from {' + '.join(PRODUCER_ROOTS)}, plus the "
            f"CLI boundary ({', '.join(SCANNED_BOUNDARY)}): {', '.join(sorted(scanned))}")


# --- A PAGINATED RESPONSE IS A SOURCE THAT CAN CONTRADICT *ITSELF*, AND IT DID -----------------------
#
# **THE FIFTH FALSE GREEN IN THIS FILE, AND THE FIFTH TIME IT WAS THE SAME SHAPE: TWO SOURCES THAT DISAGREE
# ABOUT A FACT, AND THE TOOL TRUSTS ONE OF THEM.** The other four were across sources — REST vs the rollup,
# a family never read, a head that moved. This one is INSIDE one response: `--slurp` hands us an ARRAY OF
# PAGES, GitHub repeats the per-commit facts on EVERY page, and the tool read them off `pages[0]` and never
# looked at the rest. A reviewer put `total_count: 2` on page one (matching the rows collected) and
# `total_count: 7` on page two, and `derive()` returned **GREEN** — with page two's own metadata saying five
# rows GitHub holds were not in our hands. It did it again with a page-two `total_count` that was ABSENT, and
# again with one that was a STRING. Both REST families. The tool that reconciles REST against the rollup was
# not reconciling a response against ITSELF.
#
# AND `total_count` WAS NOT THE ONLY FACT REPEATED ON EVERY PAGE. The status response carries the COMMIT
# (`.sha`) at the top level of every page, and `fetch_statuses` took page one's and STAMPED IT ON EVERY ROW.
# Put the SUPERSEDED sha on page two and its rows — rows GitHub says belong to ANOTHER COMMIT — went into the
# artifact wearing the head sha we asked for. That is not a short read; it is FABRICATED EVIDENCE, and it is
# the `status-sha-from-response` rule (and `superseded-status-response.json` with it) defeated by moving the
# bad value one page along.
#
# **SO THE RECONCILIATION MOVES TO THE SEAM, WHERE IT CANNOT BE FORGOTTEN.** `read_pages()` is now the ONLY
# way rows enter this tool from a paginated read — the same move `repo_scoped()` made for the repository. A
# fetcher does not get to parse pages itself, so it cannot forget to check them; it declares WHICH per-commit
# facts its response repeats (`CHECKRUN_PAGE_FACTS` / `STATUS_PAGE_FACTS`) and the seam proves, for every
# one of them, that EVERY page
# states it, readably, and that all the pages SAY THE SAME THING. A new paginated fetcher gets the rule by
# existing.
#
# `total_count` is the count for the WHOLE COMMIT, not for the page — every page repeats it (verified live:
# a commit with 27 check runs read at `per_page=5` returns six pages, each saying `total_count=27`), which is
# what makes both halves meaningful: the pages must agree, and the agreed value must equal the rows collected
# across ALL pages, which is exactly what `--slurp` gives us.

# The per-commit facts each REST family's response repeats on EVERY page: (the fact, the shape it must have).
# **A FACT LISTED HERE IS RECONCILED; A FACT NOT LISTED IS NOT READ AT ALL.** The status response's top-level
# `.state` is deliberately absent: this tool never reads it (a commit with zero statuses reports
# `{"state":"pending"}` — an absence read as a verdict), and reconciling a value nothing consumes would be a
# rule that can only ever wedge an honest PR. Add a field here the moment anything starts READING it.
#
# EACH FAMILY HANDS ITS OWN SPEC TO `read_pages`, rather than the seam looking one up by source name: the
# lookup would be a raw subscript inside a scanned reader, and there is no longer any such thing (see
# `check_field_shapes`). Declaring it AT THE CALL is the same rule with nothing to key on.
CHECKRUN_PAGE_FACTS = (("total_count", int),)
STATUS_PAGE_FACTS = (("total_count", int), ("sha", str))


class Facts(NamedTuple):
    """The per-commit facts EVERY page of one response agreed on — and the reason the `facts` EXEMPTION in
    the field-shape scan is GONE rather than narrowed.

    It used to be a plain dict, so `read_pages` and `fetch_statuses` read it with a raw subscript
    (`facts["sha"]`) — and the scan had to EXEMPT it. The exemption was BY NAME, so any local a reader chose
    to call `facts` was exempt too, and a reviewer read a GitHub response through it with the suite still
    green. A field a reader may read raw is a hole in the door, whoever owns the dict.

    So the seam does not hand back a dict at all. These are ATTRIBUTES — `facts.total_count`, `facts.sha` —
    which are not field reads, cannot be a raw subscript, and need no exemption to be legal. `sha` is None
    for the check-run family, whose response carries the commit on the ROWS and not at the top level.
    """
    total_count: int
    sha: object = None


# --- WHAT A FETCHER HANDS TO `build_snapshot`: TYPED VALUES IT BUILT, NEVER A RESPONSE ------------
#
# **`build_snapshot` ORCHESTRATES; IT DOES NOT READ RESPONSES — AND UNTIL NOW IT READ THE FETCHERS' ROWS
# WITH RAW SUBSCRIPTS ANYWAY** (`r["context"]`, `w["state"]`, `rest_runs.setdefault(…)`). Those rows were
# dicts THIS FILE built, so each read was individually harmless — and that is exactly what made the function
# a place where a read could live. A RESPONSE laundered back to it (wrapped in a container, or passed
# through any helper) would have been read by the very same idiom, in the one function the reader set
# cannot contain, with nothing to say a word.
#
# So the cross-source facts leave a fetcher as TYPED VALUES — attributes, which are not field reads and
# cannot be a raw subscript (the same move `Facts` made for the page seam). `build_snapshot` now performs NO
# raw read of ANY kind, and neither does anything else in the producer, which is what lets the scan assert
# the thing it always MEANT: **there is nowhere outside the door left to read a response.**
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


def readable(source: str, pages: list[dict], fact: str, kind: type) -> list[object]:
    """EVERY page STATES the fact, in the shape it must have — or NO page's value is read at all.

    A PAGE THAT DOES NOT STATE IT IS NOT A PAGE THAT AGREES WITH US. An absent value is not a benign value —
    this file already knows that about `headRefOid` and about a rollup `state`, and the knowledge simply never
    reached page two. Skipping the pages that "have nothing to say" is how a truncated read reports a complete
    one: the page that would have told you rows were missing is exactly the page you skipped.

    EVERY page is asked THROUGH THE DOOR (`field`), and every refusal is collected before any is raised — so
    the message can NAME the pages instead of dying on the first of them. It used to ask `shape_problem`
    directly, which is the door's own helper: a reader that reaches past `field()` into the door's internals
    is a reader the scan then has to scan, and its raw reads would (correctly) fail. One door, one caller.
    """
    values, problems = [], []
    for i, page in enumerate(pages):
        try:
            values.append(field(source, page, fact, kind))
        except FetchError as exc:
            problems.append((i, str(exc)))
    if problems:
        raise FetchError(
            f"{source}: page(s) " + ", ".join(str(i + 1) for i, _ in problems)
            + f" of {len(pages)} carry no readable {fact} ({'; '.join(p for _, p in problems)}) — GitHub "
            f"repeats it on EVERY page, so a page that does not state it is a page we cannot read. An absent "
            f"count is NOT a count of zero and an absent sha is NOT this commit: a fail-closed rule that "
            f"cannot fire is not a rule, and the page we waved through is the one that would have told us."
        )
    return values


def agreed(source: str, pages: list[dict], fact: str, kind: type) -> object:
    """THE ONE PLACE A FACT REPEATED ACROSS PAGES IS RECONCILED. Every page must STATE it, readably, and every
    page must state THE SAME ONE.

    TWO PAGES THAT DISAGREE ARE TWO SOURCES THAT DISAGREE. It does not matter that they arrived in one
    response — one of them is wrong, we cannot tell which, and evidence we cannot reconcile is not evidence.
    The conflict is NOT resolved by taking the first page (which is what the bug did), nor the last, nor the
    largest: the same rule, and the same reason, as `agree_or_refuse`.
    """
    # The weakening is the SHIPPED BUG, verbatim: read the fact RAW off every page, then quietly DROP the
    # pages that did not state it readably. The pages that remain then agree with each other — vacuously —
    # and the page that would have said rows were missing is the one that was thrown away.
    # MUTATE:page-fact-known:stated = [v for v in (page.get(fact) for page in pages) if isinstance(v, kind) and not isinstance(v, bool)] or [page.get(fact) for page in pages]
    stated = readable(source, pages, fact, kind)
    # MUTATE:pages-agree:stated = stated[:1]
    if len(set(stated)) != 1:
        raise FetchError(
            f"{source}: THE PAGES OF ONE RESPONSE DISAGREE ABOUT {fact} — {', '.join(f'page {i + 1} says {v!r}' for i, v in enumerate(stated))}. "
            f"GitHub repeats this fact on every page, so the pages cannot both be right, and a response that "
            f"contradicts ITSELF is not evidence about this PR. It is NOT resolved by believing page one: "
            f"that is precisely the bug — page one said the read was complete while page two said rows were "
            f"missing, and the missing row could be the FAILING one."
        )
    # `next(iter(…))`, never `stated[0]` — EVERY subscript in a scanned reader is refused, and this is one of
    # the two places that cost (the other is `fetch_check_runs`'s marker row). The pages have just been proven
    # to say ONE thing; which of them we hand back is arbitrary, and it is not a field read at all.
    return next(iter(stated))


def read_pages(fetch: Fetch, source: str, argv: list[str], rows_key: str,
               page_facts: tuple) -> tuple[list[dict], Facts]:
    """**EVERY PAGINATED READ IN THIS TOOL ENTERS HERE, AND THERE IS NO OTHER DOOR.**

    A fetcher gets its rows from this function or it gets no rows at all — which is what makes the page rules
    impossible to forget rather than merely easy to remember. It returns the rows collected across ALL pages,
    and the per-commit facts every page agreed on, as a typed `Facts` (the caller declares WHICH — see
    `CHECKRUN_PAGE_FACTS` / `STATUS_PAGE_FACTS`).

    THE COMPLETENESS TEST LIVES HERE TOO, and it REPLACED A NOTE. The tool used to record `total_count=3 but
    collected 2` in a `notes` list and return GREEN anyway — a verdict computed from evidence it had just
    finished proving incomplete, with the proof printed politely underneath. Disclosure is not a substitute
    for refusal. **The only honest thing to do with evidence you KNOW is missing a row is to REFUSE TO DERIVE
    A VERDICT FROM IT.**
    """
    pages = fetch(source, argv)
    # A `--slurp` that did not yield an ARRAY is a response we cannot read — and the row loop below would
    # then iterate an object's KEYS and blow up on the first `.get`, which is a CRASH where a verdict was
    # owed. An EMPTY array is refused for the same reason a missing `total_count` is: `--paginate --slurp`
    # over a real commit returns at least one page, so zero pages is a response we do not understand, and it
    # would leave every page rule below quantifying over the empty set — vacuously true, which is the shape
    # of every defect in this file.
    # MUTATE:pages-are-an-array:pass
    if not isinstance(pages, list) or not pages:
        raise FetchError(
            f"{source}: expected a NON-EMPTY array of pages from --slurp, got "
            f"{type(pages).__name__}{'(empty)' if isinstance(pages, list) else ''} — a response we cannot "
            f"read is not a response with nothing in it."
        )
    # AND EVERY PAGE IS AN OBJECT. A page that is not one used to reach `.get` and raise AttributeError — a
    # CRASH, which is not a refusal and not a verdict: the tool simply had no opinion where one was owed.
    # MUTATE:page-is-an-object:pass
    if not all(isinstance(page, dict) for page in pages):
        raise FetchError(
            f"{source}: page(s) "
            + ", ".join(str(i + 1) for i, p in enumerate(pages) if not isinstance(p, dict))
            + f" of {len(pages)} are not objects — GitHub returns one object per page, and a page we cannot "
            f"read carries facts we cannot check. It is refused, never skipped: skipping it is how a page "
            f"that says 'there are more rows' stops being read."
        )

    # **AND EVERY PAGE CARRIES ITS ROW ARRAY — AS AN ARRAY.** The weakening below is the code that SHIPPED,
    # and it is `x or []`: the idiom that erases the difference between "GitHub says this commit has no
    # statuses" (a FACT, and `total_count: 0` confirms it) and "the response did not contain a `statuses`
    # member at all" (a response we cannot read). Delete that member from an otherwise-green fixture and the
    # count still matched the rows collected — zero — the pages still agreed, containment still held, and
    # `derive()` returned **GREEN**. The tool refuses an absent `total_count` and an absent
    # `statusCheckRollup` for exactly this reason; the brand-new page seam defaulted the one thing left.
    # A page missing the key, or carrying anything but a LIST there, is REFUSED — never defaulted, never
    # coerced. `field()` is what makes that unforgettable rather than merely written down.
    # MUTATE:rows-are-a-list:rows = [row for page in pages for row in page.get(rows_key) or []]
    rows = [row for page in pages for row in field(source, page, rows_key, list, why=(
        f"Every page of this response carries its rows under {rows_key!r}; a page that does not is not a "
        f"page with NO rows, and the row it is hiding could be the FAILING one."))]
    # A TYPED VALUE, NOT A DICT — read by ATTRIBUTE downstream, so no reader has to subscript it and the scan
    # needs no exemption for it. The spec comes from the FETCHER (`page_facts`); a lookup keyed by `source`
    # would itself be a raw subscript in here.
    facts = Facts(**{fact: agreed(source, pages, fact, kind) for fact, kind in page_facts})

    # WHAT WE COLLECTED MUST BE WHAT GITHUB SAYS IT HOLDS — across every page, now that every page has been
    # made to say the same number. A short read is a hole we KNOW about, and a hole we know about is never
    # green. (This is not the marker's `count` rule, which asks a DIFFERENT question, downstream: "did every
    # row this fetch produced survive into the file?")
    # MUTATE:evidence-is-complete:pass
    if facts.total_count != len(rows):
        raise FetchError(
            f"{source}: GitHub reported total_count={facts.total_count} but the paginated read collected "
            f"{len(rows)} row(s) — EVIDENCE IS MISSING. A row GitHub holds for this commit is not in our "
            f"hands, and it could be the FAILING one. No verdict is derived from a read we KNOW is short. "
            f"(/check-runs is also capped at the 1000 most recent check suites; --paginate defeats page-size "
            f"truncation, and this count defeats a short read — neither proves completeness at that scale.)"
        )
    return rows, facts


def fetch_check_runs(fetch: Fetch, head_sha: str) -> tuple[list[dict], dict, list[RestRun]]:
    """(1) CHECK RUNS — pinned to <head_sha> BY THE URL. Identity AND verdict in one row.

    `--paginate` is MANDATORY (`/check-runs` pages at 30) and `--slurp` collects every page into ONE array,
    which is what lets the marker's `count` be the total ACROSS pages rather than the last page's.

    THE REPOSITORY IS `REPO_SLOT`, NOT AN ARGUMENT — see `repo_scoped`. No fetcher is trusted to remember it.

    AND THE PAGES ARE NOT PARSED HERE EITHER — see `read_pages`, the one door a paginated read comes through.
    The weakening on the call below is this family going back OUT of that door: the raw response, flattened,
    with page one believed and every other page unread. That is the code this file shipped, verbatim, and it
    is a false green on every page rule at once — which is why the marker sits on the CALL and not only on the
    rules it reaches. A body no family invokes is not a rule (`truncated-checkruns.json` learned that here).
    """
    # MUTATE:checkruns-through-the-seam:runs = [r for page in fetch("check-runs", ["gh", "api", "--paginate", "--slurp", f"repos/{REPO_SLOT}/commits/{head_sha}/check-runs"]) for r in (page or {}).get("check_runs", [])]
    runs, _facts = read_pages(fetch, "check-runs", [
        "gh", "api", "--paginate", "--slurp", f"repos/{REPO_SLOT}/commits/{head_sha}/check-runs",
    ], "check_runs", CHECKRUN_PAGE_FACTS)

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
        # MUTATE:evidence-sha-from-response:sha = head_sha
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
        # against the rollup, and it used to do so by SUBSCRIPTING the rows above — a raw read, in the one
        # function no scan covered. The bucket is computed HERE, where the response is already in hand and
        # already declared, and what leaves this fetcher is a value with no fields to read.
        seen.append(RestRun(id=run_id, bucket=checkrun_bucket(status, conclusion)))

    # The commit oid lives ONLY on the rows here, so a fetch that returned ZERO rows has NO oid to carry and
    # its marker's sha is `-`. Inventing one would be the fabrication the contract forbids outright.
    # (`next(iter(runs))`, never `runs[0]`: the field-shape scan refuses EVERY subscript in a reader, and
    # taking the first of a list is not a field read — it does not get to LOOK like one.)
    # MUTATE:checkruns-marker-sha:marker_sha = head_sha
    marker_sha = field("check-runs", next(iter(runs)), "head_sha", str) if runs else NO_OID
    marker = {"row": "source", "source": "check-runs", "sha": marker_sha, "count": str(len(rows))}

    # The FAMILY IS READ, and what it returned is what goes in the artifact. A family never read reports
    # NOTHING, and "nothing" parses as "nothing wrong" — the weakening below is that family going dark.
    # MUTATE:both-families-checkruns:return [], {"row": "source", "source": "check-runs", "sha": NO_OID, "count": "0"}, []
    return rows, marker, seen


def fetch_statuses(fetch: Fetch, head_sha: str) -> tuple[list[dict], dict, list[RestStatus]]:
    """(2) COMMIT STATUSES — the legacy family, which (1) CANNOT SEE.

    A failing Jenkins/CircleCI commit status is genuinely INVISIBLE to `/check-runs`. Read only one family
    and the other's failures are simply ABSENT from the evidence — and an absence parses as "nothing wrong".

    The response carries the commit ONCE PER PAGE, at the TOP LEVEL, and carries it EVEN WHEN `.statuses` IS
    EMPTY. That is what lets the marker PROVE a zero-status commit: `{"source":"status","sha":"<GitHub's>",
    "count":"0"}` says *we asked THIS COMMIT, and it has none* — a FACT, where an absent section says
    nothing at all.

    **AND IT IS THE COMMIT *EVERY* PAGE NAMES, WHICH IS WHY THE SHA COMES OUT OF `read_pages` AND NOT OUT OF
    PAGE ONE.** It used to be read off `pages[0]` and stamped onto every row — so a page whose own `.sha` said
    its rows belonged to a DIFFERENT commit had them relabelled with the head we asked for, and the artifact
    then carried evidence we had FABRICATED the provenance of. `superseded-status-response.json` catches that
    on page one and could never have caught it on page two; `agreed()` is what makes the pages say it once.

    **NEVER read this response's own `.state` as a verdict.** A commit carrying ZERO statuses reports
    `{"state":"pending","total_count":0}` — verified live against this repo on a commit whose checks had all
    passed. An absence read as a verdict is a lie in both directions, so `.state` is not read here at all
    (and so it is NOT in `STATUS_PAGE_FACTS`: reconciling a value nothing consumes could only wedge an
    honest PR).

    The weakening on the call below is this family going back OUT of the one door — page one believed, every
    other page unread, exactly as it shipped. A REVIEWER deleted this family's completeness call ALONE and
    both the self-test AND the mutation matrix stayed GREEN, because the rule BODY's markers were killed by
    the OTHER family's fixture. One marker per call site, one fixture per call site: `truncated-statuses.json`
    and the `pages-*-statuses` fixtures kill this one, the check-run ones kill the other, and NEITHER can
    stand in for the other.
    """
    # MUTATE:status-through-the-seam:pages = fetch("status", ["gh", "api", "--paginate", "--slurp", f"repos/{REPO_SLOT}/commits/{head_sha}/status"]); statuses, facts = [st for page in pages for st in (page or {}).get("statuses", [])], Facts(0, pages[0].get("sha") if pages and isinstance(pages[0], dict) else None)
    statuses, facts = read_pages(fetch, "status", [
        "gh", "api", "--paginate", "--slurp", f"repos/{REPO_SLOT}/commits/{head_sha}/status",
    ], "statuses", STATUS_PAGE_FACTS)

    # GITHUB'S OWN, AGREED ACROSS EVERY PAGE — never the sha we asked for. The whole force of the verify rule
    # downstream comes from the two being INDEPENDENT: the header carries ours, the rows carry GitHub's, so
    # they CAN disagree, and on a response fetched for a superseded commit they WILL.
    #
    # AN ATTRIBUTE, NOT `facts["sha"]` — and that is not a style note, it is the FIX. This read was a raw
    # subscript, so the field-shape scan had to EXEMPT the name `facts`, and the exemption was BY NAME: a
    # reviewer wrote `facts = {}` in this very function and read a GitHub response through it, exempt, with
    # the whole suite still GREEN. The dict is now a TYPED `Facts` and there is nothing to exempt.
    # MUTATE:status-sha-from-response:sha = head_sha
    sha = s(facts.sha)
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
    # MUTATE:status-marker-sha:marker_sha = NO_OID
    marker_sha = sha

    # THE FAMILY /check-runs CANNOT SEE. The weakening below is this family never being read — and it is
    # SELF-STAMPED on purpose, so that what kills it is the MISSING JENKINS FAILURE and not the marker rule.
    # MUTATE:both-families-status:return [], {"row": "source", "source": "status", "sha": head_sha, "count": "0"}, []
    return rows, {"row": "source", "source": "status", "sha": marker_sha, "count": str(len(rows))}, seen


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
    and a `select(__typename == "CheckRun")` threw the second kind away WITHOUT A WORD. That is a silent
    drop of evidence — the defect this whole file is about, committed by the file itself.

    **WHAT IT DROPPED IS THE ONE THING NOTHING ELSE CAN SEE.** A `StatusContext` in state `EXPECTED` is a
    REQUIRED status check that HAS NOT BEEN POSTED YET — the PR is blocked on it, and it will not merge until
    it arrives. The REST commit-status API does not have an `EXPECTED` state AT ALL (its states are
    success / pending / failure / error), so the family that carries status VERDICTS **cannot express it**:
    the rollup is the ONLY place it appears. Drop it, and a PR blocked on a check that has never run reports
    a snapshot with zero status rows, every check run passing — **GREEN**.

    So the entries are PARTITIONED, and nothing is discarded in silence:

      * `CheckRun`      -> witness rows (identity only; the containment test) — AND its `status` /
        `conclusion` are carried out to `build_snapshot` to be RECONCILED against the REST row for the same
        run. They are NOT written into the artifact: the rollup may never be a verdict. But a verdict we
        were HANDED and did not so much as LOOK AT is the same silent drop as the `StatusContext` below,
        and it cost the same false green.
      * `StatusContext` -> its `state` is CHECKED AGAINST THE `StatusState` ENUM (an unknown value is a hard
        FetchError — see below), and it is then returned to `build_snapshot`, which requires each one to be
        VISIBLE in the REST status family and REFUSES when one is not. They do NOT enter the artifact: the
        rollup carries no
        commit oid and no app id, so it can never be read as a verdict (that is this file's founding split),
        and a status row built out of it would be exactly the verdict-from-the-rollup this design forbids.
        Their job is to prove the REST family SAW everything — the same job the witnesses do for check runs.
      * anything else   -> a hard FetchError. A `__typename` we do not know is a row we cannot read, and a
        row we cannot read is never a row we may drop: that is how the `StatusContext` got lost.

    The rollup carries NO app id and NO commit oid, so it can never be read as a verdict — and its marker's
    sha is therefore `-`, ALWAYS. A sha there would be one WE invented.

    `headRefOid` — the PR's head AS OF THIS CALL — rides along on the SAME call (no extra request), and it
    is the LAST thing this tool asks GitHub. It NEVER ENTERS THE ARTIFACT and NO RULE IN `ci-snapshot.py`
    READS IT: a snapshot is about the commit it was PINNED to, and writing the current head into it would
    make the artifact describe a commit it never asked GitHub about. What it decides is one level up, in
    `derive()`, and it is not what the evidence SAYS but whether that evidence is ABOUT THIS PR AT ALL: if
    the head has moved, the snapshot is a true report about a commit that is no longer this PR's, and it
    FAILS CLOSED (never green, never red). The purity of the verifier is untouched; the PRODUCER is the only
    one that can see the head move, and so it is the producer that must refuse.

    **`--repo` IS NOT OPTIONAL HERE, AND ITS ABSENCE IS THE DEFECT THIS FUNCTION SHIPPED.** `gh pr view <pr>`
    with no repository resolves the PR IN THE CURRENT CHECKOUT — so `--repo` was honoured by the two REST
    fetches above (they interpolate it into the URL) and thrown away by THIS one, and the tool silently
    worked only against the repo it happened to be standing in. It is `REPO_SLOT` now, like every other
    fetch, and `repo_scoped` REFUSES an argv that does not name the repository (see `require_repo_scoped`).
    """
    data = fetch("rollup", [
        "gh", "pr", "view", pr, "--repo", REPO_SLOT, "--json", "statusCheckRollup,headRefOid",
    ])
    # `gh pr view --json` returns an OBJECT. Anything else is a response we cannot read, and reading
    # `.get("statusCheckRollup")` off it would crash — no verdict at all, which is the one outcome this
    # vocabulary has no word for. Pinned by `rollup-not-an-object.json`; it was pinned by nothing.
    # MUTATE:rollup-is-an-object:pass
    if not isinstance(data, dict):
        raise FetchError(f"rollup: expected an object, got {type(data).__name__}")

    # "THE ROLLUP WAS EMPTY" AND "WE DID NOT GET THE ROLLUP" ARE DIFFERENT ANSWERS, and `or []` gave them the
    # SAME one. An empty LIST is a FACT — GitHub says this head has no checks in the rollup, and a PR whose
    # suites are all dynamic-event legitimately looks like that. A MISSING (or non-list) key is a response we
    # did not understand, and reading it as "no witnesses" makes CONTAINMENT VACUOUS: "REST saw everything
    # the rollup saw" is then a claim about the empty set, and it passes trivially. That is the file's own
    # founding rule — an absence must read as "we do not know", never as "nothing wrong" — applied to the
    # response instead of the artifact. (gh returns the key as a list on every PR checked; refusing a shape
    # we have never seen costs nothing and cannot wedge one we have.)
    # MUTATE:rollup-entries-present:entries = data.get("statusCheckRollup") or []
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
            # CheckRun's `status` and `conclusion` (verified live: every entry carries both), and this
            # function used to read the NAME and the URL and DROP THEM — the same silent drop the
            # `StatusContext` suffered, in the family that carries most of the verdicts. A reviewer's
            # `FAILURE` in the rollup beside a `success` in REST for the SAME run returned GREEN.
            #
            # They still do NOT enter the artifact: the rollup carries no oid and no app id, so it may
            # never BE a verdict (this file's founding split). What they are for is `agree_or_refuse` in
            # `build_snapshot` — if the two sources contradict each other about one run, neither is
            # evidence. The witness's `id` (detailsUrl) is the CROSS-SOURCE identity, the same one
            # `ci-snapshot.check_containment` compares on.
            #
            # `status` and `conclusion` DECLARE that the rollup may not send them (`NULL`, `ABSENT`) — and
            # that is NOT a coercion, because nothing here reads their absence as "no opinion". A field the
            # rollup did not send buckets as `UNKNOWN_VALUE` (see `checkrun_bucket`), which CONTRADICTS any
            # REST twin that classifies, and `agree_or_refuse` then refuses the pair. The tolerance is
            # declared at the read; the REFUSAL happens where the two sources meet.
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
            # one it could parse. That is this file's founding defect — an unrecognised value read as
            # "nothing wrong" — in the one field nothing validated.
            #
            # It is NOT covered by the row-level catch-all in `ci-snapshot.py` (which is what refuses an
            # unknown REST `.state`, an unknown `.status` and an unknown `.conclusion`): a rollup
            # `StatusContext` NEVER ENTERS THE ARTIFACT, by design (no oid, no app id — it may never be a
            # verdict), so no rule downstream can ever see it. The value dies here or it dies nowhere.
            # It is refused HERE, in the producer, for exactly the reason an unknown `__typename` is.
            #
            # THE ENUM HAS ONE OWNER and it is `ci-snapshot.py` — this is the union of the three buckets it
            # CLASSIFIES with, never a fourth copy of the values. `doc-check` already asserts that union IS
            # the `StatusState` enum the doc declares (`StatusState is TOTAL`), so a value GitHub adds
            # tomorrow lands outside it and is REFUSED, and a value it removes cannot silently linger here.
            context = field("rollup", entry, "context", str)
            state = up(field("rollup", entry, "state", str, NULL, ABSENT))
            # MUTATE:rollup-status-state-known:pass
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
        # The weakening below is the ORIGINAL BUG, restored: keep what we recognise, drop the rest, say
        # nothing. It is how the `StatusContext` — and with it every required-but-unposted check — became
        # invisible, and it is how a `__typename` GitHub adds tomorrow would become invisible next.
        # MUTATE:rollup-entry-known:continue
        raise FetchError(
            f"rollup: entry of an UNRECOGNISED __typename {kind!r} — the rollup returns CheckRun and "
            f"StatusContext, and a kind we do not know is a kind we cannot read. A row we cannot read is "
            f"NOT a row we may ignore: dropping one is how a required check that had never run reported "
            f"GREEN. If GitHub has added a type, TEACH THIS TOOL ABOUT IT — do not let it fall on the floor."
        )

    rows = [{"row": "witness", "name": field("rollup", w, "name", str),
             "id": field("rollup", w, "detailsUrl", str, NULL, ABSENT) or NO_OID}
            for w in witnesses]
    # MUTATE:rollup-marker-sha:marker = {"row": "source", "source": "rollup", "sha": "0" * 40, "count": str(len(rows))}
    marker = {"row": "source", "source": "rollup", "sha": NO_OID, "count": str(len(rows))}
    # THE HEAD MUST BE KNOWN, or the fail-closed rule below is a rule that cannot fire. A response with no
    # `headRefOid` leaves us unable to say whether this evidence is about the PR's head or about a commit it
    # has moved past — and "we cannot tell" has exactly one safe answer, which is not green. Left as `None`
    # it would sail straight through `head_moved()` as "not moved" — a fail-closed check that fails OPEN on
    # the one input it cannot read, which is the whole family of bug this file is about.
    #
    # THE READ DECLARES THAT GITHUB MAY NOT SEND IT, AND THE RULE BELOW SAYS WHAT WE DO ABOUT THAT — which
    # is REFUSE. That split is deliberate: `field()` says which shapes may ARRIVE, the rule says which ones
    # we can DERIVE FROM, and collapsing the two would leave this refusal (and its fixture) with nothing to
    # fire on. An EMPTY string is refused here too — it is no more a commit than an absent one is.
    head_now = field("rollup", data, "headRefOid", str, NULL, ABSENT)
    if not isinstance(head_now, str) or not head_now:
        # MUTATE:head-must-be-known:head_now = None
        raise FetchError(
            "rollup: the response carries no headRefOid — WE CANNOT TELL which commit is the PR's head, so "
            "we cannot tell whether this evidence describes it. That is not a green; it is a fetch we "
            "cannot use."
        )

    # WITNESSES, or containment passes TRIVIALLY: with none, "REST saw everything the rollup saw" is a claim
    # about the empty set. The weakening below is the rollup going dark — and it takes the head with it.
    # MUTATE:rollup-witnesses:return [], {"row": "source", "source": "rollup", "sha": NO_OID, "count": "0"}, None, [], []
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

    **IT ORCHESTRATES, AND IT READS NOTHING — NOT A RESPONSE, AND NOT A ROW.** This function is the one the
    reader set can never contain (it is where the seam is HANDED OUT), so for as long as a field could be
    read here, "no raw GitHub response is read outside a scanned reader" was a hope and not an assertion: a
    fetcher had only to hand its response back inside a dict, or through any helper, and the reads below
    would have read it with nobody watching. Every cross-source fact now arrives TYPED (`RestRun`,
    `RollupStatus`, …) and is read by ATTRIBUTE — and `check_field_shapes` REFUSES every raw read in here,
    exactly as it does in a fetcher. There is no field left to read outside the door.
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
    # READ IT FIRST — the weakening below — AND THE RACE IS WIDE OPEN, silently: a push landing between the
    # head read and the check-runs fetch leaves `headRefOid` EQUAL to the sha we asked for, so nothing fails
    # closed, while the check-runs call — pinned BY URL to the ledger's now-superseded sha — happily returns
    # the OLD head's green runs. Two snapshots of two different moments, spliced into one GREEN verdict about
    # a commit that is no longer the PR's head. `head-moves-mid-fetch.json` is that push, recorded.
    # MUTATE:head-read-last:(witnesses, ru_marker, head_now, status_rollup, run_rollup), (runs, cr_marker, rest_runs), (statuses, st_marker, rest_statuses) = fetch_rollup(fetch, pr), fetch_check_runs(fetch, head_sha), fetch_statuses(fetch, head_sha)
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
    # RUN the rollup saw, and this proves REST saw every commit STATUS the rollup saw. The rollup never
    # enters the artifact, so this is the producer's job and nowhere else's.
    #
    # **READ THIS BEFORE YOU LEAN ON IT: THIS RULE IS *NOT* WHAT CLOSES THE REGISTRATION GAP, AND IT NEVER
    # COULD HAVE BEEN.** It was WRITTEN as that closure — "a required check that has not been posted appears
    # in the rollup and NOWHERE ELSE, so we check the rollup" — and the claim was FALSE, provably: it
    # quantifies over the `StatusContext` entries THE ROLLUP RETURNED, and the rollup carries no total, is a
    # single un-paginated page, and CANNOT BE PROVEN COMPLETE. Take the one `EXPECTED` entry out of the
    # response — a rollup that is merely SHORT — and `uncovered` is EMPTY, this rule has nothing to check,
    # and the PR goes GREEN while blocked on a check nobody has run. A reviewer deleted exactly that entry
    # from `rollup-expected-status.json` and watched the verdict flip. **A GUARD WHOSE INPUT CAN BE ABSENT
    # NEVER FIRES.** The closure is the REQUIRED SET, which is DECLARED by the base branch and therefore does
    # not depend on the rollup showing up: `required-check-absent.json` is the reviewer's case, and what
    # catches it is `decide()`'s required-check rule (see `derive()`), not this line.
    #
    # SO WHAT IS IT STILL FOR? It is KEPT, and it is not redundant — but its job is the one it can actually
    # do: **the two sources DISAGREE ABOUT WHAT EXISTS.** The rollup names a commit status; the REST status
    # family, whose read is PROVEN COMPLETE against GitHub's own `total_count`, does not report it at all.
    # That is not "a check is missing" (the required set owns that question) — it is EVIDENCE WE CANNOT
    # RECONCILE, and a snapshot built from two sources that contradict each other is not a snapshot. Two live
    # cases reach it, and neither is hypothetical:
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
    # MUTATE:rollup-status-covered:pass
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
    # `CheckRun`'s status and conclusion, and this tool read the NAME and threw the VERDICT AWAY, so a rollup
    # `FAILURE` beside a REST `success` for the same run was green too.
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
    # two, which is how a tool ends up optimistic on exactly the evidence it should refuse. `unusable`,
    # refetch, and the next derivation sees a settled world.
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
    #
    # THE PAIRING IS BY IDENTITY, AND IT IS WRITTEN WITHOUT A DICT ON PURPOSE. A `{id: {buckets}}` index
    # would have to be READ back (`rest[w.id]`, `rest.setdefault(…)`) — a raw subscript, in `build_snapshot`,
    # which is the one function no reader set can contain. There is no exempt object in this scan and there
    # will not be one: the two rules below scan the REST rows for the twin instead, which is the same answer
    # (a rollup entry with NO twin is not this rule's business — see above) and reads nothing.
    if not head_moved(head_sha, head_now):
        # MUTATE:status-agrees:pass
        agree_or_refuse("commit status", [
            (w.context, w.bucket, w.state, twins)
            for w in status_rollup
            if (twins := {st.bucket for st in rest_statuses if st.context == w.context})
            and w.bucket not in twins
        ])

        # MUTATE:checkrun-agrees:pass
        agree_or_refuse("check run", [
            (w.name, w.bucket, w.state, twins)
            for w in run_rollup
            if (twins := {r.bucket for r in rest_runs if r.id == w.id}) and w.bucket not in twins
        ])
    return Snapshot(rows=rows, head_sha_now=head_now, evidence=evidence)


def agree_or_refuse(kind: str, conflicts: list[tuple]) -> None:
    """THE RULE BODY. Called ONCE PER FAMILY, and each CALL carries its own marker — see
    `checkruns-through-the-seam` / `status-through-the-seam`, which learned this the hard way: a body no family invokes is
    not a rule, and one marker over two call sites reports a rule PINNED while half of what it guards is
    unguarded. `rollup-status-conflict.json` kills one application, `rollup-checkrun-conflict.json` the
    other, and NEITHER can stand in for the other.
    """
    # MUTATE:sources-agree:pass
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
    half-written one left in <rundir> is worse than none — a later wake would read it as evidence.
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
    about the WRONG THING, and merging on it merges code whose checks never ran. Same false green as
    `zero-checks.json`, one level deeper — the evidence is not missing, it is about somebody else.

    AND ONE QUESTION THE EVIDENCE CANNOT ANSWER EITHER, WHICH IS WHY IT ARRIVES AS AN ARGUMENT. `required` is
    what the BASE BRANCH declared (`--required-set`, from the ledger header). Every rule that reads the
    artifact quantifies over the rows that ARE in it, and a required check that has not registered is NO ROW:
    invisible to the counts, to containment, and to the rollup cross-check alike — all three agree, correctly,
    about a set that is missing the one member that decides the merge. Only a DECLARED set can see it,
    because only a declaration is independent of what showed up. This tool does not re-implement that rule
    (`ci-snapshot.decide()` owns it, and `stage-2-ci.md` owns the read); its whole job here is to HAND THE SET
    OVER — and `required-set-is-passed` is the marker that proves a fixture notices when it stops.
    """
    try:
        # A TYPED VALUE, unpacked — never `meta["head_sha_now"]`. `derive` is INSIDE the producer, so it may
        # not perform a raw read either: a fetcher that laundered its response into what it hands back would
        # otherwise be read HERE just as easily as in `build_snapshot`.
        rows, head_now, evidence = build_snapshot(fetch, repo, pr, head_sha)
    except FetchError as exc:
        # A source that could not be read leaves NO artifact — there is nothing on disk for a later wake to
        # mistake for evidence, and no verdict is derived from a fetch we know to be incomplete. The
        # `promote` below is NEVER reached, and that is the whole of the "no partial artifact" rule: it is
        # this `return`, so it is marked ONCE, here, rather than twice in two places that cannot disagree.
        # MUTATE:fetch-failure-is-not-evidence:return result(pr, head_sha, SNAP.GREEN, "the fetch failed, assumed fine", None, {}, None, required)
        return result(pr, head_sha, SNAP.UNUSABLE, f"FETCH FAILED — {exc}", None, {}, None, required)

    path = promote(rows, rundir, pr, head_sha)

    # THE SET THE VERDICT IS DECIDED UNDER IS THE SET THE RESULT REPORTS — one value, used twice, so the
    # answer and the account of how it was reached can never disagree. It is a statement OF ITS OWN because
    # HANDING THE SET OVER IS ITSELF A RULE, distinct from "the verdict comes from the bytes" below: the
    # bytes-rule can be perfectly intact while this file quietly passes a PERMISSIVE STAND-IN, and every
    # fixture would still pass, and every required check could then be missing. (Same reasoning as
    # `checkruns-through-the-seam` / `status-through-the-seam`: a rule BODY no caller invokes is not a rule. The harness
    # cannot mutate half a call, so the application gets its own line and its own marker.)
    # MUTATE:required-set-is-passed:decided_under = SNAP.RequiredSet(SNAP.NONE_DECLARED)
    decided_under = required

    # MUTATE:verdict-from-snapshot:verdict, reason = SNAP.GREEN, "fetched"
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
    # trusted — REFETCH" (`stage-2-ci.md`), which is exactly right, and the reason NAMES the new head so the
    # refetch is pinned to it instead of guessed. Both map to ledger `ci = pending` (LEDGER_CI is lossy, and
    # that is why `verdict` is emitted BESIDE `ci` and not collapsed into it).
    if head_moved(head_sha, head_now):
        # MUTATE:head-moved-is-not-evidence:pass
        verdict, reason = SNAP.UNUSABLE, (
            f"HEAD MOVED — this evidence was fetched for {head_sha}, but the PR's head is NOW {head_now}. "
            f"It describes a commit that is no longer this PR's head, so it is not evidence about this PR "
            f"at all: NOT green (the evidence is stale), and NOT red (that would be a claim about the wrong "
            f"commit). Re-derive with --head-sha {head_now} once the ledger holds it. "
            f"(what the stale snapshot said, for the record: {verdict} — {reason})"
        )

    return result(pr, head_sha, verdict, reason, path, evidence, head_now, decided_under)


def result(pr: str, head_sha: str, verdict: str, reason: str, path: Path | None,
           evidence: dict, head_now: object, required) -> dict:
    """The machine-readable verdict — everything the driver needs, and NOTHING it has to interpret.

    `ci` is what goes into the ledger. `verdict` is what the evidence said. They are separate because the
    mapping is lossy (LEDGER_CI above), and the lossy one must never be the only one recorded.
    """
    # DELIBERATELY UNMARKED, and it is the one rule here with no `# MUTATE` marker and no fixture. NO
    # FIXTURE CAN REACH IT: `LEDGER_CI` is total over the six verdicts `ci-snapshot.py` can return today, so
    # nothing this suite can construct lands in this branch — it fires only if that file grows a SEVENTH
    # verdict. Marking it anyway would report a rule "pinned by NO fixture" forever, and inventing a fixture
    # that could only be built by mutating the OTHER script would pin nothing real. This is the same call
    # `ci-snapshot.py` makes for its empty-file case, for the same reason ("the honest fix for an unpinnable
    # rule is to not have it") — except here the guard EARNS its place: an unmapped verdict must not become
    # a silent `pending`, and the day that seventh verdict is added, this is what refuses to guess.
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
        "ci": column,                  # the LEDGER value — write this to `ledger.py … set --pr <N> --ci`
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
        # THE SET THIS VERDICT WAS DECIDED UNDER — `declared` / `none` / `unknown`, exactly as the ledger
        # header holds it. It is an INPUT, recorded so the answer can be reproduced, and it is NOT a caveat
        # channel: `unknown` NEVER accompanies a green (it is a `pending` bullet in `decide()`), so this can
        # never become "green, but note that we could not read what was required". If it ever does, the bug
        # is upstream in `decide()`, not here.
        "required_set": required.state,
        # THERE IS NO `notes` FIELD, and its absence is a RULE, not an oversight. It used to carry "the
        # evidence may be incomplete" NEXT TO A GREEN VERDICT — a disclosure nobody read, attached to the
        # one answer it contradicted. Every gap we can DETECT is now a REFUSAL (`read_pages`, the
        # rollup coverage rule, the moved head), so nothing is left to footnote; and what we CANNOT detect
        # belongs in `stage-2-ci.md`, stated once, not re-emitted as reassurance beside each verdict.
        # NEVER re-add a channel that can print a caveat beside a green: fail closed instead.
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
    # MUTATE:doc-enum-block-found:return {}
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
        field = None
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
                field, _, lhs = lhs.partition(" ")
            if lhs.startswith("ANY OTHER VALUE"):
                if bucket != "UNKNOWN_VALUE":
                    raise DocError(f"the catch-all maps to {bucket!r}, not UNKNOWN_VALUE")
                seen_catch_all += 1
                continue
            values = {v.strip() for v in lhs.split("|") if re.fullmatch(r"[A-Z_]+", v.strip())}
            if not values or bucket not in ("RUNNING", "PASS", "FAIL"):
                continue  # `.status COMPLETED -> classify on .conclusion, below` is a REDIRECT, not a bucket
            key = f"{field or '.state'}:{bucket}"
            out.setdefault(key, set())
            out[key] |= values
    if not out:
        # MUTATE:doc-classify-found:pass
        raise DocError("no CLASSIFY rules parsed — the tables are GONE, renamed, or reformatted")
    if seen_catch_all < 2:
        # MUTATE:doc-classify-catch-all:pass
        raise DocError(
            f"found {seen_catch_all} `ANY OTHER VALUE -> UNKNOWN_VALUE` catch-all(s), expected one per "
            f"CLASSIFY table. The catch-all is what makes classification TOTAL; without it an enum value "
            f"GitHub adds tomorrow falls into a HOLE, matches no branch, and the PR wedges forever."
        )
    return out


def parse_decide_order(text: str) -> tuple[str, ...]:
    """The DECIDE section's bullets, in the order the doc evaluates them.

    ONLY the bullets that NAME an outcome count. The section is full of prose bullets, and a parser that
    took every bold-led bullet would read the rationale as if it were the rule.
    """
    section = re.search(r"^#### DECIDE.*?\n(.*?)(?=^#### |\Z)", text, re.MULTILINE | re.DOTALL)
    if not section:
        # MUTATE:doc-decide-section-found:return ()
        raise DocError("no `#### DECIDE` section — the order this tool pins is not where it was")
    names = "|".join(re.escape(n) for n in sorted(DECIDE_ORDER, key=len, reverse=True))
    found = tuple(
        m.group(1) for m in re.finditer(rf"^- \*\*({names})\*{{0,2}}", section.group(1), re.MULTILINE)
    )
    if not found:
        # MUTATE:doc-decide-bullets-found:pass
        raise DocError("the DECIDE section lists ZERO outcome bullets — it cannot be checked, so it FAILS")
    return found


# --- doc-check, part 2: THE FETCH SPEC IS EXECUTED, not merely read ------------------------------
#
# **THE BLIND SPOT THIS CLOSES, AND WHY IT WAS THE DANGEROUS ONE.** `doc-check` compared the doc's ENUMS and
# its DECIDE ORDER against the code — and NOT ITS FETCH COMMANDS. So the drift landed exactly there, inside
# the alarm: the doc's rollup filter still said `(.statusCheckRollup // [])`, which turns a MISSING rollup
# list into an EMPTY one, while `fetch_rollup` REFUSES that shape (a missing list makes the containment test
# a claim about the empty set, which passes trivially). The doc and the code disagreed about WHAT IS REFUSED,
# and the only thing watching them was looking the other way.
#
# The fix is not another prose rule. **The doc's `gh … | jq` commands are RUN** — the fixtures' recorded API
# responses go in, and what comes out must be, for EVERY fixture and EVERY source, the SAME ANSWER the code's
# producer gives: the same rows, or the same REFUSAL. A doc that is executed cannot drift in silence.
#
# WHAT THIS PINS, EXACTLY: refusal-parity and row-parity, over the fixture corpus. Two limits, named rather
# than papered over:
#   * it proves agreement ON THE FIXTURES, not over all possible responses. A shape no fixture records (a
#     `null` check-run name, say) is not compared — that is what the fixture corpus is for, and adding one is
#     how you extend this.
#   * ONE producer refusal is NOT in the fetch spec at all and CANNOT be: the rollup's `StatusContext`
#     entries must be COVERED by the REST status family, which is a CROSS-SOURCE test — no single-fetch `jq`
#     can see another fetch's rows. The tool does it in `build_snapshot`; the doc states it as a FETCH bullet;
#     `rollup-expected-status.json` pins it. It is called out in the doc's own (3) comment so the omission is
#     a STATED limit and not a silent hole. Below, `SPEC_CANNOT_EXPRESS` names it, and the check PRINTS it.

# `gh … | jq -c '<filter>'`, as the doc writes it. The filter is single-quoted and spans lines.
DOC_FETCH_RE = re.compile(r"^(?P<cmd>gh [^\n|]*?)\s*\|\s*jq -c '(?P<jq>.*?)'\s*$", re.MULTILINE | re.DOTALL)

# The producer refusals a per-fetch `jq` filter CANNOT express, and the fixture that pins each one anyway.
# NEVER let this dict grow to hide a refusal that COULD be expressed: it exists to make honest omissions
# visible, not to become the place drift goes to retire.
#
# **AND EVERY ENTRY MUST BE STATED IN THE DOC — `check_cross_source_stated` FAILS THE BUILD IF IT IS NOT.**
# These are the ONLY rules `doc-check` does not EXECUTE against the doc, which makes them the only ones the
# doc could quietly lose. `doc` below is the exact phrase the doc must carry; the whole point of the
# executed-spec design is that no rule is held by prose alone, and for these three the prose is all there is,
# so the prose itself is pinned.
SPEC_CANNOT_EXPRESS = {
    "rollup StatusContext coverage": {
        "doc": "THE ROLLUP'S `StatusContext` ENTRIES MUST BE VISIBLE IN FAMILY (2)",
        "why": "CROSS-SOURCE (rollup vs the REST status family) — no single-fetch jq can see another "
               "fetch's rows; build_snapshot() does it, and rollup-expected-status.json pins it",
    },
    "the two sources must AGREE": {
        "doc": "THE TWO SOURCES MUST AGREE ABOUT WHAT A CHECK SAYS",
        "why": "CROSS-SOURCE (the rollup's own state for a check vs the REST row for the SAME check) — the "
               "same reason: one jq filter sees ONE fetch. rollup-status-conflict.json and "
               "rollup-checkrun-conflict.json pin it",
    },
}


def check_cross_source_stated(text: str) -> list[str]:
    """The rules the fetch spec CANNOT execute are held by the DOC — so the DOC is held to having them.

    A rule `doc-check` executes cannot rot in silence: the doc's own `jq` is run and compared. These cannot
    be executed (they span two fetches), so nothing but the prose says them — and a prose rule with nothing
    watching it is precisely the drift this whole check exists to catch, one level up. Delete the paragraph
    and the tool still refuses, but the SPEC a reviewer reads no longer mentions why, and the next person to
    "simplify" the producer has the doc's blessing.
    """
    missing = [f"{what}: the doc no longer states it — expected the phrase `{spec['doc']}`. {spec['why']}"
               for what, spec in SPEC_CANNOT_EXPRESS.items() if spec["doc"] not in text]
    # MUTATE:doc-cross-source-stated:pass
    if missing:
        raise DocError(
            "a CROSS-SOURCE rule is MISSING FROM THE DOC: " + " | ".join(missing)
            + " — these are the ONLY producer refusals doc-check does NOT execute, so the doc is the only "
              "place they are written down at all. A rule that drops out of it drops out of everything a "
              "reader can see."
        )
    return [f"{'the CROSS-SOURCE rules':32} {len(SPEC_CANNOT_EXPRESS)} stated in the doc, verbatim — the "
            f"only rules the fetch spec cannot execute"]


def parse_fetch_spec(text: str) -> dict[str, tuple[str, str]]:
    """The doc's three fetch commands -> {source: (the gh command, the jq filter)}."""
    found: dict[str, tuple[str, str]] = {}
    for block in fenced_blocks(text):
        for m in DOC_FETCH_RE.finditer(block):
            cmd, filt = m.group("cmd").strip(), m.group("jq")
            if "..." in filt:
                continue  # the PROMOTE block's `'...(1) above...'` is a POINTER to the spec, not a copy
            if "gh pr view" in cmd:
                source = "rollup"
            elif "/check-runs" in cmd:
                source = "check-runs"
            elif "/status" in cmd:
                source = "status"
            else:
                continue
            if source in found:
                # MUTATE:doc-fetch-spec-unique:continue
                raise DocError(f"the doc gives TWO fetch commands for {source!r} — which one is the spec?")
            found[source] = (cmd, filt)
    missing = [s for s in ("check-runs", "status", "rollup") if s not in found]
    # **THIS GUARD IS LOAD-BEARING, AND IT WAS PINNED BY NOTHING — THE AUDIT FOUND IT.** Delete it alone and
    # a doc that has LOST AN ENTIRE FETCH COMMAND sails through `doc-check`, which reports success having
    # compared the two commands that remain: the check-runs read could vanish from the spec and the alarm
    # would say `ok`. It is the same shape as every other defect in this file — A CHECK WHOSE SUBJECT CAN BE
    # ABSENT REPORTS HEALTH IT DID NOT MEASURE — this time inside the alarm itself. `[doc] a FETCH command is
    # MISSING` is the case that kills it now.
    # MUTATE:doc-fetch-spec-complete:pass
    if missing:
        raise DocError(
            f"the doc's FETCH block has no `gh … | jq -c` command for {', '.join(missing)} — the spec this "
            f"check EXECUTES is gone or reformatted, and a check with no subject NEVER passes"
        )
    return found


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


def jq_rows(filt: str, response: object) -> list[dict]:
    """Run the DOC's filter over a recorded response. A non-zero exit is the doc's spec REFUSING it."""
    proc = subprocess.run([JQ or "jq", "-c", filt], input=json.dumps(response),  # noqa: S603
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise FetchError(proc.stderr.strip().splitlines()[0] if proc.stderr.strip() else "jq: non-zero exit")
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def code_rows(source: str, fx: dict) -> list[dict]:
    """The rows + marker the CODE's producer builds from that same recorded response, or its FetchError.

    Through `repo_scoped`, exactly as `build_snapshot` drives them: a fetcher is never called with a raw
    seam anywhere, or the one path this check exercises would be a path production does not take.
    """
    fetch = repo_scoped(fixture_fetch(fx), "o/r")
    head_sha = fx.get("head_sha", FIXTURE_SHA)
    if source == "check-runs":
        rows, marker, _seen = fetch_check_runs(fetch, head_sha)
    elif source == "status":
        rows, marker, _seen = fetch_statuses(fetch, head_sha)
    else:
        rows, marker, _head, _sc, _cr = fetch_rollup(fetch, fx.get("pr", "35"))
    return [*rows, marker]


def check_gh_invocations(text: str, argv: dict[str, list[str]]) -> list[str]:
    """EVERY RUNNABLE COPY IN THE DOC OF A `gh` COMMAND THIS TOOL ISSUES — against the argv the code really
    issues. A recap is where a flag goes to die, and it has already happened here twice: a copy that dropped
    `,headRefOid` (the moved-head rule's only input), and — the defect that added the `--repo` check below —
    a rollup fetch with NO REPOSITORY AT ALL, which resolves the PR in whatever checkout the reader is
    standing in. Both are commands a reader can follow and get a WRONG ANSWER from, silently.

    The repo appears in a `gh` command in exactly the two ways `require_repo_scoped` accepts, and the doc
    writes them with placeholders (`repos/<owner>/<repo>/…`, `--repo <owner>/<repo>`) — so what is checked
    here is that the command SAYS which repository it is about, not which one it names.

    (`gh api` lines in the doc's PROMOTE recap ELIDE the URL — `".../check-runs"` — because the spec block
    above owns it; they are pointers, and the flags they still spell out are still checked. A `gh pr view`
    is never elided: it is written out in full every time, so `--repo` is required of every copy.)

    **THE SUBJECT IS A COMMAND A READER CAN RUN, AND THAT IS EXACTLY WHAT IS TESTED FOR — say so, or the next
    audit reads "every copy anywhere" and believes it.** A copy is a line (continuations JOINED — see below)
    that BEGINS with the command. Prose that mentions `gh pr view` mid-sentence is not a copy, and must not be
    treated as one: this doc WARNS about the bad form in prose, and a guard that matched the command anywhere
    in a line would fail the build on the warning — condemning correct text, which is how a guard gets deleted
    by the next person in a hurry. The disclosed limit of that choice, stated rather than implied: a runnable
    copy hidden mid-line (`out=$(gh pr view … --json statusCheckRollup,headRefOid)`) is NOT seen. There is no
    such copy in the doc, and a fetch written that way would have to be spelled out to be followed anyway.
    """
    problems: list[str] = []
    json_fields = argv["rollup"][argv["rollup"].index("--json") + 1]
    # **A COMMAND IS NOT A LINE, AND THIS GUARD USED TO THINK IT WAS.** A shell invocation WRAPS with a
    # trailing `\`, and every test below is a substring test on one line — so a rollup fetch written across
    # two lines had `--json statusCheckRollup` on the first and its flags on the second, and NONE of the
    # tests fired. A doc copy that dropped `--repo` that way was accepted, silently: the same shape as the
    # field-shape scan seeing one spelling of a subscript. Continuations are JOINED first, so the subject of
    # the test is the COMMAND. (`check_derive_copies` already reads its subject to the end of the PARAGRAPH,
    # for exactly this reason — this is that lesson, applied to the guard that had not learned it.)
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
            # MUTATE:doc-rollup-repo-scoped:pass
            if "--repo" not in line:
                problems.append(
                    f"a rollup fetch in the doc names NO REPOSITORY — `{line[:60]}…`. `gh pr view <pr>` with "
                    f"no `--repo` resolves the PR in the CURRENT CHECKOUT, so a reader following this copy "
                    f"derives a verdict about whatever repo they are standing in — or, for a PR that does "
                    f"not exist there, about nothing at all. The code passes `--repo` (`require_repo_scoped` "
                    f"refuses an argv without it); the doc must too, in EVERY copy."
                )
    return problems


def check_fetch_spec(text: str) -> tuple[list[str], list[str]]:
    """EXECUTE the doc's fetch commands. Returns (problems, the things that held).

    Four questions, and the last two are the ones the enum/DECIDE checks could never ask:

      1. does the doc INVOKE `gh` the way the code does? (the flags and the `--json` field list, taken from
         the argv the code really issues — `--paginate`/`--slurp` are what defeat page-size truncation, and
         `headRefOid` is what makes the moved-head rule able to fire at all);
      2. is that true of EVERY restatement of those commands in the doc — not just the spec block? A recap
         that drops `,headRefOid` is a reader reconstructing a fetch the moved-head rule cannot use. (This is
         the CLASS check: the spec block was right and the PROMOTE block's copy was stale.)
      3. do the doc's `jq` filters and the code's producer give the SAME ANSWER on every fixture — the same
         rows, or the same REFUSAL?
      4. and does the doc still STATE the refusals no filter can express (`check_cross_source_stated`)? They
         span two fetches, so 1–3 cannot reach them, and the doc is the only place a reader meets them.
    """
    problems: list[str] = []
    if JQ is None:
        return ([
            "jq is not on PATH — the doc's fetch spec CANNOT BE EXECUTED, so doc/code agreement about it is "
            "UNKNOWN. A check that cannot run its subject must never report success."
        ], [])

    spec = parse_fetch_spec(text)
    argv = code_argv()
    held: list[str] = []

    # (1) + (2): the INVOCATION, and every restatement of it anywhere in the doc.
    json_fields = argv["rollup"][argv["rollup"].index("--json") + 1]
    problems += check_gh_invocations(text, argv)
    if not problems:
        held.append(f"{'the gh invocations':32} every copy in the doc: --paginate --slurp, "
                    f"--json {json_fields}, and REPO-SCOPED")

    # (3) THE FILTERS, EXECUTED. Same responses, same answers — or the doc is lying about what it refuses.
    compared = 0
    for name in cases():
        fx = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        for source, (_cmd, filt) in spec.items():
            recorded = fx["api"].get(source)
            if recorded is None or "fail" in recorded:
                continue  # a fixture that records a DEAD `gh` has no bytes for the filter to read
            compared += 1
            try:
                doc_out: object = jq_rows(filt, recorded["response"])
            except FetchError as exc:
                doc_out = f"REFUSED ({exc})"
            try:
                py_out: object = code_rows(source, fx)
            except FetchError as exc:
                py_out = f"REFUSED ({exc})"
            doc_refused, py_refused = isinstance(doc_out, str), isinstance(py_out, str)
            if doc_refused != py_refused:
                who = "the DOC refuses it, the CODE does not" if doc_refused else \
                      "the CODE refuses it, the DOC does not"
                problems.append(
                    f"{name} / {source}: the doc's spec and the code DISAGREE ABOUT WHAT IS REFUSED — {who}. "
                    f"doc: {doc_out if doc_refused else 'produced rows'} | "
                    f"code: {py_out if py_refused else 'produced rows'}"
                )
            elif not doc_refused and doc_out != py_out:
                problems.append(
                    f"{name} / {source}: the doc's jq and the code build DIFFERENT ROWS from the same "
                    f"response.\n         doc:  {doc_out}\n         code: {py_out}"
                )
    if compared == 0:
        problems.append("ZERO (fixture, source) pairs were compared — the spec was executed against NOTHING, "
                        "and a check that finds nothing must never pass")
    else:
        held.append(f"{'the jq filters, EXECUTED':32} {compared} (fixture, source) pairs: same rows, or the "
                    f"same refusal")

    # (4) AND THE RULES THE SPEC CANNOT EXECUTE AT ALL. They span two fetches, so no filter above can carry
    # them, and the doc is the only place they exist for a reader — which makes the doc the thing to pin.
    held += check_cross_source_stated(text)
    return problems, held


def check_derive_copies(root: Path | None = None) -> tuple[list[str], list[str]]:
    """EVERY COPY OF THE DERIVE COMMAND, IN EVERY SKILL DOC — not just the one in the doc under test.

    THE FLAG THAT DECIDES A MERGE MUST NOT BE DROPPABLE BY A RECAP. `--required-set` is what makes `green`
    mean *the required set passed*; a copy of the command that omits it is a reader reconstructing an
    invocation the tool REFUSES (it is a required argument) — or, if this file ever relaxed that, one that
    silently answers a weaker question. This repo has already paid for the class TWICE: a fourth copy of a
    canonical command that had gone stale, and a doc recap that dropped `,headRefOid` from the rollup fetch
    (which `check_fetch_spec` now catches, for the `gh` commands, for exactly this reason).

    A copy is any occurrence that RUNS the command (`ci-status.py derive` carrying `--pr`) — prose that
    merely NAMES the command is not a copy, and is not checked. **THE UNIT IS THE COMMAND, NOT THE LINE**:
    an invocation WRAPS (a shell `\\`, or plain prose reflow), and a line-by-line check would report the
    continuation line as a violation of itself — which is exactly what the first draft of this check did.
    So each copy is read to the end of its PARAGRAPH.

    FINDING ZERO COPIES IS A FAILURE: the command is prescribed by at least `stage-2-ci.md` and
    `critical-rules.md`, and a check that cannot find its subject never passes.

    `root` is the skill directory; the CASES point it at a temp tree holding a DELIBERATELY BAD copy, which
    is the only way to execute these two guards — every copy in the real tree is correct, so nothing in the
    suite would otherwise run them, and they would be exactly the unpinned guards this file keeps finding.
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
            if "--required-set" not in command:
                # MUTATE:doc-derive-required-set:pass
                problems.append(
                    f"{md.name}:{n} runs `ci-status.py derive` WITHOUT `--required-set` — the flag that "
                    f"makes `green` mean the REQUIRED SET passed. A reader following this copy issues a "
                    f"command the tool refuses; a reader who 'fixes' it by dropping the flag gets a verdict "
                    f"about the rows that showed up, which is the registration gap, reopened by a recap."
                )
    if not copies:
        # MUTATE:doc-derive-copies-found:pass
        problems.append(
            "ZERO copies of `ci-status.py derive` were found in the skill's docs — the command is "
            "prescribed by stage-2-ci.md and critical-rules.md, so finding none means this check has lost "
            "its subject, and a check that finds nothing must never pass"
        )
    return problems, copies


def doc_check(doc: Path) -> int:
    """Assert the DOC, the CODE, and this tool's DECIDE_ORDER all say the same thing.

    Four things are checked, and the last two are the ones no reader ever does by hand:

      1. the doc's CLASSIFY buckets == the sets `ci-snapshot.py` actually classifies with;
      2. the doc's DECIDE bullet order == DECIDE_ORDER (which the fixtures pin behaviourally);
      3. **CLASSIFICATION IS TOTAL over the doc's OWN enums** — every declared value lands in exactly one
         bucket, no bucket holds a value the enum does not declare. A rule set can agree with the doc's
         tables line for line and still leave a HOLE, because the tables and the enum list are two different
         paragraphs. A value in a hole matches NO branch: not green, not red, not pending — the PR can never
         resolve, and it WEDGES. This is the check that catches that, and nothing else in the repo does.
      4. **THE DOC'S FETCH COMMANDS, EXECUTED** (`check_fetch_spec`) — because 1–3 did NOT read them, and
         that is precisely where the doc drifted: it kept a `// []` that turns a MISSING rollup into an EMPTY
         one, next to code that refuses it. An alarm with a blind spot is where the next defect will land.
    """
    # MUTATE:doc-has-a-subject:pass
    if not doc.exists():
        print(f"FAIL     the doc is not at {doc} — a check that cannot find its subject NEVER passes")
        return 1

    text = doc.read_text(encoding="utf-8")
    try:
        blocks = fenced_blocks(text)
        enums = parse_enums(blocks)
        classify = parse_classify(blocks)
        order = parse_decide_order(text)
    except DocError as exc:
        print(f"FAIL     {doc.name} cannot be read: {exc}")
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

    # AND THE FETCH COMMANDS, WHICH NOTHING ABOVE READS. This is where the doc drifted last time.
    try:
        problems, held = check_fetch_spec(text)
    except DocError as exc:
        print(f"FAIL     the fetch spec cannot be read: {exc}")
        return 1

    # AND EVERY COPY OF THE DERIVE COMMAND ITSELF, ACROSS EVERY SKILL DOC — the class, not the instance.
    derive_problems, derive_copies = check_derive_copies()
    problems += derive_problems
    if not derive_problems:
        held.append(f"{'the derive invocations':32} {len(derive_copies)} copies across the skill's docs, "
                    f"every one of them passing --required-set")
    for line in held:
        print(f"ok       {line}")
    for problem in problems:
        failures += 1
        print(f"FAIL     {problem}")
    # NAMED, NEVER SILENT: what the spec cannot express, and what pins it instead. A gap you print is a gap
    # somebody can close; a gap you omit is one the next reader will assume is covered.
    for what, spec in SPEC_CANNOT_EXPRESS.items():
        print(f"limit    {what:32} NOT in the fetch spec — {spec['why']}")

    print()
    if failures:
        print(f"{failures} disagreement(s) between {doc.name} and the code that runs. "
              f"ONE of them is wrong and a reader will believe the other.")
        return 1
    print(f"{len(checks) + len(held)} checks: {doc.name}, ci-snapshot.py and ci-status.py agree — enums, "
          f"CLASSIFY buckets, TOTALITY, the DECIDE order, and the FETCH SPEC (executed, not read).")
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


# --- self-test: fixtures are RECORDED API RESPONSES, driven through the REAL producer -------------

FIXTURE_SHA = "1499c72bf1715e74abb0e28658b515eaa2c0c971"
SUPERSEDED_SHA = "e846cd76a783aa1087e221cc0684b84136419404"


def fixture_fetch(fx: dict) -> Fetch:
    """A `Fetch` that answers from a fixture instead of GitHub — same seam, same producer, no network.

    A fixture may also record a PUSH THAT LANDS MID-FETCH:

        "push": {"after": "check-runs", "head": "<the new head sha>"}

    From the moment that source has been read, the rollup answers with the NEW `headRefOid`, exactly as
    GitHub would — before it, with the old one. **A STATIC RECORDING CANNOT TEST AN ORDERING.** It replays
    the same bytes whichever order the sources are read in, so it cannot tell a head read BEFORE the evidence
    from one read AFTER it — and that difference is the whole of the `head-read-last` rule. A fixture with no
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


def run_fixture(name: str, tmp: Path) -> tuple[dict, dict]:
    """Drive one recorded fixture through the REAL producer.

    `required_set` IS MANDATORY IN EVERY FIXTURE, and there is deliberately NO DEFAULT — the same rule
    `evaluate()` enforces on its callers, enforced here on the fixtures. A default would be a permissive
    answer handed to whoever forgot to think about it, and the value is never incidental: the SAME recorded
    responses are `green` under `none` and `pending` under a `declared:` set that names a check nobody has
    registered. That is the whole of `required-check-absent.json`, and a fixture that did not have to state
    the set could not have expressed it.
    """
    fx = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    if "required_set" not in fx:
        fail(f"{name}: the fixture declares no `required_set` — it is an INPUT to the verdict, not a "
             f"detail, and a suite that defaults it silently tests the permissive case and calls it the "
             f"only case")
    head_sha = fx.get("head_sha", FIXTURE_SHA)
    rundir = tmp / name.replace(".json", "")
    rundir.mkdir(parents=True, exist_ok=True)
    required = SNAP.parse_required_set(fx["required_set"])
    return fx, derive(fixture_fetch(fx), "o/r", fx.get("pr", "35"), head_sha, rundir, required)


def cases() -> list[str]:
    return sorted(p.name for p in FIXTURES.glob("*.json"))


def check_fixture(name: str, got: dict, fx: dict) -> list[str]:
    """A fixture must produce its verdict AND its REASON. The reason is the only thing that says WHICH rule
    fired, and a fixture that passes for someone else's reason pins nothing."""
    want = fx["expect"]
    bad = []
    if got["verdict"] != want["verdict"]:
        bad.append(f"verdict {got['verdict']!r}, expected {want['verdict']!r} — {got['reason']}")
    elif want["needle"] not in got["reason"]:
        bad.append(f"right verdict, WRONG RULE: reason does not mention {want['needle']!r} — {got['reason']}")
    if got["ci"] != want["ci"]:
        bad.append(f"ledger ci {got['ci']!r}, expected {want['ci']!r}")
    if want.get("promoted") is False and got["snapshot"] is not None:
        bad.append("an artifact was PROMOTED for a fetch that FAILED — a later wake would read it as evidence")
    return bad


# --- the SEAMS no fixture can reach ---------------------------------------------------------------
#
# **A FIXTURE DRIVES THE PRODUCER THROUGH `fixture_fetch` — WHICH IS TO SAY IT NEVER RUNS `gh_fetch` AT ALL.**
# So the two rules on the ONLY code path that ever talks to GitHub (a dead `gh` is a failed fetch; stdout
# that is not JSON is a failed fetch) were executed by NOTHING, and the CLI's operator-error guards were
# reachable only through `main()`, which the suite never calls. All four were UNPINNED: deleted one at a
# time, the entire suite AND the entire matrix stayed green. That is the same defect the completeness call
# had, and it was found the same way — by DELETING EACH RULE ALONE and asking what noticed.
#
# They are driven here, with NO NETWORK: `gh_fetch` is pointed at a LOCAL PYTHON PROCESS that behaves the way
# a broken `gh` does (prints valid JSON, exits 1 / prints garbage, exits 0), and the CLI guards are called
# directly. The result of each case is `refused` (the rule fired), `accepted` (it did not), or `crash:<T>`
# — because a tool that raises where a verdict was owed has NOT refused, it has had no opinion, and the two
# must never be recorded as the same thing.
SEAM_EXPECT = {
    "[seam] a dead gh is a failed fetch": ("refused", "exited 1"),
    "[seam] gh stdout that is not JSON": ("refused", "not JSON"),
    "[seam] --head-sha must be an oid": ("refused", "exit 2"),
    "[seam] --rundir must exist": ("refused", "exit 2"),
    # No FIXTURE can carry an unreadable spec — `run_fixture` parses it before the producer ever runs, so a
    # fixture with a broken one would fail as a BROKEN FIXTURE, not as the rule firing. The guard belongs
    # here, with the other operator errors: it is about what the CALLER handed us, never about the PR.
    "[seam] --required-set must parse": ("refused", "exit 2"),
    # **THE ARGV IS THE SUBJECT HERE, AND IT HAS TO BE.** Every fixture answers from a recording and IGNORES
    # the argv it was handed, so a fetch aimed at the WRONG REPOSITORY is invisible to all of them: the
    # rollup ran `gh pr view <pr>` with no `--repo` for the life of this tool, every fixture stayed green,
    # and the flag was a lie against any repo but the one the process was standing in. A test that asserts on
    # the RESULT cannot see that. These two assert on the COMMAND.
    #
    # The first is the INVENTORY, and it is derived from the CODE (`code_argv` — every fetch `build_snapshot`
    # actually issues), so a fetcher added tomorrow is covered the day it is added, not the day someone
    # remembers to list it. The second is the DECISIVE one: a fetcher that FORGETS the repo — which is the
    # defect, reconstructed — must be REFUSED by the seam, not merely absent from a list.
    "[argv] every GitHub call is repo-scoped": ("accepted", "every GitHub call names the repo"),
    "[argv] a fetcher that forgets the repo": ("refused", "is NOT scoped to"),
    # AND THE SPOOF, which the audit of the field-shape scan's two bypasses turned up in THIS guard: it asked
    # whether the repo's name appeared ANYWHERE in the argv, so a repository named in a flag that SCOPES
    # NOTHING satisfied it — and the command still resolved against the current checkout. A guard that
    # accepts a STRING where it means a POSITION can be fed the string.
    "[argv] the repo named in a flag that scopes nothing": ("refused", "is NOT scoped to"),
    # AND THE SAME SPOOF ON THE OTHER HALF OF THE SAME GUARD, which is what the round that "fixed" the case
    # above actually left behind: it named a POSITION for `gh pr view` and went on testing ANY ARGUMENT for
    # `gh api`. So the endpoint could name `wrong/repo` while a `--template` (or a `--jq`, a `-f`, a `--field`,
    # a header — one case each, because "some flag" is how a guard gets fixed for one flag) carried the
    # repo-shaped string that satisfied the guard. Each of these WOULD HAVE QUERIED `wrong/repo`.
    "[argv] a gh api endpoint aimed at the WRONG repo": ("refused", "is NOT scoped to"),
    "[argv] a repo-shaped --jq on a wrong endpoint": ("refused", "is NOT scoped to"),
    "[argv] a repo-shaped -f field on a wrong endpoint": ("refused", "is NOT scoped to"),
    "[argv] a repo-shaped --field on a wrong endpoint": ("refused", "is NOT scoped to"),
    "[argv] a repo-shaped header on a wrong endpoint": ("refused", "is NOT scoped to"),
    # A FLAG BEFORE THE ENDPOINT THAT MIGHT EAT IT: `gh api --template <x> repos/wrong/…` — the word after
    # `--template` is its VALUE, not the path, and a parser that guessed would read the repo-shaped value as
    # the endpoint. The position is then NOT IDENTIFIABLE, and an unidentifiable position FAILS CLOSED.
    "[argv] an unknown flag ahead of the endpoint": ("refused", "is NOT scoped to"),
    # A `--repo` IS NO DEFENCE FOR A `gh api`: the subcommand decides the form. `gh api` has no `--repo` — it
    # would be a flag the command ignores — so the endpoint alone scopes it, and this endpoint is wrong.
    "[argv] gh api with a right --repo and a wrong endpoint": ("refused", "is NOT scoped to"),
    # **AND THE MIRROR, OR THE FIX IS "REFUSE EVERYTHING".** A CORRECTLY scoped endpoint that happens to carry
    # a repo-shaped string in a flag as well is a perfectly good fetch, and must be ACCEPTED. A guard is only
    # honest if it can still say yes.
    "[argv] a right endpoint with repo-shaped junk elsewhere": ("accepted", "repos/o/r/commits/"),
    # **THE SAME TWO QUESTIONS, ABOUT FIELD READS.** The argv pair above proves a fetcher cannot ask the
    # WRONG REPOSITORY; the cases below prove it cannot read a field WITHOUT SAYING WHAT IT EXPECTS — the
    # defect that shipped `page.get(rows_key) or []`, which read a MISSING row array as an EMPTY one and
    # returned GREEN. The first is the INVENTORY, over the code that RUNS (`check_field_shapes` AST-scans
    # this file). The rest are the DECISIVE ones: a reader that FORGETS, reconstructed in every spelling the
    # hole has ever had — and each must be REFUSED BY NAME, not merely absent from a list.
    #
    # **EVERY `[shape]` CASE BELOW THAT IS NOT THE INVENTORY IS A REGRESSION CASE FOR A BYPASS A REVIEWER
    # ACTUALLY DROVE THROUGH THIS SCAN.** None is hypothetical. `pages[0][rows_key]` in `read_pages` and
    # `facts = {}` / `facts["statuses"]` in `fetch_statuses` each left the entire suite GREEN; a brand-new
    # fetcher was not scanned at all; and the LAUNDERED HAND-BACKS (below) put a raw response into
    # `build_snapshot`'s hands, where it was read with a subscript that no rule in the file could see. Delete
    # any one of these cases and that bypass is legal again. (They are named, never numbered: "the last
    # three" was true of this comment once, and cases get added.)
    "[shape] every field read declares its shape": ("accepted", "every field read declares its shape"),
    "[shape] a raw .get() in a reader": ("refused", "raw `.get(…)`"),
    "[shape] a subscript with a LITERAL key": ("refused", "`page['check_runs']` with a raw SUBSCRIPT"),
    "[shape] a subscript with a VARIABLE key": ("refused", "`pages[0][rows_key]` with a raw SUBSCRIPT"),
    "[shape] a subscript on a SPOOFED local": ("refused", "`facts['statuses']` with a raw SUBSCRIPT"),
    "[shape] a field() call with NO shape": ("refused", "calls field() with NO SHAPE"),
    "[shape] a NEW fetcher that declares nothing": ("refused", "fetch_deployments"),
    "[shape] build_snapshot reads a response itself": ("refused", "calls the `fetch` seam DIRECTLY"),
    # THE HAND-BACK, BARE: the fetcher READS NOTHING and returns the response, so every read rule above has
    # nothing to fire on in the fetcher itself.
    "[shape] a fetcher that HANDS BACK the response": ("refused", "HANDS THE RAW RESPONSE BACK"),
    "[shape] a fetcher that hands back a NAME for it": ("refused", "HANDS THE RAW RESPONSE BACK"),
    # **AND THE HAND-BACK, LAUNDERED — THE TWO BYPASSES A REVIEWER DROVE THROUGH THE RULE ABOVE, PLUS THE
    # SHAPES THE NEXT ONE WOULD HAVE BROUGHT.** None of these is a bare return, so `fetchers-never-return-a-
    # response` does not fire on ANY of them and never will: the response is wrapped in a dict, passed through
    # a helper, put in a list, rebuilt by a comprehension. Under the scan as it shipped one round ago, every
    # one of these left the suite GREEN while `build_snapshot` read raw GitHub fields off the result.
    #
    # WHAT REFUSES THEM NOW IS THAT THE READ HAS NOWHERE TO HAPPEN. `build_snapshot` — and every helper it
    # calls — is scanned, so the read is NAMED, wherever the response got to. That is the guard asserting what
    # it MEANS instead of one spelling of it, and it is why these cases name a READ and not a hand-back: the
    # hand-back is not the defect, the read is, and a hand-back nobody can read is inert.
    "[shape] a WRAPPED response, read in build_snapshot": ("refused", "`box['raw']` with a raw SUBSCRIPT"),
    "[shape] a LAUNDERED response, read in build_snapshot": ("refused", "`data.get('check_runs')` with a raw"),
    "[shape] a LISTED response, read in build_snapshot": ("refused", "`page['check_runs']` with a raw SUBSCRIPT"),
    "[shape] a COMPREHENDED response, read in build_snapshot": ("refused", "`page.get('check_runs')` with a raw"),
    # AND THE READ ONE FUNCTION FURTHER OUT: a helper `build_snapshot` calls that is NOT handed the seam is in
    # no reader set either — the closure is what reaches it.
    "[shape] a HELPER of build_snapshot reads it": ("refused", "unpack:"),
    # AND THE CLI, which is the last place a response-derived value gets to. "No UNSCANNED code reads a field"
    # has to include `main`, or the boundary is an exemption wearing a boundary's clothes.
    "[shape] main reads the result raw": ("refused", "main:"),
    "[shape] a source in which NOTHING fetches": ("refused", "found NO FETCHER"),
    # And the RUN-TIME backstop behind the scan: a read that reaches `field()` with no shape at all refuses
    # there too. The scan is static and covers the readers it DERIVES; this covers the call itself, wherever
    # it is — including a caller the scan does not reach at all.
    "[shape] a field read that declares NO shape": ("refused", "DECLARES NO SHAPE"),
}

# THE READER THAT FORGETS — the defect itself, in every spelling it has ever been written in, and the last
# three are the ones that BEAT the first version of this scan.
#
# **INVENTED SOURCE, ALL OF IT.** These strings are parsed, never executed, and they exist NOWHERE ELSE in
# this tree — so a sweeper who greps for one lands here and nowhere else, and never on a live line.
#
# Each is a whole (tiny) module, because the scan DERIVES its subject from `build_snapshot`: the reader set
# is what that function hands the `fetch` seam to. That is what makes the NEW-FETCHER case catchable at all
# — under the hand-written reader list it replaced, `fetch_deployments` was simply not scanned.
def forgetful(body: str, drives: str = "fetch_check_runs(fetch, head_sha)") -> str:
    return f"def build_snapshot(fetch, repo, pr, head_sha):\n    return {drives}\n\n{body}"


FORGETFUL_READERS = {
    "[shape] a raw .get() in a reader": forgetful("""
def fetch_check_runs(fetch, head_sha):
    pages = fetch("check-runs", [])
    return [row for page in pages for row in page.get("check_runs") or []]
"""),
    "[shape] a subscript with a LITERAL key": forgetful("""
def fetch_check_runs(fetch, head_sha):
    pages = fetch("check-runs", [])
    return [row for page in pages for row in page["check_runs"]]
"""),
    # BYPASS ONE, verbatim: the key is a VARIABLE, so the scan that only knew string literals never looked.
    "[shape] a subscript with a VARIABLE key": forgetful("""
def fetch_check_runs(fetch, head_sha):
    return read_pages(fetch, "check-runs", [], "check_runs")

def read_pages(fetch, source, argv, rows_key):
    pages = fetch(source, argv)
    return pages[0][rows_key]
"""),
    # BYPASS TWO, verbatim: a local named `facts` was EXEMPT, whatever it actually held.
    "[shape] a subscript on a SPOOFED local": forgetful("""
def fetch_statuses(fetch, head_sha):
    facts = fetch("status", [])
    return facts["statuses"]
""", drives="fetch_statuses(fetch, head_sha)"),
    "[shape] a field() call with NO shape": forgetful("""
def fetch_check_runs(fetch, head_sha):
    page = fetch("check-runs", [])
    return field("check-runs", page, "check_runs")
"""),
    # THE FETCHER THAT READS NOTHING AT ALL. There is no forgetful READ here to catch — the response is
    # handed back whole. This is the case the NARROW hand-back rule exists for: it names the mistake in the
    # fetcher that made it. (It is NOT what makes a hand-back safe — see the LAUNDERED cases below, which
    # this rule does not fire on at all.)
    "[shape] a fetcher that HANDS BACK the response": forgetful("""
def fetch_check_runs(fetch, head_sha):
    return fetch("check-runs", [])
"""),
    # The same thing, one name along — the response bound to a local and returned. A rule that knew only the
    # bare call would be the guard that gets bypassed by moving the read one character sideways, again.
    "[shape] a fetcher that hands back a NAME for it": forgetful("""
def fetch_check_runs(fetch, head_sha):
    pages = fetch("check-runs", [])
    return pages, {"row": "source"}
"""),
    # THE HOLE THE OLD SCAN DISCLOSED INSTEAD OF CLOSING: a fetcher nobody added to the list was not scanned.
    # The reader set is DERIVED now, so this one is scanned the moment `build_snapshot` drives it.
    "[shape] a NEW fetcher that declares nothing": forgetful("""
def fetch_deployments(fetch, head_sha):
    data = fetch("deployments", [])
    return data["deployments"]
""", drives="fetch_deployments(fetch, head_sha)"),
    # AND THE ORCHESTRATOR ITSELF: inline a fetch into `build_snapshot` and no fetcher is involved at all. Its
    # fetcher is IMPECCABLE here — it reads through the door, with a shape — so the only thing wrong with this
    # source is the response `build_snapshot` fetches and reads itself. Two rules now name it (the seam call
    # does not belong there, AND the read is refused like any other); the case pins the first by needle.
    "[shape] build_snapshot reads a response itself": """
def build_snapshot(fetch, repo, pr, head_sha):
    rows = fetch_check_runs(fetch, head_sha)
    data = fetch("check-runs", [])
    return rows, data.get("check_runs")

def fetch_check_runs(fetch, head_sha):
    pages = fetch("check-runs", [])
    return [row for page in pages for row in field("check-runs", page, "check_runs", list)]
""",
    # --- THE LAUNDERED HAND-BACKS: the two bypasses, and the family they belong to --------------------
    # In every one of these the fetcher's OWN body is impeccable — it reads nothing, so no read rule fires on
    # it, and it does not RETURN the seam's value bare, so the hand-back rule does not fire either. The
    # response arrives in `build_snapshot` all the same, and the RAW READ THERE is what is refused. Change the
    # laundering to any other shape and the case still fails, at the same line: the read is the invariant.
    "[shape] a WRAPPED response, read in build_snapshot": """
def build_snapshot(fetch, repo, pr, head_sha):
    box = fetch_check_runs(fetch, head_sha)
    return [row for row in box["raw"]["check_runs"]], {}

def fetch_check_runs(fetch, head_sha):
    data = fetch("check-runs", [])
    return {"raw": data}
""",
    "[shape] a LAUNDERED response, read in build_snapshot": """
def build_snapshot(fetch, repo, pr, head_sha):
    data = fetch_check_runs(fetch, head_sha)
    return data.get("check_runs") or [], {}

def fetch_check_runs(fetch, head_sha):
    data = fetch("check-runs", [])
    return identity(data)

def identity(x):
    return x
""",
    "[shape] a LISTED response, read in build_snapshot": """
def build_snapshot(fetch, repo, pr, head_sha):
    pages = fetch_check_runs(fetch, head_sha)
    return [row for page in pages for row in page["check_runs"]], {}

def fetch_check_runs(fetch, head_sha):
    data = fetch("check-runs", [])
    return [data]
""",
    "[shape] a COMPREHENDED response, read in build_snapshot": """
def build_snapshot(fetch, repo, pr, head_sha):
    pages = fetch_check_runs(fetch, head_sha)
    return [row for page in pages for row in page.get("check_runs") or []], {}

def fetch_check_runs(fetch, head_sha):
    data = fetch("check-runs", [])
    return [p for p in data]
""",
    # AND ONE FUNCTION FURTHER OUT AGAIN. `unpack` is handed no seam, so it is in NO reader set — but
    # `build_snapshot` calls it, so the closure reaches it, and its raw read is refused BY NAME.
    "[shape] a HELPER of build_snapshot reads it": """
def build_snapshot(fetch, repo, pr, head_sha):
    box = fetch_check_runs(fetch, head_sha)
    return unpack(box), {}

def unpack(box):
    return box["raw"]["check_runs"]

def fetch_check_runs(fetch, head_sha):
    data = fetch("check-runs", [])
    return {"raw": data}
""",
    # AND THE CLI BOUNDARY. `main` is not in any closure — it is the caller of the producer, not its callee —
    # so it is scanned BY NAME (`SCANNED_BOUNDARY`), and its reads are refused like anyone else's. Here it
    # digs a GitHub field out of the result it was handed; the fetcher below is impeccable.
    "[shape] main reads the result raw": """
def build_snapshot(fetch, repo, pr, head_sha):
    return fetch_check_runs(fetch, head_sha)

def fetch_check_runs(fetch, head_sha):
    pages = fetch("check-runs", [])
    return [row for page in pages for row in field("check-runs", page, "check_runs", list)]

def main():
    out = derive(gh_fetch, "o/r", "1", "abc", "/tmp", None)
    return 0 if out["verdict"] == "green" else 1
""",
    # A SOURCE IN WHICH NOTHING FETCHES: the reader set is EMPTY, so the scan has nothing to scan — and a
    # scan with no subject would otherwise pass every source on Earth, including the ones above.
    "[shape] a source in which NOTHING fetches": """
def build_snapshot(fetch, repo, pr, head_sha):
    return [], {}
""",
}


def seam_cases(tmp: Path) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}

    def case(name: str, fn: Callable[[], object]) -> None:
        # TWO CASES UNDER ONE NAME IS ONE CASE NOBODY ASSERTS ON. The results are a dict keyed by name, so the
        # second would overwrite the first — and `check_seams`, which reconciles the NAMES both ways, would see
        # one name, one expectation, and report health for a case whose result it had thrown away. That is the
        # very shape this file keeps finding, so it is refused mechanically rather than watched for.
        if name in out:
            raise DocError(
                f"{name}: TWO seam cases share this name — the second overwrites the first, and the suite then "
                f"asserts on ONE of them while REPORTING both. Every case is named exactly once."
            )
        # `fail()` PRINTS to stderr before it exits, and these cases fire it ON PURPOSE, once per mutant —
        # so its output is captured here rather than smeared across the report. The suppression is scoped to
        # the case: nothing else in this file writes to stderr, and swallowing a REAL diagnostic would be
        # exactly the kind of quiet this tool exists to refuse.
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                out[name] = ("accepted", repr(fn()))
            except (FetchError, DocError) as exc:
                out[name] = ("refused", str(exc))
            except SystemExit as exc:
                out[name] = ("refused", f"exit {exc.code}")
            except Exception as exc:  # noqa: BLE001 - a CRASH is not a REFUSAL: no verdict was ever reached
                out[name] = (f"crash:{type(exc).__name__}", str(exc))

    py = sys.executable
    case("[seam] a dead gh is a failed fetch",
         lambda: gh_fetch("check-runs", [py, "-c", "import sys; print('[]'); sys.exit(1)"]))
    case("[seam] gh stdout that is not JSON",
         lambda: gh_fetch("check-runs", [py, "-c", "print('<html>rate limited</html>')"]))
    case("[seam] --head-sha must be an oid", lambda: check_head_sha("HEAD"))
    case("[seam] --rundir must exist", lambda: check_rundir(tmp / "no-such-dir"))
    # A spec that is neither `none`, `unknown`, nor `declared:<json>`. The one answer that must NEVER come
    # back is a RequiredSet — degrading an unreadable spec to "nothing is required" is the false green the
    # required set exists to close, rebuilt inside its own parser's caller.
    case("[seam] --required-set must parse", lambda: check_required_set("build,test"))
    # THE ARGV OF EVERY GitHub CALL, taken from the running code (`code_argv`) — never a list written here.
    case("[argv] every GitHub call is repo-scoped", check_repo_scoping)
    # AND THE ONE THAT PROVES THE CLASS IS CLOSED: a fetcher that forgets the repo, driven through the SAME
    # seam every real fetcher goes through. It is the rollup's old argv, verbatim (`gh pr view <pr> --json
    # …`), which is what makes this the defect itself rather than a model of it — and it must be REFUSED, not
    # merely uncovered. If this case can be `accepted`, a new fetcher can silently query the wrong repo.
    case("[argv] a fetcher that forgets the repo",
         lambda: repo_scoped(lambda _s, argv: argv, "o/r")(
             "a-new-fetcher", ["gh", "pr", "view", "35", "--json", "statusCheckRollup,headRefOid"]))
    # THE SPOOF: the repository IS in the argv — in a `--template`, where it scopes nothing. The command
    # still resolves in the current checkout. Named as a POSITION, not a substring, this is refused.
    case("[argv] the repo named in a flag that scopes nothing",
         lambda: repo_scoped(lambda _s, argv: argv, "o/r")(
             "a-new-fetcher", ["gh", "pr", "view", "35", "--json", "statusCheckRollup,headRefOid",
                               "--template", "repos/o/r/x"]))
    # THE SAME SPOOF ON THE `gh api` HALF — the half the round that wrote the case above left testing ANY
    # argument. The ENDPOINT names `wrong/repo`; the repo-shaped string sits in a flag that scopes nothing,
    # once per flag a fetcher might plausibly reach for. `gh` would query `wrong/repo` for every one of them.
    def api_call(*extra: str) -> object:
        return repo_scoped(lambda _s, argv: argv, "o/r")(
            "a-new-fetcher", ["gh", "api", "--paginate", "--slurp",
                              "repos/wrong/repo/commits/abc/check-runs", *extra])

    case("[argv] a gh api endpoint aimed at the WRONG repo", lambda: api_call("--template", "repos/o/r/x"))
    case("[argv] a repo-shaped --jq on a wrong endpoint", lambda: api_call("--jq", "repos/o/r/x"))
    case("[argv] a repo-shaped -f field on a wrong endpoint", lambda: api_call("-f", "repos/o/r/x"))
    case("[argv] a repo-shaped --field on a wrong endpoint", lambda: api_call("--field", "repos/o/r/x"))
    case("[argv] a repo-shaped header on a wrong endpoint",
         lambda: api_call("-H", "X-Repo: repos/o/r/x"))
    # The endpoint's POSITION cannot be identified past a flag that may take a value — so it fails closed.
    case("[argv] an unknown flag ahead of the endpoint",
         lambda: repo_scoped(lambda _s, argv: argv, "o/r")(
             "a-new-fetcher", ["gh", "api", "--template", "repos/o/r/x",
                               "repos/wrong/repo/commits/abc/check-runs"]))
    # A `--repo` does not rescue a `gh api` whose ENDPOINT is wrong: the subcommand decides the form.
    case("[argv] gh api with a right --repo and a wrong endpoint",
         lambda: api_call("--repo", "o/r"))
    # AND THE ACCEPTANCE: the endpoint IS this repo, and a repo-shaped string elsewhere changes nothing. If
    # this case ever goes `refused`, the guard was "fixed" by refusing everything, which fixes nothing.
    case("[argv] a right endpoint with repo-shaped junk elsewhere",
         lambda: repo_scoped(lambda _s, argv: argv, "o/r")(
             "a-new-fetcher", ["gh", "api", "--paginate", "--slurp",
                               "repos/o/r/commits/abc/check-runs", "--jq", "repos/o/r/x",
                               "-H", "X-Repo: repos/other/repo/x"]))
    # THE FIELD-READ SEAM: the inventory over the code that RUNS, and then the reader that forgets — in
    # every spelling, INCLUDING the two that beat the first version of this scan (a variable-key subscript,
    # and a local spoofing the name the scan used to exempt).
    case("[shape] every field read declares its shape", check_field_shapes)
    for shape_case, forgetful_source in FORGETFUL_READERS.items():
        case(shape_case, lambda src=forgetful_source: check_field_shapes(src))
    case("[shape] a field read that declares NO shape",
         lambda: field("check-runs", {"check_runs": []}, "check_runs"))
    doc_cases(tmp, case)  # the alarm's OWN guards — see DOC_EXPECT
    return out


# --- the DOC-CHECK'S OWN GUARDS: an alarm whose SUBJECT can go missing ----------------------------
#
# **`doc-check` IS THE THING THAT STOPS THE DOC AND THE CODE DRIFTING APART — AND NOTHING CHECKED *IT*.**
# Its guards all say the same sentence, which is this whole file's sentence: *a check that cannot find its
# subject must FAIL, never pass.* But they were reachable only from a BROKEN DOC, and every doc in the tree
# is intact — so no case in the suite ever executed one. Deleted alone, the suite AND the matrix stayed
# green. That is the third time this shape has been found here, and this time it was inside the alarm.
#
# One of them was not decoration. Weaken `doc-fetch-spec-complete` and a doc that has LOST AN ENTIRE `gh`
# COMMAND passes: the spec is then executed against whichever commands survive, and `doc-check` prints `ok`
# for a fetch it never compared. The others are message specialisations (the comparison behind each one
# still fails, more confusingly) — MEASURED, not assumed, by deleting each and running a broken doc through.
#
# The broken docs are BUILT HERE, from the REAL doc, by REMOVING THE ONE THING each guard exists to notice.
# They are never written to the tree: a doc file that is deliberately corrupt is a doc somebody will read.
DOC_EXPECT = {
    "[doc] the doc itself is GONE": ("refused", "cannot find its subject"),
    "[doc] the enum block is GONE": ("refused", "enum block is GONE"),
    "[doc] the CLASSIFY tables are GONE": ("refused", "no CLASSIFY rules parsed"),
    "[doc] the CLASSIFY catch-all is GONE": ("refused", "catch-all"),
    "[doc] the DECIDE section is GONE": ("refused", "#### DECIDE"),
    "[doc] the DECIDE section lists no outcomes": ("refused", "ZERO outcome bullets"),
    "[doc] a FETCH command is MISSING": ("refused", "check-runs"),
    "[doc] TWO fetch commands for one source": ("refused", "TWO fetch commands"),
    "[doc] the rollup fetch drops --repo": ("refused", "names NO REPOSITORY"),
    "[doc] a WRAPPED rollup fetch drops --repo": ("refused", "names NO REPOSITORY"),
    "[doc] a derive copy drops --required-set": ("refused", "WITHOUT `--required-set`"),
    "[doc] NO copy of the derive command": ("refused", "ZERO copies"),
    # The one class of rule `doc-check` CANNOT execute against the doc, so the doc's own PROSE is the
    # subject — and prose is what rots. Delete the paragraph and nothing else in this file notices.
    "[doc] a CROSS-SOURCE rule is GONE": ("refused", "MISSING FROM THE DOC"),
}


def doc_cases(tmp: Path, case: Callable[[str, Callable[[], object]], None]) -> None:
    """Drive each `doc-check` guard against a doc BROKEN in exactly the way that guard exists to notice.

    `case` is the same recorder the seams use, so a guard that RAISES is `refused`, one that returns is
    `accepted`, and one that blows up is a `crash` — three outcomes, never conflated.
    """
    text = DOC.read_text(encoding="utf-8")

    def whole_check(path: Path) -> object:
        """`doc_check` RETURNS a code, it does not raise — so a non-zero return IS its refusal and must be
        recorded as one. Returning 0 for a doc that is not there is the ACCEPTANCE this must never allow."""
        with contextlib.redirect_stdout(io.StringIO()):
            rc = doc_check(path)
        if rc != 0:
            raise DocError("doc-check FAILED — a check that cannot find its subject NEVER passes")
        return "doc-check PASSED on a doc that is NOT THERE"

    case("[doc] the doc itself is GONE", lambda: whole_check(tmp / "no-such-doc.md"))
    # The enum block is found BY NAME, so renaming the enum IS "the block is gone or renamed". The new name
    # must not CONTAIN the old one — `CheckStatusStateZ` still matches the `in block` test, and the first
    # draft of this case renamed it that way, "broke" nothing, and was caught by its own assertion.
    case("[doc] the enum block is GONE",
         lambda: parse_enums(fenced_blocks(text.replace("CheckStatusState", "CheckRunState"))))
    # The CLASSIFY tables are found by their `->` arrows: break the arrows and the tables parse to NOTHING.
    case("[doc] the CLASSIFY tables are GONE",
         lambda: parse_classify(fenced_blocks(text.replace("-> RUNNING", "~> RUNNING")
                                                  .replace("-> PASS", "~> PASS"))))
    case("[doc] the CLASSIFY catch-all is GONE",
         lambda: parse_classify(fenced_blocks(text.replace("ANY OTHER VALUE", "SOME OTHER VALUE"))))
    case("[doc] the DECIDE section is GONE",
         lambda: parse_decide_order(text.replace("#### DECIDE", "#### HOW TO DECIDE")))
    case("[doc] the DECIDE section lists no outcomes",
         lambda: parse_decide_order(re.sub(r"^- \*\*", "- __", text, flags=re.MULTILINE)))
    # THE LOAD-BEARING ONE: a doc that has lost an entire fetch command. Nothing else notices.
    case("[doc] a FETCH command is MISSING",
         lambda: parse_fetch_spec(text.replace(
             'gh api --paginate --slurp "repos/<owner>/<repo>/commits/<head_sha>/check-runs" | jq -c \'',
             "# (the check-runs fetch: DELETED by this case)\n(", 1)))
    # A SECOND copy of a command is where drift hides: `doc-check` executes one, the reader follows the
    # other. The duplicate is APPENDED, so the spec block itself is left exactly as it is.
    case("[doc] TWO fetch commands for one source",
         lambda: parse_fetch_spec(text + '\n```sh\ngh api --paginate --slurp '
                                         '"repos/<owner>/<repo>/commits/<head_sha>/check-runs" | jq -c \'.\'\n```\n'))
    # A ROLLUP COMMAND WITH NO REPOSITORY — the code's defect, in the doc's copy of it, and the reason this
    # guard exists at all: the tool and the doc BOTH ran `gh pr view <pr>` bare, and both therefore asked the
    # checkout the reader was standing in. Every OTHER doc guard passes on this text (the enums, the CLASSIFY
    # tables, the DECIDE order and every `jq` filter are untouched) — so nothing else can notice it.
    def invocations(t: str) -> object:
        problems = check_gh_invocations(t, code_argv())
        if problems:
            raise DocError(problems[0])
        return "every gh invocation in the doc is fine"

    case("[doc] the rollup fetch drops --repo",
         lambda: invocations(text.replace(" --repo <owner>/<repo>", "")))
    # THE SAME BAD COPY, WRAPPED OVER TWO LINES — which is how a reader actually writes a long `gh` command,
    # and how this guard was escaped: every test in it is a substring test, and the flags had moved to the
    # continuation line. The command is the subject, not the line.
    case("[doc] a WRAPPED rollup fetch drops --repo",
         lambda: invocations(text.replace(
             "gh pr view <pr> --repo <owner>/<repo> --json statusCheckRollup,headRefOid",
             "gh pr view <pr> \\\n     --json statusCheckRollup,headRefOid")))

    # THE DERIVE-COMMAND SWEEP, against a doc tree built HERE — the real one is correct, so nothing else can
    # ever execute these two guards. The bad copy is INVENTED and lives only in `tmp`: a stale command
    # written into the tree is a command somebody follows.
    def derive_tree(name: str, body: str) -> object:
        root = tmp / name
        root.mkdir(parents=True, exist_ok=True)
        (root / "some-doc.md").write_text(body, encoding="utf-8")
        problems, _copies = check_derive_copies(root)
        if problems:
            raise DocError(problems[0])
        return "the sweep found nothing to complain about"

    case("[doc] a derive copy drops --required-set",
         lambda: derive_tree("bad-copy", "Run `ci-status.py derive --pr 7 --head-sha <sha> --rundir <d>`.\n"))
    case("[doc] NO copy of the derive command",
         lambda: derive_tree("no-copy", "This doc prescribes nothing at all.\n"))
    # A doc that has LOST a cross-source rule. Every OTHER doc guard would still pass on it — the enums, the
    # CLASSIFY tables, the DECIDE order and every executable fetch filter are untouched — which is exactly
    # why this one has to exist: the rules that cannot be executed are the rules that can be deleted quietly.
    case("[doc] a CROSS-SOURCE rule is GONE",
         lambda: check_cross_source_stated(
             text.replace(SPEC_CANNOT_EXPRESS["the two sources must AGREE"]["doc"], "(deleted by this case)")))


def check_seams(tmp: Path) -> list[str]:
    """Every case RAN and every case is EXPECTED — reconciled BOTH ways.

    A case with no expectation is a case nobody asserts anything about: it runs, it can return whatever it
    likes, and the suite reports health it never measured. That is the SAME defect this file keeps finding
    (the audit that followed the field-shape scan's two bypasses found it here), so the reconciliation is
    mechanical, not a habit. It is HARNESS scaffolding — it guards the SUITE, not a PR — which is why it
    carries no `# MUTATE` marker, for the reason `RULES` names for `run_fixture`'s refusals.
    """
    bad = []
    got = seam_cases(tmp)
    expected = {**SEAM_EXPECT, **DOC_EXPECT}
    for name in sorted(set(got) - set(expected)):
        bad.append(f"{name}: this case RAN and NOTHING EXPECTS it — a case no table asserts on is a case "
                   f"that cannot fail, and a suite that runs it reports health it never measured")
    for name in sorted(set(expected) - set(got)):
        bad.append(f"{name}: this case is EXPECTED and never RAN — an expectation with no case is an "
                   f"assertion about nothing")
    for name, (want, needle) in expected.items():
        if name not in got:
            continue
        verdict, detail = got[name]
        if verdict != want:
            bad.append(f"{name}: {verdict!r}, expected {want!r} — {detail}")
        elif needle not in detail:
            bad.append(f"{name}: right outcome, WRONG RULE: {needle!r} not in {detail!r}")
    return bad


def self_test(tmp: Path) -> int:
    failures = 0
    # **THE STATIC SCAN RUNS BEFORE ANY FIXTURE, AND THE ORDER IS THE POINT.** A field read that forgets its
    # shape can CRASH a fixture (a raw subscript raises KeyError) before a single guard has said a word — and
    # then the suite is red for the wrong reason, with a traceback where a NAMED defect belonged. Ask the
    # question that is true of the SOURCE first, and the answer names the line. (It is also a `[shape]` seam
    # case: that is what the MUTATION matrix kills the rule with. This call is what makes the failure legible.)
    try:
        print(f"ok       {'every field read':32} -> {check_field_shapes()}")
    except FetchError as exc:
        print(f"FAIL     {exc}")
        return 1

    names = cases()
    if not names:
        print(f"FAIL     no fixtures in {FIXTURES} — a suite with nothing in it passes VACUOUSLY")
        return 1
    for name in names:
        fx, got = run_fixture(name, tmp)
        bad = check_fixture(name, got, fx)
        if not bad:
            print(f"ok       {name:32} -> {got['verdict']:14} ci={got['ci']:8} ({fx['why']})")
        else:
            failures += 1
            for b in bad:
                print(f"FAIL     {name:32} {b}")

    problems = check_seams(tmp)
    for problem in problems:
        failures += 1
        print(f"FAIL     {problem}")
    if not problems:
        print(f"ok       {'the seams no fixture reaches':32} -> {len(SEAM_EXPECT)} cases: gh_fetch's own two "
              f"rules, and the CLI's operator-error guards")
        print(f"ok       {'the doc-check guards':32} -> {len(DOC_EXPECT)} cases: a BROKEN doc, one break per "
              f"guard — an alarm that cannot find its subject must never report health")
    print()
    print(f"--- doc-check: {DOC.name} vs the code that runs ---")
    failures += doc_check(DOC)
    if failures:
        print(f"\n{failures} check(s) FAILED.")
        return 1
    print(f"\nall {len(names)} fixtures hold, and the doc agrees with the code.")
    print("`--mutants` is what proves each fixture pins its OWN rule. Run it too.")
    return 0


# --- the mutation matrix: which of THIS script's rules is pinned by NO fixture? --------------------
#
# The hooks `mutate-ci-snapshot.py` calls. Same method, same `# MUTATE:<rule>:<weakening>` markers: remove
# each rule in turn, re-run every fixture, and FAIL if nothing notices. A rule no fixture notices is a rule
# whose deletion leaves the suite GREEN while the tool has quietly stopped checking — worse than a missing
# fixture, because it LIES.

MUTATION_RULES = RULES  # the declared inventory; reconciled against the markers, BOTH directions

# THE GREEN CANARY IS OFF FOR THIS SCRIPT, and the reason is the difference between a verifier and a
# producer. `mutate-ci-snapshot.py` asserts that removing a rule can NEVER make a green fixture go
# non-green: for a VERIFIER that is sound — deleting a check can only be more PERMISSIVE — so a green
# fixture that moves means the mutation itself was bogus. **A PRODUCER INVERTS THAT.** Removing one of its
# rules CORRUPTS THE ARTIFACT (a marker whose sha it may not carry, a family it never fetched), and the
# verifier downstream then REFUSES it. `green.json` going `unusable` under such a mutant is not a broken
# mutation — it is the fixture NOTICING, which is exactly what is being asked. So green-expecting fixtures
# are killers here like any other, and the canary that would have called them harness bugs is disabled.
MUTATION_GREEN_CANARY = False


def mutation_expectations() -> dict[str, tuple[str, str]]:
    """Every case the harness mutates against — the fixtures, the seams they cannot reach, AND the broken
    docs that are the only way to execute `doc-check`'s own guards.

    THE SEAM AND DOC CASES BELONG HERE OR THEY PIN NOTHING. The harness only ever asks "did any CASE
    notice?", so a rule whose only witness is not in this dict is a rule reported PINNED BY NOTHING — which
    is precisely what `gh_fetch`'s two rules were, for as long as the only cases were fixtures, and what
    every `doc-check` guard was until a broken doc was constructed to run them against.
    """
    out = {name: (fx["expect"]["verdict"], fx["expect"]["needle"])
           for name, fx in ((n, json.loads((FIXTURES / n).read_text(encoding="utf-8"))) for n in cases())}
    out.update(SEAM_EXPECT)
    out.update(DOC_EXPECT)
    return out


def mutation_run() -> dict[str, tuple[str, str]]:
    """Every fixture against the (possibly mutated) module, as (verdict, reason) for the harness.

    A fixture asserts MORE than a verdict — the ledger `ci` it maps to, the notes it must disclose, whether
    an artifact was promoted at all. A mutant can break any of those while leaving the verdict alone, and
    the harness only ever compares (verdict, reason) — so such a break would be INVISIBLE to it, and the
    rule would be reported UNPINNED while a fixture was, in fact, catching it. So when the verdict comes out
    as expected but some OTHER assertion deviates, that deviation is reported IN the verdict slot. The
    verdict is passed through untouched whenever it differs, because the harness's kill STRENGTH is computed
    from it — overwriting a mutant's `green` here would downgrade the loudest kill there is.
    """
    import tempfile
    want = mutation_expectations()
    out: dict[str, tuple[str, str]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for name in cases():
            try:
                fx, got = run_fixture(name, Path(tmp))
            except SystemExit as exc:  # `fail()` — refusing to emit a verdict IS a deviation
                out[name] = (f"crash:SystemExit({exc.code})", "the tool refused to emit a verdict")
                continue
            except Exception as exc:  # noqa: BLE001 - a crash IS the result here, and it is NOT a verdict
                out[name] = (f"crash:{type(exc).__name__}", str(exc))
                continue
            if got["verdict"] != want[name][0]:
                out[name] = (got["verdict"], got["reason"])  # the harness reads the strength off this
                continue
            problems = check_fixture(name, got, fx)
            out[name] = (f"deviates:{problems[0]}", got["reason"]) if problems else (got["verdict"], got["reason"])
        out.update(seam_cases(Path(tmp)))  # the rules no fixture can reach — see SEAM_EXPECT
    return out


GREEN = SNAP.GREEN


def resolve_repo(fetch: Fetch) -> object:
    """WHICH REPOSITORY IS THIS CHECKOUT? The one fetch that happens outside `derive` — and it goes THROUGH
    THE DOOR, like every other read of a GitHub response.

    It is a FUNCTION, not three lines inside `main()`, for the reason everything else in this file is: the
    scan's subject is DERIVED from the call graph, and a response read in `main` would be a response read in
    a function no closure reaches. Here it is a PRODUCER ROOT, so its read is scanned like any other.

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
    # MANDATORY, AND WITH NO DEFAULT — the same rule `ci-snapshot.evaluate()` enforces on ITS callers, and
    # for the same reason: a caller who forgot to say what the base branch requires must not be handed the
    # permissive answer. It is the ledger header's value, verbatim:
    #   --required-set "$(python3 <skill>/scripts/ledger.py --file <rundir>/state.jsonl header get required_set)"
    # `unknown` is a legal value and it can NEVER go green (it is a `pending` bullet in DECIDE) — which is
    # exactly what makes a run that never performed the read merge NOTHING, instead of merging everything.
    d.add_argument("--required-set", required=True,
                   help="the ledger header's `required_set`: `declared:<json>` | `none` | `unknown`")

    c = sub.add_parser("doc-check", help="assert stage-2-ci.md agrees with the code that runs — enums, "
                                         "CLASSIFY, DECIDE order, and its fetch spec EXECUTED")
    c.add_argument("--doc", type=Path, default=DOC)

    s_ = sub.add_parser("self-test", help="run every fixture, then doc-check")
    s_.add_argument("--mutants", action="store_true",
                    help="ask which rules are pinned by NO fixture (delegates to mutate-ci-snapshot.py)")

    args = p.parse_args()

    if args.cmd == "doc-check":
        return doc_check(args.doc)

    if args.cmd == "self-test":
        if args.mutants:
            return subprocess.run(  # noqa: S603
                [sys.executable, str(HERE / "mutate-ci-snapshot.py"), "--script", str(Path(__file__))],
                check=False,
            ).returncode
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            return self_test(Path(tmp))

    # EVERY OPERATOR ERROR IS NAMED BEFORE THE FIRST FETCH. A caller's mistake surfacing later — as a crash
    # during promotion, or as a verdict about the PR — is a defect reported against the wrong thing.
    check_head_sha(args.head_sha)
    check_rundir(args.rundir)
    required = check_required_set(args.required_set)

    repo = args.repo
    if not repo:
        # THROUGH THE DOOR, in a function the scan reaches — see `resolve_repo`.
        try:
            repo = resolve_repo(gh_fetch)
        except FetchError as exc:
            fail(f"cannot determine the repo ({exc}) — pass --repo owner/name")

    out = derive(gh_fetch, repo, args.pr, args.head_sha, args.rundir, required)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    # green is the ONLY exit-0 verdict. Everything else — pending, red, unusable, an unclassified value —
    # is NOT a green, and a caller that checks only the exit status must never be told otherwise.
    #
    # THROUGH THE DOOR, and not because `out` is a GitHub response — it is the dict `result()` just built.
    # The rule is that NOTHING OUTSIDE THE DOOR READS A FIELD, in any function a response can reach, and
    # `main` is one: an exemption here (`out["verdict"]`, "it's our own dict") is exactly the exemption that
    # was spoofed twice already. A read that declares `str` costs one call and cannot become the hole.
    return 0 if field("result", out, "verdict", str) == SNAP.GREEN else 1


if __name__ == "__main__":
    raise SystemExit(main())
