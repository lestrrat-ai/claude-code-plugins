#!/usr/bin/env python3
"""Execute one already-approved campaign merge and its owned local cleanup.

`merge-check.py` remains the owner of merge readiness. This command imports that gate, re-evaluates it
against the live PR and fetched base, then performs the established merge sequence. Each completed phase
is either durable outside this process (GitHub MERGED, updated refs, absent owned resources, terminal
ledger row) or safe to repeat. A rerun therefore resumes after interruption without repeating the merge.

It is also the FINALIZER for an absent-from-snapshot row loop-control.md Step 4 routes here after a merge
that landed but never finished: the single live view distinguishes MERGED (resume the owed base-sync /
cleanup / terminal-write phases) from CLOSED-without-merge (the terminal close-out — record `aborted`, no
merge, no cleanup, because unmerged branch content must never be destroyed).

    merge.py run --ledger <state.jsonl> --pr <N> --project-root <dir> --repo <owner/name> \
        [--merge-method squash|merge|rebase]
    merge.py self-test

The sibling `merge-test.py` is the executable contract. Tests replace the process boundary; they never
contact GitHub and never merge a real PR.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from _gauntlet.modules import load_module_from_path

HERE = Path(__file__).resolve().parent
SIBLING = HERE / "merge-test.py"


def _load(name: str, filename: str):
    mod = load_module_from_path(name, HERE / filename)
    if mod is None:
        raise RuntimeError(f"cannot load {filename}")
    return mod


L = _load("merge_runner_ledger", "ledger.py")
MC = _load("merge_runner_check", "merge-check.py")

SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")
COUNT_RE = re.compile(r"^(?:0|[1-9][0-9]*)\Z")
RUN_LABEL_PREFIX = "gauntlet-run-"
MERGE_METHODS = ("squash", "merge", "rebase")
VIEW_FIELDS = (
    "state,headRefOid,headRefName,baseRefName,labels,"
    "mergeable,mergeStateStatus,isDraft"
)


class Refusal(RuntimeError):
    """A fail-closed boundary or phase failure."""


def _run(argv: list[str], *, cwd: "str | None" = None,
         text: bool = True, env: "dict[str, str] | None" = None) -> subprocess.CompletedProcess:
    # text defaults to True — every readiness/merge/cleanup call wants decoded str. The NUL-delimited path
    # plumbing passes text=False to capture RAW BYTES: a path can carry a non-UTF-8 byte (which text=True
    # would raise UnicodeDecodeError on — a ValueError this handler does not catch) or a CR (which text=True's
    # universal-newline translation would rewrite to LF, naming a DIFFERENT path). Byte mode does neither; the
    # caller splits on the NUL byte and decodes each field itself (see _nul_fields).
    # env defaults to None (inherit the parent environment). _sync_base passes a forced C locale so git's
    # fast-forward diagnostic is deterministic English — the substring the overwrite-refusal gate matches on.
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=text, check=False, env=env)  # noqa: S603
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "" if text else b"", str(exc) if text else b"")


def _require(proc: subprocess.CompletedProcess, what: str) -> str:
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "no diagnostic").strip()
        raise Refusal(f"{what} failed (exit {proc.returncode}): {detail}")
    return proc.stdout


def _one_lf(value: str) -> str:
    return value[:-1] if value.endswith("\n") else value


def _ff_detail(proc: subprocess.CompletedProcess) -> str:
    """Decode a BYTE-mode fast-forward capture's diagnostic. The checked-out-base `git merge --ff-only` in
    _sync_base runs text=False: with core.quotePath=false git emits a blocking filename's raw non-UTF-8 byte
    in stderr, and a text=True capture would raise UnicodeDecodeError — discarding BOTH the tailored refusal
    AND git's original error, then re-crashing every resume on the same input. Byte mode never raises;
    surrogateescape round-trips the odd byte into the message instead. A str stream (a fixture Fake, or a
    text-mode caller) is accepted verbatim so the same decode serves both surfaces that read the ff detail."""
    for stream in (proc.stderr, proc.stdout):
        if stream:
            decoded = (stream.decode("utf-8", "surrogateescape")
                       if isinstance(stream, (bytes, bytearray)) else stream)
            return decoded.strip()
    return "no diagnostic"


def _is_overwrite_refusal(detail: str) -> bool:
    """Positively identify git's OVERWRITE refusal from a fast-forward's diagnostic. Under a forced C locale
    (see _sync_base) git's two overwrite refusals — "Your local changes to the following files would be
    overwritten by merge:" (tracked) and "The following untracked working tree files would be overwritten by
    merge:" (untracked) — both carry the substring `overwritten by merge`. NO OTHER fast-forward failure does:
    a stale `.git/index.lock`, a genuine divergence, an unmerged index, or a transient error contains it zero
    times (verified against git 2.43.0). This is the POSITIVE signal that gates the tailored path-list +
    stash-recovery refusal: only a fast-forward git actually refused BECAUSE uncommitted work would be
    overwritten may blame the listed paths and advise a stash. Every other failure — even when the read-only
    path probe (which does not need the index lock) still names candidate paths — must fall back to git's raw
    diagnostic, because naming paths and advising a stash that cannot fix an unrelated failure would LIE."""
    return "overwritten by merge" in detail


def resolve_project_root(checkout: Path) -> Path:
    """Resolve the typed repository root once, following runtime-adapter.md's boundary."""
    if not checkout.is_absolute():
        raise Refusal("--project-root must be absolute")
    out = _require(
        _run(["git", "-C", str(checkout), "rev-parse", "--show-toplevel"]),
        "repository resolution",
    )
    raw = _one_lf(out)
    if not raw or not os.path.isabs(raw):
        raise Refusal("repository resolution returned an empty or non-absolute path")
    resolved = Path(raw)
    if os.path.normpath(str(resolved)) != os.path.normpath(str(checkout)):
        raise Refusal(f"--project-root {checkout} resolves to a different repository root {resolved}")
    return resolved


