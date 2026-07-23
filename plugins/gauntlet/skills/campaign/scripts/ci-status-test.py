#!/usr/bin/env python3
"""THE EXECUTABLE CONTRACT FOR `ci-status.py` — every producer rule, pinned by a RECORDED API RESPONSE.

Run it through the tool it tests (this is what CI runs):

    python3 ci-status.py self-test

or directly, which does the same thing:

    python3 ci-status-test.py

**THE FIXTURES ARE RECORDED GITHUB RESPONSES, DRIVEN THROUGH THE REAL PRODUCER.** They are not a model of
the fetch path; they ARE the fetch path, with one seam replaced (`fixture_fetch`, which answers from a
recording instead of the network). So the code under test is the same code that runs against GitHub, and a
rule that is deleted from the producer is a rule some fixture stops noticing.

**MOST OF THESE CASES ARE FALSE GREENS THIS TOOL ACTUALLY SHIPPED.** `zero-checks.json` is the one that
motivated the whole file (a driver read "no checks reported" and wrote `ci = green`). The rest were found by
reviewers driving real responses through the producer and watching it return `green`: a family never
fetched, a rollup `StatusContext` dropped on the floor, a page whose row array was simply absent, two
sources contradicting each other about one check, a head that moved under the fetch. Each is recorded here
so that the fix cannot be quietly reverted.

**A FIXTURE ASSERTS ITS VERDICT *AND* THE RULE THAT PRODUCED IT** (`expect.needle`, matched against the
reason). A fixture that passes for somebody else's reason pins NOTHING — it would go on passing after the
rule it was written for had been deleted, which is the shape of every defect in the tool it guards.

AND THE SEAMS NO RECORDED RESPONSE CAN REACH (`SEAM_EXPECT`). A fixture drives the producer through
`fixture_fetch`, so it never runs `gh_fetch` at all — the one code path that actually talks to GitHub — and
it never goes through the CLI, so the operator-error guards are unreachable too. Worse: **every fixture
IGNORES the argv it is handed**, so a fetch aimed at the WRONG REPOSITORY is invisible to all of them. That
is not hypothetical: the rollup ran `gh pr view <pr>` with no `--repo` for the life of this tool, every
fixture stayed green, and the flag was a lie against any repo but the one the process was standing in. The
`[seam]` and `[argv]` cases assert on the COMMAND and on the local process, with no network.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
STATUS_PY = HERE / "ci-status.py"


# --- the fingerprint canonicalization, pinned to the BYTE -----------------------------------------
#
# The payload below is built from LITERALS, straight off the spec in `stage-2-ci.md` ("SETTLED") — never by
# calling the function under test. If `fingerprint()` ever tab-joins differently, drops a duplicate line,
# hashes the `id`, or forgets a terminating newline, the `[fp]` cases go red; a test that derived its
# expectation from the same code could never notice any of that.
FP_SHA = "1499c72bf1715e74abb0e28658b515eaa2c0c971"
FP_ROWS = [
    # Deliberately SCRAMBLED, with every non-evidence row type present: order must not matter, and
    # `header`/`source`/`witness` rows must not enter the hash.
    {"row": "witness", "name": "Lint scripts", "id": "https://x/1"},
    {"row": "status", "sha": FP_SHA, "context": "ci/jenkins", "state": "SUCCESS"},
    {"row": "header", "sha": FP_SHA},
    {"row": "checkrun", "sha": FP_SHA, "name": "Lint scripts", "app_id": "15368",
     "status": "COMPLETED", "conclusion": "SUCCESS", "id": "https://x/1"},
    {"row": "source", "source": "rollup", "sha": "-", "count": "1"},
    # A re-run of the same check under a NEW job id: a DIFFERENT `id`, the SAME canonical line — and the
    # duplicate line is KEPT, so one leg becoming two IS motion while a re-run by itself is not.
    {"row": "checkrun", "sha": FP_SHA, "name": "Lint scripts", "app_id": "15368",
     "status": "COMPLETED", "conclusion": "SUCCESS", "id": "https://x/2"},
]
FP_PAYLOAD = (FP_SHA + "\n"
              + "checkrun\tLint scripts\t15368\tCOMPLETED\tSUCCESS\n" * 2
              + "status\tci/jenkins\tSUCCESS\n")
FP_EXPECT = hashlib.sha256(FP_PAYLOAD.encode("utf-8")).hexdigest()


# --- the SEAMS no fixture can reach ---------------------------------------------------------------
#
# The result of each case is `refused` (the rule fired), `accepted` (it did not), or `crash:<T>` — because a
# tool that RAISES where a verdict was owed has NOT refused, it has had no opinion, and the two must never be
# recorded as the same thing.
SEAM_EXPECT = {
    "[seam] a dead gh is a failed fetch": ("refused", "exited 1"),
    "[seam] gh stdout that is not JSON": ("refused", "not JSON"),
    "[seam] --head-sha must be an oid": ("refused", "exit 2"),
    "[seam] --rundir must exist": ("refused", "exit 2"),
    # No FIXTURE can carry an unreadable spec — `run_fixture` parses it before the producer ever runs, so a
    # fixture with a broken one would fail as a BROKEN FIXTURE, not as the rule firing. The guard belongs
    # here, with the other operator errors: it is about what the CALLER handed us, never about the PR.
    "[seam] --required-set must parse": ("refused", "exit 2"),
    # A fetcher that FORGETS the repo — the defect, reconstructed. It is the rollup's old argv, verbatim,
    # which is what makes this the defect itself rather than a model of it. It must be REFUSED by the seam,
    # not merely absent from some list: if this case can be `accepted`, a new fetcher can silently query the
    # wrong repository.
    "[argv] a fetcher that forgets the repo": ("refused", "is NOT scoped to"),
    # THE SPOOF: the repository IS in the argv — in a `--template`, where it scopes NOTHING, and the command
    # still resolves against the current checkout. **A guard that accepts a STRING where it means a POSITION
    # can be fed the string.**
    "[argv] the repo named in a flag that scopes nothing": ("refused", "is NOT scoped to"),
    # THE SAME SPOOF ON THE `gh api` HALF — the half a round of "fixing" the case above left behind. The
    # ENDPOINT names `wrong/repo` while the repo-shaped string sits in a flag that scopes nothing. `gh` would
    # query `wrong/repo`.
    "[argv] a gh api endpoint aimed at the WRONG repo": ("refused", "is NOT scoped to"),
    # A flag before the endpoint that MIGHT eat it: `gh api --template <x> repos/wrong/…`. The word after
    # `--template` is its VALUE, not the path, so the endpoint's position is NOT IDENTIFIABLE — and an
    # unidentifiable position FAILS CLOSED rather than guessing which word is the path.
    "[argv] an unknown flag ahead of the endpoint": ("refused", "is NOT scoped to"),
    # **AND THE MIRROR, OR THE FIX IS "REFUSE EVERYTHING".** A CORRECTLY scoped endpoint that happens to carry
    # a repo-shaped string in a flag as well is a perfectly good fetch, and must be ACCEPTED. A guard is only
    # honest if it can still say yes.
    "[argv] a right endpoint with repo-shaped junk elsewhere": ("accepted", "repos/o/r/commits/"),
    # And the door itself: a read that reaches `field()` with NO SHAPE refuses there, at run time, wherever
    # the caller is. `field()` takes no default and never will — a default is a legal-looking value handed to
    # whoever did not think about the illegal case.
    "[shape] a field read that declares NO shape": ("refused", "DECLARES NO SHAPE"),
    # The fingerprint canonicalization, against a payload built from LITERALS (FP_PAYLOAD above). The first
    # case pins every byte at once: field selection, tab-joining, the bytewise sort, duplicates kept,
    # non-evidence rows excluded, newline termination. The rest pin the EXCLUSIONS as behavior — the things
    # the hash must NOT move for — because those are the halves a rewrite quietly loses.
    "[fp] the fingerprint is sha256 over the CANONICAL payload": ("accepted", FP_EXPECT),
    "[fp] row order does not move the fingerprint": ("accepted", FP_EXPECT),
    "[fp] a verdict change MOVES the fingerprint": ("accepted", "True"),
    "[fp] a re-run under a new job id is NOT motion": ("accepted", "True"),
}


def load_status_module():
    """Load `ci-status.py` — used only when this file is run DIRECTLY. Driven through `ci-status.py
    self-test`, the module is handed to `run()` instead, so the tool under test is loaded exactly once.

    `HERE`-relative, never cwd-relative: the hyphen in the filename means it is not importable as a module
    path, and the installed plugin cache is a different directory from this repo.
    """
    spec = importlib.util.spec_from_file_location("ci_status", STATUS_PY)
    if spec is None or spec.loader is None:  # pragma: no cover - a broken checkout
        raise SystemExit(f"ci-status-test: cannot load {STATUS_PY}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cases(ci) -> list[str]:
    return sorted(p.name for p in ci.FIXTURES.glob("*.json"))


def artifact_state(rundir: Path) -> dict[str, str]:
    """The audit-artifact set and content digest, so a failed call cannot hide a replace behind one name."""
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(rundir.glob("ci-*.txt"))
    }


def run_fixture(ci, name: str, tmp: Path) -> tuple[dict, dict, Path, dict[str, str]]:
    """Drive one recorded fixture through the REAL producer.

    `required_set` IS MANDATORY IN EVERY FIXTURE, and there is deliberately NO DEFAULT — the same rule
    `evaluate()` enforces on its callers, enforced here on the fixtures. A default would be a permissive
    answer handed to whoever forgot to think about it, and the value is never incidental: the SAME recorded
    responses are `green` under `none` and `pending` under a `declared:` set that names a check nobody has
    registered. That is the whole of `required-check-absent.json`, and a fixture that did not have to state
    the set could not have expressed it.
    """
    fx = json.loads((ci.FIXTURES / name).read_text(encoding="utf-8"))
    if "required_set" not in fx:
        ci.fail(f"{name}: the fixture declares no `required_set` — it is an INPUT to the verdict, not a "
                f"detail, and a suite that defaults it silently tests the permissive case and calls it the "
                f"only case")
    head_sha = fx.get("head_sha", ci.FIXTURE_SHA)
    rundir = tmp / name.replace(".json", "")
    rundir.mkdir(parents=True, exist_ok=True)
    seeded = fx.get("seed_artifacts", {})
    if not isinstance(seeded, dict):
        ci.fail(f"{name}: `seed_artifacts` is not an object")
    for filename, content in seeded.items():
        if (not isinstance(filename, str) or Path(filename).name != filename
                or not re.fullmatch(r"ci-[^-]+-[0-9a-f]{40}\.txt", filename)
                or not isinstance(content, str)):
            ci.fail(f"{name}: invalid seeded audit artifact {filename!r}")
        (rundir / filename).write_text(content, encoding="utf-8")
    before = artifact_state(rundir)
    required = ci.SNAP.parse_required_set(fx["required_set"])
    got = ci.derive(ci.fixture_fetch(fx), "o/r", fx.get("pr", "35"), head_sha, rundir, required)
    return fx, got, rundir, before


def check_fixture(name: str, got: dict, fx: dict, rundir: Path,
                  artifacts_before: dict[str, str]) -> list[str]:
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
    if "promoted" in want:
        promoted = want["promoted"]
        if not isinstance(promoted, bool):
            bad.append(f"fixture promotion expectation {promoted!r} is not boolean")
        else:
            artifacts = sorted(rundir.glob("ci-*.txt"))
            artifacts_after = artifact_state(rundir)
            reported = Path(got["snapshot"]) if got["snapshot"] is not None else None
            if promoted and (reported is None or not reported.is_file() or artifacts != [reported]):
                bad.append(
                    f"expected one PROMOTED audit artifact reported by `snapshot`; "
                    f"snapshot={got['snapshot']!r}, artifacts={[str(p) for p in artifacts]!r}"
                )
            elif not promoted and (reported is not None or artifacts_after != artifacts_before):
                bad.append(
                    f"expected failed/incomplete fetch to report and promote NO artifact while preserving "
                    f"the existing set byte-for-byte; snapshot={got['snapshot']!r}, "
                    f"before={artifacts_before!r}, after={artifacts_after!r}"
                )
    # THE FINGERPRINT INVARIANT HOLDS ON EVERY FIXTURE, no per-fixture expectation needed: a trusted
    # current-head result carries the sha256 the driver compares to `ci_fingerprint`, and an untrusted one
    # carries `null` — nothing rejected is ever hashed, so no strike can accrue against rows nobody believed.
    fp = got.get("fingerprint", "ABSENT")
    if fp == "ABSENT":
        bad.append("derive emitted NO `fingerprint` field — the driver would be back to hashing by hand")
    elif got["verdict"] in ("unusable", "unverifiable"):
        if fp is not None:
            bad.append(f"fingerprint {fp!r} on an untrusted ({got['verdict']}) snapshot — nothing rejected "
                       f"is ever hashed")
    elif fp is None or not re.fullmatch(r"[0-9a-f]{64}", fp):
        bad.append(f"fingerprint {fp!r} on trusted current-head evidence — expected its 64-hex sha256")
    # And `buckets` rides with it: null exactly when the fingerprint is, the four-key tally otherwise —
    # this is what the watch policy and `liveness`'s SETTLED/RUNNING-STALL split read, so a fixture whose
    # buckets went missing is a fixture whose PR nobody can decide to watch.
    buckets = got.get("buckets", "ABSENT")
    if buckets == "ABSENT":
        bad.append("derive emitted NO `buckets` field — the watch policy is back to classifying by eye")
    elif fp != "ABSENT" and (buckets is None) != (fp is None):
        bad.append(f"buckets {buckets!r} beside fingerprint {fp!r} — they are null together or present "
                   f"together, never one without the other")
    elif buckets is not None and (
            set(buckets) != {"PASS", "RUNNING", "FAIL", "UNKNOWN_VALUE"}
            or any(not isinstance(v, int) or v < 0 for v in buckets.values())):
        bad.append(f"buckets {buckets!r} is not the four-key non-negative tally")
    return bad


def seam_cases(ci, tmp: Path) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}

    def case(name: str, fn: Callable[[], object]) -> None:
        # TWO CASES UNDER ONE NAME IS ONE CASE NOBODY ASSERTS ON. The results are a dict keyed by name, so
        # the second would overwrite the first — and `check_seams`, which reconciles the NAMES both ways,
        # would see one name, one expectation, and report health for a case whose result it had thrown away.
        if name in out:
            raise AssertionError(
                f"{name}: TWO seam cases share this name — the second overwrites the first, and the suite "
                f"then asserts on ONE of them while REPORTING both. Every case is named exactly once."
            )
        # `fail()` PRINTS to stderr before it exits, and these cases fire it ON PURPOSE — so its output is
        # captured here rather than smeared across the report. The suppression is scoped to the case:
        # swallowing a REAL diagnostic would be exactly the kind of quiet this tool exists to refuse.
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                out[name] = ("accepted", repr(fn()))
            except ci.FetchError as exc:
                out[name] = ("refused", str(exc))
            except SystemExit as exc:
                out[name] = ("refused", f"exit {exc.code}")
            except Exception as exc:  # noqa: BLE001 - a CRASH is not a REFUSAL: no verdict was ever reached
                out[name] = (f"crash:{type(exc).__name__}", str(exc))

    py = sys.executable
    # `gh_fetch` pointed at a LOCAL PYTHON PROCESS that behaves the way a broken `gh` does. No network.
    case("[seam] a dead gh is a failed fetch",
         lambda: ci.gh_fetch("check-runs", [py, "-c", "import sys; print('[]'); sys.exit(1)"]))
    case("[seam] gh stdout that is not JSON",
         lambda: ci.gh_fetch("check-runs", [py, "-c", "print('<html>rate limited</html>')"]))
    case("[seam] --head-sha must be an oid", lambda: ci.check_head_sha("HEAD"))
    case("[seam] --rundir must exist", lambda: ci.check_rundir(tmp / "no-such-dir"))
    # A spec that is neither `none`, `unknown`, nor `declared:<json>`. The one answer that must NEVER come
    # back is a RequiredSet — degrading an unreadable spec to "nothing is required" is the false green the
    # required set exists to close, rebuilt inside its own parser's caller.
    case("[seam] --required-set must parse", lambda: ci.check_required_set("build,test"))

    # THE ARGV, driven through the SAME `repo_scoped` seam every real fetcher goes through.
    def scoped_call(*argv: str) -> object:
        return ci.repo_scoped(lambda _s, a: a, "o/r")("a-new-fetcher", list(argv))

    case("[argv] a fetcher that forgets the repo",
         lambda: scoped_call("gh", "pr", "view", "35", "--json", "statusCheckRollup,headRefOid"))
    case("[argv] the repo named in a flag that scopes nothing",
         lambda: scoped_call("gh", "pr", "view", "35", "--json", "statusCheckRollup,headRefOid",
                             "--template", "repos/o/r/x"))
    case("[argv] a gh api endpoint aimed at the WRONG repo",
         lambda: scoped_call("gh", "api", "--paginate", "--slurp",
                             "repos/wrong/repo/commits/abc/check-runs", "--jq", "repos/o/r/x"))
    case("[argv] an unknown flag ahead of the endpoint",
         lambda: scoped_call("gh", "api", "--template", "repos/o/r/x",
                             "repos/wrong/repo/commits/abc/check-runs"))
    case("[argv] a right endpoint with repo-shaped junk elsewhere",
         lambda: scoped_call("gh", "api", "--paginate", "--slurp",
                             "repos/o/r/commits/abc/check-runs", "--jq", "repos/o/r/x",
                             "-H", "X-Repo: repos/other/repo/x"))
    case("[shape] a field read that declares NO shape",
         lambda: ci.field("check-runs", {"check_runs": []}, "check_runs"))

    case("[fp] the fingerprint is sha256 over the CANONICAL payload",
         lambda: ci.SNAP.fingerprint(FP_ROWS, FP_SHA))
    case("[fp] row order does not move the fingerprint",
         lambda: ci.SNAP.fingerprint(list(reversed(FP_ROWS)), FP_SHA))
    case("[fp] a verdict change MOVES the fingerprint",
         lambda: ci.SNAP.fingerprint(
             [{**r, "conclusion": "FAILURE"} if r["row"] == "checkrun" else r for r in FP_ROWS],
             FP_SHA) != FP_EXPECT)
    case("[fp] a re-run under a new job id is NOT motion",
         lambda: ci.SNAP.fingerprint(
             [{**r, "id": "https://x/rerun"} if r["row"] == "checkrun" else r for r in FP_ROWS],
             FP_SHA) == FP_EXPECT)
    return out


def check_seams(ci, tmp: Path) -> list[str]:
    """Every case RAN and every case is EXPECTED — reconciled BOTH ways.

    A case with no expectation is a case nobody asserts anything about: it runs, it can return whatever it
    likes, and the suite reports health it never measured. That is the same defect this suite exists to
    catch in the tool, so the reconciliation is mechanical rather than a habit.
    """
    bad = []
    got = seam_cases(ci, tmp)
    for name in sorted(set(got) - set(SEAM_EXPECT)):
        bad.append(f"{name}: this case RAN and NOTHING EXPECTS it — a case no table asserts on is a case "
                   f"that cannot fail, and a suite that runs it reports health it never measured")
    for name in sorted(set(SEAM_EXPECT) - set(got)):
        bad.append(f"{name}: this case is EXPECTED and never RAN — an expectation with no case is an "
                   f"assertion about nothing")
    for name, (want, needle) in SEAM_EXPECT.items():
        if name not in got:
            continue
        verdict, detail = got[name]
        if verdict != want:
            bad.append(f"{name}: {verdict!r}, expected {want!r} — {detail}")
        elif needle not in detail:
            bad.append(f"{name}: right outcome, WRONG RULE: {needle!r} not in {detail!r}")
    return bad


def required_set_cases(ci, tmp: Path) -> list[str]:
    """Drive the required-set producer through recorded GitHub responses and the real ledger accessor."""
    problems: list[str] = []
    fixtures = ci.HERE / "fixtures" / "required-set"

    def payload(name: str):
        return json.loads((fixtures / name).read_text(encoding="utf-8"))

    classic = ci.classic_required_set(payload("classic-protection.json"))
    expected_classic = [("Lint scripts", "15368"), ("Validate plugins", ci.SNAP.ANY_APP)]
    if classic != expected_classic:
        problems.append(f"[required-set] classic protection produced {classic!r}, expected {expected_classic!r}")

    for name in ("ruleset-null-app.json", "ruleset-no-app.json"):
        got = ci.ruleset_required_set([payload(name)])
        if got != [("ci/jenkins", ci.SNAP.ANY_APP)]:
            problems.append(f"[required-set] {name} produced {got!r}, expected an unbound ci/jenkins")

    pages = [payload("ruleset-paged-p1.json"), payload("ruleset-paged-p2.json")]
    got_paged = ci.ruleset_required_set(pages)
    if got_paged != expected_classic:
        problems.append(f"[required-set] paged rules produced {got_paged!r}, expected {expected_classic!r}")

    seen: list[tuple[str, list[str]]] = []

    def recorded_fetch(source: str, argv: list[str]):
        seen.append((source, argv))
        return payload("classic-protection.json") if source.endswith("classic") else pages

    spec, reason = ci.fetch_required_set(recorded_fetch, "o/r", "feature/x$(unsafe)")
    expected_spec = ci.canonical_required_set(expected_classic)
    if spec != expected_spec:
        problems.append(f"[required-set] union produced {spec!r}, expected {expected_spec!r}: {reason}")
    encoded = "feature%2Fx%24%28unsafe%29"
    if len(seen) != 2 or any(encoded not in argv[-1] for _source, argv in seen):
        problems.append(f"[required-set] base branch was not URL-encoded in both scoped reads: {seen!r}")
    ruleset_argv = next((argv for source, argv in seen if source.endswith("ruleset")), [])
    if any(flag not in ruleset_argv for flag in ("--paginate", "--slurp")):
        problems.append(f"[required-set] ruleset read was not paginated and slurped: {ruleset_argv!r}")

    no_checks, _ = ci.fetch_required_set(
        lambda source, _argv: {"protection": {"enabled": False}} if source.endswith("classic") else [[]],
        "o/r", "main",
    )
    if no_checks != ci.SNAP.NONE_DECLARED:
        problems.append(f"[required-set] two complete empty reads produced {no_checks!r}, expected `none`")

    def failed_fetch(source: str, _argv: list[str]):
        if source.endswith("ruleset"):
            raise ci.FetchError("ruleset denied")
        return {"protection": {"enabled": False}}

    unknown, unknown_reason = ci.fetch_required_set(failed_fetch, "o/r", "main")
    if unknown != ci.SNAP.CANNOT_READ or "denied" not in unknown_reason:
        problems.append(f"[required-set] one failed source produced {unknown!r}: {unknown_reason}")

    malformed, malformed_reason = ci.fetch_required_set(
        lambda source, _argv: {} if source.endswith("classic") else [[]], "o/r", "main"
    )
    if malformed != ci.SNAP.CANNOT_READ or "ABSENT" not in malformed_reason:
        problems.append(f"[required-set] an absent required field produced {malformed!r}: {malformed_reason}")

    ledger = tmp / "required-set-state.jsonl"
    header = dict(ci.LEDGER.HEADER_DEFAULTS)
    header.update({"run_id": "test", "base_branch": "feature/x$(unsafe)"})
    ci.LEDGER.dump(ledger, header, [])
    settled = ci.refresh_required_set(recorded_fetch, ledger, "o/r")
    persisted, _rows = ci.LEDGER.load(ledger)
    if not settled["settled"] or persisted["required_set"] != expected_spec:
        problems.append(f"[required-set] the canonical union was not settled atomically: {settled!r}")

    def must_not_fetch(_source: str, _argv: list[str]):
        raise AssertionError("a settled ledger was fetched again")

    try:
        reused = ci.refresh_required_set(must_not_fetch, ledger, "o/r")
        if reused["required_set"] != expected_spec:
            problems.append(f"[required-set] settled reuse changed the value: {reused!r}")
    except Exception as exc:  # noqa: BLE001 - a fetch here is the behavior this case detects
        problems.append(f"[required-set] settled ledger was not reused: {type(exc).__name__}: {exc}")

    snapshot_fixtures = ci.SNAPSHOT_PY.parent / "fixtures" / "ci-snapshot"
    required = ci.SNAP.parse_required_set(expected_spec)
    verdict, verdict_reason = ci.SNAP.evaluate(
        snapshot_fixtures / "green.jsonl", ci.FIXTURE_SHA, required=required, expect_filename_sha=False
    )
    if verdict != ci.SNAP.GREEN or "required set satisfied" not in verdict_reason:
        problems.append(f"[required-set] producer-to-verifier chain returned {verdict}: {verdict_reason}")

    problems += grouped_required_set_cases(ci, tmp)
    problems += required_set_matrix_cases(ci, tmp)
    return problems


def required_set_cli_cases(ci, tmp: Path) -> list[str]:
    """Pin the required-set process contract: settled=0, retryable unknown=1, caller/store errors=2."""
    problems: list[str] = []
    cli_tmp = tmp / "required-set-cli"
    cli_tmp.mkdir()

    def run_cli(ledger: Path, *, repo: str = "o/r", env: "dict[str, str] | None" = None):
        return subprocess.run(  # noqa: S603 - this suite drives its sibling command
            [sys.executable, str(STATUS_PY), "required-set", "--ledger", str(ledger), "--repo", repo],
            capture_output=True, text=True, check=False, env=env,
        )

    def valid_ledger(path: Path, *, header_required: str, row_required: "str | None" = None) -> None:
        header = dict(ci.LEDGER.HEADER_DEFAULTS)
        header.update({"run_id": path.stem, "base_branch": "main", "required_set": header_required})
        rows = []
        if row_required is not None:
            row = dict(ci.LEDGER.ROW_DEFAULTS)
            row.update({"pr": "1", "base_branch": "main", "required_set": row_required,
                        "status": "in_review"})
            rows.append(row)
        ci.LEDGER.dump(path, header, rows)

    settled = cli_tmp / "settled.jsonl"
    valid_ledger(settled, header_required=ci.SNAP.NONE_DECLARED)
    settled_before = settled.read_bytes()
    proc = run_cli(settled)
    if proc.returncode != 0:
        problems.append(f"[required-set CLI] settled ledger exited {proc.returncode}, not 0: {proc.stderr!r}")
    elif settled.read_bytes() != settled_before:
        problems.append("[required-set CLI] settled ledger was rewritten despite needing no read")
    elif proc.stderr:
        problems.append(f"[required-set CLI] settled ledger emitted stderr: {proc.stderr!r}")

    fake_bin = cli_tmp / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text("#!/bin/sh\nprintf 'called\\n' >> \"$GH_CALLS\"\nexit 1\n", encoding="utf-8")
    fake_gh.chmod(0o700)
    gh_calls = cli_tmp / "gh-calls"
    gh_calls.write_bytes(b"")
    denied_env = os.environ.copy()
    denied_env["PATH"] = str(fake_bin) + os.pathsep + denied_env.get("PATH", "")
    denied_env["GH_CALLS"] = str(gh_calls)
    unknown = cli_tmp / "unknown.jsonl"
    valid_ledger(unknown, header_required=ci.SNAP.CANNOT_READ, row_required="-")
    proc = run_cli(unknown, env=denied_env)
    _header, rows = ci.LEDGER.load(unknown)
    if proc.returncode != 1:
        problems.append(f"[required-set CLI] unknown group exited {proc.returncode}, not 1: {proc.stderr!r}")
    elif rows[0]["required_set"] != ci.SNAP.CANNOT_READ:
        problems.append(f"[required-set CLI] failed read persisted {rows[0]['required_set']!r}, not `unknown`")
    elif proc.stderr:
        problems.append(f"[required-set CLI] retryable unknown emitted stderr: {proc.stderr!r}")

    def check_error(name: str, ledger: Path, before: bytes, proc) -> None:
        if proc.returncode != 2:
            problems.append(f"[required-set CLI] {name} exited {proc.returncode}, not 2: {proc.stderr!r}")
        if ledger.read_bytes() != before:
            problems.append(f"[required-set CLI] {name} mutated the ledger")
        lines = [line for line in proc.stderr.splitlines() if line]
        if "Traceback" in proc.stderr:
            problems.append(f"[required-set CLI] {name} emitted a traceback: {proc.stderr!r}")
        elif len(lines) != 1:
            problems.append(f"[required-set CLI] {name} emitted {len(lines)} diagnostics, not one: "
                            f"{proc.stderr!r}")

    malformed_repo = cli_tmp / "malformed-repo.jsonl"
    valid_ledger(malformed_repo, header_required=ci.SNAP.CANNOT_READ, row_required="-")
    malformed_repo_before = malformed_repo.read_bytes()
    check_error(
        "malformed --repo",
        malformed_repo,
        malformed_repo_before,
        run_cli(malformed_repo, repo="invalid", env=denied_env),
    )

    whitespace_repo = cli_tmp / "whitespace-repo.jsonl"
    valid_ledger(whitespace_repo, header_required=ci.SNAP.CANNOT_READ)
    whitespace_repo_before = whitespace_repo.read_bytes()
    calls_before = gh_calls.read_bytes()
    check_error(
        "whitespace-only --repo owner/name",
        whitespace_repo,
        whitespace_repo_before,
        run_cli(whitespace_repo, repo=" / ", env=denied_env),
    )
    if gh_calls.read_bytes() != calls_before:
        problems.append("[required-set CLI] whitespace-only --repo fetched from GitHub")

    malformed_cases = {
        "headerless ledger": b'{"type":"row","pr":"1"}\n',
        "duplicate-row ledger": (b'{"type":"header"}\n'
                                 b'{"type":"row","pr":"1"}\n'
                                 b'{"type":"row","pr":"1"}\n'),
        "non-UTF-8 ledger": b'{"type":"header"}\n\xff\n',
    }
    for slug, (name, body) in enumerate(malformed_cases.items(), start=1):
        ledger = cli_tmp / f"malformed-{slug}.jsonl"
        ledger.write_bytes(body)
        check_error(name, ledger, body, run_cli(ledger))

    malformed_spec = cli_tmp / "malformed-spec.jsonl"
    valid_ledger(malformed_spec, header_required="not-a-required-set")
    malformed_before = malformed_spec.read_bytes()
    check_error("malformed required set", malformed_spec, malformed_before, run_cli(malformed_spec))

    malformed_row = cli_tmp / "malformed-row-spec.jsonl"
    valid_ledger(
        malformed_row,
        header_required=ci.SNAP.NONE_DECLARED,
        row_required="not-a-required-set",
    )
    malformed_row_before = malformed_row.read_bytes()
    calls_before = gh_calls.read_bytes()
    check_error(
        "malformed row required set",
        malformed_row,
        malformed_row_before,
        run_cli(malformed_row, env=denied_env),
    )
    if gh_calls.read_bytes() != calls_before:
        problems.append("[required-set CLI] malformed row required set fetched from GitHub")

    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        print("skip     [required-set CLI] chmod cannot make the ledger directory unwritable as this user")
    else:
        unwritable_dir = cli_tmp / "unwritable"
        unwritable_dir.mkdir()
        unwritable = unwritable_dir / "state.jsonl"
        valid_ledger(unwritable, header_required=ci.SNAP.NONE_DECLARED, row_required="-")
        unwritable_before = unwritable.read_bytes()
        unwritable_dir.chmod(0o500)
        try:
            proc = run_cli(unwritable)
        finally:
            unwritable_dir.chmod(0o700)
        check_error("unwritable ledger", unwritable, unwritable_before, proc)

    return problems


def grouped_required_set_cases(ci, tmp: Path) -> list[str]:
    """The GROUPED, per-base refresh (mixed bases), and `derive`'s row-based required-set resolution."""
    problems: list[str] = []

    def base_payload(context: str):
        return {"protection": {"enabled": True,
                               "required_status_checks": {"checks": [{"context": context, "app_id": None}]}}}

    def mk_fetch(context_by_base: dict):
        """A fetch keyed on the base in the argv endpoint. Records each DISTINCT base's classic read so a test
        can assert one GitHub read per base."""
        seen: list[str] = []

        def _fetch(source: str, argv: list[str]):
            endpoint = argv[-1]
            base = next((b for b in context_by_base if endpoint.endswith(ci.quote(b, safe=""))), None)
            if base is None:
                raise ci.FetchError(f"unexpected endpoint {endpoint!r}")
            if source.endswith("classic"):
                seen.append(base)
                return base_payload(context_by_base[base])
            return [[]]   # ruleset: one empty page

        return _fetch, seen

    v3_set = ci.canonical_required_set([("v3-test", ci.SNAP.ANY_APP)])
    main_set = ci.canonical_required_set([("main-test", ci.SNAP.ANY_APP)])

    def mrow(pr: str, base: str, required_set: str = "-", status: str = "in_review") -> dict:
        # required_set defaults to "-" — exactly what pr-adopt.py writes for a new explicit-base row (it sets
        # base_branch only). The grouped refresh must still read THIS base for it (never inherit the header).
        row = dict(ci.LEDGER.ROW_DEFAULTS)
        row.update({"pr": pr, "branch": f"b{pr}", "base_branch": base,
                    "required_set": required_set, "status": status})
        return row

    # 1. MIXED BASES: two explicit-base rows settle from ONE read per distinct base; the header is untouched.
    mixed = tmp / "mixed-base-state.jsonl"
    mheader = dict(ci.LEDGER.HEADER_DEFAULTS)
    mheader.update({"run_id": "mixed", "base_branch": "-", "required_set": "unknown"})
    ci.LEDGER.dump(mixed, mheader, [mrow("1", "v3"), mrow("2", "main")])
    fetch, seen = mk_fetch({"v3": "v3-test", "main": "main-test"})
    out = ci.refresh_required_set(fetch, mixed, "o/r")
    header_after, rows_after = ci.LEDGER.load(mixed)
    got = {r["pr"]: r["required_set"] for r in rows_after}
    if got.get("1") != v3_set or got.get("2") != main_set:
        problems.append(f"[grouped] per-base write wrong: {got!r}")
    if not out["settled"]:
        problems.append(f"[grouped] mixed-base groups not settled: {out!r}")
    if sorted(seen) != ["main", "v3"]:
        problems.append(f"[grouped] each distinct base must be read EXACTLY once: {seen!r}")
    if header_after["required_set"] != "unknown":
        problems.append(f"[grouped] a new-run header must stay unknown, never materialized: "
                        f"{header_after['required_set']!r}")

    # a second refresh reads NOTHING — every group is settled.
    def must_not_fetch(_source, _argv):
        raise AssertionError("a settled group was re-read")
    reused = ci.refresh_required_set(must_not_fetch, mixed, "o/r")
    if not reused["settled"] or reused["groups"]:
        problems.append(f"[grouped] settled groups must not be re-read: {reused!r}")

    # 2. A FAILED read for ONE base isolates: the other base still settles, the failed base stays unknown.
    mixed2 = tmp / "mixed-base-fail.jsonl"
    ci.LEDGER.dump(mixed2, dict(mheader), [mrow("1", "v3"), mrow("2", "deny")])

    def fetch_fail(source: str, argv: list[str]):
        if argv[-1].endswith("deny"):
            raise ci.FetchError("denied")
        return base_payload("v3-test") if source.endswith("classic") else [[]]

    out2 = ci.refresh_required_set(fetch_fail, mixed2, "o/r")
    _h2, rows2 = ci.LEDGER.load(mixed2)
    got2 = {r["pr"]: r["required_set"] for r in rows2}
    if got2.get("1") != v3_set:
        problems.append(f"[grouped] a settled group must persist despite a sibling's failure: {got2!r}")
    if got2.get("2") != ci.SNAP.CANNOT_READ:
        problems.append(f"[grouped] a failed base must stay unknown, never `none`: {got2!r}")
    if out2["settled"]:
        problems.append(f"[grouped] a run with an unknown group is NOT settled: {out2!r}")

    # 3. LEGACY ledger: `-` rows inherit the HEADER, which the same command settles — the row is NOT materialized.
    legacy = tmp / "legacy-grouped-state.jsonl"
    lheader = dict(ci.LEDGER.HEADER_DEFAULTS)
    lheader.update({"run_id": "legacy", "base_branch": "main", "required_set": "unknown"})
    lrow = dict(ci.LEDGER.ROW_DEFAULTS)
    lrow.update({"pr": "9", "branch": "b9", "status": "in_review"})   # base_branch/required_set stay "-"
    ci.LEDGER.dump(legacy, lheader, [lrow])
    lf, lseen = mk_fetch({"main": "main-test"})
    lout = ci.refresh_required_set(lf, legacy, "o/r")
    lh, lrows = ci.LEDGER.load(legacy)
    if lh["required_set"] != main_set:
        problems.append(f"[grouped] a legacy header must settle for its `-` rows: {lh['required_set']!r}")
    if lrows[0]["required_set"] != "-":
        problems.append(f"[grouped] a legacy `-` row must NOT be materialized: {lrows[0]['required_set']!r}")
    if lseen != ["main"] or not lout["settled"]:
        problems.append(f"[grouped] legacy refresh read/settle wrong: seen={lseen!r} out={lout!r}")

    # 3b. A SETTLED HEADER MUST NOT LEAK into a new explicit-base row on a DIFFERENT base. A legacy `main` run
    #     (header already `declared:[main]`) adopts a new `v3` PR (base_branch=v3, required_set="-"). The v3
    #     group must READ v3's own requirements, never inherit the settled main header.
    mixedleg = tmp / "mixed-legacy-header.jsonl"
    mlheader = dict(ci.LEDGER.HEADER_DEFAULTS)
    mlheader.update({"run_id": "mixedleg", "base_branch": "main", "required_set": main_set})
    ci.LEDGER.dump(mixedleg, mlheader, [mrow("1", "-"), mrow("7", "v3")])  # pr1 legacy inherit, pr7 explicit v3
    mlf, mlseen = mk_fetch({"v3": "v3-test"})   # only v3 is unsettled; main header is already settled
    mlout = ci.refresh_required_set(mlf, mixedleg, "o/r")
    _mlh, mlrows = ci.LEDGER.load(mixedleg)
    mlgot = {r["pr"]: r["required_set"] for r in mlrows}
    if mlgot.get("7") != v3_set:
        problems.append(f"[grouped] a new explicit v3 row must read v3, not inherit the main header: {mlgot!r}")
    if mlgot.get("1") != "-":
        problems.append(f"[grouped] the legacy `-` row must stay inheriting, not materialized: {mlgot!r}")
    if mlseen != ["v3"] or not mlout["settled"]:
        problems.append(f"[grouped] settled main header must not be re-read; only v3: seen={mlseen!r}")

    # 3c. A SETTLED ROW IS NEVER CLOBBERED — AND ITS VALUE IS ADOPTED. pr 1 holds a settled `none`; pr 2 just
    #    joined the same base with `-`. The refresh must not touch GitHub at all (a transient FetchError here
    #    used to write `unknown` over pr 1's settled `none`, and a successful read would have overwritten it
    #    too — the same class): pr 2 ADOPTS the group's settled value, pr 1 keeps it.
    adopt = tmp / "adopt-settled.jsonl"
    ci.LEDGER.dump(adopt, dict(mheader), [mrow("1", "main", required_set="none"), mrow("2", "main")])
    aout = ci.refresh_required_set(must_not_fetch, adopt, "o/r")
    _ah, arows = ci.LEDGER.load(adopt)
    agot = {r["pr"]: r["required_set"] for r in arows}
    if agot.get("1") != "none":
        problems.append(f"[grouped] a settled row was clobbered by a group refresh: {agot!r}")
    if agot.get("2") != "none":
        problems.append(f"[grouped] a fresh row must adopt its base's settled value with no read: {agot!r}")
    if not aout["settled"]:
        problems.append(f"[grouped] the adopting group must report settled: {aout!r}")

    # 3d. DISAGREEING settled values (a hand-edit this never papers over) force a fresh read — which lands
    #    ONLY on the rows that needed it: both settled rows keep their values even though the read succeeded
    #    with a third value.
    dis = tmp / "disagree-settled.jsonl"
    ci.LEDGER.dump(dis, dict(mheader), [mrow("1", "main", required_set="none"),
                                        mrow("2", "main", required_set=v3_set),
                                        mrow("3", "main")])
    df, dseen = mk_fetch({"main": "main-test"})
    ci.refresh_required_set(df, dis, "o/r")
    _dh, drows = ci.LEDGER.load(dis)
    dgot = {r["pr"]: r["required_set"] for r in drows}
    if dgot.get("1") != "none" or dgot.get("2") != v3_set:
        problems.append(f"[grouped] a successful fresh read overwrote a settled row: {dgot!r}")
    if dgot.get("3") != main_set:
        problems.append(f"[grouped] the unsettled row must take the fresh read: {dgot!r}")
    if dseen != ["main"]:
        problems.append(f"[grouped] disagreeing settled values must force exactly one read: {dseen!r}")

    # 4. `derive` resolves the ROW's effective required set from --ledger; --required-set is an ASSERTION.
    parsed = ci.resolve_required_for_derive(str(mixed), "1", None)
    if parsed.state != ci.SNAP.parse_required_set(v3_set).state:
        problems.append(f"[derive] --ledger did not resolve pr 1's effective required set: {parsed!r}")
    ci.resolve_required_for_derive(str(mixed), "1", v3_set)   # a matching assertion passes
    try:
        ci.resolve_required_for_derive(str(mixed), "1", main_set)
        problems.append("[derive] a --required-set disagreeing with the row must fail closed")
    except SystemExit:
        pass
    legacy_parsed = ci.resolve_required_for_derive(str(legacy), "9", None)
    if legacy_parsed.state != ci.SNAP.parse_required_set(main_set).state:
        problems.append(f"[derive] a legacy `-` row must resolve through the header fallback: {legacy_parsed!r}")
    try:
        ci.resolve_required_for_derive(str(mixed), "404", None)
        problems.append("[derive] a missing row must fail closed, never default the set")
    except SystemExit:
        pass

    # 5. SINGLE-BASE TOP-LEVEL CONTRACT (regression guard). A new single-base run (one explicit-base row,
    #    header still `unknown`) that settles to `none` must report the PRE-PR top-level summary: the
    #    top-level base_branch/required_set/state describe that ONE settled base — NEVER a stale header
    #    `unknown` sitting beside settled=true, and the `state` key must be PRESENT. (Mixed-base runs keep
    #    `groups` as the signal and are covered above; there the top-level summary intentionally has no
    #    single base to report.)
    single = tmp / "single-base-toplevel.jsonl"
    sheader = dict(ci.LEDGER.HEADER_DEFAULTS)
    sheader.update({"run_id": "single", "base_branch": "main", "required_set": "unknown"})
    ci.LEDGER.dump(single, sheader, [mrow("147", "main")])

    def none_fetch(source: str, _argv: list[str]):
        return {"protection": {"enabled": False}} if source.endswith("classic") else [[]]

    sout = ci.refresh_required_set(none_fetch, single, "o/r")
    for key in ("base_branch", "repo", "required_set", "state", "settled", "reason"):
        if key not in sout:
            problems.append(f"[single-base] top-level key {key!r} dropped from the return: {sorted(sout)!r}")
    none_state = ci.SNAP.parse_required_set(ci.SNAP.NONE_DECLARED).state
    if sout.get("required_set") != ci.SNAP.NONE_DECLARED:
        problems.append(f"[single-base] top-level required_set must be the settled `none`, not a stale "
                        f"header `unknown`: {sout.get('required_set')!r}")
    if sout.get("state") != none_state:
        problems.append(f"[single-base] top-level state must report the settled base's state, not be absent: "
                        f"{sout.get('state')!r}")
    if sout.get("base_branch") != "main":
        problems.append(f"[single-base] top-level base_branch must be the single base: "
                        f"{sout.get('base_branch')!r}")
    if not sout.get("settled"):
        problems.append(f"[single-base] a single-base run that read `none` must be settled: {sout!r}")

    return problems


