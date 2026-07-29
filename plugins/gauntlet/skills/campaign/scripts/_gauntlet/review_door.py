"""Shared dispatch for the two public reviewer entry-point scripts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

from .modules import load_module_from_path


class _ReviewPassOwner(Protocol):
    def add_emit_args(self, parser: argparse.ArgumentParser) -> None: ...

    def add_finding_args(self, parser: argparse.ArgumentParser) -> None: ...

    def add_amendment_args(self, parser: argparse.ArgumentParser) -> None: ...

    def dispatch(self, args: argparse.Namespace) -> int: ...


def _load_owner(script: Path) -> _ReviewPassOwner:
    """Load ``review-pass.py`` beside ``script``, preserving the door's public install error."""
    resolved = script.resolve()
    owner_path = resolved.parent / "review-pass.py"
    loaded: ModuleType | None = load_module_from_path("review_pass", owner_path)
    if loaded is None:  # a broken install — never an input error
        print(f"{resolved.stem}: cannot load its owner at {owner_path}", file=sys.stderr)
        raise SystemExit(1)
    return cast(_ReviewPassOwner, loaded)


def _dispatch(
    *,
    script_file: str,
    description: str | None,
    argv: list[str] | None,
    add_args: str,
    cmd: str,
) -> int:
    """Build one wrapper parser from its owner and dispatch the parsed top-level command.

    ``add_args`` names the owner method that declares this door's flags — the ONE definition both this
    wrapper and the owner's subcommand call — and ``cmd`` is the owner subcommand ``set_defaults`` supplies,
    where no caller can type it and no usage line can advertise it.
    """
    script = Path(script_file)
    owner = _load_owner(script)
    parser = argparse.ArgumentParser(
        prog=script.name,
        description=(description or "").splitlines()[0],
    )
    getattr(owner, add_args)(parser)
    parser.set_defaults(cmd=cmd)
    return owner.dispatch(parser.parse_args(argv))


def dispatch_progress_door(script_file: str, description: str | None, argv: list[str] | None) -> int:
    """Run the public progress wrapper without exposing the owner's ``emit`` command word."""
    return _dispatch(script_file=script_file, description=description, argv=argv,
                     add_args="add_emit_args", cmd="emit")


def dispatch_finding_door(script_file: str, description: str | None, argv: list[str] | None) -> int:
    """Run the public finding wrapper without exposing the owner's ``finding-add`` command word."""
    return _dispatch(script_file=script_file, description=description, argv=argv,
                     add_args="add_finding_args", cmd="finding-add")


def dispatch_amendment_door(script_file: str, description: str | None, argv: list[str] | None) -> int:
    """Run the public amendment wrapper without exposing the owner's ``amend`` command word."""
    return _dispatch(script_file=script_file, description=description, argv=argv,
                     add_args="add_amendment_args", cmd="amend")
