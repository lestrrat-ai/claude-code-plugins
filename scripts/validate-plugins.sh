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

# Read a jq field from a JSON file without aborting the whole script under `set -euo pipefail`.
# A valid path that holds invalid JSON makes a bare `x=$(jq -r … "$file")` exit non-zero with a raw
# jq error, which `set -e` turns into a full abort — skipping the clean fail() and every later check.
# This checks the parse first and returns non-zero (with no raw jq noise) on a malformed/missing file,
# so a caller attaches `|| fail "…"`: the `||` keeps `set -e` from aborting, and fail() runs in the
# caller (not this command-substitution subshell) so its status=1 actually sticks. A malformed manifest
# then yields a clear fail() and the script keeps running its other checks.
jq_field() {
  local filter=$1 file=$2
  jq empty "$file" 2>/dev/null || return 1
  jq -r "$filter" "$file"
}

# Emit a marketplace's {plugin-name -> source-path} map as canonical, sorted, compact JSON.
# The two hosts store the source differently, so the caller passes the jq path that extracts it:
# Claude keeps a bare string in `.source`, Codex nests it under `.source.path`. Normalization is
# part of the check, not a nicety: source paths are canonicalized (one leading `./` and any
# trailing slashes stripped) so a cosmetic `./x` vs `x` difference is not a false diff AND a real
# `x` vs `y` difference is not hidden; keys are `-S`-sorted and entries `sort_by(.name)`-ordered
# so equal maps in any spelling/order produce byte-identical output. A duplicate plugin name
# within one marketplace is rejected here (jq `error`), else two entries could collapse and let a
# divergent map compare equal. Parse first (jq_field pattern) so a malformed file returns non-zero
# — not a raw `set -e` abort — and the caller attaches `|| fail`.
marketplace_source_map() {
  local file=$1 src=$2 filter
  jq empty "$file" 2>/dev/null || return 1
  # shellcheck disable=SC2016  # This is a jq program, not shell: `$names` and `error(...)` are jq
  # syntax and MUST stay literal. Single quotes keep them so; the only shell substitution is the
  # `SRC` placeholder, swapped for the host's source path via `${filter/SRC/$src}` on the next line.
  filter='
    def canon: sub("^[.]/"; "") | sub("/+$"; "");
    (.plugins | map(.name)) as $names
    | if ($names | length) != ($names | unique | length)
      then error("duplicate plugin name within a single marketplace")
      else [ .plugins[] | {name: .name, source: (SRC | canon)} ] | sort_by(.name)
      end
  '
  jq -S -c "${filter/SRC/$src}" "$file" 2>/dev/null || return 1
}

# ONE cross-host equivalence assertion that closes the whole packaging-drift class: same
# marketplace `.name` + identical canonical {plugin-name -> source-path} map => `<plugin>@<name>`
# resolves to the SAME source directory on both hosts => identical manifests and skills. It
# therefore subsumes both drift classes (a lone marketplace-name change, or a lone source-path
# change) rather than checking them as two ad-hoc rules. Prints its own diagnostics and returns
# non-zero on any mismatch, malformed JSON, or duplicate name; it mutates no global state, so the
# self-test can exercise it against fixture files. Callers in the main flow do `|| status=1`.
check_marketplace_equivalence() {
  local claude_mp=$1 codex_mp=$2 rc=0
  local claude_name codex_name claude_map codex_map

  claude_name=$(jq_field '.name // ""' "$claude_mp") || {
    printf 'error: %s is not valid JSON\n' "$claude_mp" >&2
    return 1
  }
  codex_name=$(jq_field '.name // ""' "$codex_mp") || {
    printf 'error: %s is not valid JSON\n' "$codex_mp" >&2
    return 1
  }
  if [[ -z $claude_name || $claude_name != "$codex_name" ]]; then
    printf "error: marketplace names differ (Claude '%s', Codex '%s'); breaks the '<plugin>@%s' install\n" \
      "$claude_name" "$codex_name" "$claude_name" >&2
    rc=1
  fi

  claude_map=$(marketplace_source_map "$claude_mp" '.source') || {
    printf 'error: %s: cannot build plugin->source map (invalid JSON or duplicate plugin name)\n' "$claude_mp" >&2
    rc=1
  }
  codex_map=$(marketplace_source_map "$codex_mp" '.source.path') || {
    printf 'error: %s: cannot build plugin->source map (invalid JSON or duplicate plugin name)\n' "$codex_mp" >&2
    rc=1
  }
  if [[ $rc -eq 0 && $claude_map != "$codex_map" ]]; then
    printf 'error: marketplaces map plugins to different sources\n  Claude: %s\n  Codex:  %s\n' \
      "$claude_map" "$codex_map" >&2
    rc=1
  fi
  return "$rc"
}

