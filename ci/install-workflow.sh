#!/usr/bin/env bash
#
# Installs the Actions workflow into .github/workflows/.
#
# Why this script exists:
#   The Arena GitHub App is not granted the `workflows` permission, so the agent
#   cannot push files under .github/workflows/ (git push and the REST Contents
#   API both return 403). You run this once with your own credentials.
#
# Why it targets the default branch by default:
#   GitHub only shows the "Run workflow" button for `workflow_dispatch` when the
#   workflow file exists on the repository's DEFAULT branch. Installing it only
#   on a feature branch means the button never appears.
#
# Usage:
#   ./ci/install-workflow.sh              # install onto the default branch (recommended)
#   ./ci/install-workflow.sh --here       # install onto the current branch instead
#
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

SRC="ci/fetch-dtms.yml.txt"
DEST=".github/workflows/fetch-dtms.yml"
MODE="default-branch"

for arg in "$@"; do
  case "$arg" in
    --here) MODE="here" ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

[ -f "$SRC" ] || { echo "ERROR: $SRC not found (run from the repo)." >&2; exit 1; }

CURRENT="$(git rev-parse --abbrev-ref HEAD)"

if [ "$MODE" = "here" ]; then
  TARGET="$CURRENT"
else
  TARGET="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||' || true)"
  if [ -z "$TARGET" ]; then
    TARGET="$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo main)"
  fi
fi

echo "Installing $DEST onto branch: $TARGET"

if [ "$TARGET" != "$CURRENT" ]; then
  git fetch origin "$TARGET"
  git checkout "$TARGET"
  git pull --ff-only origin "$TARGET" || true
fi

mkdir -p "$(dirname "$DEST")"
cp "$SRC" "$DEST"
git add "$DEST"

if git diff --staged --quiet; then
  echo "Workflow already up to date on '$TARGET'."
else
  git commit -m "ci: add phase-1 DTMS extraction workflow"
  git push origin "HEAD:$TARGET"
  echo "Pushed to '$TARGET'."
fi

cat <<EOF

Done. Next steps:

  1. Commit your base file to the SAME branch you will run from ('$TARGET'),
     because the runner checks out that branch:

       git add -f "data/Base Schools.json"
       git commit -m "data: add base schools file"
       git push origin $TARGET

  2. Start it from the UI:  Actions -> "Phase 1 - Fetch DTMS IDs" -> Run workflow
     ...or from the CLI:

       gh workflow run fetch-dtms.yml --ref $TARGET -f limit=20     # smoke test
       gh workflow run fetch-dtms.yml --ref $TARGET                 # full 38K run

  3. Watch it:

       gh run watch \$(gh run list --workflow=fetch-dtms.yml -L1 --json databaseId -q '.[0].databaseId')

EOF

if [ "$TARGET" != "$CURRENT" ]; then
  echo "Returning to '$CURRENT'."
  git checkout "$CURRENT"
fi
