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

for tool in claude jq; do
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
echo "==> bundled script invocations"
# Bundled scripts are invoked through their interpreter with an absolute path
# (`python3 <skill-dir>/scripts/x.py …`) — campaign SKILL.md, "Bundled Scripts" owns the
# rule. A doc that prescribes the *path* form without an interpreter (`<skill-dir>/scripts/x.py
# --file …`) is telling an agent to rely on the executable bit and the shebang surviving every
# checkout, archive and install path. Most bundled scripts are not committed executable, so that
# instruction dies with "Permission denied" in someone else's run — which is exactly how this
# check was born.
#
# Scope: only a script PATH followed by arguments — i.e. an actual prescribed command line.
# Prose that merely NAMES the tool (`scripts/ledger.py` is the accessor; `ledger.py … set`) is
# shorthand, not an invocation, and is left alone.
bare_invocations=$(
  grep -rnE '(^|[^a-zA-Z0-9_/.-])((\./|<skill-dir>/|\$\{CLAUDE_PLUGIN_ROOT\}[^ `]*/)scripts/)[A-Za-z0-9_-]+\.(py|sh)[[:space:]]+[^`]' \
    plugins --include='*.md' |
    grep -vE '(python3|bash)[[:space:]]+[^`]*scripts/' || true
)
if [[ -n $bare_invocations ]]; then
  while IFS= read -r hit; do
    fail "prescribes a bundled script without an interpreter (use 'python3 <path>' / 'bash <path>'): $hit"
  done <<<"$bare_invocations"
fi

echo
if ((status == 0)); then
  echo "all checks passed"
else
  echo "validation failed" >&2
fi
exit "$status"
