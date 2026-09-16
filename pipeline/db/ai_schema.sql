-- ai_db 스키마 (python/ai-svc 소유).
-- Flyway가 아닌 pipeline 부트스트랩(AI_DB_MIGRATION_URL)이 적용하며, 전부 멱등(IF NOT EXISTS)이다.

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id uuid PRIMARY KEY,
    document_id varchar(255),
    user_id varchar(255),
    workspace_id varchar(255),
    input_source text NOT NULL,
    output_dir text NOT NULL,
    mode text NOT NULL,
    status text NOT NULL,
    manifest jsonb,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS user_id varchar(255);
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS workspace_id varchar(255);
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_document
    ON pipeline_runs (document_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_workspace_status
    ON pipeline_runs (workspace_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS wiki_pages (
    id varchar(255) PRIMARY KEY,
    page_type varchar(255) NOT NULL CHECK (page_type IN ('source', 'concept')),
    title text NOT NULL,
    slug varchar(255) NOT NULL,
    summary text,
    markdown_uri varchar(255),
    user_id varchar(255) NOT NULL,
    workspace_id varchar(255) NOT NULL,
    status varchar(255) NOT NULL CHECK (status IN ('draft', 'active', 'failed', 'deleted')),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT uq_wiki_pages_workspace_type_slug
        UNIQUE (user_id, workspace_id, page_type, slug)
);

CREATE INDEX IF NOT EXISTS idx_wiki_pages_workspace
    ON wiki_pages (workspace_id, status, updated_at DESC);

ALTER TABLE wiki_pages ALTER COLUMN title TYPE text;

CREATE TABLE IF NOT EXISTS document_wiki_links (
    document_id varchar(255) NOT NULL,
    wiki_page_id varchar(255) NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
    relation_type varchar(255) NOT NULL CHECK (relation_type IN ('source_of', 'extracted_concept')),
    confidence double precision,
    workspace_id varchar(255) NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (document_id, relation_type, wiki_page_id)
);

CREATE INDEX IF NOT EXISTS idx_document_wiki_links_workspace
    ON document_wiki_links (workspace_id);

CREATE TABLE IF NOT EXISTS wiki_page_links (
    from_page_id varchar(255) NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
    to_page_id varchar(255) NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
    link_type varchar(255) NOT NULL,
    label varchar(255),
    confidence double precision,
    workspace_id varchar(255) NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (from_page_id, link_type, to_page_id)
);

CREATE INDEX IF NOT EXISTS idx_wiki_page_links_workspace
    ON wiki_page_links (workspace_id);

CREATE TABLE IF NOT EXISTS source_blocks (
    document_id varchar(255) NOT NULL,
    block_id varchar(255) NOT NULL,
    text text NOT NULL,
    PRIMARY KEY (block_id, document_id)
);

CREATE TABLE IF NOT EXISTS wiki_page_embeddings (
    page_id varchar(255) NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
    embedding_model text NOT NULL,
    representation_hash text NOT NULL,
    embedding_vector double precision[] NOT NULL,
    embedding_dimension integer NOT NULL,
    status text NOT NULL DEFAULT 'completed',
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (page_id, embedding_model)
);

CREATE INDEX IF NOT EXISTS idx_wiki_page_embeddings_model_hash
    ON wiki_page_embeddings (embedding_model, representation_hash);

CREATE TABLE IF NOT EXISTS wiki_embedding_vectors (
    id text PRIMARY KEY,
    embedding_model text NOT NULL,
    representation_hash text NOT NULL,
    representation_text text NOT NULL,
    embedding_vector double precision[],
    embedding_dimension integer NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'pending',
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (embedding_model, representation_hash)
);

CREATE TABLE IF NOT EXISTS wiki_embedding_units (
    id text PRIMARY KEY,
    embedding_vector_id text NOT NULL REFERENCES wiki_embedding_vectors(id) ON DELETE RESTRICT,
    page_id varchar(255) NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
    source_document_id varchar(255) NOT NULL,
    unit_type text NOT NULL,
    block_refs text[] NOT NULL DEFAULT ARRAY[]::text[],
    text text NOT NULL,
    weight double precision NOT NULL DEFAULT 1.0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wiki_embedding_units_page
    ON wiki_embedding_units (page_id);

CREATE INDEX IF NOT EXISTS idx_wiki_embedding_units_vector
    ON wiki_embedding_units (embedding_vector_id);

-- wiki_schemas: core_db V4__add_pipeline_schema.sql에서 ai_db로 이전한 테이블 (형상 동일)
CREATE TABLE IF NOT EXISTS wiki_schemas (
    id text PRIMARY KEY,
    workspace_id text NOT NULL,
    user_id text NOT NULL,
    name text NOT NULL,
    raw_markdown text NOT NULL,
    sanitized_global_markdown text NOT NULL DEFAULT '',
    sanitized_query_markdown text NOT NULL DEFAULT '',
    sanitized_ingest_markdown text NOT NULL DEFAULT '',
    sanitized_edit_markdown text NOT NULL DEFAULT '',
    sanitized_concept_markdown text NOT NULL DEFAULT '',
    sanitized_template_markdown text NOT NULL DEFAULT '',
    preview_markdown text NOT NULL DEFAULT '',
    lint_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL,
    schema_version text NOT NULL DEFAULT '1.0',
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    activated_at timestamp with time zone
);

CREATE INDEX IF NOT EXISTS idx_wiki_schemas_workspace_user_status
    ON wiki_schemas (workspace_id, user_id, status);

CREATE UNIQUE INDEX IF NOT EXISTS uq_wiki_schemas_one_active_per_workspace_user
    ON wiki_schemas (workspace_id, user_id)
    WHERE status = 'active';

-- document_derived_state: document.edit.event 기반 파생물 stale 추적.
-- stale 여부는 컬럼으로 저장하지 않고 조회 시
-- `ingested_hash IS DISTINCT FROM last_edit_hash`로 계산한다.
CREATE TABLE IF NOT EXISTS document_derived_state (
    document_id text PRIMARY KEY,
    workspace_id text NOT NULL,
    last_edit_revision bigint NOT NULL,
    last_edit_hash varchar(64) NOT NULL,
    last_edited_at timestamptz NOT NULL,
    ingested_hash varchar(64),
    last_ingested_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_document_derived_state_workspace
    ON document_derived_state (workspace_id);

CREATE TABLE IF NOT EXISTS skills (
    id text PRIMARY KEY,
    workspace_id text,
    scope_type text NOT NULL,
    owner_user_id text,
    command varchar(63) NOT NULL,
    status text NOT NULL,
    enabled_version_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT skills_scope_check CHECK (
        (scope_type = 'personal' AND workspace_id IS NULL AND owner_user_id IS NOT NULL)
        OR (scope_type = 'team' AND workspace_id IS NOT NULL AND owner_user_id IS NULL)
    ),
    CONSTRAINT skills_status_check CHECK (status IN ('enabled', 'disabled'))
);

CREATE TABLE IF NOT EXISTS skill_versions (
    id text PRIMARY KEY,
    skill_id text NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    version integer NOT NULL,
    name varchar(63) NOT NULL,
    description text NOT NULL,
    instructions_markdown text NOT NULL,
    capabilities text[] NOT NULL DEFAULT ARRAY[]::text[],
    allowed_tools text[] NOT NULL DEFAULT ARRAY[]::text[],
    safety_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    CONSTRAINT skill_versions_version_check CHECK (version > 0),
    CONSTRAINT skill_versions_status_check CHECK (status IN ('draft', 'published', 'rejected')),
    CONSTRAINT skill_versions_unique_version UNIQUE (skill_id, version)
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'skills_enabled_version_fk') THEN
        ALTER TABLE skills ADD CONSTRAINT skills_enabled_version_fk
            FOREIGN KEY (enabled_version_id) REFERENCES skill_versions(id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS agent_runs (
    id text PRIMARY KEY,
    workspace_id text NOT NULL,
    user_id text NOT NULL,
    action text NOT NULL,
    skill_version_id text REFERENCES skill_versions(id) ON DELETE SET NULL,
    status text NOT NULL,
    request_summary text NOT NULL,
    provider text,
    model text,
    current_plan_id text,
    error_code text,
    tool_call_count integer NOT NULL DEFAULT 0,
    document_id text,
    base_version bigint,
    apply_operation_id text,
    apply_consumed_at timestamptz,
    result jsonb,
    command_envelope_hash varchar(64),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    CONSTRAINT agent_runs_tool_call_count_check CHECK (tool_call_count BETWEEN 0 AND 40)
);

ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS command_envelope_hash varchar(64);
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS provider text;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS model text;

UPDATE agent_runs
SET model = 'claude-sonnet-5', updated_at = now()
WHERE provider = 'claude'
  AND model = 'claude-haiku-4-5-20251001'
  AND status NOT IN ('completed', 'partial_failed', 'failed', 'conflicted', 'rejected', 'cancelled');

CREATE TABLE IF NOT EXISTS skill_version_sources (
    id text PRIMARY KEY,
    skill_version_id text NOT NULL REFERENCES skill_versions(id) ON DELETE CASCADE,
    source_agent_run_id text REFERENCES agent_runs(id) ON DELETE SET NULL,
    source_turn_id text,
    source_type text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_plans (
    id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    version integer NOT NULL,
    summary text NOT NULL,
    operation_hash text NOT NULL,
    status text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent_plans_version_check CHECK (version > 0),
    CONSTRAINT agent_plans_unique_version UNIQUE (run_id, version)
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agent_runs_current_plan_fk') THEN
        ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_current_plan_fk
            FOREIGN KEY (current_plan_id) REFERENCES agent_plans(id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS agent_plan_operations (
    id text PRIMARY KEY,
    plan_id text NOT NULL REFERENCES agent_plans(id) ON DELETE CASCADE,
    sequence integer NOT NULL,
    tool_name text NOT NULL,
    target_type text NOT NULL,
    target_id text,
    base_version bigint,
    source_parent_id text,
    destination_parent_id text,
    arguments jsonb NOT NULL DEFAULT '{}'::jsonb,
    reason text NOT NULL,
    depends_on text[] NOT NULL DEFAULT ARRAY[]::text[],
    status text NOT NULL,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent_plan_operations_unique_sequence UNIQUE (plan_id, sequence)
);

CREATE TABLE IF NOT EXISTS agent_approvals (
    id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    plan_id text NOT NULL REFERENCES agent_plans(id) ON DELETE CASCADE,
    plan_version integer NOT NULL,
    operation_hash text NOT NULL,
    user_id text NOT NULL,
    decision text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_jobs (
    id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    job_type text NOT NULL,
    status text NOT NULL,
    attempt_count integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text,
    lease_token text,
    leased_until timestamptz,
    heartbeat_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent_jobs_attempt_count_check CHECK (attempt_count >= 0)
);

CREATE TABLE IF NOT EXISTS agent_tool_executions (
    id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    plan_id text NOT NULL REFERENCES agent_plans(id) ON DELETE CASCADE,
    operation_id text NOT NULL REFERENCES agent_plan_operations(id) ON DELETE CASCADE,
    tool_name text NOT NULL,
    idempotency_key text NOT NULL,
    attempt integer NOT NULL,
    status text NOT NULL,
    response_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_code text,
    finished_at timestamptz,
    CONSTRAINT agent_tool_executions_unique_idempotency UNIQUE (idempotency_key)
);

CREATE TABLE IF NOT EXISTS agent_run_artifacts (
    id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    workspace_id text NOT NULL,
    user_id text NOT NULL,
    content_hash text NOT NULL,
    purpose text NOT NULL,
    object_key text,
    document_id text,
    base_version bigint,
    target jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz
);

ALTER TABLE agent_run_artifacts
    ADD COLUMN IF NOT EXISTS object_key text;

CREATE UNIQUE INDEX IF NOT EXISTS uq_skills_personal_command
    ON skills (owner_user_id, command) WHERE scope_type = 'personal';
CREATE UNIQUE INDEX IF NOT EXISTS uq_skills_team_command
    ON skills (workspace_id, command) WHERE scope_type = 'team';
CREATE INDEX IF NOT EXISTS idx_skill_versions_skill_status
    ON skill_versions (skill_id, status, version DESC);
CREATE INDEX IF NOT EXISTS idx_skill_version_sources_run
    ON skill_version_sources (source_agent_run_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_actor_status
    ON agent_runs (workspace_id, user_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_claim
    ON agent_jobs (status, available_at, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_run
    ON agent_jobs (run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_plan_operations_plan_status
    ON agent_plan_operations (plan_id, status, sequence);
CREATE INDEX IF NOT EXISTS idx_agent_tool_executions_run
    ON agent_tool_executions (run_id, plan_id);
CREATE INDEX IF NOT EXISTS idx_agent_run_artifacts_actor
    ON agent_run_artifacts (workspace_id, user_id, run_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_runs_apply_operation
    ON agent_runs (apply_operation_id) WHERE apply_operation_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS checkpoint_migrations (v integer PRIMARY KEY);

CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id text NOT NULL,
    checkpoint_ns text NOT NULL DEFAULT '',
    checkpoint_id text NOT NULL,
    parent_checkpoint_id text,
    type text,
    checkpoint jsonb NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    thread_id text NOT NULL,
    checkpoint_ns text NOT NULL DEFAULT '',
    channel text NOT NULL,
    version text NOT NULL,
    type text NOT NULL,
    blob bytea,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);

CREATE TABLE IF NOT EXISTS checkpoint_writes (
    thread_id text NOT NULL,
    checkpoint_ns text NOT NULL DEFAULT '',
    checkpoint_id text NOT NULL,
    task_id text NOT NULL,
    idx integer NOT NULL,
    channel text NOT NULL,
    type text,
    blob bytea NOT NULL,
    task_path text NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

INSERT INTO checkpoint_migrations (v) VALUES (9) ON CONFLICT (v) DO NOTHING;
CREATE INDEX IF NOT EXISTS checkpoints_thread_id_idx ON checkpoints (thread_id);
CREATE INDEX IF NOT EXISTS checkpoint_blobs_thread_id_idx ON checkpoint_blobs (thread_id);
CREATE INDEX IF NOT EXISTS checkpoint_writes_thread_id_idx ON checkpoint_writes (thread_id);

-- 실행 중인 AI 작업의 변경과 복구 기록은 같은 트랜잭션에서 남긴다.
CREATE TABLE IF NOT EXISTS ai_task_runs (
    id text PRIMARY KEY,
    workspace_id text NOT NULL,
    user_id text NOT NULL,
    kind text NOT NULL,
    parent_run_id text REFERENCES ai_task_runs(id),
    command_hash text,
    command jsonb,
    status text NOT NULL DEFAULT 'running',
    result jsonb,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE ai_task_runs ADD COLUMN IF NOT EXISTS command jsonb;
CREATE INDEX IF NOT EXISTS idx_ai_task_runs_parent ON ai_task_runs(parent_run_id);
CREATE TABLE IF NOT EXISTS ai_task_changes (
    id bigserial PRIMARY KEY,
    run_id text NOT NULL REFERENCES ai_task_runs(id),
    table_name text NOT NULL,
    row_key jsonb NOT NULL,
    before_value jsonb,
    after_value jsonb,
    applied boolean NOT NULL DEFAULT true,
    undone boolean NOT NULL DEFAULT false
);
ALTER TABLE ai_task_changes ADD COLUMN IF NOT EXISTS applied boolean NOT NULL DEFAULT true;
CREATE INDEX IF NOT EXISTS idx_ai_task_changes_run ON ai_task_changes(run_id, id DESC);

CREATE OR REPLACE FUNCTION record_ai_task_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    task_id text := nullif(current_setting('app.ai_task_run_id', true), '');
    task_workspace text;
    previous jsonb;
    following jsonb;
    identity jsonb;
BEGIN
    IF task_id IS NULL THEN RETURN NULL; END IF;
    SELECT workspace_id INTO task_workspace FROM ai_task_runs
      WHERE id = task_id AND status = 'running';
    IF NOT FOUND THEN RAISE EXCEPTION 'AI task no longer accepts changes' USING ERRCODE = '57014'; END IF;
    IF TG_OP <> 'INSERT' THEN previous := to_jsonb(OLD); END IF;
    IF TG_OP <> 'DELETE' THEN following := to_jsonb(NEW); END IF;
    IF previous IS NOT DISTINCT FROM following THEN RETURN NULL; END IF;
    IF COALESCE(following, previous)->>'workspace_id' IS NOT NULL
       AND COALESCE(following, previous)->>'workspace_id' <> task_workspace THEN
        RAISE EXCEPTION 'AI task workspace mismatch';
    END IF;
    SELECT jsonb_object_agg(key, COALESCE(following, previous)->key) INTO identity
      FROM unnest(string_to_array(TG_ARGV[0], ',')) AS key;
    IF previous IS NOT NULL AND following IS NOT NULL AND EXISTS (
        SELECT 1 FROM jsonb_object_keys(identity) AS key WHERE previous->key IS DISTINCT FROM following->key
    ) THEN RAISE EXCEPTION 'AI task cannot change a primary key'; END IF;
    INSERT INTO ai_task_changes(run_id, table_name, row_key, before_value, after_value)
      VALUES(task_id, TG_TABLE_NAME, identity, previous, following);
    RETURN NULL;
END $$;

DO $$
DECLARE item text[];
BEGIN
    FOREACH item SLICE 1 IN ARRAY ARRAY[
        ['pipeline_runs','id'], ['wiki_pages','id'], ['document_wiki_links','document_id,relation_type,wiki_page_id'],
        ['wiki_page_links','from_page_id,link_type,to_page_id'], ['source_blocks','block_id,document_id'],
        ['wiki_page_embeddings','page_id,embedding_model'], ['wiki_embedding_vectors','id'],
        ['wiki_embedding_units','id'], ['wiki_schemas','id'], ['document_derived_state','document_id']
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS ai_task_change ON %I', item[1]);
        EXECUTE format('CREATE TRIGGER ai_task_change AFTER INSERT OR UPDATE OR DELETE ON %I '
                       'FOR EACH ROW EXECUTE FUNCTION record_ai_task_change(%L)', item[1], item[2]);
    END LOOP;
END $$;

DROP TRIGGER IF EXISTS ai_task_change ON skills;
CREATE TRIGGER ai_task_change AFTER INSERT OR UPDATE OR DELETE ON skills FOR EACH ROW EXECUTE FUNCTION record_ai_task_change('id');
DROP TRIGGER IF EXISTS ai_task_change ON skill_versions;
CREATE TRIGGER ai_task_change AFTER INSERT OR UPDATE OR DELETE ON skill_versions FOR EACH ROW EXECUTE FUNCTION record_ai_task_change('id');
DROP TRIGGER IF EXISTS ai_task_change ON skill_version_sources;
CREATE TRIGGER ai_task_change AFTER INSERT OR UPDATE OR DELETE ON skill_version_sources FOR EACH ROW EXECUTE FUNCTION record_ai_task_change('id');
