#!/usr/bin/env bash
# Validate both marketplace formats, every plugin they list, and skill layout.
#
# Runs fully offline: both CLIs install only from this checkout and need no
# credentials, so this is safe on forked pull requests.
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

verify_gauntlet_skill_entrypoints() {
  local host source installed skill entrypoint
  local count=0
  host=$1
  source=$2
  installed=$3

  while IFS= read -r skill; do
    count=$((count + 1))
    entrypoint=${skill#"$source/"}
    [[ -f $installed/$entrypoint ]] ||
      fail "gauntlet: installed $host plugin is missing $entrypoint"
  done < <(find "$source/skills" -mindepth 2 -maxdepth 2 -name SKILL.md -type f | sort)

  ((count > 0)) || fail "gauntlet: source plugin contains no skills/*/SKILL.md entrypoints"
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

mkdir -p "$root/.tmp"
claude_config_dir=
codex_home=
# shellcheck disable=SC2317  # Invoked indirectly by the EXIT trap below.
cleanup() {
  [[ -z $claude_config_dir ]] || rm -rf -- "$claude_config_dir"
  [[ -z $codex_home ]] || rm -rf -- "$codex_home"
}
trap cleanup EXIT
claude_config_dir=$(mktemp -d "$root/.tmp/claude-validate.XXXXXX")
codex_home=$(mktemp -d "$root/.tmp/codex-validate.XXXXXX")

echo
echo "==> Claude isolated plugin installation"
claude_marketplace_name=$(jq -r '.name' "$claude_marketplace")
if ! CLAUDE_CONFIG_DIR=$claude_config_dir \
  claude plugin marketplace add "$root" --scope user; then
  fail "Claude could not add the local marketplace"
else
  while IFS=$'\t' read -r name source; do
    CLAUDE_CONFIG_DIR=$claude_config_dir \
      claude plugin install "$name@$claude_marketplace_name" --scope user ||
      fail "$name: Claude could not install the plugin"
  done < <(jq -r '.plugins[] | select(.source | type == "string") | [.name, .source] | @tsv' "$claude_marketplace")
fi

claude_plugin_list=$claude_config_dir/plugin-list.json
if ! CLAUDE_CONFIG_DIR=$claude_config_dir claude plugin list --json >"$claude_plugin_list"; then
  fail "Claude could not list the isolated plugins"
elif ! jq -e 'type == "array"' "$claude_plugin_list" >/dev/null; then
  fail "Claude isolated plugin list is not a JSON array"
else
  while IFS=$'\t' read -r name source; do
    expected_id=$name@$claude_marketplace_name
    match_count=$(jq --arg id "$expected_id" '[.[] | select(.id == $id)] | length' "$claude_plugin_list")
    if [[ $match_count != 1 ]]; then
      fail "$name: expected exactly one installed Claude plugin '$expected_id', found $match_count"
      continue
    fi

    listed_version=$(jq -r --arg id "$expected_id" '.[] | select(.id == $id) | .version // ""' "$claude_plugin_list")
    listed_scope=$(jq -r --arg id "$expected_id" '.[] | select(.id == $id) | .scope // ""' "$claude_plugin_list")
    installed=$(jq -r --arg id "$expected_id" '.[] | select(.id == $id) | .installPath // ""' "$claude_plugin_list")
    source_manifest=$source/.claude-plugin/plugin.json
    source_name=$(jq -r '.name // ""' "$source_manifest")
    source_version=$(jq -r '.version // ""' "$source_manifest")

    [[ $listed_scope == user ]] ||
      fail "$name: installed Claude plugin has scope '$listed_scope', expected 'user'"
    [[ $listed_version == "$source_version" ]] ||
      fail "$name: installed Claude metadata has version '$listed_version', source has '$source_version'"

    if [[ ! -d $installed ]]; then
      fail "$name: installed Claude path '$installed' is not a directory"
      continue
    fi
    installed_real=$(cd -- "$installed" && pwd -P)
    cache_real=$(cd -- "$claude_config_dir/plugins/cache" && pwd -P)
    [[ $installed_real == "$cache_real"/* ]] || {
      fail "$name: installed Claude path '$installed_real' is outside '$cache_real'"
      continue
    }

    installed_manifest=$installed/.claude-plugin/plugin.json
    [[ -f $installed_manifest ]] || {
      fail "$name: installed Claude plugin is missing .claude-plugin/plugin.json"
      continue
    }
    installed_name=$(jq -r '.name // ""' "$installed_manifest")
    installed_version=$(jq -r '.version // ""' "$installed_manifest")
    [[ $installed_name == "$source_name" ]] ||
      fail "$name: installed Claude manifest name '$installed_name', source has '$source_name'"
    [[ $installed_version == "$source_version" ]] ||
      fail "$name: installed Claude manifest version '$installed_version', source has '$source_version'"

    if [[ $name == gauntlet ]]; then
      verify_gauntlet_skill_entrypoints Claude "$source" "$installed_real"
    fi
  done < <(jq -r '.plugins[] | select(.source | type == "string") | [.name, .source] | @tsv' "$claude_marketplace")
fi

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

CODEX_HOME=$codex_home codex plugin marketplace add . --json >"$codex_home/marketplace-add.json" || status=1
marketplace_name=$(jq -r '.name' "$codex_marketplace")
while IFS=$'\t' read -r name source; do
  CODEX_HOME=$codex_home codex plugin add "$name@$marketplace_name" --json >"$codex_home/plugin-$name.json" || {
    fail "$name: Codex could not install the plugin"
    continue
  }

  installed=$(jq -r '.installedPath // ""' "$codex_home/plugin-$name.json")
  source_manifest=$source/.codex-plugin/plugin.json
  source_name=$(jq -r '.name // ""' "$source_manifest")
  source_version=$(jq -r '.version // ""' "$source_manifest")

  if [[ ! -d $installed ]]; then
    fail "$name: installed Codex path '$installed' is not a directory"
    continue
  fi
  if [[ ! -d $codex_home/plugins/cache ]]; then
    fail "$name: isolated Codex plugin cache '$codex_home/plugins/cache' is not a directory"
    continue
  fi
  installed_real=$(cd -- "$installed" && pwd -P)
  cache_real=$(cd -- "$codex_home/plugins/cache" && pwd -P)
  [[ $installed_real == "$cache_real"/* ]] || {
    fail "$name: installed Codex path '$installed_real' is outside '$cache_real'"
    continue
  }

  installed_manifest=$installed_real/.codex-plugin/plugin.json
  [[ -f $installed_manifest ]] || {
    fail "$name: installed Codex plugin is missing .codex-plugin/plugin.json"
    continue
  }
  installed_name=$(jq -r '.name // ""' "$installed_manifest")
  installed_version=$(jq -r '.version // ""' "$installed_manifest")
  [[ $installed_name == "$source_name" ]] ||
    fail "$name: installed Codex manifest name '$installed_name', source has '$source_name'"
  [[ $installed_version == "$source_version" ]] ||
    fail "$name: installed Codex manifest version '$installed_version', source has '$source_version'"

  if [[ $name == gauntlet ]]; then
    verify_gauntlet_skill_entrypoints Codex "$source" "$installed_real"
  fi
done < <(jq -r '.plugins[] | [.name, .source.path] | @tsv' "$codex_marketplace")

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

campaign=plugins/gauntlet/skills/campaign
[[ -f $campaign/references/runtime-adapter.md ]] ||
  fail "campaign is missing references/runtime-adapter.md"
[[ -f $campaign/references/cross-agent-reviewers.md ]] ||
  fail "campaign is missing references/cross-agent-reviewers.md"
grep -Fq 'references/runtime-adapter.md' "$campaign/SKILL.md" ||
  fail "campaign SKILL.md does not load the runtime adapter"
grep -Fq 'user option, never a campaign rule' "$campaign/references/cross-agent-reviewers.md" ||
  fail "cross-agent review must remain an explicit user option"
grep -Fq 'codex exec --sandbox workspace-write' "$campaign/references/cross-agent-reviewers.md" ||
  fail "cross-agent reviewer map is missing the Codex command"
grep -Fq 'claude -p --safe-mode --no-session-persistence' "$campaign/references/cross-agent-reviewers.md" ||
  fail "cross-agent reviewer map is missing candidate-instruction-safe Claude Code command"
grep -Fq -- '--skip-git-repo-check -C "<review-root>"' "$campaign/references/cross-agent-reviewers.md" ||
  fail "cross-agent reviewer map is missing the instruction-neutral Codex working root"

campaign_host_leaks=$(
  grep -rnE 'ScheduleWakeup|\$\{CLAUDE_PLUGIN_ROOT\}|Subagent Dispatch|fresh-subagent' \
    "$campaign" --include='*.md' |
    grep -v '/references/runtime-adapter.md:' || true
)
if [[ -n $campaign_host_leaks ]]; then
  fail "shared campaign docs bypass the runtime adapter: $campaign_host_leaks"
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
