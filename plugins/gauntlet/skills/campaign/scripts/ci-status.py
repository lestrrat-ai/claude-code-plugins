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

    ci-status.py derive --pr 31 --head-sha <40-hex> --rundir <rundir>

prints a verdict as JSON and exits 0 ONLY on green. Nothing here is judged by eye, and `gh pr checks` is
never read: its `--json` surface carries exactly ONE field (`bucket`) — no sha, no name, no conclusion —
so it can never say WHICH COMMIT it describes and can never be evidence. Use it to WAIT, never to decide.

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

**A check that finds nothing MUST NOT PASS.** If the doc cannot be found, or a block cannot be parsed, or
zero rules are extracted, `doc-check` FAILS. An extractor that silently matches nothing and reports success
is the false green of this whole story, one level up, in the tool written to prevent it.

  derive     fetch a PR's checks, promote the snapshot, verify it, and print the verdict as JSON
  doc-check  assert stage-2-ci.md's enums / CLASSIFY / DECIDE order agree with the code that runs
  self-test  run every fixture, assert its verdict AND the rule that produced it, then run doc-check
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
from pathlib import Path
from typing import Callable, NoReturn

HERE = Path(__file__).resolve().parent
SNAPSHOT_PY = HERE / "ci-snapshot.py"
DOC = HERE.parent / "references" / "stage-2-ci.md"
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
# greened a commit with no checks on it. It is an OPERATOR ERROR, loudly.
LEDGER_CI = {
    SNAP.GREEN: "green",
    SNAP.RED: "red",
    SNAP.PENDING: "pending",
    SNAP.UNUSABLE: "pending",
    SNAP.UNVERIFIABLE: "pending",
    SNAP.UNCLASSIFIED: "pending",
}

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
DECIDE_ORDER = ("UNUSABLE", "red", "UNKNOWN_VALUE", "pending", "pending (nothing registered)", "green")

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
RULES = {
    "evidence-sha-from-response": "a checkrun row's sha is GITHUB'S `.head_sha`, NEVER the sha we asked for",
    "status-sha-from-response": "a status row's sha is the response's own top-level `.sha`",
    "checkruns-marker-sha": "the check-runs marker's sha is GitHub's, and `-` ONLY when it returned no rows",
    "status-marker-sha": "the status marker's sha is GitHub's own — present even at ZERO statuses",
    "rollup-marker-sha": "the rollup marker's sha is ALWAYS `-`: the rollup carries no commit oid to copy",
    "both-families-checkruns": "the check-run family is FETCHED — a family never read reports nothing, and nothing parses as nothing-wrong",
    "both-families-status": "the commit-status family is FETCHED — /check-runs CANNOT SEE a failing Jenkins status",
    "rollup-witnesses": "the rollup is read for WITNESSES — with none, containment passes TRIVIALLY",
    "head-read-last": "the PR's CURRENT head is read AFTER the evidence — a head read FIRST cannot see a push that lands mid-fetch",
    "head-must-be-known": "a rollup response with NO headRefOid is a FAILED fetch — an unknown head makes the fail-closed rule below unable to fire",
    "head-moved-is-not-evidence": "a MOVED head FAILS CLOSED — evidence about a commit that is not the head is not evidence about the PR",
    "fetch-failure-is-not-evidence": "a `gh` call that FAILS yields NO verdict from evidence, and promotes NOTHING",
    "verdict-from-snapshot": "the verdict comes from ci-snapshot.evaluate() over the PROMOTED BYTES — never from what we think we fetched",
    "disclose-truncation": "an evidence count that disagrees with GitHub's own total_count is DISCLOSED, never silently dropped",
}


class OperatorError(Exception):
    """The caller asked a question that cannot be answered — never a verdict about the PR."""


class FetchError(Exception):
    """A source could not be read. The snapshot is NOT promoted and there is NO verdict from evidence."""


def fail(msg: str) -> NoReturn:
    print(f"ci-status: {msg}", file=sys.stderr)
    raise SystemExit(2)


# --- FETCH ---------------------------------------------------------------------------------------
#
# Every fetch goes through ONE seam, `Fetch = (source, argv) -> parsed JSON`, so the fixtures can drive the
# whole producer with RECORDED API RESPONSES and no network. The seam is the source of the fixtures' power:
# the code under test below is the SAME code that runs against GitHub, not a re-implementation of it.

Fetch = Callable[[str, "list[str]"], object]