def required_set_matrix_cases(ci, tmp: Path) -> list[str]:
    """The EXHAUSTIVE state matrix for "the settled required set for a base B".

    B lives in TWO storage channels — the ledger HEADER (only when B IS the header base) and the explicit-base
    GROUP rows — and three review rounds each clipped one cell where they were not treated as ONE fact. This
    table pins the DECIDED value for every reachable cell of
        (base relation: row base == header base | != | legacy `-` inherits header base)
      x (row required_set: `-` | `none` | `declared:` | `unknown`)
      x (header required_set: `unknown` | `none` | `declared:`)
      x (read outcome: not-needed | success | failed).
    Delete either half of the fix and a NAMED cell below fails: the header-fold in
    `ci-status.refresh_required_set` (a `-`/`unknown` row on the header base adopts a settled header value with
    NO read, and a FAILED read never clobbers it), or the base-agreement gate in
    `ledger.effective_required_set` (a `-` row on a DIFFERENT base than the header stays `unknown`, never
    reads as the header base's set). No cell is a false green: every unread base fails closed as `unknown`.
    """
    L = ci.LEDGER
    problems: list[str] = []
    UNKNOWN = L.HEADER_DEFAULTS["required_set"]  # the fail-closed `unknown` default, from its owner

    def declared(base: str) -> str:
        return ci.canonical_required_set([(f"{base}-req", ci.SNAP.ANY_APP)])

    def run_cell(name: str, header_base: str, header_req: str, row_base: str, row_req: str, read: str) -> dict:
        """One single-row ledger driven once through `refresh_required_set`.

        `read`: `none` = assert NO GitHub read happens; `ok` = the (single) base's read succeeds with its own
        distinct declared set; `fail` = that read raises. The fetch records every base it is asked for, so
        `seen == []` mechanically proves adoption/skip rather than a read.
        """
        path = tmp / f"matrix-{name}.jsonl"
        header = dict(L.HEADER_DEFAULTS)
        header.update({"run_id": name, "base_branch": header_base, "required_set": header_req})
        row = dict(L.ROW_DEFAULTS)
        row.update({"pr": "1", "branch": "b1", "base_branch": row_base,
                    "required_set": row_req, "status": "in_review"})
        L.dump(path, header, [row])
        hb, rb0 = L.load(path)
        eff_before = L.effective_required_set(hb, rb0[0])
        target = L.effective_base(hb, rb0[0])
        seen: list[str] = []

        def fetch(source: str, _argv: list[str]):
            if source.endswith("classic"):
                seen.append(target)
            if read == "fail":
                raise ci.FetchError(f"simulated outage on {target}")
            if source.endswith("classic"):
                return {"protection": {"enabled": True, "required_status_checks":
                        {"checks": [{"context": f"{target}-req", "app_id": None}]}}}
            return [[]]

        out = ci.refresh_required_set(fetch, path, "o/r")
        ha, ra = L.load(path)
        return {"row": ra[0]["required_set"], "header": ha["required_set"], "settled": out["settled"],
                "seen": seen, "eff_before": eff_before, "eff_after": L.effective_required_set(ha, ra[0])}

    MAIN, V3 = declared("main"), declared("v3")

    # name, HB, HR, RB, RR, read -> expected row, header, settled, seen, eff_before, eff_after
    cells = [
        # --- A. ROW BASE == HEADER BASE ("main"): the header is base B's OTHER settled channel (round 3) ---
        # A `-` row adopts the settled header with NO read; a FAILED read can never clobber it (the round-3 repro).
        ("A1-adopt-none-noread",    "main", "none",         "main", "-",       "none", "none", "none",         True,  [],       "none",         "none"),
        ("A2-adopt-none-underfail", "main", "none",         "main", "-",       "fail", "none", "none",         True,  [],       "none",         "none"),
        ("A3-adopt-declared",       "main", MAIN,           "main", "-",       "none", MAIN,   MAIN,           True,  [],       MAIN,           MAIN),
        # An explicit `unknown` row on the header base HEALS from the header's settled read, no re-read.
        ("A4-unknown-adopts",       "main", "none",         "main", "unknown", "fail", "none", "none",         True,  [],       "unknown",      "none"),
        # An already-settled row with an `unknown` header needs nothing — no read, value kept.
        ("A5-settled-hdr-unknown",  "main", UNKNOWN,        "main", "none",    "none", "none", UNKNOWN,        True,  [],       "none",         "none"),
        ("A6-declared-row-kept",    "main", UNKNOWN,        "main", MAIN,      "none", MAIN,   UNKNOWN,        True,  [],       MAIN,           MAIN),
        # --- B. ROW BASE != HEADER BASE ("v3" vs "main"), MIXED (stage-3 non-goal): header NEVER leaks ---
        # A different base is read for its OWN set; before the read the `-` row is `unknown`, NOT the header's `none`.
        ("B1-mixed-reads-own",      "main", "none",         "v3",   "-",       "ok",   V3,     "none",         True,  ["v3"],   "unknown",      V3),
        ("B2-mixed-fail-closed",    "main", "none",         "v3",   "-",       "fail", UNKNOWN,"none",         False, ["v3"],   "unknown",      "unknown"),
        ("B3-mixed-declared-noleak","main", MAIN,           "v3",   "-",       "ok",   V3,     MAIN,           True,  ["v3"],   "unknown",      V3),
        # --- C. LEGACY `-` BASE ROW (inherits header base "main"): the HEADER channel, row never materialized ---
        ("C1-legacy-settles-hdr",   "main", UNKNOWN,        "-",    "-",       "ok",   "-",    MAIN,           True,  ["main"], "unknown",      MAIN),
        ("C2-legacy-hdr-settled",   "main", "none",         "-",    "-",       "none", "-",    "none",         True,  [],       "none",         "none"),
        # --- D. NEW-RUN SHAPE (header base "-", header `unknown`): explicit-base row owns the read ---
        ("D1-newrun-reads",         "-",    UNKNOWN,        "main", "-",       "ok",   MAIN,   UNKNOWN,        True,  ["main"], "unknown",      MAIN),
        ("D2-newrun-fail-closed",   "-",    UNKNOWN,        "main", "-",       "fail", UNKNOWN,UNKNOWN,        False, ["main"], "unknown",      "unknown"),
    ]
    for (name, hb, hr, rb, rr, read, e_row, e_hdr, e_settled, e_seen, e_eff_b, e_eff_a) in cells:
        got = run_cell(name, hb, hr, rb, rr, read)
        want = {"row": e_row, "header": e_hdr, "settled": e_settled, "seen": e_seen,
                "eff_before": e_eff_b, "eff_after": e_eff_a}
        for key, wanted in want.items():
            if got[key] != wanted:
                problems.append(f"[matrix {name}] {key} = {got[key]!r}, expected {wanted!r} (full: {got!r})")

    # A header value that DISAGREES with a settled ROW on the same base is a hand-edit, never papered over:
    # the header is folded in as one more settled SOURCE, so two distinct values force EXACTLY one read, which
    # lands ONLY on the unsettled `-` row; the settled row and the header both keep their values.
    dis = tmp / "matrix-header-row-disagree.jsonl"
    dh = dict(L.HEADER_DEFAULTS)
    dh.update({"run_id": "hdr-disagree", "base_branch": "main", "required_set": "none"})
    mk_row = lambda pr, rs: {**dict(L.ROW_DEFAULTS), "pr": pr, "branch": f"b{pr}",
                             "base_branch": "main", "required_set": rs, "status": "in_review"}
    L.dump(dis, dh, [mk_row("1", MAIN), mk_row("2", "-")])
    dseen: list[str] = []

    def dfetch(source: str, _argv: list[str]):
        if source.endswith("classic"):
            dseen.append("main")
            return {"protection": {"enabled": True, "required_status_checks":
                    {"checks": [{"context": "main-req", "app_id": None}]}}}
        return [[]]

    dout = ci.refresh_required_set(dfetch, dis, "o/r")
    _dh2, drows = L.load(dis)
    dgot = {r["pr"]: r["required_set"] for r in drows}
    if dgot.get("1") != MAIN:
        problems.append(f"[matrix disagree] a settled row must survive the forced read: {dgot!r}")
    if dgot.get("2") != MAIN:
        problems.append(f"[matrix disagree] the unsettled `-` row must take the fresh read: {dgot!r}")
    if dseen != ["main"]:
        problems.append(f"[matrix disagree] header-vs-row disagreement forces EXACTLY one read: {dseen!r}")
    if not dout["settled"]:
        problems.append(f"[matrix disagree] the group must settle after the read: {dout!r}")

    return problems


