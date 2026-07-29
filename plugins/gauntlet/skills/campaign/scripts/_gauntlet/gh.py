# ci: pyright
"""The `gh pr view` fetch shared by Gauntlet campaign deciders."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _detail(exc: Exception) -> str:
    """One failure, described so the message is never EMPTY and always names what went wrong.

    The catches below are total, so they see exceptions raised with NO message at all — `MemoryError` is one
    the parses can genuinely produce — and a reason line reading `could not read ...:` with nothing after the
    colon tells a reader nothing. The type name is always there, so it always leads; the message, when there
    is one, follows it.
    """
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


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

    THE GUARANTEE IS TOTAL, AND IT IS DELIBERATELY NOT A LIST: NO `Exception` ESCAPES THIS FUNCTION. Reading
    the recorded file, spawning `gh`, and parsing either response are each wrapped whole, so however the read,
    the spawn or the parse fails, the failure leaves as an error string. DO NOT REPLACE THAT WITH THE TYPES
    THAT HAPPEN TO REACH IT TODAY. Naming them is what this function did for three rounds, and each round
    shipped exactly the one type the round had seen: the parse alone can raise a decode error, a recursion
    error and a plain integer-limit `ValueError`, and the next input nobody has thought of raises something
    else again. An enumeration cannot terminate; the wrapper can.

    OVER-CATCHING IS THE CHEAP MISTAKE HERE AND UNDER-CATCHING IS THE EXPENSIVE ONE — that asymmetry, not
    tidiness, is why the catch is wide. Each `try` body is tiny and fixed (a read plus a parse, a single
    call, a single parse), so there is almost no room for a programming error to hide in one; if one did, it
    becomes a value, the caller wraps it in its own error type, and the run still ends in a structured
    verdict. Under-catching ends it in a traceback with NO verdict, from the one call every caller relies on
    to fail closed. `Exception` is also the widest catch that is SAFE: `KeyboardInterrupt`, `SystemExit` and
    `GeneratorExit` derive from `BaseException` alone, so an operator's Ctrl-C and a caller's exit still
    propagate. NEVER widen this to `BaseException`.

    BRANCH ON `error is not None`, NEVER ON `view is None`. `null` is valid JSON, so a recorded `null` is a
    SUCCESSFUL read of a view that is malformed — a different thing from a fetch that failed, and not this
    function's call to make. Nothing here inspects the SHAPE of the response: the fields a view must carry
    differ per caller, and validating them is the caller's boundary, not the fetch's.
    """
    if view_json is not None:
        try:
            return json.loads(Path(view_json).read_text(encoding="utf-8")), None
        except Exception as exc:  # noqa: BLE001 — the total guarantee above
            return None, f"could not read the recorded view {view_json}: {_detail(exc)}"
    argv = ["gh", "pr", "view", str(pr)]
    if repo:
        argv += ["--repo", repo]
    argv += ["--json", fields]
    try:
        proc = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, check=False, cwd=cwd)
    except Exception as exc:  # noqa: BLE001 — the total guarantee above
        return None, f"could not run `gh pr view {pr}`: {_detail(exc)}"
    if proc.returncode != 0:
        return None, f"`gh pr view {pr}` exited {proc.returncode}: {proc.stderr.strip()}"
    try:
        return json.loads(proc.stdout), None
    except Exception as exc:  # noqa: BLE001 — the total guarantee above
        return None, f"could not read the `gh pr view {pr}` response: {_detail(exc)}"
