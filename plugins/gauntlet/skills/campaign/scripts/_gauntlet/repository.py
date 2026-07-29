"""The `owner/name` check every Gauntlet campaign tool applies to an explicit `--repo`."""

from __future__ import annotations

import re

# GitHub repository coordinates are ASCII identifiers. Owners use alphanumerics and single, non-edge
# hyphens; repository names add `.`, `_`, and unrestricted hyphens. GitHub owns both length limits; the
# named constants below are this repository's defining sites for them.
OWNER_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
REPOSITORY_RE = re.compile(r"[A-Za-z0-9._-]+")
OWNER_MAX_LENGTH = 39
REPOSITORY_MAX_LENGTH = 100


def repo_problem(repo: str) -> "str | None":
    """`None` if `repo` is a well-formed `owner/name`; otherwise why it is not. PURE — no I/O, no raising.

    An explicit `--repo` is a CALLER INPUT, not a GitHub read result, and it is interpolated into `gh`
    argument vectors by every tool that takes it. Six tools took it; one checked it. The other five
    accepted anything at all and handed it to `gh`, which answers with its own error about a repository
    the caller never meant to name — or, for a value that happens to resolve, about the WRONG one.

    This owns the CHECK and its WORDING. Each caller keeps its own refusal: they exit through different
    doors — `fail`, a `Refusal`, an `argparse` error — and which door a tool uses is that tool's business.
    """
    parts = repo.split("/")
    if (len(parts) != 2
            or not 1 <= len(parts[0]) <= OWNER_MAX_LENGTH
            or not 1 <= len(parts[1]) <= REPOSITORY_MAX_LENGTH
            or OWNER_RE.fullmatch(parts[0]) is None
            or REPOSITORY_RE.fullmatch(parts[1]) is None):
        return f"--repo {repo!r} is not a valid GitHub owner/name"
    return None
