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

# Step 1 — seed secrets BEFORE bundle deploy.
#
# Apps' secret resource bindings are validated EAGERLY at app create/update
# time, not lazily at app start. So `bundle deploy` will 404 on the app
# update if the secret keys don't exist yet. We seed them here from
# variable-overrides.json so the bundle deploy passes validation.
echo "==> step 1: seed secret scope + per-config secrets ($SCOPE)"
databricks secrets create-scope "$SCOPE" $EXTRA_FLAGS 2>/dev/null \
    || echo "    (scope already exists — ok)"

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
# Empty string is a valid secret value (e.g., lakebase_instance="" when in
# Project mode). The CLI rejects empty `--string-value`, so we use a space
# and have the app's config.py treat both empty and whitespace as None.
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
    [ -z "$v" ] && v=" "
    databricks secrets put-secret "$SCOPE" "$k" --string-value "$v" $EXTRA_FLAGS
    echo "    put $SCOPE/$k = ${v:- (empty)}"
done

# Step 2 — bundle deploy (creates UC schema/volume, app + bindings, postdeploy job)
echo "==> step 2: bundle deploy"
databricks bundle deploy -t "$TARGET" --auto-approve $EXTRA_FLAGS

# Step 3 — postdeploy job: Lakebase DDL + GRANTs + Delta tables + Volume dirs
echo "==> step 3: postdeploy_setup"
databricks bundle run -t "$TARGET" doc_translation_postdeploy_setup $EXTRA_FLAGS

# Step 4 — App source push + start
echo "==> step 4: deploy + start app"
databricks bundle run -t "$TARGET" doc_translation_app $EXTRA_FLAGS

echo "==> done. App URL is in the output above."
