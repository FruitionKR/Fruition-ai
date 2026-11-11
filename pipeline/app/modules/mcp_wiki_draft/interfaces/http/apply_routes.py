from __future__ import annotations

import re
from functools import lru_cache

from fastapi import APIRouter, Depends, Header, Request
from starlette.concurrency import run_in_threadpool

from app.modules.mcp_wiki_draft.application import McpWikiDraftError
from app.modules.mcp_wiki_draft.apply_application import McpWikiDraftApply
from app.modules.mcp_wiki_draft.infrastructure.apply_authority import ConfiguredWikiApprovalAuthority
from app.modules.mcp_wiki_draft.infrastructure.apply_repository import PostgresMcpWikiApplyRepository
from app.modules.mcp_wiki_draft.interfaces.http.apply_schemas import ApplyWikiDraftRequest
from app.modules.mcp_wiki_draft.interfaces.http.routes import _error, _uuid, _validated
from app.modules.wiki_embedding.infrastructure.threaded_wiki_embedding_job import DeferredWikiEmbeddingJob


router = APIRouter(prefix="/internal/mcp/wiki", tags=["mcp-wiki-apply"])
_OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}")


@lru_cache(maxsize=1)
def get_mcp_wiki_draft_apply() -> McpWikiDraftApply:
    # 웹 편입 worker(get_deferred_pipeline_run_use_case)와 같이 여기서는 발행만 하고 임베딩은 maintenance worker가 만든다.
    # Document가 발행 결과를 받으면 mcp_publication_index로 그 worker를 깨운다. pipeline-api에 BGE-M3를 올리지 않는다.
    return McpWikiDraftApply(
        PostgresMcpWikiApplyRepository(DeferredWikiEmbeddingJob()), ConfiguredWikiApprovalAuthority()
    )


@router.post("/drafts/{draft_id}/apply")
async def apply_draft(
    draft_id: str,
    request: Request,
    use_case: McpWikiDraftApply = Depends(get_mcp_wiki_draft_apply),
):
    try:
        payload = await _validated(request, ApplyWikiDraftRequest)
        return await run_in_threadpool(use_case.apply, _uuid(draft_id, "Draft"), payload)
    except McpWikiDraftError as exc:
        return _error(exc)


@router.get("/publication-results/{approval_id}/{operation_id}")
async def publication_result(
    approval_id: str,
    operation_id: str,
    workspace_id: str = Header(alias="X-MCP-Workspace-Id"),
    user_id: str = Header(alias="X-MCP-User-Id"),
    use_case: McpWikiDraftApply = Depends(get_mcp_wiki_draft_apply),
):
    try:
        approval_id = _uuid(approval_id, "Approval")
        if _OPERATION_ID.fullmatch(operation_id) is None:
            raise McpWikiDraftError(404, "NOT_FOUND", "Operation not found.")
        return await run_in_threadpool(
            use_case.result, approval_id, operation_id, workspace_id, user_id
        )
    except McpWikiDraftError as exc:
        return _error(exc)
