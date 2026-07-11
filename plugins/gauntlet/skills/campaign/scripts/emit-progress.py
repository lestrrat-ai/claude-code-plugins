#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NoReturn

STATUS_STARTED = "started"
STATUS_DONE = "done"
ALLOWED_STATUSES = (STATUS_STARTED, STATUS_DONE)


def fail(msg: str) -> NoReturn:
    print(f"emit-progress: {msg}", file=sys.stderr)
    raise SystemExit(1)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Append one canonical reviewer progress event to a progress.jsonl file."
    )
    parser.add_argument("--file", required=True, help="path to the progress.jsonl to append to")
    parser.add_argument("--unit", required=True, help="plan unit id this event is about")
    parser.add_argument("--status", required=True, choices=ALLOWED_STATUSES, help="started or done")
    parser.add_argument("--evidence", help="concrete citation; required for --status done")
    return parser.parse_args(argv)


def build_event(unit: str, status: str, evidence: "str | None") -> dict:
    unit = unit.strip()
    if not unit:
        fail("--unit must be non-empty")

    if status == STATUS_STARTED:
        if evidence is not None:
            fail("--evidence must NOT be provided when --status is started")
        return {"type": "progress", "unit": unit, "status": STATUS_STARTED}

    # status == STATUS_DONE
    if evidence is None or not evidence.strip():
        fail("--evidence is required and must be non-empty when --status is done")
    return {
        "type": "progress",
        "unit": unit,
        "status": STATUS_DONE,
        "evidence": evidence,
    }


def append_event(path: Path, event: dict) -> str:
    line = json.dumps(event, separators=(",", ":")) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as outfile:
        outfile.write(line)
    return line


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    event = build_event(args.unit, args.status, args.evidence)
    line = append_event(Path(args.file), event)
    sys.stdout.write(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
