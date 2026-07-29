# ci: pyright
"""The `gh pr view` fetch shared by Gauntlet campaign deciders."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def pr_view_json(pr: str, *, fields: str, repo: "str | None" = None, cwd: "str | None" = None,
                 view_json: "str | None" = None) -> "tuple[object, str | None]":
    """One PR's `--json` view, as `(view, error)` — exactly one of the pair is meaningful.

    `view_json` names a RECORDED response file and takes the place of the call, so a decider can be driven
    without `gh` and without a network. Otherwise the fetch is `gh pr view <pr> [--repo <repo>] --json
    <fields>`, run in `cwd` when one is given.

    THE ERROR COMES BACK AS A VALUE — it is never raised here and never swallowed. Every caller is a gate
    that must fail CLOSED on a view it could not obtain, and each spells that refusal in its own vocabulary
    (a `recheck`, a not-yet) with its own exception type. Returning the message leaves that wrapping to the
    caller while making the failure branch impossible to skip: there is no path here that hands back a view
    the fetch did not produce.

    EVERY failure path yields an error. A SPAWN failure raises `OSError` from `subprocess.run` itself,
    BEFORE any returncode exists — `gh` absent or not executable, or `cwd` not a usable directory. Uncaught,
    that prints a traceback and never fails closed, so it is caught here and joins the non-zero-exit and
    non-JSON branches, as does an unreadable or unparseable `view_json` file.

    UNDECODABLE BYTES ARE A FAILURE PATH, NOT A CRASH, and they raise from a place that looks like neither a
    read nor a parse. `UnicodeDecodeError` subclasses `ValueError`, so an `except OSError` never sees it and
    an `except json.JSONDecodeError` catches only the parse. It arrives from TWO spots: `read_text` on a
    recorded file whose bytes are not UTF-8, and — because `text=True` decodes inside `communicate()` —
    `subprocess.run` ITSELF when `gh`'s stdout or stderr is not UTF-8, i.e. from the very call the `OSError`
    clause guards. Both are caught, so invalid bytes from either source come back as an error value.

    DEEP NESTING IS A FAILURE PATH TOO, and it escapes BOTH clauses above. `json.loads` recurses per
    nesting level, so a response of enough open brackets raises `RecursionError` — whose bases are
    `RuntimeError` and `Exception`, so it is NOT a `ValueError`: neither `json.JSONDecodeError` nor
    `UnicodeDecodeError` can see it, and it is not an `OSError` either. Uncaught it is a traceback with no
    verdict, from the one call every caller relies on to fail closed. Both parses catch it.

    BRANCH ON `error is not None`, NEVER ON `view is None`. `null` is valid JSON, so a recorded `null` is a
    SUCCESSFUL read of a view that is malformed — a different thing from a fetch that failed, and not this
    function's call to make. Nothing here inspects the SHAPE of the response: the fields a view must carry
    differ per caller, and validating them is the caller's boundary, not the fetch's.
    """
    if view_json is not None:
        try:
            return json.loads(Path(view_json).read_text(encoding="utf-8")), None
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            return None, str(exc)
        except RecursionError as exc:
            return None, f"{view_json} is nested too deeply to parse ({exc})"
    argv = ["gh", "pr", "view", str(pr)]
    if repo:
        argv += ["--repo", repo]
    argv += ["--json", fields]
    try:
        proc = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, check=False, cwd=cwd)
    except OSError as exc:
        return None, f"could not run `gh pr view {pr}`: {exc}"
    except UnicodeDecodeError as exc:
        return None, f"`gh pr view {pr}` output is not valid UTF-8: {exc}"
    if proc.returncode != 0:
        return None, f"`gh pr view {pr}` exited {proc.returncode}: {proc.stderr.strip()}"
    try:
        return json.loads(proc.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"gh response is not JSON ({exc})"
    except RecursionError as exc:
        return None, f"gh response is nested too deeply to parse ({exc})"
