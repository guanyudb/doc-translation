# Doc Translation Platform — Production Pipeline Design

> Production-grade architecture brief for the document-translation review app.
> Written with two hats: data engineer (scale, idempotency, recovery) and
> compliance officer (traceability, immutability, attribution).

See the rendered architecture diagram: [`architecture.svg`](architecture.svg) · [`architecture.png`](architecture.png) · Mermaid source: [`architecture.mmd`](architecture.mmd)

---

## 0. Confirmed scope decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | Use **filename** for protocol/study metadata (no intake form yet) | Keeps phase-1 lightweight; revisit when a regulator asks |
| 2 | **Single tenant** | One business unit, one schema, one Volume |
| 3 | Files land in `golden/` Volume after certification | The "system of record" location for downstream consumers |
| 4 | **Hybrid** storage: Delta = long-term archive, Lakebase = hot OLTP | Lakebase handles sub-100ms reviewer writes; Delta is the queryable, governable, time-travel-capable record |

---

## 1. Architecture at a glance

```
[Raw Volume]  →  [Auto Loader trigger]  →  [Translation Job (Lakeflow)]  →  [Translated Volume]
                          │                            │                              │
                          ▼                            ▼                              ▼
                   bronze_documents              silver_translation_runs       (HTML cache, Delta)
                          │                            │                              │
                          └────────────────────────────┴──────────────┬───────────────┘
                                                                      ▼
                                                            [ Lakebase: live review ]
                                                                      │ (reviewer app)
                                                                      ▼
                                                         [ Certify → Promote ]
                                                                      │
                                                                      ▼
                                                  [Golden Volume]   [gold_certified_documents]
                                                                      │
                                                                      ▼
                                                           [audit_events — immutable]
```

Two storage planes, intentionally split:

- **Delta lakehouse** owns lineage, run history, and audit log. Append-mostly, analytical, regulatable.
- **Lakebase Postgres** owns the live review session (paragraph status, edits, comments). Low-latency, transactional, hot path for the app. Mirrored to Delta at promotion time.

This split is deliberate: the app needs sub-100ms writes per paragraph, which Lakebase delivers. The auditor needs an immutable, time-travel-capable record, which Delta delivers. Trying to force either system into the other's role causes pain on the appropriate axis.

---

## 2. Document lifecycle (single source of truth)

Every document carries one `lifecycle_state`. Transitions are append-only events in `audit_events`. No state is ever rewritten in place.

| State | Meaning | Allowed next |
|---|---|---|
| `LANDED` | File arrived in raw Volume; SHA-256 + size recorded | `QUEUED`, `REJECTED_INTAKE` |
| `QUEUED` | Auto Loader picked it up; job submitted | `TRANSLATING`, `FAILED_QUEUE` |
| `TRANSLATING` | Pipeline running; per-paragraph progress emitted | `TRANSLATED`, `FAILED_TRANSLATION` |
| `TRANSLATED` | Output written to translated Volume; hash recorded | `UNDER_REVIEW` |
| `UNDER_REVIEW` | At least one reviewer opened it | `EDITED`, `CERTIFIED`, `REJECTED_REVIEW` |
| `EDITED` | Has unpublished edits | `UNDER_REVIEW`, `CERTIFIED` |
| `CERTIFIED` | All paragraphs status=certified, no pending edits | `PROMOTING` |
| `PROMOTING` | Publishing to golden zone (atomic) | `PUBLISHED`, `FAILED_PROMOTION` |
| `PUBLISHED` | In golden Volume; ACL locked | `ARCHIVED` |
| `ARCHIVED` | Retention period started | (terminal) |
| `FAILED_*` | DLQ; ops triage required | manual recovery via runbook |
| `REJECTED_*` | Reviewer or intake rejected | manual reroute |

A compliance officer reads this and immediately knows: *what can I find out about any document?* The lifecycle history is recoverable from `audit_events` alone — every other table is a materialization for query convenience.

