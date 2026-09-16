import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.modules.skill.domain.entities import SkillAuthoringReference
from app.modules.skill.domain.exceptions import ReferenceDocumentTooLargeError
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_ingestion_repository import (
    list_source_blocks,
)


class BackendSkillReferenceReader:
    def __init__(self, base_url: str, service_token: str, timeout_seconds: int = 30) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._timeout_seconds = timeout_seconds

    def read(
        self,
        *,
        workspace_id: str,
        user_id: str,
        document_id: str,
    ) -> SkillAuthoringReference:
        request = Request(
            self._base_url + "/internal/agent/skill-authoring/references/read",
            data=json.dumps(
                {
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "document_id": document_id,
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Agent-Service-Token": self._service_token,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                value = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 413:
                raise ReferenceDocumentTooLargeError() from exc
            if exc.code in {400, 403, 404}:
                raise ValueError("Reference document is not accessible.") from exc
            raise RuntimeError("Skill reference service request failed.") from exc
        except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Skill reference service request failed.") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Skill reference service response is invalid.")
        document_role = value.get("document_role")
        if document_role == "EDITABLE":
            markdown = value.get("markdown")
            if not isinstance(markdown, str):
                raise RuntimeError("Skill reference service response is invalid.")
        elif document_role == "ORIGINAL":
            markdown = "\n\n".join(block["text"] for block in list_source_blocks(document_id))
            if not markdown.strip():
                raise ValueError("Reference document is not accessible.")
        else:
            raise RuntimeError("Skill reference service response is invalid.")
        return SkillAuthoringReference(id=document_id, name="", markdown=markdown)


def build_skill_reference_reader() -> BackendSkillReferenceReader:
    token = os.environ.get("AGENT_INTERNAL_TOKEN")
    if not token:
        raise RuntimeError("Set AGENT_INTERNAL_TOKEN for Skill reference reads.")
    base_url = os.environ.get("DOCUMENT_INTERNAL_BASE_URL")
    if not base_url:
        raise RuntimeError("Set DOCUMENT_INTERNAL_BASE_URL for Skill reference reads.")
    return BackendSkillReferenceReader(
        base_url,
        token,
        int(os.environ.get("AGENT_TOOL_TIMEOUT_SECONDS", "30")),
    )
