#!/usr/bin/env bash
# Build the React frontend → static/ then verify the output exists.
#
# Run this BEFORE `./deploy.sh` (or before `databricks bundle deploy`): the
# bundle ships the prebuilt static/ directory as the app's source, and
# server_api.py serves it. The build step is intentionally separate from
# deploy so the Databricks Apps runtime never needs Node.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "→ Installing frontend deps + building"
cd frontend
npm install
npm run build
cd ..

echo "→ Verifying static/ output"
test -f static/index.html
test -d static/assets
ls -lh static/

echo
echo "Build complete. Next: ./deploy.sh   (or re-run it if already deployed)"
