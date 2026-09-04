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
#   * A Lakebase Autoscaling Project (branch + database)
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

# Step 0 — build the React frontend into static/ (unless already built).
# The Apps runtime has no Node, so the SPA must be prebuilt and shipped as
# static/. Skips the build if static/index.html is newer than the frontend
# sources (cheap re-deploys); pass FORCE_BUILD=1 to always rebuild.
if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
    if [[ ! -f static/index.html || "${FORCE_BUILD:-0}" == "1" ]]; then
        echo "==> step 0: build React frontend"
        ./build.sh
    else
        echo "==> step 0: static/ present — skipping frontend build (FORCE_BUILD=1 to rebuild)"
    fi
fi

# Step 1 — seed secrets BEFORE bundle deploy.
#
# Apps' secret resource bindings are validated EAGERLY at app create/update
# time, not lazily at app start. So `bundle deploy` will 404 on the app
# update if the secret keys don't exist yet. We seed them here from
# variable-overrides.json so the bundle deploy passes validation.
echo "==> step 1: seed secret scope + per-config secrets ($SCOPE)"
databricks secrets create-scope "$SCOPE" $EXTRA_FLAGS 2>/dev/null \
    || echo "    (scope already exists — ok)"

# POSIX-safe key=value parsing. Earlier attempts used `IFS=$'\t' read ...`
# but ANSI-C quoting isn't available in dash (which the Databricks Web
# Terminal's compute env uses as /bin/sh). Here Python emits one
# `key=value` line per secret; bash splits on the FIRST `=` using
# parameter expansion — works in any POSIX shell.
PYREAD='import json, os
d = json.load(open(os.environ["OVR"]))
uc = d.get("uc_catalog","")
sc = d.get("uc_schema","doc_translation")
vn = d.get("uc_volume_name","doc-translation")
pairs = [
    ("pg_schema",         d.get("pg_schema","doc_translation")),
    ("lakebase_project",  d.get("lakebase_project","")),
    ("lakebase_branch",   d.get("lakebase_branch","production")),
    ("volume_root",       f"/Volumes/{uc}/{sc}/{vn}"),
    ("delta_catalog",     uc),
    ("delta_schema",      sc),
    ("app_title",         d.get("app_title","Doc Translation Review")),
    ("app_logo_url",      d.get("app_logo_url","")),
    ("app_logo_alt",      d.get("app_logo_alt","")),
    # Admins who may change Settings — the deploying user by default; set
    # `app_admin_emails` (comma-separated) in overrides to add more.
    ("admin_emails",      d.get("app_admin_emails") or d.get("workspace_user_email","")),
]
for k, v in pairs:
    print(f"{k}={v}")'

# Empty string is a valid secret value (e.g. a blank branding field). The CLI
# rejects empty `--string-value`, so substitute a single space; the app
# config.py treats both empty and whitespace-only as None.
OVR="$OVERRIDES" python3 -c "$PYREAD" | while IFS= read -r LINE; do
    KEY="${LINE%%=*}"
    VALUE="${LINE#*=}"
    [ -z "$VALUE" ] && VALUE=" "
    databricks secrets put-secret "$SCOPE" "$KEY" --string-value "$VALUE" $EXTRA_FLAGS
    echo "    put $SCOPE/$KEY = ${VALUE:- (empty)}"
done

# Step 2 — bundle deploy (creates UC schema/volume, app + bindings, postdeploy job).
# --force-lock overrides any stale deploy lock left by a previous run that
# crashed before releasing. Safe here because deploy.sh is the canonical
# orchestrator for this bundle (no concurrent multi-user deploys assumed).
echo "==> step 2: bundle deploy"
databricks bundle deploy -t "$TARGET" --auto-approve --force-lock $EXTRA_FLAGS

# Step 3 — postdeploy job: Lakebase DDL + GRANTs + Delta tables + Volume dirs
echo "==> step 3: postdeploy_setup"
databricks bundle run -t "$TARGET" doc_translation_postdeploy_setup $EXTRA_FLAGS

# Step 4 — App source push + start
echo "==> step 4: deploy + start app"
databricks bundle run -t "$TARGET" doc_translation_app $EXTRA_FLAGS

# Step 5 — grant the app SP CAN_MANAGE_RUN on the translation pipeline job.
# The app triggers this job to re-translate a DOCX (POST /retranslate); without
# run permission that fails. `bundle deploy` (step 2) resets job permissions, so
# this MUST run after it. Best-effort — a failure here only affects DOCX
# re-translate, not the core deploy.
echo "==> step 5: grant app SP run permission on the translation pipeline job"
OVR="$OVERRIDES" FLAGS="$EXTRA_FLAGS" python3 <<'PYGRANT' || echo "    (grant step failed — DOCX re-translate may need a manual grant)"
import json, os, subprocess, sys
d = json.load(open(os.environ["OVR"]))
flags = os.environ.get("FLAGS", "").split()
app = d.get("app_name", "doc-translation")
raw_dir = (f"/Volumes/{d.get('uc_catalog','')}/{d.get('uc_schema','doc_translation')}"
           f"/{d.get('uc_volume_name','doc-translation')}/raw_documents")

def cli(*args):
    return subprocess.run(["databricks", *args, *flags], capture_output=True, text=True)

r = cli("apps", "get", app, "-o", "json")
sp = (json.loads(r.stdout).get("service_principal_client_id")
      if r.returncode == 0 and r.stdout.strip() else None)
if not sp:
    print("    [warn] could not resolve app SP; skipping"); sys.exit(0)
jobs = json.loads(cli("jobs", "list", "-o", "json").stdout or "[]")
job_id = None
for j in jobs:
    if (j.get("settings") or {}).get("name") != "doc-translation · auto-translate pipeline":
        continue
    full = json.loads(cli("jobs", "get", str(j["job_id"]), "-o", "json").stdout or "{}")
    for t in ((full.get("settings") or {}).get("tasks") or []):
        bp = (t.get("notebook_task") or {}).get("base_parameters") or {}
        if bp.get("raw_dir") == raw_dir:
            job_id = j["job_id"]; break
    if job_id:
        break
if not job_id:
    print("    [warn] pipeline job not found by raw_dir; skipping"); sys.exit(0)
acl = {"access_control_list": [{"service_principal_name": sp, "permission_level": "CAN_MANAGE_RUN"}]}
r = cli("permissions", "update", "jobs", str(job_id), "--json", json.dumps(acl))
print(f"    granted app SP CAN_MANAGE_RUN on pipeline job {job_id}" if r.returncode == 0
      else f"    [warn] grant failed: {r.stderr[:200]}")
PYGRANT

echo "==> done. App URL is in the output above."
