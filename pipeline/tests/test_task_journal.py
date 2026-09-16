from pathlib import Path
from uuid import uuid4

import pytest
import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal


@pytest.fixture
def journal_database(monkeypatch):
    import os
    dsn = os.environ.get("TEST_AGENT_DATABASE_URL")
    if not dsn:
        pytest.skip("격리 PostgreSQL 테스트 URL이 지정되지 않았습니다.")
    schema = "test_task_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f"-csearch_path={schema}")
    try:
        with connect() as conn:
            conn.execute(Path(__file__).resolve().parents[1].joinpath("db/ai_schema.sql").read_text())
        monkeypatch.setattr(journal, "connect", connect)
        yield connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def command(run_id="run"):
    return dict(run_id=run_id, workspace_id="ws", user_id="user", kind="ingest")


def insert_page(conn, name="page"):
    conn.execute("INSERT INTO wiki_pages(id, page_type, title, slug, user_id, workspace_id, status, created_at, updated_at) "
                 "VALUES (%s, 'source', '원래 제목', %s, 'user', 'ws', 'active', now(), now())", (name, name))


def test_native_journal_rolls_back_all_completed_steps(journal_database):
    connect = journal_database
    with connect() as conn:
        insert_page(conn)
        before = conn.execute("SELECT to_jsonb(t) AS value FROM wiki_pages t").fetchone()["value"]
    journal.register(command())
    with connect() as conn:
        conn.execute("SELECT set_config('app.ai_task_run_id', 'run', true)")
        conn.execute("UPDATE wiki_pages SET title = '첫 번째 변경' WHERE id = 'page'")
    with connect() as conn:
        conn.execute("SELECT set_config('app.ai_task_run_id', 'run', true)")
        conn.execute("UPDATE wiki_pages SET title = '두 번째 변경' WHERE id = 'page'")
        conn.execute("INSERT INTO source_blocks VALUES ('doc', 'block', '새 블록')")
    journal.request_cancel("run", "ws", "user")
    journal.rollback("run")
    assert journal.status("run", "ws", "user")["status"] == "cancelled"
    with connect() as conn:
        assert conn.execute("SELECT to_jsonb(t) AS value FROM wiki_pages t").fetchone()["value"] == before
        assert conn.execute("SELECT * FROM source_blocks").fetchall() == []
    journal.rollback("run")


def test_native_journal_preserves_later_change(journal_database):
    connect = journal_database
    with connect() as conn:
        insert_page(conn)
    journal.register(command())
    with connect() as conn:
        conn.execute("SELECT set_config('app.ai_task_run_id', 'run', true)")
        conn.execute("UPDATE wiki_pages SET title = 'AI 변경' WHERE id = 'page'")
    with connect() as conn:
        conn.execute("UPDATE wiki_pages SET title = '다른 사용자 변경' WHERE id = 'page'")
    journal.request_cancel("run", "ws", "user")
    journal.rollback("run")
    assert journal.status("run", "ws", "user")["status"] == "rollback_failed"
    with connect() as conn:
        assert conn.execute("SELECT title FROM wiki_pages").fetchone()["title"] == "다른 사용자 변경"


def test_cancel_fences_new_changes_and_cascade_does_not_delete_others(journal_database):
    connect = journal_database
    journal.register(command())
    with connect() as conn:
        conn.execute("SELECT set_config('app.ai_task_run_id', 'run', true)")
        insert_page(conn)
    with connect() as conn:
        conn.execute("INSERT INTO document_wiki_links VALUES ('other-document', 'page', 'source_of', 1, 'ws', now())")
    journal.request_cancel("run", "ws", "user")
    with pytest.raises(psycopg.errors.QueryCanceled):
        with connect() as conn:
            conn.execute("SELECT set_config('app.ai_task_run_id', 'run', true)")
            conn.execute("UPDATE wiki_pages SET title = '늦은 결과' WHERE id = 'page'")
    journal.rollback("run")
    assert journal.status("run", "ws", "user")["status"] == "rollback_failed"
    with connect() as conn:
        assert len(conn.execute("SELECT * FROM document_wiki_links").fetchall()) == 1


