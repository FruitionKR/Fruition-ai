"""편입 배정과 승격(ADR 0042 2단계). 이번 문서의 개념을 상위 항목과 세부 항목으로 나눠 하위 항목으로 둔다.

1. 배정: 어느 개념이 다른 개념에 딸리는지는 판정기(Jev `part_of` 또는 편입 LLM)가 정한다. 코드는 후보만 고른다.
   세부 항목마다 상위 후보는 임베딩("이름: 정의") 유사도 상위 `PART_OF_CANDIDATE_LIMIT`개이고, 이번 문서 개념과
   워크스페이스 기존 개념 페이지가 모두 후보다(기존 페이지는 상위 항목만 된다). 이름 규칙으로 배정하거나 후보를
   고르지 않는다. 임베딩을 쓸 수 없거나, 애매하거나, 판정기를 쓸 수 없으면 배정하지 않는다.
2. 승격: 배정을 따라 올라간 맨 위 항목이 기존 개념 페이지이거나 세부 항목이 `K`개 이상이면 그 페이지의 하위 항목이 된다.
   모자라면 배정을 버리고 지금처럼 각자 개념 페이지로 둔다.
3. 같은 것 찾기: 새 하위 항목마다 워크스페이스의 기존 하위 항목과 "바로 위 이름 > 이름"이 같거나 쌍 판정이 same이면
   기존 항목에 근거를 더하고, 집이 다른 페이지면 이번 페이지에는 참조를 둔다. 기존 항목과 같지 않은 새 항목끼리도
   같은 집·같은 위 항목 아래면 같은 규칙으로 먼저 나온 항목에 근거를 더한다. 애매하면 따로 둔다.
"""
from __future__ import annotations

import copy
import logging
from collections import Counter
from collections.abc import Callable
from typing import Any

from app.core.jev_client import JevUnavailable
from app.modules.wiki_generation.application.judge_candidates import PairJudge, PartOfJudge
from app.modules.wiki_generation.domain.text_utils import name_key, unique_keep_order
from app.modules.wiki_ingestion.domain.concept_entries import ENTRY_REFERENCE, new_entry_id
from app.modules.wiki_ingestion.domain.concept_split_rules import reaches_page_threshold

logger = logging.getLogger(__name__)
# 세부 항목마다 part_of로 물을 상위 후보 수. 편입 잠금 안의 판정이라 작게 둔다.
PART_OF_CANDIDATE_LIMIT = 5
# (질의 텍스트, 후보 텍스트 목록) → 후보별 임베딩 유사도. 개념 판정과 같은 BGE-M3 벡터·캐시를 쓴다.
# 임베딩 서버를 쓸 수 없으면 JevUnavailable을 낸다.
Similarity = Callable[[str, list[str]], list[float]]


