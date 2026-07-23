#!/usr/bin/env python3
# ci: pyright
"""SKILL.md's Bundled Scripts table vs scripts/ — the COMPLETE-by-contract claim, executed.

The table in SKILL.md ("Bundled Scripts") declares itself COMPLETE: one row per bundled script,
sibling `*-test.py` suites and package internals aside. A completeness claim with no check goes stale
the first time a script lands without a row — by an author who never read the claim. This suite is the
check. It fails when a script has no row (the table lies by omission), when a row names no script (the
table lies by invention), and when a name appears twice. A scan that matches nothing is itself a
failure: a missing section anchor, zero parsed rows, or an empty scripts directory can only mean the
layout moved, never that all is well.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SKILL = SCRIPTS.parent / "SKILL.md"
SECTION = "## Bundled Scripts"
ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|")


def sibling_owner(name: str) -> str | None:
    """The accessor a `<owner>-test.py` sibling suite belongs to, else None."""
    if name.endswith("-test.py"):
        return name[: -len("-test.py")] + ".py"
    return None


def bundled_scripts() -> set[str]:
    """Every top-level script that the table owes a row.

    Package internals and fixtures live in subdirectories, so non-recursion excludes them. A sibling
    suite is covered by its accessor's row; a `*-test.py` with NO accessor sibling is a standalone
    suite and owes a row of its own (`transport-contract-test.py`, this file).
    """
    names: set[str] = set()
    for path in sorted(SCRIPTS.iterdir()):
        if not path.is_file() or path.suffix not in (".py", ".sh"):
            continue
        owner = sibling_owner(path.name)
        if owner is not None and (SCRIPTS / owner).is_file():
            continue
        names.add(path.name)
    return names


def table_names(text: str) -> list[str]:
    """First-cell script names of every table row in the Bundled Scripts section, in order."""
    start = text.find(SECTION)
    if start < 0:
        return []
    section = text[start:]
    stop = section.find("\n## ")
    if stop > 0:
        section = section[:stop]
    names: list[str] = []
    for line in section.splitlines():
        match = ROW.match(line)
        if match is not None:
            names.append(match.group(1))
    return names


def main() -> int:
    failures = 0

    def fail(message: str) -> None:
        nonlocal failures
        failures += 1
        print(f"FAIL     {message}")

    if not SKILL.is_file():
        fail(f"{SKILL} does not exist — the checker is not where it thinks it is")
        return 1
    text = SKILL.read_text(encoding="utf-8")
    if SECTION not in text:
        fail(f'SKILL.md has no "{SECTION}" section — the anchor this scan needs moved, so the scan '
             f"checks nothing")
        return 1

    rows = table_names(text)
    scripts = bundled_scripts()
    if not rows:
        fail("the Bundled Scripts section has no parseable table row — a scan that matches nothing "
             "passes every time and checks nothing")
    if not scripts:
        fail(f"{SCRIPTS} yields no bundled script at all — the directory this scan reads moved")
    if failures:
        return 1

    for name in sorted({n for n in rows if rows.count(n) > 1}):
        fail(f"`{name}` has {rows.count(name)} table rows — one script, one row")

    for name in sorted(scripts - set(rows)):
        fail(f"`{name}` is in scripts/ but has no table row — the COMPLETE-by-contract claim is now "
             f"a lie by omission; add its row in SKILL.md, 'Bundled Scripts'")

    for name in sorted(set(rows) - scripts):
        owner = sibling_owner(name)
        hint = (" (a sibling suite is covered by its accessor's row, not its own)"
                if owner is not None and (SCRIPTS / owner).is_file() else "")
        fail(f"table row `{name}` names no bundled script{hint} — the table lies by invention; "
             f"drop or rename the row")

    if failures:
        return 1
    print(f"script table: {len(rows)} rows, {len(scripts)} scripts — the table and the directory agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
