from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.core.ai_markdown_sanitizer import external_urls, sanitize_ai_markdown
from app.modules.wiki_generation.domain.text_utils import clean_concept_tags
from app.modules.wiki_generation.infrastructure.assemble import ConceptPageAssembler
from app.modules.wiki_ingestion.domain.concept_entries import (
    render_entry_index,
    replay_concept_entries,
)
from app.modules.wiki_ingestion.domain.contribution_identity import (
    CONCEPT_REFERENCE_FIELDS,
    globalize_contribution_identity,
)
from app.modules.wiki_ingestion.domain.operation_recovery import (
    PageContribution,
    PageRebuildError,
)
from app.modules.wiki_ingestion.domain.orphan_link_lint import (
    replay_supported_links,
)
from app.modules.wiki_ingestion.infrastructure.concept_evidence import (
    append_concept_evidence,
    concept_evidence_updates,
)


# 재편입이 무효화한 블록만 기록하는 기여(내용 없음). 이 페이지에 새 근거가 없어도 옛 근거를 재생에서 빼게 한다.
INVALIDATION = "invalidation"


class ConceptContributionError(PageRebuildError):
    pass


@dataclass(frozen=True)
class RebuiltConceptPage:
    page_id: str
    markdown: str
    operation_ids: tuple[str, ...]
    supported_links: tuple[dict[str, Any], ...]
    # 수동 기여가 있으면 None이다. 이름은 rename으로만 바꾸므로 복구가 덮어쓰지 않는다.
    title: str | None
    summary: str | None
    source_document_ids: tuple[str, ...]
    # 기여 재생으로 남은 하위 항목. 이 페이지가 집이다(ADR 0042).
    entries: tuple[dict[str, Any], ...]
    # 재생 결과 페이지가 없다. 나머지 값은 비어 있다(ADR 0042).
    deleted: bool = False


def load_concept_contributions(
    *,
    workspace_id: str,
    page_id: str,
    keep_contributions: list[dict[str, Any]],
    read_text: Callable[[str], str],
) -> list[PageContribution]:
    loaded: list[PageContribution] = []
    for item in keep_contributions:
        operation_id = str(item["operation_id"])
        key = (
            f"wiki/{workspace_id}/pages/{page_id}/ops/"
            f"{operation_id}.json"
        )
        try:
            contribution = json.loads(read_text(key))
        except (KeyError, OSError, json.JSONDecodeError) as exc:
            raise ConceptContributionError(
                f"failed to read concept contribution JSON: {key}"
            ) from exc
        if (
            contribution.get("operation_id") != operation_id
            or contribution.get("page_id") != page_id
        ):
            raise ConceptContributionError(
                f"concept contribution identity does not match: {key}"
            )
        markdown_key = f"wiki/{workspace_id}/pages/{page_id}/ops/{operation_id}.md"
        if contribution.get("artifact_type") == "manual":
            try:
                contribution["markdown"] = read_text(markdown_key)
            except (KeyError, OSError) as exc:
                raise ConceptContributionError(
                    f"failed to read manual contribution markdown: {markdown_key}"
                ) from exc
        loaded.append(
            PageContribution(
                page_id=page_id,
                page_type="concept",
                operation_id=operation_id,
                sequence=int(item["sequence"]),
                markdown_key=markdown_key,
                contribution=contribution,
            )
        )
    return loaded


