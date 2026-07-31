#!/usr/bin/env python3
"""Fixtures for `reconcile.py` — canonical snapshot fetch + ledger FACT detection.

They live in a SIBLING file, and `reconcile.py self-test` FAILS LOUDLY if it cannot load them.

EVERY FIXTURE HAS TEETH. It asserts the EXACT facts object — not just that a key exists but its whole
shape — and, for refusals, the EXACT exit code (2, fail closed), that stdout is EMPTY (no facts leak from
a refused run), and that stderr NAMES the specific thing wrong (the missing field, the foreign label). A
suite that only checked `code == 0` would pass against a detector that emitted the wrong facts, and the
facts are what the skill routes on.

Three decisions this suite PINS, because they are the ones a reader would otherwise have to guess:
- **A TERMINAL row is not compared at all.** `merged`/`aborted` rows emit `{"terminal": status}` and
  nothing else — even when the snapshot still shows the PR (a reopened-after-merge oddity). Presence is not
  reported, absence is not reported, no change is computed. The fixtures drive both branches.
- **`absent_from_snapshot` is a FACT, never an error.** A live row missing from the snapshot exits 0 with
  `{"absent_from_snapshot": true}` — the merged/closed-by-absence signal — not a non-zero "PR vanished".
- **An unlabelled entry is answered DIFFERENTLY by the two commands, and both halves are pinned.** `fetch`
  SKIPS it as a stale-search-index ghost, reports it, and promotes the rest; `detect` refuses the WHOLE
  file, because only there can the snapshot and the run-id have come from different runs. One fixture
  drives both over the SAME bytes, so neither half can drift into the other.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from _gauntlet.modules import load_sibling
from _gauntlet.testing import capture_cli, checker

OWNER = Path(__file__).resolve().parent / "reconcile.py"


M = load_sibling("reconcile_owner", OWNER.parent, OWNER.name)
LED = M.L                                   # the ledger schema owner reconcile reuses

RUN_ID = "grec-0001"
RUN_LABEL = M.RUN_LABEL_PREFIX + RUN_ID
REVIEWING = M.REVIEWING_LABEL
ACCEPTED = M.ACCEPTED_LABEL
SHA_A = "a" * 40
SHA_B = "b" * 40

_UNSET = object()


check = checker(M.SelfTestFailure)


# --- fixture builders ---------------------------------------------------------

# `headRefOid` is typed to admit None on purpose: the null-canonical-field fixture builds a
# deliberately malformed entry to prove the refusal, and that null IS the input under test.
def entry(number, *, head=None, headRefOid: "str | None" = SHA_A, title=None, base="main", state="OPEN",
          mergeable="MERGEABLE", mergeStateStatus="CLEAN", label_names=None, raw_labels=_UNSET) -> dict:
    """A canonical `prs.json` entry. Defaults carry the run label + `gauntlet-reviewing`, so the happy path
    passes the run-scope check; `label_names`/`raw_labels` override for the scope and malformed fixtures."""
    head = head if head is not None else f"branch-{number}"
    title = title if title is not None else f"title-{number}"
    if raw_labels is not _UNSET:
        labels = raw_labels
    else:
        if label_names is None:
            label_names = [RUN_LABEL, REVIEWING]
        labels = [{"name": n} for n in label_names]
    return {"number": number, "headRefName": head, "headRefOid": headRefOid, "title": title,
            "baseRefName": base, "state": state, "mergeable": mergeable,
            "mergeStateStatus": mergeStateStatus, "labels": labels}


def row(pr, *, branch=None, head_sha=SHA_A, status="in_review", **over) -> dict:
    branch = branch if branch is not None else f"branch-{pr}"
    return dict(LED.ROW_DEFAULTS, pr=str(pr), id=f"pr{pr}", branch=branch,
                head_sha=head_sha, status=status, **over)


def build_ledger(tmp, rows, *, base_branch="main", run_id=RUN_ID) -> Path:
    ledger = Path(tmp) / "state.jsonl"
    header = dict(LED.HEADER_DEFAULTS, run_id=run_id, base_branch=base_branch)
    LED.dump(ledger, header, rows)
    return ledger


def build_prs(tmp, entries) -> Path:
    prs = Path(tmp) / "prs.json"
    prs.write_text(json.dumps(entries) if not isinstance(entries, str) else entries, encoding="utf-8")
    return prs


def run(ledger: Path, prs: Path, run_id=RUN_ID) -> "tuple[int, dict, str]":
    """Drive `detect` through the CLI and return (exit code, parsed stdout, stderr).

    Every caller here is a SUCCESS-path fixture that goes straight on to subscript the parse, so empty or
    unparseable stdout is a fixture failure and is raised as one. Returning `None` for it, as this used to,
    only deferred the report to a confusing subscript of nothing several lines later. The refusal fixtures
    do not come through here: they drive `capture_cli` themselves and assert on stderr.
    """
    code, out, err = capture_cli(
        M.main, ["detect", "--ledger", str(ledger), "--prs", str(prs), "--run-id", run_id])
    if not out.strip():
        raise M.SelfTestFailure(f"detect printed nothing on stdout (exit {code}, stderr {err!r})")
    return code, json.loads(out), err


def scenario(rows, entries, *, base_branch="main", run_id=RUN_ID):
    """One temp dir holding a ledger + a prs.json, run through the CLI. Returns (code, parsed, err)."""
    with tempfile.TemporaryDirectory() as d:
        ledger = build_ledger(d, rows, base_branch=base_branch)
        prs = build_prs(d, entries)
        return run(ledger, prs, run_id=run_id)


def fetch_paths(tmp: str, *, hostile: bool = False) -> "tuple[Path, Path]":
    root_name = "project with space\nand newline" if hostile else "project"
    output_name = "prs ; $(never-executed).json" if hostile else "prs.json"
    project_root = Path(tmp) / root_name
    output = project_root / ".gauntlet" / "tmp" / "run dir" / output_name
    output.parent.mkdir(parents=True)
    return project_root, output


def response_bytes(entries) -> bytes:
    return (json.dumps(entries, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class RecordedRunner:
    def __init__(self, stdout: bytes, returncode: int, stderr: bytes) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv: list[str], **kwargs) -> "subprocess.CompletedProcess[bytes]":
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv, self.returncode, stdout=self.stdout, stderr=self.stderr)


def completed(stdout: bytes, *, returncode: int = 0, stderr: bytes = b"") -> RecordedRunner:
    return RecordedRunner(stdout, returncode, stderr)


def expect_fetch_refusal(project_root: Path, output: Path, run_id: str, runner, needle: str) -> None:
    try:
        M.fetch_snapshot(project_root, output, run_id, runner=runner)
    except M.Refusal as exc:
        check(needle in str(exc), f"refusal did not name {needle!r}: {exc}")
    else:
        raise M.SelfTestFailure(f"fetch unexpectedly accepted response; wanted refusal naming {needle!r}")


# --- canonical fetch ---------------------------------------------------------

def t_fetch_exact_argv_and_raw_bytes():
    hostile_run_id = "run with space;$(never-executed)\nand newline"
    hostile_label = M.RUN_LABEL_PREFIX + hostile_run_id
    item = entry(41, title="snowman ☃", label_names=[hostile_label, REVIEWING])
    payload = response_bytes([item])
    with tempfile.TemporaryDirectory() as d:
        project_root, output = fetch_paths(d, hostile=True)
        runner = completed(payload)
        count = M.fetch_snapshot(project_root, output, hostile_run_id, runner=runner)

        expected = [
            "gh", "pr", "list",
            "--label", hostile_label,
            "--state", "open",
            "--limit", "1000",
            "--json", "number,headRefName,headRefOid,title,baseRefName,state,mergeable,mergeStateStatus,labels",
        ]
        check(runner.calls == [(expected, {
            "cwd": project_root, "capture_output": True, "check": False,
        })], f"fetch argv/cwd drifted or passed through a shell: {runner.calls!r}")
        check(count == 1, f"one response row must yield count 1, got {count}")
        check(output.read_bytes() == payload,
              "fetch did not promote the exact captured stdout bytes (Unicode or whitespace changed)")
        check(not (project_root / "never-executed").exists(),
              "hostile argv/path text was interpreted instead of passed as data")


def t_fetch_malformed_and_missing_field_preserve_old():
    with tempfile.TemporaryDirectory() as d:
        project_root, output = fetch_paths(d)
        old = b"previous snapshot\n"
        output.write_bytes(old)
        expect_fetch_refusal(project_root, output, RUN_ID, completed(b"not json\n"), "not valid JSON")
        check(output.read_bytes() == old, "malformed JSON replaced the previous snapshot")

        missing = entry(41)
        del missing["headRefOid"]
        expect_fetch_refusal(project_root, output, RUN_ID, completed(response_bytes([missing])),
                             "headRefOid")
        check(output.read_bytes() == old, "missing-field response replaced the previous snapshot")


def t_fetch_non_utf8_preserves_old():
    with tempfile.TemporaryDirectory() as d:
        project_root, output = fetch_paths(d)
        output.write_bytes(b"old")
        expect_fetch_refusal(project_root, output, RUN_ID, completed(b"\xff\xfe"), "not UTF-8")
        check(output.read_bytes() == b"old", "non-UTF-8 response replaced the previous snapshot")


def t_fetch_skips_ghost_and_promotes_the_rest():
    # THE GHOST: `gh pr list --label X` compiles to the issues search index, which does not durably apply
    # label REMOVALS, so it keeps returning a PR whose own `labels` (from the primary DB) no longer carry
    # the run label. fetch cannot be mis-scoped — its selector and its validator are the same `run_id` —
    # so it skips the ghost and promotes the run's real PRs instead of losing the whole response.
    with tempfile.TemporaryDirectory() as d:
        project_root, output = fetch_paths(d)
        ghost = entry(201, label_names=[])                 # label REMOVED; the index has not caught up
        live = entry(199)
        ghosts: list[dict] = []
        count = M.fetch_snapshot(project_root, output, RUN_ID,
                                 runner=completed(response_bytes([ghost, live])), ghosts=ghosts)
        check(count == 1, f"the labelled remainder must still be promoted, got count {count}")
        promoted = json.loads(output.read_text(encoding="utf-8"))
        check([e["number"] for e in promoted] == [199],
              f"the promoted snapshot must hold the labelled entry ONLY, got {promoted!r}")
        check(ghosts == [{"index": 0, "number": 201, "labels": []}],
              f"the skip must be recorded with WHY it was skipped (its labels), got {ghosts!r}")
        # The promoted file is ghost-free, so `detect` — whose refusal is deliberately unchanged — reads it.
        ledger = build_ledger(output.parent, [row(199)])
        facts = M.detect(ledger, output, RUN_ID)
        check(facts["rows"]["199"]["absent_from_snapshot"] is False,
              f"detect must consume the ghost-free promotion, got {facts['rows']['199']!r}")


def t_fetch_skips_another_runs_ghost():
    # A ghost belonging to ANOTHER run is self-identifying: its labels name that other run. Skipping it is
    # right for the same reason — this query asked for OUR label, so the entry is the index disagreeing.
    with tempfile.TemporaryDirectory() as d:
        project_root, output = fetch_paths(d)
        foreign = entry(41, label_names=["gauntlet-run-other", REVIEWING])
        ghosts: list[dict] = []
        count = M.fetch_snapshot(project_root, output, RUN_ID,
                                 runner=completed(response_bytes([foreign, entry(199)])), ghosts=ghosts)
        check(count == 1, f"the foreign ghost must not cost the run its own row, got count {count}")
        check(ghosts == [{"index": 0, "number": 41, "labels": ["gauntlet-run-other", REVIEWING]}],
              f"the skip must carry the OTHER run's label as its reason, got {ghosts!r}")
        check([e["number"] for e in json.loads(output.read_text(encoding="utf-8"))] == [199],
              "the foreign ghost was promoted into this run's snapshot")


def t_fetch_reports_every_skip():
    # A skip can NEVER be silent: stderr names it, and `skipped_unlabelled` is in the result JSON. The key
    # is present even on a clean fetch, so a reader tests its contents, never a missing key.
    with tempfile.TemporaryDirectory() as d:
        project_root, output = fetch_paths(d)

        def cli_over(payload: bytes) -> "tuple[int, str, str]":
            # `cmd_fetch` OWNS the reporting under test, and only the gh call is stubbed: the real
            # `fetch_snapshot` underneath still validates, skips, and promotes.
            return capture_cli(
                lambda _argv: M.cmd_fetch(project_root, output, RUN_ID, runner=completed(payload)), [])

        code, out, err = cli_over(response_bytes([entry(201, label_names=[]), entry(199)]))
        _clean_code, clean_out, _clean_err = cli_over(response_bytes([entry(199)]))
    check(code == 0, f"a skipped ghost is not a failure; fetch must exit 0, got {code} (stderr {err!r})")
    result = json.loads(out)
    check(result["entries"] == 1, f"the result must count the PROMOTED rows, got {result!r}")
    check(result["skipped_unlabelled"] == [{"index": 0, "number": 201, "labels": []}],
          f"the result JSON must carry the skip and its reason, got {result!r}")
    check("201" in err and "search index" in err and RUN_LABEL in err,
          f"stderr must name the skipped PR, the run label, and WHY it was skipped, got {err!r}")
    check(json.loads(clean_out)["skipped_unlabelled"] == [],
          f"a clean fetch must still carry an EMPTY skipped_unlabelled, got {clean_out!r}")


def t_fetch_all_ghosts_promotes_an_empty_snapshot():
    # EVERY entry a ghost — what a run looks like after its last PR's labels were removed (an abort or a
    # merge). The right answer is an EMPTY snapshot, not a refusal: absence is the terminal signal, and
    # refusing here would deny the run the very completion signal loop-control gates the finished branch
    # on. Promoting `[]` is also what a genuinely empty run promotes, so detect needs no special case.
    with tempfile.TemporaryDirectory() as d:
        project_root, output = fetch_paths(d)
        output.write_bytes(b"stale")
        ghosts: list[dict] = []
        count = M.fetch_snapshot(project_root, output, RUN_ID, ghosts=ghosts,
                                 runner=completed(response_bytes([entry(201, label_names=[]),
                                                                  entry(199, label_names=[REVIEWING])])))
        check(count == 0, f"an all-ghost response promotes nothing, got count {count}")
        check(json.loads(output.read_text(encoding="utf-8")) == [],
              f"an all-ghost response must promote an EMPTY array, got {output.read_bytes()!r}")
        check([g["number"] for g in ghosts] == [201, 199], f"every ghost must be reported, got {ghosts!r}")
        ledger = build_ledger(output.parent, [row(199)])
        facts = M.detect(ledger, output, RUN_ID)
        check(facts["rows"]["199"] == {"absent_from_snapshot": True},
              f"an all-ghost snapshot reads as plain absence, got {facts['rows']['199']!r}")


def t_fetch_ghost_still_refused_by_detect():
    # THE ASYMMETRY, on ONE set of bytes. fetch skips the ghost; the SAME snapshot handed to detect through
    # --prs is refused whole. detect's --prs and --run-id are independent arguments, so an unlabelled entry
    # there can mean a run was pointed at another run's file — the isolation violation the label prevents.
    payload = response_bytes([entry(201, label_names=[]), entry(199)])
    with tempfile.TemporaryDirectory() as d:
        project_root, output = fetch_paths(d)
        ghosts: list[dict] = []
        M.fetch_snapshot(project_root, output, RUN_ID, runner=completed(payload), ghosts=ghosts)
        check(len(ghosts) == 1, f"fetch must skip the ghost, got {ghosts!r}")

        unfiltered = output.parent / "handed-in.json"
        unfiltered.write_bytes(payload)
        ledger = build_ledger(output.parent, [row(199)])
        code, out, err = capture_cli(M.main, [
            "detect", "--ledger", str(ledger), "--prs", str(unfiltered), "--run-id", RUN_ID])
    check(code == 2, f"detect must still refuse the WHOLE mis-scoped file, got {code}")
    check(out.strip() == "", f"a refused detect leaks no facts, got {out!r}")
    check("run-isolation" in err, f"detect's refusal must still name the isolation property, got {err!r}")


def t_fetch_ghost_is_validated_not_skipped_instead():
    # A GHOST IS VALIDATED AND *THEN* SKIPPED — never skipped INSTEAD of validated. The skip relaxes the
    # scope check and nothing else, so a ghost from a DRIFTED command (a `--json` set that lost a field, a
    # field at the wrong shape) still refuses the WHOLE response. Only the ORDER inside `_validated_entries`
    # makes that true: validate every canonical field, then decide the entry's outcome.
    with tempfile.TemporaryDirectory() as d:
        project_root, output = fetch_paths(d)
        old = b"previous snapshot\n"
        output.write_bytes(old)

        missing = entry(201, label_names=[])
        del missing["headRefName"]
        expect_fetch_refusal(project_root, output, RUN_ID,
                             completed(response_bytes([missing, entry(199)])), "headRefName")
        check(output.read_bytes() == old, "a ghost missing a canonical field replaced the old snapshot")

        wrong_shape = entry(201, headRefOid=12345, label_names=[])  # type: ignore[arg-type]
        expect_fetch_refusal(project_root, output, RUN_ID,
                             completed(response_bytes([wrong_shape, entry(199)])), "headRefOid")
        check(output.read_bytes() == old, "a wrong-shaped ghost field replaced the old snapshot")


def t_fetch_ghost_duplicate_number_refused():
    # A ghost registers its `number` like any other entry, so a response naming one PR twice is refused
    # even when one of the two is unlabelled. Dropping the ghost's registration would make the ambiguity
    # depend on which of the pair the search index went stale on.
    with tempfile.TemporaryDirectory() as d:
        project_root, output = fetch_paths(d)
        output.write_bytes(b"old")
        payload = response_bytes([entry(199, label_names=[]), entry(199)])
        expect_fetch_refusal(project_root, output, RUN_ID, completed(payload),
                             "lists PR #199 at both entry #0 and #1")
        check(output.read_bytes() == b"old", "a duplicate naming a ghost replaced the old snapshot")


def t_fetch_limit_boundary_refused():
    with tempfile.TemporaryDirectory() as d:
        project_root, output = fetch_paths(d)
        output.write_bytes(b"old")
        rows = [entry(n) for n in range(M.SNAPSHOT_LIMIT)]
        expect_fetch_refusal(project_root, output, RUN_ID, completed(response_bytes(rows)), "may be truncated")
        check(output.read_bytes() == b"old", "limit-boundary response replaced the previous snapshot")


def t_fetch_limit_boundary_counts_ghosts():
    # The truncation guard counts what the QUERY RETURNED, not the survivors. Counting survivors would let
    # a capped response carrying one ghost slip under the boundary, and absence read off a truncated list
    # is not evidence.
    with tempfile.TemporaryDirectory() as d:
        project_root, output = fetch_paths(d)
        output.write_bytes(b"old")
        rows = [entry(n) for n in range(M.SNAPSHOT_LIMIT - 1)] + [entry(9999, label_names=[])]
        expect_fetch_refusal(project_root, output, RUN_ID, completed(response_bytes(rows)), "may be truncated")
        check(output.read_bytes() == b"old", "a ghost let a limit-boundary response through the guard")


def t_fetch_command_failures_preserve_old():
    with tempfile.TemporaryDirectory() as d:
        project_root, output = fetch_paths(d)
        output.write_bytes(b"old")
        expect_fetch_refusal(project_root, output, RUN_ID,
                             completed(b"partial", returncode=7, stderr=b"network failed"), "exited 7")
        check(output.read_bytes() == b"old", "non-zero gh response replaced the previous snapshot")

        def cannot_spawn(argv, **kwargs):
            raise FileNotFoundError("gh missing")

        expect_fetch_refusal(project_root, output, RUN_ID, cannot_spawn, "could not run")
        check(output.read_bytes() == b"old", "spawn failure replaced the previous snapshot")


def t_fetch_replace_failure_preserves_old_and_cleans_temp():
    with tempfile.TemporaryDirectory() as d:
        project_root, output = fetch_paths(d)
        output.write_bytes(b"old")
        real_replace = M.os.replace

        def fail_replace(source, target):
            raise OSError("simulated replace failure")

        M.os.replace = fail_replace
        try:
            expect_fetch_refusal(project_root, output, RUN_ID, completed(response_bytes([entry(41)])),
                                 "atomically promote")
        finally:
            M.os.replace = real_replace
        check(output.read_bytes() == b"old", "failed atomic replace damaged the previous snapshot")
        leftovers = list(output.parent.glob(f".{output.name}.*.tmp"))
        check(leftovers == [], f"failed atomic replace left temp artifacts: {leftovers!r}")


def t_fetch_rejects_untyped_or_escaping_paths():
    with tempfile.TemporaryDirectory() as d:
        project_root, output = fetch_paths(d)
        runner = completed(response_bytes([]))
        expect_fetch_refusal(Path("relative-root"), output, RUN_ID, runner, "absolute")
        expect_fetch_refusal(project_root, Path("relative-output"), RUN_ID, runner, "absolute")
        outside = Path(d) / "outside.json"
        expect_fetch_refusal(project_root, outside, RUN_ID, runner, "stay under")
        check(runner.calls == [], "invalid typed paths reached gh instead of failing before the fetch")


def t_fetch_then_detect_consistent():
    with tempfile.TemporaryDirectory() as d:
        project_root, output = fetch_paths(d)
        ledger = build_ledger(output.parent, [row(41)])
        count = M.fetch_snapshot(project_root, output, RUN_ID, runner=completed(response_bytes([entry(41)])))
        facts = M.detect(ledger, output, RUN_ID)
        check(count == 1 and facts["counts"]["snapshot_entries"] == 1,
              f"fetch and detect disagree about snapshot size: {count}, {facts!r}")
        check(facts["rows"]["41"]["absent_from_snapshot"] is False,
              f"detect did not consume the fetched row: {facts!r}")


# --- happy-path facts ---------------------------------------------------------

def t_all_quiet():
    code, res, err = scenario([row(41)], [entry(41)])
    check(code == 0, f"all-quiet must exit 0, got {code} (stderr {err!r})")
    check(res["facts_only"] is True, f"facts_only must be true, got {res!r}")
    check("routing" in res["note"] and "loop-control.md" in res["note"],
          f"note must say routing lives in loop-control.md, got {res['note']!r}")
    check(res["run_id"] == RUN_ID, f"run_id must echo the arg, got {res['run_id']!r}")
    check(set(res["generated_from"]) == {"ledger", "prs"},
          f"generated_from must name ledger+prs, got {res['generated_from']!r}")
    check(res["rows"]["41"] == {
        "absent_from_snapshot": False, "state": "OPEN", "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "label_facts": {REVIEWING: True, ACCEPTED: False},
    }, f"a quiet present row must carry ONLY the neutral observations, got {res['rows']['41']!r}")
    check(res["unadopted"] == [], f"nothing unadopted, got {res['unadopted']!r}")
    check(res["counts"] == {
        "ledger_rows": 1, "terminal_rows": 0, "live_rows": 1, "snapshot_entries": 1,
        "present_in_snapshot": 1, "absent_from_snapshot": 0, "head_moved": 0,
        "base_changed": 0, "branch_mismatch": 0, "unadopted": 0,
    }, f"counts drifted, got {res['counts']!r}")


def t_merged_by_absence():
    # A live row with an EMPTY snapshot: absent, exit 0, NOT an error.
    code, res, err = scenario([row(41)], [])
    check(code == 0, f"absence is a FACT, must exit 0, got {code} (stderr {err!r})")
    check(res["rows"]["41"] == {"absent_from_snapshot": True},
          f"an absent live row reports ONLY absent_from_snapshot:true, got {res['rows']['41']!r}")
    check(res["counts"]["absent_from_snapshot"] == 1 and res["counts"]["present_in_snapshot"] == 0
          and res["counts"]["live_rows"] == 1 and res["counts"]["snapshot_entries"] == 0,
          f"absence counts drifted, got {res['counts']!r}")
    check(res["unadopted"] == [], f"an empty snapshot yields no unadopted, got {res['unadopted']!r}")


def t_head_moved():
    code, res, err = scenario([row(41, head_sha=SHA_A)], [entry(41, headRefOid=SHA_B)])
    check(code == 0, f"a moved head is a fact, exit 0, got {code} (stderr {err!r})")
    facts = res["rows"]["41"]
    check(facts.get("head_moved") == {"ledger": SHA_A, "snapshot": SHA_B},
          f"head_moved must report BOTH values, got {facts.get('head_moved')!r}")
    check(facts["absent_from_snapshot"] is False, "a present row is not absent")
    check(res["counts"]["head_moved"] == 1, f"head_moved count drifted, got {res['counts']!r}")


def t_base_changed():
    # A LEGACY row (no explicit base_branch) inherits the header base through effective_base, so the
    # comparison is against "main" — the same result the old header-only compare gave, now via the accessor.
    code, res, err = scenario([row(41)], [entry(41, base="develop")], base_branch="main")
    check(code == 0, f"exit 0, got {code} (stderr {err!r})")
    facts = res["rows"]["41"]
    check(facts.get("base_changed") == {"ledger": "main", "snapshot": "develop"},
          f"base_changed compares snapshot baseRefName to the row's effective_base (legacy row inherits the "
          f"header), got {facts!r}")
    check(res["counts"]["base_changed"] == 1, f"base_changed count drifted, got {res['counts']!r}")


def t_base_changed_uses_row_effective_base():
    """base_changed compares against the ROW's effective_base, not the one header base — the mixed-base rule.

    A row on `v3` (explicit row base) whose live target is `v3` is UNCHANGED even though the header base is
    `main`; the same row whose live target reverted to `main` is a base_changed{ledger: v3, snapshot: main}
    — the row's recorded base, not the header, is the currency. loop-control.md routes that fact to the park.
    """
    # Explicit row base v3 matches the live v3 target -> no base_changed, even though the header base is main.
    code, res, err = scenario([row(41, base_branch="v3")], [entry(41, base="v3")], base_branch="main")
    check(code == 0, f"exit 0, got {code} (stderr {err!r})")
    facts = res["rows"]["41"]
    check("base_changed" not in facts,
          f"a row whose live base equals its EXPLICIT row base is unchanged, header base notwithstanding: {facts!r}")
    check(res["counts"]["base_changed"] == 0, f"base_changed count should be 0, got {res['counts']!r}")
    # The same row retargeted back to the header's `main` DIFFERS from its recorded `v3` -> base_changed.
    code, res, err = scenario([row(41, base_branch="v3")], [entry(41, base="main")], base_branch="main")
    check(code == 0, f"exit 0, got {code} (stderr {err!r})")
    facts = res["rows"]["41"]
    check(facts.get("base_changed") == {"ledger": "v3", "snapshot": "main"},
          f"the recorded row base v3 is the currency, not the header main: {facts!r}")


def t_branch_mismatch():
    code, res, err = scenario([row(41, branch="the-branch")], [entry(41, head="OTHER-branch")])
    check(code == 0, f"exit 0, got {code} (stderr {err!r})")
    facts = res["rows"]["41"]
    check(facts.get("branch_mismatch") == {"ledger": "the-branch", "snapshot": "OTHER-branch"},
          f"branch_mismatch must report both branch names, got {facts!r}")
    check(res["counts"]["branch_mismatch"] == 1, f"branch_mismatch count drifted, got {res['counts']!r}")


def t_all_three_changes_together():
    # head, base and branch all differ at once — each key present, plus the verbatim GitHub fields.
    rows = [row(41, branch="b1", head_sha=SHA_A)]
    entries = [entry(41, head="b2", headRefOid=SHA_B, base="develop",
                     state="OPEN", mergeable="CONFLICTING", mergeStateStatus="DIRTY")]
    code, res, err = scenario(rows, entries, base_branch="main")
    check(code == 0, f"exit 0, got {code} (stderr {err!r})")
    facts = res["rows"]["41"]
    check(facts == {
        "absent_from_snapshot": False,
        "head_moved": {"ledger": SHA_A, "snapshot": SHA_B},
        "base_changed": {"ledger": "main", "snapshot": "develop"},
        "branch_mismatch": {"ledger": "b1", "snapshot": "b2"},
        "state": "OPEN", "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY",
        "label_facts": {REVIEWING: True, ACCEPTED: False},
    }, f"combined-change facts drifted, got {facts!r}")


def t_state_and_merge_fields_verbatim():
    # `state`, `mergeable`, `mergeStateStatus` are passed through EXACTLY as GitHub spelled them.
    code, res, err = scenario(
        [row(41)], [entry(41, state="MERGED", mergeable="UNKNOWN", mergeStateStatus="BEHIND")])
    check(code == 0, f"exit 0, got {code} (stderr {err!r})")
    facts = res["rows"]["41"]
    check(facts["state"] == "MERGED" and facts["mergeable"] == "UNKNOWN"
          and facts["mergeStateStatus"] == "BEHIND",
          f"verbatim GitHub fields must pass through unjudged, got {facts!r}")


# --- label facts, reported not judged -----------------------------------------

def t_label_drift_reported_not_judged():
    # The ledger expects `gauntlet-reviewing` but the snapshot shows `gauntlet-accepted` — reconcile
    # REPORTS both booleans and adds NO judgment field. Routing that mismatch is the skill's job.
    code, res, err = scenario([row(41)], [entry(41, label_names=[RUN_LABEL, ACCEPTED])])
    check(code == 0, f"exit 0, got {code} (stderr {err!r})")
    facts = res["rows"]["41"]
    check(facts["label_facts"] == {REVIEWING: False, ACCEPTED: True},
          f"label_facts must mirror the snapshot labels, got {facts['label_facts']!r}")
    check(set(facts) == {"absent_from_snapshot", "state", "mergeable", "mergeStateStatus", "label_facts"},
          f"a label mismatch must add NO judgment key — keys were {sorted(facts)!r}")


def t_both_status_labels_reported():
    _c, res, _e = scenario([row(41)], [entry(41, label_names=[RUN_LABEL, REVIEWING, ACCEPTED])])
    check(res["rows"]["41"]["label_facts"] == {REVIEWING: True, ACCEPTED: True},
          f"a PR wearing BOTH status labels reports both true, got {res['rows']['41']['label_facts']!r}")


def t_neither_status_label_reported():
    code, res, err = scenario([row(41)], [entry(41, label_names=[RUN_LABEL])])
    check(code == 0, f"exit 0, got {code} (stderr {err!r})")
    check(res["rows"]["41"]["label_facts"] == {REVIEWING: False, ACCEPTED: False},
          f"neither status label -> both false, got {res['rows']['41']['label_facts']!r}")


# --- unadopted ----------------------------------------------------------------

def t_unadopted_listed():
    code, res, err = scenario([row(41)], [entry(41), entry(99, title="candidate", head="cand-branch")])
    check(code == 0, f"exit 0, got {code} (stderr {err!r})")
    check(res["unadopted"] == [{"number": 99, "title": "candidate", "headRefName": "cand-branch"}],
          f"an entry with no ledger row is unadopted (facts only), got {res['unadopted']!r}")
    check(res["counts"]["unadopted"] == 1, f"unadopted count drifted, got {res['counts']!r}")
    check("99" not in res["rows"], "an unadopted PR gets no reconcile row")


def t_unadopted_number_stays_int():
    _c, res, _e = scenario([row(41)], [entry(41), entry(7)])
    check(res["unadopted"][0]["number"] == 7 and isinstance(res["unadopted"][0]["number"], int),
          f"unadopted number is the verbatim int, got {res['unadopted'][0]!r}")


# --- terminal rows: not compared at all ---------------------------------------

def t_terminal_merged_even_when_present():
    # A merged row whose PR is STILL in the snapshot (with a moved head) — the tool stays silent beyond
    # `terminal`. Presence, absence and change are all NOT reported for a terminal row.
    code, res, err = scenario([row(41, status="merged")], [entry(41, headRefOid=SHA_B, base="develop")])
    check(code == 0, f"exit 0, got {code} (stderr {err!r})")
    check(res["rows"]["41"] == {"terminal": "merged"},
          f"a terminal row emits ONLY {{terminal: status}}, got {res['rows']['41']!r}")
    check(res["unadopted"] == [], "a reappearing terminal PR has a row, so it is not unadopted")
    check(res["counts"]["terminal_rows"] == 1 and res["counts"]["live_rows"] == 0
          and res["counts"]["present_in_snapshot"] == 0 and res["counts"]["absent_from_snapshot"] == 0,
          f"terminal counts drifted, got {res['counts']!r}")


def t_terminal_aborted_absent():
    # An aborted row absent from the snapshot — still ONLY `terminal`, absence is NOT reported.
    code, res, err = scenario([row(41, status="aborted")], [])
    check(code == 0, f"exit 0, got {code} (stderr {err!r})")
    check(res["rows"]["41"] == {"terminal": "aborted"},
          f"an aborted row emits ONLY {{terminal: aborted}}, got {res['rows']['41']!r}")


# --- refusals: fail closed, exit 2, empty stdout, named cause ------------------

def _refusal(rows, entries, *, run_id=RUN_ID, base_branch="main"):
    with tempfile.TemporaryDirectory() as d:
        ledger = build_ledger(d, rows, base_branch=base_branch)
        prs = build_prs(d, entries)
        code, out, err = capture_cli(
            M.main, ["detect", "--ledger", str(ledger), "--prs", str(prs), "--run-id", run_id])
        return code, out, err


def t_missing_canonical_field_refused():
    bad = entry(41)
    del bad["headRefOid"]
    code, out, err = _refusal([row(41)], [bad])
    check(code == 2, f"a missing canonical field must exit 2 (fail closed), got {code}")
    check(out.strip() == "", f"a refusal must print NO facts to stdout, got {out!r}")
    check("headRefOid" in err, f"the refusal must NAME the missing field, got {err!r}")
    check("reconcile.py fetch" in err, f"the refusal must point at the executable owner, got {err!r}")


def t_null_canonical_field_refused():
    code, _out, err = _refusal([row(41)], [entry(41, headRefOid=None)])
    check(code == 2, f"a null canonical field must exit 2, got {code}")
    check("null" in err and "headRefOid" in err, f"the refusal must name null + field, got {err!r}")


def t_wrong_type_canonical_field_refused():
    bad = entry(41)
    bad["number"] = "41"          # a string where an int is required
    code, _out, err = _refusal([row(41)], [bad])
    check(code == 2, f"a wrong-typed field must exit 2, got {code}")
    check("number" in err and "integer" in err, f"the refusal must name field + expected shape, got {err!r}")


def t_boolean_number_refused():
    bad = entry(41)
    bad["number"] = True          # bool is a subclass of int — must be refused, not read as a number
    code, _out, err = _refusal([row(41)], [bad])
    check(code == 2, f"a boolean number must exit 2, got {code}")
    check("number" in err and "boolean" in err, f"the refusal must name the boolean, got {err!r}")


def t_foreign_label_refuses_whole_file():
    code, out, err = _refusal([row(41)], [entry(41, label_names=["gauntlet-run-OTHER", REVIEWING])])
    check(code == 2, f"a snapshot entry outside this run's label scope must exit 2, got {code}")
    check(out.strip() == "", f"no facts on a run-scope refusal, got {out!r}")
    check(RUN_LABEL in err and "run-isolation" in err,
          f"the refusal must name the missing run label and the isolation property, got {err!r}")


def t_one_foreign_entry_refuses_the_whole_file():
    # A good entry FOLLOWED by a foreign one — the whole file is refused, the good row is NOT reconciled.
    code, out, _err = _refusal([row(41)], [entry(41), entry(99, label_names=["gauntlet-run-OTHER"])])
    check(code == 2, f"one foreign entry refuses the whole file, got {code}")
    check(out.strip() == "", "a partly-foreign snapshot yields no facts at all")


def t_labels_not_a_list_refused():
    code, _out, err = _refusal([row(41)], [entry(41, raw_labels="gauntlet-reviewing")])
    check(code == 2, f"a non-list `labels` must exit 2, got {code}")
    check("labels" in err, f"the refusal must name `labels`, got {err!r}")


def t_malformed_label_element_refused():
    code, _out, err = _refusal([row(41)], [entry(41, raw_labels=[{"name": RUN_LABEL}, 123])])
    check(code == 2, f"a malformed label element must exit 2, got {code}")
    check("label" in err, f"the refusal must name the label problem, got {err!r}")


def t_duplicate_number_refused():
    code, _out, err = _refusal([row(41)], [entry(41), entry(41, head="dup")])
    check(code == 2, f"a duplicate PR number in the snapshot must exit 2, got {code}")
    check("41" in err and "twice" in err, f"the refusal must name the duplicated PR, got {err!r}")


def t_prs_not_json_refused():
    with tempfile.TemporaryDirectory() as d:
        ledger = build_ledger(d, [row(41)])
        prs = build_prs(d, "{ not json")
        code, _out, err = capture_cli(
            M.main, ["detect", "--ledger", str(ledger), "--prs", str(prs), "--run-id", RUN_ID])
    check(code == 2, f"invalid JSON must exit 2, got {code}")
    check("not valid JSON" in err, f"the refusal must say the JSON is invalid, got {err!r}")


def t_prs_not_an_array_refused():
    with tempfile.TemporaryDirectory() as d:
        ledger = build_ledger(d, [row(41)])
        prs = build_prs(d, {"number": 41})     # an object, not an array
        code, _out, err = capture_cli(
            M.main, ["detect", "--ledger", str(ledger), "--prs", str(prs), "--run-id", RUN_ID])
    check(code == 2, f"a non-array prs.json must exit 2, got {code}")
    check("not a JSON array" in err, f"the refusal must say it is not an array, got {err!r}")


def t_missing_ledger_refused():
    with tempfile.TemporaryDirectory() as d:
        prs = build_prs(d, [entry(41)])
        missing = Path(d) / "nope.jsonl"
        code, _out, err = capture_cli(
            M.main, ["detect", "--ledger", str(missing), "--prs", str(prs), "--run-id", RUN_ID])
    check(code == 2, f"a missing ledger must exit 2, got {code}")
    check("no ledger" in err, f"the refusal must name the missing ledger, got {err!r}")


def t_corrupt_ledger_refused():
    # A present-but-headerless ledger — the schema owner rejects it, and reconcile turns that into its own
    # fail-closed refusal rather than letting the SystemExit escape.
    with tempfile.TemporaryDirectory() as d:
        ledger = Path(d) / "state.jsonl"
        ledger.write_text('{"type": "row", "pr": "41"}\n', encoding="utf-8")
        prs = build_prs(d, [entry(41)])
        code, out, err = capture_cli(
            M.main, ["detect", "--ledger", str(ledger), "--prs", str(prs), "--run-id", RUN_ID])
    check(code == 2, f"a corrupt ledger must exit 2, got {code}")
    check(out.strip() == "", f"no facts on a corrupt ledger, got {out!r}")
    check("schema owner" in err, f"the refusal must attribute it to the ledger owner, got {err!r}")


CASES = [
    ("fetch-exact-argv", "fetch owns exact label/state/limit/fields argv and captures raw bytes",
     t_fetch_exact_argv_and_raw_bytes),
    ("fetch-refuse-malformed", "malformed/missing-field output preserves the old snapshot",
     t_fetch_malformed_and_missing_field_preserve_old),
    ("fetch-refuse-non-utf8", "non-UTF-8 output preserves the old snapshot", t_fetch_non_utf8_preserves_old),
    ("fetch-skip-ghost", "an unlabelled ghost is skipped and the labelled remainder is promoted",
     t_fetch_skips_ghost_and_promotes_the_rest),
    ("fetch-skip-foreign-ghost", "a ghost carrying ANOTHER run's label is skipped too",
     t_fetch_skips_another_runs_ghost),
    ("fetch-report-skip", "every skip lands on stderr and in skipped_unlabelled — never silent",
     t_fetch_reports_every_skip),
    ("fetch-all-ghosts", "an all-ghost response promotes an EMPTY snapshot, not a refusal",
     t_fetch_all_ghosts_promotes_an_empty_snapshot),
    ("fetch-skip-detect-refuses", "the SAME bytes fetch skips are refused whole by detect",
     t_fetch_ghost_still_refused_by_detect),
    ("fetch-ghost-still-validated", "a ghost is validated and THEN skipped, so a drifted one still refuses",
     t_fetch_ghost_is_validated_not_skipped_instead),
    ("fetch-ghost-duplicate-number", "a ghost registers its number, so a duplicated PR still refuses",
     t_fetch_ghost_duplicate_number_refused),
    ("fetch-refuse-limit", "a response at the limit boundary may be truncated and is refused",
     t_fetch_limit_boundary_refused),
    ("fetch-limit-counts-ghosts", "the limit guard counts the response, so a ghost cannot slip it",
     t_fetch_limit_boundary_counts_ghosts),
    ("fetch-command-failures", "non-zero and spawn failures preserve the old snapshot",
     t_fetch_command_failures_preserve_old),
    ("fetch-atomic-preserve", "failed atomic promotion preserves old bytes and cleans its temp",
     t_fetch_replace_failure_preserves_old_and_cleans_temp),
    ("fetch-typed-paths", "relative or escaping paths refuse before gh runs",
     t_fetch_rejects_untyped_or_escaping_paths),
    ("fetch-then-detect", "detect consumes exactly the snapshot fetch validated and promoted",
     t_fetch_then_detect_consistent),
    ("all-quiet", "a present, unchanged live row -> only neutral observations", t_all_quiet),
    ("merged-by-absence", "an absent live row -> absent_from_snapshot:true, exit 0, NOT an error",
     t_merged_by_absence),
    ("head-moved", "headRefOid != row head_sha -> head_moved{ledger,snapshot}", t_head_moved),
    ("base-changed", "baseRefName != a legacy row's inherited effective_base -> base_changed{ledger,snapshot}", t_base_changed),
    ("base-changed-row-effective", "base_changed compares against the row's effective_base, not the header base", t_base_changed_uses_row_effective_base),
    ("branch-mismatch", "headRefName != row branch -> branch_mismatch{ledger,snapshot}", t_branch_mismatch),
    ("all-three-changes", "head+base+branch differ together -> all three keys + verbatim fields",
     t_all_three_changes_together),
    ("verbatim-github-fields", "state/mergeable/mergeStateStatus pass through unjudged",
     t_state_and_merge_fields_verbatim),
    ("label-drift", "accepted shown while reviewing expected -> label_facts, NO judgment key",
     t_label_drift_reported_not_judged),
    ("both-status-labels", "a PR wearing both status labels -> both true", t_both_status_labels_reported),
    ("neither-status-label", "neither status label -> both false", t_neither_status_label_reported),
    ("unadopted-listed", "a snapshot entry with no row -> unadopted (facts only)", t_unadopted_listed),
    ("unadopted-int", "an unadopted number stays a verbatim int", t_unadopted_number_stays_int),
    ("terminal-present", "a merged row still in the snapshot -> only {terminal}, nothing else",
     t_terminal_merged_even_when_present),
    ("terminal-absent", "an aborted row absent from the snapshot -> only {terminal}", t_terminal_aborted_absent),
    ("refuse-missing-field", "a missing canonical field -> exit 2, names field + block",
     t_missing_canonical_field_refused),
    ("refuse-null-field", "a null canonical field -> exit 2, names null", t_null_canonical_field_refused),
    ("refuse-wrong-type", "a wrong-typed field -> exit 2, names field + shape",
     t_wrong_type_canonical_field_refused),
    ("refuse-bool-number", "a boolean number -> exit 2 (bool is not a PR number)", t_boolean_number_refused),
    ("refuse-foreign-label", "an entry outside the run's label scope -> whole-file refusal",
     t_foreign_label_refuses_whole_file),
    ("refuse-one-foreign", "one foreign entry among good ones refuses the whole file",
     t_one_foreign_entry_refuses_the_whole_file),
    ("refuse-labels-not-list", "a non-list `labels` -> exit 2", t_labels_not_a_list_refused),
    ("refuse-bad-label-elem", "a malformed label element -> exit 2", t_malformed_label_element_refused),
    ("refuse-duplicate-number", "a PR listed twice in the snapshot -> exit 2", t_duplicate_number_refused),
    ("refuse-bad-json", "prs.json not valid JSON -> exit 2", t_prs_not_json_refused),
    ("refuse-not-array", "prs.json not a JSON array -> exit 2", t_prs_not_an_array_refused),
    ("refuse-missing-ledger", "a missing ledger -> exit 2", t_missing_ledger_refused),
    ("refuse-corrupt-ledger", "a corrupt ledger -> exit 2, attributed to the schema owner",
     t_corrupt_ledger_refused),
]
