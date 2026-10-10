import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.rows import dict_row

from app.core.pipeline_control import ScopePurgedError
from app.modules.data_purge.infrastructure import postgres_purge as purge
from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal
from app.modules.wiki_ingestion.infrastructure import object_storage

BUCKET = "fruition-storage"


class FakeStorage:
    def __init__(self, keys):
        self.objects = set(keys)

    def list_objects(self, bucket, prefix, recursive):
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        if recursive:
            return [SimpleNamespace(object_name=key, is_dir=False) for key in keys]
        # delimiter="/" 동작: prefix 바로 아래 하위 경로는 "…/" 디렉터리 항목 하나로 묶는다.
        dirs = {prefix + key[len(prefix):].split("/", 1)[0] + "/" for key in keys if "/" in key[len(prefix):]}
        return [SimpleNamespace(object_name=name, is_dir=True) for name in sorted(dirs)]

    def remove_object(self, bucket, key):
        assert bucket == BUCKET
        self.objects.discard(key)


@pytest.fixture
def database(monkeypatch):
    dsn = os.environ.get("TEST_AGENT_DATABASE_URL")
    if not dsn:
        pytest.skip("격리 PostgreSQL 테스트 URL이 지정되지 않았습니다.")
    schema = "test_purge_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f"-csearch_path={schema}")

    try:
        with connect() as conn:
            conn.execute(Path(__file__).resolve().parents[3].joinpath("db/ai_schema.sql").read_text())
        monkeypatch.setattr(purge, "connect", connect)
        monkeypatch.setattr(journal, "connect", connect)
        yield connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def storage(monkeypatch):
    fake = FakeStorage(
        {
            "wiki/alice/ws/sources/doc.md",
            "wiki/bob/ws/concepts/topic.md",
            "wiki/ws/pages/page-ws/ops/1.json",
            "wiki/alice/other/sources/keep.md",
            "agent-runs/a/artifacts/alice-ws.md",
            "agent-runs/b/artifacts/alice-other.md",
            "agent-runs/c/artifacts/bob-other.md",
        }
        | {f"pipeline-runs/{RUN_WS}/pipeline.log", f"pipeline-runs/{RUN_OTHER}/pipeline.log"}
        | {"wiki/carol/ws/lint/report.md"}
    )
    monkeypatch.setattr(object_storage, "client", lambda: fake)
    monkeypatch.setenv("S3_BUCKET", BUCKET)
    return fake


RUN_WS = "00000000-0000-0000-0000-000000000001"
RUN_OTHER = "00000000-0000-0000-0000-000000000002"


