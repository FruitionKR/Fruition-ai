"""편입 배정과 승격(ADR 0042 2단계). 이번 문서의 개념을 상위 항목과 세부 항목으로 나눠 하위 항목으로 둔다.

1. 배정: 어느 개념이 다른 개념에 딸리는지는 판정기(Jev `part_of` 또는 편입 LLM)가 정한다. 코드는 후보만 고른다.
   상위 후보는 핵심·기존 개념과 섹션·언급을 나누어 각각 임베딩("이름: 정의") 유사도 상위
   `PART_OF_CANDIDATE_LIMIT`개를 고른다. 이번 문서 개념과 워크스페이스 기존 개념 페이지가 모두 후보다(기존 페이지는 상위 항목만 된다). 이름 규칙으로 배정하거나 후보를
   고르지 않는다. 임베딩을 쓸 수 없거나, 애매하거나, 판정기를 쓸 수 없으면 배정하지 않는다.
2. 놓기: 배정을 따라 올라간 맨 위가 기존 개념 페이지면 그 페이지, 기존 하위 항목이면 그 항목의 집 페이지 그 항목 아래,
   이번 문서 개념이면 그 개념의 새 페이지의 하위 항목이 된다. 배정은 버리지 않는다. 하위 항목이 많아지면 lint 분리 규칙이
   자식 페이지로 나눈다(세부 항목 수로 페이지 여부를 정하던 `K`는 지웠다. 작은 개념일수록 흩어졌다).
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
from app.modules.wiki_generation.domain.text_utils import name_key, strip_citation_groups, unique_keep_order
from app.modules.wiki_ingestion.domain.concept_entries import ENTRY_REFERENCE, new_entry_id

logger = logging.getLogger(__name__)
# 후보군마다 part_of로 물을 상위 후보 수. 핵심·기존 개념과 섹션·언급을 따로 추린다.
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
    consumed_terms: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """개념 판정이 끝난 정규화 결과에서 세부 항목을 개념 목록에서 빼고 집 페이지의 하위 항목으로 옮긴다.

    세부 항목은 핵심 개념·섹션 후보·언급 모두다. 섹션·언급은 맨 위가 핵심 개념이나 기존 페이지·항목일 때만 하위 항목이
    되고(배정받은 것에는 `assigned_entry`를 표시해 의미 클러스터 후보에서 뺀다), 나머지는 지금처럼 의미 클러스터로 간다.
    `consumed_terms`는 개념 갱신 판정이 기존 개념과 같다고 본 (후보 종류, slug)다. 이미 그 개념의 근거라 배정하지 않는다.
    결과의 `entry_changes`는 집 페이지 slug별 `add` 목록, `entry_references`는 참조 링크다.
    """
    ledger = [concept for concept in resolved.get("concept_ledger", []) if concept.get("slug")]
    terms = _source_terms(resolved, consumed_terms or set())
    existing = {concept["slug"]: concept for concept in resolved.get("existing_concept_index", []) if concept.get("slug")}
    # 기존 하위 항목도 상위 후보다(항목 id → 그 항목). 집 페이지의 그 항목 아래로 놓는다.
    existing_entries = {
        entry["entry_id"]: {**entry, "page_slug": page_slug}
        for page_slug, concept in existing.items()
        for entry in concept.get("entries") or []
    }
    assigned = _assign_heads([*ledger, *terms.values()], existing, existing_entries, judge_part_of, similarity)
    placements = _place(assigned, set(existing), existing_entries, set(terms))
    if not placements:
        return resolved
    result = copy.deepcopy(resolved)
    by_slug = {concept["slug"]: concept for concept in result["concept_ledger"]}
    details_of = {**by_slug, **terms}
    title_of = {**{slug: c.get("title") or slug for slug, c in existing.items()},
                **{slug: c.get("title") or slug for slug, c in details_of.items()},
                **{entry_id: entry["name"] for entry_id, entry in existing_entries.items()}}
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
                "definition": details_of[detail].get("definition") or "",
            }
        matched.update(_match_entries(queries, pool, judge_same))
        siblings.update(_match_siblings(
            {detail: query for detail, query in queries.items() if detail not in matched}, placements, judge_same,
        ))

    document_id = str(result.get("document", {}).get("document_id") or "")
    evidence_by_id = {item.get("evidence_id"): item for item in result.get("evidence_units", [])}
    # 집은 위 항목부터 정한다. 다른 페이지 항목과 같다고 본 항목의 새 아래 항목은 그 항목의 집에 함께 둔다.
    # 기존 하위 항목 아래 놓인 세부 항목의 위 항목은 그 항목 자신이다(집·id가 이미 정해져 있다).
    entry_ids: dict[str, str] = {entry_id: entry_id for entry_id in existing_entries}
    home_of: dict[str, str] = {entry_id: entry["page_slug"] for entry_id, entry in existing_entries.items()}
    entry_changes: dict[str, list[dict[str, Any]]] = {}
    references: list[dict[str, Any]] = []
    adds: dict[str, dict[str, Any]] = {}
    for detail in sorted(placements, key=lambda slug: depth[slug]):
        page, parent = placements[detail]
        concept = details_of[detail]
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
            # 원래 개념의 정체성은 항목에 남긴다. 부모 페이지 별칭·태그로 섞으면 다른 개념을 동의어로 오인한다.
            "slug": concept.get("term_slug") or detail,
            "aliases": list(concept.get("aliases") or []),
            "tags": list(concept.get("tags") or []),
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

    # 원본 집은 하나지만 통과한 모든 배정은 해당 상위 위치의 참조로 남긴다.
    for detail in placements:
        add = adds[detail]
        home = home_of[detail]
        for head in assigned[detail]:
            if head in home_of:
                container, parent_id = home_of[head], entry_ids[head]
            elif head not in terms:
                container, parent_id = head, None
            else:
                continue
            if parent_id == add["entry_id"] or (container, parent_id) == (home, add["parent_entry_id"]):
                continue
            reference = {
                "source": f"concept:{container}", "target": f"concept:{home}",
                "relation": ENTRY_REFERENCE, "decided_by": "concept_entry",
                "entry_id": add["entry_id"], "name": add["name"], "parent_entry_id": parent_id,
            }
            if reference not in references:
                references.append(reference)
            if container not in by_slug:
                by_slug[container] = _existing_concept_entry(existing[container], document_id)
                result["concept_ledger"].append(by_slug[container])

    result["concept_ledger"] = [concept for concept in result["concept_ledger"] if concept["slug"] not in home_of]
    # 세부 항목을 가리키던 근거·연결·갱신 판정은 그 항목의 집 페이지를 가리킨다.
    slug_map = result.setdefault("concept_slug_map", {})
    for slug, target in list(slug_map.items()):
        slug_map[slug] = home_of.get(target, target)
    slug_map.update({detail: home_of[detail] for detail in placements if detail not in terms})
    for evidence in result.get("evidence_units", []):
        evidence["related_concept_slugs"] = unique_keep_order(
            [home_of.get(slug, slug) for slug in evidence.get("related_concept_slugs", [])]
        )
    # 하위 항목이 된 개념을 가리키던 갱신 판정은 그 항목의 근거로 간다. 집 페이지 개념의 근거로 바꾸면 "같은 하위 항목"
    # 판정이 "같은 상위 개념" 갱신으로 둔갑한다.
    for decision in result.get("concept_update_decisions", []):
        detail = decision.get("concept_slug")
        if detail in home_of:
            decision["concept_slug"] = home_of[detail]
            decision["entry"] = {key: adds[detail][key] for key in ("entry_id", "name", "definition", "parent_entry_id")}
    # 배정받은 섹션·언급은 하위 항목이 됐다. 소스 페이지 목록·검색 단위에는 그대로 두고 의미 클러스터 후보에서만 뺀다.
    for kind, field in (("section", "section_candidates"), ("mention", "mentions")):
        for item in result.get(field, []):
            key = f"{kind}:{item.get('slug')}"
            if key in placements:
                item["assigned_entry"] = {"page_slug": home_of[key], "entry_id": entry_ids[key]}
    result["entry_changes"] = entry_changes
    result["entry_references"] = references
    logger.info("[하위 항목 배정] 배정=%d 하위 항목=%d 같은 항목=%d 참조=%d",
                len(assigned), len(placements), len(matched), len(references))
    return result


def _assign_heads(
    ledger: list[dict[str, Any]],
    existing: dict[str, dict[str, Any]],
    existing_entries: dict[str, dict[str, Any]],
    judge_part_of: PartOfJudge | None,
    similarity: Similarity | None,
) -> dict[str, list[str]]:
    """세부 항목 slug → 통과한 모든 상위 항목 slug·기존 항목 id. 순환 관계만 제외한다.

    기존 개념 페이지와 하위 항목은 세부 항목이 되지 않는다(기존 페이지를 옮기는 일은 lint 이동이 한다).
    """
    if judge_part_of is None or similarity is None:
        return {}
    # 판정기는 `definition`만 본다. 파일 개념 인덱스는 정의를 `summary`에 담으므로 옮겨 둔다.
    heads = {
        slug: {**concept, "definition": concept.get("definition") or concept.get("summary") or ""}
        for slug, concept in {**existing, **{c["slug"]: c for c in ledger}}.items()
        if _has_definition(concept)
    }
    # 기존 하위 항목은 "바로 위 이름 > 이름"을 제목으로 둔다(같은 것 찾기·lint 이동 후보와 같다).
    names = {entry_id: entry["name"] for entry_id, entry in existing_entries.items()}
    heads.update({
        entry_id: {
            "slug": entry_id,
            "title": f"{names.get(entry.get('parent_entry_id')) or existing[entry['page_slug']].get('title') or entry['page_slug']}"
                     f" > {entry['name']}",
            "aliases": list(entry.get("aliases") or []),
            "definition": entry.get("definition") or "",
        }
        for entry_id, entry in existing_entries.items()
        if str(entry.get("definition") or "").strip()
    })
    details = [concept for concept in ledger if concept["slug"] not in existing and concept["slug"] in heads]
    assigned: dict[str, list[str]] = {}
    for _, head, detail in rank_part_of(details, heads, judge_part_of, similarity):
        if detail not in existing and not _creates_cycle(assigned, detail, head):
            assigned.setdefault(detail, []).append(head)
    # 본문 목록 순서는 이번 문서 개념 순서를 따른다.
    return {concept["slug"]: assigned[concept["slug"]] for concept in ledger if concept["slug"] in assigned}


def rank_part_of(
    details: list[dict[str, Any]],
    heads: dict[str, dict[str, Any]],
    judge_part_of: PartOfJudge,
    similarity: Similarity,
) -> list[tuple[float, str, str]]:
    """판정기가 part_of라 한 (P(part_of), 상위 slug, 세부 slug)를 확률이 높은 순으로 돌려준다.

    후보군마다 임베딩 유사도 상위 `PART_OF_CANDIDATE_LIMIT`개다. 편입은 핵심·기존 개념과 섹션·언급을 나누고,
    기존 개념만 검사하는 lint는 한 후보군이다.
    서로가 서로에 딸린다는 판정은 애매하다고 보고 버린다. 임베딩 서버를 쓸 수 없으면 빈 목록이다. 판정 요청 실패는 전파한다.
    """
    try:
        pairs = [(heads[head], detail) for detail in details for head in _candidate_heads(detail, heads, similarity)]
    except JevUnavailable as exc:
        logger.warning("[part_of 판정 생략] %s", exc)
        return []
    decisions = judge_part_of(pairs) if pairs else {}
    part_of = {pair: item["probability"] for pair, item in decisions.items() if item.get("part_of")}
    return sorted(
        ((probability, head, detail) for (head, detail), probability in part_of.items() if (detail, head) not in part_of),
        key=lambda row: (-row[0], row[2], row[1]),
    )


def _candidate_heads(detail: dict[str, Any], heads: dict[str, dict[str, Any]], similarity: Similarity) -> list[str]:
    """개념 후보와 섹션·언급 후보에서 각각 임베딩 상위 5개를 고른다."""
    others = [slug for slug in sorted(heads) if slug != detail["slug"]]
    if not others:
        return []
    scores = similarity(concept_text(detail), [concept_text(heads[slug]) for slug in others])
    ranked = sorted(zip(others, scores, strict=True), key=lambda row: (-row[1], row[0]))
    # 섹션·언급의 가까운 이웃이 핵심·기존 개념을 검사 대상에서 밀어내지 않게 한다.
    candidates = []
    for is_term in (False, True):
        group = [slug for slug, _ in ranked if ("term_slug" in heads[slug]) == is_term]
        candidates.extend(group[:PART_OF_CANDIDATE_LIMIT])
    return candidates


def concept_text(concept: dict[str, Any]) -> str:
    """임베딩 텍스트 "이름: 정의". DB 개념 인덱스는 definition, 파일 개념 인덱스는 summary에 정의를 담는다."""
    return f"{concept.get('title') or concept['slug']}: {concept.get('definition') or concept.get('summary') or ''}"


def _has_definition(concept: dict[str, Any]) -> bool:
    return bool(str(concept.get("definition") or concept.get("summary") or "").strip())


def _creates_cycle(assigned: dict[str, list[str]], detail: str, head: str) -> bool:
    seen = set()
    pending = [head]
    while pending:
        head = pending.pop()
        if head == detail:
            return True
        if head in seen:
            continue
        seen.add(head)
        pending.extend(assigned.get(head, []))
    return False


def _place(
    assigned: dict[str, list[str]],
    existing: set[str],
    existing_entries: dict[str, dict[str, Any]],
    terms: set[str],
) -> dict[str, tuple[str, str | None]]:
    """원본 집을 정한다. 집에 닿지 않는 섹션 가지가 유효한 다른 배정을 가리지 않게 한다.

    상위부터 해석하므로 섹션 체인도 집에 닿는다. 나머지 배정은 호출자가 참조로 남긴다.
    """
    locations: dict[str, tuple[str, str | None] | None] = {}

    def locate(slug: str) -> tuple[str, str | None] | None:
        if slug in locations:
            return locations[slug]
        if slug in existing_entries:
            location = (existing_entries[slug]["page_slug"], None)
        elif slug in existing:
            location = (slug, None)
        else:
            location = None
            for head in assigned.get(slug, []):
                parent = locate(head)
                if parent is not None:
                    location = (parent[0], head if head != parent[0] else None)
                    break
            if location is None and slug not in terms:
                location = (slug, None)
        locations[slug] = location
        return location

    return {detail: location for detail in assigned
            if (location := locate(detail)) is not None and location[0] != detail}


def _depth(placements: dict[str, tuple[str, str | None]], detail: str) -> int:
    # 기존 하위 항목은 맨 위에 둔 것으로 센다(그 아래 세부 항목의 같은 것 찾기는 그 항목 이름 아래에서 한다).
    depth, parent = 0, placements[detail][1]
    while parent in placements:
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
                "aliases": list(entry.get("aliases") or []),
                "slugs": list(entry.get("slugs") or []),
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
    """세부 항목 slug → 같은 기존 하위 항목. 원래 개념 slug·이름 일치는 판정 없이, 나머지는 쌍 판정 same만 같은 항목으로 본다.

    원래 slug는 하위 항목이 된 개념의 정체성이다. 같은 개념을 다시 편입하면 상위가 달라져도 그 항목에 근거를 더한다.
    """
    if not pool:
        return {}
    by_slug = {slug: entry for entry in pool for slug in entry.get("slugs", [])}
    by_title: dict[str, dict[str, Any]] = {}
    for entry in pool:
        by_title.setdefault(name_key(entry["title"]), entry)
    matched = {
        detail: by_slug.get(detail) or by_title[name_key(query["title"])]
        for detail, query in queries.items()
        if detail in by_slug or name_key(query["title"]) in by_title
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
        # 먼저 나온 형제가 이미 다른 형제에 합쳐졌으면 그 최종 항목으로 잇는다(B→A, C→B면 C→A).
        while target in found:
            target = found[target]
        if (
            decision.get("decision") == "merge_into" and target in order
            and placements.get(target) == placements[detail] and order[target] < order[detail]
        ):
            found[detail] = target
    return found


# 문서마다 part_of로 물을 언급 수와, 같은 근거 블록에서 물을 언급 수. 언급은 많고 정의가 원문 발췌라 잠금 안 판정량을 묶는다.
# 근거 블록이 많은 언급부터, 같으면 문서 순서로 고른다. 넘친 언급은 지금처럼 의미 클러스터로 간다.
MENTION_DETAIL_LIMIT = 20
MENTION_DETAILS_PER_BLOCK = 2


def _source_terms(resolved: dict[str, Any], consumed: set[tuple[str, str]]) -> dict[str, dict[str, Any]]:
    """배정 세부 항목이 될 섹션 후보·언급("section:{slug}"·"mention:{slug}" → 개념 모양). 정의가 있는 것만이다.

    섹션은 근거 주장(`evidence_claim_ids`)으로, 언급은 근거 블록으로 근거를 옮긴다.
    """
    terms: dict[str, dict[str, Any]] = {}
    for item in resolved.get("section_candidates", []):
        if ("section", item.get("slug")) not in consumed and _has_definition(item):
            terms[f"section:{item['slug']}"] = _term(item, "section", item.get("title"))
    mentions = sorted(
        (item for item in resolved.get("mentions", [])
         if ("mention", item.get("slug")) not in consumed and _has_definition(item) and item.get("anchor_reference_ids")),
        key=lambda item: (-len(set(item["anchor_reference_ids"])), min(item["anchor_reference_ids"])),
    )
    per_block: Counter[str] = Counter()
    for item in mentions:
        block = min(item["anchor_reference_ids"])
        if len([key for key in terms if key.startswith("mention:")]) >= MENTION_DETAIL_LIMIT:
            break
        if per_block[block] >= MENTION_DETAILS_PER_BLOCK:
            continue
        per_block[block] += 1
        terms[f"mention:{item['slug']}"] = _term(item, "mention", item.get("name"))
    return terms


def _term(item: dict[str, Any], kind: str, title: Any) -> dict[str, Any]:
    return {
        "slug": f"{kind}:{item['slug']}",
        "term_slug": item["slug"],
        "title": str(title or item["slug"]),
        "definition": str(item.get("definition") or ""),
        "aliases": [],
        "anchor_reference_ids": list(item.get("anchor_reference_ids") or []),
        "evidence_claim_ids": list(item.get("evidence_claim_ids") or []),
    }


def _existing_concept_entry(concept: dict[str, Any], document_id: str) -> dict[str, Any]:
    # 정의·설명은 기존 페이지 본문에서 읽은 값이라 근거 표시를 뺀다(조립기가 다시 붙인다). 이번 문서는 이 개념을 직접
    # 언급하지 않았으므로 집계값(mention_count·importance_score)은 싣지 않는다. 0을 실으면 재생이 원래 값을 덮는다.
    return {
        "slug": concept["slug"],
        "title": concept.get("title") or concept["slug"],
        "aliases": list(concept.get("aliases") or []),
        "tags": list(concept.get("tags") or []),
        "definition": strip_citation_groups(concept.get("definition") or concept.get("summary") or ""),
        "why_page_worthy": strip_citation_groups(concept.get("why_page_worthy") or ""),
        "anchor_reference_ids": [],
        "mention_reference_ids": [],
        "display_reference_ids": [],
        "source_document_ids": [document_id] if document_id else [],
        "evidence_claim_ids": [],
    }