def liveness_cases(ci, tmp: Path) -> list[str]:
    """Drive `liveness` through every transition the derivation block defines, on a real ledger file.

    The clock is injected (`now`), so the RUNNING-STALL bound is exercised at exactly the cap, one minute
    under it, and from a fresh start — a suite that waited wall-clock hours would never run, and a suite
    that monkeypatched time inside the tool would not be testing the tool.
    """
    problems: list[str] = []
    ledger = tmp / "liveness-state.jsonl"
    sha = ci.FIXTURE_SHA
    fp1 = "a" * 64
    now = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)

    def reset(status: str = "in_review", **fields: str) -> None:
        header = dict(ci.LEDGER.HEADER_DEFAULTS)
        header["run_id"] = "test"
        row = dict(ci.LEDGER.ROW_DEFAULTS)
        row.update({"pr": "35", "head_sha": sha, "status": status})
        row.update(fields)
        ci.LEDGER.dump(ledger, header, [row])

    def row() -> dict:
        _header, rows = ci.LEDGER.load(ledger)
        return rows[0]

    def derived(verdict: str = "pending", ci_value: str = "pending", running: int = 1,
                head_sha: str = sha, fail: int = 0, unknown: int = 0) -> dict:
        trusted_current_head = verdict not in ("unusable", "unverifiable")
        return ci.derive_output({
            "head_sha": head_sha, "verdict": verdict, "ci": ci_value, "reason": "row X made it fire",
            "fingerprint": fp1 if trusted_current_head else None,
            "buckets": ({"PASS": 1, "RUNNING": running, "FAIL": fail, "UNKNOWN_VALUE": unknown}
                        if trusted_current_head else None),
        })

    def case(name: str, want: object, got: object) -> None:
        if got != want:
            problems.append(f"[liveness] {name}: {got!r}, expected {want!r}")

    # A fingerprint the ledger has not seen: MOVING — counters reset, fingerprint and ci recorded.
    reset(settled_strikes="1", ci_stalled_since="2026-07-18T00:00:00+00:00")
    out = ci.liveness(ledger, "35", derived(), "none", now)
    r = row()
    case("moving resets the counters and records fp/ci",
         ("moving", "0", "-", fp1, "pending", False),
         (out["state"], r["settled_strikes"], r["ci_stalled_since"], r["ci_fingerprint"], r["ci"],
          out["escalated"]))

    # SETTLED red at an unchanged fingerprint: strike one, then the STRIKE CAP parks it.
    reset(ci_fingerprint=fp1, ci="red")
    out = ci.liveness(ledger, "35", derived(verdict="red", ci_value="red", running=0), "none", now)
    case("settled red takes strike one", ("settled", "1", False),
         (out["state"], row()["settled_strikes"], out["escalated"]))
    out = ci.liveness(ledger, "35", derived(verdict="red", ci_value="red", running=0), "none", now)
    r = row()
    case("the STRIKE CAP escalates: parked, ruling voided",
         (True, "awaiting-user", "-", "2", True),
         (out["escalated"], r["status"], r["blocker_ruling"], r["settled_strikes"],
          "STRIKE CAP" in r["ci_reason"]))

    # A machine action due suppresses the strike and clears the stall clock — and only that derivation.
    reset(ci_fingerprint=fp1, ci="red", settled_strikes="1",
          ci_stalled_since="2026-07-18T00:00:00+00:00")
    out = ci.liveness(ledger, "35", derived(verdict="red", ci_value="red", running=0), "due", now)
    r = row()
    case("a due machine action stops the bounds",
         ("machine-action", "1", "-", False),
         (out["state"], r["settled_strikes"], r["ci_stalled_since"], out["escalated"]))

    # RUNNING-STALL: the clock starts at the first motionless derivation, escalates at the CAP — and a
    # slow-but-alive check one minute under the cap does NOT park.
    reset(ci_fingerprint=fp1)
    out = ci.liveness(ledger, "35", derived(running=1), "none", now)
    r = row()
    case("the stall clock starts on disk",
         ("running-stall", now.isoformat(timespec="seconds"), "0"),
         (out["state"], r["ci_stalled_since"], r["settled_strikes"]))
    out = ci.liveness(ledger, "35", derived(running=1), "none",
                      now + timedelta(hours=5, minutes=59))
    case("slow is not dead: under the CAP nothing fires", False, out["escalated"])
    out = ci.liveness(ledger, "35", derived(running=1), "none", now + timedelta(hours=6))
    r = row()
    case("the CI STALL CAP escalates",
         (True, "awaiting-user", True),
         (out["escalated"], r["status"], "STALL CAP" in r["ci_reason"]))

    # NOT VERIFIED: UNUSABLE and UNVERIFIABLE keep their exact verdicts while sharing one neutral
    # liveness state, one persisted counter, and one cap. Only a trusted current-head result resets it.
    reset()
    out = ci.liveness(ledger, "35", derived(verdict="unusable"), "none", now)
    case("UNUSABLE enters the neutral liveness class without losing its verdict",
         ("not-verified", "unusable", "1", False),
         (out["state"], out["verdict"], row()["unusable_refetches"], out["escalated"]))
    out = ci.liveness(ledger, "35", derived(verdict="unverifiable"), "none", now)
    case("UNVERIFIABLE shares the counter without losing its verdict",
         ("not-verified", "unverifiable", "2", False),
         (out["state"], out["verdict"], row()["unusable_refetches"], out["escalated"]))
    out = ci.liveness(ledger, "35", derived(verdict="unverifiable"), "none", now)
    r = row()
    case("UNVERIFIABLE at the REFETCH CAP names the exact verdict",
         ("not-verified", "unverifiable", True, "3", True),
         (out["state"], out["verdict"], out["escalated"], r["unusable_refetches"],
          "UNVERIFIABLE at the REFETCH CAP" in r["ci_reason"]))

    # Reach the same cap from the checked-in witness-identity fixture, not a synthesized reason. This
    # refusal identifies the duplicated witness and containment failure, but has no VERIFY rule or row.
    duplicate_path = (ci.SNAPSHOT_PY.parent / "fixtures" / "ci-snapshot"
                      / "duplicate-witness-id.jsonl")
    duplicate_verdict, duplicate_refusal = ci.SNAP.evaluate(
        duplicate_path, sha, required=ci.SNAP.NO_REQUIRED, expect_filename_sha=False
    )
    duplicate_derived = ci.derive_output({
        "head_sha": sha,
        "verdict": duplicate_verdict,
        "ci": "pending",
        "reason": duplicate_refusal,
        "fingerprint": None,
        "buckets": None,
    })
    reset(unusable_refetches="2")
    out = ci.liveness(ledger, "35", duplicate_derived, "none", now)
    r = row()
    expected_refusal = (
        "witness identity is not unique "
        "(https://github.com/lestrrat-ai/claude-code-plugins/actions/runs/29263565055/job/1) "
        "— containment cannot be proven"
    )
    expected_cap_reason = (
        f"UNVERIFIABLE at the REFETCH CAP — 3 consecutive not-verified derivations at head {sha} "
        f"yielded no trusted current-head evidence. Last refusal: {expected_refusal}"
    )
    case("witness-identity refusal reaches the cap without invented row detail",
         ("unverifiable", expected_refusal, True, "awaiting-user", expected_cap_reason),
         (duplicate_verdict, duplicate_refusal, out["escalated"], r["status"], r["ci_reason"]))

    reset(unusable_refetches="2")
    out = ci.liveness(ledger, "35", derived(), "none", now)
    case("trusted current-head evidence resets the refetch counter", "0", row()["unusable_refetches"])

    # The cross-step case a synthesized liveness result cannot prove: derive retains a verified artifact
    # for the requested head, then the moved-head override makes the final result untrusted. Liveness must
    # count that unedited result even though its audit artifact exists.
    moved_fx, moved, moved_rundir, _before = run_fixture(
        ci, "head-moves-mid-fetch.json", tmp / "moved-head-liveness"
    )
    moved_artifacts = artifact_state(moved_rundir)
    reset()
    moved_calls = []
    for _call in range(3):
        out = ci.liveness(ledger, moved_fx.get("pr", "35"), ci.derive_output(moved), "none", now)
        moved_calls.append((row()["unusable_refetches"], out["escalated"],
                            artifact_state(moved_rundir)))
    r = row()
    moved_snapshot = Path(moved["snapshot"]) if moved["snapshot"] is not None else None
    case("a retained moved-head artifact reaches the refetch cap without changing bytes",
         (True, True, None, None, "not-verified",
          [("1", False, moved_artifacts), ("2", False, moved_artifacts),
           ("3", True, moved_artifacts)]),
         (moved["head_moved"], moved_snapshot is not None and moved_snapshot.is_file(),
          moved["fingerprint"], moved["buckets"], out["state"], moved_calls))
    case("the moved-head cap names missing trusted current-head evidence",
         (True, "awaiting-user"),
         ("no trusted current-head evidence" in r["ci_reason"], r["status"]))

    # A HELD row is observed, never struck: `ci` lands, the counters and the open park question do not
    # move, and no second park can overwrite the first.
    reset(status="awaiting-user", ci_reason="the open question", ci_fingerprint=fp1,
          settled_strikes="1")
    out = ci.liveness(ledger, "35", derived(verdict="red", ci_value="red", running=0), "none", now)
    r = row()
    case("a held row is observed, never struck",
         ("held", "red", "1", "the open question", "-", False),
         (out["state"], r["ci"], r["settled_strikes"], r["ci_reason"], r["blocker_ruling"],
          out["escalated"]))

    # A STALE derivation is refused outright — exit 2, and the row is untouched.
    reset(head_sha="0" * 40, settled_strikes="1")
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            ci.liveness(ledger, "35", derived(), "none", now)
            problems.append("[liveness] a derivation pinned to a superseded head was RECORDED — it must "
                            "be refused so the new head's budget is not spent on old evidence")
        except SystemExit as exc:
            r = row()
            case("a stale derivation is refused, nothing written",
                 (2, "1", "pending"), (exc.code, r["settled_strikes"], r["ci"]))

    # GREEN settles without striking: the strike rule is an explicit {red, pending} membership test.
    reset(ci_fingerprint=fp1)
    out = ci.liveness(ledger, "35", derived(verdict="green", ci_value="green", running=0), "none", now)
    case("green never strikes", ("settled", "0", False),
         (out["state"], row()["settled_strikes"], out["escalated"]))

    # An UNCLASSIFIED verdict parks ON THIS derivation — no cap, no strike, the value itself is the blocker.
    reset()
    out = ci.liveness(ledger, "35", derived(verdict="unclassified", running=0), "none", now)
    r = row()
    case("an unknown value parks immediately",
         ("unknown-value", True, "awaiting-user", "-", True),
         (out["state"], out["escalated"], r["status"], r["blocker_ruling"],
          "UNKNOWN VALUE" in r["ci_reason"]))

    # WATCH WARRANTED — the mechanical reduction of "WATCH ONLY WHAT CAN MOVE", emitted so the driver never
    # reads that table by hand: trusted-current-head AND verdict != unclassified AND buckets.RUNNING > 0.
    # Each case is one row of the WATCH table, asserted against `watch_warranted` and `watch_reason`.
    def watch(name: str, warranted: bool, needle: str, derv: dict, **reset_fields: str) -> None:
        reset(**reset_fields)
        out = ci.liveness(ledger, "35", derv, "none", now)
        case(name, (warranted, True), (out["watch_warranted"], needle in out["watch_reason"]))

    watch("pending with RUNNING rows warrants a watch", True, "still RUNNING",
          derived(running=2), ci_fingerprint=fp1)
    watch("settled red warrants no watch — nothing can move", False, "nothing can move",
          derived(verdict="red", ci_value="red", running=0), ci_fingerprint=fp1, ci="red")
    # red with a still-RUNNING row (a FAIL row AND a RUNNING row in buckets): the fix moves it, but the
    # RUNNING row can still move, so the watch IS warranted — the WATCH table's red split, verbatim.
    watch("red with a still-RUNNING row warrants a watch", True, "still RUNNING",
          derived(verdict="red", ci_value="red", running=1, fail=1), ci_fingerprint=fp1, ci="red")
    watch("green warrants no watch", False, "nothing can move",
          derived(verdict="green", ci_value="green", running=0), ci_fingerprint=fp1)
    watch("an unusable derivation warrants no watch — no trusted current-head evidence",
          False, "no trusted current-head evidence", derived(verdict="unusable"))
    watch("an unverifiable derivation warrants no watch — no trusted current-head evidence",
          False, "no trusted current-head evidence", derived(verdict="unverifiable"))
    # THE COUNTEREXAMPLE the exclusion exists for: `decide()` ranks UNKNOWN_VALUE above plain `pending`,
    # so an UNCLASSIFIED verdict can carry a still-RUNNING row (buckets RUNNING>0 AND UNKNOWN_VALUE>0). A
    # bare RUNNING>0 reading would warrant a watch; the park is the resolution, so watch_warranted is
    # FALSE. Delete the `verdict != unclassified` term and this case goes red.
    watch("unclassified with a RUNNING row warrants NO watch — the park is the resolution",
          False, "park is the resolution",
          derived(verdict="unclassified", ci_value="pending", running=1, unknown=1))
    # A HELD row does not change the watch decision — parking never stops a warranted watch nor starts an
    # unwarranted one. Same RUNNING>0 pending derivation, on a parked row -> still warranted.
    watch("a held row with a RUNNING row still warrants a watch", True, "still RUNNING",
          derived(running=1), status="awaiting-user", ci_reason="the open question", ci_fingerprint=fp1)

    return problems


