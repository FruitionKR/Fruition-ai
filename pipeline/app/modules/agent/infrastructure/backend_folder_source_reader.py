import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.modules.agent.domain.folder_sources import (
    FolderSourceDocument,
    FolderSourceListing,
    FolderSourceUnavailableError,
)

FOLDER_SOURCES_PATH = "/internal/agent/skill-execution/folder-sources/read"
FOLDER_NOT_ACCESSIBLE_MESSAGE = "이 폴더의 문서를 읽을 수 없어요. 다른 폴더나 문서를 골라 주세요."
FOLDER_READ_FAILED_MESSAGE = "폴더 문서를 읽지 못했어요. 잠시 후 다시 시도해 주세요."


class BackendFolderSourceReader:
    """document-svc에서 폴더 바로 아래 Markdown 문서 본문을 읽는다. 읽기 권한 확인은 document-svc가 한다."""

    def __init__(self, base_url: str, service_token: str, timeout_seconds: int = 30) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._timeout_seconds = timeout_seconds

    def read(
        self,
        *,
        workspace_id: str,
        user_id: str,
        folder_id: str,
        max_documents: int,
    ) -> FolderSourceListing:
        request = Request(
            self._base_url + FOLDER_SOURCES_PATH,
            data=json.dumps(
                {
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "folder_id": folder_id,
                    "max_documents": max_documents,
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
            if exc.code in {400, 403, 404}:
                raise FolderSourceUnavailableError(FOLDER_NOT_ACCESSIBLE_MESSAGE) from exc
            raise FolderSourceUnavailableError(FOLDER_READ_FAILED_MESSAGE) from exc
        except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FolderSourceUnavailableError(FOLDER_READ_FAILED_MESSAGE) from exc
        return _listing(value)


def _listing(value: object) -> FolderSourceListing:
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise FolderSourceUnavailableError(FOLDER_READ_FAILED_MESSAGE)
    documents = tuple(
        FolderSourceDocument(
            document_id=item["document_id"],
            title=item.get("display_name") if isinstance(item.get("display_name"), str) else "",
            markdown=item["markdown"],
            updated_at=item.get("updated_at") if isinstance(item.get("updated_at"), str) else None,
        )
        for item in value["items"]
        # 원본(업로드) 문서처럼 Markdown 본문이 없는 항목은 원문으로 쓰지 않는다.
        if isinstance(item, dict) and isinstance(item.get("document_id"), str) and isinstance(item.get("markdown"), str)
    )
    total = value.get("total_documents")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        total = len(documents)
    return FolderSourceListing(total_documents=total, documents=documents)


def build_folder_source_reader() -> BackendFolderSourceReader:
    token = os.environ.get("AGENT_INTERNAL_TOKEN")
    if not token:
        raise RuntimeError("Set AGENT_INTERNAL_TOKEN for folder source reads.")
    base_url = os.environ.get("DOCUMENT_INTERNAL_BASE_URL")
    if not base_url:
        raise RuntimeError("Set DOCUMENT_INTERNAL_BASE_URL for folder source reads.")
    return BackendFolderSourceReader(
        base_url,
        token,
        int(os.environ.get("AGENT_TOOL_TIMEOUT_SECONDS", "30")),
    )