def seed(conn):
    """ws: 삭제 대상 워크스페이스, other: alice와 bob이 함께 쓰는 남는 워크스페이스."""
    for page, user, workspace in (("page-ws", "alice", "ws"), ("page-other", "alice", "other")):
        conn.execute(
            "INSERT INTO wiki_pages(id, page_type, title, slug, user_id, workspace_id, status, created_at, updated_at) "
            "VALUES (%s, 'source', '제목', %s, %s, %s, 'active', now(), now())",
            (page, page, user, workspace),
        )
        doc = "doc-" + workspace
        conn.execute("INSERT INTO document_wiki_links VALUES (%s, %s, 'source_of', 1, %s, now())", (doc, page, workspace))
        conn.execute("INSERT INTO source_blocks(document_id, block_id, text) VALUES (%s, 'b1', '본문')", (doc,))
        conn.execute("INSERT INTO source_block_snapshots(document_id, source_content_hash) VALUES (%s, 'h')", (doc,))
        conn.execute(
            "INSERT INTO document_derived_state(document_id, workspace_id, last_edit_revision, last_edit_hash, last_edited_at) "
            "VALUES (%s, %s, 1, 'h', now())",
            (doc, workspace),
        )
    # 'shared' 벡터는 두 워크스페이스가 함께 참조하고, 'only-ws'는 ws만 참조한다.
    for vector in ("shared", "only-ws"):
        conn.execute(
            "INSERT INTO wiki_embedding_vectors(id, embedding_model, representation_hash, representation_text) "
            "VALUES (%s, 'm', %s, '원문')",
            (vector, vector),
        )
    for unit, vector, page in (("u1", "shared", "page-ws"), ("u2", "only-ws", "page-ws"), ("u3", "shared", "page-other")):
        conn.execute(
            "INSERT INTO wiki_embedding_units(id, embedding_vector_id, page_id, source_document_id, unit_type, text) "
            "VALUES (%s, %s, %s, 'doc', 'summary', '원문')",
            (unit, vector, page),
        )
    for run, workspace in ((RUN_WS, "ws"), (RUN_OTHER, "other")):
        conn.execute(
            "INSERT INTO pipeline_runs(id, document_id, user_id, workspace_id, input_source, output_dir, mode, status) "
            "VALUES (%s, %s, 'bob', %s, 'kafka', 'runs/x', 'ingest', 'completed')",
            (run, "doc-" + workspace, workspace),
        )
    conn.execute("INSERT INTO wiki_schemas(id, workspace_id, user_id, name, raw_markdown, status) "
                 "VALUES ('schema-ws', 'ws', 'alice', 's', '', 'active'), ('schema-other', 'other', 'alice', 's', '', 'active'), "
                 "('schema-bob', 'other', 'bob', 's', '', 'active')")
    conn.execute("INSERT INTO wiki_source_tombstones(workspace_id, document_id) VALUES ('ws', 'doc-trash')")
    for task, workspace, user in (("task-ws", "ws", "alice"), ("task-alice", "other", "alice"), ("task-bob", "other", "bob")):
        conn.execute("INSERT INTO ai_task_runs(id, workspace_id, user_id, kind, status) VALUES (%s, %s, %s, 'query', 'running')",
                     (task, workspace, user))
        conn.execute("INSERT INTO ai_task_changes(run_id, table_name, row_key) VALUES (%s, 'wiki_pages', '{}')", (task,))
    for skill, workspace, scope, owner in (("team-ws", "ws", "team", None), ("team-other", "other", "team", None),
                                           ("personal-alice", None, "personal", "alice"),
                                           ("personal-bob", None, "personal", "bob")):
        conn.execute("INSERT INTO skills(id, workspace_id, scope_type, owner_user_id, command, status) "
                     "VALUES (%s, %s, %s, %s, %s, 'enabled')", (skill, workspace, scope, owner, skill))
        conn.execute("INSERT INTO skill_versions(id, skill_id, version, name, description, instructions_markdown, status, created_by) "
                     "VALUES (%s, %s, 1, 'n', 'd', 'i', 'published', 'alice')", (skill + "-v1", skill))
    for agent_run, workspace, user, key in (("agent-ws", "ws", "alice", "agent-runs/a/artifacts/alice-ws.md"),
                                            ("agent-alice", "other", "alice", "agent-runs/b/artifacts/alice-other.md"),
                                            ("agent-bob", "other", "bob", "agent-runs/c/artifacts/bob-other.md")):
        conn.execute("INSERT INTO agent_runs(id, workspace_id, user_id, action, status, request_summary) "
                     "VALUES (%s, %s, %s, 'edit', 'completed', '요청')", (agent_run, workspace, user))
        conn.execute("INSERT INTO agent_run_artifacts(id, run_id, workspace_id, user_id, content_hash, purpose, object_key) "
                     "VALUES (%s, %s, %s, %s, 'h', 'draft', %s)", (agent_run + "-art", agent_run, workspace, user, key))
        conn.execute("INSERT INTO checkpoints(thread_id, checkpoint_id, checkpoint) VALUES (%s, 'c1', '{}')", (agent_run,))
        conn.execute("INSERT INTO checkpoint_blobs(thread_id, channel, version, type) VALUES (%s, 'ch', '1', 't')", (agent_run,))


def ids(conn, table, column="id"):
    return {row["v"] for row in conn.execute(sql.SQL("SELECT {} AS v FROM {}").format(
        sql.Identifier(column), sql.Identifier(table))).fetchall()}


def test_workspace_purge_removes_workspace_ai_data_except_usage(database, storage):
    with database() as conn:
        seed(conn)
        conn.execute("INSERT INTO ai_model_usage(id, run_id, workspace_id, user_id, kind, provider, requested_model, model, status) "
                     "VALUES (gen_random_uuid(), 'task-ws', 'ws', 'alice', 'query', 'p', 'm', 'm', 'succeeded')")

    first = purge.purge_workspace("ws")

    assert first["wiki_pages"] == 1 and first["s3_objects"] == 5  # alice·bob·carol 객체, 작업 산출물, 에이전트 산출물(실행 로그 제외)
    with database() as conn:
        assert ids(conn, "wiki_pages") == {"page-other"}
        assert ids(conn, "wiki_embedding_vectors") == {"shared"}
        assert ids(conn, "source_blocks", "document_id") == {"doc-other"}
        assert ids(conn, "source_block_snapshots", "document_id") == {"doc-other"}
        assert ids(conn, "document_derived_state", "document_id") == {"doc-other"}
        assert ids(conn, "wiki_source_tombstones", "document_id") == set()
        assert {str(run) for run in ids(conn, "pipeline_runs")} == {RUN_OTHER}
        assert ids(conn, "wiki_schemas") == {"schema-other", "schema-bob"}
        assert ids(conn, "ai_task_runs") == {"task-alice", "task-bob"}
        assert ids(conn, "skills") == {"team-other", "personal-alice", "personal-bob"}
        assert ids(conn, "agent_runs") == {"agent-alice", "agent-bob"}
        assert ids(conn, "agent_run_artifacts", "run_id") == {"agent-alice", "agent-bob"}
        assert ids(conn, "checkpoints", "thread_id") == {"agent-alice", "agent-bob"}
        assert ids(conn, "checkpoint_blobs", "thread_id") == {"agent-alice", "agent-bob"}
        assert ids(conn, "ai_model_usage", "workspace_id") == {"ws"}
    assert storage.objects == {"wiki/alice/other/sources/keep.md", "agent-runs/b/artifacts/alice-other.md",
                               "agent-runs/c/artifacts/bob-other.md", f"pipeline-runs/{RUN_WS}/pipeline.log",
                               f"pipeline-runs/{RUN_OTHER}/pipeline.log"}  # 실행 로그는 lifecycle 만료에 맡긴다

    second = purge.purge_workspace("ws")
    assert set(second.values()) == {0}


