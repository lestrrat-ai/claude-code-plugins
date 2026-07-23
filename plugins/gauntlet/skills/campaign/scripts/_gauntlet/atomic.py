# ci: pyright
"""Atomic text-file replacement shared by Gauntlet's local stores."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def replace_text(
    path: Path,
    text: str,
    *,
    temp_prefix: str,
    encoding: str | None = None,
    mode: int | None = None,
) -> None:
    """Durably replace ``path`` with ``text`` through a same-directory temp file.

    The caller owns directory creation, serialization, locking, error conversion,
    encoding choice, and the target's permission policy. This function owns only
    the shared write, flush, fsync, rename, and failure-cleanup sequence.
    """
    fd, temp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=temp_prefix,
        suffix=".tmp",
    )
    temp = Path(temp_name)
    try:
        if encoding is None:
            stream = os.fdopen(fd, "w")
        else:
            stream = os.fdopen(fd, "w", encoding=encoding)
        with stream:  # fdopen owns and closes the descriptor
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