def test_cancel_before_delivery_never_calls_handler(journal_database):
    from app.core.pipeline_control import PipelineRunCancelledError
    journal.register(command())
    journal.request_cancel("run", "ws", "user")
    def unexpected():
        pytest.fail("취소한 작업의 handler를 호출했습니다.")
    with pytest.raises(PipelineRunCancelledError):
        journal.execute(command(), unexpected)
    assert journal.status("run", "ws", "user")["status"] == "cancelled"


def test_active_worker_settles_before_rollback(journal_database):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app.core.pipeline_control import PipelineRunCancelledError
    started, release = Event(), Event()
    connect = journal_database
    def handle():
        with connect() as conn:
            conn.execute("SELECT set_config('app.ai_task_run_id', 'run', true)")
            insert_page(conn)
        started.set()
        assert release.wait(5)
        return {"late": True}
    with ThreadPoolExecutor(max_workers=1) as workers:
        forward = workers.submit(journal.execute, command(), handle)
        assert started.wait(5)
        journal.request_cancel("run", "ws", "user")
        journal.rollback_pending()
        assert journal.status("run", "ws", "user")["status"] == "cancel_requested"
        release.set()
        with pytest.raises(PipelineRunCancelledError):
            forward.result(5)
    assert journal.status("run", "ws", "user")["status"] == "cancelled"
    with connect() as conn:
        assert conn.execute("SELECT * FROM wiki_pages").fetchall() == []


def test_parent_restores_after_completed_child_and_rejects_late_child(journal_database):
    from app.core.pipeline_control import PipelineRunCancelledError
    connect = journal_database
    journal.register(command())
    with connect() as conn:
        conn.execute("SELECT set_config('app.ai_task_run_id', 'run', true)")
        insert_page(conn)
    child = dict(command("child"), ingest_run_id="run", kind="post_ingest")
    journal.register(child)
    with connect() as conn:
        conn.execute("SELECT set_config('app.ai_task_run_id', 'child', true)")
        conn.execute("UPDATE wiki_pages SET title = '후처리' WHERE id = 'page'")
    journal.complete("child", {})
    journal.request_cancel("run", "ws", "user")
    with pytest.raises(PipelineRunCancelledError):
        journal.register(dict(child, run_id="late-child"))
    journal.rollback_pending()
    assert journal.status("child", "ws", "user")["status"] == "cancelled"
    journal.rollback_pending()
    assert journal.status("run", "ws", "user")["status"] == "cancelled"
    with connect() as conn:
        assert conn.execute("SELECT * FROM wiki_pages").fetchall() == []


def test_object_response_loss_restores_original_and_conflict_remains_retryable(journal_database, monkeypatch):
    from io import BytesIO
    from minio.error import S3Error
    from app.modules.task_cancellation.infrastructure import object_change_journal as objects
    from app.modules.wiki_ingestion.infrastructure import object_storage
    class Storage:
        values = {("bucket", "key"): (b"original", "text/markdown")}
        lose_response = True
        def get_object(self, bucket, key):
            if (bucket, key) not in self.values:
                raise S3Error("NoSuchKey", "missing", "", "", "", None)
            value, content_type = self.values[bucket, key]
            response = BytesIO(value)
            response.headers = {"Content-Type": content_type}
            response.release_conn = lambda: None
            return response
        def put_object(self, bucket, key, stream, length, content_type):
            self.values[bucket, key] = (stream.read(), content_type)
            if self.lose_response:
                self.lose_response = False
                raise OSError("응답 유실")
        def remove_object(self, bucket, key):
            self.values.pop((bucket, key), None)
    storage = Storage()
    monkeypatch.setattr(object_storage, "client", lambda: storage)
    journal.register(command())
    with pytest.raises(OSError):
        objects.change_object("run", storage, "bucket", "key", {"text": "AI", "content_type": "text/markdown"})
    storage.values["bucket", "key"] = (b"other", "text/markdown")
    journal.request_cancel("run", "ws", "user")
    journal.rollback("run")
    assert journal.status("run", "ws", "user")["status"] == "rollback_failed"
    assert storage.values["bucket", "key"][0] == b"other"
    storage.values["bucket", "key"] = (b"AI", "text/markdown")
    journal.request_cancel("run", "ws", "user")
    journal.rollback("run")
    assert journal.status("run", "ws", "user")["status"] == "cancelled"
    assert storage.values["bucket", "key"][0] == b"original"


