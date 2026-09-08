#!/usr/bin/env bash
# First-time setup for a new clone. Creates the per-workspace config a target
# needs so `./deploy.sh` can run — the `.databricks/bundle/<target>/` folder and
# a `variable-overrides.json` copied from the template. This file is gitignored
# (it holds workspace-specific values), which is why a fresh clone doesn't have it.
#
# Usage:
#   ./init.sh              # sets up the 'prod' target
#   ./init.sh <target>     # sets up a custom target name
#
# Never overwrites an existing variable-overrides.json.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

TARGET="${1:-prod}"
DIR=".databricks/bundle/$TARGET"
DEST="$DIR/variable-overrides.json"

mkdir -p "$DIR"
if [[ -f "$DEST" ]]; then
    echo "✓ $DEST already exists — leaving it untouched."
else
    cp variable-overrides.example.json "$DEST"
    echo "✓ Created $DEST from variable-overrides.example.json"
fi

echo
echo "Next steps:"
echo "  1. Edit $DEST — fill in workspace_user_email, uc_catalog, lakebase_project,"
echo "     lakebase_branch, lakebase_database_slug, warehouse_id, app_name"
echo "     (each field is documented in the file / variables.yml)."
echo "  2. Deploy:  ./deploy.sh $TARGET --profile <your-databricks-cli-profile>"
