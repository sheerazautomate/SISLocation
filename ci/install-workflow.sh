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
#   ./ci/install-workflow.sh --phase2     # install the coordinate workflow
#   ./ci/install-workflow.sh --dashboard  # install the GitHub Pages dashboard workflow
#
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

SRC="ci/fetch-dtms.yml.txt"
DEST=".github/workflows/fetch-dtms.yml"
MODE="default-branch"
TITLE="Phase 1 - Fetch DTMS IDs"
PHASE="phase-1 DTMS"
KIND="extract"

for arg in "$@"; do
  case "$arg" in
    --here) MODE="here" ;;
    --phase2)
      SRC="ci/fetch-coords.yml.txt"
      DEST=".github/workflows/fetch-coords.yml"
      TITLE="Phase 2 - Fetch School Coordinates"
      PHASE="phase-2 coordinates"
      ;;
    --dashboard)
      SRC="ci/dashboard-pages.yml.txt"
      DEST=".github/workflows/dashboard-pages.yml"
      TITLE="Dashboard - Publish to GitHub Pages"
      PHASE="dashboard"
      KIND="dashboard"
      ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
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
  if [ "$KIND" = "dashboard" ]; then
    git commit -m "ci: add dashboard Pages workflow"
  else
    git commit -m "ci: add $PHASE extraction workflow"
  fi
  git push origin "HEAD:$TARGET"
  echo "Pushed to '$TARGET'."
fi

if [ "$KIND" = "dashboard" ]; then
cat <<EOF

Done. Next steps (one time):

  1. Turn on GitHub Pages: Settings -> Pages -> Build and deployment ->
     Source: "GitHub Actions".

  2. The site rebuilds after each successful Phase 2 run on '$TARGET', after
     pushes that change the dashboard or the data, and on demand:

       gh workflow run $(basename "$DEST") --ref $TARGET

  3. The site address is https://<owner>.github.io/<repo>/. The workflow log of
     the deploy job prints the exact URL.

EOF
else
cat <<EOF

Done. Next steps:

  1. Ensure the base file (and data/school_ids.jsonl for phase 2) plus the
     pipeline Python code are committed to the branch you select for the run.

  2. Start it from the UI:  Actions -> "$TITLE" -> Run workflow
     ...or from the CLI:

       gh workflow run $(basename "$DEST") --ref $TARGET -f limit=20     # smoke test
       gh workflow run $(basename "$DEST") --ref $TARGET                 # full 38K run

  3. Watch it:

       gh run watch \$(gh run list --workflow=$(basename "$DEST") -L1 --json databaseId -q '.[0].databaseId')

EOF
fi

if [ "$TARGET" != "$CURRENT" ]; then
  echo "Returning to '$CURRENT'."
  git checkout "$CURRENT"
fi