def gh_fetch(source: str, argv: list[str]) -> object:
    """Run `gh` and parse its stdout as JSON.

    A NON-ZERO EXIT IS A FAILED FETCH, FULL STOP. The doc's shell version needs `set -o pipefail` to learn
    this, because a dead `gh` piped into `jq` yields an EMPTY stdin, and `jq` then prints nothing and exits
    0 — the fetch failed and the shell called it success. There is no pipeline here, and the exit status is
    checked directly, which is the same rule with nothing left to forget.
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


def fetch_check_runs(fetch: Fetch, repo: str, head_sha: str) -> tuple[list[dict], dict, int | None]:
    """(1) CHECK RUNS — pinned to <head_sha> BY THE URL. Identity AND verdict in one row.

    `--paginate` is MANDATORY (`/check-runs` pages at 30) and `--slurp` collects every page into ONE array,
    which is what lets the marker's `count` be the total ACROSS pages rather than the last page's.
    """
    pages = fetch("check-runs", [
        "gh", "api", "--paginate", "--slurp", f"repos/{repo}/commits/{head_sha}/check-runs",
    ])
    if not isinstance(pages, list):
        raise FetchError(f"check-runs: expected an array of pages from --slurp, got {type(pages).__name__}")
    runs = [r for page in pages for r in (page or {}).get("check_runs", [])]

    rows = []
    for r in runs:
        app = r.get("app") or {}
        # GITHUB'S OWN `.head_sha`, off the row it sits on — NEVER the `head_sha` we asked for. The whole
        # force of the verify rule downstream comes from these two being INDEPENDENT: the header carries
        # ours, the rows carry GitHub's, so they CAN disagree, and on a snapshot fetched for a superseded
        # commit they WILL. Interpolate our own literal here and the comparison is a copy against its own
        # source: it matches BY CONSTRUCTION, it can never fail, and the verification is deleted rather
        # than implemented. That bug has already shipped in this repo once.
        # MUTATE:evidence-sha-from-response:sha = head_sha
        sha = s(r.get("head_sha"))
        rows.append({
            "row": "checkrun",
            "sha": sha,
            "name": s(r.get("name")),
            "app_id": s(app.get("id")) if app.get("id") is not None else NO_OID,
            "status": up(r.get("status")),
            "conclusion": up(r.get("conclusion") or NO_OID),
            "id": s(r.get("details_url") or NO_OID),
        })

    # The commit oid lives ONLY on the rows here, so a fetch that returned ZERO rows has NO oid to carry and
    # its marker's sha is `-`. Inventing one would be the fabrication the contract forbids outright.
    # MUTATE:checkruns-marker-sha:marker_sha = head_sha
    marker_sha = s(runs[0].get("head_sha")) if runs else NO_OID
    marker = {"row": "source", "source": "check-runs", "sha": marker_sha, "count": str(len(rows))}

    # GitHub's OWN count of what it holds for this commit. If the paginated read collected a different
    # number of rows, EVIDENCE IS MISSING — and a missing row could be the failing one. It is DISCLOSED
    # (below), never dropped in silence.
    total = pages[0].get("total_count") if pages and isinstance(pages[0], dict) else None
    # The FAMILY IS READ, and what it returned is what goes in the artifact. A family never read reports
    # NOTHING, and "nothing" parses as "nothing wrong" — the weakening below is that family going dark.
    # MUTATE:both-families-checkruns:return [], {"row": "source", "source": "check-runs", "sha": NO_OID, "count": "0"}, None
    return rows, marker, total if isinstance(total, int) else None


def fetch_statuses(fetch: Fetch, repo: str, head_sha: str) -> tuple[list[dict], dict]:
    """(2) COMMIT STATUSES — the legacy family, which (1) CANNOT SEE.

    A failing Jenkins/CircleCI commit status is genuinely INVISIBLE to `/check-runs`. Read only one family
    and the other's failures are simply ABSENT from the evidence — and an absence parses as "nothing wrong".

    The response carries the commit ONCE, at the TOP LEVEL, and carries it EVEN WHEN `.statuses` IS EMPTY.
    That is what lets the marker PROVE a zero-status commit: `{"source":"status","sha":"<GitHub's>",
    "count":"0"}` says *we asked THIS COMMIT, and it has none* — a FACT, where an absent section says
    nothing at all.

    **NEVER read this response's own `.state` as a verdict.** A commit carrying ZERO statuses reports
    `{"state":"pending","total_count":0}` — verified live against this repo on a commit whose checks had all
    passed. An absence read as a verdict is a lie in both directions, so `.state` is not read here at all.
    """
    pages = fetch("status", [
        "gh", "api", "--paginate", "--slurp", f"repos/{repo}/commits/{head_sha}/status",
    ])
    if not isinstance(pages, list):
        raise FetchError(f"status: expected an array of pages from --slurp, got {type(pages).__name__}")
    statuses = [st for page in pages for st in (page or {}).get("statuses", [])]

    # MUTATE:status-sha-from-response:sha = head_sha
    sha = s(pages[0].get("sha")) if pages and isinstance(pages[0], dict) else None
    rows = [
        {"row": "status", "sha": sha, "context": s(st.get("context")), "state": up(st.get("state"))}
        for st in statuses
    ]
    # ALWAYS GitHub's, never `-`: a `-` here did not come from the response, and a marker whose sha is not
    # GitHub's cannot disagree with the ledger — so it could never fail. That is a rubber stamp.
    # MUTATE:status-marker-sha:marker_sha = NO_OID
    marker_sha = sha if sha is not None else NO_OID
    # THE FAMILY /check-runs CANNOT SEE. The weakening below is this family never being read — and it is
    # SELF-STAMPED on purpose, so that what kills it is the MISSING JENKINS FAILURE and not the marker rule.
    # MUTATE:both-families-status:return [], {"row": "source", "source": "status", "sha": head_sha, "count": "0"}
    return rows, {"row": "source", "source": "status", "sha": marker_sha, "count": str(len(rows))}


def fetch_rollup(fetch: Fetch, pr: str) -> tuple[list[dict], dict, object]:
    """(3) ROLLUP — WITNESSES ONLY (identity, no verdict), for the containment test.

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
    """
    data = fetch("rollup", ["gh", "pr", "view", pr, "--json", "statusCheckRollup,headRefOid"])
    if not isinstance(data, dict):
        raise FetchError(f"rollup: expected an object, got {type(data).__name__}")
    witnesses = [
        w for w in (data.get("statusCheckRollup") or [])
        if isinstance(w, dict) and w.get("__typename") == "CheckRun"
    ]
    rows = [{"row": "witness", "name": s(w.get("name")), "id": s(w.get("detailsUrl") or NO_OID)}
            for w in witnesses]
    # MUTATE:rollup-marker-sha:marker = {"row": "source", "source": "rollup", "sha": "0" * 40, "count": str(len(rows))}
    marker = {"row": "source", "source": "rollup", "sha": NO_OID, "count": str(len(rows))}
    # THE HEAD MUST BE KNOWN, or the fail-closed rule below is a rule that cannot fire. A response with no
    # `headRefOid` leaves us unable to say whether this evidence is about the PR's head or about a commit it
    # has moved past — and "we cannot tell" has exactly one safe answer, which is not green. Left as `None`
    # it would sail straight through `head_moved()` as "not moved" — a fail-closed check that fails OPEN on
    # the one input it cannot read, which is the whole family of bug this file is about.
    head_now = data.get("headRefOid")
    if not isinstance(head_now, str) or not head_now:
        # MUTATE:head-must-be-known:head_now = None
        raise FetchError(
            "rollup: the response carries no headRefOid — WE CANNOT TELL which commit is the PR's head, so "
            "we cannot tell whether this evidence describes it. That is not a green; it is a fetch we "
            "cannot use."
        )

    # WITNESSES, or containment passes TRIVIALLY: with none, "REST saw everything the rollup saw" is a claim
    # about the empty set. The weakening below is the rollup going dark — and it takes the head with it.
    # MUTATE:rollup-witnesses:return [], {"row": "source", "source": "rollup", "sha": NO_OID, "count": "0"}, None
    return rows, marker, head_now


def up(value: object) -> object:
    """REST returns `status`/`conclusion`/`state` in lowercase; the enums are UPPERCASE."""
    return value.upper() if isinstance(value, str) else value


def build_snapshot(fetch: Fetch, repo: str, pr: str, head_sha: str) -> tuple[list[dict], dict]:
    """The artifact, in order: header, then each source's rows FOLLOWED BY ITS OWN MARKER.

    THE HEADER'S SHA IS THE ONE ROW WHOSE SHA IS OURS — it records the commit we ASKED FOR (the ledger's
    `head_sha`). Every EVIDENCE row and every MARKER carries what GITHUB said. Two independent sources, which
    is the ONLY reason comparing them can tell you anything: build the check out of your own input and it
    passes by construction, including on a snapshot fetched for the wrong commit.
    """
    rows: list[dict] = [{"row": "header", "sha": head_sha}]
    meta: dict = {"notes": [], "head_sha_now": None}

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
    # MUTATE:head-read-last:(witnesses, ru_marker, head_now), (runs, cr_marker, total), (statuses, st_marker) = fetch_rollup(fetch, pr), fetch_check_runs(fetch, repo, head_sha), fetch_statuses(fetch, repo, head_sha)
    (runs, cr_marker, total), (statuses, st_marker), (witnesses, ru_marker, head_now) = (
        fetch_check_runs(fetch, repo, head_sha),
        fetch_statuses(fetch, repo, head_sha),
        fetch_rollup(fetch, pr),
    )
    rows += runs + [cr_marker] + statuses + [st_marker] + witnesses + [ru_marker]

    meta["head_sha_now"] = head_now
    meta["evidence"] = {"checkrun": len(runs), "status": len(statuses), "witness": len(witnesses)}

    # A SILENT OMISSION IS A LIE. `--paginate` defeats page-size truncation; it does NOT prove completeness,
    # and `/check-runs` is capped at the 1000 most recent check suites. If what we collected disagrees with
    # GitHub's own `total_count`, SAY SO — in the output, where the driver reads it.
    if total is not None and total != len(runs):
        # MUTATE:disclose-truncation:pass
        meta["notes"].append(
            f"check-runs: GitHub reported total_count={total} but the paginated read collected "
            f"{len(runs)} row(s) — evidence may be INCOMPLETE, and a row that is not here could be the "
            f"failing one. (/check-runs is also capped at the 1000 most recent check suites; --paginate "
            f"defeats page-size truncation, it does not prove completeness at that scale.)"
        )
    return rows, meta


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


def derive(fetch: Fetch, repo: str, pr: str, head_sha: str, rundir: Path) -> dict:
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
    """
    try:
        rows, meta = build_snapshot(fetch, repo, pr, head_sha)
    except FetchError as exc:
        # A source that could not be read leaves NO artifact — there is nothing on disk for a later wake to
        # mistake for evidence, and no verdict is derived from a fetch we know to be incomplete. The
        # `promote` below is NEVER reached, and that is the whole of the "no partial artifact" rule: it is
        # this `return`, so it is marked ONCE, here, rather than twice in two places that cannot disagree.
        # MUTATE:fetch-failure-is-not-evidence:return result(pr, head_sha, SNAP.GREEN, "the fetch failed, assumed fine", None, {}, None, [])
        return result(pr, head_sha, SNAP.UNUSABLE, f"FETCH FAILED — {exc}", None, {}, None, [str(exc)])

    path = promote(rows, rundir, pr, head_sha)

    # MUTATE:verdict-from-snapshot:verdict, reason = SNAP.GREEN, "fetched"
    verdict, reason = SNAP.evaluate(path, head_sha, expect_filename_sha=True)

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
    head_now = meta["head_sha_now"]
    if head_moved(head_sha, head_now):
        # MUTATE:head-moved-is-not-evidence:pass
        verdict, reason = SNAP.UNUSABLE, (
            f"HEAD MOVED — this evidence was fetched for {head_sha}, but the PR's head is NOW {head_now}. "
            f"It describes a commit that is no longer this PR's head, so it is not evidence about this PR "
            f"at all: NOT green (the evidence is stale), and NOT red (that would be a claim about the wrong "
            f"commit). Re-derive with --head-sha {head_now} once the ledger holds it. "
            f"(what the stale snapshot said, for the record: {verdict} — {reason})"
        )

    return result(pr, head_sha, verdict, reason, path, meta["evidence"], head_now, meta["notes"])


