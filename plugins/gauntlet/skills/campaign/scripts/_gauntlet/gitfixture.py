# ci: pyright
"""One owner for the throwaway Git repositories the campaign self-test suites build.

Several suites here prove their claims against a REAL git, not a mock: they build a repository in a
temporary directory, commit into it, push between clones, and read SHAs back with plumbing. Each of them
used to carry its own copy of every step, and the copies drifted — one suite checked a setup command's
exit code and another did not, so a broken setup could leave a fixture proving something about a
repository it never built. The methods below are the whole set; each says what it is for, and no count
of them is restated here, because a count is one more thing to keep true.

The failure TYPE is why this is a class and not a module of plain functions. Every suite raises its own module's
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

    def init_bare(self, remote: Path, *, branch: str = "main") -> None:
        """Create `remote` as a bare repository whose default branch is `branch`.

        A bare repository is the only thing a fixture can push to and clone from, so the suites that
        model a real campaign — a PR branch on a remote, a base that advanced under it — start here
        rather than with `init_repo`. It is given no commit identity: nothing is ever committed IN a
        bare repository, only pushed into it from a clone that has one.
        """
        self._plain(["git", "init", "--bare", "-b", branch, str(remote)],
                    f"could not create the fixture remote at {remote}")

    def clone(self, remote: Path, dest: Path) -> None:
        """Clone `remote` into `dest` and give the clone the fixture commit identity.

        The identity comes with the clone because every caller needs one: a clone exists in these suites
        to commit into and push from. A caller wanting a bare-handed clone can still run `git clone`
        itself; none does.
        """
        self._plain(["git", "clone", str(remote), str(dest)],
                    f"could not clone {remote} into {dest}")
        self.configure_identity(dest)

    def _plain(self, argv: list[str], what: str) -> None:
        """Run a git command that has NO repository to be `-C`'d into, and raise the bound failure type.

        `run` cannot serve `init --bare` or `clone`: both NAME their target directory as an argument and
        neither can run inside it, because it does not exist yet. Everything else about the two matches
        `run` — the same capture, the same `errors="replace"` decode, the same failure type — so the
        divergence stays confined to this one method.
        """
        result = subprocess.run(argv,  # noqa: S603 - fixture paths we built
                                capture_output=True, text=True, errors="replace", check=False)
        if result.returncode != 0:
            raise self._failure(f"{what} ({result.returncode}): {result.stderr.strip()}")