> **Currently implemented (iteration 1):** `UNDER_REVIEW → PROMOTING → PUBLISHED` (the app-facing portion). LANDED/QUEUED/TRANSLATING come online once the Auto Loader trigger is wired (iteration 3).

---

## 3. Storage model

### Delta (in UC, governable, time-traveled)

Schema: `hls_amer_catalog.guanyu_chen.*` (single-tenant default).

**`bronze_documents`** — one row per landed file, ever.
- `document_id` (UUID, deterministic from input_hash)
- `input_path`, `input_hash_sha256`, `input_size_bytes`
- `landed_at`, `landed_by` (SP that wrote it, from cloud event)
- `source_system` (PI provided it via SFTP, etc.)
- `intake_metadata` (JSON: protocol number, indication, study phase — supplied by submitter)
- `lifecycle_state` (current — updated via merge from audit_events)

**`silver_translation_runs`** — one row per pipeline attempt.
- `run_id`, `document_id`, `attempt_number`
- `model_endpoint`, `model_version`, `prompt_template_hash`
- `started_at`, `ended_at`, `status`
- `paragraphs_total`, `paragraphs_translated`, `paragraphs_failed`
- `output_path`, `output_hash_sha256`
- `cost_estimate_usd`, `tokens_in`, `tokens_out`
- `cluster_id` / `job_run_id` (Databricks linkage for log retrieval)
- `error_class`, `error_message` (when failed)

**`silver_review_snapshots`** — periodic snapshot of Lakebase state.
- Every paragraph's status, comment, edit, reviewer, timestamp
- Snapshots at promotion time (iteration 1); every N minutes possible later
- This is what survives if Lakebase is ever lost

**`gold_certified_documents`** — one row per published document. *(Currently implemented as `golden_publications`.)*
- `document_id`, `published_version`, `golden_path`, `golden_hash_sha256`
- `published_at`, `published_by`
- `certified_by` (set of reviewers — at least one)
- `edits_applied` (count + diff stats)
- `original_hash` (binds back to bronze)
- `digital_signature` (optional, see §7)

**`audit_events`** — append-only, the ground truth.
- `event_id` (monotonic), `document_id`, `event_at` (monotonic timestamp from DB, not client clock)
- `actor` (email or SP id), `actor_session_id`
- `event_type` (`OPENED`, `LANDED`, `QUEUED`, `PARAGRAPH_EDITED`, `PARAGRAPH_CERTIFIED`, `GOLD_PROMOTED`, `ACL_CHANGED`, `INVALID_WRITE_BLOCKED`, etc.)
- `before_value`, `after_value` (JSON)
- `client_ip`, `client_user_agent` (from `X-Forwarded-*` headers in Apps)
- `correlation_id` (job run id, request id, edit id — whatever ties to the source)

Table constraints I'd enforce: `audit_events` gets *only* INSERT grants for the SP and the app SP. No UPDATE, no DELETE — ever. The auditor's read-only role gets SELECT. This is the 21 CFR Part 11 "non-rewritable, non-erasable" requirement.

Delta `audit_events` is created with `delta.appendOnly = 'true'` and 7-year retention (`2557 days`).

### Lakebase (hot path, OLTP)

Schema: `databricks_postgres.doc_translation`.

`review_pairs`, `review_feedback`, `review_edit_history`, `review_publish_log` — already in production. Adds in iteration 1:

- `review_pairs.lifecycle_state` — `UNDER_REVIEW | PROMOTING | PUBLISHED | ARCHIVED` with CHECK constraint
- `review_pairs.locked_at`, `original_hash`, `translated_hash`, `source_lang`
- `audit_events` — full event log (mirrored to Delta at promotion)
- `golden_publications` — one row per promotion

Planned in future iterations:
- `review_sessions` — every time a reviewer opens a document (session_id, started_at, last_active_at, client_ip). Lets us detect concurrent reviewers, abandoned sessions, ghost edits.
- `review_cursor` — recoverable bookmark per reviewer

CDC from Lakebase → Delta runs at promotion time (iteration 2). Anything in flight (last 5 min) is recoverable from Postgres WAL.

---

