"""탈퇴 사용자·삭제된 워크스페이스의 AI 데이터를 지운다.

ai_model_usage는 워크스페이스 사용량 정산에 쓰이므로 지우지 않는다(보관 기간은 법률 검토 뒤 정한다).
AI 실행 로그(pipeline-runs/)는 지우지 않고 S3 lifecycle 만료에 맡긴다(로그에는 문서 본문이 없다).
S3를 DB보다 먼저 지운다. DB를 먼저 지우면 S3 삭제가 실패한 뒤 재호출할 때 지울 키를 다시 찾을 수 없다.
"""

import hashlib

import psycopg
from psycopg.rows import dict_row

from app.modules.wiki_ingestion.infrastructure import object_storage
from app.core.ai_database import ai_database_url


def connect():
    # 파기는 AI 작업 변경 기록(app.ai_task_run_id)을 남기지 않는다.
    return psycopg.connect(ai_database_url(), row_factory=dict_row)


_CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")


def _delete_agent_runs(conn, condition: str, params) -> tuple[dict[str, int], list[str]]:
    """agent_runs를 먼저 지운 뒤 checkpoint를 지운다.

    checkpoint 쓰기는 run 행에 KEY SHARE를 잡는 트리거(db/ai_schema.sql)로 보호된다. run 행 삭제가 진행 중이던
    쓰기를 기다려 끝낸 뒤, 별도 문장으로 checkpoint를 지우므로 그 쓰기까지 지워진다. 이후 쓰기는 거부된다.
    """
    ids = [row["id"] for row in conn.execute(f"DELETE FROM agent_runs WHERE {condition} RETURNING id", params).fetchall()]
    deleted = {"agent_runs": len(ids)}
    deleted.update((table, conn.execute(f"DELETE FROM {table} WHERE thread_id = ANY(%s)", (ids,)).rowcount)
                   for table in _CHECKPOINT_TABLES)
    return deleted, ids


def _sweep_artifacts(run_ids: list[str]) -> int:
    # DB 삭제 커밋 뒤에 한 번 더 지운다. S3 삭제와 DB 삭제 사이에 등록된 산출물 객체를 정리한다.
    # 객체 키 규칙은 postgres_agent_run_repository._artifact_object_key와 같다. 이후 등록은 run 행이 없어 DB에서 실패하고 객체를 스스로 지운다.
    if not run_ids:
        return 0
    prefixes = [f"agent-runs/{hashlib.sha256(run_id.encode('utf-8')).hexdigest()}/" for run_id in run_ids]
    return _delete_objects(prefixes, [])