def apply_concept_entries(
    resolved: dict[str, Any],
    *,
    judge_part_of: PartOfJudge | None = None,
    judge_same: PairJudge | None = None,
    similarity: Similarity | None = None,
) -> dict[str, Any]:
    """개념 판정이 끝난 정규화 결과에서 세부 항목을 개념 목록에서 빼고 집 페이지의 하위 항목으로 옮긴다.

    결과의 `entry_changes`는 집 페이지 slug별 `add` 목록, `entry_references`는 참조 링크다.
    """
    ledger = [concept for concept in resolved.get("concept_ledger", []) if concept.get("slug")]
    existing = {concept["slug"]: concept for concept in resolved.get("existing_concept_index", []) if concept.get("slug")}
    assigned = _assign_heads(ledger, existing, judge_part_of, similarity)
    placements = _place(assigned, set(existing))
    if not placements:
        return resolved
    result = copy.deepcopy(resolved)
    by_slug = {concept["slug"]: concept for concept in result["concept_ledger"]}
    title_of = {**{slug: c.get("title") or slug for slug, c in existing.items()},
                **{slug: c.get("title") or slug for slug, c in by_slug.items()}}
    pool = _existing_entries(existing)
    # 같은 것 찾기는 위 항목부터 깊이별로 한다. 위 항목이 기존 항목과 같으면 아래 항목은 그 항목 이름 아래에서 찾는다.
    depth = {detail: _depth(placements, detail) for detail in placements}
    matched: dict[str, dict[str, Any]] = {}
    # 같은 집·같은 위 항목 아래 이번 문서의 새 세부 항목끼리 같은 것. 세부 항목 slug → 먼저 나온 같은 세부 항목 slug.
    siblings: dict[str, str] = {}
    for level in sorted(set(depth.values())):
        queries = {}
        for detail in (slug for slug in placements if depth[slug] == level):
            page, parent = placements[detail]
            parent = siblings.get(parent, parent)
            parent_name = matched[parent]["name"] if parent in matched else title_of[parent or page]
            queries[detail] = {
                "slug": f"incoming:{detail}",
                "title": f"{parent_name} > {title_of[detail]}",
                "aliases": [],
                "definition": by_slug[detail].get("definition") or "",
            }
        matched.update(_match_entries(queries, pool, judge_same))
        siblings.update(_match_siblings(
            {detail: query for detail, query in queries.items() if detail not in matched}, placements, judge_same,
        ))

    document_id = str(result.get("document", {}).get("document_id") or "")
    evidence_by_id = {item.get("evidence_id"): item for item in result.get("evidence_units", [])}
    # 집은 위 항목부터 정한다. 다른 페이지 항목과 같다고 본 항목의 새 아래 항목은 그 항목의 집에 함께 둔다.
    entry_ids: dict[str, str] = {}
    home_of: dict[str, str] = {}
    entry_changes: dict[str, list[dict[str, Any]]] = {}
    references: list[dict[str, Any]] = []
    adds: dict[str, dict[str, Any]] = {}
    for detail in sorted(placements, key=lambda slug: depth[slug]):
        page, parent = placements[detail]
        concept = by_slug[detail]
        # 이 항목이 놓일 페이지와 그 페이지 안의 바로 위 항목이다.
        container = home_of[parent] if parent else page
        parent_entry_id = entry_ids[parent] if parent else None
        # 같은 형제로 본 세부 항목은 먼저 나온 항목에 근거만 더한다(이름·정의·위치는 먼저 나온 항목을 따른다).
        sibling = adds.get(siblings.get(detail, ""))
        match = matched.get(detail) or (
            {"page_slug": home_of[siblings[detail]], "slug": sibling["entry_id"], "name": sibling["name"],
             "definition": sibling["definition"], "parent_entry_id": sibling["parent_entry_id"]}
            if sibling else None
        )
        home = match["page_slug"] if match else container
        entry_ids[detail] = match["slug"] if match else new_entry_id(home, detail)
        refs = unique_keep_order([
            *concept.get("anchor_reference_ids", []),
            *(ref for evidence_id in concept.get("evidence_claim_ids", [])
              for ref in evidence_by_id.get(evidence_id, {}).get("anchor_reference_ids", [])),
        ])
        # 같은 항목이면 이름·정의·위치는 기존 항목을 따른다. 근거는 그 항목의 집에 더한다.
        add = {
            "op": "add",
            "entry_id": entry_ids[detail],
            "name": match["name"] if match else concept.get("title") or detail,
            "definition": match["definition"] if match else concept.get("definition") or "",
            "parent_entry_id": match["parent_entry_id"] if match else parent_entry_id,
            "anchor_reference_ids": refs,
            "evidence_claim_ids": list(concept.get("evidence_claim_ids", [])),
        }
        if home != container:
            references.append({
                "source": f"concept:{container}",
                "target": f"concept:{home}",
                "relation": ENTRY_REFERENCE,
                "decided_by": "concept_entry",
                "entry_id": add["entry_id"],
                "name": add["name"],
                "parent_entry_id": parent_entry_id,
            })
        entry_changes.setdefault(home, []).append(add)
        adds[detail] = add
        home_of[detail] = home
        # 이번 문서에 없던 기존 페이지는 개념 판정이 기존 개념에 병합할 때와 같게 목록에 넣는다.
        # 참조를 둔 페이지(container)도 넣어야 그 페이지 기여·본문에 참조가 남아 복구 재생이 된다.
        for slug in dict.fromkeys((home, container)):
            if slug not in by_slug:
                by_slug[slug] = _existing_concept_entry(existing[slug], document_id)
                result["concept_ledger"].append(by_slug[slug])
        target = by_slug[home]
        for field in ("anchor_reference_ids", "evidence_claim_ids"):
            target[field] = unique_keep_order([*target.get(field, []), *concept.get(field, [])])

    result["concept_ledger"] = [concept for concept in result["concept_ledger"] if concept["slug"] not in home_of]
    # 세부 항목을 가리키던 근거·연결·갱신 판정은 그 항목의 집 페이지를 가리킨다.
    slug_map = result.setdefault("concept_slug_map", {})
    for slug, target in list(slug_map.items()):
        slug_map[slug] = home_of.get(target, target)
    slug_map.update(home_of)
    for evidence in result.get("evidence_units", []):
        evidence["related_concept_slugs"] = unique_keep_order(
            [home_of.get(slug, slug) for slug in evidence.get("related_concept_slugs", [])]
        )
    for decision in result.get("concept_update_decisions", []):
        if decision.get("concept_slug") in home_of:
            decision["concept_slug"] = home_of[decision["concept_slug"]]
    result["entry_changes"] = entry_changes
    result["entry_references"] = references
    logger.info("[하위 항목 배정] 배정=%d 하위 항목=%d 같은 항목=%d 참조=%d",
                len(assigned), len(placements), len(matched), len(references))
    return result


