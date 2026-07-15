# Release notes

`release.yml` cuts a GitHub release when a version bump lands on `main`. The
release body is GitHub's auto-generated list of merged PRs. To highlight the
top changes, add a short curated blurb here named for the version:

    release-notes/<version>.md   e.g. release-notes/0.1.6.md

Commit it in the same PR as the `plugin.json` version bump. The workflow
prepends it above the auto-generated list, separated by a rule. If the file is
absent the release still ships — with the auto-generated list only (and a
warning in the run log).

Keep it to a few bullets: what a user would care about, not every PR.
