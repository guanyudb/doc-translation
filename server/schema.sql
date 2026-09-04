-- Canonical Lakebase (Postgres) schema for the Doc Translation Review platform.
--
-- SINGLE SOURCE OF TRUTH — read by BOTH server/store.py (`ensure_schema`) and
-- setup/postdeploy.py. Previously the DDL was duplicated in those two files and
-- drifted; keep all schema changes here only.
--
-- Idempotent: `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ... ADD COLUMN IF NOT
-- EXISTS`, so this both creates a fresh schema and migrates an existing one.
-- `{schema}` is substituted (str.replace) with the target Postgres schema name.
-- Executed as a single multi-statement command, so keep every statement `;`-terminated.

CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.review_pairs (
    pair_id          TEXT PRIMARY KEY,
    original_path    TEXT NOT NULL,
    translated_path  TEXT NOT NULL,
    source_lang      TEXT,
    target_lang      TEXT,
    total_paragraphs INT,
    created_at       TIMESTAMPTZ DEFAULT now(),
    finalized_at     TIMESTAMPTZ,
    lifecycle_state  TEXT NOT NULL DEFAULT 'UNDER_REVIEW'
                      CHECK (lifecycle_state IN ('UNDER_REVIEW','PROMOTING','PUBLISHED','ARCHIVED')),
    locked_at        TIMESTAMPTZ,
    original_hash    TEXT,
    translated_hash  TEXT
);

CREATE TABLE IF NOT EXISTS {schema}.review_feedback (
    pair_id        TEXT REFERENCES {schema}.review_pairs(pair_id) ON DELETE CASCADE,
    paragraph_idx  INT  NOT NULL,
    status         TEXT DEFAULT 'pending' CHECK (status IN ('pending','certified','flagged')),
    comment        TEXT,
    reviewer       TEXT,
    updated_at     TIMESTAMPTZ DEFAULT now(),
    edited_text    TEXT,
    last_published_text TEXT,
    PRIMARY KEY (pair_id, paragraph_idx)
);
CREATE INDEX IF NOT EXISTS review_feedback_status_idx ON {schema}.review_feedback (pair_id, status);