## 4. Translation pipeline (Lakeflow Job)

> **Status:** planned (iteration 3). Today, translation happens manually in a notebook.

**Trigger.** Two options, default to (a):

1. **Auto Loader on `raw_documents/`** — file-arrival trigger on a Databricks Job. Native, robust, exactly-once semantics via `cloudFiles` metadata table.
2. **Lakeflow file-arrival trigger** — newer, simpler, but less mature for this scale. Worth migrating to once it stabilizes.

**Idempotency key.** `document_id = uuid_v5(namespace, input_path + input_hash)`. Re-uploading the same bytes produces the same `document_id`, so we never double-translate. Re-uploading with the same name but different bytes produces a new `document_id` and a new `bronze_documents` row — old one isn't lost.

**Steps inside the job:**

1. Compute SHA-256 of input → write `bronze_documents` row → emit `LANDED` audit event
2. Pre-flight: language detect, page count, size sanity check. Reject (event `REJECTED_INTAKE`) if oversize / corrupted / unsupported.
3. Submit `silver_translation_runs` row (status=`RUNNING`), emit `TRANSLATING` event
4. The existing in-place OOXML pipeline runs (ThreadPoolExecutor + MD5 paragraph cache + FMAPI Gemini)
5. **Per-paragraph progress events** — every N paragraphs, emit `PARAGRAPH_TRANSLATED` event with `correlation_id = run_id`. Gives ops a live progress bar without polling the file.
6. On completion: write output to `translated_inplace/`, compute output hash, update run row, emit `TRANSLATED` event
7. Insert `review_pairs` row in Lakebase so the app sees it

**Failure handling.**

- Transient (rate limit, network) → exponential backoff, up to 3 retries, same `attempt_number+1` row
- Permanent (corrupted DOCX, unsupported language) → `FAILED_TRANSLATION` event, file moved to `dlq/` Volume, ops alert (email + Slack via webhook). Document stays in bronze with the failure linked.
- Partial (some paragraphs failed) → translation still completes; per-paragraph failures are flagged in the review app so the reviewer knows which paragraphs to redo manually.

**Scale.** One job run per document. Parallelism within: ThreadPoolExecutor sized to FMAPI rate-limit headroom. Multiple documents run in parallel as separate job runs. Cap concurrent runs at N (configurable) so we don't burn through FMAPI quota.

**Cost telemetry.** Tokens in/out + estimated cost into `silver_translation_runs`. Powers the cost dashboard. Set a per-document budget cap; if exceeded mid-run, pause and alert.

---

## 5. App: large-volume document handling

> **Status:** partially implemented. The current app reads the full DOCX, renders all paragraphs to HTML in one shot, and ships ~500KB of HTML to the iframe. Fine for 100 paragraphs, painful at 5,000+, and a multi-hundred-MB DOCX would OOM the MEDIUM compute.

**Planned changes:**

1. **Pre-render at translation time, store in Delta.** After translation, the job also runs `docx_render.render` and writes:
   ```
   silver_rendered_html (document_id, pane [orig|tran], page,
                         paragraph_idx_start, paragraph_idx_end, html_chunk)
   ```
   The app reads per-page chunks instead of rendering on demand. Cache stays warm across reviewers.

2. **Page-level pagination in the dual_pane component.** Currently every paragraph is in the DOM. For huge docs, render only the active page ± 1 (windowed). The component already knows page boundaries.

3. **Virtualized minimap.** Today the minimap creates one DOM node per paragraph. At 5k paragraphs that's a measurable scroll cost. Aggregate into buckets of N once paragraph count exceeds a threshold.

4. **File size policy.** Hard cap: 200 MB DOCX, 10k paragraphs, 500 pages. Anything above goes through a special "long-form" flow (split before translation, review in chunks). Documented in the intake form.

5. **App compute headroom.** Move to LARGE for the reviewer app workspace if average document size grows. Lakebase pool size already at 8; bump to 16 if multiple reviewers per document becomes common.