def _labels(view: dict) -> list[str]:
    raw = view.get("labels")
    if not isinstance(raw, list):
        raise Refusal("live PR field 'labels' must be a list")
    names: list[str] = []
    for item in raw:
        name = item.get("name") if isinstance(item, dict) else item
        if not isinstance(name, str):
            raise Refusal("live PR labels must contain string names")
        names.append(name)
    return names


def _validate_view(view: object) -> dict:
    if not isinstance(view, dict):
        raise Refusal("live PR view is not a JSON object")
    for field in ("state", "headRefOid", "headRefName", "baseRefName", "mergeable",
                  "mergeStateStatus"):
        if not isinstance(view.get(field), str):
            raise Refusal(f"live PR field {field!r} must be a string")
    if not isinstance(view.get("isDraft"), bool):
        raise Refusal("live PR field 'isDraft' must be a bool")
    _labels(view)
    return view


def _view(pr: str, repo: str, root: Path) -> dict:
    argv = ["gh", "pr", "view", pr, "--repo", repo, "--json", VIEW_FIELDS]
    out = _require(_run(argv, cwd=str(root)), f"live view for PR {pr}")
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError as exc:
        raise Refusal(f"live view for PR {pr} is not JSON: {exc}") from exc
    return _validate_view(parsed)


def _validate_ref(root: Path, value: str, role: str) -> None:
    if not value or value == "-":
        raise Refusal(f"{role} is unresolved")
    proc = _run(["git", "-C", str(root), "check-ref-format", f"refs/heads/{value}"])
    _require(proc, f"validation of {role} {value!r}")


