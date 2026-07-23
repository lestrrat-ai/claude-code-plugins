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
