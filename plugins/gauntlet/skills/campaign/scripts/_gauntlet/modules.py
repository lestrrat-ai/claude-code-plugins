# ci: pyright
"""Load sibling Gauntlet scripts whose filenames are not importable module names."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def load_module_from_path(module_name: str, path: Path, *, register: bool = False) -> ModuleType | None:
    """Execute ``path`` as ``module_name`` and return its module object.

    ``register`` mirrors the caller-controlled importlib sequence: when true, place the module in
    ``sys.modules`` before executing it and leave that entry in place even if execution fails. Exceptions
    raised while creating or executing the module pass through unchanged. An absent spec or loader returns
    ``None``, so callers retain ownership of their existing public error without catching any execution
    exception.
    """
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    if register:
        sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_sibling(module_name: str, directory: Path, filename: str, *, register: bool = False) -> ModuleType:
    """Load ``directory / filename`` as ``module_name``, raising instead of returning ``None``.

    This is the non-optional door onto ``load_module_from_path`` for the campaign tools that load a sibling
    script by its own directory. It changes exactly one thing: an absent spec or loader — a BROKEN INSTALL,
    never an input error — becomes ``RuntimeError(f"cannot load {filename}")`` so the caller's binding is a
    real module at every use. Everything else is the underlying loader's, unchanged and NEVER restated here:
    ``register`` still decides ``sys.modules`` placement (default off, so repeated loads of one file under
    different names leave no entries behind), and exceptions raised while executing the module still pass
    through untouched — a failed module is the module's own error, not a load failure.

    Callers whose install error is not that exact ``RuntimeError`` — a different message, a ``SystemExit``,
    a printed diagnostic — keep calling ``load_module_from_path`` and own their own ``None`` handling.
    """
    module = load_module_from_path(module_name, directory / filename, register=register)
    if module is None:
        raise RuntimeError(f"cannot load {filename}")
    return module
