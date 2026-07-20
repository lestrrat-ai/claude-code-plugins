#!/usr/bin/env python3
"""Executable fixtures for `worker-prompt.py` and its canonical prompt template."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from _gauntlet.modules import load_module_from_path
from _gauntlet.testing import capture_cli

OWNER = Path(__file__).resolve().parent / "worker-prompt.py"


def _load_owner():
    module = load_module_from_path("worker_prompt_owner", OWNER)
    if module is None:
        raise RuntimeError(f"cannot load worker-prompt tool at {OWNER}")
    return module


M = _load_owner()
ISSUES = b"- src/widget.py:19: preserve literal {{ROLE}} and $(touch NEVER)\n"
LOGS = b"lint: src/widget.py needs layout; literal @@END COMMON@@ and unicode \xe9\x9b\xaa\n"
# A deterministic, host-neutral stand-in for the driver-resolved format-preflight.py path so the goldens
# below stay reproducible. The real bound path (M.FORMAT_PREFLIGHT) is exercised by the economy fixture.
FIXTURE_FORMAT_PREFLIGHT = "/fixture/skill/scripts/format-preflight.py"
GOLDEN_PROMPT_SHA256 = {
    "review": "586d63c999e4b027def4a5748ba4b88c6e31f5910bcd1f895df548e178f0acac",
    "ci-session": "07bad4c143b866ad03094cc0916ac3a8f17ad327dab932e527156f8f7be727f3",
    "ci-economy": "f521bbe436bbf0b6ddffe23824008346757a577a898e055f234406e6a29d59fa",
}
GOLDEN_METADATA_SHA256 = {
    "review": "e070e3e5618e093696610da7a0fd47cccaf44f3c01f9640154b4774c55a9c65d",
    "ci-session": "5a8ef32ab7a6f2227da4d2150dbea22f59fa26fd784246f1ffb06b873ac4e5c9",
    "ci-economy": "4c1b13ffe2d88d78c5968cec542dc98821c0681cdc9f83ed5fbcbe93e75c2979",
}


def check(condition: bool, message: str) -> None:
    if not condition:
        raise M.SelfTestFailure(message)


def expect_refusal(call, message: str) -> None:
    try:
        call()
    except M.Refusal:
        return
    raise M.SelfTestFailure(message)


def fixed_render(role: str) -> bytes:
    return M.render_prompt(
        role=role,
        project_root="/fixture/repository root",
        worktree="/fixture/worktree $(literal)",
        pr=42,
        base="main",
        issues=ISSUES,
        logs=None if role == "review" else LOGS,
        format_preflight=FIXTURE_FORMAT_PREFLIGHT,
        sections=M.load_template(),
    )


def t_golden_bytes_and_metadata() -> None:
    """Every role has byte-exact prompt and metadata goldens, not substring-only health checks."""
    for role in M.ROLES:
        prompt = fixed_render(role)
        prompt_digest = hashlib.sha256(prompt).hexdigest()
        metadata = M.metadata_bytes(role, prompt)
        metadata_digest = hashlib.sha256(metadata).hexdigest()
        check(prompt_digest == GOLDEN_PROMPT_SHA256[role],
              f"{role} prompt bytes changed: {prompt_digest}")
        check(metadata_digest == GOLDEN_METADATA_SHA256[role],
              f"{role} metadata bytes changed: {metadata_digest}")


def t_roles_include_only_their_blocks() -> None:
    prompts = {role: fixed_render(role) for role in M.ROLES}
    for role, prompt in prompts.items():
        check(M.MODEL_CLASS[role].encode() in prompt, f"{role} prompt lost its logical model class")
        check(b"[GAUNTLET_FIX_SCOPE_V1]" in prompt and b"[GAUNTLET_FIX_SWEEP_V1]" in prompt and
              b"[GAUNTLET_FIX_REPORT_V1]" in prompt,
              f"{role} prompt lost a shared scope/sweep/report block")
    check(b"[GAUNTLET_FIX_REVIEW_V1]" in prompts["review"], "review prompt lost its role block")
    check(b"BEGIN EXACT LOG DATA" not in prompts["review"], "review prompt gained a CI log block")
    check(b"[GAUNTLET_FIX_CI_NO_WEAKENING_V1]" not in prompts["review"],
          "review prompt gained a CI prohibition")
    for role in ("ci-session", "ci-economy"):
        check(b"[GAUNTLET_FIX_CI_NO_WEAKENING_V1]" in prompts[role],
              f"{role} prompt lost the all-CI no-weakening prohibition")
        check(b"BEGIN EXACT LOG DATA" in prompts[role], f"{role} prompt lost exact log data")
        check(b"[GAUNTLET_FIX_REVIEW_V1]" not in prompts[role], f"{role} prompt gained review rules")
    check(b"[GAUNTLET_FIX_CI_ECONOMY_PROHIBITIONS_V1]" in prompts["ci-economy"],
          "economy prompt lost its formatter prohibitions")
    check(b"[GAUNTLET_FIX_CI_ECONOMY_PROHIBITIONS_V1]" not in prompts["ci-session"],
          "session CI prompt gained economy restrictions")


def t_payload_is_bound_once_as_data() -> None:
    prompt = fixed_render("ci-session")
    check(prompt.count(ISSUES) == 1, "issue bytes were changed or inserted more than once")
    check(prompt.count(LOGS) == 1, "log bytes were changed or inserted more than once")
    check(b"{{ROLE}}" in prompt, "a template-looking slot inside issue data was rebound")
    check(b"@@END COMMON@@" in prompt, "a section-looking marker inside log data was parsed")
    check(b"$(touch NEVER)" in prompt, "shell-looking issue bytes were changed")


def t_corrupt_templates_are_refused() -> None:
    """Regression: correct prose cannot hide a dispatched prompt missing required blocks."""
    source = M.TEMPLATE.read_bytes()
    fixtures = {
        "missing shared sweep/report": source.replace(b"[GAUNTLET_FIX_SWEEP_V1]", b"", 1),
        "missing economy prohibition": source.replace(
            b"[GAUNTLET_FIX_CI_ECONOMY_PROHIBITIONS_V1]", b"", 1),
    }
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        for name, data in fixtures.items():
            path = root / (name.replace(" ", "-").replace("/", "-") + ".txt")
            path.write_bytes(data)
            expect_refusal(lambda path=path: M.load_template(path),
                           f"template fixture {name!r} was accepted")


def t_invalid_inputs_are_refused() -> None:
    sections = M.load_template()
    common = dict(role="review", project_root="/repo", worktree="/worktree", pr=1, base="main",
                  issues=ISSUES, logs=None, format_preflight=FIXTURE_FORMAT_PREFLIGHT, sections=sections)
    expect_refusal(lambda: M.render_prompt(**{**common, "role": "unknown"}),
                   "an invalid role was accepted")
    expect_refusal(lambda: M.render_prompt(**{**common, "logs": LOGS}),
                   "review role accepted a log payload")
    expect_refusal(lambda: M.render_prompt(**{**common, "role": "ci-session"}),
                   "CI session role accepted missing logs")
    expect_refusal(lambda: M.render_prompt(**{**common, "role": "ci-economy"}),
                   "CI economy role accepted missing logs")
    expect_refusal(lambda: M.render_prompt(**{**common, "issues": b""}),
                   "empty issues were accepted")
    expect_refusal(lambda: M.render_prompt(**{**common, "issues": b"bad\x00bytes"}),
                   "NUL issue bytes were accepted")
    expect_refusal(lambda: M.render_prompt(**{**common, "issues": b"\xff"}),
                   "non-UTF-8 issue bytes were accepted")


def t_cli_requires_preflight_and_role_inputs() -> None:
    with tempfile.TemporaryDirectory(prefix="worker prompt cli ") as raw:
        root = Path(raw)
        repo = root / "repo"
        worktree = root / "worktree"
        repo.mkdir()
        worktree.mkdir()
        issues = root / "issues.bin"
        logs = root / "logs.bin"
        issues.write_bytes(ISSUES)
        logs.write_bytes(LOGS)
        base = ["fix", "--role", "review", "--project-root", str(repo), "--worktree",
                str(worktree), "--pr", "42", "--base", "main", "--issues-file", str(issues),
                "--output-dir", str(root / "out")]
        code, _, _ = capture_cli(M.main, base)
        check(code != 0, "missing --preflight-verdict passed")
        code, _, err = capture_cli(M.main, [*base, "--preflight-verdict", "rebase-first"])
        check(code == M.EXIT_REFUSED and "exactly 'proceed'" in err,
              "non-proceed preflight was not a controlled refusal")
        ci = ["fix", "--role", "ci-session", "--project-root", str(repo), "--worktree",
              str(worktree), "--pr", "42", "--base", "main", "--preflight-verdict", "proceed",
              "--issues-file", str(issues), "--output-dir", str(root / "ci-out")]
        code, _, err = capture_cli(M.main, ci)
        check(code == M.EXIT_REFUSED and "requires --logs-file" in err,
              "CI role without logs was not a controlled refusal")


def t_atomic_bundle_and_conflict_refusal() -> None:
    with tempfile.TemporaryDirectory(prefix="worker prompt atomic ") as raw:
        root = Path(raw)
        output = root / "artifact with spaces $(literal)"
        prompt = fixed_render("review")
        metadata = M.metadata_bytes("review", prompt)
        M.publish_bundle(output, prompt, metadata)
        check((output / M.PROMPT_NAME).read_bytes() == prompt, "published prompt bytes changed")
        check((output / M.METADATA_NAME).read_bytes() == metadata, "published metadata bytes changed")
        record = json.loads(metadata)
        check(record["role"] == "review" and record["model_class"] == "session",
              "metadata lost the role or logical model class")
        check(record["prompt_sha256"] == hashlib.sha256(prompt).hexdigest(),
              "metadata digest is not bound to exact prompt bytes")
        expect_refusal(lambda: M.publish_bundle(output, prompt, metadata),
                       "an existing output bundle was overwritten")


def t_partial_stage_rolls_back() -> None:
    with tempfile.TemporaryDirectory(prefix="worker prompt rollback ") as raw:
        root = Path(raw)
        output = root / "bundle"
        calls = 0

        def fail_second(path: Path, data: bytes) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("fixture failure after prompt stage")
            M._write_file(path, data)

        expect_refusal(lambda: M.publish_bundle(output, b"prompt", b"metadata", writer=fail_second),
                       "a staged metadata failure was not refused")
        check(not output.exists(), "a partial output bundle became visible")
        check(not list(root.iterdir()), "rollback left a staging directory behind")


def t_paths_and_payload_files_fail_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="worker prompt paths ") as raw:
        root = Path(raw)
        regular = root / "issues"
        regular.write_bytes(ISSUES)
        link = root / "issues-link"
        link.symlink_to(regular)
        expect_refusal(lambda: M._read_payload(str(link), "issues"), "symlink payload was accepted")
        expect_refusal(lambda: M.validate_context_path("relative/path", "worktree"),
                       "relative context path was accepted")
        expect_refusal(lambda: M.validate_context_path(str(root) + "\nforged", "worktree"),
                       "control character in context path was accepted")
        symlink_parent = root / "linked-parent"
        real_parent = root / "real-parent"
        real_parent.mkdir()
        symlink_parent.symlink_to(real_parent, target_is_directory=True)
        expect_refusal(lambda: M.publish_bundle(symlink_parent / "out", b"p", b"m"),
                       "symlink output parent was accepted")


def t_publish_probe_oserror_is_controlled_refusal() -> None:
    """A pre-staging OSError (overlong basename, unwritable parent) is exit-2 REFUSED, not a traceback."""
    with tempfile.TemporaryDirectory(prefix="worker prompt probe ") as raw:
        root = Path(raw)
        repo = root / "repo"
        worktree = root / "worktree"
        repo.mkdir()
        worktree.mkdir()
        issues = root / "issues.bin"
        issues.write_bytes(ISSUES)

        def fix_argv(output_dir: Path) -> list:
            return ["fix", "--role", "review", "--project-root", str(repo), "--worktree",
                    str(worktree), "--pr", "42", "--base", "main", "--preflight-verdict", "proceed",
                    "--issues-file", str(issues), "--output-dir", str(output_dir)]

        # Overlong output basename: `output_dir.exists()` raises ENAMETOOLONG before os.rename.
        overlong = root / ("o" * 300)
        code, _, err = capture_cli(M.main, fix_argv(overlong))
        check(code == M.EXIT_REFUSED,
              f"overlong output basename exited {code}, not the controlled {M.EXIT_REFUSED}")
        check("REFUSED" in err and "Traceback" not in err,
              "overlong output basename was not a controlled refusal")
        # `overlong.exists()` would itself raise ENAMETOOLONG; list the parent instead.
        check(("o" * 300) not in os.listdir(root) and
              not any(name.startswith(".o") for name in os.listdir(root)),
              "overlong output basename left a partial bundle or staging dir")

        # Staging creation fails: tempfile.mkdtemp under a read-only parent raises OSError.
        readonly = root / "readonly"
        readonly.mkdir()
        target = readonly / "out"
        readonly.chmod(0o500)
        try:
            if os.access(readonly, os.W_OK):
                return  # a write override (e.g. root) defeats the perm bit; skip the mkdtemp-failure leg
            code, _, err = capture_cli(M.main, fix_argv(target))
            check(code == M.EXIT_REFUSED,
                  f"unwritable staging parent exited {code}, not the controlled {M.EXIT_REFUSED}")
            check("REFUSED" in err and "Traceback" not in err,
                  "unwritable staging parent was not a controlled refusal")
            check(not target.exists(), "unwritable staging parent left a partial bundle")
        finally:
            readonly.chmod(0o700)


def t_output_is_host_neutral() -> None:
    for role in M.ROLES:
        combined = fixed_render(role).lower() + M.metadata_bytes(role, fixed_render(role)).lower()
        check(b"claude" not in combined and b"codex" not in combined,
              f"{role} output contains a host-specific model or invocation")
        check(b"spawn_agent" not in combined and b"agent tool" not in combined,
              f"{role} output tries to select a launch mechanism")


def t_economy_binds_runnable_preflight_command() -> None:
    """The economy prompt ships a real absolute format-preflight.py path, not an unresolved placeholder."""
    bound = str(M.FORMAT_PREFLIGHT)
    check(bound.endswith("/scripts/format-preflight.py"),
          f"resolved format-preflight path is not the bundled script: {bound}")
    prompt = M.render_prompt(
        role="ci-economy",
        project_root="/fixture/repo",
        worktree="/fixture/worktree",
        pr=7,
        base="main",
        issues=ISSUES,
        logs=LOGS,
        format_preflight=bound,
        sections=M.load_template(),
    )
    check(b"<skill-dir>" not in prompt,
          "economy prompt still ships an unresolved <skill-dir> placeholder")
    check(b"{{FORMAT_PREFLIGHT}}" not in prompt and b"{{WORKTREE}}" not in prompt,
          "economy prompt left a preflight slot unbound")
    check(b"python3 " + bound.encode() + b" check --worktree /fixture/worktree" in prompt,
          "economy preflight command does not carry the bound absolute path and worktree")


TESTS = (
    ("golden bytes", t_golden_bytes_and_metadata),
    ("economy runnable preflight", t_economy_binds_runnable_preflight_command),
    ("role inclusion", t_roles_include_only_their_blocks),
    ("payload safety", t_payload_is_bound_once_as_data),
    ("missing prompt blocks", t_corrupt_templates_are_refused),
    ("invalid input combinations", t_invalid_inputs_are_refused),
    ("preflight and role CLI", t_cli_requires_preflight_and_role_inputs),
    ("atomic bundle and conflict", t_atomic_bundle_and_conflict_refusal),
    ("partial rollback", t_partial_stage_rolls_back),
    ("hostile paths and bytes", t_paths_and_payload_files_fail_closed),
    ("publish probe OSError refusal", t_publish_probe_oserror_is_controlled_refusal),
    ("host-neutral output", t_output_is_host_neutral),
)


def run_all() -> int:
    failures = 0
    for name, test in TESTS:
        try:
            test()
            print(f"PASS     {name}")
        except Exception as exc:  # fixture runner must report every independent failure
            failures += 1
            print(f"FAIL     {name}: {exc}")
    print(f"worker-prompt fixtures: {len(TESTS) - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run_all())
