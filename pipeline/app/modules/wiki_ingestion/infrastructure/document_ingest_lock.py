from __future__ import annotations


MCP_ACTIVE_INGEST_STATES = ("ACTIVE_WINDOWING", "READY_TO_FINALIZE", "AWAITING_WEB_APPROVAL", "APPLYING", "UNKNOWN")


class ActiveIngestRunError(RuntimeError):
    code = "ACTIVE_RUN_EXISTS"


def lock_document_ingest(conn, document_id: str) -> None:
    """Serialize web/automatic and MCP intake ownership for one document."""
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        ("wiki-document-ingest:" + document_id,),
    )


def reject_other_active_ingest(conn, document_id: str, owner_id: str) -> None:
    pipeline = conn.execute(
        "SELECT 1 FROM pipeline_runs WHERE document_id = %s AND status = 'running' AND id::text <> %s LIMIT 1",
        (document_id, owner_id),
    ).fetchone()
    mcp = conn.execute(
        "SELECT 1 FROM mcp_wiki_ingest_runs WHERE document_id = %s "
        "AND state=ANY(%s) AND run_id::text <> %s LIMIT 1",
        (document_id, list(MCP_ACTIVE_INGEST_STATES), owner_id),
    ).fetchone()
    if pipeline is not None or mcp is not None:
        raise ActiveIngestRunError("Another ingest run already owns this document.")