def _assign_heads(
    ledger: list[dict[str, Any]],
    existing: dict[str, dict[str, Any]],
    judge_part_of: PartOfJudge | None,
    similarity: Similarity | None,
) -> dict[str, str]:
    """세부 항목 slug → 상위 항목 slug. 판정기의 part_of만 배정한다. 기존 개념 페이지는 세부 항목이 되지 않는다."""
    if judge_part_of is None or similarity is None:
        return {}
    heads = {slug: concept for slug, concept in {**existing, **{c["slug"]: c for c in ledger}}.items() if _has_definition(concept)}
    details = [concept for concept in ledger if concept["slug"] not in existing and concept["slug"] in heads]
    try:
        pairs = [(heads[head], detail) for detail in details for head in _candidate_heads(detail, heads, similarity)]
        decisions = judge_part_of(pairs) if pairs else {}
    except JevUnavailable as exc:
        logger.warning("[part_of 판정 생략] %s", exc)
        return {}
    part_of = {pair: item["probability"] for pair, item in decisions.items() if item.get("part_of")}
    # 서로가 서로에 딸린다는 판정은 애매하다고 보고 배정하지 않는다.
    ranked = sorted(
        ((probability, head, detail) for (head, detail), probability in part_of.items() if (detail, head) not in part_of),
        key=lambda row: (-row[0], row[2], row[1]),
    )
    assigned: dict[str, str] = {}
    for _, head, detail in ranked:
        if detail not in assigned and detail not in existing and not _creates_cycle(assigned, detail, head):
            assigned[detail] = head
    # 본문 목록 순서는 이번 문서 개념 순서를 따른다.
    return {concept["slug"]: assigned[concept["slug"]] for concept in ledger if concept["slug"] in assigned}


def _candidate_heads(detail: dict[str, Any], heads: dict[str, dict[str, Any]], similarity: Similarity) -> list[str]:
    """part_of로 물을 상위 후보. 임베딩 유사도 상위 `PART_OF_CANDIDATE_LIMIT`개다."""
    others = [slug for slug in sorted(heads) if slug != detail["slug"]]
    if not others:
        return []
    scores = similarity(_concept_text(detail), [_concept_text(heads[slug]) for slug in others])
    ranked = sorted(zip(others, scores, strict=True), key=lambda row: (-row[1], row[0]))
    return [slug for slug, _ in ranked[:PART_OF_CANDIDATE_LIMIT]]


def _concept_text(concept: dict[str, Any]) -> str:
    """임베딩 텍스트 "이름: 정의". DB 개념 인덱스는 definition, 파일 개념 인덱스는 summary에 정의를 담는다."""
    return f"{concept.get('title') or concept['slug']}: {concept.get('definition') or concept.get('summary') or ''}"


def _has_definition(concept: dict[str, Any]) -> bool:
    return bool(str(concept.get("definition") or concept.get("summary") or "").strip())


def _creates_cycle(assigned: dict[str, str], detail: str, head: str) -> bool:
    seen = set()
    while head not in seen:
        if head == detail:
            return True
        seen.add(head)
        if head not in assigned:
            return False
        head = assigned[head]
    return True


def _place(assigned: dict[str, str], existing: set[str]) -> dict[str, tuple[str, str | None]]:
    """세부 항목 slug → (집 페이지 slug, 바로 위 세부 항목 slug 또는 None).

    맨 위 항목 아래 세부 항목 수(모든 깊이)로 승격을 정한다. 깊은 세부 항목도 같은 페이지 안에서 중첩된다.
    """
    def root(slug: str) -> str:
        while slug in assigned:
            slug = assigned[slug]
        return slug

    roots = {detail: root(detail) for detail in assigned}
    counts = Counter(roots.values())
    pages = {slug for slug, count in counts.items() if slug in existing or reaches_page_threshold(count)}
    return {
        detail: (page, assigned[detail] if assigned[detail] != page else None)
        for detail, page in roots.items()
        if page in pages
    }


def _depth(placements: dict[str, tuple[str, str | None]], detail: str) -> int:
    depth, parent = 0, placements[detail][1]
    while parent:
        depth, parent = depth + 1, placements[parent][1]
    return depth