def rebuild_concept_page(
    contributions: tuple[PageContribution, ...] | list[PageContribution],
) -> RebuiltConceptPage:
    ordered = sorted(contributions, key=lambda item: item.sequence)
    if not ordered or any(
        item.page_type != "concept" or item.contribution is None
        for item in ordered
    ):
        raise ConceptContributionError(
            "concept contribution JSON is required to rebuild a concept page"
        )

    page_id = ordered[0].page_id
    # 다시 합치기는 자식에 삭제 기록(lint `content_action: "delete"`)을 남긴다(ADR 0042). 삭제는 그때까지의 기록을
    # 모두 지운 것과 같고, 뒤에 기록이 없으면 페이지가 없다. 삭제 기록을 되돌리면 앞 기록이 다시 재생된다.
    last_delete = max((index for index, item in enumerate(ordered) if _is_delete(item)), default=-1)
    ordered = ordered[last_delete + 1:]
    if not ordered:
        return _deleted_page(page_id)
    manual = [item for item in ordered if _is_manual(item)]
    generated = [item for item in ordered if not _is_manual(item)]
    # 페이지 identity는 page_id다. 분리로 자식 페이지에 이어 붙인 원래 편입 기록은
    # 부모 개념의 slug를 가지거나 개념 없이 하위 항목만 가질 수 있다(ADR 0042).
    if any(item.page_id != page_id for item in ordered):
        raise ConceptContributionError(
            "all contributions must belong to the same concept page"
        )

    artifacts = [
        item.contribution if _is_manual(item) else globalize_contribution_identity(item.contribution)
        for item in ordered
        if item.contribution is not None
    ]
    artifacts, stale_by_operation = _without_invalidated(artifacts)
    contribution_json = [item for item in artifacts if item.get("artifact_type") != "manual"]
    original_json = [
        globalize_contribution_identity(item.contribution)
        for item in generated
        if item.contribution is not None and not _is_invalidation(item.contribution)
    ]
    # 개념도 항목 변경도 없는 기록은 깨진 기록이다. 근거 없음으로 보고 페이지를 지우지 않는다.
    if any(not (item.get("concept") or {}).get("slug") and "entry_changes" not in item for item in contribution_json):
        raise ConceptContributionError("concept contribution has neither concept nor entry changes")
    # 링크는 수동 기여의 추가·삭제까지 적용 순서대로 재생해야 사람이 지운 edge가 되살아나지 않는다.
    supported_links = tuple(replay_supported_links(artifacts))
    entries = tuple(replay_concept_entries(artifacts))
    # 남은 유효 근거가 없으면 페이지도 없다. lint 메타데이터(분리·링크 기록)만으로 존속시키지 않는다(ADR 0042 결정 3).
    if not (manual or entries or any(
        _supports_page(before, after) for before, after in zip(original_json, contribution_json, strict=True)
    )):
        return _deleted_page(page_id)
    # 다른 페이지로 옮겼거나 지운 하위 항목의 근거는 본문에 두지 않는다. 옮긴 근거는 새 집이 보인다(ADR 0042).
    entry_claims = {
        claim_id
        for item in contribution_json
        for change in item.get("entry_changes", [])
        if change.get("op") == "add"
        for claim_id in change.get("evidence_claim_ids", [])
    }
    dropped_claims = entry_claims - {claim_id for entry in entries for claim_id in entry["evidence_claim_ids"]}
    if dropped_claims:
        contribution_json = [
            {
                **item,
                "evidence_units": [
                    unit for unit in item.get("evidence_units", []) if unit.get("evidence_id") not in dropped_claims
                ],
            }
            for item in contribution_json
        ]
    # 그 항목들만 인용한 블록의 핵심 내용과 개념 인용도 뺀다. 남은 항목·근거와 같이 쓰는 블록은 남긴다.
    entry_refs = {
        ref
        for item in contribution_json
        for change in item.get("entry_changes", [])
        if change.get("op") == "add"
        for ref in change.get("anchor_reference_ids", [])
    }
    kept_refs = {ref for entry in entries for ref in entry["anchor_reference_ids"]} | {
        ref
        for item in contribution_json
        for unit in item.get("evidence_units", [])
        for ref in unit.get("anchor_reference_ids", [])
    }
    dropped_refs = entry_refs - kept_refs
    if dropped_refs or dropped_claims:
        contribution_json = [_without_moved_citations(item, dropped_refs, dropped_claims) for item in contribution_json]
    # 문서 연결(그리고 소스 → 개념 자동 링크)은 최종 생존 근거로 정한다(ADR 0042 결정 5).
    source_document_ids = tuple(_source_document_ids(original_json, contribution_json, entries))
    if manual:
        # 사람이 마지막으로 고친 본문을 기준으로 삼는다. 그 전 AI 기여는 사람이 본 본문에 이미
        # 들어 있으므로, 그 뒤 AI 기여의 근거만 재편입과 같은 방식으로 덧붙인다.
        base = manual[-1]
        updates = [
            update
            for item in generated
            if item.sequence > base.sequence and item.contribution is not None
            for update in concept_evidence_updates(
                _without_stale_units(item.contribution, stale_by_operation.get(item.operation_id, set()))
            )
        ]
        markdown = str(base.contribution["markdown"])
        if updates:
            # 사람이 쓴 외부 링크는 남기고 AI가 덧붙인 근거의 외부 링크만 무력화한다.
            markdown = sanitize_ai_markdown(
                append_concept_evidence(markdown, updates),
                keep_urls=external_urls(markdown),
            )
        return RebuiltConceptPage(
            page_id=page_id,
            # 하위 항목 목록은 데이터로 그린다. 사람이 고친 목록은 남지 않는다.
            markdown=render_entry_index(markdown, list(entries), list(supported_links)),
            operation_ids=tuple(item.operation_id for item in ordered),
            supported_links=supported_links,
            title=None,
            summary=None,
            source_document_ids=source_document_ids,
            entries=entries,
        )

    if not any(item["concept"].get("slug") for item in contribution_json):
        raise ConceptContributionError(
            "concept rebuild requires a contribution with a concept"
        )
    normalized = _merge_contributions(contribution_json)
    pages = ConceptPageAssembler().build_top(
        normalized,
        top_n=None,
        source_key_points=_merge_source_key_points(contribution_json),
    )
    if len(pages) != 1:
        raise ConceptContributionError("concept rebuild must produce exactly one page")

    concept = normalized["concept_ledger"][0]

    return RebuiltConceptPage(
        page_id=page_id,
        markdown=render_entry_index(
            str(pages[0]["markdown"]),
            list(entries),
            list(supported_links),
        ),
        operation_ids=tuple(item.operation_id for item in ordered),
        supported_links=supported_links,
        title=str(concept.get("title") or ""),
        summary=str(
            concept.get("definition")
            or concept.get("why_page_worthy")
            or ""
        ),
        source_document_ids=source_document_ids,
        entries=entries,
    )