def _validate_state(header: dict, row: dict, pr: str, root: Path, view: dict,
                    *, check_live_refs: bool = True, require_resolved_ownership: bool = True,
                    require_own_label: bool = True) -> None:
    # Validation is SCOPED BY PATH, because the finalize reaches this from paths that do very different
    # things. The three relaxations below default to the FULL validation the ONE merge-INITIATING path needs
    # (a live OPEN state on an in_review row: it starts a merge, then cleans up its owned resources). Every
    # OTHER path only FINALIZES an already-decided outcome — an external MERGE, a CLOSE, or a terminal repeat
    # — so it merges nothing and requires only what its terminal write / owned cleanup actually touches. A
    # relaxation is dropped ONLY on a path that does not use what it checks. The DESTRUCTIVE-OP guard below
    # (an owned resource's identity) is NOT one of these relaxations: it runs on EVERY path whenever a
    # resource is owned (`_owned=="yes"`), because that is exactly when `_cleanup` removes it — the guard
    # between a bug and a destroyed worktree is never relaxed.
    #   * `check_live_refs=False` drops the live head/base/branch equality pins — the checks a MERGE needs to
    #     land on the exact reviewed tip, and the head pin a MERGED-resume keeps to confirm OUR reviewed head
    #     is what landed. Only the CLOSED terminal paths drop it: a push or a base/branch rename before a
    #     CLOSE is irrelevant to a row that only records terminal `aborted`, and a CLOSED PR never re-enters
    #     the open snapshot to have its head_sha refreshed, so pinning it there would wedge the row forever.
    #   * `require_resolved_ownership=False` drops the fail-closed that BOTH ownership fields are RESOLVED
    #     (∈{yes,no}). That fail-closed is a MERGE-INITIATING sanity gate: a HALF-ADOPTION (pr-adopt.py
    #     registers the ledger row BEFORE it resolves the worktree, so its documented git-failure path leaves
    #     `worktree`/`worktree_owned`/`branch_owned` at their ROW_DEFAULTS "-") must never MERGE. A
    #     finalize-only path merges nothing; an unresolved field there just means this run owns NOTHING to
    #     clean, so the row must still TERMINATE, not wedge — the destructive guard already protects every
    #     owned removal, so a half-adopted PR that is later CLOSED or externally MERGED still finalizes.
    #   * `require_own_label=False` drops the requirement that the PR still carries THIS run's own label. A
    #     half-adoption fails before `gh pr edit` attaches it, so a half-adopted PR that is later CLOSED or
    #     externally MERGED carries no run label; requiring it on those finalize-only paths would wedge the
    #     row forever. The FOREIGN-label refusal below is NEVER relaxed on any path — it is the real
    #     run-isolation guard — so dropping own-label presence never lets a finalize act on another run's PR.
    if row.get("pr") != pr:
        raise Refusal(f"ledger row belongs to PR {row.get('pr')}, not PR {pr}")
    if not pr.isdecimal() or int(pr) < 1:
        raise Refusal(f"PR number {pr!r} is not a positive integer")
    run_id = header.get("run_id", "-")
    if not run_id or run_id == "-":
        raise Refusal("ledger run_id is unresolved")
    # The ROW owns the base: its explicit `base_branch`, else the legacy header, through `ledger.py`'s
    # accessor. A run may hold rows on different bases, so every base check here is per-row, never the header.
    base = L.effective_base(header, row)
    branch = row.get("branch", "-")
    _validate_ref(root, base, "base_branch")
    _validate_ref(root, branch, "row branch")
    if row.get("head_sha") is None or not SHA_RE.match(row["head_sha"]):
        raise Refusal("ledger head_sha must be a full lowercase 40-character object id")
    if row.get("tier") not in ("TRIVIAL", "STANDARD", "HIGH"):
        raise Refusal(f"ledger tier {row.get('tier')!r} is malformed")
    if not COUNT_RE.match(row.get("reviews_ok", "")):
        raise Refusal(f"ledger reviews_ok {row.get('reviews_ok')!r} is malformed")
    if row.get("ci") not in ("green", "red", "pending"):
        raise Refusal(f"ledger ci {row.get('ci')!r} is malformed")
    if require_resolved_ownership:
        if row.get("worktree_owned") not in ("yes", "no"):
            raise Refusal(f"ledger worktree_owned {row.get('worktree_owned')!r} is unresolved")
        if row.get("branch_owned") not in ("yes", "no"):
            raise Refusal(f"ledger branch_owned {row.get('branch_owned')!r} is unresolved")
    if row.get("branch_owned") == "yes" and row.get("worktree_owned") != "yes":
        raise Refusal("branch_owned=yes with worktree_owned=no is not an adoption-produced ownership state")
    if check_live_refs:
        if view["headRefOid"] != row["head_sha"]:
            raise Refusal(
                f"live head {view['headRefOid']} differs from ledger head {row['head_sha']} — re-gate")
        if view["headRefName"] != branch:
            raise Refusal(
                f"live head branch {view['headRefName']!r} differs from ledger branch {branch!r}")
        if view["baseRefName"] != base:
            # A live retarget away from the recorded base is an unsupported mid-run change — fail closed with
            # the SAME machine-blocker wording every other base door records (pr-adopt.py owns it, via
            # merge-check). A base that merely ADVANCED (same name) is handled by _base_is_current, not here.
            raise Refusal(MC.PA.BASE_CHANGE_PARK_REASON.format(recorded=base, live=view["baseRefName"]))
    labels = _labels(view)
    ours = f"{RUN_LABEL_PREFIX}{run_id}"
    run_labels = [name for name in labels if name.startswith(RUN_LABEL_PREFIX)]
    # The FOREIGN-label refusal comes FIRST and is never relaxed — it is the run-isolation guard, and must
    # fire even on a path where this run's OWN label presence is not required.
    foreign = [name for name in run_labels if name != ours]
    if foreign:
        raise Refusal(f"PR {pr} also carries another run's owner label {foreign[0]}")
    if require_own_label and ours not in run_labels:
        raise Refusal(f"PR {pr} does not carry this run's owner label {ours}")
    # DESTRUCTIVE-OP guard — UNCONDITIONAL, never gated on a relaxation flag. Whenever a resource is OWNED,
    # `_cleanup` will remove it, so its identity must be exactly the repository-derived campaign resource. A
    # half-adoption leaves `worktree_owned` at "-" (this guard skips it — nothing is owned to remove); an
    # adopted row that owns the worktree must have it resolved to `<root>/.worktrees/<branch>`, never the
    # repository root.
    if row.get("worktree_owned") == "yes":
        worktree = Path(row.get("worktree", ""))
        if not worktree.is_absolute():
            raise Refusal("owned worktree must be an absolute path")
        expected = root / ".worktrees" / branch
        if os.path.normpath(str(worktree)) != os.path.normpath(str(expected)):
            raise Refusal(
                f"owned worktree {worktree} is not the repository-derived campaign path {expected}")
        if os.path.normpath(str(worktree)) == os.path.normpath(str(root)):
            raise Refusal("the repository root can never be an owned cleanup target")


def _base_is_current(row: dict, header: dict) -> None:
    worktree = row["worktree"]
    base = L.effective_base(header, row)
    # Fully-qualified refspec so a dash-leading base name (network-supplied via baseRefName) can never be
    # parsed by git as an option; the same safety idiom as _sync_base and pr-adopt.py. This updates the very
    # origin/<base> remote-tracking ref the ancestry probe below reads.
    tracking_ref = f"refs/heads/{base}:refs/remotes/origin/{base}"
    _require(
        _run(["git", "-C", worktree, "fetch", "origin", tracking_ref]),
        f"refresh of origin/{base} for merge-check",
    )
    probe = _run(
        ["git", "-C", worktree, "merge-base", "--is-ancestor", f"origin/{base}", "HEAD"])
    if probe.returncode == 1:
        raise Refusal("merge-check: base moved ahead — rebase")
    _require(probe, f"merge-check ancestry against origin/{base}")