6. **Pre-warming.** Sidebar pair list shows progress; clicking a pair triggers a background fetch of the rendered HTML so the first scroll is instant. Use `st.cache_data` + a background thread (Streamlit 1.32+ supports this cleanly).

---

## 6. Golden zone & promotion

> **Status: implemented in iteration 1.**

Path: `/Volumes/hls_amer_catalog/guanyu_chen/doc-translation/golden/<pair_id>/`

**Atomic promotion** (no half-promoted files):

1. Reviewer clicks "Promote to Gold" only when:
   - Every paragraph status = `certified`
   - Zero `flagged` paragraphs
   - Zero unpublished edits (reviewer must Publish any pending edits to a reviewed DOCX first)
2. App computes SHA-256 of both files (original + translated/reviewed)
3. App writes them to the final golden path
4. Insert `golden_publications` row with hashes, paragraph counts, reviewer list, timestamp
5. Update `review_pairs.lifecycle_state` → `PUBLISHED`, set `locked_at = now()`
6. Emit `GOLD_PROMOTED` audit event
7. Mirror to Delta (iteration 2)

**Versioning.** Single-tenant single-version: golden is the final word. Re-promotion is blocked by the state machine (only `UNDER_REVIEW → PROMOTING` is allowed). If a reviewer needs to fix something after publish, an administrator re-opens it (`PUBLISHED → UNDER_REVIEW` transition, audited as `LIFECYCLE_RE_OPENED`).

**Read-only enforcement.** Once `PUBLISHED`, every write path in `store.py` calls `_assert_unlocked` and raises `PairLockedError`. The blocked attempt is itself audited (`INVALID_WRITE_BLOCKED`) — a tamper attempt always leaves a trail.

---

## 7. Audit, traceability, compliance

This is the section where the compliance officer hat matters most.

**What I need to reconstruct, given a `document_id`, in under 5 minutes:**

- Where did the file come from? `bronze_documents` (path, hash, who uploaded)
- What translated it? `silver_translation_runs` (model, model version, prompt hash, job_run_id, cost)
- Who reviewed it? `review_sessions` joined with `audit_events` filtered to that document
- What did they change? `review_edit_history` (every keystroke save, with before/after)
- Who certified each paragraph? `audit_events` filtered to `PARAGRAPH_STATUS_CHANGED → certified`
- Who published v3? `golden_publications` and `audit_events` filtered to `GOLD_PROMOTED`
- What did v3 actually contain? Golden Volume file at the version path, plus its hash in `golden_publications` for tamper verification

**21 CFR Part 11 (FDA electronic records) checklist** — relevant if any of these documents head to a regulator:

| Requirement | Implementation |
|---|---|
| Unique user identification | `reviewer` always = OAuth email from `X-Forwarded-Email`; SP IDs for system actions |
| Authority checks | Apps SSO + UC ACLs on Volume + Lakebase roles |
| Audit trail computer-generated, time-stamped | `audit_events.event_at` = `now()` from Postgres (not app clock); append-only |
| Operational system checks | Lifecycle state machine enforces valid transitions; invalid attempts emit `INVALID_WRITE_BLOCKED` events |
| Open system protection | TLS in transit (Apps + Lakebase), at rest by UC/cloud KMS |
| Electronic signatures | Optional: every `CERTIFY` event includes reviewer's signed payload (email + UTC timestamp + paragraph hash). For full 11.50/11.70 compliance, integrate with a signature service. Out of scope v1, but design leaves room. |
| Record retention | Delta `audit_events` and `golden_publications` configured for 7-year retention (`2557 days`); `delta.appendOnly = 'true'`. Volume files in `golden/` kept indefinitely until retention sweep. |

**HIPAA** — if these documents ever contain PHI (some clinical trial docs do):

- BAA with Databricks already in place (org-level)
- Encrypt-at-rest is automatic on Volumes
- Audit log includes who-accessed-what (read events, not just write events). `OPENED` events fire on every app open.
- Minimum-necessary access: reviewer ACLs scoped per protocol via UC groups (when multi-tenant lands)

