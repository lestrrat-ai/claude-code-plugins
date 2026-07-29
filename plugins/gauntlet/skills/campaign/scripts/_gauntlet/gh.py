"""The `gh pr view` fetch shared by Gauntlet campaign deciders."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _detail(exc: Exception) -> str:
    """One failure, described so the message is never EMPTY and always names what went wrong.

    ONLY THE TOTAL `except Exception` CLAUSES BELOW CALL THIS, and that is the whole reason it exists: being
    total, they see exceptions raised with NO message at all — `MemoryError` is one the parses can genuinely
    produce — and a reason line reading `could not read ...:` with nothing after the colon tells a reader
    nothing. The type name is always there, so it always leads; the message, when there is one, follows it.

    The narrower clauses that precede each total one do NOT call this, and must not start. They exist to
    reproduce a message from before this module owned the fetch, byte for byte, and a type-name prefix would
    change it. They also never need one: every type they name always carries a message of its own.
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

    THE NARROW `except` LEADING EACH BLOCK IS NOT THE LIST THE TOTAL GUARANTEE FORBIDS, AND CONFUSING THE
    TWO IS THE MISTAKE TO AVOID. The forbidden list is an attempt to enumerate the failures this function
    HANDLES; it cannot terminate, which is why the trailing `except Exception` on every block stays and is
    what keeps the handling total. The leading clauses enumerate a different thing: the failures that ALREADY
    HAD A MESSAGE before this fetch was extracted into one owner. That set is CLOSED and can never grow,
    because history fixes it rather than anything the code can raise — it is exactly what the two `load_view`
    bodies this replaced caught, and both raised byte-identical strings, so one owner reproduces them with no
    per-caller parameter. A failure absent from that set had no earlier message at all: it ended in a
    traceback and no verdict, so there is nothing about it that could drift. NEVER ADD A CLAUSE THERE. A new
    failure belongs to the total catch, whose wording this module owns outright.

    `_detail` STAYS ON THE TOTAL CLAUSES ONLY. Its type-name prefix exists for an exception carrying NO
    message, which is a property of a total catch; every type the leading clauses name always carries one
    (`JSONDecodeError` always builds `msg: line L column C (char N)`, and an `OSError` from a real syscall
    always carries errno and strerror), so those clauses need no prefix and cannot print a bare colon.

    ORDERING IS WHAT MAKES BOTH TRUE AT ONCE, and it is safe by subclassing rather than by luck: the
    integer-limit `ValueError` and `UnicodeDecodeError` are `ValueError`s but NOT `json.JSONDecodeError`s,
    so they fall past the leading clause to the total one, which is where they belong — they are new here.

    BRANCH ON `error is not None`, NEVER ON `view is None`. `null` is valid JSON, so a recorded `null` is a
    SUCCESSFUL read of a view that is malformed — a different thing from a fetch that failed, and not this
    function's call to make. Nothing here inspects the SHAPE of the response: the fields a view must carry
    differ per caller, and validating them is the caller's boundary, not the fetch's.
    """
    if view_json is not None:
        try:
            return json.loads(Path(view_json).read_text(encoding="utf-8")), None
        # HISTORICAL MESSAGE, exactly as the recorded-file read spelled it before this owner existed. See the
        # docstring's closed-set paragraph — this clause is not the enumeration the total guarantee forbids.
        except (OSError, json.JSONDecodeError) as exc:
            return None, str(exc)
        except Exception as exc:  # noqa: BLE001 — the total guarantee above
            return None, f"could not read the recorded view {view_json}: {_detail(exc)}"
    argv = ["gh", "pr", "view", str(pr)]
    if repo:
        argv += ["--repo", repo]
    argv += ["--json", fields]
    try:
        proc = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, check=False, cwd=cwd)
    # HISTORICAL MESSAGE, exactly as the spawn spelled it before this owner existed.
    except OSError as exc:
        return None, f"could not run `gh pr view {pr}`: {exc}"
    except Exception as exc:  # noqa: BLE001 — the total guarantee above
        return None, f"could not run `gh pr view {pr}`: {_detail(exc)}"
    if proc.returncode != 0:
        return None, f"`gh pr view {pr}` exited {proc.returncode}: {proc.stderr.strip()}"
    try:
        return json.loads(proc.stdout), None
    # HISTORICAL MESSAGE, exactly as the response parse spelled it before this owner existed.
    except json.JSONDecodeError as exc:
        return None, f"gh response is not JSON ({exc})"
    except Exception as exc:  # noqa: BLE001 — the total guarantee above
        return None, f"could not read the `gh pr view {pr}` response: {_detail(exc)}"
