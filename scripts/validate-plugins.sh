#!/usr/bin/env bash
# Validate both marketplace formats, every plugin they list, and skill layout.
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

for tool in claude codex jq; do
  command -v "$tool" >/dev/null || {
    printf 'error: required tool not found: %s\n' "$tool" >&2
    exit 127
  }
done

claude_marketplace=.claude-plugin/marketplace.json
codex_marketplace=.agents/plugins/marketplace.json
[[ -f $claude_marketplace ]] || {
  printf 'error: missing %s\n' "$claude_marketplace" >&2
  exit 1
}
[[ -f $codex_marketplace ]] || {
  printf 'error: missing %s\n' "$codex_marketplace" >&2
  exit 1
}

echo "==> Claude marketplace manifest"
claude plugin validate . --strict || status=1

echo
echo "==> Claude plugin sources"
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
done < <(jq -r '.plugins[] | [.name, (.source | tostring), (.source | type)] | @tsv' "$claude_marketplace")

echo
echo "==> Codex marketplace manifest"
jq -e '
  (.name | type == "string" and length > 0) and
  (.plugins | type == "array" and length > 0) and
  all(.plugins[];
    (.name | type == "string" and length > 0) and
    (.source.source == "local") and
    (.source.path | type == "string" and startswith("./")) and
    (.policy.installation | IN("NOT_AVAILABLE", "AVAILABLE", "INSTALLED_BY_DEFAULT")) and
    (.policy.authentication | IN("ON_INSTALL", "ON_USE")) and
    (.category | type == "string" and length > 0)
  )
' "$codex_marketplace" >/dev/null || fail "$codex_marketplace: invalid Codex marketplace shape"

while IFS=$'\t' read -r name source; do
  [[ -d $source ]] || {
    fail "$name: Codex source directory '$source' does not exist"
    continue
  }

  claude_plugin=$source/.claude-plugin/plugin.json
  codex_plugin=$source/.codex-plugin/plugin.json
  [[ -f $codex_plugin ]] || {
    fail "$name: '$source' has no .codex-plugin/plugin.json"
    continue
  }

  codex_name=$(jq -r '.name // ""' "$codex_plugin")
  [[ $codex_name == "$name" ]] ||
    fail "$name: Codex plugin.json declares name '$codex_name'"

  claude_version=$(jq -r '.version // ""' "$claude_plugin")
  codex_version=$(jq -r '.version // ""' "$codex_plugin")
  [[ $codex_version == "$claude_version" ]] ||
    fail "$name: manifest versions differ (Claude $claude_version, Codex $codex_version)"
done < <(jq -r '.plugins[] | [.name, .source.path] | @tsv' "$codex_marketplace")

mkdir -p "$root/.tmp"
codex_home=$(mktemp -d "$root/.tmp/codex-validate.XXXXXX")
trap 'rm -rf "$codex_home"' EXIT
CODEX_HOME=$codex_home codex plugin marketplace add . --json >"$codex_home/marketplace-add.json" || status=1
marketplace_name=$(jq -r '.name' "$codex_marketplace")
while IFS= read -r name; do
  CODEX_HOME=$codex_home codex plugin add "$name@$marketplace_name" --json >"$codex_home/plugin-$name.json" || {
    fail "$name: Codex could not install the plugin"
    continue
  }

  installed=$(jq -r '.installedPath // ""' "$codex_home/plugin-$name.json")
  [[ -f $installed/skills/review/SKILL.md ]] ||
    fail "$name: installed Codex plugin is missing skills/review/SKILL.md"
done < <(jq -r '.plugins[].name' "$codex_marketplace")

echo
echo "==> shared agent instructions"
[[ -f AGENTS.md ]] || fail "missing AGENTS.md"
[[ -L CLAUDE.md ]] || fail "CLAUDE.md must be a symlink to AGENTS.md"
[[ $(readlink CLAUDE.md) == AGENTS.md ]] ||
  fail "CLAUDE.md must target AGENTS.md"
grep -Fq '## Keep Claude Code and Codex compatible' AGENTS.md ||
  fail "AGENTS.md is missing the cross-runtime compatibility contract"
[[ -f docs/runtime-compatibility.md ]] || fail "missing docs/runtime-compatibility.md"

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

host_specific_review_terms=$(
  grep -nE 'subagent_type:|Agent-tool block|Use the Agent tool' \
    plugins/gauntlet/skills/review/SKILL.md \
    plugins/gauntlet/skills/copilot-address-reviews/SKILL.md || true
)
if [[ -n $host_specific_review_terms ]]; then
  fail "shared review skills contain Claude-only agent calls: $host_specific_review_terms"
fi

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
  # shellcheck disable=SC2016  # `\$\{CLAUDE_PLUGIN_ROOT\}` is REGEX TEXT matched in the docs, not an
  # expansion: single quotes are what keeps it a pattern. Double quotes would expand it (to empty) here and
  # the check would silently stop matching that whole form.
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
