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

# Step 2 — seed secret scope + per-config secrets (idempotent).
#
# Why secrets at all: DAB doesn't substitute ${var.X} inside user files like
# app.yaml. The canonical workaround is to put workspace-specific config
# values in a secret scope and bind them as resources, then reference via
# `valueFrom:` in app.yaml. Cheap, well-supported, audit-friendly.
echo "==> step 2: seed secret scope ($SCOPE) + per-config secrets"
databricks secrets create-scope "$SCOPE" $EXTRA_FLAGS 2>/dev/null \
    || echo "    (scope already exists — ok)"

# Read values from the variable-overrides file, compute derived values,
# and put them as secrets. Defaults match variables.yml.
PYREAD='import json,os,sys
d=json.load(open(os.environ["OVR"]))
uc=d.get("uc_catalog","")
sc=d.get("uc_schema","doc_translation")
vn=d.get("uc_volume_name","doc-translation")
print(d.get("pg_schema","doc_translation"))
print(d.get("lakebase_project",""))
print(d.get("lakebase_branch","main"))
print(d.get("lakebase_instance",""))
print(f"/Volumes/{uc}/{sc}/{vn}")
print(uc)
print(sc)'
read -r PG_SCHEMA LB_PROJECT LB_BRANCH LB_INSTANCE VOL_ROOT DELTA_CAT DELTA_SCH < <(
    OVR="$OVERRIDES" python3 -c "$PYREAD" | xargs
)
for kv in \
    "pg_schema=$PG_SCHEMA" \
    "lakebase_project=$LB_PROJECT" \
    "lakebase_branch=$LB_BRANCH" \
    "lakebase_instance=$LB_INSTANCE" \
    "volume_root=$VOL_ROOT" \
    "delta_catalog=$DELTA_CAT" \
    "delta_schema=$DELTA_SCH"; do
    k="${kv%%=*}"
    v="${kv#*=}"
    databricks secrets put-secret "$SCOPE" "$k" --string-value "$v" $EXTRA_FLAGS
    echo "    put $SCOPE/$k = $v"
done

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
