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
    contribution_json = [item for item in artifacts if item.get("artifact_type") != "manual"]
    # 링크는 수동 기여의 추가·삭제까지 적용 순서대로 재생해야 사람이 지운 edge가 되살아나지 않는다.
    supported_links = tuple(replay_supported_links(artifacts))
    entries = tuple(replay_concept_entries(artifacts))
    source_document_ids = tuple(_authoritative_document_ids(contribution_json))
    if manual:
        # 사람이 마지막으로 고친 본문을 기준으로 삼는다. 그 전 AI 기여는 사람이 본 본문에 이미
        # 들어 있으므로, 그 뒤 AI 기여의 근거만 재편입과 같은 방식으로 덧붙인다.
        base = manual[-1]
        updates = [
            update
            for item in generated
            if item.sequence > base.sequence and item.contribution is not None
            for update in concept_evidence_updates(item.contribution)
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

    evidence_units = _unique_dicts(
        item
        for contribution in contributions
        for item in contribution.get("evidence_units", [])
        if isinstance(item, dict)
    )
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


def _authoritative_document_ids(
    contributions: list[dict[str, Any]],
) -> list[str]:
    document_ids: list[str] = []
    for contribution in contributions:
        if contribution.get("artifact_type") not in (None, "ingest", "document"):
            continue
        contribution_document_ids: list[str] = []
        concept = contribution.get("concept") or {}
        contribution_document_ids.extend(
            str(document_id)
            for document_id in concept.get("source_document_ids", [])
            if document_id and not str(document_id).startswith("lint:")
        )
        contribution_document_ids.extend(
            str(item["source_document_id"])
            for item in contribution.get("evidence_units", [])
            if isinstance(item, dict)
            and item.get("source_document_id")
            and not str(item["source_document_id"]).startswith("lint:")
        )
        if not contribution_document_ids:
            document_id = str(contribution.get("document_id") or "")
            if document_id and not document_id.startswith("lint:"):
                contribution_document_ids.append(document_id)
        document_ids.extend(contribution_document_ids)
    return _unique(document_ids)


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