def result(pr: str, head_sha: str, verdict: str, reason: str, path: Path | None,
           evidence: dict, head_now: object, notes: list[str]) -> dict:
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
    if verdict not in LEDGER_CI:
        fail(
            f"DECIDE returned {verdict!r}, which this tool has no ledger mapping for. That is an outcome "
            f"nobody has thought about, and guessing `pending` for it is the same 'close enough' that "
            f"greened a commit with no checks on it. Refusing to emit a verdict."
        )
    return {
        "pr": pr,
        "head_sha": head_sha,          # the commit we PINNED to (the ledger's) — what the fetch asked for
        "verdict": verdict,            # the DECIDE outcome, in full
        "ci": LEDGER_CI[verdict],      # the LEDGER value — write this to `ledger.py … set --pr <N> --ci`
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
        "notes": notes,                # anything filtered, capped, or incomplete. NEVER silent.
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
        raise DocError("no CLASSIFY rules parsed — the tables are GONE, renamed, or reformatted")
    if seen_catch_all < 2:
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
        raise DocError("no `#### DECIDE` section — the order this tool pins is not where it was")
    names = "|".join(re.escape(n) for n in sorted(DECIDE_ORDER, key=len, reverse=True))
    found = tuple(
        m.group(1) for m in re.finditer(rf"^- \*\*({names})\*{{0,2}}", section.group(1), re.MULTILINE)
    )
    if not found:
        raise DocError("the DECIDE section lists ZERO outcome bullets — it cannot be checked, so it FAILS")
    return found


def doc_check(doc: Path) -> int:
    """Assert the DOC, the CODE, and this tool's DECIDE_ORDER all say the same thing.

    Three things are checked, and the third is the one no reader ever does by hand:

      1. the doc's CLASSIFY buckets == the sets `ci-snapshot.py` actually classifies with;
      2. the doc's DECIDE bullet order == DECIDE_ORDER (which the fixtures pin behaviourally);
      3. **CLASSIFICATION IS TOTAL over the doc's OWN enums** — every declared value lands in exactly one
         bucket, no bucket holds a value the enum does not declare. A rule set can agree with the doc's
         tables line for line and still leave a HOLE, because the tables and the enum list are two different
         paragraphs. A value in a hole matches NO branch: not green, not red, not pending — the PR can never
         resolve, and it WEDGES. This is the check that catches that, and nothing else in the repo does.
    """
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
    print()
    if failures:
        print(f"{failures} disagreement(s) between {doc.name} and the code that runs. "
              f"ONE of them is wrong and a reader will believe the other.")
        return 1
    print(f"{len(checks)} checks: {doc.name}, ci-snapshot.py and ci-status.py agree — "
          f"enums, CLASSIFY buckets, TOTALITY, and the DECIDE order.")
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
    fx = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    head_sha = fx.get("head_sha", FIXTURE_SHA)
    rundir = tmp / name.replace(".json", "")
    rundir.mkdir(parents=True, exist_ok=True)
    return fx, derive(fixture_fetch(fx), "o/r", fx.get("pr", "35"), head_sha, rundir)


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
    for needle in want.get("notes", []):
        if not any(needle in n for n in got["notes"]):
            bad.append(f"no note mentioning {needle!r} — a silent omission is a lie")
    if want.get("promoted") is False and got["snapshot"] is not None:
        bad.append("an artifact was PROMOTED for a fetch that FAILED — a later wake would read it as evidence")
    return bad


def self_test(tmp: Path) -> int:
    failures = 0
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
    return {name: (fx["expect"]["verdict"], fx["expect"]["needle"])
            for name, fx in ((n, json.loads((FIXTURES / n).read_text(encoding="utf-8"))) for n in cases())}


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
    return out


GREEN = SNAP.GREEN


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("derive", help="fetch, promote, verify and decide a PR's CI status")
    d.add_argument("--pr", required=True)
    d.add_argument("--head-sha", required=True, help="the LEDGER's head_sha — the commit to pin the fetch to")
    d.add_argument("--rundir", required=True, type=Path, help="where the snapshot is promoted")
    d.add_argument("--repo", help="owner/name (default: the current checkout's, via `gh repo view`)")

    c = sub.add_parser("doc-check", help="assert stage-2-ci.md agrees with the code that runs")
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

    # An OPERATOR ERROR is not a verdict about the PR. A `--head-sha` that is not a git object id makes
    # every comparison downstream unfalsifiable, and blaming the EVIDENCE for the caller's mistake is how a
    # tool reports a defect that is not there. Exit 2: no verdict at all beats a verdict about the wrong
    # question.
    if not SHA_RE.match(args.head_sha):
        fail(f"--head-sha {args.head_sha!r} is not a git object id (40 LOWERCASE hex) — refusing to derive")
    if not args.rundir.is_dir():
        fail(f"--rundir {args.rundir} is not a directory")

    repo = args.repo
    if not repo:
        try:
            repo = str(gh_fetch("repo", ["gh", "repo", "view", "--json", "nameWithOwner"]).get("nameWithOwner"))  # type: ignore[union-attr]
        except (FetchError, AttributeError) as exc:
            fail(f"cannot determine the repo ({exc}) — pass --repo owner/name")

    out = derive(gh_fetch, repo, args.pr, args.head_sha, args.rundir)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    # green is the ONLY exit-0 verdict. Everything else — pending, red, unusable, an unclassified value —
    # is NOT a green, and a caller that checks only the exit status must never be told otherwise.
    return 0 if out["verdict"] == SNAP.GREEN else 1


if __name__ == "__main__":
    raise SystemExit(main())
