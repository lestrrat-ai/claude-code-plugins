"""The `Z`-suffixed UTC stamp shared by Gauntlet campaign stores."""

from __future__ import annotations

from datetime import datetime, timezone

# The ONE spelling of the `Z`-suffixed stamp: UTC, second precision, `Z` as a literal. Producers and
# parsers of that form both take the format from here, so it can never be half-changed.
#
# This is NOT the campaign's only timestamp form, and must never be read as one. `ledger.py`'s
# `now_activity()` and the `ci_stalled_since` value `ci-status.py` writes use
# `datetime.isoformat(timespec="seconds")` instead, which spells the offset `+00:00` rather than `Z`.
# Those two agree with each other deliberately (`ledger.now_activity` owns that reasoning); neither
# reads nor writes anything through this module, and neither belongs here.
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def now_iso() -> str:
    """The current UTC moment as a `TS_FORMAT` stamp."""
    return datetime.now(timezone.utc).strftime(TS_FORMAT)
