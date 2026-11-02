from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from app.core.jev_client import JevUnavailable
from app.core.pipeline_control import PipelineRunCancelledError
from app.modules.wiki_generation.application.ports import JsonCompletionPort

logger = logging.getLogger(__name__)
# (판정할 개념, 후보 pool) → incoming slug별 판정. JevConceptResolver.judge_concepts와 같은 계약이다.
PairJudge = Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, dict[str, Any]]]
# [(상위 항목, 세부 항목)] → (상위 slug, 세부 slug)별 part_of 판정. JevConceptResolver.judge_part_of와 같은 계약이다.
PartOfJudge = Callable[[list[tuple[dict[str, Any], dict[str, Any]]]], dict[tuple[str, str], dict[str, Any]]]


# 편입 LLM 판정의 확신도 문턱. Jev part_of(`PART_OF_MIN_PROBABILITY`)와 같은 값이다. 애매하면 배정·병합하지 않는다.
LLM_JUDGE_MIN_CONFIDENCE = 0.9
# LLM 판정 한 번에 묻는 질문 수. 편입 하나의 LLM 호출 수는 질문 수 / 이 값(올림)이다.
LLM_JUDGE_QUESTIONS_PER_CALL = 40
# LLM 같은 하위 항목 판정에서 항목마다 보여 줄 후보 수(Jev `ENTRY_CANDIDATE_LIMIT`와 같다).
LLM_ENTRY_CANDIDATE_LIMIT = 10


