from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.modules.wiki_generation.application.ports import JsonCompletionPort


EVALUATOR_PROMPT = (
    Path(__file__).resolve().parents[4]
    / "prompts"
    / "post_ingest_wiki_evaluator.system.md"
)
QUESTION_PROMPT = (
    Path(__file__).resolve().parents[4]
    / "prompts"
    / "post_ingest_question_generator.system.md"
)
EVIDENCE_EVALUATOR_PROMPT = (
    Path(__file__).resolve().parents[4]
    / "prompts"
    / "post_ingest_evidence_evaluator.system.md"
)
MIN_QUALITY_SCORE = 0.75
MAX_QUESTION_SOURCE_CHARS = 60_000
ADMINISTRATIVE_METADATA_PATTERN = re.compile(
    r"(?i)(^\s*[-*]?\s*상태\s*:|^\s*status\s*:|작성일\s*:|authored date\s*:"
    r"|기능 SDD\s*:|구현 위치\s*:|implementation location\s*:"
    r"|\b\d+\s+passed\b|\bsubtests?\s+passed\b)"
)


def generate_post_ingest_quality_cases(
    *,
    completion: JsonCompletionPort,
    source_document_id: str,
    source_blocks: list[dict[str, Any]],
    limit: int = 3,
) -> list[dict[str, Any]]:
    if limit < 1:
        return []
    blocks_by_id = {
        str(block.get("block_id")): str(block.get("text") or "")
        for block in source_blocks
        if block.get("block_id") and str(block.get("text") or "").strip()
    }
    if not blocks_by_id:
        return []
    prompt_blocks = []
    used_chars = 0
    # ponytail: 초대형 문서는 앞 60k만 사용한다.
    # 후반부 편향이 관측되면 packet 표본 추출로 바꾼다.
    for block_id, text in blocks_by_id.items():
        if used_chars >= MAX_QUESTION_SOURCE_CHARS:
            break
        clipped = text[: MAX_QUESTION_SOURCE_CHARS - used_chars]
        prompt_blocks.append({"block_id": block_id, "text": clipped})
        used_chars += len(clipped)
    value = completion.complete_json(
        QUESTION_PROMPT.read_text(encoding="utf-8"),
        json.dumps(
            {"limit": limit, "source_blocks": prompt_blocks},
            ensure_ascii=False,
            indent=2,
        ),
    )
    cases = []
    seen_questions = set()
    seen_claims = set()
    for item in value.get("cases", []):
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        evidence = [
            (str(entry.get("block_id") or ""), str(entry.get("quote") or "").strip())
            for entry in item.get("evidence", [])
            if isinstance(entry, dict)
        ]
        if len(evidence) != 1:
            continue
        verified = [
            (block_id, quote)
            for block_id, quote in evidence
            if len(quote) >= 8
            and block_id in blocks_by_id
            and _compact(quote) in _compact(blocks_by_id[block_id])
        ]
        claim = verified[0][1] if verified else ""
        if (
            not question
            or question in seen_questions
            or not claim
            or claim in seen_claims
        ):
            continue
        seen_questions.add(question)
        seen_claims.add(claim)
        cases.append(
            {
                "question": question,
                "expected_claims": list(dict.fromkeys(quote for _, quote in verified)),
                "source_document_id": source_document_id,
                "source_block_ids": list(
                    dict.fromkeys(block_id for block_id, _ in verified)
                ),
            }
        )
        if len(cases) >= limit:
            break
    return [
        case
        for case in cases
        if not ADMINISTRATIVE_METADATA_PATTERN.search(
            str(case["expected_claims"][0])
        )
    ]


