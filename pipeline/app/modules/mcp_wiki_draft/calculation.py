from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.ai_database import connect_ai
from app.core.jev_client import JevUnavailable
from app.modules.mcp_wiki_draft.application import McpWikiDraftError, load_submission_policy
from app.modules.mcp_wiki_draft.source_block_registry import (
    SourceBlocksChangedError,
    load_source_block_registry,
    match_snapshot_to_registry,
)
from app.modules.wiki_generation.domain.concept_identity import concept_name_keys
from app.modules.wiki_generation.domain.entities import SourceDocument
from app.modules.wiki_generation.domain.text_utils import name_key
from app.modules.wiki_generation.infrastructure.assemble import (
    ConceptPageAssembler,
    LinkBuilder,
    SourcePageAssembler,
)
from app.modules.wiki_generation.infrastructure.jev_concept_resolver import NameMatchConceptResolver
from app.modules.wiki_generation.infrastructure.normalize import SemanticNormalizer
from app.modules.wiki_ingestion.application.concept_contribution import (
    build_concept_contributions,
)
from app.modules.wiki_ingestion.domain.operation_recovery import PageContribution
from app.modules.wiki_ingestion.infrastructure.backend_document_reader import (
    read_contributions,
)
from app.modules.wiki_ingestion.infrastructure.concept_contribution_rebuild import (
    ConceptContributionError,
    load_concept_contributions,
    surviving_evidence_units,
)
from app.modules.wiki_ingestion.infrastructure.object_storage import (
    ImmutableObjectConflict,
    read_text_object,
    storage_uri,
)
from app.modules.wiki_ingestion.infrastructure.pending_contributions import (
    with_pending_contributions,
)
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_ingestion_repository import (
    _active_manual_source_markdown as active_manual_source_markdown,
    apply_ingest_outputs,
    list_active_concept_index,
)
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_writer import (
    load_concept_identity_index,
    read_optional_text_object,
)
from app.modules.wiki_ingestion.infrastructure.wiki_publication import PublicationConflict
from run_lab import resolve_judged_concepts


class PreviewCalculationError(RuntimeError):
    pass


class SourceChangedError(PreviewCalculationError):
    """시작 시점 블록 ID가 현재 문서 블록 레지스트리와 맞지 않는다(그 사이 다른 편입이 반영됐다)."""


class SemanticMergeError(PreviewCalculationError):
    pass


def _hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def _version(value: datetime) -> int:
    return max(1, int(value.timestamp() * 1_000_000))


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _body(row: dict[str, Any]) -> str:
    uri = row.get("markdown_uri")
    if not uri:
        return ""
    try:
        return read_optional_text_object(str(uri))
    except Exception as exc:
        raise PreviewCalculationError(f"failed to read committed wiki body: {row['id']}") from exc


def _page_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    body = _body(row)
    return {
        "page_id": str(row["id"]),
        "version": _version(row["updated_at"]),
        "content_hash": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "markdown_uri": row.get("markdown_uri"),
        "status": row["status"],
        "page_type": row["page_type"],
        "slug": row["slug"],
        "title": row["title"],
        "body": body,
    }


