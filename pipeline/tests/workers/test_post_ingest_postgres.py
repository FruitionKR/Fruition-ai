"""후속 편입(`_handle_post_ingest`)이 실제 PostgreSQL에서 편입 실행의 source_contribution까지 쓰는지 본다.

모델 호출(클러스터 생성·품질 평가)과 클러스터 파일 저장만 가짜다. 실행 기록 갱신은 실제 함수를 그대로 쓴다.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import quote
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Json

from app.workers import task_worker

DSN = os.environ.get("TEST_AGENT_DATABASE_URL") or os.environ.get("CONCEPT_LOCK_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="격리 PostgreSQL 테스트 URL이 지정되지 않았습니다.")
AI_SCHEMA_SQL = (Path(__file__).resolve().parents[2] / "db" / "ai_schema.sql").read_text(encoding="utf-8")


@pytest.fixture
def connect(monkeypatch):
    name = "test_post_ingest_" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    separator = "&" if "?" in DSN else "?"
    scoped = f"{DSN}{separator}options={quote(f'-csearch_path={name}')}"
    with psycopg.connect(scoped) as conn:
        conn.execute(AI_SCHEMA_SQL)

    def factory() -> psycopg.Connection:
        return psycopg.connect(scoped, row_factory=dict_row)

    monkeypatch.setattr(task_worker.database, "connect", factory)
    monkeypatch.setattr(task_worker.database, "connect_ai", factory)
    try:
        yield factory
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def test_post_ingest_writes_cluster_claim_signatures_to_the_ingest_run(connect, monkeypatch) -> None:
    ingest_run, post_run = str(uuid4()), str(uuid4())
    with connect() as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, document_id, workspace_id, input_source, output_dir, mode, status, manifest) "
            "VALUES (%s, 'doc-1', 'ws-1', 'test', 'test', 'ingest', 'succeeded', %s)",
            (ingest_run, Json({
                "source_contribution": {"concept_slugs": ["alpha"], "links": []},
                "post_ingest": {"cluster_normalized": {"document": {"document_id": "doc-1"}}, "page_ids": [],
                                "quality_required": False},
            })),
        )
    active = "# Active Meaning Clusters\n\n## cluster-a\n\n### Claims\n- claim_001: 알파 근거 [doc-1:B1]\n"
    artifact = {"active_markdown": active, "maintenance_summary": {}}
    monkeypatch.setenv("QUERY_EMBEDDING_MODE", "text-only")
    with (
        patch.object(task_worker, "build_post_ingest_cluster_artifact", return_value=artifact),
        patch.object(task_worker, "_post_ingest_client", return_value=MagicMock()),
        patch.object(task_worker, "read_optional_text_object", return_value=""),
        patch.object(task_worker, "jev_concept_judge", return_value=None),
        patch.object(task_worker.database, "persist_post_ingest_clusters", return_value=[]),
    ):
        result = task_worker._handle_post_ingest({
            "run_id": post_run, "ingest_run_id": ingest_run, "kind": "post_ingest", "document_id": "doc-1",
            "workspace_id": "ws-1", "user_id": "user-1", "provider": "openai", "model": "gpt-5-nano",
        })

    assert result["quality_status"] == "ready"
    with connect() as conn:
        contribution = conn.execute(
            "SELECT manifest->'source_contribution' AS value FROM pipeline_runs WHERE id = %s", (ingest_run,)
        ).fetchone()["value"]
    assert contribution["concept_slugs"] == ["alpha"]
    assert isinstance(contribution["claim_signatures"], list)
    assert "relation_signatures" not in contribution