def _require_ready(row: dict, header: dict, view: dict) -> None:
    """Delegate policy to merge-check.py, then supply its fetched-base ancestry phase.

    merge-check.decide returns MERGE only for CLEAN/HAS_HOOKS; a BLOCKED PR is left as the PROBE sentinel
    because `decide` alone cannot tell a genuine block from one that is merely BEHIND its base. Mirror
    merge-check.check(): the ancestry probe runs for BOTH the MERGE and PROBE verdicts. `_base_is_current`
    raises the rebase Refusal when the PR is behind its base, so a BLOCKED-behind PR is routed to a rebase
    exactly as the gate does — NOT parked. A PROBE that survives the probe is BLOCKED-but-current: a genuine
    human/ruleset block that parks. PROBE never reaches an actual merge; it can only refuse (rebase or park).
    """
    result = MC.decide(row, view, required=MC.REQUIRED, effective_base=L.effective_base(header, row))
    verdict = result.get("verdict")
    if verdict not in (MC.MERGE, MC.PROBE):
        raise Refusal(f"merge-check: {result.get('reason', 'not ready')}")
    _base_is_current(row, header)
    if verdict == MC.PROBE:
        raise Refusal(f"merge-check: {MC.BLOCKED_PARK_REASON}")


def _parse_worktrees(data: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for token in data.split("\0"):
        if token == "":
            if current:
                entries.append(current)
                current = {}
            continue
        key, sep, value = token.partition(" ")
        current[key] = value if sep else ""
    if current:
        entries.append(current)
    return entries


def _worktree_listing(root: Path) -> list[dict[str, str]]:
    out = _require(
        _run(["git", "-C", str(root), "worktree", "list", "--porcelain", "-z"]),
        "worktree discovery",
    )
    return _parse_worktrees(out)


def _nul_fields(data: bytes) -> list[str]:
    """Split BYTE-mode NUL-delimited plumbing output; drop the trailing empty field. The split happens on
    the NUL byte BEFORE decoding, so no text-mode universal-newline translation can rewrite a CR or CRLF
    inside a path, and a non-UTF-8 byte cannot raise. NUL cannot appear in a path, so a path carrying
    spaces, tabs, CR, LF, or CRLF survives as one field and can never be split into two. Each field is then
    decoded utf-8/surrogateescape, so a non-UTF-8 byte round-trips instead of crashing the probe."""
    return [field.decode("utf-8", "surrogateescape") for field in data.split(b"\0") if field]


def _quote_path(path: str) -> str:
    """JSON-quote a path so spaces, tabs, and newlines are escaped and can never forge a message line."""
    return json.dumps(path)


def _path_conflicts(path: str, incoming: "set[str]") -> bool:
    """A staged, unstaged, or untracked path conflicts with the incoming fast-forward when it equals an
    incoming path or is a file/directory prefix of one (either direction) — the shape git rejects as it
    updates that path. A change to any OTHER path is left alone by the fast-forward and does not block it."""
    if path in incoming:
        return True
    return any(path.startswith(f"{inc}/") or inc.startswith(f"{path}/") for inc in incoming)


def _blocking_uncommitted_paths(checkout: str, base: str) -> "list[str] | None":
    """After a checked-out-base fast-forward fails, name the uncommitted paths that block it.

    Read-only: runs only git plumbing (merge-base/ls-files/diff-index/diff-files/diff-tree) and mutates
    nothing. Returns JSON-quoted, deduplicated, sorted paths, or None when this is NOT a diagnosable
    uncommitted-work block (the graph forbids a fast-forward, the index is unmerged/conflicted, or any
    plumbing command failed) — in which case the caller keeps the original raw git error rather than
    replacing it with a secondary failure.
    """
    # A fast-forward is only possible when HEAD is already an ancestor of origin/<base>. If it is not,
    # the failure is a genuine divergence, not an uncommitted-work block — keep the raw refusal.
    ancestor = _run(["git", "-C", checkout, "merge-base", "--is-ancestor", "HEAD", f"origin/{base}"])
    if ancestor.returncode != 0:
        return None

    def plumb(args: "list[str]") -> "list[str] | None":
        # BYTE mode: a -z path can carry a non-UTF-8 byte or a CR, so capture raw and split on the NUL byte
        # BEFORE decoding (see _nul_fields). A spawn failure or nonzero exit yields None, so the caller keeps
        # the original raw fast-forward error rather than a secondary diagnostic failure.
        proc = _run(["git", "-C", checkout, *args], text=False)
        if proc.returncode != 0:
            return None
        return _nul_fields(proc.stdout)

    # An UNMERGED (conflicted) index makes ff-only fail with git's own unresolved-conflict error, and git
    # REFUSES both `commit` and `stash` while the index is unmerged — so the tailored recovery advice would
    # be wrong here. Detect it read-only and decline, preserving git's raw diagnostic. The ancestor guard above
    # does NOT catch this: a conflicted merge leaves HEAD un-advanced, so HEAD is still an ancestor of
    # origin/<base>. (A non-empty ls-files --unmerged listing means at least one path is at stage > 0.)
    unmerged = plumb(["ls-files", "--unmerged", "-z", "--"])
    if unmerged is None or unmerged:
        return None

    staged = plumb(["diff-index", "--cached", "--name-only", "-z", "--no-renames", "HEAD", "--"])
    # A path staged to EXACTLY the incoming target content does NOT block `git merge --ff-only` — git names
    # only the real blocker, so naming it over-reports. `staged` (index vs HEAD) identifies WHICH paths are
    # staged; this second probe (index vs origin/<base>) keeps only those whose staged content DIFFERS from
    # the incoming tree, dropping any already equal to it, BEFORE the incoming-overlap test below.
    staged_vs_incoming = plumb(
        ["diff-index", "--cached", "--name-only", "-z", "--no-renames", f"origin/{base}", "--"])
    unstaged = plumb(["diff-files", "--name-only", "-z", "--no-renames", "--"])
    untracked = plumb(["ls-files", "--others", "--exclude-standard", "-z", "--"])
    incoming = plumb(
        ["diff-tree", "--no-commit-id", "-r", "--name-only", "-z", "--no-renames",
         "HEAD", f"origin/{base}", "--"])
    if (staged is None or staged_vs_incoming is None or unstaged is None
            or untracked is None or incoming is None):
        return None

    diverged_from_incoming = set(staged_vs_incoming)
    staged = [path for path in staged if path in diverged_from_incoming]

    incoming_set = set(incoming)
    # git merge --ff-only rejects a staged, unstaged, OR untracked path only when it overlaps a path the
    # incoming fast-forward must update. A staged, unstaged, or untracked change to an UNRELATED path does
    # NOT block the fast-forward (git updates only the incoming paths and leaves the rest alone), so every
    # category is filtered through the same incoming-overlap test — never added unconditionally.
    blockers: "set[str]" = set()
    for path in (*staged, *unstaged, *untracked):
        if _path_conflicts(path, incoming_set):
            blockers.add(path)
    return [_quote_path(path) for path in sorted(blockers)]


def _sync_base(root: Path, base: str) -> None:
    remote_ref = f"refs/heads/{base}:refs/remotes/origin/{base}"
    _require(
        _run(["git", "-C", str(root), "fetch", "origin", remote_ref]),
        f"refresh of origin/{base}",
    )
    branch_ref = f"refs/heads/{base}"
    checked: list[str] = []
    for entry in _worktree_listing(root):
        path = entry.get("worktree")
        if entry.get("branch") == branch_ref and path is not None:
            checked.append(path)
    if len(checked) > 1:
        raise Refusal(f"base branch {base!r} is checked out in more than one worktree")
    if checked:
        # ff-only intentionally accepts a local-ahead base (git reports "Already up to date", exit 0).
        # Downstream diffs/rebases read origin/<base> (freshly fetched above), not this local branch, so a
        # local-ahead checkout poisons nothing; local-ahead means origin is an ancestor of local, i.e. local
        # already contains the merged tip. The dangerous diverged case still fails ff-only and refuses below.
        # BYTE mode: with core.quotePath=false git emits a blocking filename's raw non-UTF-8 byte in stderr,
        # which a text=True capture raises UnicodeDecodeError on (escaping past main and discarding BOTH
        # diagnostics). Capture raw and decode the detail via _ff_detail at BOTH surfaces that read it.
        # Forced C locale: git's diagnostic is deterministic English, so _is_overwrite_refusal below can match
        # the stable `overwritten by merge` substring — the positive signal that this ff failed BECAUSE of
        # uncommitted work, not some unrelated cause. (It also makes the preserved "Original Git diagnostic"
        # always English, a diagnostic improvement.)
        ff = _run(["git", "-C", checked[0], "merge", "--ff-only", f"origin/{base}"],
                  text=False, env={**os.environ, "LC_ALL": "C"})
        if ff.returncode != 0:
            blockers = _blocking_uncommitted_paths(checked[0], base)
            detail = _ff_detail(ff)
            # The tailored path-list + stash refusal is gated on a POSITIVELY VERIFIED overwrite failure AND
            # confident path detection. `blockers` alone is NOT enough: the read-only path probe does not need
            # the index lock, so an UNRELATED ff failure (a stale `.git/index.lock`, etc.) still yields a
            # non-empty `blockers` list — and blaming those paths + advising a stash that cannot fix the real
            # cause would be a false, misleading refusal. Only when git itself refused because uncommitted work
            # would be overwritten (`_is_overwrite_refusal(detail)`) may this message name paths and propose a
            # stash. Every other failure falls through to the raw-error backstop below, unchanged.
            if blockers and _is_overwrite_refusal(detail):
                listed = "\n".join(f"  - {path}" for path in blockers)
                # Diagnose-only: name the offending paths and PROPOSE the safe fix. NEVER commit, stash,
                # reset, restore, checkout, or clean — the campaign does not own these paths.
                raise Refusal(
                    f"fast-forward of checked-out base {base!r} at {checked[0]} refused because "
                    f"uncommitted paths block the update:\n{listed}\n"
                    "The PR is already merged. Stash the listed changes (use `git stash -u` to include "
                    "untracked files) — or commit them on a SEPARATE branch and switch back to "
                    f"{base!r} — then re-run the same merge.py run command to resume the owed base-sync. "
                    "Do NOT commit on the checked-out base itself: that makes a diverged sibling commit "
                    "the re-run's fast-forward would refuse. The campaign left these paths untouched.\n"
                    f"Original Git diagnostic: {detail}")
            # Not a verified overwrite block (a stale index lock, a divergence, an unmerged index, a plumbing
            # probe that failed, or any other cause): keep git's raw error.
            # `ff` is byte-mode, so hand _require the surrogateescape-decoded detail — never raw bytes, whose
            # repr would leak `b'...'` into the message. _require here always raises (returncode != 0).
            _require(
                subprocess.CompletedProcess(ff.args, ff.returncode, "", detail),
                f"fast-forward of checked-out base {base}")
    else:
        # If the local base ref already EXISTS and already CONTAINS origin/<base> (origin is an ancestor of
        # the local ref — the ref is equal to or LOCAL-AHEAD of origin), it is already synchronized: skip the
        # fetch. A non-forced local-ref fetch would reject a local-ahead ref as a non-fast-forward (exit 1)
        # and wedge post-merge finalization (cleanup + terminal write) even though nothing needs fetching.
        # Both `refs/heads/<base>` and `origin/<base>` are prefixed, so a dash-leading base can never be
        # option-parsed. A BEHIND local ref (origin ahead) and an ABSENT local ref both fall through to the
        # non-forced fetch below: behind fast-forwards, diverged still refuses (fails closed — never a `+`).
        exists = _run(["git", "-C", str(root), "show-ref", "--verify", "--quiet", branch_ref])
        if exists.returncode == 0:
            synced = _run(
                ["git", "-C", str(root), "merge-base", "--is-ancestor", f"origin/{base}", branch_ref])
            if synced.returncode == 0:
                return
        # Fully-qualified refspec (no leading `+`, preserving fast-forward-only semantics) so a dash-leading
        # base name can never be parsed by git as an option — the same safety idiom used just above.
        local_ref = f"refs/heads/{base}:refs/heads/{base}"
        _require(
            _run(["git", "-C", str(root), "fetch", "origin", local_ref]),
            f"fast-forward of local base ref {base}",
        )


def _entry_at(entries: list[dict[str, str]], path: str) -> "dict[str, str] | None":
    target = os.path.normpath(path)
    for entry in entries:
        if os.path.normpath(entry.get("worktree", "")) == target:
            return entry
    return None


def _cleanup(root: Path, row: dict) -> dict:
    branch = row["branch"]
    branch_ref = f"refs/heads/{branch}"
    worktree = row["worktree"]
    removed_worktree = False
    removed_branch = False

    entries = _worktree_listing(root)
    entry = _entry_at(entries, worktree)
    if row["worktree_owned"] == "yes":
        if entry is not None:
            if entry.get("branch") != branch_ref:
                raise Refusal(
                    f"owned worktree path {worktree} now holds a foreign or detached checkout")
            status = _require(
                _run(["git", "-C", worktree, "status", "--porcelain", "--untracked-files=all"]),
                f"cleanliness check for owned worktree {worktree}",
            )
            if status:
                raise Refusal(f"owned worktree {worktree} is dirty; refusing cleanup")
            _require(
                _run(["git", "-C", str(root), "worktree", "remove", worktree]),
                f"removal of owned worktree {worktree}",
            )
            removed_worktree = True
    elif entry is not None and entry.get("branch") not in (branch_ref, None):
        raise Refusal(f"reused worktree path {worktree} now holds a foreign branch")

    if row["branch_owned"] == "yes":
        entries = _worktree_listing(root)
        checked = [entry.get("worktree") for entry in entries if entry.get("branch") == branch_ref]
        if checked:
            raise Refusal(f"owned branch {branch!r} is still checked out at {checked[0]}")
        exists = _run(["git", "-C", str(root), "show-ref", "--verify", "--quiet", branch_ref])
        if exists.returncode == 0:
            actual = _one_lf(_require(
                _run(["git", "-C", str(root), "rev-parse", branch_ref]),
                f"identity check for owned branch {branch}",
            ))
            if actual != row["head_sha"]:
                raise Refusal(
                    f"owned branch {branch!r} moved to {actual}; expected {row['head_sha']}")
            _require(
                _run(["git", "-C", str(root), "branch", "-D", "--", branch]),
                f"removal of owned branch {branch}",
            )
            removed_branch = True
        elif exists.returncode != 1:
            _require(exists, f"lookup of owned branch {branch}")

    return {
        "worktree": "removed" if removed_worktree else
                    ("already-absent" if row["worktree_owned"] == "yes" else "reused-left"),
        "branch": "removed" if removed_branch else
                  ("already-absent" if row["branch_owned"] == "yes" else "reused-left"),
    }


def _mark_terminal(ledger: Path, pr: str, status: str) -> None:
    """Write a TERMINAL ledger status (`merged` after a landed merge, `aborted` after a closed-without-merge
    close-out) as the last, resumable phase. A same-value write is a no-op, so a re-run finalizes idempotently."""
    header, rows = L.load(ledger)
    row = L.find_row(rows, pr)
    if row is None:
        raise Refusal(f"ledger row for PR {pr} disappeared before terminal write")
    if row["status"] != status:
        row["status"] = status
        try:
            L.save(ledger, header, rows, activity=True)
        except OSError as exc:
            raise Refusal(f"terminal ledger write failed: {exc}") from exc


def execute(ledger: Path, pr: str, project_root: Path, repo: str,
            merge_method: str = "squash") -> dict:
    if merge_method not in MERGE_METHODS:
        raise Refusal(
            f"merge method {merge_method!r} is not one of {', '.join(MERGE_METHODS)}")
    root = resolve_project_root(project_root)
    header, rows = L.load(ledger)
    row = L.find_row(rows, pr)
    if row is None:
        raise Refusal(f"no ledger row for PR {pr}")
    # Cross-repo fail-closed guard. Every gh call below is scoped by `--repo repo` while operating in
    # cwd=root, but NOTHING else confirms that `--repo` names the checkout's OWN GitHub repository — while
    # resolve_project_root rigorously validates the checkout root. A mismatched `--repo` whose PR N collides
    # on head_sha + branch + base + run-label (possible across shared history or a fork) would merge the
    # reviewed HEAD onto an UN-reviewed base, then clean local resources and write the ledger merged. Derive
    # the canonical repo from the checkout and refuse a mismatch BEFORE the first view. `gh repo view` runs
    # in cwd=root through the same _run/_require, so an error or an ambiguous remote fails CLOSED. GitHub
    # owner/name is case-insensitive, so compare case-insensitively.
    canonical = _one_lf(_require(
        _run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
             cwd=str(root)),
        "repository identity resolution",
    ))
    if not canonical or canonical.lower() != repo.lower():
        raise Refusal(
            f"--repo {repo!r} does not name the checkout's repository {canonical!r}")
    view = _view(pr, repo, root)

    # Classify the finalize PATH before validating — the tier the validation runs at depends on it.
    #   * close_out: the CLOSED side of the absent-row finalizer (loop-control.md Step 4) — a human closed
    #     the PR, or the driver died after `gh pr close`. There is NOTHING to merge and NOTHING to clean up:
    #     the branch content never reached `<base>`, so an owned worktree/branch holds UNMERGED work that
    #     removing it would destroy. ANY non-terminal row — `in_review` OR any held status
    #     (`L.HELD_STATUSES`) — is a real close-out: a CLOSED PR moots every held reason, and a human closing
    #     a parked PR IS the resolution. Only a `merged` row with a CLOSED live state is a contradiction (a
    #     merged PR reports MERGED, not CLOSED), left to the terminal status gate below.
    #   * aborted_repeat: an already-`aborted` row whose PR is still CLOSED — the terminal-repeat no-op,
    #     symmetric with the `merged`-repeat below.
    #   Both are LEDGER-ONLY (record `aborted`/no-op, merge and clean nothing), so both drop the live
    #   head/base/branch pins (`check_live_refs=False`): a push or base/branch rename before the CLOSE must
    #   not wedge a settled row, and a CLOSED PR never re-enters the open snapshot to be re-gated.
    close_out = view["state"] == "CLOSED" and row["status"] not in ("merged", "aborted")
    aborted_repeat = row["status"] == "aborted" and view["state"] == "CLOSED"
    ledger_only = close_out or aborted_repeat
    # merge_initiating is the ONE path that STARTS a merge — a live OPEN state on an in_review row. It is the
    # only path that requires the full merge-tip pins, RESOLVED ownership, and this run's OWN-label presence:
    # a half-adoption (unresolved ownership, no own label yet) must never be merged. Every other path only
    # finalizes an already-decided outcome (an external MERGE, a CLOSE, or a terminal repeat), so it merges
    # nothing and tolerates a half-adoption (which then owns nothing to clean) rather than wedging it.
    merge_initiating = view["state"] == "OPEN" and row["status"] == "in_review"
    _validate_state(header, row, pr, root, view,
                    check_live_refs=not ledger_only,
                    require_resolved_ownership=merge_initiating,
                    require_own_label=merge_initiating)
    if close_out:
        _mark_terminal(ledger, pr, "aborted")
        return {"status": "closed-unmerged", "pr": pr, "cleanup": {}}

    if row["status"] == "aborted":
        # Terminal-repeat, symmetric with the `merged` no-op below (both terminal statuses are safe to
        # repeat). A CLOSED live state confirms the recorded abort still holds -> the same already-complete
        # no-op, no ledger write. A live OPEN or MERGED state CONTRADICTS the terminal `aborted` row (the PR
        # is no longer closed-without-merge); refuse naming the mismatch, never a silent no-op.
        if view["state"] != "CLOSED":
            raise Refusal(
                f"terminal ledger row says aborted but GitHub state is {view['state']!r}, not CLOSED")
        return {"status": "already-complete", "pr": pr, "cleanup": {}}

    if row["status"] == "merged":
        if view["state"] != "MERGED":
            raise Refusal(
                f"terminal ledger row says merged but GitHub state is {view['state']!r}")
        return {"status": "already-complete", "pr": pr, "cleanup": {}}

    # Only NONTERMINAL rows (in_review or a held status) remain. Classify by the LIVE state, not the row
    # status, so an EXTERNAL merge finalizes the landed work regardless of a held row.
    if view["state"] == "MERGED":
        # A maintainer merged the exact reviewed head while this row was still nonterminal — in_review OR
        # held (`L.HELD_STATUSES`). The full ownership validation above confirmed our reviewed head/base/
        # branch on our owned resources, so the work LANDED: fall through to resume the owed base-sync /
        # owned cleanup / terminal write. This is the ONLY way a held row proceeds; the campaign still never
        # INITIATES a merge on a held (or OPEN) PR — that stays refused just below.
        pass
    elif row["status"] != "in_review":
        # A nonterminal, non-MERGED row that is not in_review is HELD (or malformed). The campaign must
        # never INITIATE a merge on it. (CLOSED already closed out above; a live-MERGED held row already
        # resumed above — only a live OPEN held row reaches here.)
        if row["status"] in L.HELD_STATUSES:
            raise Refusal(f"PR {pr} is held ({row['status']})")
        raise Refusal(f"PR {pr} row status is {row['status']!r}, not in_review")
    elif view["state"] == "OPEN":
        _require_ready(row, header, view)
        # --match-head-commit pins the merge to the exact reviewed SHA. If a push advanced the live tip
        # in the window between the pre-merge view and this call, GitHub refuses fail-closed rather than
        # squashing the unreviewed head; the post-merge re-validation would only detect that after landing.
        # Base is pinned only PRE-merge (the _validate_state above, check_live_refs) and re-checked POST-merge
        # (confirmed, below). gh pr merge exposes no expected-base compare-and-swap — only --match-head-commit —
        # so a base retarget in this window lands the reviewed HEAD onto the new base and is caught only after
        # the fact. Accepted single-user residual (intent-134 Non-goal): head SHA is pinned, so only reviewed
        # CONTENT lands, and the actor is the single user retargeting their own open PR. Falsify + fix here if
        # gh pr merge ever gains an expected-base / --match-base flag: pin the base at the mutation boundary too.
        merge_argv = ["gh", "pr", "merge", pr, "--repo", repo, f"--{merge_method}",
                      "--match-head-commit", row["head_sha"]]
        merge_proc = _run(merge_argv, cwd=str(root))
        # A transport failure can happen after GitHub accepted the merge. Re-read state before deciding
        # whether this phase failed; MERGED is the durable checkpoint and prevents a second merge attempt.
        confirmed = _view(pr, repo, root)
        _validate_state(header, row, pr, root, confirmed)
        if confirmed["state"] != "MERGED":
            if merge_proc.returncode != 0:
                _require(merge_proc, f"merge of PR {pr}")
            # Out of scope: a base branch that requires a GitHub merge queue. There, `gh pr merge` returns
            # success while leaving the PR OPEN-and-queued, so this branch raises Refusal WITHOUT mutating
            # any state (no _sync_base / _cleanup / _mark_terminal below run). That is fail-CLOSED and safe:
            # no duplicate merge (a reissue is pinned to head_sha by --match-head-commit above) and no
            # ledger write. This repo runs no merge-queue ruleset; the queued-OPEN response is only
            # reachable if the single user enables a merge queue on their own repo. Resuming an accepted
            # queued request is a deliberate feature gap, not a defect.
            raise Refusal(
                f"merge command returned success but PR {pr} state is {confirmed['state']!r}, not MERGED")
        view = confirmed
    else:
        # in_review but the live state is neither OPEN nor MERGED (CLOSED already finalized above).
        raise Refusal(f"PR {pr} state is {view['state']!r}; expected OPEN, MERGED, or CLOSED")

    # MERGED is confirmed before any local ref or worktree is changed. Each following phase is idempotent.
    # Post-merge local sync targets THIS row's base — after a v3 PR, local v3; after a main PR, local main.
    _sync_base(root, L.effective_base(header, row))
    cleanup = _cleanup(root, row)
    _mark_terminal(ledger, pr, "merged")
    return {"status": "merged", "pr": pr, "cleanup": cleanup}