def _without_moved_citations(
    contribution: dict[str, Any],
    dropped_refs: set[str],
    dropped_claims: set[str],
) -> dict[str, Any]:
    concept = dict(contribution["concept"])
    for field in ("anchor_reference_ids", "mention_reference_ids", "display_reference_ids"):
        concept[field] = [ref for ref in concept.get(field, []) if ref not in dropped_refs]
    concept["evidence_claim_ids"] = [
        claim_id for claim_id in concept.get("evidence_claim_ids", []) if claim_id not in dropped_claims
    ]
    return {
        **contribution,
        "concept": concept,
        "source_key_points": [
            item
            for item in contribution.get("source_key_points", [])
            if not (item.get("anchor_reference_ids") and set(item["anchor_reference_ids"]) <= dropped_refs)
        ],
    }


def _deleted_page(page_id: str) -> RebuiltConceptPage:
    return RebuiltConceptPage(
        page_id=page_id, markdown="", operation_ids=(), supported_links=(), title=None, summary=None,
        source_document_ids=(), entries=(), deleted=True,
    )


def _supports_page(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """페이지를 받치는 기록: 근거 단위가 있거나, lint가 아닌 기록의 직접 개념 기여다.
    인용이 있던 개념이 무효화로 인용을 모두 잃으면 받치지 않는다. 인용 없이 이름만 나온 개념은 받친다."""
    if after.get("evidence_units"):
        return True
    concept = after.get("concept") or {}
    if after.get("artifact_type") == "lint" or not concept.get("slug"):
        return False
    return bool(_concept_citations(concept)) or not _concept_citations(before.get("concept") or {})


def _concept_citations(concept: dict[str, Any]) -> list[str]:
    return [*concept.get("anchor_reference_ids", []), *concept.get("evidence_claim_ids", [])]


def _is_invalidation(contribution: dict[str, Any]) -> bool:
    return contribution.get("artifact_type") == INVALIDATION


def _without_invalidated(
    artifacts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
    """뒤 기록이 무효화한 블록(재편입의 수정·삭제 블록, `invalidated_reference_ids`)에 근거한 앞 기록 내용을 뺀다.

    이전 기록은 끄지 않으므로 그대로인 블록의 근거는 남는다. 근거 단위·핵심 내용은 앵커 하나라도 무효화되면 뺀다
    (옛 원문에서 나온 주장이다). 개념·하위 항목 인용은 무효화된 블록과 뺀 근거만 덜어 내고, 인용을 모두 잃은 하위 항목
    추가는 뺀다. 무효화 전용 기록은 재생 내용이 없어 결과 목록에서 뺀다. 사람이 쓴 기록은 손대지 않는다.
    """
    stale_by_operation: dict[str, set[str]] = {}
    later: set[str] = set()
    for item in reversed(artifacts):
        if later and item.get("artifact_type") != "manual":
            stale_by_operation[str(item.get("operation_id") or "")] = set(later)
        later |= set(item.get("invalidated_reference_ids", []))
    if not stale_by_operation:
        return [item for item in artifacts if not _is_invalidation(item)], {}
    dropped_claims = {
        str(unit.get("evidence_id"))
        for item in artifacts
        for unit in item.get("evidence_units", [])
        if isinstance(unit, dict)
        and stale_by_operation.get(str(item.get("operation_id") or ""), set()) & set(unit.get("anchor_reference_ids", []))
    }
    return [
        _without_stale(item, stale_by_operation.get(str(item.get("operation_id") or ""), set()), dropped_claims)
        for item in artifacts
        if not _is_invalidation(item)
    ], stale_by_operation


def _without_stale(contribution: dict[str, Any], stale: set[str], dropped_claims: set[str]) -> dict[str, Any]:
    if contribution.get("artifact_type") == "manual" or not (stale or dropped_claims):
        return contribution
    concept = dict(contribution.get("concept") or {})
    for field in CONCEPT_REFERENCE_FIELDS:
        concept[field] = [ref for ref in concept.get(field, []) if ref not in stale]
    concept["evidence_claim_ids"] = [
        claim_id for claim_id in concept.get("evidence_claim_ids", []) if claim_id not in dropped_claims
    ]
    result = {
        **contribution,
        "concept": concept,
        "evidence_units": [
            unit for unit in contribution.get("evidence_units", [])
            if str(unit.get("evidence_id")) not in dropped_claims
        ],
        "source_key_points": [
            item for item in contribution.get("source_key_points", [])
            if not stale & set(item.get("anchor_reference_ids", []))
        ],
    }
    if "entry_changes" in contribution:
        changes = []
        for change in contribution["entry_changes"]:
            if change.get("op") != "add":
                changes.append(change)
                continue
            refs = [ref for ref in change.get("anchor_reference_ids", []) if ref not in stale]
            claims = [claim_id for claim_id in change.get("evidence_claim_ids", []) if claim_id not in dropped_claims]
            cited = change.get("anchor_reference_ids") or change.get("evidence_claim_ids")
            if cited and not refs and not claims:
                continue
            changes.append({**change, "anchor_reference_ids": refs, "evidence_claim_ids": claims})
        result["entry_changes"] = changes
    return result


def _without_stale_units(contribution: dict[str, Any], stale: set[str]) -> dict[str, Any]:
    """수동 본문 뒤 AI 기록의 근거 중 무효화된 블록에 근거한 것을 뺀다. 원래(로컬) 형식은 그대로 둔다."""
    if not stale:
        return contribution
    document_id = str(contribution.get("document_id") or "")
    return {
        **contribution,
        "evidence_units": [
            unit for unit in contribution.get("evidence_units", [])
            if not stale & {
                ref if ":" in str(ref) else f"{unit.get('source_document_id') or document_id}:{ref}"
                for ref in unit.get("anchor_reference_ids", [])
            }
        ],
    }


def _is_delete(item: PageContribution) -> bool:
    contribution = item.contribution or {}
    return contribution.get("artifact_type") == "lint" and contribution.get("content_action") == "delete"


def _is_manual(item: PageContribution) -> bool:
    return (item.contribution or {}).get("artifact_type") == "manual"


def _merge_contributions(
    contributions: list[dict[str, Any]],
) -> dict[str, Any]:
    concepts = [item["concept"] for item in contributions if item["concept"].get("slug")]
    merged_concept = dict(concepts[0])
    list_fields = (
        "aliases",
        "anchor_reference_ids",
        "mention_reference_ids",
        "display_reference_ids",
        "source_document_ids",
        "evidence_claim_ids",
    )
    for concept in concepts[1:]:
        for key, value in concept.items():
            if key not in list_fields and value not in (None, "", []):
                merged_concept[key] = value
    for field in list_fields:
        merged_concept[field] = _unique(
            value
            for concept in concepts
            for value in concept.get(field, [])
        )
    merged_concept["tags"] = clean_concept_tags(
        (tag for concept in concepts for tag in concept.get("tags", [])),
        merged_concept.get("title"),
        merged_concept["aliases"],
    )

    # 기여의 근거는 그 개념의 주장(`evidence_claim_ids`)으로 고른 것이다. 조립기는 `related_concept_slugs`로 근거를 고르므로
    # 이 페이지 개념을 더해, 편입이 덧붙여 보이던 근거가 재생에서 빠지지 않게 한다.
    slug = merged_concept["slug"]
    evidence_units = [
        {**item, "related_concept_slugs": _unique([*item.get("related_concept_slugs", []), slug])}
        for item in _unique_dicts(
            item
            for contribution in contributions
            for item in contribution.get("evidence_units", [])
            if isinstance(item, dict)
        )
    ]
    document_ids = merged_concept.get("source_document_ids", [])
    if not document_ids:
        document_ids = _unique(
            item.get("source_document_id")
            for item in evidence_units
            if item.get("source_document_id")
        )
    document_id = str(
        document_ids[0]
        if document_ids
        else contributions[0].get("document_id") or ""
    )
    return {
        "document": {
            "document_id": document_id,
            "title": str(merged_concept.get("title") or ""),
        },
        "semantic_notes": [],
        "concept_ledger": [merged_concept],
        "categories": [],
        "section_candidates": [],
        "mentions": [],
        "observations": [],
        "evidence_units": evidence_units,
        "missing_related_concept_hints": [],
        "warnings": [],
    }


def _source_document_ids(
    original: list[dict[str, Any]],
    surviving: list[dict[str, Any]],
    entries: tuple[dict[str, Any], ...],
) -> list[str]:
    """재생 결과 이 페이지를 받치는 문서. 소스 → 개념 자동 링크도 이 목록에서 파생한다(ADR 0042 결정 5).

    - 직접 개념 기여: lint가 아닌 기록의 개념. 인용이 모두 다른 페이지로 옮겨 간 개념(하위 개념만 그 문서와 관계있음)은 뺀다.
      인용 없이 이름만 나온 개념은 센다.
    - 남은 근거 단위의 문서와 남은 하위 항목 근거 블록(`문서ID:블록ID`)의 문서. lint 승격·병합 근거도 센다.
    옮긴 근거는 새 집 페이지가 센다. 취소한 기록은 재생에 없으므로 세지 않는다.
    """
    document_ids: list[str] = []
    for before, after in zip(original, surviving, strict=True):
        concept = after.get("concept") or {}
        if after.get("artifact_type") in (None, "ingest", "document") and concept.get("slug"):
            cited = [*concept.get("anchor_reference_ids", []), *concept.get("evidence_claim_ids", [])]
            was_cited = [
                *(before.get("concept") or {}).get("anchor_reference_ids", []),
                *(before.get("concept") or {}).get("evidence_claim_ids", []),
            ]
            if cited or not was_cited:
                document_ids.extend(
                    str(document_id) for document_id in concept.get("source_document_ids", []) if document_id
                )
                if not concept.get("source_document_ids") and after.get("document_id"):
                    document_ids.append(str(after["document_id"]))
        document_ids.extend(
            str(unit["source_document_id"])
            for unit in after.get("evidence_units", [])
            if isinstance(unit, dict) and unit.get("source_document_id")
        )
    document_ids.extend(
        str(ref).split(":", 1)[0] for entry in entries for ref in entry.get("anchor_reference_ids", []) if ":" in str(ref)
    )
    return _unique(document_id for document_id in document_ids if not document_id.startswith("lint:"))


def _merge_source_key_points(
    contributions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    key_points: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for contribution in contributions:
        for item in contribution.get("source_key_points", []):
            if not isinstance(item, dict):
                continue
            key = (
                str(item.get("text") or ""),
                tuple(str(ref) for ref in item.get("anchor_reference_ids", [])),
            )
            if key in seen:
                continue
            seen.add(key)
            key_points.append(item)
    return key_points


def _unique(values: Any) -> list[Any]:
    return list(dict.fromkeys(values))


def _unique_dicts(values: Any) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(values):
        key = str(value.get("evidence_id") or index)
        by_id[key] = value
    return list(by_id.values())