def llm_part_of_judge(completion: JsonCompletionPort, system_prompt: str) -> PartOfJudge:
    """Jev를 쓸 수 없을 때 편입 LLM이 같은 part_of 질문에 답한다(ADR 0042). 결과 계약은 `judge_part_of`와 같다."""

    def judge(pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[tuple[str, str], dict[str, Any]]:
        decisions: dict[tuple[str, str], dict[str, Any]] = {}
        for start in range(0, len(pairs), LLM_JUDGE_QUESTIONS_PER_CALL):
            batch = {f"q{start + index}": pair for index, pair in enumerate(pairs[start:start + LLM_JUDGE_QUESTIONS_PER_CALL])}
            payload = {"questions": [
                {"id": key, "head": _concept_identity(head), "detail": _concept_identity(detail)}
                for key, (head, detail) in batch.items()
            ]}
            raw = _judge_call(completion, system_prompt, payload)
            for item in raw.get("judgments") or []:
                if not isinstance(item, dict) or item.get("id") not in batch:
                    continue
                head, detail = batch[item["id"]]
                confidence = _confidence(item.get("confidence"))
                decisions[(head["slug"], detail["slug"])] = {
                    "part_of": item.get("decision") == "part_of" and confidence >= LLM_JUDGE_MIN_CONFIDENCE,
                    "probability": confidence,
                }
        return decisions

    return judge


def llm_same_entry_judge(
    completion: JsonCompletionPort,
    system_prompt: str,
    similarity: Callable[[str, list[str]], list[float]],
) -> PairJudge:
    """Jev를 쓸 수 없을 때 편입 LLM이 같은 하위 항목 질문에 답한다. 결과 계약은 `judge_entries`와 같다.

    후보는 임베딩 유사도 상위 `LLM_ENTRY_CANDIDATE_LIMIT`개다(자기 자신 제외). 정의가 없는 항목은 묻지 않는다.
    """

    def judge(entries: list[dict[str, Any]], pool: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        def text(item: dict[str, Any]) -> str:
            return f"{item['title']}: {item.get('definition') or ''}"

        pool = [item for item in pool if str(item.get("definition") or "").strip()]
        questions = []
        for entry in entries:
            others = [item for item in pool if item["slug"] != entry["slug"]]
            if not str(entry.get("definition") or "").strip() or not others:
                continue
            scores = similarity(text(entry), [text(item) for item in others])
            ranked = sorted(zip(scores, others, strict=True), key=lambda row: (-row[0], row[1]["slug"]))
            questions.append((entry, [item for _, item in ranked[:LLM_ENTRY_CANDIDATE_LIMIT]]))
        decisions: dict[str, dict[str, Any]] = {}
        for start in range(0, len(questions), LLM_JUDGE_QUESTIONS_PER_CALL):
            batch = {entry["slug"]: candidates for entry, candidates in questions[start:start + LLM_JUDGE_QUESTIONS_PER_CALL]}
            payload = {"questions": [
                {"id": entry["slug"], "incoming": _concept_identity(entry), "candidates": [_concept_identity(item) for item in candidates]}
                for entry, candidates in questions[start:start + LLM_JUDGE_QUESTIONS_PER_CALL]
            ]}
            raw = _judge_call(completion, system_prompt, payload)
            for item in raw.get("judgments") or []:
                if not isinstance(item, dict) or item.get("id") not in batch:
                    continue
                target = str(item.get("same_as") or "")
                if target in {candidate["slug"] for candidate in batch[item["id"]]} and (
                    _confidence(item.get("confidence")) >= LLM_JUDGE_MIN_CONFIDENCE
                ):
                    decisions[item["id"]] = {"incoming_slug": item["id"], "decision": "merge_into",
                                             "canonical_slug": target, "alias_to_add": None}
        return decisions

    return judge


def _judge_call(completion: JsonCompletionPort, system_prompt: str, payload: dict[str, Any]) -> dict[str, Any]:
    """판정 LLM 호출. 전송 오류·시간 초과·JSON 재시도 소진은 판정 없음(빈 결과)으로 보고 편입을 실패시키지 않는다.

    작업 취소와 전체 기한 초과(PipelineRunCancelledError)는 그대로 올린다.
    """
    try:
        raw = completion.complete_json(system_prompt, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    except PipelineRunCancelledError:
        raise
    except Exception as exc:
        logger.warning("[LLM 하위 항목 판정 생략] %s", type(exc).__name__)
        return {}
    return raw if isinstance(raw, dict) else {}


def _confidence(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def judge_meaning_cluster_candidates(
    *,
    completion: JsonCompletionPort,
    existing_active_markdown: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    system_prompt = """Stage=MeaningClusterJudge.
You decide whether section/mention evidence claims should update an existing term_cluster or create a new term_cluster.

Rules:
- Return JSON only.
- Do not create core concepts or graph edges.
- Prefer same_cluster when the candidate is a synonym, abbreviation, translation, spelling variant, or narrower wording of an existing cluster.
- Use new_cluster only when no existing cluster or other incoming candidate has the same meaning.
- Incoming candidates may share the same new target_cluster_id if they should be grouped together.
- target_cluster_id must be descriptive kebab-case such as "back-emf" or "manufacturing-uncertainty".
- Never use generic ids such as "cluster-001", "new-cluster", "candidate-1", or "term".
- Use needs_review only when the candidate is ambiguous.
Schema:
{
  "decisions": [
    {
      "candidate_id": "cand_001",
      "decision": "same_cluster | new_cluster | needs_review",
      "target_cluster_id": "cluster-id-to-update-or-create",
      "representative": "short display label",
      "reason": "brief Korean reason"
    }
  ]
}
"""
    payload = {
        "existing_active_clusters": existing_active_markdown,
        "incoming_candidates": [
            {
                "candidate_id": item["candidate_id"],
                "term": item["term"],
                "suggested_slug": item["slug"],
                "claim": item["claim"],
                "refs": item["refs"],
                "candidate_type": item["candidate_type"],
            }
            for item in candidates
        ],
    }
    raw = completion.complete_json(system_prompt, json.dumps(payload, ensure_ascii=False, indent=2))
    valid_candidate_ids = {item["candidate_id"] for item in candidates}
    normalized_decisions: list[dict[str, Any]] = []
    for item, candidate_id in _valid_candidate_decisions(raw, valid_candidate_ids):
        decision = str(item.get("decision") or "new_cluster")
        if decision not in {"same_cluster", "new_cluster", "needs_review"}:
            decision = "new_cluster"
        normalized_decisions.append(
            {
                "candidate_id": candidate_id,
                "decision": decision,
                "target_cluster_id": item.get("target_cluster_id"),
                "representative": item.get("representative"),
                "reason": item.get("reason"),
            }
        )
    return normalized_decisions

PROMOTION_BATCH_CHARS = 12000
PROMOTION_PROMPT = """Stage=ClusterPromotionJudge.
Each item is one term_cluster with every evidence claim accumulated so far.

Rules:
- Return JSON only.
- Judge each cluster independently, using only its own claims. Never compare, merge, or rename clusters.
- Use promotion_status "candidate" only when the accumulated claims show a reusable domain concept worth a
  long-lived wiki page: grounded claims that define or use it consistently, with relation or retrieval
  value beyond a single document.
- Do not promote merely because a term is definable.
- Do not promote bibliographic/entity metadata: author names, researcher names, universities, funders,
  journals, conferences, publishers, citations, document titles, affiliations, or project metadata.
- Do not promote one-off named tools, software, product names, experimental labels, parameter labels, or
  isolated metrics unless the claims show a reusable domain concept.
- Otherwise use "none".
Schema:
{
  "decisions": [
    {"cluster_id": "back-emf", "promotion_status": "candidate | none", "reason": "brief Korean reason"}
  ]
}
"""


def judge_cluster_promotions(
    *,
    completion: JsonCompletionPort,
    clusters: list[dict[str, str]],
) -> dict[str, str]:
    """근거가 덧붙은 클러스터마다 쌓인 근거만 보고 승격 여부를 정하고, 승격할 클러스터의 이유를 돌려준다."""
    promoted: dict[str, str] = {}
    for batch in _promotion_batches(clusters):
        try:
            decided = _judge_promotion_batch(completion, batch)
        except (RuntimeError, ValueError, TimeoutError):
            decided = {}
        # 묶음 호출이 실패하거나 빠뜨린 클러스터는 하나씩 다시 묻는다.
        for cluster in batch:
            if cluster["cluster_id"] not in decided:
                decided.update(_judge_promotion_batch(completion, [cluster]))
        promoted.update(
            {cluster_id: item["reason"] for cluster_id, item in decided.items() if item["status"] == "candidate"}
        )
    return promoted


def _promotion_batches(clusters: list[dict[str, str]]) -> list[list[dict[str, str]]]:
    batches: list[list[dict[str, str]]] = []
    size = 0
    for cluster in clusters:
        section = cluster["section"]
        if len(section) > PROMOTION_BATCH_CHARS:
            # ponytail: 너무 커진 클러스터는 머리(이름·대표명)와 최근 근거만 본다. 요약 단계가 필요하면 그때 둔다.
            section = f"{section[:400]}\n...\n{section[-(PROMOTION_BATCH_CHARS - 400):]}"
        cluster = {**cluster, "section": section}
        if batches and size + len(section) <= PROMOTION_BATCH_CHARS:
            batches[-1].append(cluster)
            size += len(section)
        else:
            batches.append([cluster])
            size = len(section)
    return batches


def _judge_promotion_batch(
    completion: JsonCompletionPort,
    batch: list[dict[str, str]],
) -> dict[str, dict[str, str]]:
    raw = completion.complete_json(
        PROMOTION_PROMPT,
        json.dumps(
            {"clusters": [{"cluster_id": item["cluster_id"], "markdown": item["section"]} for item in batch]},
            ensure_ascii=False,
            indent=2,
        ),
    )
    valid_ids = {item["cluster_id"] for item in batch}
    decided: dict[str, dict[str, str]] = {}
    for item in raw.get("decisions") or []:
        if not isinstance(item, dict) or str(item.get("cluster_id")) not in valid_ids:
            continue
        status = "candidate" if item.get("promotion_status") == "candidate" else "none"
        decided[str(item["cluster_id"])] = {"status": status, "reason": str(item.get("reason") or "")}
    return decided


def normalize_concept_update_decisions(
    raw: dict[str, Any],
    *,
    concepts: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    concept_slug_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    if not concepts or not candidates:
        return []
    valid_candidate_ids = {item["candidate_id"] for item in candidates}
    valid_concept_slugs = {str(concept.get("slug")) for concept in concepts if concept.get("slug")}
    slug_map = concept_slug_map or {}
    normalized_decisions: list[dict[str, Any]] = []
    for item, candidate_id in _valid_candidate_decisions(
        raw,
        valid_candidate_ids,
        key="concept_update_decisions",
    ):
        decision = str(item.get("decision") or "not_same_concept")
        raw_concept_slug = str(item.get("concept_slug") or "")
        concept_slug = slug_map.get(raw_concept_slug, raw_concept_slug)
        if decision != "same_concept" or concept_slug not in valid_concept_slugs:
            decision = "not_same_concept"
            concept_slug = ""
        normalized_decisions.append(
            {
                "candidate_id": candidate_id,
                "decision": decision,
                "concept_slug": concept_slug,
                "reason": item.get("reason"),
            }
        )
    return normalized_decisions


def judge_concept_update_candidates(
    *,
    completion: JsonCompletionPort,
    concepts: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    judge_pairs: PairJudge | None = None,
) -> list[dict[str, Any]]:
    """섹션 후보·언급이 기존·이번 문서 개념과 같은지 판정한다. 개념은 이름·별칭·정의만 본다.

    judge_pairs가 있으면 개념 병합과 같은 쌍별 판정(이름 일치 → 후보 100개 → Jev)을 쓰고,
    없거나 쓸 수 없으면 LLM 한 번에 판정한다.
    """
    if not concepts or not candidates:
        return []
    identities = [_concept_identity(concept) for concept in concepts if concept.get("slug")]
    if judge_pairs is not None:
        try:
            return _pair_judged_updates(judge_pairs, identities, concepts, candidates)
        except JevUnavailable as exc:
            logger.warning("[개념 갱신 후보 Jev 대체] %s", exc)
    system_prompt = """Stage=ConceptUpdateCandidateJudge.
You decide whether section/mention evidence claims already belong to an existing/current core concept.

Rules:
- Return JSON only.
- Use same_concept only when the candidate has the same identity as an existing/current concept, such as a synonym, translation, abbreviation, or spelling variant.
- A procedure, record, metric, property, evidence, or subtopic that merely belongs to or supports an existing concept is not same_concept.
- Use not_same_concept otherwise so it remains available for active cluster judging.
- Do not create new concepts or clusters.

Schema:
{
  "decisions": [
    {
      "candidate_id": "cand_001",
      "decision": "same_concept | not_same_concept",
      "concept_slug": "existing-concept-slug-or-empty",
      "reason": "brief Korean reason"
    }
  ]
}
"""
    # ponytail: LLM 대체 경로는 개념 전부를 한 요청에 담는다. 개념당 약 400자라 Gemini 한도에서 수천 개까지 든다.
    payload = {
        "concepts": identities,
        "incoming_candidates": [
            {
                "candidate_id": item["candidate_id"],
                "term": item["term"],
                "suggested_slug": item["slug"],
                "claim": item["claim"],
                "refs": item["refs"],
                "candidate_type": item["candidate_type"],
            }
            for item in candidates
        ],
    }
    raw = completion.complete_json(
        system_prompt,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )
    return normalize_concept_update_decisions(
        {"concept_update_decisions": raw.get("decisions", [])},
        concepts=concepts,
        candidates=candidates,
    )


def _concept_identity(concept: dict[str, Any]) -> dict[str, Any]:
    return {
        "slug": concept["slug"],
        "title": concept.get("title"),
        "aliases": list(concept.get("aliases") or []),
        # DB 개념 인덱스는 definition, 파일 개념 인덱스는 summary에 정의를 담는다.
        "definition": concept.get("definition") or concept.get("summary") or "",
    }


def _pair_judged_updates(
    judge_pairs: PairJudge,
    identities: list[dict[str, Any]],
    concepts: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    # 후보는 근거 문장을 정의 자리에 둔 개념처럼 판정한다. same이면 같은 개념이고, 나머지는 클러스터 판정으로 간다.
    incoming = [
        {"slug": item["candidate_id"], "title": item["term"], "aliases": [item["slug"]], "definition": item["claim"]}
        for item in candidates
    ]
    resolutions = judge_pairs(incoming, [concept for concept in identities if str(concept["definition"]).strip()])
    decisions = []
    for item in candidates:
        resolution = resolutions.get(item["candidate_id"]) or {}
        same = resolution.get("decision") == "merge_into"
        decisions.append(
            {
                "candidate_id": item["candidate_id"],
                "decision": "same_concept" if same else "not_same_concept",
                "concept_slug": (resolution.get("canonical_slug") or "") if same else "",
            }
        )
    return normalize_concept_update_decisions(
        {"concept_update_decisions": decisions},
        concepts=concepts,
        candidates=candidates,
    )


def _valid_candidate_decisions(
    raw: dict[str, Any],
    valid_candidate_ids: set[str],
    *,
    key: str = "decisions",
) -> list[tuple[dict[str, Any], str]]:
    decisions = raw.get(key, [])
    if not isinstance(decisions, list):
        return []

    valid_decisions = []
    for item in decisions:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("candidate_id") or "")
        if candidate_id in valid_candidate_ids:
            valid_decisions.append((item, candidate_id))
    return valid_decisions
