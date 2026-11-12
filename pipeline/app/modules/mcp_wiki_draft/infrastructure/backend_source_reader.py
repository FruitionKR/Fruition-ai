from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.modules.agent_run.infrastructure.backend_tool_gateway import agent_backend_url
from app.modules.mcp_wiki_draft.application import IntakeLimits, McpWikiDraftError
from app.modules.mcp_wiki_draft.interfaces.http.schemas import McpWikiActorRequest


_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_BLOCK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}")
_MIME_TYPES = {"text/markdown", "text/x-markdown", "text/plain"}
_RESPONSE_KEYS = {
    "schema_version", "document_id", "display_name", "workspace_id", "source_version",
    "source_content_hash", "mime_type", "blocks",
}
_BLOCK_KEYS = {"block_id", "ordinal", "text", "content_hash"}
# document가 웹 분할(`/internal/mcp/source-blocks/resolve`)로 받은 줄 범위·블록 종류. 이전 document는 보내지 않는다.
_LOCATION_KEYS = {"line_start", "line_end", "block_type"}
_BLOCK_TYPES = {"paragraph", "heading", "list", "code"}


class BackendMcpWikiSourceReader:
    def __init__(self, base_url: str, service_token: str, timeout_seconds: int = 30) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._timeout_seconds = timeout_seconds

    def read(
        self, actor: McpWikiActorRequest, document_id: str, limits: IntakeLimits
    ) -> dict:
        body = json.dumps(
            {"schema_version": "mcp.wiki-source.v1", "actor": actor.model_dump()},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = Request(
            f"{self._base_url}/internal/documents/{quote(document_id, safe='')}/mcp-wiki-source",
            data=body,
            headers={"Content-Type": "application/json", "X-Agent-Service-Token": self._service_token},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read(limits.source_response_bytes + 1)
        except HTTPError as exc:
            status = exc.code if exc.code in {400, 403, 404, 409, 413} else 503
            code = _error_code(exc) if status != 503 else "DEPENDENCY_UNAVAILABLE"
            raise McpWikiDraftError(status, code, "Authoritative document source was rejected.", retryable=status == 503) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise McpWikiDraftError(503, "DEPENDENCY_UNAVAILABLE", "Authoritative document source is unavailable.", retryable=True) from exc
        if len(raw) > limits.source_response_bytes:
            raise McpWikiDraftError(413, "RESPONSE_LIMIT_EXCEEDED", "Authoritative document source response is too large.")
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise McpWikiDraftError(503, "DEPENDENCY_UNAVAILABLE", "Authoritative document source response is invalid.", retryable=True) from None
        return _validate(value, actor, document_id, limits)


class ConfiguredMcpWikiSourceReader:
    def read(self, actor: McpWikiActorRequest, document_id: str, limits: IntakeLimits) -> dict:
        token = os.environ.get("AGENT_INTERNAL_TOKEN", "").strip()
        if not token:
            raise McpWikiDraftError(503, "DEPENDENCY_UNAVAILABLE", "MCP wiki source callback is not configured.", retryable=True)
        try:
            timeout = int(os.environ.get("MCP_WIKI_DRAFT_SOURCE_TIMEOUT_SECONDS", ""))
            if timeout <= 0:
                raise ValueError
        except ValueError:
            raise McpWikiDraftError(503, "DEPENDENCY_UNAVAILABLE", "MCP wiki source timeout is not configured.", retryable=True) from None
        return BackendMcpWikiSourceReader(agent_backend_url(), token, timeout).read(actor, document_id, limits)


def build_source_reader() -> ConfiguredMcpWikiSourceReader:
    return ConfiguredMcpWikiSourceReader()


def _validate(value, actor, document_id, limits) -> dict:
    if (
        not isinstance(value, dict)
        or set(value) != _RESPONSE_KEYS
        or value.get("schema_version") != "mcp.wiki-source.v1"
        or value.get("document_id") != document_id
        # 표시 이름은 소스 페이지 제목 한 줄에 들어가는 신뢰할 수 없는 텍스트다. Document 업로드·이름 변경이
        # 받는 이름은 그대로 받고, 줄을 끊을 수 있는 문자(Cc·Zl·Zp)만 막는다.
        or not isinstance(value.get("display_name"), str)
        or not value["display_name"].strip()
        or len(value["display_name"]) > 255
        or any(unicodedata.category(char) in {"Cc", "Zl", "Zp"} for char in value["display_name"])
        or value.get("workspace_id") != actor.workspace_id
        or type(value.get("source_version")) is not int
        or value["source_version"] < 1
        or not isinstance(value.get("source_content_hash"), str)
        or _HASH.fullmatch(value["source_content_hash"]) is None
        or value.get("mime_type") not in _MIME_TYPES
        or not isinstance(value.get("blocks"), list)
        or not 1 <= len(value["blocks"]) <= limits.source_blocks
    ):
        raise McpWikiDraftError(503, "DEPENDENCY_UNAVAILABLE", "Authoritative document source response is invalid.", retryable=True)
    seen_ids: set[str] = set()
    total = 0
    for ordinal, block in enumerate(value["blocks"], 1):
        if (
            not isinstance(block, dict)
            or set(block) not in (_BLOCK_KEYS, _BLOCK_KEYS | _LOCATION_KEYS)
            or not _valid_location(block)
            or not isinstance(block.get("block_id"), str)
            or _BLOCK_ID.fullmatch(block["block_id"]) is None
            or block["block_id"] in seen_ids
            or type(block.get("ordinal")) is not int
            or block["ordinal"] != ordinal
            or not isinstance(block.get("text"), str)
            or not block["text"]
            or not isinstance(block.get("content_hash"), str)
        ):
            raise McpWikiDraftError(503, "DEPENDENCY_UNAVAILABLE", "Authoritative document blocks are invalid.", retryable=True)
        seen_ids.add(block["block_id"])
        encoded = block["text"].encode("utf-8")
        total += len(encoded)
        expected = "sha256:" + hashlib.sha256(encoded).hexdigest()
        if block["content_hash"] != expected or len(encoded) > limits.window_text_bytes:
            raise McpWikiDraftError(413, "SOURCE_LIMIT_EXCEEDED", "A source block cannot fit in a configured window.")
    if total > limits.source_text_bytes:
        raise McpWikiDraftError(413, "SOURCE_LIMIT_EXCEEDED", "Authoritative document source exceeds configured storage bounds.")
    return value


def _valid_location(block: dict) -> bool:
    if not _LOCATION_KEYS & set(block):
        return True
    start, end, block_type = block["line_start"], block["line_end"], block["block_type"]
    if start is None and end is None and block_type is None:
        return True
    return (
        type(start) is int and type(end) is int and 1 <= start <= end
        and block_type in _BLOCK_TYPES
    )


def _error_code(exc: HTTPError) -> str:
    try:
        value = json.loads(exc.read(65_536))
        code = value.get("error", {}).get("code")
        return code if isinstance(code, str) else "DEPENDENCY_UNAVAILABLE"
    except Exception:
        return "DEPENDENCY_UNAVAILABLE"
