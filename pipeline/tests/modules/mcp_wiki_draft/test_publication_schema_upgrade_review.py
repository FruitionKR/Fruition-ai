"""Publication schema upgrades preserve receipts and enforce outcome invariants."""
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_AGENT_DATABASE_URL"), reason="requires isolated PostgreSQL")
SCHEMA = Path(__file__).parents[3] / "db" / "ai_schema.sql"


@pytest.mark.parametrize("legacy", [False, True])
def test_publication_schema_preserves_hash_and_is_repeatable(legacy):
    with psycopg.connect(os.environ["TEST_AGENT_DATABASE_URL"]) as conn:
        try:
            name = "receipt_upgrade_" + uuid4().hex
            conn.execute(f'CREATE SCHEMA "{name}"')
            conn.execute(f'SET search_path TO "{name}",public')
            schema = SCHEMA.read_text()
            conn.execute(schema)
            if legacy:
                conn.execute("ALTER TABLE mcp_wiki_publication_results RENAME COLUMN apply_request_hash TO request_hash")
                conn.execute("ALTER TABLE mcp_wiki_publication_results DROP CONSTRAINT mcp_wiki_publication_result_changed_check")
            column = "request_hash" if legacy else "apply_request_hash"
            digest = "sha256:" + "a" * 64
            conn.execute(
                f"INSERT INTO mcp_wiki_publication_results (approval_id,operation_id,draft_id,draft_version,{column},"
                "workspace_id,user_id,grant_id,client_id,policy_generation,draft_hash,public_state_hash,state) "
                "VALUES (%s,'operation',%s,1,%s,'workspace','user',%s,'client',1,%s,%s,'UNKNOWN')",
                (uuid4(), uuid4(), digest, uuid4(), digest, digest),
            )
            for _ in range(2):
                conn.execute(schema)
                assert conn.execute("SELECT apply_request_hash FROM mcp_wiki_publication_results").fetchone() == (digest,)
            conn.execute(
                "UPDATE mcp_wiki_publication_results SET state='PUBLISHED',changed=true,event_id=%s,"
                "published_reference_hash=%s,committed_at=now()", (uuid4(), digest)
            )
            # Upgrade must reject inconsistent old receipts, never silently rewrite them.
            conn.execute("ALTER TABLE mcp_wiki_publication_results DROP CONSTRAINT mcp_wiki_publication_result_changed_check")
            conn.execute("UPDATE mcp_wiki_publication_results SET changed=false")
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute(schema)
            assert conn.execute("SELECT changed FROM mcp_wiki_publication_results").fetchone() == (False,)
        finally:
            conn.rollback()


def test_schema_moves_legacy_mcp_source_slug_to_web_slug():
    with psycopg.connect(os.environ["TEST_AGENT_DATABASE_URL"]) as conn:
        try:
            name = "source_slug_" + uuid4().hex
            conn.execute(f'CREATE SCHEMA "{name}"')
            conn.execute(f'SET search_path TO "{name}",public')
            schema = SCHEMA.read_text()
            conn.execute(schema)
            rows = [
                ("legacy", "source", "doc_AB_1", "w1"),  # 예전 MCP 소스 slug
                ("web", "source", "doc-2", "w1"),        # 웹 slug는 그대로
                ("legacy-taken", "source", "doc_2", "w1"),  # 웹 소스 페이지가 이미 있으면 그대로
                ("concept", "concept", "Keep_Me", "w1"),  # 개념 slug는 대상이 아니다
            ]
            for page_id, page_type, slug, workspace in rows:
                conn.execute(
                    "INSERT INTO wiki_pages(id,page_type,title,slug,user_id,workspace_id,status,created_at,updated_at) "
                    "VALUES (%s,%s,'t',%s,'u',%s,'active',now(),now())",
                    (page_id, page_type, slug, workspace),
                )
            for _ in range(2):
                conn.execute(schema)
            slugs = dict(conn.execute("SELECT id, slug FROM wiki_pages").fetchall())
            assert slugs == {"legacy": "doc-ab-1", "web": "doc-2", "legacy-taken": "doc_2", "concept": "Keep_Me"}
        finally:
            conn.rollback()
