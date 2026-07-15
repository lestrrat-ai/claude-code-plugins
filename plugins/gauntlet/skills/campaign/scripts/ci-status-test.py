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
import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
STATUS_PY = HERE / "ci-status.py"


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
        bad.append("an artifact was PROMOTED for a fetch that FAILED — a later wake would read it as evidence")
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
              f"rules, the CLI's operator-error guards, and the repo-scoping refusal")

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
