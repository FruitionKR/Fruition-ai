from functools import lru_cache

from app.modules.mcp_wiki_draft.application import McpWikiDraftIntake
from app.modules.mcp_wiki_draft.infrastructure.backend_source_reader import build_source_reader
from app.modules.mcp_wiki_draft.infrastructure.postgres_repository import PostgresMcpWikiDraftRepository


@lru_cache(maxsize=1)
def get_mcp_wiki_draft_intake() -> McpWikiDraftIntake:
    return McpWikiDraftIntake(PostgresMcpWikiDraftRepository(), build_source_reader())
