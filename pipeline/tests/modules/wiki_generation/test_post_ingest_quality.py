import json

import pytest

from app.modules.wiki_generation.infrastructure.post_ingest_quality import (
    EVIDENCE_EVALUATOR_PROMPT,
    QUESTION_PROMPT,
    evaluate_post_ingest_answer,
    evaluate_post_ingest_evidence,
    generate_post_ingest_quality_cases,
    retrieval_reference_metrics,
)


class FixedCompletion:
    def __init__(self, response: dict | list[dict]) -> None:
        self.responses = response if isinstance(response, list) else [response]
        self.user_prompt = ""
        self.user_prompts = []

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict:
        assert "source-grounded" in system_prompt
        self.user_prompt = user_prompt
        self.user_prompts.append(user_prompt)
        return self.responses.pop(0)


def test_question_prompts_require_standalone_scope_matched_cases() -> None:
    generator = QUESTION_PROMPT.read_text(encoding="utf-8")
    evaluator = EVIDENCE_EVALUATOR_PROMPT.read_text(encoding="utf-8")

    assert "one uniquely intended answer" in generator
    assert "Every factual part of the quote must be requested" in generator
    assert "administrative metadata" in generator
    assert "answerability" in evaluator
    assert "evidence_precision" in evaluator
    assert "contradictory_evidence_ranks" in evaluator


def test_quality_cases_use_only_verbatim_raw_source_evidence() -> None:
    completion = FixedCompletion(
        {
            "cases": [
                {
                    "question": "지속형 위키의 핵심은 무엇인가?",
                    "evidence": [
                        {
                            "block_id": "B0001",
                            "quote": "지식을 여러 실행에 걸쳐 누적한다.",
                        }
                    ],
                },
                {
                    "question": "다른 실제 사실은 무엇인가?",
                    "evidence": [
                        {"block_id": "B0002", "quote": "다른 실제 사실이다."}
                    ],
                },
            ]
        }
    )

    cases = generate_post_ingest_quality_cases(
        completion=completion,
        source_document_id="doc-1",
        source_blocks=[
            {
                "block_id": "B0001",
                "text": "지식을 여러 실행에 걸쳐\n누적한다.",
            },
            {
                "block_id": "B0002",
                "text": "다른 실제 사실이다.",
            },
        ],
        limit=3,
    )

    assert len(cases) == 2
    assert cases[0]["question"] == "지속형 위키의 핵심은 무엇인가?"
    assert cases[0]["expected_claims"] == ["지식을 여러 실행에 걸쳐 누적한다."]
    assert cases[0]["source_block_ids"] == ["B0001"]
    assert cases[0]["source_document_id"] == "doc-1"
    assert "다른 실제 사실이다" in completion.user_prompts[0]


def test_quality_cases_reject_multiple_quotes_for_one_question() -> None:
    completion = FixedCompletion(
        {
            "cases": [
                {
                    "question": "로컬 base URL은 무엇인가?",
                    "evidence": [
                        {"block_id": "B0001", "quote": "로컬 URL은 localhost다."},
                        {"block_id": "B0002", "quote": "API 계약은 별도로 정의한다."},
                    ],
                }
            ]
        }
    )

    cases = generate_post_ingest_quality_cases(
        completion=completion,
        source_document_id="doc-1",
        source_blocks=[
            {"block_id": "B0001", "text": "로컬 URL은 localhost다."},
            {"block_id": "B0002", "text": "API 계약은 별도로 정의한다."},
        ],
        limit=1,
    )

    assert cases == []
    assert completion.responses == []


def test_evidence_evaluation_rejects_semantically_misaligned_pair() -> None:
    completion = FixedCompletion(
        {
            "evaluations": [
                {
                    "case_index": 0,
                    "aligned": False,
                    "standalone": True,
                    "unambiguous": True,
                    "scope_matched": False,
                    "durable": True,
                    "answerability": True,
                    "evidence_recall": 1.0,
                    "evidence_precision": 1.0,
                    "source_alignment": 1.0,
                    "missing_claims": [],
                    "irrelevant_evidence_ranks": [],
                    "contradictory_evidence_ranks": [],
                    "reason": "다른 질문의 답",
                    "warnings": [],
                }
            ]
        }
    )

    evaluations = evaluate_post_ingest_evidence(
        completion=completion,
        cases=[
            {
                "question": "로컬 base URL은 무엇인가?",
                "expected_claims": ["API 계약은 별도로 정의한다."],
                "source_document_id": "doc-1",
                "source_block_ids": ["B0001"],
                "evidence_snippets": [],
            }
        ],
        source_blocks=[
            {"block_id": "B0001", "text": "API 계약은 별도로 정의한다."}
        ],
    )

    assert evaluations[0]["passed"] is False
    assert evaluations[0]["aligned"] is False


