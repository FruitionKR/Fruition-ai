"""업무 DB 변경을 역순으로 지정한다. 각 변경의 원자적 복구는 backend가 수행한다."""
import os
from urllib.parse import quote

import httpx

from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database


def rollback_backend(run_id: str, workspace_id: str, user_id: str) -> None:
    base = os.environ.get("AGENT_BACKEND_URL", "http://document-svc:8080").rstrip("/")
    path = base + "/internal/agent/tools/rollback/" + quote(run_id, safe="") + "/changes"
    actor = {"workspace_id": workspace_id, "user_id": user_id}
    with httpx.Client(headers={"X-Agent-Service-Token": os.environ["AGENT_INTERNAL_TOKEN"]}, timeout=30) as client:
        response = client.post(path, json=actor)
        response.raise_for_status()
        for change_id in response.json():
            response = client.post(path + "/" + str(int(change_id)), json=actor)
            response.raise_for_status()
        response = client.post(path.removesuffix("/changes") + "/finalize-edits", json=actor)
        response.raise_for_status()
        # Kafka보다 먼저 복구 revision을 반영해 완료 응답 뒤에 옛 이벤트가 도착해도 역행하지 않게 한다.
        with database.connect_ai() as conn:
            for event in response.json():
                conn.execute(
                    "INSERT INTO document_derived_state(document_id, workspace_id, last_edit_revision, last_edit_hash, last_edited_at) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (document_id) DO UPDATE SET "
                    "last_edit_revision = EXCLUDED.last_edit_revision, last_edit_hash = EXCLUDED.last_edit_hash, "
                    "last_edited_at = EXCLUDED.last_edited_at, updated_at = now() "
                    "WHERE document_derived_state.last_edit_revision < EXCLUDED.last_edit_revision",
                    (event["document_id"], event["workspace_id"], event["revision"], event["content_hash"], event["created_at"]),
                )