def evaluate_post_ingest_evidence(
    *,
    completion: JsonCompletionPort,
    cases: list[dict[str, Any]],
    source_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    payload_cases = []
    for index, case in enumerate(cases):
        source_document_id = str(case.get("source_document_id") or "")
        wanted_refs = _case_source_refs(case)
        payload_cases.append(
            {
                "case_index": index,
                "question": case.get("question"),
                "expected_claims": case.get("expected_claims", []),
                "gold_source_refs": sorted(_gold_source_refs(case)),
                "source_blocks": [
                    {
                        "block_id": block.get("block_id"),
                        "source_document_id": block.get("source_document_id")
                        or block.get("document_id")
                        or source_document_id,
                        "text": block.get("text"),
                    }
                    for block in source_blocks
                    if _source_ref(block, source_document_id) in wanted_refs
                ],
                "retrieved_evidence": case.get("evidence_snippets", []),
            }
        )
    value = completion.complete_json(
        EVIDENCE_EVALUATOR_PROMPT.read_text(encoding="utf-8"),
        json.dumps({"cases": payload_cases}, ensure_ascii=False, indent=2),
    )
    by_index = {
        int(item["case_index"]): item
        for item in value.get("evaluations", [])
        if isinstance(item, dict) and str(item.get("case_index", "")).isdigit()
    }
    missing_indexes = sorted(set(range(len(cases))) - set(by_index))
    if missing_indexes:
        raise RuntimeError(
            f"evidence evaluator omitted case indexes: {missing_indexes}"
        )
    return [
        _normalize_evidence_evaluation(by_index[index])
        for index in range(len(cases))
    ]


def retrieval_reference_metrics(
    case: dict[str, Any],
    evidence_snippets: list[dict[str, Any]],
) -> dict[str, Any]:
    gold_refs = _gold_source_refs(case)
    matching_ranks = [
        int(snippet.get("rank") or index)
        for index, snippet in enumerate(evidence_snippets, start=1)
        if gold_refs.intersection(_evidence_source_refs(snippet))
    ]
    rank = min(matching_ranks, default=None)
    return {
        "gold_ref_hit": rank is not None,
        "gold_ref_rank": rank,
        "reciprocal_rank": 0.0 if rank is None else 1.0 / rank,
    }


def _normalize_evidence_evaluation(value: dict[str, Any]) -> dict[str, Any]:
    question_dimensions = {
        field: value.get(field) is True
        for field in (
            "aligned",
            "standalone",
            "unambiguous",
            "scope_matched",
            "durable",
        )
    }
    scores = {
        field: _bounded_float(value.get(field))
        for field in (
            "evidence_recall",
            "evidence_precision",
            "source_alignment",
        )
    }
    missing_claims = _strings(value.get("missing_claims"))
    contradictory_ranks = _positive_ints(value.get("contradictory_evidence_ranks"))
    passed = (
        all(question_dimensions.values())
        and value.get("answerability") is True
        and not missing_claims
        and not contradictory_ranks
        and all(score >= MIN_QUALITY_SCORE for score in scores.values())
    )
    return {
        "passed": passed,
        **question_dimensions,
        "answerability": value.get("answerability") is True,
        **scores,
        "missing_claims": missing_claims,
        "irrelevant_evidence_ranks": _positive_ints(
            value.get("irrelevant_evidence_ranks")
        ),
        "contradictory_evidence_ranks": contradictory_ranks,
        "reason": str(value.get("reason") or "").strip(),
        "warnings": _strings(value.get("warnings")),
    }


def evaluate_post_ingest_answer(
    *,
    completion: JsonCompletionPort,
    case: dict[str, Any],
    source_blocks: list[dict[str, Any]],
    answer: str,
    evidence_snippets: list[dict[str, Any]],
) -> dict[str, Any]:
    case_document_id = str(case.get("source_document_id") or "")
    wanted_refs = {
        f"{case_document_id}:{block_id}" if case_document_id else str(block_id)
        for block_id in case.get("source_block_ids", [])
    }
    wanted_refs.update(
        f"{snippet.get('source_document_id')}:{block_id}"
        if snippet.get("source_document_id")
        else str(block_id)
        for snippet in evidence_snippets
        for block_id in snippet.get("source_block_ids", [])
    )
    wanted_refs.update(
        f"{ref.get('source_document_id')}:{ref.get('source_block_id')}"
        for snippet in evidence_snippets
        for ref in snippet.get("source_refs", [])
        if ref.get("source_document_id") and ref.get("source_block_id")
    )
    payload = {
        "question": case.get("question"),
        "expected_claims": case.get("expected_claims", []),
        "source_blocks": [
            {
                "block_id": block.get("block_id"),
                "source_document_id": block.get("source_document_id")
                or block.get("document_id")
                or case_document_id,
                "text": block.get("text"),
            }
            for block in source_blocks
            if _source_ref(block, case_document_id) in wanted_refs
        ],
        "wiki_answer": answer,
        "wiki_evidence_snippets": evidence_snippets,
    }
    value = completion.complete_json(
        EVALUATOR_PROMPT.read_text(encoding="utf-8"),
        json.dumps(payload, ensure_ascii=False, indent=2),
    )
    return _normalize_quality_evaluation(value)


def _normalize_quality_evaluation(value: dict[str, Any]) -> dict[str, Any]:
    missing_claims = _strings(value.get("missing_claims"))
    unsupported_claims = _strings(value.get("unsupported_claims"))
    scores = {
        field: _bounded_float(value.get(field))
        for field in ("faithfulness", "semantic_recall", "citation_alignment")
    }
    passed = bool(value.get("passed")) and not missing_claims and not unsupported_claims
    passed = passed and all(score >= MIN_QUALITY_SCORE for score in scores.values())
    return {
        "passed": passed,
        **scores,
        "missing_claims": missing_claims,
        "unsupported_claims": unsupported_claims,
        "reason": str(value.get("reason") or "").strip(),
        "warnings": _strings(value.get("warnings")),
    }


def _bounded_float(value: object) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _positive_ints(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    return sorted(
        {
            int(item)
            for item in value
            if str(item).isdigit() and int(item) > 0
        }
    )


def _gold_source_refs(case: dict[str, Any]) -> set[str]:
    source_document_id = str(case.get("source_document_id") or "")
    return {
        f"{source_document_id}:{block_id}" if source_document_id else str(block_id)
        for block_id in case.get("source_block_ids", [])
    }


def _evidence_source_refs(snippet: dict[str, Any]) -> set[str]:
    refs = {
        f"{ref.get('source_document_id')}:{ref.get('source_block_id')}"
        for ref in snippet.get("source_refs", [])
        if isinstance(ref, dict)
        and ref.get("source_document_id")
        and ref.get("source_block_id")
    }
    if refs:
        return refs
    source_document_id = str(snippet.get("source_document_id") or "")
    return {
        f"{source_document_id}:{block_id}" if source_document_id else str(block_id)
        for block_id in snippet.get("source_block_ids", [])
    }


def _case_source_refs(case: dict[str, Any]) -> set[str]:
    refs = _gold_source_refs(case)
    for snippet in case.get("evidence_snippets", []):
        if isinstance(snippet, dict):
            refs.update(_evidence_source_refs(snippet))
    return refs


def _source_ref(block: dict[str, Any], default_document_id: str = "") -> str:
    block_id = str(block.get("block_id") or "")
    document_id = str(
        block.get("source_document_id")
        or block.get("document_id")
        or default_document_id
    )
    return f"{document_id}:{block_id}" if document_id else block_id


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