# Regression guard: a scratch two-marketplace fixture that PROVES check_marketplace_equivalence
# catches both drift classes and fails loudly. Case (a) also proves normalization — the two
# fixtures differ in plugin order, in `./x` vs `x`, and in a trailing slash, yet must compare
# equal. Cases (b)/(c) prove the two drift classes fail; (d) proves the duplicate-name guard.
# If the equivalence check is ever gutted, (b)/(c)/(d) start passing when they must fail and this
# returns non-zero. Runs as a preamble on every invocation, so CI's plain run exercises it.
run_self_test() {
  local dir rc=0 claude_mp codex_mp
  local base_claude base_codex
  dir=$(mktemp -d "${TMPDIR:-/tmp}/validate-selftest.XXXXXX") || {
    printf 'error: self-test could not create a temp dir\n' >&2
    return 1
  }
  claude_mp=$dir/claude-marketplace.json
  codex_mp=$dir/codex-marketplace.json

  # Deliberately cosmetically different: order, leading `./`, and a trailing slash all vary.
  base_claude='{"name":"mp","plugins":[{"name":"a","source":"./plugins/a"},{"name":"b","source":"plugins/b/"}]}'
  base_codex='{"name":"mp","plugins":[{"name":"b","source":{"source":"local","path":"./plugins/b"}},{"name":"a","source":{"source":"local","path":"plugins/a"}}]}'

  # (a) equivalent under normalization -> must PASS
  printf '%s\n' "$base_claude" >"$claude_mp"
  printf '%s\n' "$base_codex" >"$codex_mp"
  if ! check_marketplace_equivalence "$claude_mp" "$codex_mp" 2>/dev/null; then
    printf 'error: self-test(a): equivalence check rejected equivalent marketplaces\n' >&2
    rc=1
  fi

  # (b) marketplace .name drift -> must FAIL
  printf '%s\n' "$base_claude" >"$claude_mp"
  printf '%s\n' "${base_codex/\"name\":\"mp\"/\"name\":\"mp2\"}" >"$codex_mp"
  if check_marketplace_equivalence "$claude_mp" "$codex_mp" 2>/dev/null; then
    printf 'error: self-test(b): equivalence check accepted a marketplace .name drift\n' >&2
    rc=1
  fi

  # (c) plugin source-map drift (one host points a plugin at a divergent dir) -> must FAIL
  printf '%s\n' "$base_claude" >"$claude_mp"
  printf '%s\n' "${base_codex/plugins\/a/plugins\/DIVERGENT}" >"$codex_mp"
  if check_marketplace_equivalence "$claude_mp" "$codex_mp" 2>/dev/null; then
    printf 'error: self-test(c): equivalence check accepted a plugin source-map drift\n' >&2
    rc=1
  fi

  # (d) duplicate plugin name within one marketplace -> must FAIL
  printf '%s\n' '{"name":"mp","plugins":[{"name":"a","source":"./plugins/a"},{"name":"a","source":"./plugins/a"}]}' >"$claude_mp"
  printf '%s\n' "$base_codex" >"$codex_mp"
  if check_marketplace_equivalence "$claude_mp" "$codex_mp" 2>/dev/null; then
    printf 'error: self-test(d): equivalence check accepted a duplicate plugin name\n' >&2
    rc=1
  fi

  # --- path-resolution guard: manifest-declared path fields must resolve inside the plugin root ---
  # These prove the NEW checks catch drift. Gutting resolve_plugin_path (making it always succeed)
  # makes (f)/(g)/(h)/(j)/(l) start passing when they must fail, so this returns non-zero.
  local proot=$dir/plugin mf=$dir/codex-plugin.json
  mkdir -p "$proot/skills/demo"
  : >"$proot/skills/demo/SKILL.md"

  # (e) a valid in-root skills path -> must RESOLVE
  if ! resolve_plugin_path "$proot" "./skills/" >/dev/null 2>&1; then
    printf 'error: self-test(e): resolve_plugin_path rejected a valid in-root skills path\n' >&2
    rc=1
  fi

  # (f) a declared path that points nowhere (the reported finding: Codex skills: ./missing-skills/)
  #     -> must FAIL to resolve
  if resolve_plugin_path "$proot" "./missing-skills/" >/dev/null 2>&1; then
    printf 'error: self-test(f): resolve_plugin_path accepted a non-existent skills path\n' >&2
    rc=1
  fi

  # (g) a path that escapes the plugin root via `..` -> must FAIL
  if resolve_plugin_path "$proot" "../outside" >/dev/null 2>&1; then
    printf 'error: self-test(g): resolve_plugin_path accepted a root-escaping (..) path\n' >&2
    rc=1
  fi

  # (h) an absolute path -> must FAIL
  if resolve_plugin_path "$proot" "/etc" >/dev/null 2>&1; then
    printf 'error: self-test(h): resolve_plugin_path accepted an absolute path\n' >&2
    rc=1
  fi

  # (i) resolve_skills_dir reads the Codex `skills` field and resolves it -> must RESOLVE
  printf '%s\n' '{"name":"x","skills":"./skills/"}' >"$mf"
  if ! resolve_skills_dir Codex "$proot" "$mf" >/dev/null 2>&1; then
    printf 'error: self-test(i): resolve_skills_dir rejected a valid declared Codex skills path\n' >&2
    rc=1
  fi

  # (j) a Codex manifest whose `skills` path is missing -> resolve_skills_dir must FAIL
  printf '%s\n' '{"name":"x","skills":"./missing-skills/"}' >"$mf"
  if resolve_skills_dir Codex "$proot" "$mf" >/dev/null 2>&1; then
    printf 'error: self-test(j): resolve_skills_dir accepted a missing declared Codex skills path\n' >&2
    rc=1
  fi

  # (k) validate_manifest_paths: a present field with a good path -> must PASS
  printf '%s\n' '{"name":"x","assets":"./skills/"}' >"$mf"
  if ! validate_manifest_paths Codex "$proot" "$mf" assets 2>/dev/null; then
    printf 'error: self-test(k): validate_manifest_paths rejected a valid path field\n' >&2
    rc=1
  fi

  # (l) validate_manifest_paths: a present field with a root-escaping path -> must FAIL
  printf '%s\n' '{"name":"x","assets":"../escape"}' >"$mf"
  if validate_manifest_paths Codex "$proot" "$mf" assets 2>/dev/null; then
    printf 'error: self-test(l): validate_manifest_paths accepted a root-escaping path field\n' >&2
    rc=1
  fi

  # (m) validate_manifest_paths: an ABSENT field is optional -> must PASS
  printf '%s\n' '{"name":"x"}' >"$mf"
  if ! validate_manifest_paths Codex "$proot" "$mf" assets 2>/dev/null; then
    printf 'error: self-test(m): validate_manifest_paths failed on an absent (optional) path field\n' >&2
    rc=1
  fi

  rm -rf -- "$dir"
  return "$rc"
}

