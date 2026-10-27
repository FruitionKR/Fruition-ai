import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.modules.agent.domain.folder_lookup import (
    FOLDER_LOOKUP_READ_FAILED_MESSAGE,
    FolderEntry,
    FolderLookupUnavailableError,
    folder_entries,
)

FOLDER_TREE_PATH = "/internal/agent/folder-lookup/tree"


class BackendFolderTreeReader:
    """document-svc에서 사용자 기준 폴더·문서 트리(사이드바와 같은 범위)를 읽어 폴더 목록으로 펼친다."""

    def __init__(self, base_url: str, service_token: str, timeout_seconds: int = 30) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._timeout_seconds = timeout_seconds

    def read(self, *, workspace_id: str, user_id: str) -> list[FolderEntry]:
        request = Request(
            self._base_url + FOLDER_TREE_PATH,
            data=json.dumps({"workspace_id": workspace_id, "user_id": user_id}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Agent-Service-Token": self._service_token,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                value = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FolderLookupUnavailableError(FOLDER_LOOKUP_READ_FAILED_MESSAGE) from exc
        if not isinstance(value, dict) or not isinstance(value.get("items"), list):
            raise FolderLookupUnavailableError(FOLDER_LOOKUP_READ_FAILED_MESSAGE)
        return folder_entries(value["items"])


def build_folder_tree_reader() -> BackendFolderTreeReader:
    token = os.environ.get("AGENT_INTERNAL_TOKEN")
    if not token:
        raise RuntimeError("Set AGENT_INTERNAL_TOKEN for folder lookup.")
    base_url = os.environ.get("DOCUMENT_INTERNAL_BASE_URL")
    if not base_url:
        raise RuntimeError("Set DOCUMENT_INTERNAL_BASE_URL for folder lookup.")
    return BackendFolderTreeReader(base_url, token, int(os.environ.get("AGENT_TOOL_TIMEOUT_SECONDS", "30")))
