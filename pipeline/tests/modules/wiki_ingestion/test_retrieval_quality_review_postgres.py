"""lint가 post_ingest 검색 품질 실패를 유형별로 나누는지 실제 PostgreSQL로 확인한다."""
from __future__ import annotations

from uuid import uuid4

from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database
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
        "answerability": True, "evidence_recall": 1.0,
        **GOOD_QUESTION, **overrides,
    }


def _ingest_run(world: World, document_id: str) -> str:
    """편입 성공 한 번. 실제 편입 커밋처럼 문서 품질 상태를 이 run으로 비운다."""
    run_id = str(uuid4())
    with world.connect() as conn:
        conn.execute(
            """
            INSERT INTO pipeline_runs (id, document_id, user_id, workspace_id, input_source, output_dir,
                                       mode, status, manifest, finished_at)
            VALUES (%s, %s, 'user', %s, 'test', 'runs', 'document', 'succeeded', '{}', now())
            """,
            (run_id, document_id, WS),
        )
        database._reset_document_quality_review(conn, document_id, WS, run_id)
    return run_id


def _post_ingest(run_id: str, status: str, evaluations: list[dict]) -> None:
    database.update_pipeline_run_post_ingest(
        run_id, {"run_id": f"post-{run_id}", "status": status, "result": {"wiki_quality_evaluations": evaluations}},
    )


def _run(world: World, document_id: str, status: str, evaluations: list[dict]) -> None:
    _post_ingest(_ingest_run(world, document_id), status, evaluations)


def test_lint_classifies_failed_quality_cases_from_latest_runs(world: World) -> None:
    # doc-a:B0001만 검색 단위가 있다. B0009는 위키 어디에도 근거로 쓰이지 않았다.
    world.ingest("op-a", "doc-a", {"motor": {"evidence": ["B0001"]}})
    _run(world, "doc-a", "needs_review", [
        _case("검색 실패", "B0001"),
        _case("위키 누락", "B0009"),
        _case("나쁜 질문", "B0001", standalone=False),
        _case("옛 근거", "B0001", gold_ref_hit=True, contradictory_evidence_ranks=[2]),
        _case("일부 누락", "B0001", gold_ref_hit=True, missing_claims=["빠진 주장"]),
        _case("무관 근거", "B0001", gold_ref_hit=True, evidence_precision=0.4),
        _case("통과", "B0001", passed=True, gold_ref_hit=True),
    ])
    # 다시 편입해 통과한 문서는 옛 실패를 보고하지 않는다.
    _run(world, "doc-b", "needs_review", [_case("옛 실패", "B0001")])
    _run(world, "doc-b", "ready", [])
    # 휴지통으로 옮긴 문서도 보고하지 않는다.
    _run(world, "doc-c", "needs_review", [_case("지운 문서", "B0001")])
    with world.connect() as conn:
        conn.execute("INSERT INTO wiki_source_tombstones (workspace_id, document_id) VALUES (%s, 'doc-c')", (WS,))
        candidates = list_quality_review_candidates(conn, WS)

    assert [(item["question"], item["failure_type"]) for item in candidates] == [
        ("검색 실패", "retrieval"),
        ("위키 누락", "not_in_wiki"),
        ("나쁜 질문", "question"),
        ("옛 근거", "contradictory"),
        ("일부 누락", "incomplete"),
        ("무관 근거", "imprecise"),
    ]
    assert candidates[0]["gold_source_refs"] == ["doc-a:B0001"]
    assert candidates[4]["missing_claims"] == ["빠진 주장"]

    log = render_lint_log_markdown(
        {"user_id": "user", "workspace_id": WS, "active_path": "active.md", "quality_review_candidates": candidates},
        "2026-11-02",
    )
    assert "- contradictory: 1, imprecise: 1, incomplete: 1, not_in_wiki: 1, question: 1, retrieval: 1\n" in log
    assert "- not_in_wiki: document:doc-a\n  question: 위키 누락\n  gold_source_refs: [doc-a:B0009]\n" in log


def test_quality_state_follows_latest_ingest_only(world: World) -> None:
    old = _ingest_run(world, "doc-a")
    new = _ingest_run(world, "doc-a")
    # 다시 편입한 뒤 늦게 끝난 옛 post_ingest는 새 편입의 상태를 덮지 않는다.
    _post_ingest(old, "needs_review", [_case("옛 실패", "B0001")])
    with world.connect() as conn:
        assert list_quality_review_candidates(conn, WS) == []
    _post_ingest(new, "running", [])
    with world.connect() as conn:
        assert list_quality_review_candidates(conn, WS) == []
    _post_ingest(new, "needs_review", [_case("새 실패", "B0001")])
    with world.connect() as conn:
        assert [item["question"] for item in list_quality_review_candidates(conn, WS)] == ["새 실패"]
    # 다시 편입하면 평가가 끝나기 전까지 후보가 아니다.
    _ingest_run(world, "doc-a")
    with world.connect() as conn:
        assert list_quality_review_candidates(conn, WS) == []


def test_schema_backfills_quality_state_from_latest_run_manifest(world: World) -> None:
    from psycopg.types.json import Json

    from tests.modules.wiki_ingestion.test_concept_entry_lint_postgres import AI_SCHEMA_SQL

    with world.connect() as conn:
        for age, status, question in ((10, "needs_review", "옛 실패"), (0, "needs_review", "최신 실패")):
            manifest = {"post_ingest": {"run_id": f"post-{age}", "status": status,
                                        "result": {"wiki_quality_evaluations": [_case(question, "B0001")]}}}
            conn.execute(
                """
                INSERT INTO pipeline_runs (id, document_id, user_id, workspace_id, input_source, output_dir,
                                           mode, status, manifest, finished_at)
                VALUES (%s, 'doc-a', 'user', %s, 'test', 'runs', 'document', 'succeeded', %s,
                        now() - make_interval(mins => %s))
                """,
                (uuid4(), WS, Json(manifest), age),
            )
        conn.execute(AI_SCHEMA_SQL)
        assert [item["question"] for item in list_quality_review_candidates(conn, WS)] == ["최신 실패"]