def _existing_entries(existing: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """워크스페이스 하위 항목을 "바로 위 이름 > 이름"을 제목으로 둔 판정 후보로 만든다."""
    pool = []
    for page_slug, concept in existing.items():
        entries = concept.get("entries") or []
        names = {entry["entry_id"]: entry["name"] for entry in entries}
        for entry in entries:
            parent_name = names.get(entry.get("parent_entry_id")) or concept.get("title") or page_slug
            pool.append({
                "slug": entry["entry_id"],
                "title": f"{parent_name} > {entry['name']}",
                "aliases": [],
                "definition": entry.get("definition") or "",
                "page_slug": page_slug,
                "name": entry["name"],
                "parent_entry_id": entry.get("parent_entry_id"),
            })
    return pool


def _match_entries(
    queries: dict[str, dict[str, Any]],
    pool: list[dict[str, Any]],
    judge_same: PairJudge | None,
) -> dict[str, dict[str, Any]]:
    """세부 항목 slug → 같은 기존 하위 항목. 이름 일치는 판정 없이, 나머지는 쌍 판정 same만 같은 항목으로 본다."""
    if not pool:
        return {}
    by_title: dict[str, dict[str, Any]] = {}
    for entry in pool:
        by_title.setdefault(name_key(entry["title"]), entry)
    matched = {
        detail: by_title[name_key(query["title"])]
        for detail, query in queries.items()
        if name_key(query["title"]) in by_title
    }
    rest = [query for detail, query in queries.items() if detail not in matched]
    if judge_same is None or not rest:
        return matched
    by_id = {entry["slug"]: entry for entry in pool}
    try:
        decisions = judge_same(rest, pool)
    except JevUnavailable as exc:
        logger.warning("[같은 하위 항목 판정 생략] %s", exc)
        return matched
    for detail, query in queries.items():
        decision = decisions.get(query["slug"]) or {}
        if decision.get("decision") == "merge_into" and decision.get("canonical_slug") in by_id:
            matched[detail] = by_id[decision["canonical_slug"]]
    return matched


def _match_siblings(
    queries: dict[str, dict[str, Any]],
    placements: dict[str, tuple[str, str | None]],
    judge_same: PairJudge | None,
) -> dict[str, str]:
    """같은 집 페이지·같은 위 항목 아래 이번 문서의 새 세부 항목끼리 같은 것을 찾는다(기존 항목 찾기와 같은 규칙).

    "바로 위 이름 > 이름"이 같으면 판정 없이(같은 이름은 정체성이다), 나머지는 쌍 판정 same만 같은 항목으로 본다.
    한 단계의 항목을 판정기 한 번에 묻고, 같은 형제 무리 안에서 먼저 나온 항목에 합친 판정만 쓴다(서로 합쳐지지 않게).
    애매하거나 판정기를 쓸 수 없으면 따로 둔다.
    """
    order = {detail: index for index, detail in enumerate(queries)}
    found: dict[str, str] = {}
    first_by_name: dict[tuple[Any, str], str] = {}
    for detail, query in queries.items():
        key = (placements[detail], name_key(query["title"]))
        if key in first_by_name:
            found[detail] = first_by_name[key]
        else:
            first_by_name[key] = detail
    rest = [detail for detail in queries if detail not in found]
    groups = Counter(placements[detail] for detail in rest)
    asked = [detail for detail in rest if groups[placements[detail]] > 1]
    if judge_same is None or not asked:
        return found
    try:
        decisions = judge_same([queries[detail] for detail in asked], [queries[detail] for detail in asked])
    except JevUnavailable as exc:
        logger.warning("[같은 형제 항목 판정 생략] %s", exc)
        return found
    for detail in asked:
        decision = decisions.get(queries[detail]["slug"]) or {}
        target = str(decision.get("canonical_slug") or "").removeprefix("incoming:")
        if (
            decision.get("decision") == "merge_into" and target in order and target not in found
            and placements.get(target) == placements[detail] and order[target] < order[detail]
        ):
            found[detail] = target
    return found


def _existing_concept_entry(concept: dict[str, Any], document_id: str) -> dict[str, Any]:
    return {
        "slug": concept["slug"],
        "title": concept.get("title") or concept["slug"],
        "aliases": list(concept.get("aliases") or []),
        "tags": list(concept.get("tags") or []),
        "definition": concept.get("definition") or concept.get("summary") or "",
        "why_page_worthy": concept.get("why_page_worthy") or "",
        "anchor_reference_ids": [],
        "mention_reference_ids": [],
        "display_reference_ids": [],
        "source_document_ids": [document_id] if document_id else [],
        "evidence_claim_ids": [],
        "mention_count": 0,
        "importance_score": 0.0,
    }
