"""lint가 post_ingest 검색 품질 실패를 유형별로 나누는지 실제 PostgreSQL로 확인한다."""
from __future__ import annotations

from uuid import uuid4

from psycopg.types.json import Json

from app.modules.wiki_ingestion.infrastructure.retrieval_quality_review import (
    list_quality_review_candidates,
)
from app.modules.wiki_ingestion.infrastructure.wiki_lint_report import render_lint_log_markdown
from tests.modules.wiki_ingestion.test_concept_entry_lint_postgres import (  # noqa: F401
    WS,
    World,
    pytestmark,
    world,
)

GOOD_QUESTION = dict.fromkeys(("aligned", "standalone", "unambiguous", "scope_matched", "durable"), True)


def _case(question: str, block: str, **overrides) -> dict:
    return {
        "question": question, "source_block_ids": [block], "passed": False, "gold_ref_hit": False,
        "contradictory_evidence_ranks": [], "missing_claims": [], "reason": f"{question} 이유",
        "answerability": True, "evidence_recall": 1.0, "source_alignment": 1.0,
        **GOOD_QUESTION, **overrides,
    }


def _run(world: World, document_id: str, status: str, evaluations: list[dict], age: int) -> None:
    manifest = {"post_ingest": {"run_id": f"post-{document_id}-{age}", "status": status,
                                "result": {"wiki_quality_evaluations": evaluations}}}
    with world.connect() as conn:
        conn.execute(
            """
            INSERT INTO pipeline_runs (id, document_id, user_id, workspace_id, input_source, output_dir,
                                       mode, status, manifest, finished_at)
            VALUES (%s, %s, 'user', %s, 'test', 'runs', 'document', 'succeeded', %s,
                    now() - make_interval(mins => %s))
            """,
            (uuid4(), document_id, WS, Json(manifest), age),
        )


def test_lint_classifies_failed_quality_cases_from_latest_runs(world: World) -> None:
    # doc-a:B0001만 검색 단위가 있다. B0009는 위키 어디에도 근거로 쓰이지 않았다.
    world.ingest("op-a", "doc-a", {"motor": {"evidence": ["B0001"]}})
    _run(world, "doc-a", "needs_review", [
        _case("검색 실패", "B0001"),
        _case("위키 누락", "B0009"),
        _case("나쁜 질문", "B0001", standalone=False),
        _case("옛 근거", "B0001", gold_ref_hit=True, contradictory_evidence_ranks=[2]),
        _case("일부 누락", "B0001", gold_ref_hit=True, missing_claims=["빠진 주장"]),
        _case("출처 불일치", "B0001", gold_ref_hit=True, source_alignment=0.5),
        # 예전 정밀도 기준으로만 떨어진 문항은 지금 통과 조건으로는 실패가 아니므로 보고하지 않는다.
        _case("정밀도만 미달", "B0001", gold_ref_hit=True, evidence_precision=0.4),
        _case("통과", "B0001", passed=True, gold_ref_hit=True),
    ], age=0)
    # 다시 편입해 통과한 문서는 옛 실패를 보고하지 않는다.
    _run(world, "doc-b", "needs_review", [_case("옛 실패", "B0001")], age=10)
    _run(world, "doc-b", "ready", [], age=0)
    # 휴지통으로 옮긴 문서도 보고하지 않는다.
    _run(world, "doc-c", "needs_review", [_case("지운 문서", "B0001")], age=0)
    with world.connect() as conn:
        conn.execute("INSERT INTO wiki_source_tombstones (workspace_id, document_id) VALUES (%s, 'doc-c')", (WS,))
        candidates = list_quality_review_candidates(conn, WS)

    assert [(item["question"], item["failure_type"]) for item in candidates] == [
        ("검색 실패", "retrieval"),
        ("위키 누락", "not_in_wiki"),
        ("나쁜 질문", "question"),
        ("옛 근거", "contradictory"),
        ("일부 누락", "incomplete"),
        ("출처 불일치", "imprecise"),
    ]
    assert candidates[0]["gold_source_refs"] == ["doc-a:B0001"]
    assert candidates[4]["missing_claims"] == ["빠진 주장"]

    log = render_lint_log_markdown(
        {"user_id": "user", "workspace_id": WS, "active_path": "active.md", "quality_review_candidates": candidates},
        "2026-11-02",
    )
    assert "- contradictory: 1, imprecise: 1, incomplete: 1, not_in_wiki: 1, question: 1, retrieval: 1\n" in log
    assert "- not_in_wiki: document:doc-a\n  question: 위키 누락\n  gold_source_refs: [doc-a:B0009]\n" in log
