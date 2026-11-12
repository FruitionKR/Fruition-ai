"""MCP get_wiki_page가 lint 제출 전에 보여 주는 문장·링크·대상 묶음.

모두 lint 검증(`calculate_lint_preview`)이 받는 모양 그대로다. 문장 id는 검증의 `evidence_by_page` 키,
링크 ref는 ADD_LINK/REMOVE_LINK가 받는 `concept:<slug>`, 대상 묶음은 검증이 target_snapshot에 요구하는 페이지다.
"""
from __future__ import annotations

import logging
from typing import Any

from app.modules.mcp_wiki_draft.application import McpWikiDraftError, load_lint_limits
from app.modules.mcp_wiki_draft.calculation import PreviewCalculationError
from app.modules.mcp_wiki_draft.lint_calculation import surviving_statements
from app.modules.wiki_ingestion.infrastructure.concept_contribution_rebuild import ConceptContributionError


logger = logging.getLogger(__name__)

EMPTY_LINT_CONTEXT: dict[str, Any] = {
    "statements": [],
    "links": [],
    "lint_target_page_ids": [],
    "lint_target_truncated": False,
    "lint_context_error": None,
}


def concept_lint_context(conn, workspace_id: str, page: dict[str, Any]) -> dict[str, Any]:
    if page.get("page_type") != "concept" or page.get("status") != "active":
        return dict(EMPTY_LINT_CONTEXT)
    page_id = str(page["id"])
    try:
        max_pages: int | None = load_lint_limits().max_target_pages
    except McpWikiDraftError:
        max_pages = None
    target_ids, truncated = lint_target_closure(conn, workspace_id, page_id, max_pages)
    error = None
    try:
        statements = surviving_statements(conn, workspace_id, page_id)
    except (PreviewCalculationError, ConceptContributionError):
        logger.warning("MCP wiki page statements unavailable", exc_info=True)
        statements, error = [], "STATEMENTS_UNAVAILABLE"
    return {
        "statements": statements,
        "links": concept_links(conn, workspace_id, page_id),
        "lint_target_page_ids": target_ids,
        "lint_target_truncated": truncated,
        "lint_context_error": error,
    }


def concept_links(conn, workspace_id: str, page_id: str) -> list[dict[str, str]]:
    rows = conn.execute(
        "SELECT link.from_page_id,link.to_page_id,link.link_type,source.slug AS source_slug,target.slug AS target_slug "
        "FROM wiki_page_links link "
        "JOIN wiki_pages source ON source.id=link.from_page_id AND source.workspace_id=link.workspace_id "
        "JOIN wiki_pages target ON target.id=link.to_page_id AND target.workspace_id=link.workspace_id "
        "WHERE link.workspace_id=%s AND (link.from_page_id=%s OR link.to_page_id=%s) "
        "AND link.link_type<>'source_mentions_concept' "
        "AND source.page_type='concept' AND target.page_type='concept' "
        "AND source.status='active' AND target.status='active' "
        "ORDER BY link.from_page_id,link.link_type,link.to_page_id",
        (workspace_id, page_id, page_id),
    ).fetchall()
    links = []
    for row in rows:
        outgoing = str(row["from_page_id"]) == page_id
        links.append({
            "page_id": str(row["to_page_id"] if outgoing else row["from_page_id"]),
            "slug": str(row["target_slug"] if outgoing else row["source_slug"]),
            "relation": str(row["link_type"]),
            "direction": "outgoing" if outgoing else "incoming",
            "source_ref": f"concept:{row['source_slug']}",
            "target_ref": f"concept:{row['target_slug']}",
        })
    return links


def lint_target_closure(
    conn, workspace_id: str, page_id: str, max_pages: int | None
) -> tuple[list[str], bool]:
    """이 페이지를 lint 대상으로 낼 때 검증이 target_snapshot에 함께 요구하는 페이지(자기 포함).

    검증 규칙과 같다: 대상의 링크(source_mentions_concept 제외) 양 끝, 대상의 하위 항목 참조가 가리키는 slug,
    대상 slug를 참조하는 활성 페이지가 모두 대상이어야 한다. 이를 닫힐 때까지 넓힌다. `max_pages`를 넘으면
    넓히기를 멈추고 truncated=True다(이 페이지는 설정한 lint 대상 수 안에서 lint할 수 없다).
    """
    found = {page_id}
    frontier = {page_id}
    while frontier:
        ids = sorted(frontier)
        slugs = [str(row["slug"]) for row in conn.execute(
            "SELECT slug FROM wiki_pages WHERE workspace_id=%s AND id=ANY(%s)", (workspace_id, ids),
        ).fetchall()]
        linked = {
            str(value)
            for row in conn.execute(
                "SELECT from_page_id,to_page_id FROM wiki_page_links "
                "WHERE workspace_id=%s AND (from_page_id=ANY(%s) OR to_page_id=ANY(%s)) "
                "AND link_type<>'source_mentions_concept'",
                (workspace_id, ids, ids),
            ).fetchall()
            for value in (row["from_page_id"], row["to_page_id"])
        }
        referenced = {
            str(row["id"]) for row in conn.execute(
                "SELECT page.id FROM wiki_page_entry_references reference "
                "JOIN wiki_pages page ON page.workspace_id=reference.workspace_id AND page.slug=reference.target_slug "
                "WHERE reference.workspace_id=%s AND reference.page_id=ANY(%s) "
                "AND page.page_type='concept' AND page.status='active'",
                (workspace_id, ids),
            ).fetchall()
        }
        inbound = {
            str(row["id"]) for row in conn.execute(
                "SELECT DISTINCT page.id FROM wiki_page_entry_references reference "
                "JOIN wiki_pages page ON page.id=reference.page_id "
                "WHERE reference.workspace_id=%s AND reference.target_slug=ANY(%s) AND page.status='active'",
                (workspace_id, slugs),
            ).fetchall()
        }
        frontier = (linked | referenced | inbound) - found
        found |= frontier
        if max_pages is not None and len(found) > max_pages:
            return sorted(found), True
    return sorted(found), False