def _combined_concepts(submissions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    combined: dict[str, dict[str, Any]] = {}
    for submission in submissions:
        window = str(submission["window_ordinal"])
        for concept in submission["contributions"]:
            key = name_key(concept["concept_name"])
            target = combined.setdefault(key, {
                "concept_name": concept["concept_name"], "statements": [], "links": [],
            })
            merge_target = concept.get("merge_target_page_id")
            if merge_target is not None:
                if target.get("merge_target_page_id", merge_target) != merge_target:
                    raise SemanticMergeError("conflicting semantic merge targets for the same concept")
                target["merge_target_page_id"] = merge_target
            # finalize에서 서버 판정기(Jev)가 고정한 결과. 키가 있으면 판정했다는 뜻이고 값이 None이면 새 개념이다.
            if "server_merge_target_page_id" in concept:
                target["server_merge_target_page_id"] = concept["server_merge_target_page_id"]
            statement_ids = {
                item["statement_id"]: f"{window}:{item['statement_id']}"
                for item in concept["statements"]
            }
            target["statements"].extend(
                {**item, "statement_id": statement_ids[item["statement_id"]]}
                for item in concept["statements"]
            )
            target["links"].extend(
                {**item, "source_statement_id": statement_ids[item["source_statement_id"]]}
                for item in concept["links"]
            )
    return list(combined.values())


def _load_reconciled(conn, workspace_id: str, page_ids: list[str]) -> list[dict[str, Any]]:
    if not page_ids:
        return []
    try:
        remote = read_contributions(page_ids, workspace_id)
        if any(str(item.get("page_id")) not in page_ids for item in remote):
            raise PreviewCalculationError("remote contribution ledger returned an unexpected page")
        return with_pending_contributions(
            conn, remote, page_ids, cleanup_resolved=False
        )
    except PreviewCalculationError:
        raise
    except Exception as exc:
        raise PreviewCalculationError("contribution ledger could not be reconciled") from exc


def _change(
    kind: str,
    action: str,
    page_id: str,
    subject_id: str,
    before: str | None,
    after: str | None,
    evidence: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    value = {
        "kind": kind,
        "action": action,
        "page_id": page_id,
        "subject_id": subject_id,
        "before_text": before,
        "after_text": after,
        "evidence": evidence or [],
    }
    return {"change_id": _hash(value), **value}


def _canonical_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _removed_statement_changes(
    page_id: str, loaded: list[PageContribution], contribution: dict[str, Any]
) -> list[dict[str, Any]]:
    before = surviving_evidence_units(loaded)
    appended = PageContribution(
        page_id, "concept", str(contribution["operation_id"]),
        max((item.sequence for item in loaded), default=0) + 1, "", contribution,
    )
    after = surviving_evidence_units([*loaded, appended])
    return [
        _change("CONCEPT_STATEMENT", "REMOVE", page_id, evidence_id,
                str(before[evidence_id]["claim"]), None)
        for evidence_id in sorted(before.keys() - after.keys())
    ]


def _structural_rows(conn, workspace_id: str, page_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    if not page_ids:
        return {"entries": [], "links": [], "references": [], "manual_links": [], "document_links": []}
    return {
        "entries": [dict(row) for row in conn.execute(
            "SELECT entry_id,page_id,name,parent_entry_id,definition,block_refs,slugs,aliases,tags "
            "FROM wiki_page_entries WHERE workspace_id=%s AND page_id=ANY(%s) ORDER BY page_id,entry_id",
            (workspace_id, page_ids),
        ).fetchall()],
        "links": [dict(row) for row in conn.execute(
            "SELECT link.from_page_id,link.to_page_id,link.link_type,link.label,link.confidence "
            "FROM wiki_page_links link "
            "WHERE link.workspace_id=%s AND (link.from_page_id=ANY(%s) OR link.to_page_id=ANY(%s)) "
            "ORDER BY link.from_page_id,link.link_type,link.to_page_id",
            (workspace_id, page_ids, page_ids),
        ).fetchall()],
        "references": [dict(row) for row in conn.execute(
            "SELECT page_id,entry_id,target_slug,name,parent_entry_id FROM wiki_page_entry_references "
            "WHERE workspace_id=%s AND page_id=ANY(%s) ORDER BY page_id,position",
            (workspace_id, page_ids),
        ).fetchall()],
        "manual_links": [dict(row) for row in conn.execute(
            "SELECT from_page_id,to_page_id,present FROM wiki_source_manual_links "
            "WHERE workspace_id=%s AND (from_page_id=ANY(%s) OR to_page_id=ANY(%s)) ORDER BY from_page_id,to_page_id",
            (workspace_id, page_ids, page_ids),
        ).fetchall()],
        "document_links": [dict(row) for row in conn.execute(
            "SELECT document_id,wiki_page_id,relation_type,confidence FROM document_wiki_links "
            "WHERE workspace_id=%s AND wiki_page_id=ANY(%s) ORDER BY document_id,wiki_page_id,relation_type",
            (workspace_id, page_ids),
        ).fetchall()],
    }


def freeze_server_concept_merges(
    conn,
    *,
    workspace_id: str,
    submissions: list[dict[str, Any]],
    judge,
) -> list[dict[str, Any]]:
    """finalize에서 서버 개념 판정기(Jev)를 한 번 돌려 결과를 제출에 고정한다.

    승인·적용은 고정된 제출로 다시 계산하므로 유료·비결정적 판정을 다시 부르지 않는다. 판정기가 없거나
    호출할 수 없으면 제출을 그대로 돌려줘 외부 AI 판단으로 처리한다. 내부 LLM 대체 경로는 쓰지 않는다.
    """
    if judge is None:
        return submissions
    concepts = _combined_concepts(submissions)
    index = [item for item in load_concept_identity_index(conn, workspace_id) if item["status"] == "active"]
    by_slug = {str(item["slug"]): item for item in index}
    pool = [
        {"slug": str(item["slug"]), "title": str(item["title"]), "identity_aliases": item["identity_aliases"],
         "aliases": item["identity_aliases"], "definition": str(item.get("summary") or "")}
        for item in index
    ]
    incoming = [
        # MCP 기여에는 정의 칸이 없어 앞 문장들을 정의로 쓴다. ponytail: 고정 500자, 정의 칸이 생기면 그걸 쓴다.
        {"slug": f"mcp-incoming-{position}", "title": concept["concept_name"], "identity_aliases": [], "aliases": [],
         "definition": " ".join(item["text"] for item in concept["statements"][:3])[:500], "key": name_key(concept["concept_name"])}
        for position, concept in enumerate(concepts)
    ]
    try:
        resolutions = judge.judge_concepts(incoming, pool)
    except JevUnavailable:
        return submissions
    decided: dict[str, str | None] = {}
    related: dict[str, list[dict[str, str]]] = {}
    for concept, submitted in zip(incoming, concepts, strict=True):
        resolution = resolutions.get(concept["slug"])
        target = by_slug.get(str(resolution.get("canonical_slug"))) if resolution and resolution.get("decision") == "merge_into" else None
        # 판정 대상이 된 개념만 고정한다. 정의가 없거나 후보가 없어 묻지 않은 개념은 외부 AI 판단으로 남긴다.
        if target is not None or concept["slug"] in resolutions or judge_asked(concept, pool):
            decided[concept["key"]] = str(target["id"]) if target is not None else None
        if resolution and resolution.get("decision") == "link_to":
            targets = list(dict.fromkeys(resolution.get("link_targets") or [resolution.get("canonical_slug")]))
            if any(target not in by_slug for target in targets):
                raise SemanticMergeError("related judgment target is outside active workspace concepts")
            # Provenance records definition inputs, not a statement-level Jev judgment.
            offset = 0
            for statement in submitted["statements"][:3]:
                if offset < 500 and statement["text"]:
                    related.setdefault(concept["key"], []).extend({
                        "source_statement_id": statement["statement_id"],
                        "server_target_page_id": str(by_slug[target]["id"]),
                        "target_concept_name": str(by_slug[target]["title"]),
                        "relation": "concept_related_to",
                    } for target in targets)
                offset += len(statement["text"]) + 1
    if not decided:
        return submissions
    frozen = []
    max_links = load_submission_policy().max_links if related else None
    for submission in submissions:
        contributions = []
        for contribution in submission["contributions"]:
            key = name_key(contribution["concept_name"])
            prefix = f"{submission['window_ordinal']}:"
            local_ids = {item["statement_id"] for item in contribution["statements"]}
            links = list(contribution["links"])
            links.extend({**link, "source_statement_id": link["source_statement_id"][len(prefix):]}
                         for link in related.get(key, [])
                         if link["source_statement_id"].startswith(prefix)
                         and link["source_statement_id"][len(prefix):] in local_ids)
            contributions.append({**contribution, "links": links,
                                  **({"server_merge_target_page_id": decided[key]} if key in decided else {})})
        if max_links is not None and sum(len(item["links"]) for item in contributions) > max_links:
            raise McpWikiDraftError(413, "PREVIEW_TOO_LARGE", "Server judgments exceed the configured link bound.")
        frozen.append({**submission, "contributions": contributions})
    return frozen


def judge_asked(concept: dict[str, Any], pool: list[dict[str, Any]]) -> bool:
    """Jev가 이 개념을 실제로 물었는지. judge_concepts는 정의가 있는 개념과 정의 있는 후보가 있을 때만 묻는다."""
    return bool(concept["definition"].strip()) and any(str(item.get("definition") or "").strip() for item in pool)




class _SubmittedConceptJudgment:
    """웹 편입의 개념 판정기 자리. 판정은 사용자 AI 제출과 finalize에서 고정한 Jev 결과를 그대로 쓴다.

    웹과 같이 `NameMatchConceptResolver`가 이름 일치를 먼저 병합하고, 이름이 맞지 않는 개념만 여기로 온다.
    대상이 활성 개념이 아니면 새 페이지로 바꾸지 않고 실패한다(MCP 검증).
    """

    def __init__(self, concepts: list[dict[str, Any]], concept_index: list[dict[str, Any]]) -> None:
        self._targets = {
            name_key(concept["concept_name"]): (
                concept["server_merge_target_page_id"] if "server_merge_target_page_id" in concept
                else concept.get("merge_target_page_id")
            )
            for concept in concepts
        }
        self._active = {
            str(item["page_id"]): item for item in concept_index if item.get("status", "active") == "active"
        }

    def resolve(
        self,
        incoming_concepts: list[dict[str, Any]],
        existing_concepts: list[dict[str, Any]],
        missing_related_hints: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        resolutions = []
        for concept in incoming_concepts:
            target_id = self._targets.get(name_key(concept["title"]))
            if target_id is None:
                continue
            if len([item for item in self._active.values() if concept_name_keys(concept) & concept_name_keys(item)]) == 1:
                # 이름이 같은 기존 개념이 하나면 외부 AI 대상보다 먼저다. slug가 같아 이름 일치에서 빠진 개념은
                # 웹 slug 배정(allocate_concept_slugs)이 그 페이지로 보낸다.
                continue
            target = self._active.get(str(target_id))
            if target is None:
                raise SemanticMergeError("semantic merge target is no longer an active workspace concept")
            resolutions.append({
                "incoming_slug": concept["slug"], "decision": "merge_into", "canonical_slug": str(target["slug"]),
            })
        return {"resolutions": resolutions}


def build_ingest_manifest(
    *,
    run: dict[str, Any],
    submissions: list[dict[str, Any]],
    source_blocks: list[dict[str, Any]],
    current_source_blocks: list[dict[str, Any]],
    concept_index: list[dict[str, Any]],
    operation_id: str,
    previous_max_block_number: int = 0,
) -> dict[str, Any]:
    """제출한 창 기여를 웹 추출 결과(의미 노트)로 바꾼 뒤 웹 편입의 AI 뒤 단계로 manifest를 만든다.

    바꾸는 것은 AI 단계뿐이다. 정규화·개념 확정·slug 배정·페이지 조립·기여 생성은 웹 코드를 그대로 부른다.
    웹 생성 평가 가드(숫자·근거 검사)는 AI 출력 품질 검사라 MCP 제출 검증이 대신한다.
    """
    document_id = str(run["document_id"])
    if run.get("source_display_name") is None:
        # 컬럼 추가 이전에 시작한 run이다. 문서 ID를 제목으로 대신 쓰지 않고 다시 시작하게 한다.
        raise PreviewCalculationError("run predates the source display name snapshot")
    display_name = str(run["source_display_name"])
    concepts = _combined_concepts(submissions)
    statements = [statement for concept in concepts for statement in concept["statements"]]
    # 스냅샷 블록 ID는 document가 웹 분할·레지스트리로 받은 영구 ID다. 그 사이 레지스트리가 바뀌었으면 ID가 달라
    # 다른 블록을 가리키므로 막는다. 같으면 웹 재편입과 같은 비교 결과(무효화 포함)를 쓴다.
    try:
        registry = match_snapshot_to_registry(
            document_id, source_blocks, current_source_blocks, previous_max_block_number,
        )
    except SourceBlocksChangedError as exc:
        raise SourceChangedError(str(exc)) from exc

    def refs(statement: dict[str, Any]) -> list[str]:
        return list(dict.fromkeys(str(item["block_id"]) for item in statement["evidence"]))

    note = {
        "chunk_id": "mcp-submission",
        "key_points": [{"text": item["text"], "anchor_block_ids": refs(item)} for item in statements],
        "core_concepts": [
            {
                "title": concept["concept_name"],
                # MCP 기여에는 정의 칸이 없어 첫 문장을 정의로 쓴다.
                "definition": concept["statements"][0]["text"] if concept["statements"] else "",
                "anchor_block_ids": [ref for item in concept["statements"] for ref in refs(item)],
            }
            for concept in concepts
        ],
        "evidence_claims": [
            {
                "id": item["statement_id"], "claim": item["text"], "anchor_block_ids": refs(item),
                "related_concept_hints": [concept["concept_name"]], "confidence": 1.0,
            }
            for concept in concepts for item in concept["statements"]
        ],
    }
    document = SourceDocument(
        document_id=document_id, title=display_name, source_path=display_name,
        content_sha1=str(run["source_content_hash"]),
    )
    normalized = SemanticNormalizer(document, registry.blocks).normalize_notes([note])
    if [item["claim"] for item in normalized["evidence_units"]] != [item["text"] for item in statements]:
        raise PreviewCalculationError("normalized evidence does not match submitted statements")
    initial_slug = {name_key(item["title"]): str(item["slug"]) for item in normalized["concept_ledger"]}
    active = [item for item in concept_index if item.get("status", "active") == "active"]
    raw_resolution = NameMatchConceptResolver(_SubmittedConceptJudgment(concepts, concept_index)).resolve(
        normalized["concept_ledger"], active, normalized.get("missing_related_concept_hints", []),
    )
    resolved = resolve_judged_concepts(normalized, raw_resolution, [], concept_index)
    slug_map = resolved.get("concept_slug_map", {})

    def final_slug(name: str) -> str:
        slug = initial_slug[name_key(name)]
        return str(slug_map.get(slug, slug))

    # 연결 판단도 AI 단계다. 제출한 연결(외부 AI)과 고정한 Jev 관련 판정을 웹 링크 모양으로 바꾼다.
    active_by_id = {str(item["page_id"]): item for item in active}
    active_by_name: dict[str, list[dict[str, Any]]] = {}
    for item in active:
        for key in concept_name_keys(item):
            active_by_name.setdefault(key, []).append(item)
    links = LinkBuilder().build(resolved)
    seen = {frozenset((item["source"], item["target"])) for item in links}
    for concept in concepts:
        source = final_slug(concept["concept_name"])
        for link in concept["links"]:
            if "server_target_page_id" in link:
                target_page = active_by_id.get(str(link["server_target_page_id"]))
                if target_page is None:
                    raise SemanticMergeError("related target is no longer an active workspace concept")
                target = str(target_page["slug"])
            elif name_key(link["target_concept_name"]) in initial_slug:
                target = final_slug(link["target_concept_name"])
            else:
                matches = {str(item["page_id"]): item for item in active_by_name.get(name_key(link["target_concept_name"]), [])}
                if len(matches) != 1:
                    raise PreviewCalculationError("link target concept identity is unresolved or ambiguous")
                target = str(next(iter(matches.values()))["slug"])
            edge = frozenset((f"concept:{source}", f"concept:{target}"))
            if source == target or edge in seen:
                continue
            seen.add(edge)
            links.append({"source": f"concept:{source}", "target": f"concept:{target}", "relation": link["relation"]})

    # 웹 편입의 `_source_block_records`와 같은 모양이다. 줄 범위가 없는 스냅샷(이 변경 전에 시작한 run)은 비워 둔다.
    source_block_records = [
        {
            "document_id": document_id, "block_id": block.block_id, "text": block.text,
            "position": int(item["ordinal"]),
            "line_start": item.get("line_start"), "line_end": item.get("line_end"),
            "block_type": item.get("block_type"),
        }
        for block, item in zip(registry.blocks, source_blocks, strict=True)
    ]
    invalidated = registry.invalidated_block_ids
    source_page = SourcePageAssembler().build(resolved)
    key_points = [point for note in resolved.get("semantic_notes", []) for point in note.get("key_points", [])]
    return {
        "operation_id": operation_id,
        "user_id": str(run["user_id"]),
        "workspace_id": str(run["workspace_id"]),
        "document_id": document_id,
        "source_document_id": document_id,
        "display_name": display_name,
        "source_page": source_page,
        "source_extraction_artifact": source_page.get("source_extraction_artifact"),
        "source_blocks": source_block_records,
        "source_content_hash": str(run["source_content_hash"]).removeprefix("sha256:"),
        "source_block_changes": {"invalidated_block_ids": invalidated},
        "concept_pages": ConceptPageAssembler().build_top(resolved, top_n=None, source_key_points=key_points),
        "concept_contributions": build_concept_contributions(
            operation_id=operation_id,
            normalized=resolved,
            source_blocks=source_block_records,
            links=links,
            concept_update_decisions=[],
            invalidated_block_ids=invalidated,
        ),
        "links": links,
        "meaning_clusters": {"concept_update_decisions": []},
        "normalized": resolved,
        "warnings": resolved.get("warnings", []),
    }


# 공개 상태 행의 key 위치 중 페이지 id 자리. 새 페이지 id는 실행마다 달라 미리보기와 적용이 slug로 맞춘다.
_PAGE_POSITIONS = {
    "page": (1,), "entry": (1,), "link": (1, 3), "reference": (1,), "manual": (1, 2),
    "document_link": (2,), "alias": (1,), "block": (), "tombstone": (),
}
_PAGE_FIELDS = ("page_type", "slug", "title", "summary", "status")


def _public_rows(conn, workspace_id: str, document_id: str) -> dict[tuple[str, ...], dict[str, Any]]:
    """편입이 바꿀 수 있는 공개 상태 행 전체. 본문은 불변 객체라 markdown_uri가 같으면 같다.

    ponytail: 워크스페이스 행 전체를 두 번(전·후) 읽는다. 워크스페이스가 커서 느려지면 문서·개념 범위로 좁힌다.
    """
    rows: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT id,page_type,slug,title,summary,status,markdown_uri,updated_at FROM wiki_pages WHERE workspace_id=%s",
        (workspace_id,),
    ).fetchall():
        rows[("page", str(row["id"]))] = {key: row[key] for key in (*_PAGE_FIELDS, "markdown_uri", "updated_at")}
    for row in conn.execute(
        "SELECT page_id,entry_id,name,parent_entry_id,definition,block_refs,slugs,aliases,tags "
        "FROM wiki_page_entries WHERE workspace_id=%s",
        (workspace_id,),
    ).fetchall():
        rows[("entry", str(row["page_id"]), str(row["entry_id"]))] = _jsonable(
            {key: row[key] for key in ("name", "parent_entry_id", "definition", "block_refs", "slugs", "aliases", "tags")}
        )
    for row in conn.execute(
        "SELECT from_page_id,link_type,to_page_id,label,confidence FROM wiki_page_links WHERE workspace_id=%s",
        (workspace_id,),
    ).fetchall():
        rows[("link", str(row["from_page_id"]), str(row["link_type"]), str(row["to_page_id"]))] = {
            "label": row["label"], "confidence": row["confidence"],
        }
    for row in conn.execute(
        "SELECT page_id,target_slug,entry_id,parent_entry_id,name FROM wiki_page_entry_references WHERE workspace_id=%s",
        (workspace_id,),
    ).fetchall():
        key = ("reference", str(row["page_id"]), str(row["target_slug"]), str(row["entry_id"]), str(row["parent_entry_id"] or ""))
        rows[key] = {"name": row["name"]}
    for row in conn.execute(
        "SELECT from_page_id,to_page_id,present FROM wiki_source_manual_links WHERE workspace_id=%s",
        (workspace_id,),
    ).fetchall():
        rows[("manual", str(row["from_page_id"]), str(row["to_page_id"]))] = {"present": bool(row["present"])}
    for row in conn.execute(
        "SELECT document_id,wiki_page_id,relation_type,confidence FROM document_wiki_links WHERE workspace_id=%s",
        (workspace_id,),
    ).fetchall():
        key = ("document_link", str(row["document_id"]), str(row["wiki_page_id"]), str(row["relation_type"]))
        rows[key] = {"confidence": row["confidence"]}
    for row in conn.execute(
        "SELECT page_id,alias,origin FROM wiki_concept_identity_aliases WHERE workspace_id=%s",
        (workspace_id,),
    ).fetchall():
        rows[("alias", str(row["page_id"]), str(row["alias"]))] = {"origin": row["origin"]}
    for row in conn.execute(
        "SELECT block_id,text,position FROM source_blocks WHERE document_id=%s", (document_id,),
    ).fetchall():
        rows[("block", str(row["block_id"]))] = {"text": row["text"], "position": row["position"]}
    if conn.execute(
        "SELECT 1 FROM wiki_source_tombstones WHERE workspace_id=%s AND document_id=%s",
        (workspace_id, document_id),
    ).fetchone():
        rows[("tombstone", document_id)] = {}
    return rows


def _key_pages(key: tuple[str, ...]) -> list[str]:
    return [key[index] for index in _PAGE_POSITIONS[key[0]]]


def _describe_ingest(
    conn,
    *,
    run: dict[str, Any],
    submissions: list[dict[str, Any]],
    manifest: dict[str, Any],
    before: dict[tuple[str, ...], dict[str, Any]],
    after: dict[tuple[str, ...], dict[str, Any]],
    written: dict[str, str],
    operation_id: str,
) -> dict[str, Any]:
    """웹 편입 반영 전·후 공개 상태로 미리보기를 만든다. 미리보기와 적용이 같은 함수로 같은 해시를 낸다."""
    workspace_id = str(run["workspace_id"])
    document_id = str(run["document_id"])
    bodies: dict[str, str] = {}

    def body(row: dict[str, Any] | None) -> str | None:
        if row is None:
            return None
        uri = str(row.get("markdown_uri") or "")
        if uri not in bodies:
            text = written[uri] if uri in written else _body({"id": "", "markdown_uri": uri})
            # 웹 본문은 근거 id에 작업 id를 싣는다. 미리보기(draft id)와 적용(document 작업 id)이 같은 해시를 내게 자리표시로 바꾼다.
            bodies[uri] = text.replace(operation_id, "{operation_id}")
        return bodies[uri]

    def text_hash(value: str | None) -> str | None:
        return None if value is None else "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    changed = []
    for key in sorted(before.keys() | after.keys()):
        old, new = before.get(key), after.get(key)
        if key[0] == "page" and old is not None and new is not None:
            if all(old[field] == new[field] for field in _PAGE_FIELDS) and (
                old["markdown_uri"] == new["markdown_uri"] or body(old) == body(new)
            ):
                continue
        elif old == new:
            continue
        changed.append(key)

    new_ids = {
        key[1]: f"new:{row['page_type']}:{row['slug']}"
        for key, row in after.items() if key[0] == "page" and key not in before
    }

    def public_key(key: tuple[str, ...]) -> list[str]:
        positions = _PAGE_POSITIONS[key[0]]
        return [new_ids.get(part, part) if index in positions else part for index, part in enumerate(key)]

    def content(key: tuple[str, ...], row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        if key[0] == "page":
            return {**{field: row[field] for field in _PAGE_FIELDS}, "content_hash": text_hash(body(row))}
        return _jsonable(row)

    # 새 페이지 id는 실행마다 달라 정렬도 slug로 바꾼 key로 한다.
    effect = sorted([public_key(key), content(key, after.get(key))] for key in changed)
    scope = {page_id for key in changed for page_id in _key_pages(key) if ("page", page_id) in before}
    public_rows = []
    for key, row in sorted(before.items()):
        pages = _key_pages(key)
        if pages and not set(pages) & scope:
            continue
        if key[0] == "page":
            row = {**_page_snapshot({**row, "id": key[1]}), "title": row["title"], "summary": row["summary"]}
            row.pop("body")
        public_rows.append([list(key), _jsonable(row)])
    public_state = {"rows": public_rows}
    public_state_hash = _hash(public_state)
    target_pages = [
        {"page_id": key[1], "version": _version(row["updated_at"]), "content_hash": text_hash(body(row))}
        for key, row in sorted(before.items())
        if key[0] == "page" and key[1] in scope and row["status"] == "active"
    ]

    changes: list[dict[str, Any]] = []
    for key in changed:
        old, new = before.get(key), after.get(key)
        page_key = public_key(key)
        if key[0] == "page":
            row = new or old
            if row["page_type"] == "source":
                changes.append(_change(
                    "SOURCE_PAGE", "UPDATE" if old is not None else "ADD", page_key[1], document_id, body(old), body(new),
                ))
            elif old is not None and old["status"] == "active" and (new is None or new["status"] != "active"):
                changes.append(_change("PAGE_DELETE", "DELETE", page_key[1], page_key[1], old["title"], None))
            elif old is not None and old["status"] != "active" and new is not None and new["status"] == "active":
                for manual_key, manual in sorted(after.items()):
                    if manual_key[0] == "manual" and key[1] in manual_key[1:] and manual["present"]:
                        changes.append(_change(
                            "REVIVAL_LINK", "REVIVE", page_key[1], ":".join(public_key(manual_key)[1:]), None,
                            _canonical_text({"from_page_id": public_key(manual_key)[1], "to_page_id": public_key(manual_key)[2], **manual}),
                        ))
        elif key[0] in {"entry", "link", "reference"}:
            kind = {"entry": "ENTRY", "link": "LINK", "reference": "REFERENCE"}[key[0]]
            action = "ADD" if old is None else "REMOVE" if new is None else "UPDATE"
            changes.append(_change(
                kind, action, page_key[1], ":".join(page_key[2:]),
                _canonical_text(_jsonable(old)) if old is not None else None,
                _canonical_text(_jsonable(new)) if new is not None else None,
            ))

    # 제출 문장마다 반영된 페이지. 정규화한 근거는 제출 문장 순서와 같다(build_ingest_manifest가 확인한다).
    statements = [statement for concept in _combined_concepts(submissions) for statement in concept["statements"]]
    concept_page = {
        str(row["slug"]): key[1] for key, row in after.items()
        if key[0] == "page" and row["page_type"] == "concept" and row["status"] == "active"
    }
    for statement, evidence in zip(statements, manifest["normalized"]["evidence_units"], strict=True):
        slugs = evidence.get("related_concept_slugs") or []
        if not slugs or str(slugs[0]) not in concept_page:
            raise PreviewCalculationError("submitted statement did not land on a concept page")
        page_id = new_ids.get(concept_page[str(slugs[0])], concept_page[str(slugs[0])])
        changes.append(_change(
            "CONCEPT_STATEMENT", "ADD", page_id, statement["statement_id"], None, statement["text"], statement["evidence"],
        ))

    # 이번 편입이 남긴 개념 기여. 기존 페이지는 원장 재생으로 빠지는 문장을 보이고, 원장이 없으면 막는다.
    contributions: dict[str, dict[str, Any]] = {}
    suffix = f"/ops/{operation_id}.json"
    for uri, text in written.items():
        if uri.endswith(suffix):
            page_id = uri.split("/pages/", 1)[1].split("/", 1)[0]
            contributions[page_id] = json.loads(text)
    invalidated = list(manifest["source_block_changes"]["invalidated_block_ids"])
    existing = sorted(
        page_id for page_id in contributions
        if before.get(("page", page_id), {}).get("status") == "active"
        and before[("page", page_id)]["page_type"] == "concept"
    )
    # 원장은 이번 반영 전 상태로 읽는다. 이 트랜잭션이 남긴 대기 기여가 보이지 않는 다른 연결을 쓴다.
    with connect_ai() as committed:
        ledger = _load_reconciled(committed, workspace_id, existing)
    for page_id in existing:
        kept = sorted(
            (item for item in ledger if str(item.get("page_id")) == page_id and item.get("active")),
            key=lambda item: int(item.get("sequence_revision") or 0),
        )
        if not kept:
            raise PreviewCalculationError("active concept contribution ledger is missing")
        try:
            loaded = load_concept_contributions(
                workspace_id=workspace_id, page_id=page_id,
                keep_contributions=[
                    {"operation_id": item["ingest_operation_id"], "sequence": item["sequence_revision"]} for item in kept
                ],
                read_text=read_text_object,
            )
        except ConceptContributionError as exc:
            raise PreviewCalculationError("concept contribution replay failed") from exc
        changes.extend(_removed_statement_changes(page_id, loaded, contributions[page_id]))
    for page_id in sorted(contributions):
        for block_id in invalidated:
            changes.append(_change(
                "EVIDENCE_INVALIDATION", "REMOVE", new_ids.get(page_id, page_id), block_id, f"{document_id}:{block_id}", None,
            ))

    changes = sorted({item["change_id"]: item for item in changes}.values(), key=lambda item: item["change_id"])
    source_snapshots = [{
        "snapshot_id": str(run["snapshot_id"]),
        "document_id": document_id,
        "source_version": int(run["source_version"]),
        "source_content_hash": str(run["source_content_hash"]),
    }]
    draft_hash = _hash({
        "draft_kind": "INGEST",
        "source_snapshots": source_snapshots,
        "target_pages": target_pages,
        "normalized_submissions": submissions,
        "changes": changes,
        "public_state_hash": public_state_hash,
        # 보이지 않는 반영(개념 본문·문서 연결·별칭·블록)도 승인에 묶는다.
        "effect": effect,
    })
    preview = {
        "schema_version": "mcp.wiki-preview.v1",
        "draft_id": str(run["draft_id"]),
        "draft_kind": "INGEST",
        "draft_version": 1,
        "draft_hash": draft_hash,
        "source_snapshots": source_snapshots,
        "target_pages": target_pages,
        "public_state_hash": public_state_hash,
        "changes": changes,
        "complete": True,
    }
    return {
        "preview": preview,
        "normalized_submissions": submissions,
        "calculation_inputs": {
            "public_state": public_state,
            "effect": effect,
            "invalidated_block_ids": invalidated,
        },
    }


def publish_ingest(
    conn,
    *,
    run: dict[str, Any],
    submissions: list[dict[str, Any]],
    source_blocks: list[dict[str, Any]],
    operation_id: str,
    artifact_writer: Callable[[str, str, str], str],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """웹 편입 반영(`apply_ingest_outputs`)을 그대로 실행하고 전·후 공개 상태로 미리보기를 계산한다.

    호출자가 개념 잠금과 트랜잭션을 잡는다. finalize는 savepoint 안에서 실행한 뒤 되돌리고(`preview_ingest`),
    승인 적용은 실제로 반영한 뒤 같은 계산의 해시를 승인값과 맞춘다.
    돌려주는 값: 계산 결과, 임베딩할 페이지 id, manifest.
    """
    workspace_id = str(run["workspace_id"])
    document_id = str(run["document_id"])
    try:
        current_blocks, previous_max = load_source_block_registry(conn, document_id)
        manifest = build_ingest_manifest(
            run=run,
            submissions=submissions,
            source_blocks=_with_snapshot_lines(conn, run, source_blocks),
            current_source_blocks=current_blocks,
            concept_index=list_active_concept_index(workspace_id, use_cache=False, include_deleted=True),
            operation_id=operation_id,
            previous_max_block_number=previous_max,
        )
        manual_source_markdown = active_manual_source_markdown(document_id, manifest)
        before = _public_rows(conn, workspace_id, document_id)
        written: dict[str, str] = {}

        def writer(key: str, text: str, content_type: str) -> str:
            uri = artifact_writer(key, text, content_type)
            written[storage_uri(key)] = text
            return uri

        page_ids = apply_ingest_outputs(
            conn,
            document_id=document_id,
            workspace_id=workspace_id,
            ingest_run_id=None,
            manifest=manifest,
            manual_source_markdown=manual_source_markdown,
            artifact_writer=writer,
        )
        after = _public_rows(conn, workspace_id, document_id)
        calculation = _describe_ingest(
            conn, run=run, submissions=submissions, manifest=manifest,
            before=before, after=after, written=written, operation_id=operation_id,
        )
    except (PreviewCalculationError, McpWikiDraftError, ImmutableObjectConflict, PublicationConflict):
        # 발행 객체 충돌은 대사 실패가 아니다. 그대로 올려 적용이 미확정(APPLYING)으로 남게 한다.
        raise
    except (RuntimeError, ConceptContributionError) as exc:
        # 원장·대기 기여 대사 실패(document-svc 조회, 대기 기여 모순). 웹 반영이 막힌 상태라 미리보기를 만들지 않는다.
        raise PreviewCalculationError("contribution ledger could not be reconciled") from exc
    return calculation, page_ids, manifest


def _with_snapshot_lines(conn, run: dict[str, Any], source_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """창 블록에는 줄 범위가 없다. 시작 시점 스냅샷에 저장한 줄 범위·블록 종류를 붙인다."""
    lines = {
        str(row["block_id"]): row for row in conn.execute(
            "SELECT block_id,line_start,line_end,block_type FROM mcp_wiki_source_blocks WHERE snapshot_id=%s",
            (str(run["snapshot_id"]),),
        ).fetchall()
    }
    return [
        {**item, **{key: lines.get(str(item["block_id"]), {}).get(key) for key in ("line_start", "line_end", "block_type")}}
        for item in source_blocks
    ]


class _DryRun(Exception):
    def __init__(self, calculation: dict[str, Any]) -> None:
        self.calculation = calculation


def preview_ingest(
    conn,
    *,
    run: dict[str, Any],
    submissions: list[dict[str, Any]],
    source_blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    """finalize 미리보기. 웹 편입 반영을 savepoint 안에서 실행해 본 뒤 되돌린다. 객체 저장소에는 쓰지 않는다."""
    try:
        with conn.transaction():
            calculation, _page_ids, _manifest = publish_ingest(
                conn, run=run, submissions=submissions, source_blocks=source_blocks,
                operation_id=str(run["draft_id"]),
                artifact_writer=lambda key, _text, _content_type: storage_uri(key),
            )
            raise _DryRun(calculation)
    except _DryRun as dry_run:
        return dry_run.calculation
