#!/usr/bin/env bash
set -euo pipefail
# One-command ship: commit → push → GitHub-based deploy (git pull + cached rebuild).
#
#   ./deploy/ship.sh                      # deploy as-is (pushes unpushed commits)
#   ./deploy/ship.sh "fix: explorer nav"  # commit all changes with a message, then deploy
#   ./deploy/ship.sh --vm <name> "msg"    # deploy to a specific VM (default: s3lm-budget)
#   ./deploy/ship.sh --help               # this help
#
# Env: DEPLOY_BRANCH=main GCP_ACCOUNT GCP_PROJECT GCP_ZONE GCLOUD_BIN GITHUB_ACCOUNT
set -e
usage() { grep '^#' "$0" | sed 's/^# \{0,1\}//' | grep -v '^!'; exit 0; }
VM=""
MSG=""
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --vm) VM="$2"; shift 2 ;;
    --help|-h) usage ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
[ "${#ARGS[@]}" -gt 0 ] && MSG="${ARGS[-1]}"

cd "$(dirname "$0")/.."

# Verify the account, billing, organization and VM before committing or pushing.
"./deploy/deploy.sh" --check ${VM:+"$VM"}

echo "→ [1/3] commit"
if [ -n "$(git status --porcelain)" ]; then
  if [ -z "$MSG" ]; then
    echo "  ✗ working tree has changes — provide a commit message:" >&2
    echo "      ./deploy/ship.sh \"what changed\"" >&2
    exit 2
  fi
  git add -A
  git commit -m "$MSG"
else
  echo "  nothing to commit"
fi

echo "→ [2/3] push"
AHEAD=$(git rev-list --count "origin/${DEPLOY_BRANCH:-main}..HEAD" 2>/dev/null || echo "?")
if [ "$AHEAD" = "0" ]; then
  echo "  already up to date"
else
  if command -v gh >/dev/null 2>&1; then
    # Select the saved repository login for this push without switching gh globally.
    PUSH_TOKEN=$(GH_TOKEN= GITHUB_TOKEN= gh auth token --hostname github.com \
      --user "${GITHUB_ACCOUNT:-vijaykavuri43-sudo}")
    GH_TOKEN="$PUSH_TOKEN" git -c credential.helper= \
      -c 'credential.helper=!gh auth git-credential' push origin HEAD
    unset PUSH_TOKEN
  else
    git push origin HEAD
  fi
fi

echo "→ [3/3] deploy (${VM:-s3lm-budget})"
exec "$(dirname "$0")/deploy.sh" ${VM:+"$VM"}