class SelfTestFailure(AssertionError):
    """A claimed merge-runner rule did not hold."""


def _sibling_cases() -> list:
    if not SIBLING.exists():
        raise SelfTestFailure(f"fixture suite is missing at {SIBLING}")
    mod = load_module_from_path("merge_runner_test", SIBLING, register=True)
    if mod is None:
        raise SelfTestFailure(f"cannot load fixture suite at {SIBLING}")
    cases = getattr(mod, "CASES", None)
    if not cases:
        raise SelfTestFailure(f"{SIBLING.name} exports no CASES")
    return list(cases)


def self_test() -> int:
    failures = 0
    try:
        cases = _sibling_cases()
    except SelfTestFailure as exc:
        print(f"FAIL sibling-fixtures: {exc}")
        return 1
    for name, rule, fn in cases:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {rule}\n     {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}: {rule}")
    if failures:
        print(f"{failures} merge-runner fixture(s) failed")
        return 1
    print(f"all {len(cases)} merge-runner fixtures hold")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=next(iter((__doc__ or "").splitlines()), ""))
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="resume or execute one proven-ready merge")
    run.add_argument("--ledger", required=True, type=Path)
    run.add_argument("--pr", required=True)
    run.add_argument("--project-root", required=True, type=Path)
    run.add_argument("--repo", required=True)
    run.add_argument("--merge-method", default="squash", choices=MERGE_METHODS,
                     help="merge method (default: squash; use the repo's prevailing method if squash is disabled)")
    sub.add_parser("self-test", help="run every fixture in merge-test.py")
    args = parser.parse_args(argv)
    if args.cmd == "self-test":
        return self_test()
    try:
        result = execute(args.ledger, str(args.pr), args.project_root, args.repo,
                         merge_method=args.merge_method)
    except (Refusal, SystemExit) as exc:
        detail = exc if isinstance(exc, Refusal) else "ledger rejected malformed state"
        # Byte-exact final boundary: detail can carry a surrogateescape-decoded raw git byte (U+DCFF for a
        # 0xff filename byte under core.quotePath=false — see _ff_detail). A text sys.stderr would apply the
        # default backslashreplace and emit the 6 ASCII chars `\udcff` instead of the verbatim byte, breaking
        # the "raw git diagnostic appended verbatim" guarantee at the one surface the CLI user sees. Encode
        # with surrogateescape and write raw. The tailored JSON path list is pure ASCII (json.dumps,
        # ensure_ascii=True), so it stays line-safe; only git's own detail round-trips to raw bytes. Flush:
        # the process returns immediately after.
        sys.stderr.buffer.write(f"merge: REFUSED — {detail}\n".encode("utf-8", "surrogateescape"))
        sys.stderr.buffer.flush()
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
