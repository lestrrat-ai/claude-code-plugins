# ci: pyright
"""One owner for the throwaway Git repositories the campaign self-test suites build.

Several suites here prove their claims against a REAL git, not a mock: they build a repository in a
temporary directory, commit into it, and read SHAs back with plumbing. Each of them used to carry its own
copy of the same three steps — run git in a repository and raise when it fails, give the repository a
commit identity so `git commit` works on a machine with no global config, and read a revision back as a
string.

The failure TYPE is why this is a class and not four plain functions. Every suite raises its own module's
`SelfTestFailure`, and `_gauntlet.testing.run_cases` reports its `failure` argument as the fixture's own
message while reporting anything else with a type-name prefix. A single helper hard-coding one exception
would therefore change how a fixture's failure READS in every suite but one. Binding the type once, at
construction, keeps each suite's reporting exactly as it was — the same reason `testing.checker` takes the
type as an argument.

Output is captured as TEXT with `errors="replace"`. Some fixtures deliberately create paths holding bytes
that are not valid UTF-8, and git echoes those paths back in its stderr; a strict decode would turn a
fixture's diagnostic into a `UnicodeDecodeError` that hides the real failure.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# The commit identity every fixture repository is given. It is a documentation-reserved domain, so it can
# never resolve; no fixture asserts on either value, they only have to exist for `git commit` to run.
FIXTURE_NAME = "Gauntlet Test"
FIXTURE_EMAIL = "gauntlet@example.invalid"


class GitFixture:
    """Git operations on throwaway repositories, reporting failures as one suite's own failure type."""

    def __init__(self, failure: "type[BaseException]") -> None:
        self._failure = failure

    def run(self, repo: Path, *args: str) -> "subprocess.CompletedProcess[str]":
        """Run `git <args>` inside `repo`; raise the bound failure type when git exits non-zero.

        There is no "tolerate a failure" switch on purpose. A fixture that ignores a setup command's exit
        code builds a repository that is not the one it describes, and then proves something about the
        wrong subject; the two suites that used to carry such a flag never once passed it.
        """
        result = subprocess.run(["git", "-C", str(repo), *args],  # noqa: S603 - fixture repos we built
                                capture_output=True, text=True, errors="replace", check=False)
        if result.returncode != 0:
            raise self._failure(
                f"git {' '.join(args)} failed in {repo} ({result.returncode}): {result.stderr.strip()}")
        return result

    def configure_identity(self, repo: Path) -> None:
        """Give `repo` a commit identity, so committing does not depend on the machine's global config."""
        self.run(repo, "config", "user.name", FIXTURE_NAME)
        self.run(repo, "config", "user.email", FIXTURE_EMAIL)

    def init_repo(self, repo: Path, *, branch: str = "main") -> None:
        """Create `repo` if absent, initialise it on `branch`, and give it the fixture commit identity."""
        repo.mkdir(parents=True, exist_ok=True)
        self.run(repo, "init", "-q", "-b", branch)
        self.configure_identity(repo)

    def head(self, repo: Path, ref: str = "HEAD") -> str:
        """`ref` resolved to a full SHA, stripped — what a fixture pins a ledger row's `head_sha` to."""
        return self.run(repo, "rev-parse", ref).stdout.strip()