def verdict_doc_cases(ci) -> list[str]:
    """Pin both no-fingerprint verdicts in the DECIDE doc order independently of `doc-check`."""
    problems: list[str] = []
    order = ci.parse_decide_order(ci.SPEC_DOC.read_text(encoding="utf-8"))
    if order != ci.DECIDE_ORDER:
        problems.append(f"[verdict docs] parsed DECIDE order {order!r}, expected {ci.DECIDE_ORDER!r}")
    got = tuple(name for name in order if name in ci.NOT_VERIFIED_DECIDE_NAMES)
    if got != ci.NOT_VERIFIED_DECIDE_NAMES:
        problems.append(
            f"[verdict docs] not-verified outcomes are {got!r}, expected "
            f"{ci.NOT_VERIFIED_DECIDE_NAMES!r}"
        )
    return problems


def run(ci, tmp: Path) -> int:
    """Every fixture, then the seams, then `doc-check`. Non-zero on any failure.

    `ci` is the ALREADY-LOADED `ci-status.py` module — handed in by its `self-test`, so the tool under test
    is loaded exactly once and the code these fixtures drive is the code that command would run.
    """
    failures = 0
    names = cases(ci)
    # A SUITE WITH NOTHING IN IT PASSES VACUOUSLY, which is this tool's founding defect turned on itself.
    if not names:
        print(f"FAIL     no fixtures in {ci.FIXTURES} — a suite with nothing in it passes VACUOUSLY, and "
              f"zero evidence is not green")
        return 1
    for name in names:
        fx, got, rundir, artifacts_before = run_fixture(ci, name, tmp)
        bad = check_fixture(name, got, fx, rundir, artifacts_before)
        if not bad:
            print(f"ok       {name:32} -> {got['verdict']:14} ci={got['ci']:8} ({fx['why']})")
        else:
            failures += 1
            for b in bad:
                print(f"FAIL     {name:32} {b}")

    problems = check_seams(ci, tmp)
    for problem in problems:
        failures += 1
        print(f"FAIL     {problem}")
    if not problems:
        print(f"ok       {'the seams no fixture reaches':32} -> {len(SEAM_EXPECT)} cases: gh_fetch's own two "
              f"rules, the CLI's operator-error guards, the repo-scoping refusal, and the fingerprint "
              f"canonicalization")

    required_problems = required_set_cases(ci, tmp)
    for problem in required_problems:
        failures += 1
        print(f"FAIL     {problem}")
    if not required_problems:
        print(f"ok       {'required-set producer':32} -> both APIs, strict shapes, canonical ledger state, "
              f"grouped per-base refresh, and derive's row-based resolution")

    required_cli_problems = required_set_cli_cases(ci, tmp)
    for problem in required_cli_problems:
        failures += 1
        print(f"FAIL     {problem}")
    if not required_cli_problems:
        print(f"ok       {'required-set CLI exits':32} -> settled=0, unknown=1, caller/store errors=2; "
              f"errors preserve the ledger and emit one diagnostic without a traceback")

    liveness_problems = liveness_cases(ci, tmp)
    for problem in liveness_problems:
        failures += 1
        print(f"FAIL     {problem}")
    if not liveness_problems:
        print(f"ok       {'liveness bookkeeping':32} -> every transition of the derivation block: strikes "
              f"to the cap, the stall clock, the refetch cap, machine-action stop, held observation, "
              f"both exact not-verified verdicts, stale-head refusal, retained moved-head artifact, and "
              f"the watch_warranted reduction (incl. the UNCLASSIFIED exclusion)")

    verdict_doc_problems = verdict_doc_cases(ci)
    for problem in verdict_doc_problems:
        failures += 1
        print(f"FAIL     {problem}")
    if not verdict_doc_problems:
        print(f"ok       {'DECIDE verdict terminology':32} -> UNUSABLE and UNVERIFIABLE both remain explicit "
              f"before liveness groups them")

    print()
    print(f"--- doc-check: {ci.SPEC_DOC.name} + {ci.DRIVER_DOC.name} vs the code that runs ---")
    failures += ci.doc_check()
    if failures:
        print(f"\n{failures} check(s) FAILED.")
        return 1
    print(f"\nall {len(names)} fixtures hold, and the doc agrees with the code.")
    return 0


def main() -> int:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        return run(load_status_module(), Path(tmp))


if __name__ == "__main__":
    raise SystemExit(main())
