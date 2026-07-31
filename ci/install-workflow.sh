#!/usr/bin/env bash
# Installs the Actions workflow. Needed because the Arena GitHub App is not
# granted the `workflows` permission, so the agent cannot push .github/workflows/*.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
mkdir -p .github/workflows
cp ci/fetch-dtms.yml.txt .github/workflows/fetch-dtms.yml
git add .github/workflows/fetch-dtms.yml
git commit -m "ci: add phase-1 DTMS extraction workflow"
git push origin "$(git rev-parse --abbrev-ref HEAD)"
echo "Installed. GitHub -> Actions -> 'Phase 1 - Fetch DTMS IDs' -> Run workflow."