@pytest.mark.parametrize("updating", [False, True])
def test_skill_publication_restores_exact_rows(journal_database, updating):
    connect = journal_database
    def publish(conn, version):
        conn.execute("INSERT INTO skill_versions(id, skill_id, version, name, description, instructions_markdown, status, created_by) "
                     "VALUES (%s, 'skill', %s, '정리', '주제별 정리', '문서를 정리한다', 'published', 'user')", (f"v{version}", version))
        conn.execute("UPDATE skills SET enabled_version_id = %s, updated_at = now() WHERE id = 'skill'", (f"v{version}",))
    with connect() as conn:
        if updating:
            conn.execute("INSERT INTO skills(id, scope_type, owner_user_id, command, status) VALUES ('skill', 'personal', 'user', 'organize', 'enabled')")
            publish(conn, 1)
        before = {table: conn.execute(sql.SQL("SELECT to_jsonb(t) AS value FROM {} t ORDER BY id").format(sql.Identifier(table))).fetchall()
                  for table in ("skills", "skill_versions")}
    journal.register(dict(command(), kind="skill_update" if updating else "skill_publish"))
    with connect() as conn:
        conn.execute("SELECT set_config('app.ai_task_run_id', 'run', true)")
        if not updating:
            conn.execute("INSERT INTO skills(id, scope_type, owner_user_id, command, status) VALUES ('skill', 'personal', 'user', 'organize', 'enabled')")
        publish(conn, 2 if updating else 1)
    journal.request_cancel("run", "ws", "user")
    journal.rollback("run")
    assert journal.status("run", "ws", "user")["status"] == "cancelled"
    with connect() as conn:
        for table, expected in before.items():
            assert conn.execute(sql.SQL("SELECT to_jsonb(t) AS value FROM {} t ORDER BY id").format(sql.Identifier(table))).fetchall() == expected