def test_evidence_evaluation_retries_instead_of_accepting_omitted_cases() -> None:
    completion = FixedCompletion(
        {
            "evaluations": [
                {
                    "case_index": 0,
                    "aligned": True,
                }
            ]
        }
    )
    cases = [
        {
            "question": f"질문 {index}",
            "expected_claims": ["주장"],
            "source_document_id": "doc-1",
            "source_block_ids": ["B0001"],
            "evidence_snippets": [],
        }
        for index in range(2)
    ]

    with pytest.raises(
        RuntimeError,
        match=r"evidence evaluator omitted case indexes: \[1\]",
    ):
        evaluate_post_ingest_evidence(
            completion=completion,
            cases=cases,
            source_blocks=[{"block_id": "B0001", "text": "주장"}],
        )


def test_quality_cases_require_every_question_quality_dimension() -> None:
    completion = FixedCompletion(
        {
            "cases": [
                {
                    "question": "문서의 현재 상태는 무엇인가?",
                    "evidence": [
                        {"block_id": "B0001", "quote": "- 상태: Draft"}
                    ],
                }
            ]
        }
    )

    cases = generate_post_ingest_quality_cases(
        completion=completion,
        source_document_id="doc-1",
        source_blocks=[{"block_id": "B0001", "text": "- 상태: Draft"}],
        limit=1,
    )

    assert cases == []


def test_quality_cases_keep_valid_subset() -> None:
    completion = FixedCompletion(
        {
            "cases": [
                    {
                        "question": "로컬 base URL은 무엇인가?",
                        "evidence": [
                            {
                                "block_id": "B0001",
                                "quote": "로컬 base URL은 http://localhost:8000이다.",
                            }
                        ],
                    },
                    {
                        "question": "문서의 현재 상태는 무엇인가?",
                        "evidence": [
                            {"block_id": "B0002", "quote": "- 상태: Draft"}
                        ],
                    },
            ]
        }
    )

    cases = generate_post_ingest_quality_cases(
        completion=completion,
        source_document_id="doc-1",
        source_blocks=[
            {
                "block_id": "B0001",
                "text": "로컬 base URL은 http://localhost:8000이다.",
            },
            {"block_id": "B0002", "text": "- 상태: Draft"},
        ],
        limit=2,
    )

    assert [case["question"] for case in cases] == ["로컬 base URL은 무엇인가?"]


def test_quality_cases_respect_zero_limit() -> None:
    completion = FixedCompletion({"cases": []})

    assert generate_post_ingest_quality_cases(
        completion=completion,
        source_document_id="doc-1",
        source_blocks=[],
        limit=0,
    ) == []


def test_evidence_evaluation_batches_cases_and_rejects_contradiction() -> None:
    completion = FixedCompletion(
        {
            "evaluations": [
                {
                    "case_index": 0,
                    "passed": True,
                    "aligned": True,
                    "standalone": True,
                    "unambiguous": True,
                    "scope_matched": True,
                    "durable": True,
                    "answerability": True,
                    "evidence_recall": 1.0,
                    "evidence_precision": 0.8,
                    "source_alignment": 1.0,
                    "missing_claims": [],
                    "irrelevant_evidence_ranks": [2],
                    "contradictory_evidence_ranks": [],
                    "reason": "답변 가능한 근거가 있음",
                    "warnings": [],
                },
                {
                    "case_index": 1,
                    "passed": True,
                    "aligned": True,
                    "standalone": True,
                    "unambiguous": True,
                    "scope_matched": True,
                    "durable": True,
                    "answerability": True,
                    "evidence_recall": 1.0,
                    "evidence_precision": 1.0,
                    "source_alignment": 1.0,
                    "missing_claims": [],
                    "irrelevant_evidence_ranks": [],
                    "contradictory_evidence_ranks": [1],
                    "reason": "상충 근거가 있음",
                    "warnings": [],
                },
            ]
        }
    )
    cases = [
        {
            "question": "보존 기간은?",
            "expected_claims": ["45일간 보관한다."],
            "source_document_id": "doc-1",
            "source_block_ids": ["B0001"],
            "evidence_snippets": [
                {
                    "rank": 1,
                    "source_document_id": "doc-1",
                    "source_block_ids": ["B0001"],
                    "source_refs": [
                        {
                            "source_document_id": "doc-1",
                            "source_block_id": "B0001",
                        }
                    ],
                    "text": "45일간 보관한다.",
                }
            ],
        },
        {
            "question": "교체 주기는?",
            "expected_claims": ["60일마다 교체한다."],
            "source_document_id": "doc-2",
            "source_block_ids": ["B0002"],
            "evidence_snippets": [],
        },
    ]

    evaluations = evaluate_post_ingest_evidence(
        completion=completion,
        cases=cases,
        source_blocks=[
            {
                "source_document_id": "doc-1",
                "block_id": "B0001",
                "text": "45일간 보관한다.",
            },
            {
                "source_document_id": "doc-2",
                "block_id": "B0002",
                "text": "60일마다 교체한다.",
            },
        ],
    )

    assert len(completion.user_prompts) == 1
    assert evaluations[0]["passed"] is True
    assert evaluations[0]["irrelevant_evidence_ranks"] == [2]
    assert evaluations[1]["passed"] is False
    assert evaluations[1]["contradictory_evidence_ranks"] == [1]