# Resolve a manifest-DECLARED, plugin-root-relative path to a real location that provably stays
# inside the plugin root, and print its canonical absolute path. This is the primitive that closes
# the "un-validated path field" class directly: a field whose value is a path is only accepted if it
# resolves to something that EXISTS and cannot escape the plugin root. On any violation it prints
# nothing and returns non-zero (so a caller runs fail()) — never a raw `set -e` abort.
# Rejections, in order: empty; absolute (`/…`); any `..` component — checked LEXICALLY before the
# filesystem is touched, because a symlink could otherwise map an in-tree spelling to an out-of-tree
# target; then the path must actually exist, and its realpath must be the root itself or a strict
# descendant of it (the final containment backstop even if a symlink slipped past the lexical check).
resolve_plugin_path() {
  local root=$1 declared=$2 root_real rel target target_real
  root_real=$(cd -- "$root" 2>/dev/null && pwd -P) || return 1
  [[ -n $declared ]] || return 1
  [[ $declared == /* ]] && return 1              # absolute path escapes the root
  rel=${declared#./}                             # strip one leading ./
  rel=${rel%/}                                   # strip trailing slashes
  rel=${rel%/}
  [[ -n $rel ]] || return 1
  case "/$rel/" in                               # any `..` component escapes the root
    */../*) return 1 ;;
  esac
  target=$root/$rel
  [[ -e $target ]] || return 1
  if [[ -d $target ]]; then
    # Canonicalise the directory itself (follows symlinks fully).
    target_real=$(cd -- "$target" 2>/dev/null && pwd -P) || return 1
  else
    # A file: canonicalise its parent dir, then re-append the leaf name.
    target_real=$(cd -- "$(dirname -- "$target")" 2>/dev/null && pwd -P)/$(basename -- "$target") || return 1
  fi
  [[ $target_real == "$root_real" || $target_real == "$root_real"/* ]] || return 1
  printf '%s\n' "$target_real"
}

# Validate every path-bearing manifest field in `$4…` for one host: a field that is ABSENT is fine
# (the schema makes these optional), a field that is PRESENT must hold a path that resolve_plugin_path
# accepts. Prints diagnostics and returns non-zero on any violation; mutates no global state, so the
# self-test can drive it on fixtures and main-flow callers attach `|| status=1`. `skills` is NOT
# listed here — resolve_skills_dir owns it because the entrypoint check needs the resolved dir anyway;
# this helper covers any OTHER path field a manifest grows, so a new one is validated automatically.
validate_manifest_paths() {
  local host=$1 plugin_root=$2 manifest=$3
  shift 3
  local field present declared rc=0
  jq empty "$manifest" 2>/dev/null || {
    printf 'error: %s: %s is not valid JSON\n' "$host" "$manifest" >&2
    return 1
  }
  for field in "$@"; do
    present=$(jq -r "has(\"$field\")" "$manifest" 2>/dev/null) || { rc=1; continue; }
    [[ $present == true ]] || continue
    declared=$(jq -r ".${field} // \"\"" "$manifest" 2>/dev/null)
    if ! resolve_plugin_path "$plugin_root" "$declared" >/dev/null; then
      printf "error: %s: manifest field '%s' path '%s' does not resolve to an existing location inside the plugin root\n" \
        "$host" "$field" "$declared" >&2
      rc=1
    fi
  done
  return "$rc"
}

# Resolve the directory a host discovers skills in, printing its canonical absolute path. Codex
# DECLARES it in the manifest `skills` field — resolve THAT, never a hard-coded `skills/`, so a
# drifted Codex `skills` path fails instead of being silently ignored (the exact finding this closes).
# Claude auto-discovers `skills/` with no manifest field, so its default is `./skills`. Silent: returns
# non-zero with nothing printed if the declared/default path does not resolve inside the plugin root,
# and the caller fails loudly rather than falling back to a wrong dir.
resolve_skills_dir() {
  local host=$1 plugin_root=$2 manifest=$3 declared
  case $host in
    Codex)
      declared=$(jq_field '.skills // ""' "$manifest") || return 1   # malformed manifest
      [[ -n $declared ]] || declared=./skills                        # field absent -> Codex default
      ;;
    Claude)
      declared=./skills                                              # auto-discovered, no field
      ;;
    *) return 1 ;;
  esac
  resolve_plugin_path "$plugin_root" "$declared"
}

# Verify every source skills/*/SKILL.md entrypoint is present in the installed copy. `skills_dir` and
# `plugin_root` are the RESOLVED absolute paths (from resolve_skills_dir); entrypoints are named
# relative to the plugin root so they match the installed layout regardless of what subdir the
# manifest pointed `skills` at.
verify_gauntlet_skill_entrypoints() {
  local host plugin_root skills_dir installed skill entrypoint
  local count=0
  host=$1
  plugin_root=$2
  skills_dir=$3
  installed=$4

  while IFS= read -r skill; do
    count=$((count + 1))
    entrypoint=${skill#"$plugin_root/"}
    [[ -f $installed/$entrypoint ]] ||
      fail "gauntlet: installed $host plugin is missing $entrypoint"
  done < <(find "$skills_dir" -mindepth 2 -maxdepth 2 -name SKILL.md -type f | sort)

  ((count > 0)) || fail "gauntlet: source plugin contains no skills/*/SKILL.md entrypoints"
}

# Structural class-closer: after BOTH hosts install from this checkout, prove the two installed copies
# carry the IDENTICAL set of skill entrypoints AND byte-identical content for each. Any manifest field
# whose drift changes what actually gets installed (a wrong/missing `skills` path that installs empty
# or divergent content, or any un-enumerated field with the same effect) fails HERE without the check
# having to name that field — that is what stops the "round N finds yet another un-validated field"
# loop. Prints diagnostics and calls fail() on any mismatch.
assert_installed_skill_parity() {
  local name=$1 claude_root=$2 codex_root=$3
  local claude_list codex_list rel

  claude_list=$(cd -- "$claude_root" 2>/dev/null &&
    find . -path '*/skills/*/SKILL.md' -type f | sed 's,^\./,,' | LC_ALL=C sort) || {
    fail "$name: cannot enumerate installed Claude skills under '$claude_root'"
    return
  }
  codex_list=$(cd -- "$codex_root" 2>/dev/null &&
    find . -path '*/skills/*/SKILL.md' -type f | sed 's,^\./,,' | LC_ALL=C sort) || {
    fail "$name: cannot enumerate installed Codex skills under '$codex_root'"
    return
  }

  if [[ -z $claude_list ]]; then
    fail "$name: installed Claude copy has no skills/*/SKILL.md entrypoints"
    return
  fi
  if [[ $claude_list != "$codex_list" ]]; then
    fail "$name: installed skill entrypoint sets differ between hosts
  Claude: $(printf '%s' "$claude_list" | tr '\n' ' ')
  Codex:  $(printf '%s' "$codex_list" | tr '\n' ' ')"
    return
  fi

  # Same entrypoint set -> compare bytes of each entrypoint.
  while IFS= read -r rel; do
    [[ -n $rel ]] || continue
    cmp -s "$claude_root/$rel" "$codex_root/$rel" ||
      fail "$name: installed skill content differs at '$rel' between hosts"
  done <<<"$claude_list"

  # Full-tree parity of the shared skills/ dir (references/ and scripts/ too, not just SKILL.md).
  if [[ -d $claude_root/skills && -d $codex_root/skills ]]; then
    diff -rq "$claude_root/skills" "$codex_root/skills" >/dev/null 2>&1 ||
      fail "$name: installed skills/ trees differ between hosts (run: diff -rq '$claude_root/skills' '$codex_root/skills')"
  fi
}

for tool in claude codex jq; do
  command -v "$tool" >/dev/null || {
    printf 'error: required tool not found: %s\n' "$tool" >&2
    exit 127
  }
done

# Prove the cross-marketplace equivalence check works before trusting it below. `--self-test`
# runs only this and exits; otherwise it runs as a preamble so the plain CI invocation exercises
# it. A gutted equivalence check makes this fail loudly rather than silently passing everything.
if [[ ${1:-} == --self-test ]]; then
  run_self_test
  exit $?
fi
run_self_test || {
  printf 'error: cross-marketplace equivalence self-test failed; the check is broken\n' >&2
  exit 1
}

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

  declared=$(jq_field '.name // ""' "$source/.claude-plugin/plugin.json") ||
    fail "$name: $source/.claude-plugin/plugin.json is not valid JSON"
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
claude_marketplace_name=$(jq_field '.name' "$claude_marketplace") ||
  fail "$claude_marketplace is not valid JSON"
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
    source_name=$(jq_field '.name // ""' "$source_manifest") || fail "$name: $source_manifest is not valid JSON"
    source_version=$(jq_field '.version // ""' "$source_manifest") || fail "$name: $source_manifest is not valid JSON"

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
    installed_name=$(jq_field '.name // ""' "$installed_manifest") || fail "$name: $installed_manifest is not valid JSON"
    installed_version=$(jq_field '.version // ""' "$installed_manifest") || fail "$name: $installed_manifest is not valid JSON"
    [[ $installed_name == "$source_name" ]] ||
      fail "$name: installed Claude manifest name '$installed_name', source has '$source_name'"
    [[ $installed_version == "$source_version" ]] ||
      fail "$name: installed Claude manifest version '$installed_version', source has '$source_version'"

    if [[ $name == gauntlet ]]; then
      source_real=$(cd -- "$source" && pwd -P)
      # Claude's manifest declares no path-bearing field (it auto-discovers skills/); any it grows
      # later is validated automatically. The skills dir itself is resolved below.
      validate_manifest_paths Claude "$source_real" "$source_manifest" || status=1
      if skills_real=$(resolve_skills_dir Claude "$source_real" "$source_manifest"); then
        verify_gauntlet_skill_entrypoints Claude "$source_real" "$skills_real" "$installed_real"
        claude_gauntlet_installed=$installed_real
      else
        fail "$name: Claude skills directory does not resolve to an existing dir inside the plugin root"
      fi
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

  codex_name=$(jq_field '.name // ""' "$codex_plugin") || fail "$name: $codex_plugin is not valid JSON"
  [[ $codex_name == "$name" ]] ||
    fail "$name: Codex plugin.json declares name '$codex_name'"

  claude_name=$(jq_field '.name // ""' "$claude_plugin") || fail "$name: $claude_plugin is not valid JSON"
  [[ $codex_name == "$claude_name" ]] ||
    fail "$name: manifest names differ (Claude $claude_name, Codex $codex_name)"

  claude_version=$(jq_field '.version // ""' "$claude_plugin") || fail "$name: $claude_plugin is not valid JSON"
  codex_version=$(jq_field '.version // ""' "$codex_plugin") || fail "$name: $codex_plugin is not valid JSON"
  [[ $codex_version == "$claude_version" ]] ||
    fail "$name: manifest versions differ (Claude $claude_version, Codex $codex_version)"
done < <(jq -r '.plugins[] | [.name, .source.path] | @tsv' "$codex_marketplace")

CODEX_HOME=$codex_home codex plugin marketplace add . --json >"$codex_home/marketplace-add.json" || status=1
marketplace_name=$(jq_field '.name' "$codex_marketplace") || fail "$codex_marketplace is not valid JSON"
while IFS=$'\t' read -r name source; do
  CODEX_HOME=$codex_home codex plugin add "$name@$marketplace_name" --json >"$codex_home/plugin-$name.json" || {
    fail "$name: Codex could not install the plugin"
    continue
  }

  installed=$(jq_field '.installedPath // ""' "$codex_home/plugin-$name.json") ||
    fail "$name: $codex_home/plugin-$name.json is not valid JSON"
  source_manifest=$source/.codex-plugin/plugin.json
  source_name=$(jq_field '.name // ""' "$source_manifest") || fail "$name: $source_manifest is not valid JSON"
  source_version=$(jq_field '.version // ""' "$source_manifest") || fail "$name: $source_manifest is not valid JSON"

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
  installed_name=$(jq_field '.name // ""' "$installed_manifest") || fail "$name: $installed_manifest is not valid JSON"
  installed_version=$(jq_field '.version // ""' "$installed_manifest") || fail "$name: $installed_manifest is not valid JSON"
  [[ $installed_name == "$source_name" ]] ||
    fail "$name: installed Codex manifest name '$installed_name', source has '$source_name'"
  [[ $installed_version == "$source_version" ]] ||
    fail "$name: installed Codex manifest version '$installed_version', source has '$source_version'"

  if [[ $name == gauntlet ]]; then
    source_real=$(cd -- "$source" && pwd -P)
    # Validate every OTHER path-bearing field the Codex manifest carries (the schema currently
    # carries none beyond `skills`; a future one is validated automatically). `skills` itself is
    # resolved below from its DECLARED value, never a hard-coded `skills/`.
    validate_manifest_paths Codex "$source_real" "$codex_plugin" || status=1
    if skills_real=$(resolve_skills_dir Codex "$source_real" "$codex_plugin"); then
      verify_gauntlet_skill_entrypoints Codex "$source_real" "$skills_real" "$installed_real"
      codex_gauntlet_installed=$installed_real
    else
      fail "$name: Codex declared skills path does not resolve to an existing dir inside the plugin root"
    fi
  fi
done < <(jq -r '.plugins[] | [.name, .source.path] | @tsv' "$codex_marketplace")

echo
echo "==> installed skill-content equivalence (both hosts)"
# The structural class-closer: both isolated installs must yield byte-identical skill content.
if [[ -n ${claude_gauntlet_installed:-} && -n ${codex_gauntlet_installed:-} ]]; then
  assert_installed_skill_parity gauntlet "$claude_gauntlet_installed" "$codex_gauntlet_installed"
else
  fail "install-content equivalence: one or both hosts did not report a gauntlet install path; cannot prove identical installed skills"
fi

echo
echo "==> cross-marketplace equivalence"
# Same marketplace name + same {plugin-name -> source-path} map => both hosts install from one
# directory => identical manifests and skills. This subsumes the per-marketplace name and
# source-map drift classes in a single assertion.
check_marketplace_equivalence "$claude_marketplace" "$codex_marketplace" || status=1

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
grep -Fq 'the default per host, overridable' "$campaign/references/cross-agent-reviewers.md" ||
  fail "cross-agent review must document the cross-engine default per host"
grep -Fq '"codex", "exec", "--sandbox", "workspace-write"' "$campaign/references/cross-agent-reviewers.md" ||
  fail "cross-agent reviewer map is missing the typed Codex argv"
grep -Fq '"claude", "-p", "--safe-mode", "--no-session-persistence"' "$campaign/references/cross-agent-reviewers.md" ||
  fail "cross-agent reviewer map is missing the candidate-instruction-safe typed Claude Code argv"
grep -Fq -- '"-C", transport.review_root' "$campaign/references/cross-agent-reviewers.md" ||
  fail "cross-agent reviewer map is missing the capability-gated Codex working-root argv"
grep -Fq '## Typed repository context and data/process boundary' "$campaign/references/runtime-adapter.md" ||
  fail "runtime adapter is missing the typed repository/data boundary"
grep -Fq 'ReviewIsolationCapability' "$campaign/references/runtime-adapter.md" ||
  fail "runtime adapter is missing the review-isolation capability owner"
python3 "$campaign/scripts/review-dispatch.py" self-test || status=1
python3 "$campaign/scripts/transport-contract-test.py" || status=1
python3 "$campaign/scripts/worker-prompt.py" self-test || status=1

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
