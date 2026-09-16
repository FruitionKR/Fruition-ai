import math

import pytest

from app.modules.query.application.evidence_text import tokens
from app.modules.query.infrastructure.bm25_searcher import Bm25Searcher
from app.modules.query.infrastructure.rule_based_query_rewriter import RuleBasedQueryRewriter


def test_score_uses_number_of_documents_for_idf() -> None:
    scores = Bm25Searcher().score("rare common", ["rare", "common", "common"])

    rare_idf = math.log(1 + (3 - 1 + 0.5) / (1 + 0.5))
    common_idf = math.log(1 + (3 - 2 + 0.5) / (2 + 0.5))

    assert scores == pytest.approx([1.0, common_idf / rare_idf, common_idf / rare_idf])


def test_score_normalizes_korean_particles_attached_to_structured_terms() -> None:
    scores = Bm25Searcher().score(
        "index.md log.md",
        [
            "index.md는 페이지 카탈로그이고 log.md는 추가 전용 기록이다.",
            "관련 없는 문서",
        ],
    )

    assert scores == [1.0, 0.0]


def test_rewritten_compound_particle_query_matches_evidence_tokens() -> None:
    rewrite = RuleBasedQueryRewriter().rewrite("문서에서는")
    searcher = Bm25Searcher()

    assert searcher.score(rewrite.retrieval_query, ["문서 내용", "다른 내용"])[0] == 1.0
    assert set(tokens("문서에서는 내용")) == {"문서", "내용"}


def test_short_compound_particle_does_not_match_partially_stripped_evidence() -> None:
    rewrite = RuleBasedQueryRewriter().rewrite("나에서는")

    assert rewrite.retrieval_query == "나에서는"
    assert Bm25Searcher().score(rewrite.retrieval_query, ["나에서 내용"])[0] == 0.0
    assert tokens("나에서는") == ["나에서는"]