def test_backend_rollback_applies_restored_revision_before_late_event(journal_database, monkeypatch):
    import httpx
    from app.modules.task_cancellation.infrastructure import backend_rollback
    from app.workers import edit_event_consumer
    connect = journal_database
    monkeypatch.setattr(backend_rollback.database, "connect_ai", connect)
    monkeypatch.setenv("AGENT_INTERNAL_TOKEN", "test-token")
    calls = []
    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/changes"):
            return httpx.Response(200, json=[2, 1])
        if request.url.path.endswith("/finalize-edits"):
            return httpx.Response(200, json=[dict(document_id="doc", workspace_id="ws", revision=3,
                                                content_hash="original", created_at="2026-09-09T00:00:00Z")])
        return httpx.Response(200)
    real_client = httpx.Client
    monkeypatch.setattr(backend_rollback.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    backend_rollback.rollback_backend("run", "ws", "user")
    assert [path.rsplit("/", 1)[1] for path in calls] == ["changes", "2", "1", "finalize-edits"]
    edit_event_consumer._handle(b'{"document_id":"doc","workspace_id":"ws","revision":2,"content_hash":"AI result","created_at":"2026-09-09T00:00:00Z"}')
    with connect() as conn:
        row = conn.execute("SELECT last_edit_revision, last_edit_hash FROM document_derived_state WHERE document_id = 'doc'").fetchone()
        assert row == dict(last_edit_revision=3, last_edit_hash="original")



def test_cancel_snapshot_before_dispatch_finishes_persistent_undo(journal_database, monkeypatch):
    from dataclasses import replace
    from unittest.mock import MagicMock
    from app.modules.agent_run.infrastructure.postgres_agent_run_repository import PostgresAgentRunRepository
    from app.modules.agent_run.infrastructure.postgres_agent_job_repository import PostgresAgentJobRepository
    from app.modules.agent_run.application.rollback_agent_run import RollbackAgentRunUseCase
    from tests.modules.agent_run.test_agent_rollback import Journal, organization
    from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database
    monkeypatch.setattr(database, "connect_ai", journal_database)
    runs, jobs = PostgresAgentRunRepository(), PostgresAgentJobRepository()
    plan = organization()
    run = runs.create_with_planning_job(replace(Journal(plan).run, status="executing"), "planning")
    runs.save_plan(run.id, plan)
    with journal_database() as conn:
        conn.execute("UPDATE agent_runs SET status = 'executing' WHERE id = %s", (run.id,))
    assert jobs.prepare_undo(run.id, plan.id, plan.operations[0].id, "create_folder", {}, {})
    runs.cancel("ws", "user", run.id)
    gateway = MagicMock()
    RollbackAgentRunUseCase(jobs, runs, gateway).execute(run.id)
    assert runs.get_for_user("ws", "user", run.id).status == "cancelled"
    assert jobs.get_undo_record(run.id, plan.operations[0].id)["status"] == "undo_done"
    gateway.execute.assert_not_called()


@pytest.mark.parametrize("fail_recovered", [False, True])
def test_last_rollback_lease_is_reclaimed_and_failure_can_be_retried(journal_database, monkeypatch, fail_recovered):
    from unittest.mock import MagicMock
    from app.modules.agent_run.infrastructure.agent_worker import AgentWorker
    from app.modules.agent_run.infrastructure.postgres_agent_job_repository import PostgresAgentJobRepository
    from app.modules.agent_run.infrastructure.postgres_agent_run_repository import PostgresAgentRunRepository, register_cancelled_turn
    from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database

    connect = journal_database
    monkeypatch.setattr(database, "connect_ai", connect)
    runs, jobs = PostgresAgentRunRepository(), PostgresAgentJobRepository()
    register_cancelled_turn(dict(command("turn"), kind="agent", message="정리해 줘"))
    runs.cancel("ws", "user", "turn")
    for attempt in range(1, 4):
        job = jobs.claim_next("worker")
        assert job.attempt_count == attempt
        if attempt < 3:
            jobs.fail(job, "setup_unavailable")
            with connect() as conn:
                conn.execute("UPDATE agent_jobs SET available_at = now() WHERE id = %s", (job.id,))
    jobs.mark_run_status("turn", ("cancel_requested",), "rolling_back")
    assert jobs.claim_next("replacement") is None  # 유효한 lease는 회수하지 않는다.
    with connect() as conn:
        conn.execute("UPDATE agent_jobs SET leased_until = now() - interval '1 second' WHERE id = %s", (job.id,))
    recovered = jobs.claim_next("replacement")
    assert recovered is not None and recovered.id == job.id
    assert recovered.attempt_count == 4
    assert not jobs.heartbeat(job)  # 이전 소유자는 회수된 lease를 연장할 수 없다.
    if fail_recovered:
        jobs.fail(recovered, "database_unavailable")
        assert runs.get_for_user("ws", "user", "turn").status == "rollback_failed"
        runs.cancel("ws", "user", "turn")
        recovered = jobs.claim_next("retry-worker")
        assert recovered is not None and recovered.id != job.id
    AgentWorker(jobs, runs, MagicMock(), None).process(recovered)
    assert runs.get_for_user("ws", "user", "turn").status == "cancelled"
    assert jobs.claim_next("replacement") is None
