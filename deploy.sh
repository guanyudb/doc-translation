#!/usr/bin/env bash
# Doc Translation Review Platform — one-command deploy via DAB.
#
# Usage:
#   ./deploy.sh                  # deploys the 'prod' target
#   ./deploy.sh <target>         # deploys a custom target name
#   ./deploy.sh prod --profile <profile>  # passes through to databricks
#
# Prerequisites (workspace must already have):
#   * A Unity Catalog with CREATE SCHEMA + CREATE VOLUME permission for you
#   * A Lakebase instance (Project preferred; Provisioned legacy ok)
#   * A SQL warehouse (any serverless 2X-Small is fine)
#   * `.databricks/bundle/<target>/variable-overrides.json` populated from
#     `variable-overrides.example.json`
#
# The 5-step ordering (deploy guide §"The 5-step ordering"):
#   1. (Streamlit — no build step needed)
#   2. Seed secrets (idempotent)
#   3. bundle deploy
#   4. bundle run postdeploy_setup (GRANTs + DDL)
#   5. bundle run app (source push + start)

set -euo pipefail

TARGET="${1:-prod}"
shift || true
EXTRA_FLAGS="$@"

echo "==> deploying target: $TARGET"

# Sanity check: variable-overrides.json must exist
OVERRIDES=".databricks/bundle/$TARGET/variable-overrides.json"
if [[ ! -f "$OVERRIDES" ]]; then
    echo "ERROR: $OVERRIDES is missing."
    echo "       Copy variable-overrides.example.json into place and fill in your values:"
    echo "         mkdir -p $(dirname "$OVERRIDES")"
    echo "         cp variable-overrides.example.json $OVERRIDES"
    echo "         # then edit it"
    exit 1
fi

# Step 1 — bundle deploy (creates UC schema/volume, secret scope, app
# resource with bindings, postdeploy job; uploads source files).
echo "==> step 1: bundle deploy"
databricks bundle deploy -t "$TARGET" --auto-approve $EXTRA_FLAGS

# Step 2 — postdeploy job: seeds the secret VALUES (from bundle vars),
# runs Lakebase DDL + GRANTs, creates Delta mirror tables, pre-creates
# Volume subdirs. Secrets MUST be seeded before step 3 so the app's
# secret bindings resolve at boot.
echo "==> step 2: postdeploy_setup (secrets + DDL + GRANTs + Volume dirs)"
databricks bundle run -t "$TARGET" doc_translation_postdeploy_setup $EXTRA_FLAGS

# Step 3 — App source push + start. `bundle run` on an `apps.<name>` resource
# does both the `apps deploy` and the start (deploy guide gotcha #8).
echo "==> step 3: deploy + start app"
databricks bundle run -t "$TARGET" doc_translation_app $EXTRA_FLAGS

echo "==> done. App URL is in the output above."
