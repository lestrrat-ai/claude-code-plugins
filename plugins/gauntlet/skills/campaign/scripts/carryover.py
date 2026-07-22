#!/usr/bin/env python3
"""Distill a run's TERMINAL ledger into its carryover history file — exactly once, on normal exit.

`references/carryover.md` owns the rules; this is the tool the driver runs at Loop control step 5's exit
path instead of hand-authoring `.gauntlet/history/<run-id>.md`. The file's CONTENT is mechanical — a
deterministic projection of `state.jsonl` at the moment every row is terminal — so it is a tool, not
prose. PRUNING and any JUDGMENT stay with the driver (a fresh run edits/removes OTHER runs' files); this
tool only WRITES this run's own file, and only when the run is actually finished.

**The once-only rule is this tool's REFUSAL, not an exhortation.** A run is distilled exactly once
(`carryover.md`, "distilled exactly once, on normal exit"): an already-present `<run-id>.md` means a
previous exit already wrote it, so `distill` REFUSES to overwrite it. `--force` exists for one case only —
re-running after a crash that died mid-write — and it must be asked for explicitly.

**Follow-ups are NOT in this file, by design.** Work the campaign found-and-deferred lives in its OWN
durable store, `.gauntlet/followups.jsonl` (`references/followups.md`) — a sibling of `history/`, NOT
run-scoped, shared by every run, and the SOURCE OF TRUTH that outlives any single run. The carryover
history file records only what a run's PRs came to (`carryover.md`: merged / aborted / skipped
API-declined). Duplicating follow-ups here would fork that source of truth. So the distill never reads or
writes the follow-up store.

**The clock is an INPUT.** `--now <iso>` is REQUIRED — like the liveness tools, this takes the clock as
an argument rather than reading a hidden one, so a distill is reproducible and testable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

from _gauntlet.atomic import replace_text
from _gauntlet.modules import load_module_from_path

DESCRIPTION = "Distill a run's terminal ledger into .gauntlet/history/<run-id>.md — exactly once."

HERE = Path(__file__).resolve().parent
LEDGER_PY = HERE / "ledger.py"          # the schema owner — its loader's strictness is reused, never re-rolled
TEST_PY = HERE / "carryover-test.py"    # the fixture suite — this tool's executable contract, a SIBLING

FORMAT_VERSION = "2"

# Exit codes, per the house split (mirrors ledger.py's `fail`=1 / EXIT_STOP=3 shape, one tier up):
#   2  OPERATOR ERROR — the tool was pointed at something that is not a distillable ledger: bad args
#      (argparse), an unreadable/malformed store, or a header that names no run.
#   3  STOP — a VALID ledger, but writing is refused right now: a row is still non-terminal (a live run is
#      never distilled), or the file already exists and `--force` was not given (already distilled once).
# 0 is written-and-done. There is no 1: a distill either has a valid finished run to write, or it refuses.
EXIT_OPERATOR = 2
EXIT_STOP = 3

# Terminal statuses — the ONLY two a distillable row may hold. `ledger.py`'s `status` taxonomy owns the
# full set; these are its two ENDS (`files-and-ledger.md`, `status`). Any other value is a live PR.
TERMINAL_STATUSES = ("merged", "aborted")

# The per-section projections. KEYS ARE LEDGER FIELD NAMES (`ledger.py` ROW_FIELDS) so there is no
# translation layer to drift — a reader greps the history file with the same field name it greps the
# ledger with. Each section answers one thing a future run needs (`carryover.md`):
#   merged        — de-dup against work that SHIPPED: which PR, its slug, the SHA it merged at, its tier
#                   and how many review rounds it took.
#   aborted       — the durable WHY a PR could not clear the bar: `ci_reason` (the machine blocker) and
#                   `blocker_ruling` (the user's answer, e.g. abort@<iso>).
#   api-declined  — the parked API fact to remind the user about: the PR and its `api_approval` verdict.
# EVERY projected object ALSO carries `base_branch` (v2): the row's EFFECTIVE base at distillation
# (`ledger.effective_base` — its own recorded base, else the run's legacy header base). It is added by
# `render`, not listed here, because it is a COMPUTED value, not a raw field read. Pruning uses each
# PR/base pair, so history for one base never prunes against another (`carryover.md`).
MERGED_FIELDS = ("pr", "slug", "head_sha", "tier", "review_rounds")
ABORTED_FIELDS = ("pr", "slug", "ci_reason", "blocker_ruling")
API_DECLINED_FIELDS = ("pr", "slug", "api_approval")


class Refusal(Exception):
    """A distill the tool declines to perform, carrying its exit code. Nothing is written."""

    def __init__(self, code: int, msg: str) -> None:
        super().__init__(msg)
        self.code = code
        self.msg = msg


def _ledger():
    mod = load_module_from_path("carryover_ledger", LEDGER_PY)
    if mod is None:  # pragma: no cover - a broken checkout, not a verdict
        raise RuntimeError(f"cannot load the ledger accessor at {LEDGER_PY}")
    return mod


# --- projection (pure) --------------------------------------------------------

def sections(rows: list[dict]) -> "list[tuple[str, tuple[str, ...], list[dict]]]":
    """The three terminal-class projections, in a FIXED order. Row order is the ledger's, so the output is
    deterministic. A declined-API PR is terminal-`aborted`, so it appears in BOTH `aborted` and
    `api-declined`; that overlap is documented in the file's header and is not double-counting.
    """
    merged = [r for r in rows if r["status"] == "merged"]
    aborted = [r for r in rows if r["status"] == "aborted"]
    api_declined = [r for r in rows if r["api_approval"].startswith("declined@")]
    return [
        ("merged", MERGED_FIELDS, merged),
        ("aborted", ABORTED_FIELDS, aborted),
        ("api-declined", API_DECLINED_FIELDS, api_declined),
    ]


def render(run_id: str, base_branches: "list[str]", now: str,
           projected: "list[tuple[str, tuple[str, ...], list[dict]]]",
           base_of: "Callable[[dict], str]") -> str:
    """The history file as text — a self-describing artifact. The leading comment DOCUMENTS the format so
    the file explains itself; the data is one JSON object per PR (fields, never sentences).

    `base_branches` is the sorted, deduplicated set of every projected row's effective base — the v2
    metadata that replaces v1's single `base_branch:` line. `base_of` resolves one row's effective base
    (`ledger.effective_base`), stamped onto every object so history prunes per PR/base pair.
    """
    out: list[str] = []
    out.append(f"<!-- gauntlet carryover history — format v{FORMAT_VERSION}")
    out.append("Distilled ONCE from a run's terminal ledger by `carryover.py distill`, on the run's")
    out.append("normal exit (every PR merged or aborted). A deterministic projection of state.jsonl —")
    out.append("DO NOT hand-edit, and it is never re-distilled (an existing file means a previous exit")
    out.append("already wrote it). Object keys are ledger field names (scripts/ledger.py ROW_FIELDS).")
    out.append("Each `## <section>` heading is followed by zero or more rows, one JSON object per PR:")
    out.append("  merged        pr, slug, head_sha (at merge), tier, review_rounds, base_branch")
    out.append("  aborted       pr, slug, ci_reason, blocker_ruling, base_branch  (the durable why it stopped)")
    out.append("  api-declined  pr, slug, api_approval, base_branch. A declined PR is ALSO aborted, so it")
    out.append("                appears in BOTH sections — this is a reminder projection, not double-counting.")
    out.append("Each object's `base_branch` is the row's EFFECTIVE base at distillation (its own recorded")
    out.append("base, else the run's legacy header base). `base_branches` below is the sorted, deduplicated")
    out.append("set of those bases; prune each entry against ITS OWN base, never the run's (carryover.md).")
    out.append("A v1 file instead carries a single `base_branch:` metadata line and NO per-object base — in")
    out.append("that file, that one base is the effective base of every object (carryover.md owns the read).")
    out.append("Follow-ups are NOT here: they live in .gauntlet/followups.jsonl (followups.md).")
    out.append("-->")
    out.append(f"# carryover {run_id}")
    out.append("")
    out.append(f"run_id: {run_id}")
    out.append(f"base_branches: {json.dumps(base_branches)}")
    out.append(f"distilled_at: {now}")
    for name, fields, rows in projected:
        out.append("")
        out.append(f"## {name}")
        for row in rows:
            obj = {f: row[f] for f in fields}
            obj["base_branch"] = base_of(row)
            out.append(json.dumps(obj))
    return "\n".join(out) + "\n"


# --- the distill --------------------------------------------------------------

def check_now(now: str) -> str:
    now = now.strip()
    if not now:
        raise Refusal(EXIT_OPERATOR, "--now is empty — the distilled-at timestamp is a required INPUT (this "
                                     "tool reads no hidden clock); pass the ISO-8601 time")
    return now


def check_run_id(header: dict) -> str:
    """A distillable ledger names its run. `run_id` defaults to `-` (unset) when absent, so a `-` here
    means the header carries no run identity — not a distill source.

    The base branch is NO LONGER a header-level gate (v2): the base a PR merges into is per-ROW state
    (`ledger.effective_base`), so a new run's header base is `-` and each row carries its own explicit
    base. A run's bases are read from its rows, not this one header field, so a `-` header base is normal.
    """
    run_id = header.get("run_id", "-")
    if not run_id.strip() or run_id == "-":
        raise Refusal(EXIT_OPERATOR, "the ledger header has no run_id — this is not a run's ledger, so there "
                                     "is nothing to distill")
    return run_id


def check_terminal(rows: list[dict]) -> None:
    """EVERY row must be terminal. A live run is never distilled — name the offending PRs and stop."""
    live = [(r["pr"], r["status"]) for r in rows if r["status"] not in TERMINAL_STATUSES]
    if live:
        named = ", ".join(f"pr {pr} is {status}" for pr, status in live)
        raise Refusal(EXIT_STOP, f"{len(live)} PR(s) not terminal ({named}) — a run is distilled only on "
                                 f"its NORMAL exit, when every row is merged or aborted. This run is still "
                                 f"live; drive it to completion first")


def distill(ledger, ledger_path: Path, out_dir: Path, now: str, *, force: bool) -> dict:
    """Load, validate, and (on success) ATOMICALLY write `<out_dir>/<run_id>.md`. Returns the summary dict.
    Raises `Refusal` for every declined case, having written NOTHING.
    """
    now = check_now(now)
    header, rows = ledger.load(ledger_path)  # the schema owner's loader — its strictness IS the validation
    run_id = check_run_id(header)
    check_terminal(rows)

    out_path = out_dir / f"{run_id}.md"
    if out_path.exists() and not force:
        raise Refusal(EXIT_STOP, f"{out_path} already exists — this run was already distilled (a run is "
                                 f"distilled exactly once). Pass --force ONLY to re-run after a crash that "
                                 f"died mid-write")

    # Each row's EFFECTIVE base resolves through the schema owner (its own recorded base, else the legacy
    # header) — never a second copy of that fallback. `base_branches` is the sorted, deduplicated set for
    # the v2 metadata; `base_of` stamps each object so history prunes per PR/base pair.
    def base_of(row: dict) -> str:
        return ledger.effective_base(header, row)

    base_branches = sorted({base_of(r) for r in rows})
    projected = sections(rows)
    text = render(run_id, base_branches, now, projected, base_of)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Same-directory temp + os.replace: a reader sees the whole old file or the whole new one, never a torn
    # write, and a failure leaves the ORIGINAL untouched and takes the temp with it (the house pattern).
    replace_text(out_path, text, temp_prefix=f".{run_id}.md.", encoding="utf-8")

    return {
        "run_id": run_id,
        "path": str(out_path),
        "base_branches": base_branches,
        **{name.replace("-", "_"): len(rows) for name, _fields, rows in projected},
    }


# --- cli ----------------------------------------------------------------------

def cmd_distill(args) -> int:
    ledger = _ledger()
    ledger_path = Path(args.ledger)
    out_dir = Path(args.out_dir)
    try:
        summary = distill(ledger, ledger_path, out_dir, args.now, force=args.force)
    except SystemExit:
        # `ledger.load` refuses a malformed/headerless/duplicate-pr store by PRINTING its own `ledger: …`
        # explanation to stderr and raising SystemExit(1). Translate that to this tool's operator-error
        # code without re-printing — the loader already said what is wrong, and reusing it is the point
        # (its strictness is not re-implemented here).
        return EXIT_OPERATOR
    except Refusal as exc:
        print(f"carryover: {exc.msg}", file=sys.stderr)
        return exc.code
    except OSError as exc:
        print(f"carryover: cannot read ledger {ledger_path}: {exc}", file=sys.stderr)
        return EXIT_OPERATOR
    print(json.dumps(summary))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("distill", help="write this run's .gauntlet/history/<run-id>.md from its TERMINAL "
                                       "ledger — refuses a live run, and refuses to overwrite (once only)")
    d.add_argument("--ledger", required=True, help="path to the run's ledger (<rundir>/state.jsonl)")
    d.add_argument("--out-dir", dest="out_dir", required=True,
                   help="the history directory (<repo>/.gauntlet/history) — the tool does NOT guess the "
                        "repo root; created if absent")
    d.add_argument("--now", required=True,
                   help="the distilled-at timestamp (ISO-8601), a REQUIRED input — this tool reads no "
                        "hidden clock")
    d.add_argument("--force", action="store_true",
                   help="overwrite an existing <run-id>.md. For ONE case only: re-running after a crash "
                        "that died mid-write. A normal exit distills exactly once and never needs it")

    sub.add_parser("self-test", help="run every fixture and assert the rules this file enforces still hold")
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "self-test":
        return self_test()
    if args.cmd == "distill":
        return cmd_distill(args)
    raise AssertionError(f"unreachable subcommand {args.cmd!r}")  # pragma: no cover


# --- self-test: the fixtures ARE the contract, and they are a SIBLING ---------

class SelfTestFailure(AssertionError):
    """A rule this file claims to enforce does not hold."""


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise SelfTestFailure(msg)


def sibling_cases() -> list:
    if not TEST_PY.exists():
        raise SelfTestFailure(f"the fixture file {TEST_PY} IS MISSING — this suite has no fixtures to run "
                              f"and CANNOT report health. Every rule this file enforces is now unpinned.")
    mod = load_module_from_path("carryover_test", TEST_PY, register=True)
    if mod is None:
        raise SelfTestFailure(f"{TEST_PY} exists but cannot be loaded as a module")
    cases = getattr(mod, "CASES", None)
    if not cases:
        raise SelfTestFailure(f"{TEST_PY} exports no CASES — every rule in this file is unpinned while the "
                              f"suite still exits 0")
    return list(cases)


def self_test() -> int:
    failures = 0
    try:
        cases = sibling_cases()
    except SelfTestFailure as exc:
        print(f"FAIL     {'sibling-fixtures':30} -> the fixtures in {TEST_PY.name} must be RUNNABLE\n"
              f"         {exc}")
        print("\n1 check(s) FAILED — the carryover distiller's contract is broken.")
        return 1
    for name, rule, fn in cases:
        try:
            fn()
        except SelfTestFailure as exc:
            print(f"FAIL     {name:30} -> {rule}\n         {exc}")
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL     {name:30} -> {rule}\n         raised {type(exc).__name__}: {exc}")
            failures += 1
        else:
            print(f"ok       {name:30} -> {rule}")
    print()
    if failures:
        print(f"{failures} check(s) FAILED — the carryover distiller's contract is broken.")
        return 1
    print(f"all {len(cases)} fixtures hold — the carryover distiller's contract is intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