def test_retrieval_reference_metrics_uses_explicit_provenance_rank() -> None:
    metrics = retrieval_reference_metrics(
        {
            "source_document_id": "doc-1",
            "source_block_ids": ["B0001"],
        },
        [
            {
                "rank": 1,
                "source_document_id": "doc-other",
                "source_block_ids": ["B0009"],
                "source_refs": [],
            },
            {
                "rank": 2,
                "source_refs": [
                    {
                        "source_document_id": "doc-1",
                        "source_block_id": "B0001",
                    }
                ],
            },
        ],
    )

    assert metrics == {
        "gold_ref_hit": True,
        "gold_ref_rank": 2,
        "reciprocal_rank": 0.5,
    }


def test_quality_evaluation_rejects_missing_expected_claim() -> None:
    completion = FixedCompletion(
        {
            "passed": True,
            "faithfulness": 1.0,
            "semantic_recall": 1.0,
            "citation_alignment": 1.0,
            "missing_claims": ["세 번째 계층"],
            "unsupported_claims": [],
            "reason": "일부가 누락됨",
            "warnings": [],
        }
    )

    result = evaluate_post_ingest_answer(
        completion=completion,
        case={
            "question": "세 계층은?",
            "expected_claims": ["세 계층으로 구성된다."],
            "source_block_ids": ["B0001"],
        },
        source_blocks=[{"block_id": "B0001", "text": "세 계층으로 구성된다."}],
        answer="두 계층입니다. [1]",
        evidence_snippets=[
            {
                "rank": 1,
                "source_block_ids": ["B0001"],
                "text": "세 계층으로 구성된다.",
            }
        ],
    )

    assert result["passed"] is False
    assert result["missing_claims"] == ["세 번째 계층"]
    assert "세 계층으로 구성된다" in completion.user_prompt


def test_quality_evaluation_accepts_grounded_complete_answer() -> None:
    completion = FixedCompletion(
        {
            "passed": True,
            "faithfulness": 1.0,
            "semantic_recall": 0.9,
            "citation_alignment": 1.0,
            "missing_claims": [],
            "unsupported_claims": [],
            "reason": "원문 기대 사실을 충실히 답함",
            "warnings": ["표현 개선 가능"],
        }
    )

    result = evaluate_post_ingest_answer(
        completion=completion,
        case={
            "question": "핵심은?",
            "expected_claims": ["지식을 누적한다."],
            "source_block_ids": ["B0001"],
        },
        source_blocks=[{"block_id": "B0001", "text": "지식을 누적한다."}],
        answer="지식을 누적합니다. [1]",
        evidence_snippets=[],
    )

    assert result["passed"] is True
    assert result["warnings"] == ["표현 개선 가능"]


def test_quality_evaluation_loads_source_for_every_used_evidence_block() -> None:
    completion = FixedCompletion(
        {
            "passed": True,
            "faithfulness": 1.0,
            "semantic_recall": 1.0,
            "citation_alignment": 1.0,
            "missing_claims": [],
            "unsupported_claims": [],
            "reason": "통과",
            "warnings": [],
        }
    )

    evaluate_post_ingest_answer(
        completion=completion,
        case={
            "question": "핵심은?",
            "expected_claims": ["기대 사실"],
            "source_document_id": "doc-a",
            "source_block_ids": ["B0001"],
        },
        source_blocks=[
            {
                "source_document_id": "doc-a",
                "block_id": "B0001",
                "text": "기대 사실",
            },
            {
                "source_document_id": "doc-b",
                "block_id": "B0001",
                "text": "추가 인용 사실",
            },
        ],
        answer="기대 사실과 추가 인용 사실입니다. [1]",
        evidence_snippets=[
            {
                "rank": 1,
                "source_document_id": "doc-b",
                "source_block_ids": ["B0001"],
                "text": "추가 인용 사실",
            }
        ],
    )

    payload = json.loads(completion.user_prompt)
    assert [
        (block["source_document_id"], block["block_id"])
        for block in payload["source_blocks"]
    ] == [
        ("doc-a", "B0001"),
        ("doc-b", "B0001"),
    ]
