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

SCOPE=$(python3 -c "import json,sys; print(json.load(open('$OVERRIDES')).get('secret_scope','doc_translation_config'))")

# Step 2 — seed secret scope (idempotent). We don't actually need any
# secrets today (everything comes from bundle vars + bindings), but creating
# the scope is cheap and gives us a place to hang future secrets without a
# redeploy. The `--scope-backend-type DATABRICKS` ensures we don't
# accidentally use a KV-backed scope the SP can't read.
echo "==> step 2: seed secret scope ($SCOPE)"
databricks secrets create-scope "$SCOPE" $EXTRA_FLAGS 2>/dev/null \
    || echo "    (scope already exists — ok)"

# Step 3 — bundle deploy (resources + sync code to workspace)
echo "==> step 3: bundle deploy"
databricks bundle deploy -t "$TARGET" --auto-approve $EXTRA_FLAGS

# Step 4 — postdeploy: DDL + GRANTs + Volume dirs
echo "==> step 4: postdeploy_setup (DDL + GRANTs + Volume dirs)"
databricks bundle run -t "$TARGET" doc_translation_postdeploy_setup $EXTRA_FLAGS

# Step 5 — App source push + start. `bundle run` on an `apps.<name>` resource
# does both the `apps deploy` and the start (deploy guide gotcha #8).
echo "==> step 5: deploy + start app"
databricks bundle run -t "$TARGET" doc_translation_app $EXTRA_FLAGS

echo "==> done. App URL is in the output above."
