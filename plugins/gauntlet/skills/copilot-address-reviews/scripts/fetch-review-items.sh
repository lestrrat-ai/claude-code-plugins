#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: fetch-review-items.sh [options] <pr-url>

Fetch GitHub Copilot PR review items, normalize them to JSON, then deduplicate
them via dedup_review_items.py.

Options:
  --tmp-dir PATH              Directory for raw gh output. Default: .gauntlet/tmp
  --pr-view-json PATH         Use existing gh pr view JSON instead of fetching
  --review-threads-json PATH  Use existing GraphQL review thread JSON instead of fetching
  --raw-output PATH           Normalized pre-dedup JSON. Default: <tmp-dir>/copilot-review-items.raw.json
  --dedup-output PATH         Deduped JSON. Default: <tmp-dir>/copilot-review-items.json
  --copilot-pattern REGEX     Regex for Copilot author logins
  --help                      Show this message
EOF
}

die() {
  printf '%s\n' "$*" >&2
  exit 1
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing required command: $1" >&2
    exit 1
  fi
}

# Arity guard: call BEFORE reading $2 for a value-taking option.
# remaining_argc is $# at the point of the case branch (includes the flag itself),
# so a present value means remaining_argc >= 2.
require_value() {
  local flag="$1"
  local remaining_argc="$2"
  if [[ "$remaining_argc" -lt 2 ]]; then
    die "option $flag requires a value"
  fi
}

# Existence + well-formedness guard for a JSON input file.
read_json_file() {
  local path="$1"
  local label="$2"
  if [[ ! -f "$path" ]]; then
    die "$label: file not found: $path"
  fi
  if ! jq empty "$path" >/dev/null 2>&1; then
    die "$label: not valid JSON: $path"
  fi
}