def test_user_purge_keeps_shared_wiki_and_removes_personal_data(database, storage):
    with database() as conn:
        seed(conn)

    first = purge.purge_user("alice")

    assert first["skills"] == 1
    with database() as conn:
        assert ids(conn, "wiki_pages") == {"page-ws", "page-other"}
        assert ids(conn, "skills") == {"team-ws", "team-other", "personal-bob"}
        assert ids(conn, "skill_versions") == {"team-ws-v1", "team-other-v1", "personal-bob-v1"}
        assert ids(conn, "agent_runs") == {"agent-bob"}
        assert ids(conn, "checkpoints", "thread_id") == {"agent-bob"}
        assert ids(conn, "ai_task_runs") == {"task-bob"}
        assert ids(conn, "ai_task_changes", "run_id") == {"task-bob"}
        assert ids(conn, "wiki_schemas") == {"schema-bob"}
    assert "agent-runs/a/artifacts/alice-ws.md" not in storage.objects
    assert "agent-runs/b/artifacts/alice-other.md" not in storage.objects
    assert "agent-runs/c/artifacts/bob-other.md" in storage.objects

    second = purge.purge_user("alice")
    assert set(second.values()) == {0}


def test_redelivered_task_in_purged_scope_is_rejected(database, storage):
    """파기 뒤 Kafka 재전달로 같은 작업이 다시 와도 작업 기록을 되살리지 않는다."""
    purge.purge_workspace("ws")
    purge.purge_user("alice")

    for workspace, user in (("ws", "bob"), ("other", "alice")):
        with pytest.raises(ScopePurgedError):
            journal.register(dict(run_id="again", workspace_id=workspace, user_id=user, kind="query"))
    journal.register(dict(run_id="kept", workspace_id="other", user_id="bob", kind="query"))
    with database() as conn:
        assert ids(conn, "ai_task_runs") == {"kept"}


def test_purge_routes_require_internal_token(monkeypatch):
    import api

    calls = []
    monkeypatch.setattr(purge, "purge_workspace", lambda workspace_id: calls.append(workspace_id) or {"wiki_pages": 1})
    client = TestClient(api.app)

    assert client.post("/internal/ai/purge/workspaces", json={}).status_code == 401
    response = client.post("/internal/ai/purge/workspaces", json={"workspace_ids": ["ws", "ws2", "ws"]},
                           headers={"X-Internal-Token": os.environ["INTERNAL_CALLBACK_TOKEN"]})
    assert response.status_code == 200
    assert response.json() == {"deleted": {"wiki_pages": 2}}
    assert calls == ["ws", "ws2"]


def test_workspace_purge_removes_files_of_user_without_db_rows(database, storage):
    """사용자를 먼저 파기해 DB 흔적이 없고 lint 산출물만 남은 wiki/U/W/ 도 S3 목록으로 찾아 지운다."""
    storage.objects.add("wiki/dave/ws/lint/only.md")  # dave는 어느 테이블에도 행이 없다.

    purge.purge_workspace("ws")

    assert not {key for key in storage.objects if key.startswith(("wiki/dave/ws/", "wiki/carol/ws/"))}


def test_edit_event_in_purged_workspace_does_not_revive_derived_state(database, monkeypatch, caplog):
    from app.workers import edit_event_consumer as consumer

    monkeypatch.setattr(consumer.database, "connect_ai", database)
    event = (b'{"document_id": "doc-x", "workspace_id": "ws", "revision": 1, "content_hash": "h",'
             b' "created_at": "2026-10-11T00:00:00Z"}')
    purge.purge_workspace("ws")

    with caplog.at_level("INFO", logger="edit_event_consumer"):
        consumer._handle(event)
    with database() as conn:
        assert ids(conn, "document_derived_state", "document_id") == set()
    assert "skip" in caplog.text

    consumer._handle(event.replace(b'"ws"', b'"other"'))
    with database() as conn:
        assert ids(conn, "document_derived_state", "document_id") == {"doc-x"}


def test_document_deleted_in_purged_workspace_is_discarded_without_tombstone(database):
    from app.workers import ingest_worker

    command = {"kind": "document_deleted", "workspace_id": "ws", "document_id": "doc-x", "user_id": "alice"}
    purge.purge_workspace("ws")

    with pytest.raises(ScopePurgedError):
        ingest_worker._handle_controlled(command)
    with database() as conn:
        assert ids(conn, "wiki_source_tombstones", "document_id") == set()
