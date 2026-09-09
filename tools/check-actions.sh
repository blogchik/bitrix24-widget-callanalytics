#!/usr/bin/env bash
#
# Every action these workflows run must be pinned to a commit sha and must declare a
# JavaScript runtime GitHub still supports.
#
#   GH_TOKEN=$(gh auth token) ./tools/check-actions.sh
#
# Two failures, and both of them arrive as warnings that nobody reads until they become
# errors:
#
#  1. **A floating tag.** `uses: some/action@v4` is a mutable reference to code this
#     workflow executes with the repository checked out and, in the deploy pipeline, with
#     a key that can restart production. Whoever controls that tag controls the run.
#     A 40-character sha is the only reference that cannot be moved under us.
#
#  2. **A deprecated Node runtime.** An action declaring `using: node20` is currently
#     forced onto Node 24 by the runner, with a warning; when the runner stops carrying
#     that shim the action stops working, and it will be the deployment pipeline that
#     finds out. This turns the warning into a failing check while it is still cheap.
#
# Actions that are `composite` or `docker` run no bundled JavaScript and are exempt from
# the second rule - they are still subject to the first.
set -euo pipefail

cd "$(dirname "$0")/.."

command -v gh >/dev/null || { echo 'FAIL: gh is required'; exit 1; }

# What the runner currently ships. Raise this when GitHub raises it; do not lower it to
# make a check pass.
MINIMUM_NODE=24

problems=0
checked=0

note() { printf '  %s\n' "$*"; }
bad()  { printf 'FAIL: %s\n' "$*" >&2; problems=$((problems + 1)); }

# Collect every `uses:` in every workflow, deduplicated - the same action appears in
# several jobs and there is no reason to ask GitHub about it more than once.
mapfile -t uses < <(grep -rhoE '^\s*(-\s*)?uses:\s*\S+' .github/workflows/*.yml \
  | sed -E 's/^\s*(-\s*)?uses:\s*//' | sort -u)

for ref in "${uses[@]}"; do
  # A local action is ours, in this tree, reviewed in the same pull request.
  case "$ref" in ./*) note "$ref (local)"; continue ;; esac

  repo_and_path="${ref%@*}"
  sha="${ref##*@}"
  owner="${repo_and_path%%/*}"
  rest="${repo_and_path#*/}"
  name="${rest%%/*}"
  subpath="${rest#"$name"}"
  subpath="${subpath#/}"

  if [[ ! "$sha" =~ ^[0-9a-f]{40}$ ]]; then
    bad "$ref is not pinned to a commit sha"
    continue
  fi

  # `action.yml` and `action.yaml` are both legal and both are used in the wild.
  meta=''
  for file in action.yml action.yaml; do
    path="${subpath:+$subpath/}$file"
    if meta=$(gh api "repos/$owner/$name/contents/$path?ref=$sha" --jq '.content' 2>/dev/null \
              | base64 -d 2>/dev/null) && [ -n "$meta" ]; then
      break
    fi
    meta=''
  done

  if [ -z "$meta" ]; then
    bad "$ref: could not read its action definition at that sha"
    continue
  fi

  using=$(printf '%s\n' "$meta" | grep -E "^\s*using:" | head -1 \
          | sed -E "s/.*using:\s*['\"]?([A-Za-z0-9]+)['\"]?.*/\1/")
  checked=$((checked + 1))

  case "$using" in
    node*)
      version="${using#node}"
      if [ "$version" -lt "$MINIMUM_NODE" ]; then
        bad "$ref runs on $using; GitHub deprecated it and the runner only shims it for now"
      else
        note "$ref ($using)"
      fi
      ;;
    composite|docker)
      note "$ref ($using - no bundled JavaScript runtime)"
      ;;
    '')
      bad "$ref: its action definition declares no runtime"
      ;;
    *)
      bad "$ref declares an unrecognised runtime '$using'"
      ;;
  esac
done

echo
if [ "$problems" -gt 0 ]; then
  echo "$problems problem(s) across $checked action(s)." >&2
  exit 1
fi
echo "$checked actions, all pinned to a sha and none on a deprecated runtime."