**Tamper evidence.** Future enhancement: chain `audit_events` Merkle-style — each row includes hash of previous row's contents. Detects tampering. Cheap to add, expensive to retrofit.

---

## 8. Observability & operations

**Dashboards** (AI/BI):

- Pipeline health: runs/hour, success rate, p50/p95 latency, cost/doc, FMAPI quota burn
- Review backlog: docs in `UNDER_REVIEW`, age, by-reviewer counts, paragraph throughput
- DLQ: anything in `FAILED_*` states, with age and last error
- Certification flow: time from `LANDED` to `PUBLISHED` (the business metric)

**Alerts** (Lakeflow alerts on Delta tables):

- Any document in `FAILED_*` > 1 hour
- Any document in `UNDER_REVIEW` > N days (SLA breach)
- FMAPI error rate > 5% over 15 min
- Lakebase pool exhaustion (pool stats logged every minute to a Delta table)
- Cost anomaly: single doc > 3× rolling-7d-median

**Runbook entries** (linked from each alert):

- "Job stuck in TRANSLATING" → check Databricks job UI, then `silver_translation_runs.error_message`, then DLQ
- "Reviewer reports paragraph mismatch" → re-run rendering, compare paragraph index between original and translated via `silver_rendered_html`
- "Compliance request: produce full history for X" → standardized SQL query against the 4 tables above, exported as PDF

**Identity injection.** Every event must carry an actor. The app SP doing system work uses a distinguishable `actor` value (`system:doc-translation-app@<sp-id>`), separate from human reviewers. Auditors care about this distinction.

---

## 9. Implementation phases

### Phase 1 — Lifecycle + audit + golden (✅ shipped)

- Delta-side: nothing yet (lives in iteration 2)
- Lakebase: `audit_events`, `golden_publications`, lifecycle column on `review_pairs`
- App: lifecycle chip, Promote dialog, read-only mode, audit drawer, lock guards on every write
- Volume: `golden/<pair_id>/` zone with SHA-256 hashing

### Phase 2 — Delta long-term mirror (🚧 in progress)

- Delta tables: `audit_events`, `golden_publications`, `silver_review_snapshots`
- `server/delta_sync.py`: idempotent MERGE via SQL warehouse statement-execution
- Wire `sync_pair_to_delta` into the Promote flow as a best-effort post-step
- On sync failure: emit `DELTA_SYNC_FAILED` audit event; doesn't block the promotion (Lakebase is the source of truth)

### Phase 3 — Translation orchestration

- Auto Loader on `raw_documents/`
- Lakeflow Job that wraps the existing OOXML pipeline
- `bronze_documents`, `silver_translation_runs` Delta tables populated by the job
- Per-paragraph progress audit events

### Phase 4 — Large-volume handling

- Pre-rendered HTML chunks in `silver_rendered_html`
- Windowed paragraph rendering in the dual_pane component
- Aggregated minimap above N paragraphs

### Phase 5 — Observability

- AI/BI dashboards
- Lakeflow alerts
- Runbook in Confluence/Notion

### Phase 6 — Multi-tenant + signatures (only when needed)

- Protocol/study sidecar JSON or intake form
- Two-eyes policy for certify
- Digital signatures (DocuSign or in-house) on `GOLD_PROMOTED`
- Merkle-chained `audit_events`

---

## 10. Open questions for the next round

1. **SQL warehouse for the SP** — using my `Agent Demo Warehouse` (id `68d156b1d8b5502b`) for phase 2. Production should likely use a dedicated, named warehouse owned by the platform team, not an individual.
2. **Sync cadence** — currently "at promotion only" (cheapest). If we want live in-flight visibility (e.g., for ops dashboards showing how many docs are mid-review), switch to "every N minutes" via a separate Lakeflow Job that mirrors the active rows.
3. **Re-open workflow** — when an administrator needs to unlock a `PUBLISHED` document, what's the approval path? Inline button + comment? Out-of-band ticket?
4. **Golden retention** — currently kept indefinitely. Confirm whether 7 years is the right window or if there's a longer regulator-mandated horizon.
