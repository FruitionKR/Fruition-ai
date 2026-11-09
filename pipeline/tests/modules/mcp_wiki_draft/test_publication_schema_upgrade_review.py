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