# Chokepoint for EVERY page of EVERY paginated GraphQL fetch. Fails closed on a
# short or malformed page so a truncated result can never be reported as complete
# and a null/empty cursor can never spin the pagination loop forever.
#
#   validate_page <json_file> <connection_path> <label>
#
# <connection_path> is the jq path to the GraphQL *connection object*, e.g.
#   Axis A: .data.repository.pullRequest.reviewThreads
#   Axis B: .data.node.comments
#
# Asserts, in order, calling die() on the first failure:
#   1. top-level .errors is absent/empty
#   2. <connection_path> is non-null            (covers null pullRequest AND null node)
#   3. <connection_path>.nodes is an array
#   4. <connection_path>.pageInfo.hasNextPage is present AND a boolean (never defaulted)
#   5. when hasNextPage == true, <connection_path>.pageInfo.endCursor is a non-empty string
# Because presence and shape are guaranteed here, callers MUST NOT re-add // false /
# // "" fallbacks on hasNextPage/endCursor — a fallback would silently re-open the hole.
validate_page() {
  local path="$1"
  local connection_path="$2"
  local label="$3"

  # One type-safe pass: reject a non-object top-level value up front (a raw
  # `has(...)`/`getpath(...)` on an array/scalar would abort jq), then surface
  # GraphQL errors, then assert the connection shape. The `if ! result=$(...)`
  # form captures jq's exit status explicitly so a parse error on a non-JSON
  # page routes to the SAME labeled die below instead of tripping `set -e` with
  # a raw jq trace.
  local result
  if ! result=$(jq -r --arg cp "$connection_path" '
    def conn: getpath($cp | ltrimstr(".") | split("."));
    if (type != "object") then
      "GraphQL page is not a JSON object (got \(type))"
    elif (has("errors") and (.errors | length > 0)) then
      "GraphQL API returned errors: \([.errors[].message] | join("; "))"
    elif (conn == null) then
      "connection object is null (\($cp))"
    elif ((conn.nodes | type) != "array") then
      "\($cp).nodes is missing or not an array"
    elif ((conn.pageInfo | type) != "object") then
      "\($cp).pageInfo is missing or not an object"
    elif ((conn.pageInfo | has("hasNextPage")) | not) then
      "\($cp).pageInfo.hasNextPage is missing"
    elif ((conn.pageInfo.hasNextPage | type) != "boolean") then
      "\($cp).pageInfo.hasNextPage is not a boolean"
    elif (conn.pageInfo.hasNextPage == true
          and ((conn.pageInfo.endCursor | type) != "string" or conn.pageInfo.endCursor == "")) then
      "\($cp).pageInfo.hasNextPage is true but endCursor is null/empty"
    else
      "ok"
    end
  ' "$path" 2>/dev/null); then
    die "$label: unreadable GraphQL page (invalid JSON or not a JSON object): $path"
  fi

  if [[ "$result" != "ok" ]]; then
    die "$label: $result"
  fi
}

# Validate the nested comments connection carried by every review-thread node of an
# Axis A page. The Axis B seed reads .comments.pageInfo off these nodes, so a missing
# or false-defaulted nested pageInfo here would silently drop overflow comments or
# spin the Axis B loop. Enforce the same shape as validate_page, per node.
validate_nested_comment_connections() {
  local path="$1"
  local label="$2"
  # Same treatment as validate_page: a jq-level `$c | type != "object"` guard runs
  # BEFORE any `$c.nodes` access, so a node whose .comments is an array/scalar yields
  # a labeled, thread-naming message instead of aborting jq on a string-index error.
  # The `if ! result=$(...)` form then captures jq's exit status explicitly, so any
  # residual jq failure routes to the SAME labeled die below rather than tripping
  # `set -e` with a raw (stderr-suppressed) trace.
  local result
  if ! result=$(jq -r '
    [ .data.repository.pullRequest.reviewThreads.nodes[]
      | .id as $tid
      | .comments as $c
      | if ($c == null) then
          "thread \($tid): comments connection is null"
        elif (($c | type) != "object") then
          "thread \($tid): comments connection is not an object (got \($c | type))"
        elif (($c.nodes | type) != "array") then
          "thread \($tid): comments.nodes is missing or not an array"
        elif (($c.pageInfo | type) != "object") then
          "thread \($tid): comments.pageInfo is missing or not an object"
        elif (($c.pageInfo | has("hasNextPage")) | not) then
          "thread \($tid): comments.pageInfo.hasNextPage is missing"
        elif (($c.pageInfo.hasNextPage | type) != "boolean") then
          "thread \($tid): comments.pageInfo.hasNextPage is not a boolean"
        elif ($c.pageInfo.hasNextPage == true
              and (($c.pageInfo.endCursor | type) != "string" or $c.pageInfo.endCursor == "")) then
          "thread \($tid): comments.pageInfo.hasNextPage is true but endCursor is null/empty"
        else
          empty
        end
    ] | .[0] // "ok"
  ' "$path" 2>/dev/null); then
    die "$label: unreadable GraphQL page (invalid JSON or not a JSON object): $path"
  fi

  if [[ "$result" != "ok" ]]; then
    die "$label: $result"
  fi
}

# Fail closed on an Axis A (review threads) page: validate the reviewThreads
# connection, then every thread's nested comments connection. Keeps its name and
# behavior for existing callers; internally routes through the validate_page chokepoint.
validate_graphql_response() {
  local path="$1"
  local label="$2"
  validate_page "$path" ".data.repository.pullRequest.reviewThreads" "$label"
  validate_nested_comment_connections "$path" "$label"
}

# Pre-flight the user-supplied Copilot author regex before jq test() aborts on it.
validate_regex() {
  local pattern="$1"
  if ! jq -n --arg p "$pattern" '"" | test($p; "i")' >/dev/null 2>&1; then
    die "invalid --copilot-pattern: not a valid regex: $pattern"
  fi
}

merge_review_thread_page() {
  local aggregate_json="$1"
  local page_json="$2"
  local merged_json="${aggregate_json}.tmp"

  jq -s '
    .[0] as $aggregate
    | .[1] as $page
    | {
        data: {
          repository: {
            pullRequest: {
              reviewThreads: {
                # Carry the (already validate_page-checked) latest page pageInfo so the
                # merged aggregate stays a well-formed connection object. After the loop
                # exits, this is the final page pageInfo (hasNextPage == false), which
                # lets the same validate_page chokepoint re-validate the aggregate.
                pageInfo: (
                  $page.data.repository.pullRequest.reviewThreads.pageInfo
                  // $aggregate.data.repository.pullRequest.reviewThreads.pageInfo
                ),
                nodes: (
                  ($aggregate.data.repository.pullRequest.reviewThreads.nodes // [])
                  + ($page.data.repository.pullRequest.reviewThreads.nodes // [])
                )
              }
            }
          }
        }
      }
  ' "$aggregate_json" "$page_json" >"$merged_json"

  mv "$merged_json" "$aggregate_json"
}

merge_thread_comment_page() {
  local aggregate_json="$1"
  local page_json="$2"
  local thread_id="$3"
  local merged_json="${aggregate_json}.tmp"

  jq \
    --arg thread_id "$thread_id" \
    --slurpfile page "$page_json" \
    '
      (.data.repository.pullRequest.reviewThreads.nodes) |= map(
        if .id == $thread_id then
          .comments.nodes += ($page[0].data.node.comments.nodes // [])
          | .comments.pageInfo = ($page[0].data.node.comments.pageInfo // .comments.pageInfo)
        else
          .
        end
      )
    ' "$aggregate_json" >"$merged_json"

  mv "$merged_json" "$aggregate_json"
}

fetch_all_review_threads() {
  local output_json="$1"
  local cursor=""
  local has_next="true"
  local page_number=1

  jq -n '
    {
      data: {
        repository: {
          pullRequest: {
            reviewThreads: {
              pageInfo: { hasNextPage: false, endCursor: null },
              nodes: []
            }
          }
        }
      }
    }
  ' >"$output_json"

  while [[ "$has_next" == "true" ]]; do
    local page_json="$TMP_DIR/gh-pr-review-threads-page-$page_number.json"

    # shellcheck disable=SC2016  # $owner/$name are GraphQL variables; the shell must not expand them
    gh api graphql \
      -F owner="$REPO_OWNER" \
      -F name="$REPO_NAME" \
      -F number="$PR_NUMBER" \
      -F after="$cursor" \
      -f query='
        query($owner: String!, $name: String!, $number: Int!, $after: String) {
          repository(owner: $owner, name: $name) {
            pullRequest(number: $number) {
              reviewThreads(first: 100, after: $after) {
                pageInfo {
                  hasNextPage
                  endCursor
                }
                nodes {
                  id
                  isResolved
                  isOutdated
                  path
                  line
                  startLine
                  originalLine
                  originalStartLine
                  diffSide
                  startDiffSide
                  comments(first: 100) {
                    pageInfo {
                      hasNextPage
                      endCursor
                    }
                    nodes {
                      id
                      databaseId
                      url
                      body
                      createdAt
                      authorAssociation
                      author {
                        login
                      }
                    }
                  }
                }
              }
            }
          }
        }
      ' >"$page_json"

    validate_graphql_response "$page_json" "review threads page $page_number"
    merge_review_thread_page "$output_json" "$page_json"

    # No // false / // "" fallbacks: validate_graphql_response has already asserted
    # hasNextPage is a boolean and, when true, endCursor is a non-empty string.
    has_next=$(jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage' "$page_json")
    cursor=$(jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.endCursor' "$page_json")
    page_number=$((page_number + 1))
  done
}

fetch_all_thread_comments() {
  local aggregate_json="$1"
  local comment_thread_ids

  comment_thread_ids=$(jq -r '
    .data.repository.pullRequest.reviewThreads.nodes[]
    | select(.comments.pageInfo.hasNextPage == true)
    | .id
  ' "$aggregate_json")

  if [[ -z "$comment_thread_ids" ]]; then
    return
  fi

  while IFS= read -r thread_id; do
    [[ -n "$thread_id" ]] || continue

    local cursor
    local has_next
    local page_number=2

    # No // "" / // false fallbacks: the aggregate was built from Axis A pages that
    # validate_nested_comment_connections already asserted carry a boolean
    # hasNextPage and, when true, a non-empty endCursor for every thread.
    cursor=$(jq -r '
      .data.repository.pullRequest.reviewThreads.nodes[]
      | select(.id == $thread_id)
      | .comments.pageInfo.endCursor
    ' --arg thread_id "$thread_id" "$aggregate_json")
    has_next=$(jq -r '
      .data.repository.pullRequest.reviewThreads.nodes[]
      | select(.id == $thread_id)
      | .comments.pageInfo.hasNextPage
    ' --arg thread_id "$thread_id" "$aggregate_json")

    while [[ "$has_next" == "true" ]]; do
      local page_json="$TMP_DIR/gh-thread-comments-${thread_id//[^A-Za-z0-9._-]/_}-page-$page_number.json"

      # shellcheck disable=SC2016  # $threadId/$after are GraphQL variables; the shell must not expand them
      gh api graphql \
        -F threadId="$thread_id" \
        -F after="$cursor" \
        -f query='
          query($threadId: ID!, $after: String) {
            node(id: $threadId) {
              ... on PullRequestReviewThread {
                id
                comments(first: 100, after: $after) {
                  pageInfo {
                    hasNextPage
                    endCursor
                  }
                  nodes {
                    id
                    databaseId
                    url
                    body
                    createdAt
                    authorAssociation
                    author {
                      login
                    }
                  }
                }
              }
            }
          }
        ' >"$page_json"

      validate_page "$page_json" ".data.node.comments" \
        "thread comments (thread $thread_id, page $page_number)"
      merge_thread_comment_page "$aggregate_json" "$page_json" "$thread_id"

      # No // false / // "" fallbacks: validate_page has already asserted hasNextPage
      # is a boolean and, when true, endCursor is a non-empty string.
      has_next=$(jq -r '.data.node.comments.pageInfo.hasNextPage' "$page_json")
      cursor=$(jq -r '.data.node.comments.pageInfo.endCursor' "$page_json")
      page_number=$((page_number + 1))
    done
  done <<<"$comment_thread_ids"
}

extract_repo_parts() {
  local pr_url="$1"
  local trimmed="$pr_url"

  trimmed="${trimmed#https://github.com/}"
  trimmed="${trimmed#http://github.com/}"
  trimmed="${trimmed#github.com/}"

  IFS='/' read -r REPO_OWNER REPO_NAME PULL_SEGMENT PR_NUMBER _ <<<"$trimmed"
  if [[ -z "${REPO_OWNER:-}" || -z "${REPO_NAME:-}" || "${PULL_SEGMENT:-}" != "pull" || -z "${PR_NUMBER:-}" ]]; then
    die "unable to parse GitHub PR URL: $pr_url"
  fi
  if ! [[ "$PR_NUMBER" =~ ^[0-9]+$ ]]; then
    die "invalid PR number in URL: expected an integer, got '$PR_NUMBER' ($pr_url)"
  fi
}

TMP_DIR=".gauntlet/tmp"
PR_VIEW_JSON=""
REVIEW_THREADS_JSON=""
RAW_OUTPUT=""
DEDUP_OUTPUT=""
# Anchored so the alternation matches the WHOLE login, not a substring: an
# unanchored pattern accepts an impostor like `not-a-copilot-user`. The optional
# `[bot]` suffix covers the review author `copilot-pull-request-reviewer[bot]`;
# the comment author is the bare `Copilot` (case-folded by test(...; "i") below).
COPILOT_PATTERN='^(copilot|github-copilot|copilot-pull-request-reviewer)(\[bot\])?$'

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tmp-dir)
      require_value "$1" "$#"
      TMP_DIR="$2"
      shift 2
      ;;
    --pr-view-json)
      require_value "$1" "$#"
      PR_VIEW_JSON="$2"
      shift 2
      ;;
    --review-threads-json)
      require_value "$1" "$#"
      REVIEW_THREADS_JSON="$2"
      shift 2
      ;;
    --raw-output)
      require_value "$1" "$#"
      RAW_OUTPUT="$2"
      shift 2
      ;;
    --dedup-output)
      require_value "$1" "$#"
      DEDUP_OUTPUT="$2"
      shift 2
      ;;
    --copilot-pattern)
      require_value "$1" "$#"
      COPILOT_PATTERN="$2"
      shift 2
      ;;
    --help)
      usage
      exit 0
      ;;
    --*)
      echo "unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      break
      ;;
  esac
done

if [[ $# -ne 1 ]]; then
  usage >&2
  exit 1
fi

PR_URL="$1"
mkdir -p "$TMP_DIR"

if [[ -z "$RAW_OUTPUT" ]]; then
  RAW_OUTPUT="$TMP_DIR/copilot-review-items.raw.json"
fi

if [[ -z "$DEDUP_OUTPUT" ]]; then
  DEDUP_OUTPUT="$TMP_DIR/copilot-review-items.json"
fi

# shellcheck disable=SC1007  # `CDPATH= cd` is a deliberate empty-assignment prefix, not a typo'd assignment
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

require_cmd jq
require_cmd python3

validate_regex "$COPILOT_PATTERN"

if [[ -z "$PR_VIEW_JSON" || -z "$REVIEW_THREADS_JSON" ]]; then
  require_cmd gh
fi

if [[ -z "$PR_VIEW_JSON" ]]; then
  PR_VIEW_JSON="$TMP_DIR/gh-pr-view.json"
  gh pr view "$PR_URL" --json number,title,url,headRefName,baseRefName,files >"$PR_VIEW_JSON"
fi

read_json_file "$PR_VIEW_JSON" "--pr-view-json"

CANONICAL_PR_URL=$(jq -r '.url' "$PR_VIEW_JSON")
extract_repo_parts "$CANONICAL_PR_URL"

if [[ -z "$REVIEW_THREADS_JSON" ]]; then
  REVIEW_THREADS_JSON="$TMP_DIR/gh-pr-review-threads.json"
  fetch_all_review_threads "$REVIEW_THREADS_JSON"
  fetch_all_thread_comments "$REVIEW_THREADS_JSON"
fi

read_json_file "$REVIEW_THREADS_JSON" "--review-threads-json"
validate_graphql_response "$REVIEW_THREADS_JSON" "--review-threads-json"

jq -n \
  --slurpfile pr "$PR_VIEW_JSON" \
  --slurpfile threads "$REVIEW_THREADS_JSON" \
  --arg copilot_pattern "$COPILOT_PATTERN" \
  '
    ($pr[0]) as $prv
    | ($threads[0].data.repository.pullRequest.reviewThreads.nodes) as $thread_nodes
    | [
        $thread_nodes[]
        | select(.isResolved | not)
        | . as $thread
        | ($thread.comments.nodes // [])[]
        | select((.author.login // "") | test($copilot_pattern; "i"))
        | {
            pr: {
              url: $prv.url,
              number: $prv.number,
              title: $prv.title,
              head_ref: $prv.headRefName,
              base_ref: $prv.baseRefName
            },
            thread_id: $thread.id,
            comment_id: (.databaseId // .id),
            comment_graphql_id: .id,
            author_login: (.author.login // null),
            author_association: (.authorAssociation // null),
            created_at: (.createdAt // null),
            path: ($thread.path // null),
            line: ($thread.line // null),
            start_line: ($thread.startLine // null),
            original_line: ($thread.originalLine // null),
            original_start_line: ($thread.originalStartLine // null),
            diff_side: ($thread.diffSide // null),
            start_diff_side: ($thread.startDiffSide // null),
            is_resolved: $thread.isResolved,
            is_outdated: ($thread.isOutdated // false),
            body: (.body // ""),
            url: (.url // null)
          }
      ]
  ' >"$RAW_OUTPUT"

# Catch a stale/wrong --copilot-pattern before it fails open. If unresolved review
# threads carried comments but NONE matched, a wrong allowlist yields zero items —
# indistinguishable from a genuinely clean PR (SKILL-001). Warn loudly on stderr,
# naming the author logins that were actually seen and the pattern used, so a changed
# Copilot login is visible instead of silent. This is NOT treated as an error: a PR
# with only human comments legitimately matches nothing, so exit stays 0. A truly
# empty PR (no unresolved comments at all) stays silent — nothing to warn about.
MATCHED_COUNT=$(jq 'length' "$RAW_OUTPUT")
UNRESOLVED_COMMENT_COUNT=$(jq '
  [ .data.repository.pullRequest.reviewThreads.nodes[]
    | select(.isResolved | not)
    | (.comments.nodes // [])[]
  ] | length
' "$REVIEW_THREADS_JSON")

if [[ "$MATCHED_COUNT" -eq 0 && "$UNRESOLVED_COMMENT_COUNT" -gt 0 ]]; then
  AUTHORS_SEEN=$(jq -r '
    [ .data.repository.pullRequest.reviewThreads.nodes[]
      | select(.isResolved | not)
      | (.comments.nodes // [])[]
      | .author.login // "(null)"
    ] | unique | join(", ")
  ' "$REVIEW_THREADS_JSON")
  printf "WARNING: 0 of %s review comments matched --copilot-pattern '%s'\n" \
    "$UNRESOLVED_COMMENT_COUNT" "$COPILOT_PATTERN" >&2
  printf 'WARNING: authors seen: %s\n' "$AUTHORS_SEEN" >&2
  printf "WARNING: if Copilot's login has changed, pass --copilot-pattern to override\n" >&2
fi

python3 "$SCRIPT_DIR/dedup_review_items.py" "$RAW_OUTPUT" "$DEDUP_OUTPUT"

echo "raw_items=$RAW_OUTPUT"
echo "deduped_items=$DEDUP_OUTPUT"
