"""post_ingest 검색 품질 평가에서 떨어진 문항을 lint가 읽어 실패 유형별로 나눈다.

보고만 한다. 유형은 나중에 lint가 고칠 대상을 정하는 근거다.
- question: 평가 질문 자체가 나쁘다. 위키 문제가 아니다.
- contradictory: 질문과 어긋나는 옛·틀린 근거가 검색됐다.
- not_in_wiki: 정답 원문 블록을 근거로 가진 검색 단위가 위키에 없다.
- retrieval: 검색 단위는 있는데 검색 결과에 오르지 못했다. lint로는 고칠 수 없다.
- incomplete: 정답 근거는 찾았지만 기대 주장 일부가 빠졌다.
- imprecise: 주장은 다 덮었지만 무관하거나 출처가 맞지 않는 근거가 섞였다(precision·source_alignment 미달).
"""
from __future__ import annotations

from typing import Any

import psycopg

from app.modules.wiki_generation.infrastructure.post_ingest_quality import MIN_QUALITY_SCORE

QUESTION_FIELDS = ("aligned", "standalone", "unambiguous", "scope_matched", "durable")


def list_quality_review_candidates(
    conn: psycopg.Connection,
    workspace_id: str,
) -> list[dict[str, Any]]:
    # 문서마다 최신 성공 편입의 상태 한 행이다. 다시 편입해 통과했거나 아직 평가 중이면 needs_review가 아니다.
    rows = conn.execute(
        """
        SELECT review.document_id, review.post_ingest_run_id, review.evaluations
        FROM document_quality_reviews review
        WHERE review.workspace_id = %s
          AND review.status = 'needs_review'
          AND NOT EXISTS (
              SELECT 1 FROM wiki_source_tombstones tombstone
              WHERE tombstone.workspace_id = review.workspace_id
                AND tombstone.document_id = review.document_id
          )
        ORDER BY review.document_id
        """,
        (workspace_id,),
    ).fetchall()
    failed = [
        (row, evaluation)
        for row in rows
        for evaluation in row["evaluations"] or []
        if isinstance(evaluation, dict) and not evaluation.get("passed")
    ]
    indexed = _indexed_refs(
        conn,
        workspace_id,
        sorted({ref for row, evaluation in failed for ref in _gold_refs(evaluation, row["document_id"])}),
    )
    return [
        {
            "document_id": row["document_id"],
            "post_ingest_run_id": row["post_ingest_run_id"],
            "question": str(evaluation.get("question") or ""),
            "failure_type": classify_quality_failure(
                evaluation, _gold_refs(evaluation, row["document_id"]), indexed
            ),
            "gold_source_refs": sorted(_gold_refs(evaluation, row["document_id"])),
            "missing_claims": list(evaluation.get("missing_claims") or []),
            "reason": str(evaluation.get("reason") or ""),
        }
        for row, evaluation in failed
    ]


def classify_quality_failure(
    evaluation: dict[str, Any],
    gold_refs: set[str],
    indexed_refs: set[str],
) -> str:
    if not all(evaluation.get(field) is True for field in QUESTION_FIELDS):
        return "question"
    if evaluation.get("contradictory_evidence_ranks"):
        return "contradictory"
    if not evaluation.get("gold_ref_hit"):
        return "retrieval" if gold_refs & indexed_refs else "not_in_wiki"
    if (
        evaluation.get("missing_claims")
        or evaluation.get("answerability") is not True
        or float(evaluation.get("evidence_recall") or 0.0) < MIN_QUALITY_SCORE
    ):
        return "incomplete"
    return "imprecise"


def _gold_refs(evaluation: dict[str, Any], document_id: str) -> set[str]:
    source_document_id = str(evaluation.get("source_document_id") or document_id)
    return {f"{source_document_id}:{block_id}" for block_id in evaluation.get("source_block_ids") or []}


def _indexed_refs(conn: psycopg.Connection, workspace_id: str, refs: list[str]) -> set[str]:
    """활성 페이지의 검색 단위가 근거로 가진 원문 블록만 남긴다. block_refs는 전역 형식이거나 단위 문서 기준 블록 id다."""
    if not refs:
        return set()
    rows = conn.execute(
        """
        SELECT ref
        FROM unnest(%s::text[]) AS ref
        WHERE EXISTS (
            SELECT 1
            FROM wiki_embedding_units unit
            JOIN wiki_pages page ON page.id = unit.page_id
            WHERE page.workspace_id = %s
              AND page.status = 'active'
              AND page.page_type IN ('source', 'concept')
              AND (
                  ref = ANY(unit.block_refs)
                  OR (
                      unit.source_document_id = split_part(ref, ':', 1)
                      AND split_part(ref, ':', 2) = ANY(unit.block_refs)
                  )
              )
        )
        """,
        (refs, workspace_id),
    ).fetchall()
    return {row["ref"] for row in rows}