CREATE TABLE IF NOT EXISTS {schema}.review_edit_history (
    edit_id        BIGSERIAL PRIMARY KEY,
    pair_id        TEXT NOT NULL REFERENCES {schema}.review_pairs(pair_id) ON DELETE CASCADE,
    paragraph_idx  INT  NOT NULL,
    previous_text  TEXT,
    new_text       TEXT,
    reviewer       TEXT,
    edited_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS review_edit_history_pair_idx
    ON {schema}.review_edit_history (pair_id, paragraph_idx, edited_at DESC);

CREATE TABLE IF NOT EXISTS {schema}.review_publish_log (
    publish_id      BIGSERIAL PRIMARY KEY,
    pair_id         TEXT NOT NULL REFERENCES {schema}.review_pairs(pair_id) ON DELETE CASCADE,
    output_path     TEXT NOT NULL,
    edits_applied   INT  NOT NULL DEFAULT 0,
    published_by    TEXT,
    published_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS review_publish_log_pair_idx
    ON {schema}.review_publish_log (pair_id, published_at DESC);

CREATE TABLE IF NOT EXISTS {schema}.audit_events (
    event_id        BIGSERIAL PRIMARY KEY,
    pair_id         TEXT,
    event_type      TEXT NOT NULL,
    actor           TEXT NOT NULL,
    actor_type      TEXT NOT NULL DEFAULT 'human',
    paragraph_idx   INT,
    before_value    JSONB,
    after_value     JSONB,
    correlation_id  TEXT,
    client_ip       TEXT,
    client_ua       TEXT,
    event_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_events_pair_idx  ON {schema}.audit_events (pair_id, event_at DESC);
CREATE INDEX IF NOT EXISTS audit_events_type_idx  ON {schema}.audit_events (event_type, event_at DESC);
CREATE INDEX IF NOT EXISTS audit_events_actor_idx ON {schema}.audit_events (actor, event_at DESC);

CREATE TABLE IF NOT EXISTS {schema}.golden_publications (
    publication_id          BIGSERIAL PRIMARY KEY,
    pair_id                 TEXT NOT NULL UNIQUE REFERENCES {schema}.review_pairs(pair_id),
    golden_original_path    TEXT NOT NULL,
    golden_translated_path  TEXT NOT NULL,
    golden_original_hash    TEXT NOT NULL,
    golden_translated_hash  TEXT NOT NULL,
    total_paragraphs        INT  NOT NULL,
    certified_paragraphs    INT  NOT NULL,
    edits_applied           INT  NOT NULL DEFAULT 0,
    distinct_reviewers      JSONB NOT NULL DEFAULT '[]'::jsonb,
    published_by            TEXT NOT NULL,
    published_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    delta_synced_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS golden_publications_published_idx
    ON {schema}.golden_publications (published_at DESC);

CREATE TABLE IF NOT EXISTS {schema}.paragraph_confidence (
    pair_id          TEXT NOT NULL REFERENCES {schema}.review_pairs(pair_id) ON DELETE CASCADE,
    paragraph_idx    INT  NOT NULL,
    length_ratio     REAL,
    untranslated_pct REAL,
    repeated_ngrams  INT,
    confidence       REAL NOT NULL,
    computed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_lang      TEXT,
    target_lang      TEXT,
    PRIMARY KEY (pair_id, paragraph_idx)
);
CREATE INDEX IF NOT EXISTS paragraph_confidence_score_idx
    ON {schema}.paragraph_confidence (pair_id, confidence);

CREATE TABLE IF NOT EXISTS {schema}.translation_glossary (
    entry_id            BIGSERIAL PRIMARY KEY,
    source_lang         TEXT,
    target_lang         TEXT,
    model_phrase        TEXT NOT NULL,
    correction          TEXT NOT NULL,
    occurrences         INT  NOT NULL DEFAULT 1,
    distinct_reviewers  INT  NOT NULL DEFAULT 1,
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved            BOOLEAN NOT NULL DEFAULT TRUE,
    source              TEXT NOT NULL DEFAULT 'tenant',
    list_name           TEXT,
    UNIQUE (source_lang, target_lang, model_phrase, correction)
);
CREATE INDEX IF NOT EXISTS translation_glossary_lookup_idx
    ON {schema}.translation_glossary (source_lang, target_lang, approved, occurrences DESC);

CREATE TABLE IF NOT EXISTS {schema}.translation_prompts (
    prompt_id    BIGSERIAL PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    body         TEXT NOT NULL,
    description  TEXT,
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by   TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Migrations for schemas created before a column/constraint existed. All
-- idempotent, so they are safe to run on every deploy.
-- ---------------------------------------------------------------------------
ALTER TABLE {schema}.review_feedback ADD COLUMN IF NOT EXISTS edited_text TEXT;
ALTER TABLE {schema}.review_feedback ADD COLUMN IF NOT EXISTS last_published_text TEXT;

ALTER TABLE {schema}.review_pairs ADD COLUMN IF NOT EXISTS lifecycle_state TEXT NOT NULL DEFAULT 'UNDER_REVIEW';
ALTER TABLE {schema}.review_pairs ADD COLUMN IF NOT EXISTS original_hash TEXT;
ALTER TABLE {schema}.review_pairs ADD COLUMN IF NOT EXISTS translated_hash TEXT;
ALTER TABLE {schema}.review_pairs ADD COLUMN IF NOT EXISTS source_lang TEXT;
ALTER TABLE {schema}.review_pairs ADD COLUMN IF NOT EXISTS locked_at TIMESTAMPTZ;
DO $do$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'review_pairs_lifecycle_chk') THEN
        ALTER TABLE {schema}.review_pairs
            ADD CONSTRAINT review_pairs_lifecycle_chk
            CHECK (lifecycle_state IN ('UNDER_REVIEW','PROMOTING','PUBLISHED','ARCHIVED'));
    END IF;
END $do$;

ALTER TABLE {schema}.translation_glossary ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'tenant';
ALTER TABLE {schema}.translation_glossary ADD COLUMN IF NOT EXISTS list_name TEXT;
UPDATE {schema}.translation_glossary
    SET list_name = CASE source
        WHEN 'seed'   THEN 'Seed — ICH clinical'
        WHEN 'tenant' THEN 'Mined from reviews'
        ELSE 'Imported'
    END
    WHERE list_name IS NULL OR list_name = '';
