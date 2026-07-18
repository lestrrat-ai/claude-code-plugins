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
import re
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


def run_fixture(ci, name: str, tmp: Path) -> tuple[dict, dict]:
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
    required = ci.SNAP.parse_required_set(fx["required_set"])
    return fx, ci.derive(ci.fixture_fetch(fx), "o/r", fx.get("pr", "35"), head_sha, rundir, required)


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
        bad.append("an artifact was PROMOTED for a fetch that FAILED — a later heartbeat would read it as evidence")
    # THE FINGERPRINT INVARIANT HOLDS ON EVERY FIXTURE, no per-fixture expectation needed: a VERIFIED
    # snapshot carries the sha256 the driver compares to `ci_fingerprint`, and an untrusted one carries
    # `null` — nothing rejected is ever hashed, so no strike can accrue against rows nobody believed.
    fp = got.get("fingerprint", "ABSENT")
    if fp == "ABSENT":
        bad.append("derive emitted NO `fingerprint` field — the driver would be back to hashing by hand")
    elif got["verdict"] in ("unusable", "unverifiable"):
        if fp is not None:
            bad.append(f"fingerprint {fp!r} on an untrusted ({got['verdict']}) snapshot — nothing rejected "
                       f"is ever hashed")
    elif fp is None or not re.fullmatch(r"[0-9a-f]{64}", fp):
        bad.append(f"fingerprint {fp!r} on a VERIFIED snapshot — expected the 64-hex sha256 of its "
                   f"evidence rows")
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
                head_sha: str = sha) -> dict:
        verified = verdict not in ("unusable", "unverifiable")
        return ci.derive_output({
            "head_sha": head_sha, "verdict": verdict, "ci": ci_value, "reason": "row X made it fire",
            "fingerprint": fp1 if verified else None,
            "buckets": ({"PASS": 1, "RUNNING": running, "FAIL": 0, "UNKNOWN_VALUE": 0}
                        if verified else None),
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

    # UNUSABLE: its own counter, its own cap — and any verified outcome resets it.
    reset()
    for expect in ("1", "2"):
        out = ci.liveness(ledger, "35", derived(verdict="unusable"), "none", now)
        case(f"unusable refetch {expect} does not escalate", (expect, False),
             (row()["unusable_refetches"], out["escalated"]))
    out = ci.liveness(ledger, "35", derived(verdict="unusable"), "none", now)
    r = row()
    case("the REFETCH CAP escalates",
         (True, "3", True), (out["escalated"], r["unusable_refetches"], "REFETCH CAP" in r["ci_reason"]))
    reset(unusable_refetches="2")
    out = ci.liveness(ledger, "35", derived(), "none", now)
    case("a verified snapshot resets the refetch counter", "0", row()["unusable_refetches"])

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
        fx, got = run_fixture(ci, name, tmp)
        bad = check_fixture(name, got, fx)
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
        print(f"ok       {'required-set producer':32} -> 10 cases: both APIs, strict shapes, canonical ledger state")

    liveness_problems = liveness_cases(ci, tmp)
    for problem in liveness_problems:
        failures += 1
        print(f"FAIL     {problem}")
    if not liveness_problems:
        print(f"ok       {'liveness bookkeeping':32} -> every transition of the derivation block: strikes "
              f"to the cap, the stall clock, the refetch cap, machine-action stop, held observation, "
              f"stale-head refusal")

    print()
    print(f"--- doc-check: {ci.DOC.name} vs the code that runs ---")
    failures += ci.doc_check(ci.DOC)
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
