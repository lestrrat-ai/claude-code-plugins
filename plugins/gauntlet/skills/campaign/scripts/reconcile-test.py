#!/usr/bin/env python3
"""Fixtures for `reconcile.py` — the ledger-vs-snapshot FACT detector.

They live in a SIBLING file, and `reconcile.py self-test` FAILS LOUDLY if it cannot load them.

EVERY FIXTURE HAS TEETH. It asserts the EXACT facts object — not just that a key exists but its whole
shape — and, for refusals, the EXACT exit code (2, fail closed), that stdout is EMPTY (no facts leak from
a refused run), and that stderr NAMES the specific thing wrong (the missing field, the foreign label). A
suite that only checked `code == 0` would pass against a detector that emitted the wrong facts, and the
facts are what the skill routes on.

Two decisions this suite PINS, because they are the ones a reader would otherwise have to guess:
- **A TERMINAL row is not compared at all.** `merged`/`aborted` rows emit `{"terminal": status}` and
  nothing else — even when the snapshot still shows the PR (a reopened-after-merge oddity). Presence is not
  reported, absence is not reported, no change is computed. The fixtures drive both branches.
- **`absent_from_snapshot` is a FACT, never an error.** A live row missing from the snapshot exits 0 with
  `{"absent_from_snapshot": true}` — the merged/closed-by-absence signal — not a non-zero "PR vanished".
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from _gauntlet.modules import load_module_from_path
from _gauntlet.testing import capture_cli

OWNER = Path(__file__).resolve().parent / "reconcile.py"
REFERENCES = Path(__file__).resolve().parent.parent / "references"


def _load_owner():
    mod = load_module_from_path("reconcile_owner", OWNER)
    if mod is None:
        raise RuntimeError(f"cannot load the reconcile detector at {OWNER}")
    return mod


M = _load_owner()
LED = M.L                                   # the ledger schema owner reconcile reuses

RUN_ID = "grec-0001"
RUN_LABEL = M.RUN_LABEL_PREFIX + RUN_ID
REVIEWING = M.REVIEWING_LABEL
ACCEPTED = M.ACCEPTED_LABEL
SHA_A = "a" * 40
SHA_B = "b" * 40

_UNSET = object()


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise M.SelfTestFailure(msg)


# --- fixture builders ---------------------------------------------------------

def entry(number, *, head=None, headRefOid=SHA_A, title=None, base="main", state="OPEN",
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


def run(ledger: Path, prs: Path, run_id=RUN_ID):
    code, out, err = capture_cli(
        M.main, ["detect", "--ledger", str(ledger), "--prs", str(prs), "--run-id", run_id])
    parsed = json.loads(out) if out.strip() else None
    return code, parsed, err


def scenario(rows, entries, *, base_branch="main", run_id=RUN_ID):
    """One temp dir holding a ledger + a prs.json, run through the CLI. Returns (code, parsed, err)."""
    with tempfile.TemporaryDirectory() as d:
        ledger = build_ledger(d, rows, base_branch=base_branch)
        prs = build_prs(d, entries)
        return run(ledger, prs, run_id=run_id)


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
    code, res, err = scenario([row(41)], [entry(41, base="develop")], base_branch="main")
    check(code == 0, f"exit 0, got {code} (stderr {err!r})")
    facts = res["rows"]["41"]
    check(facts.get("base_changed") == {"ledger": "main", "snapshot": "develop"},
          f"base_changed compares snapshot baseRefName to the HEADER base_branch, got {facts!r}")
    check(res["counts"]["base_changed"] == 1, f"base_changed count drifted, got {res['counts']!r}")


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
    check("files-and-ledger.md" in err, f"the refusal must point at the canonical block, got {err!r}")


def t_null_canonical_field_refused():
    code, out, err = _refusal([row(41)], [entry(41, headRefOid=None)])
    check(code == 2, f"a null canonical field must exit 2, got {code}")
    check("null" in err and "headRefOid" in err, f"the refusal must name null + field, got {err!r}")


def t_wrong_type_canonical_field_refused():
    bad = entry(41)
    bad["number"] = "41"          # a string where an int is required
    code, out, err = _refusal([row(41)], [bad])
    check(code == 2, f"a wrong-typed field must exit 2, got {code}")
    check("number" in err and "integer" in err, f"the refusal must name field + expected shape, got {err!r}")


def t_boolean_number_refused():
    bad = entry(41)
    bad["number"] = True          # bool is a subclass of int — must be refused, not read as a number
    code, out, err = _refusal([row(41)], [bad])
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
    code, out, err = _refusal([row(41)], [entry(41), entry(99, label_names=["gauntlet-run-OTHER"])])
    check(code == 2, f"one foreign entry refuses the whole file, got {code}")
    check(out.strip() == "", "a partly-foreign snapshot yields no facts at all")


def t_labels_not_a_list_refused():
    code, out, err = _refusal([row(41)], [entry(41, raw_labels="gauntlet-reviewing")])
    check(code == 2, f"a non-list `labels` must exit 2, got {code}")
    check("labels" in err, f"the refusal must name `labels`, got {err!r}")


def t_malformed_label_element_refused():
    code, out, err = _refusal([row(41)], [entry(41, raw_labels=[{"name": RUN_LABEL}, 123])])
    check(code == 2, f"a malformed label element must exit 2, got {code}")
    check("label" in err, f"the refusal must name the label problem, got {err!r}")


def t_duplicate_number_refused():
    code, out, err = _refusal([row(41)], [entry(41), entry(41, head="dup")])
    check(code == 2, f"a duplicate PR number in the snapshot must exit 2, got {code}")
    check("41" in err and "twice" in err, f"the refusal must name the duplicated PR, got {err!r}")


def t_prs_not_json_refused():
    with tempfile.TemporaryDirectory() as d:
        ledger = build_ledger(d, [row(41)])
        prs = build_prs(d, "{ not json")
        code, out, err = capture_cli(
            M.main, ["detect", "--ledger", str(ledger), "--prs", str(prs), "--run-id", RUN_ID])
    check(code == 2, f"invalid JSON must exit 2, got {code}")
    check("not valid JSON" in err, f"the refusal must say the JSON is invalid, got {err!r}")


def t_prs_not_an_array_refused():
    with tempfile.TemporaryDirectory() as d:
        ledger = build_ledger(d, [row(41)])
        prs = build_prs(d, {"number": 41})     # an object, not an array
        code, out, err = capture_cli(
            M.main, ["detect", "--ledger", str(ledger), "--prs", str(prs), "--run-id", RUN_ID])
    check(code == 2, f"a non-array prs.json must exit 2, got {code}")
    check("not a JSON array" in err, f"the refusal must say it is not an array, got {err!r}")


def t_missing_ledger_refused():
    with tempfile.TemporaryDirectory() as d:
        prs = build_prs(d, [entry(41)])
        missing = Path(d) / "nope.jsonl"
        code, out, err = capture_cli(
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


# --- anti-drift: the canonical field set matches the doc that OWNS the command --

def t_canonical_fields_match_the_doc():
    doc = (REFERENCES / "files-and-ledger.md").read_text(encoding="utf-8")
    matches = re.findall(r'"--json",\s*"([^"]+)"', doc)
    check(len(matches) == 1,
          f"expected exactly one `--json` field list in files-and-ledger.md's canonical block, "
          f"found {len(matches)} — the anchor this check reads moved")
    doc_fields = tuple(matches[0].split(","))
    check(doc_fields == M.CANONICAL_FIELDS,
          f"reconcile.CANONICAL_FIELDS {M.CANONICAL_FIELDS!r} has DRIFTED from the canonical command's "
          f"field set {doc_fields!r} — a field added to the command must be added to the detector, or a "
          f"fact silently goes unread")


CASES = [
    ("all-quiet", "a present, unchanged live row -> only neutral observations", t_all_quiet),
    ("merged-by-absence", "an absent live row -> absent_from_snapshot:true, exit 0, NOT an error",
     t_merged_by_absence),
    ("head-moved", "headRefOid != row head_sha -> head_moved{ledger,snapshot}", t_head_moved),
    ("base-changed", "baseRefName != header base_branch -> base_changed{ledger,snapshot}", t_base_changed),
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
    ("canonical-fields-match-doc", "reconcile.CANONICAL_FIELDS == the canonical command's field set",
     t_canonical_fields_match_the_doc),
]