def _mark_purged(scope_type: str, scope_id: str) -> None:
    # 삭제보다 먼저 커밋해 파기 중에 들어온 작업도 journal register에서 거절되게 한다.
    with connect() as conn:
        conn.execute("INSERT INTO ai_purged_scopes(scope_type, scope_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                     (scope_type, scope_id))


def purge_workspace(workspace_id: str) -> dict[str, int]:
    _mark_purged("workspace", workspace_id)
    with connect() as conn:
        artifacts = _column(conn, "SELECT object_key FROM agent_run_artifacts "
                                  "WHERE workspace_id = %s AND object_key IS NOT NULL", (workspace_id,))
    # wiki 객체(본문·클러스터·로그·페이지 이력)는 모두 wiki/{workspace_id}/ 아래에 있다(ADR 0033).
    prefixes = [f"wiki/{workspace_id}/"]
    # 실행 로그(pipeline-runs/*/pipeline.log)는 지우지 않는다. AI 역할에 삭제 권한이 없고 S3 30일 lifecycle로 만료된다.
    keys = artifacts
    deleted = {"s3_objects": _delete_objects(prefixes, keys)}

    with connect() as conn:
        params = {"ws": workspace_id}
        # source_blocks·source_block_snapshots에는 workspace_id가 없어 이 워크스페이스의 문서 ID로 지운다.
        params["docs"] = _column(conn, "SELECT document_id FROM document_derived_state WHERE workspace_id = %(ws)s "
                                       "UNION SELECT document_id FROM document_wiki_links WHERE workspace_id = %(ws)s "
                                       "UNION SELECT document_id FROM wiki_source_tombstones WHERE workspace_id = %(ws)s "
                                       "UNION SELECT document_id FROM pipeline_runs WHERE workspace_id = %(ws)s AND document_id IS NOT NULL "
                                       "UNION SELECT command->>'document_id' FROM ai_task_runs "
                                       "WHERE workspace_id = %(ws)s AND command->>'document_id' IS NOT NULL", params)
        # 임베딩 벡터는 같은 문구를 쓰는 다른 워크스페이스와 공유하므로, 이 워크스페이스가 참조하던 것 중 남은 참조가 없는 것만 지운다.
        params["vectors"] = _column(conn, "SELECT DISTINCT u.embedding_vector_id FROM wiki_embedding_units u "
                                          "JOIN wiki_pages p ON p.id = u.page_id WHERE p.workspace_id = %(ws)s", params)
        statements = [
            ("ai_task_changes", "DELETE FROM ai_task_changes WHERE run_id IN (SELECT id FROM ai_task_runs WHERE workspace_id = %(ws)s)"),
            ("ai_task_runs", "DELETE FROM ai_task_runs WHERE workspace_id = %(ws)s"),
            # 개인 스킬은 workspace_id가 NULL이라 팀 스킬만 지워진다. 버전·출처는 CASCADE.
            ("skills", "DELETE FROM skills WHERE workspace_id = %(ws)s"),
            # 링크·임베딩·임베딩 단위는 CASCADE.
            ("wiki_pages", "DELETE FROM wiki_pages WHERE workspace_id = %(ws)s"),
            ("wiki_embedding_vectors", "DELETE FROM wiki_embedding_vectors v WHERE v.id = ANY(%(vectors)s) "
                                       "AND NOT EXISTS (SELECT 1 FROM wiki_embedding_units u WHERE u.embedding_vector_id = v.id)"),
            ("source_blocks", "DELETE FROM source_blocks WHERE document_id = ANY(%(docs)s)"),
            ("source_block_snapshots", "DELETE FROM source_block_snapshots WHERE document_id = ANY(%(docs)s)"),
            ("wiki_schemas", "DELETE FROM wiki_schemas WHERE workspace_id = %(ws)s"),
            ("document_derived_state", "DELETE FROM document_derived_state WHERE workspace_id = %(ws)s"),
            ("wiki_source_tombstones", "DELETE FROM wiki_source_tombstones WHERE workspace_id = %(ws)s"),
            ("pipeline_runs", "DELETE FROM pipeline_runs WHERE workspace_id = %(ws)s"),
        ]
        # 계획·승인·작업·산출물 행은 ON DELETE CASCADE로 함께 지워진다.
        agent_deleted, run_ids = _delete_agent_runs(conn, "workspace_id = %(ws)s", params)
        deleted.update(agent_deleted)
        deleted.update((table, conn.execute(statement, params).rowcount) for table, statement in statements)
    deleted["s3_objects"] += _sweep_artifacts(run_ids)
    return deleted


def purge_user(user_id: str) -> dict[str, int]:
    """공유 워크스페이스에 남는 사용자 개인 데이터를 지운다. 공용 위키는 남긴다."""
    _mark_purged("user", user_id)
    with connect() as conn:
        artifacts = _column(conn, "SELECT object_key FROM agent_run_artifacts "
                                  "WHERE user_id = %s AND object_key IS NOT NULL", (user_id,))
    deleted = {"s3_objects": _delete_objects([], artifacts)}

    with connect() as conn:
        statements = [
            # 작업 기록을 지우면 실행 중이던 작업은 이후 변경이 거부되고, 재전달은 ai_purged_scopes로 거절된다.
            ("ai_task_changes", "DELETE FROM ai_task_changes WHERE run_id IN (SELECT id FROM ai_task_runs WHERE user_id = %(user)s)"),
            ("ai_task_runs", "DELETE FROM ai_task_runs WHERE user_id = %(user)s"),
            ("skills", "DELETE FROM skills WHERE scope_type = 'personal' AND owner_user_id = %(user)s"),
            ("wiki_schemas", "DELETE FROM wiki_schemas WHERE user_id = %(user)s"),
        ]
        agent_deleted, run_ids = _delete_agent_runs(conn, "user_id = %(user)s", {"user": user_id})
        deleted.update(agent_deleted)
        deleted.update((table, conn.execute(statement, {"user": user_id}).rowcount) for table, statement in statements)
    deleted["s3_objects"] += _sweep_artifacts(run_ids)
    return deleted


def _column(conn, query: str, params) -> list[str]:
    return [next(iter(row.values())) for row in conn.execute(query, params).fetchall()]


def _delete_objects(prefixes: list[str], keys: list[str]) -> int:
    storage = object_storage.client()
    bucket = object_storage.bucket_name()
    targets = [object_storage.split_storage_uri(key) for key in keys]
    targets += [(bucket, item.object_name) for prefix in prefixes
                for item in storage.list_objects(bucket, prefix=prefix, recursive=True)]
    # ponytail: 객체마다 한 번씩 요청한다. 워크스페이스 객체가 수만 개로 늘면 remove_objects 일괄 삭제로 바꾼다.
    for target_bucket, key in targets:
        storage.remove_object(target_bucket, key)  # 없는 키 삭제도 S3에서는 성공이라 재호출해도 같다.
    return len(targets)
