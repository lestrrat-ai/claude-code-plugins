#!/usr/bin/env bash
# Validate the marketplace manifest, every plugin it lists, and skill layout.
#
# Runs fully offline: `claude plugin validate` needs no credentials, so this is
# safe on forked pull requests.
#
# Usage: scripts/validate-plugins.sh
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$root"

status=0
fail() {
  printf 'error: %s\n' "$*" >&2
  status=1
}

for tool in claude jq git; do
  command -v "$tool" >/dev/null || {
    printf 'error: required tool not found: %s\n' "$tool" >&2
    exit 127
  }
done

manifest=.claude-plugin/marketplace.json
[[ -f $manifest ]] || {
  printf 'error: missing %s\n' "$manifest" >&2
  exit 1
}

echo "==> marketplace manifest"
claude plugin validate . --strict || status=1

echo
echo "==> plugin sources"
while IFS=$'\t' read -r name source kind; do
  # Remote sources (github/git objects) resolve at install time, not here.
  if [[ $kind != string ]]; then
    printf 'skip: %s has a %s source; nothing to check locally\n' "$name" "$kind"
    continue
  fi

  # A bare path such as "gauntlet" is a hard schema error; catch it with a
  # readable message rather than the validator's "Invalid input".
  [[ $source == ./* ]] ||
    fail "$name: source must be a ./-prefixed relative path, got '$source'"

  # `claude plugin validate` passes silently when a source points nowhere, so a
  # typo'd path would otherwise ship green. Assert existence ourselves.
  [[ -d $source ]] || {
    fail "$name: source directory '$source' does not exist"
    continue
  }
  [[ -f $source/.claude-plugin/plugin.json ]] || {
    fail "$name: '$source' has no .claude-plugin/plugin.json"
    continue
  }

  declared=$(jq -r '.name // ""' "$source/.claude-plugin/plugin.json")
  [[ $declared == "$name" ]] ||
    fail "$name: plugin.json declares name '$declared'; marketplace entry says '$name'"

  echo "--> $name ($source)"
  claude plugin validate "$source" --strict || status=1
done < <(jq -r '.plugins[] | [.name, (.source | tostring), (.source | type)] | @tsv' "$manifest")

echo
echo "==> skill directories"
while IFS= read -r skill; do
  dir=${skill%/SKILL.md}
  dir=${dir##*/}

  # Read only the first frontmatter block.
  declared=$(awk '
    /^---[[:space:]]*$/ { blocks++; next }
    blocks == 1 && /^name:[[:space:]]/ {
      sub(/^name:[[:space:]]*/, ""); print; exit
    }
    blocks >= 2 { exit }
  ' "$skill")

  # The directory name is what makes a skill invocable as /<plugin>:<dir>.
  # Frontmatter `name` is a display label, so a mismatch silently misleads.
  if [[ -n $declared && $declared != "$dir" ]]; then
    fail "$skill: frontmatter name '$declared' does not match directory '$dir'"
  fi

  grep -Eq '^description:[[:space:]]*\S' "$skill" ||
    fail "$skill: frontmatter is missing a non-empty 'description'"
done < <(find plugins -path '*/skills/*/SKILL.md' -type f | sort)

echo
echo "==> bundled script permissions"
# The docs tell agents to run bundled scripts directly (`<skill-dir>/scripts/x.py …`),
# so a script committed without the executable bit dies with "Permission denied" in
# someone else's run. Enforce the pair in BOTH directions:
#
#   shebang  => must be committed 100755   (it is meant to be run directly)
#   100755   => must carry a shebang       (otherwise exec'ing it is a coin toss)
#
# Read the INDEX, not the worktree: a local `chmod` that was never staged fixes
# nothing for anyone else, and the index mode is what an installed copy ships with.
while IFS= read -r entry; do
  mode=${entry%% *}
  file=${entry#*$'\t'}

  case $file in */scripts/*) ;; *) continue ;; esac
  [[ $mode == 100644 || $mode == 100755 ]] || continue # symlinks, submodules

  head2=$(git cat-file blob ":$file" | head -c 2 || true)

  if [[ $head2 == '#!' && $mode != 100755 ]]; then
    fail "$file: has a shebang but is committed $mode; run: git update-index --chmod=+x $file"
  elif [[ $head2 != '#!' && $mode == 100755 ]]; then
    fail "$file: is committed executable but has no shebang; add one, or run: git update-index --chmod=-x $file"
  fi
done < <(git ls-files --stage -- plugins)

echo
if ((status == 0)); then
  echo "all checks passed"
else
  echo "validation failed" >&2
fi
exit "$status"
