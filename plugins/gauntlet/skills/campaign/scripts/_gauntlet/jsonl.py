"""Shared JSONL object reader for Gauntlet campaign stores."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import cast


class JsonlError(ValueError):
    """A malformed JSONL record, with its source line number."""


def object_lines(text: str) -> "Iterator[tuple[int, dict[str, object]]]":
    """Yield non-blank JSON object records as ``(line_number, record)`` pairs."""
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise JsonlError(f"malformed JSON on line {number}: {exc}") from exc
        if not isinstance(record, dict):
            raise JsonlError(f"line {number}: record is not a JSON object")
        yield number, cast(dict[str, object], record)
