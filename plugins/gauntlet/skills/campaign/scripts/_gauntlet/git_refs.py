"""Git ref selection shared by campaign base-fetch operations."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class BaseFetchRefs:
    """The fully qualified refspec and exact local ref for one fetched base."""

    refspec: str
    local_ref: str


def select_base_fetch_refs(
        worktree: str, remote: str, base: str) -> "tuple[BaseFetchRefs | None, str | None]":
    """Select a safe destination for fetching ``refs/heads/<base>``.

    The normal remote-tracking destination is retained unless it is symbolic. Git follows a symbolic
    fetch destination and updates its target, so those cases use a private ref keyed by the exact remote
    and branch names. Return a diagnostic instead of selecting any private candidate that is itself
    symbolic.
    """
    tracking_ref = f"refs/remotes/{remote}/{base}"
    probe = subprocess.run(  # noqa: S603
        ["git", "-C", worktree, "symbolic-ref", "--quiet", tracking_ref],
        capture_output=True, text=True, check=False)
    if probe.returncode not in (0, 1):
        return None, f"could not inspect fetch destination {tracking_ref}: {probe.stderr.strip()}"

    local_ref = tracking_ref
    if probe.returncode == 0:
        key = hashlib.sha256(f"{remote}\0{base}".encode("utf-8")).hexdigest()
        local_ref = f"refs/gauntlet/base-fetch/{key}"
        private_probe = subprocess.run(  # noqa: S603
            ["git", "-C", worktree, "symbolic-ref", "--quiet", local_ref],
            capture_output=True, text=True, check=False)
        if private_probe.returncode == 0:
            return None, f"private fetch destination {local_ref} is symbolic"
        if private_probe.returncode != 1:
            return (
                None,
                f"could not inspect private fetch destination {local_ref}: {private_probe.stderr.strip()}",
            )

    return BaseFetchRefs(f"+refs/heads/{base}:{local_ref}", local_ref), None
