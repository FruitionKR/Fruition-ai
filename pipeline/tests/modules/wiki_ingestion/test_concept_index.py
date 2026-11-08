import json

import pytest

from app.modules.wiki_ingestion.application.models import WikiMaintenanceCommand
from app.modules.wiki_ingestion.infrastructure import wiki_maintenance
from app.modules.wiki_ingestion.infrastructure.active_cluster_markdown import (
    merge_active_cluster_markdown,
    parse_active_cluster_lint,
    reconcile_active_cluster_invalidations,
)
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as repository
from app.modules.wiki_ingestion.infrastructure.concept_evidence import append_concept_evidence
from app.modules.wiki_ingestion.infrastructure.embedding_units import extract_embedding_units
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_ingestion_repository import (
    _concept_index_from_markdown,
    _merge_promotion_into_existing_concept,
    _delete_source_related_links,
    _resolve_or_create_wiki_page_id,
)


@pytest.fixture(autouse=True)
def _no_quality_review_rows(monkeypatch) -> None:
    # 이 파일의 lint 테스트는 가짜 연결을 쓴다. 검색 품질 후보 조회는 실제 PostgreSQL 테스트가 따로 본다.
    monkeypatch.setattr(repository, "_list_quality_review_candidates", lambda *_args: [])


def test_list_source_blocks_for_refs_preserves_document_scope(monkeypatch) -> None:
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(repository, "connect", Connection)
    monkeypatch.setattr(
        repository,
        "_source_blocks_for_refs",
        lambda _conn, _refs: [
            {"ref": "doc-a:B0001", "text": "첫 근거"},
            {"ref": "doc-b:B0001", "text": "둘째 근거"},
        ],
    )

    assert repository.list_source_blocks_for_refs(
        ["doc-a:B0001", "doc-b:B0001"]
    ) == [
        {
            "source_document_id": "doc-a",
            "block_id": "B0001",
            "text": "첫 근거",
        },
        {
            "source_document_id": "doc-b",
            "block_id": "B0001",
            "text": "둘째 근거",
        },
    ]


def test_promotion_generator_preserves_representative_and_allowed_refs(monkeypatch) -> None:
    payloads = []

    class FakeClient:
        def complete_json(self, system_prompt, user_prompt):
            payloads.append((system_prompt, json.loads(user_prompt)))
            return {
                "definition": {
                    "text": "공급 계약의 운영 근거다.",
                    "anchor_block_ids": ["B0001", "not-allowed"],
                },
                "evidence": [],
            }

    monkeypatch.setattr(wiki_maintenance, "_lint_api_client", lambda _command: FakeClient())
    generator = wiki_maintenance.PostgresWikiMaintenance()._build_promotion_page_generator(
        WikiMaintenanceCommand("user", "workspace", "provider", "model")
    )

    page = generator(
        {
            "id": "orchid-lease",
            "representative": "Orchid Lease",
            "claims": [
                {
                    "id": "claim_001",
                    "claim": "Orchid Lease는 공급 계약이다.",
                    "refs": ["doc_a:B0001"],
                }
            ],
            "relations": [],
            "source_blocks": [{"ref": "doc_a:B0001", "text": "Orchid Lease"}],
        }
    )

    assert page["title"] == "Orchid Lease"
    assert "# Orchid Lease" in page["markdown"]
    assert "claim_001: Orchid Lease는 공급 계약이다. [doc_a:B0001]" in page["markdown"]
    assert "not-allowed" not in page["markdown"]
    assert payloads[0][1]["cluster"]["representative"] == "Orchid Lease"


def test_lint_merge_does_not_overwrite_previous_operation_snapshot(
    monkeypatch,
) -> None:
    writes = []
    upserts = []

    class Result:
        def fetchone(self):
            return {
                "id": "page-shared",
                "title": "Shared",
                "summary": "공유 개념",
                "markdown_uri": "wiki/ws/pages/page-shared/ops/ingest-A.md",
            }

    class Connection:
        def execute(self, _query, _params):
            return Result()

    monkeypatch.setattr(
        repository,
        "_read_optional_text_object",
        lambda _key: "# Shared\n\n## Evidence\n- 기존 근거\n",
    )
    monkeypatch.setattr(
        repository,
        "write_text_object",
        lambda key, text: writes.append((key, text)) or key,
    )
    monkeypatch.setattr(
        repository,
        "_upsert_wiki_page",
        lambda *args: upserts.append(args),
    )
    monkeypatch.setattr(
        repository,
        "_persist_embedding_units",
        lambda *_args: None,
    )

    change = _merge_promotion_into_existing_concept(
        Connection(),
        "user-1",
        "ws",
        "shared",
        [
            {
                "id": "claim-1",
                "claim": "lint 근거",
                "refs": ["doc-A:B0001"],
            }
        ],
        aliases=[],
        detail_items=[],
    )

    assert writes[0][0] == "wiki/ws/concepts/shared.md"
    assert all("ops/ingest-A.md" not in key for key, _text in writes)
    assert upserts[0][6] == "wiki/ws/concepts/shared.md"
    assert change is not None
    assert "lint 근거" in change["markdown"]


def test_promotion_merge_neutralizes_external_links_in_claims(monkeypatch) -> None:
    writes = []

    class Result:
        def fetchone(self):
            return {
                "id": "page-shared",
                "title": "Shared",
                "summary": "공유 개념",
                "markdown_uri": "wiki/ws/concepts/shared.md",
            }

    class Connection:
        def execute(self, _query, _params):
            return Result()

    monkeypatch.setattr(repository, "_read_optional_text_object", lambda _key: "# Shared\n\n## Evidence\n- 기존 근거\n")
    monkeypatch.setattr(repository, "write_text_object", lambda key, text: writes.append((key, text)) or key)
    monkeypatch.setattr(repository, "_upsert_wiki_page", lambda *_args: None)
    monkeypatch.setattr(repository, "_persist_embedding_units", lambda *_args: None)

    change = _merge_promotion_into_existing_concept(
        Connection(),
        "user-1",
        "ws",
        "shared",
        [{"id": "claim-1", "claim": "lint 근거 [안내](https://attacker.example)", "refs": ["doc-A:B0001"]}],
        aliases=[],
        detail_items=[],
    )

    assert change is not None
    assert "안내 (attacker.example)" in writes[0][1]
    assert change["markdown"] == writes[0][1]


def test_concept_index_uses_markdown_definition_and_evidence() -> None:
    markdown = """---
type: concept
---

# Back EMF

## Definition
Back EMF는 회전 전동기에서 유기되는 역기전력이다. [doc_a:B0001]

## Why It Matters
전동기 성능 평가에 필요하다.

## Aliases
back electromotive force, BEMF

## Evidence
- Back EMF는 토크 리플 및 코깅 토크와 함께 최적화 대상이다. [doc_a:B0002]
- 제조 공차는 Back EMF 응답에 영향을 준다. [doc_b:B0003]

## Reference Summary
- display refs: doc_a:B0001
"""

    concept = _concept_index_from_markdown("back-emf", "Back EMF", "s3://bucket/wiki/concepts/back-emf.md", markdown)

    assert "summary" not in concept
    assert concept["definition"] == "Back EMF는 회전 전동기에서 유기되는 역기전력이다. [doc_a:B0001]"
    assert concept["why_page_worthy"] == "전동기 성능 평가에 필요하다."
    assert concept["aliases"] == ["back electromotive force", "BEMF"]
    # 개념 판정은 이름·별칭·정의만 보므로 근거 목록은 읽지 않는다.
    assert "evidence" not in concept


def test_append_concept_evidence_replaces_placeholder_and_deduplicates() -> None:
    markdown = """# Back EMF

## Definition
Back EMF 정의

## Evidence
- 아직 연결된 evidence claim 없음

## Related Concepts
- 관련 개념 없음
"""

    updated_once = append_concept_evidence(
        markdown,
        [
            {
                "claim_id": "claim_001",
                "claim": "Back EMF는 제조 공차의 영향을 받는다.",
                "refs": ["doc_a:B0001"],
            }
        ],
    )
    updated_twice = append_concept_evidence(
        updated_once,
        [
            {
                "claim_id": "claim_001",
                "claim": "Back EMF는 제조 공차의 영향을 받는다.",
                "refs": ["doc_a:B0001"],
            }
        ],
    )

    assert "- 아직 연결된 evidence claim 없음" not in updated_once
    assert updated_once.count("claim_001: Back EMF는 제조 공차의 영향을 받는다. [doc_a:B0001]") == 1
    assert updated_twice.count("claim_001: Back EMF는 제조 공차의 영향을 받는다. [doc_a:B0001]") == 1
    assert "## Related Concepts" in updated_once


def test_embedding_units_extract_global_source_refs() -> None:
    markdown = """# Source

## Key Points
- Back EMF는 제조 공차의 영향을 받는다. [doc_a:B0001, doc_b:B0002]
"""

    units = extract_embedding_units(markdown)

    assert units[0]["block_refs"] == ["doc_a:B0001", "doc_b:B0002"]
    assert units[0]["text"] == "Back EMF는 제조 공차의 영향을 받는다."


def test_resolve_or_create_wiki_page_id_reuses_existing_uuid() -> None:
    class FakeConn:
        def __init__(self) -> None:
            self.rows = []
            self.page_id = None

        def execute(self, _query: str, params: tuple[str, str, str, str]):
            self.rows.append(params)
            return self

        def fetchone(self):
            if self.page_id is None:
                self.page_id = self.rows[0][0]
            return {"id": self.page_id}

    conn = FakeConn()

    first_id = _resolve_or_create_wiki_page_id(conn, "user_1", "workspace_1", "concept", "back-emf")
    second_id = _resolve_or_create_wiki_page_id(conn, "user_1", "workspace_1", "concept", "back-emf")

    assert first_id.startswith("wiki_page_")
    assert "back-emf" not in first_id
    assert second_id == first_id


def test_parse_active_cluster_lint_reads_promotion_and_refs() -> None:
    markdown = """# Active Meaning Clusters

## cluster: back-emf

representative: Back EMF

### Evidence Claims
- claim_001: Back EMF는 제조 공차의 영향을 받는다. [doc_a:B0001, doc_b:B0002]

### Promotion
status: candidate
source_refs: [doc_a, doc_b]
reason: definition/evidence/relation이 충분함
"""

    clusters = parse_active_cluster_lint(markdown)

    assert clusters == [
        {
            "id": "back-emf",
            "representative": "Back EMF",
            "refs": ["doc_a:B0001", "doc_b:B0002"],
            "claims": [
                    {
                        "id": "claim_001",
                        "text": "Back EMF는 제조 공차의 영향을 받는다. [doc_a:B0001, doc_b:B0002]",
                        "claim": "Back EMF는 제조 공차의 영향을 받는다.",
                        "refs": ["doc_a:B0001", "doc_b:B0002"],
                        "decision": "",
                    }
                ],
                "promotion_status": "candidate",
                "promotion_source_refs": ["doc_a", "doc_b"],
                "promotion_target": "",
        }
    ]


def test_merge_active_cluster_markdown_deduplicates_claims() -> None:
    existing = """# Active Meaning Clusters

## cluster: back-emf

### Evidence Claims
- claim_001: Back EMF는 제조 공차의 영향을 받는다. [doc_a:B0001]
"""
    incoming = """# Active Meaning Clusters

## cluster: back-emf

### Evidence Claims
- claim_001: Back EMF는 제조 공차의 영향을 받는다. [doc_a:B0001]
- claim_002: Back EMF는 토크 리플 분석에도 쓰인다. [doc_b:B0002]
"""

    merged = merge_active_cluster_markdown(existing, incoming)

    assert merged.count("claim_001") == 1
    assert "claim_002: Back EMF는 토크 리플 분석에도 쓰인다. [doc_b:B0002]" in merged


def test_merge_active_cluster_markdown_fills_missing_representative() -> None:
    existing = """# Active Meaning Clusters

## cluster: back-emf

type: term_cluster

### Evidence Claims
- claim_001: 기존 근거 [doc_a:B0001]
"""
    incoming = """# Active Meaning Clusters

## cluster: back-emf

type: term_cluster
representative: Back EMF

### Evidence Claims
- claim_002: 신규 근거 [doc_b:B0002]
"""

    merged = merge_active_cluster_markdown(existing, incoming)

    assert parse_active_cluster_lint(merged)[0]["representative"] == "Back EMF"
    assert merged.count("representative: Back EMF") == 1


def test_merge_active_cluster_markdown_preserves_existing_representative() -> None:
    existing = """# Active Meaning Clusters

## cluster: back-emf

type: term_cluster
representative: Existing Back EMF

### Evidence Claims
- claim_001: 기존 근거 [doc_a:B0001]
"""
    incoming = """# Active Meaning Clusters

## cluster: back-emf

type: term_cluster
representative: Incoming Back EMF

### Evidence Claims
- claim_002: 신규 근거 [doc_b:B0002]
"""

    merged = merge_active_cluster_markdown(existing, incoming)

    assert parse_active_cluster_lint(merged)[0]["representative"] == "Existing Back EMF"
    assert "representative: Incoming Back EMF" not in merged


def test_merge_active_cluster_markdown_accumulates_promotion_source_refs() -> None:
    existing = """# Active Meaning Clusters

## cluster: back-emf

### Evidence Claims
- claim_001: 기존 근거 [doc_a:B0001]

### Promotion
status: candidate
source_refs: [doc_a]
reason: 기존 누적 근거
"""
    incoming = """# Active Meaning Clusters

## cluster: back-emf

### Evidence Claims
- claim_002: 추가 근거 [doc_a:B0001]
- claim_003: 추가 근거 [doc_b:B0002]

### Promotion
status: candidate
source_refs: [doc_b]
reason: 신규 누적 근거
"""

    merged = merge_active_cluster_markdown(existing, incoming)

    assert merged.count("claim_001") == 1
    assert merged.count("claim_002") == 1
    assert merged.count("claim_003") == 1
    assert merged.count("### Promotion") == 1
    assert "source_refs: [doc_a, doc_b]" in merged


def test_merge_active_cluster_markdown_replaces_non_candidate_promotion() -> None:
    existing = """# Active Meaning Clusters

## cluster: back-emf

### Evidence Claims
- claim_001: 기존 근거 [doc_a:B0001]

### Promotion
status: needs_review
source_refs: [doc_a]
reason: 판단 보류
"""
    incoming = """# Active Meaning Clusters

## cluster: back-emf

### Evidence Claims
- evidence claim 없음

### Promotion
status: candidate
source_refs: []
reason: 다른 문서에서 핵심 개념으로 추출됨
target: concept:back-electromotive-force
"""

    merged = merge_active_cluster_markdown(existing, incoming)
    [cluster] = parse_active_cluster_lint(merged)

    assert merged.count("### Promotion") == 1
    assert cluster["promotion_status"] == "candidate"
    assert cluster["promotion_target"] == "back-electromotive-force"
    assert cluster["promotion_source_refs"] == ["doc_a"]
    assert "evidence claim 없음" not in merged


def test_lint_materialize_keeps_concurrent_ingest_updates(monkeypatch) -> None:
    calls = []
    active_markdown = {
        "value": """# Active Meaning Clusters

## cluster: shared

### Evidence Claims
- claim_001: 기존 근거 [doc-1:B0001]
- claim_003: 다른 문서 근거 [doc-3:B0001]

### Promotion
status: candidate
source_refs: [doc-1, doc-3]
"""
    }
    concept_indexes = iter(({}, {"shared": "page-shared"}))
    # 승격 개수 규칙은 아래 전용 테스트가 본다.
    monkeypatch.setattr(repository, "cluster_reaches_promotion", lambda _count: True)

    def candidate(run_id, claim_signatures=None):
        return {
            "pipeline_run_id": run_id,
            "document_id": "doc-2",
            "user_id": "user-1",
            "workspace_id": "workspace-1",
            "invalidated_source_refs": ["doc-2:B0001"],
            "stale_concept_slugs": [],
            "stale_relations": [],
            "structural_reconciled": False,
            "_cluster_reconciliation_ready": True,
            "_current_claim_signatures": claim_signatures or [],
        }

    reconciliation_candidates = iter(
        (
            [candidate("run-before-ingest")],
            [
                candidate(
                    "run-after-ingest",
                    [
                        ["shared", "claim_002", "ingest 중 추가된 근거 [doc-2:B0001]"]
                    ],
                )
            ],
        )
    )

    class Result:
        def fetchone(self):
            return {
                "id": "page-shared",
                "title": "Shared",
                "summary": "공유 개념",
                "markdown_uri": "wiki/workspace-1/concepts/shared.md",
            }

    class Connection:
        def execute(self, _query, _params):
            return Result()

    monkeypatch.setattr(
        repository,
        "_load_existing_concept_ids_by_slug",
        lambda *_args: next(concept_indexes),
    )
    monkeypatch.setattr(
        repository,
        "_load_concept_identity_index",
        lambda *_args: [
            {"id": "page-shared", "slug": "shared", "title": "Shared", "identity_aliases": [], "status": "active"},
        ],
    )
    monkeypatch.setattr(repository, "_orphan_source_refs", lambda _refs: [])

    def load_reconciliation_candidates(*_args):
        candidates = next(reconciliation_candidates)
        calls.append(
            "candidates:fresh"
            if candidates[0]["_current_claim_signatures"]
            else "candidates:stale"
        )
        return candidates

    monkeypatch.setattr(
        repository,
        "_list_reconciliation_candidates",
        load_reconciliation_candidates,
    )
    monkeypatch.setattr(repository, "_active_relation_keys", lambda *_args: set())
    monkeypatch.setattr(
        repository,
        "_apply_structural_reconciliation",
        lambda _conn, candidates, _relation_keys: (
            calls.append("structural")
            or [{"pipeline_run_id": candidates[0]["pipeline_run_id"]}]
        ),
    )
    monkeypatch.setattr(repository, "_source_blocks_for_refs", lambda *_args: [])
    monkeypatch.setattr(
        repository,
        "_read_optional_text_object",
        lambda path: (
            active_markdown["value"]
            if path.endswith("clusters/active.md")
            else "# Shared\n\n## Evidence\n- ingest가 먼저 저장한 근거\n"
        ),
    )
    monkeypatch.setattr(
        repository,
        "_lock_concept_persistence",
        lambda *_args: calls.append("lock"),
    )
    monkeypatch.setattr(repository, "_upsert_wiki_page", lambda *_args: None)
    monkeypatch.setattr(repository, "_persist_embedding_units", lambda *_args: None)

    def generate_page(_input):
        calls.append("llm")
        active_markdown["value"] = """# Active Meaning Clusters

## cluster: shared

### Evidence Claims
- claim_001: 기존 근거 [doc-1:B0001]
- claim_002: ingest 중 추가된 근거 [doc-2:B0001]

### Promotion
status: candidate
source_refs: [doc-1, doc-2]

## cluster: ingest-new

### Evidence Claims
- claim_003: ingest가 추가한 cluster [doc-2:B0002]
"""
        return {"slug": "shared", "title": "Shared", "markdown": "# Shared"}

    result = repository.lint_wiki_workspace(
        "user-1",
        "workspace-1",
        materialize_promotions=True,
        promotion_page_generator=generate_page,
        apply_reconciliation=True,
        operation_id="lint-op-1",
        write_log=False,
        connection=Connection(),
    )

    assert calls == [
        "candidates:stale",
        "llm",
        "lock",
        "candidates:fresh",
        "structural",
    ]
    assert result["applied_reconciliations"] == [
        {"pipeline_run_id": "run-after-ingest"}
    ]
    assert result["applied_cluster_reconciliation"] == {"removed_claims": []}
    assert "ingest 중 추가된 근거" in result["_lint_page_changes"][0]["markdown"]
    assert "ingest 중 추가된 근거" in result["_lint_object_changes"]["archived_sections"][0]

def test_lint_wiki_workspace_dry_run_does_not_write_log(monkeypatch) -> None:
    writes = []
    monkeypatch.setattr(repository, "_read_optional_text_object", lambda _path: "")
    monkeypatch.setattr(repository, "write_text_object", lambda path, text: writes.append((path, text)))
    monkeypatch.setattr(repository, "_list_reconciliation_candidates", lambda *_args: [])

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(repository, "connect", FakeConnection)

    result = repository.lint_wiki_workspace("user_1", "workspace_1", write_log=False)

    assert result["active_path"] == "wiki/workspace_1/clusters/active.md"
    assert writes == []


def test_lint_blocks_promotion_when_reingest_invalidated_its_source_ref(
    monkeypatch,
) -> None:
    active_markdown = """# Active Meaning Clusters

## cluster: stale-cluster

### Evidence Claims
- claim_001: 수정 전 주장이다. [doc_1:B0002]

### Promotion
status: candidate
source_refs: [doc_1]
"""
    monkeypatch.setattr(
        repository,
        "_read_optional_text_object",
        lambda _path: active_markdown,
    )
    monkeypatch.setattr(repository, "_orphan_source_refs", lambda _refs: [])
    monkeypatch.setattr(
        repository,
        "_list_reconciliation_candidates",
        lambda *_args: [
            {
                "pipeline_run_id": "run-2",
                "document_id": "doc_1",
                "invalidated_source_refs": ["doc_1:B0002"],
                "stale_concept_slugs": [],
                "stale_relations": [],
                "structural_reconciled": False,
                "_current_claim_signatures": [],
            }
        ],
    )

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(repository, "connect", FakeConnection)

    result = repository.lint_wiki_workspace(
        "user_1",
        "workspace_1",
        write_log=False,
    )

    assert result["promotion_candidates"] == []
    assert result["needs_review"] == ["stale-cluster"]


def _term_claims(prefix: str, terms: list[str], document_id: str = "doc_1") -> str:
    return "\n".join(
        f"- claim_{prefix}_{index:03d}: {term} - {term}의 근거 문장이다. [{document_id}:B{index:04d}]"
        for index, term in enumerate(terms, start=1)
    )


ROLLOUT_TERMS = ["Rollout History", "Rollout Undo", "Rollout Pause", "Rollout Resume", "Rollout Status"]


def test_lint_promotes_only_candidates_with_enough_detail_items(monkeypatch) -> None:
    # 세부 항목은 근거 줄 용어를 이름 정규화로 묶은 것이다. 표기만 다른 용어·클러스터 자신의 이름은 세지 않는다.
    active_markdown = f"""# Active Meaning Clusters

## cluster: few-items

representative: Few Items

### Evidence Claims
{_term_claims("a", ["Few Items", "rollout-history", "Rollout History", "Rollout Undo", "Rollout Pause", "Rollout Resume"])}
- ev_0001: 용어 없는 근거 주장이다. [doc_2:B0001]

### Promotion
status: candidate
source_refs: [doc_1, doc_2]

## cluster: enough-items

### Evidence Claims
{_term_claims("b", ROLLOUT_TERMS)}

### Promotion
status: candidate
source_refs: [doc_1]

## cluster: concept-match

### Evidence Claims
- claim_005: 다른 문서의 핵심 개념과 같다. [doc_1:B0004]

### Promotion
status: candidate
source_refs: [doc_1]
target: concept:shared-concept
"""
    monkeypatch.setattr(repository, "_read_optional_text_object", lambda _path: active_markdown)
    monkeypatch.setattr(repository, "_orphan_source_refs", lambda _refs: [])
    monkeypatch.setattr(repository, "_list_reconciliation_candidates", lambda *_args: [])

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(repository, "connect", FakeConnection)

    result = repository.lint_wiki_workspace("user_1", "workspace_1", write_log=False)

    # 세부 항목이 P개 미만인 후보는 근거 문서가 여럿이어도 승격하지 않는다. 핵심 개념과 맞춘 후보는 그 개념에 병합한다.
    assert result["promotion_candidates"] == ["enough-items", "concept-match"]
    assert {"cluster_id": "few-items", "reason": "promotion candidate has too few detail items"} in result[
        "invalid_promotions"
    ]


def test_lint_does_not_block_claim_regenerated_by_latest_reingest(
    monkeypatch,
) -> None:
    active_markdown = """# Active Meaning Clusters

## cluster: current-cluster

### Evidence Claims
- claim_002: 수정 후 최신 주장이다. [doc_1:B0002]
- claim_003: 다른 문서의 같은 주장이다. [doc_2:B0001]

### Promotion
status: candidate
source_refs: [doc_1, doc_2]
"""
    monkeypatch.setattr(
        repository,
        "_read_optional_text_object",
        lambda _path: active_markdown,
    )
    monkeypatch.setattr(repository, "_orphan_source_refs", lambda _refs: [])
    monkeypatch.setattr(
        repository,
        "_list_reconciliation_candidates",
        lambda *_args: [
            {
                "pipeline_run_id": "run-2",
                "document_id": "doc_1",
                "invalidated_source_refs": ["doc_1:B0002"],
                "stale_concept_slugs": [],
                "stale_relations": [],
                "structural_reconciled": False,
                "_current_claim_signatures": [
                    [
                        "current-cluster",
                        "claim_002",
                        "수정 후 최신 주장이다. [doc_1:B0002]",
                    ]
                ],
            }
        ],
    )

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(repository, "connect", FakeConnection)

    monkeypatch.setattr(repository, "cluster_reaches_promotion", lambda _count: True)
    result = repository.lint_wiki_workspace(
        "user_1",
        "workspace_1",
        write_log=False,
    )

    assert result["promotion_candidates"] == ["current-cluster"]
    assert result["needs_review"] == []


def test_lint_does_not_remove_claim_for_legacy_reingest_without_signatures(
    monkeypatch,
) -> None:
    active_markdown = """# Active Meaning Clusters

## cluster: legacy-cluster

### Evidence Claims
- claim_001: 최신 여부를 판별할 수 없는 주장이다. [doc_1:B0002]
"""
    reconciled_refs: list[set[str]] = []
    monkeypatch.setattr(
        repository,
        "_read_optional_text_object",
        lambda _path: active_markdown,
    )
    monkeypatch.setattr(repository, "_orphan_source_refs", lambda _refs: [])
    monkeypatch.setattr(
        repository,
        "_list_reconciliation_candidates",
        lambda *_args: [
            {
                "pipeline_run_id": "run-2",
                "document_id": "doc_1",
                "invalidated_source_refs": ["doc_1:B0002"],
                "stale_concept_slugs": [],
                "stale_relations": [],
                "structural_reconciled": False,
                "_cluster_reconciliation_ready": False,
                "_current_claim_signatures": [],
            }
        ],
    )
    monkeypatch.setattr(
        repository,
        "_reconcile_active_cluster_invalidations",
        lambda markdown, refs, *_args: (
            reconciled_refs.append(refs) or markdown,
            [],
        ),
    )
    monkeypatch.setattr(repository, "_active_relation_keys", lambda *_args: set())
    monkeypatch.setattr(
        repository,
        "_apply_structural_reconciliation",
        lambda *_args: [],
    )

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(repository, "connect", FakeConnection)

    result = repository.lint_wiki_workspace(
        "user_1",
        "workspace_1",
        apply_reconciliation=True,
        write_log=False,
    )

    assert reconciled_refs == [set()]
    assert result["needs_review"] == ["legacy-cluster"]


def test_reconcile_active_clusters_removes_only_stale_reingest_claims() -> None:
    markdown = """# Active Meaning Clusters

## cluster: motor

### Evidence Claims
- claim_old: 수정 전 주장이다. [doc_1:B0002]
- claim_new: 수정 후 주장이다. [doc_1:B0002]
- claim_other: 다른 문서 주장이다. [doc_2:B0001]
"""

    reconciled, removed_claims = reconcile_active_cluster_invalidations(
        markdown,
        {"doc_1:B0002"},
        {("motor", "claim_new", "수정 후 주장이다. [doc_1:B0002]")},
    )

    assert "claim_old" not in reconciled
    assert "claim_new" in reconciled
    assert "claim_other" in reconciled
    assert removed_claims[0]["claim_id"] == "claim_old"


def test_delete_source_related_links_is_scoped_to_workspace() -> None:
    class EmptyRows:
        def fetchall(self):
            return []

    class FakeConn:
        def __init__(self) -> None:
            self.calls = []

        def execute(self, query: str, params=None):
            self.calls.append((" ".join(query.split()), params))
            return EmptyRows()

    conn = FakeConn()

    _delete_source_related_links(conn, "workspace_1")

    assert len(conn.calls) == 1
    assert conn.calls[0][1] == ("workspace_1", "workspace_1")
    assert "user_id" not in conn.calls[0][0]
    assert "DELETE FROM wiki_page_links" in conn.calls[0][0]
    assert "from_page.workspace_id = %s" in conn.calls[0][0]


class _FakeConceptJudge:
    def __init__(self, resolutions=None, error=None) -> None:
        self.resolutions = resolutions or {}
        self.error = error
        self.calls = []

    def judge_concepts(self, concepts, pool):
        self.calls.append((concepts, pool))
        if self.error:
            raise self.error
        return self.resolutions


def _materialize_with_judge(monkeypatch, judge, page_slug="orchid-lease", representative="Orchid Lease"):
    upserts = []
    links = []
    monkeypatch.setattr(repository, "cluster_reaches_promotion", lambda _count: True)
    active = f"""# Active Meaning Clusters

## cluster: orchid-lease

representative: {representative}

### Evidence Claims
- claim_001: Orchid Lease는 공급 계약이다. [doc-1:B0001]
- claim_002: Orchid Lease는 다른 계약서에도 나온다. [doc-2:B0001]

### Promotion
status: candidate
source_refs: [doc-1, doc-2]
"""

    class Result:
        def fetchone(self):
            return {
                "id": "page-supply",
                "title": "공급 계약",
                "summary": "공급 조건을 정한 계약",
                "markdown_uri": "wiki/ws/concepts/supply-contract.md",
            }

    class Connection:
        def execute(self, _query, _params):
            return Result()

    monkeypatch.setattr(repository, "_load_existing_concept_ids_by_slug", lambda *_args: {"supply-contract": "page-supply"})
    monkeypatch.setattr(
        repository,
        "_load_concept_identity_index",
        lambda *_args: [
            {"id": "page-supply", "slug": "supply-contract", "title": "공급 계약", "identity_aliases": [], "status": "active"},
        ],
    )
    monkeypatch.setattr(repository, "_source_blocks_for_refs", lambda *_args: [])
    monkeypatch.setattr(repository, "_lock_concept_persistence", lambda *_args: None)
    monkeypatch.setattr(
        repository,
        "_read_optional_text_object",
        lambda path: active
        if path.endswith("clusters/active.md")
        else "# 공급 계약\n\n## Aliases\n-\n\n## Evidence\n- 기존 근거\n",
    )
    monkeypatch.setattr(
        repository,
        "list_active_concept_index",
        lambda _workspace_id, *, use_cache: [
            {"slug": "supply-contract", "title": "공급 계약", "aliases": [], "definition": "공급 조건을 정한 계약"},
            {"slug": "draft", "title": "초안", "aliases": [], "definition": ""},
        ],
    )
    monkeypatch.setattr(repository, "_resolve_or_create_wiki_page_id", lambda *_args: "page-orchid")
    monkeypatch.setattr(
        repository,
        "_resolve_concept_page_identity",
        lambda _conn, _user, _workspace, *, title, identity_aliases, preferred_slug: (
            "page-orchid",
            "supply-contract-2" if preferred_slug == "supply-contract" else preferred_slug,
            "new",
        ),
    )
    monkeypatch.setattr(repository, "_upsert_wiki_page", lambda *args: upserts.append(args))
    monkeypatch.setattr(repository, "_persist_embedding_units", lambda *_args: None)
    monkeypatch.setattr(repository, "_upsert_wiki_page_link", lambda *args: links.append(args[1:]))
    page = {
        "slug": page_slug,
        "title": "Orchid Lease",
        "markdown": "# Orchid Lease\n\n## Definition\nOrchid Lease는 공급 계약이다.\n\n## Aliases\n-\n",
    }
    result = repository._materialize_promotion_candidates(
        Connection(),
        "user-1",
        "ws",
        parse_active_cluster_lint(active),
        active,
        lambda _cluster: page,
        concept_judge=judge,
        operation_id="lint-op-1",
    )
    return result, upserts, links


def test_lint_promotion_judged_same_merges_into_existing_concept(monkeypatch) -> None:
    judge = _FakeConceptJudge(
        {"orchid-lease": {"incoming_slug": "orchid-lease", "decision": "merge_into", "canonical_slug": "supply-contract"}}
    )

    result, upserts, links = _materialize_with_judge(monkeypatch, judge)

    [(concepts, pool)] = judge.calls
    assert concepts == [
        {"slug": "orchid-lease", "title": "Orchid Lease", "aliases": [], "definition": "Orchid Lease는 공급 계약이다."}
    ]
    assert [concept["slug"] for concept in pool] == ["supply-contract", "draft"]
    assert result["promotions"] == []
    assert result["merged_promotions"] == [
        {"cluster_id": "orchid-lease", "concept_slug": "supply-contract", "page_id": "page-supply"}
    ]
    assert [upsert[1] for upsert in upserts] == ["page-supply"]
    assert links == []
    assert "merged_to: concept:supply-contract" in result["archived_sections"][0]


def test_lint_judged_merge_adds_cluster_representative_to_concept_aliases(monkeypatch) -> None:
    judge = _FakeConceptJudge(
        {"orchid-lease": {"incoming_slug": "orchid-lease", "decision": "merge_into", "canonical_slug": "supply-contract"}}
    )

    result, _upserts, _links = _materialize_with_judge(monkeypatch, judge)

    # 클러스터 id는 대표명과 대소문자·하이픈만 달라 한 번만 넣는다.
    [merged] = result["page_changes"]
    concept = _concept_index_from_markdown("supply-contract", "공급 계약", "uri", merged["markdown"])
    assert concept["aliases"] == ["Orchid Lease"]
    assert merged["aliases"] == ["Orchid Lease"]
    assert "- 기존 근거" in merged["markdown"]


def test_lint_judged_merge_keeps_representative_and_cluster_id_as_separate_aliases(monkeypatch) -> None:
    judge = _FakeConceptJudge(
        {"orchid-lease": {"incoming_slug": "orchid-lease", "decision": "merge_into", "canonical_slug": "supply-contract"}}
    )

    result, _upserts, _links = _materialize_with_judge(monkeypatch, judge, representative="오키드 리스")

    # 대표명과 클러스터 id가 다르면 별칭 두 개로 읽혀야 다음 편입의 이름 일치가 둘 다 찾는다.
    [merged] = result["page_changes"]
    concept = _concept_index_from_markdown("supply-contract", "공급 계약", "uri", merged["markdown"])
    assert concept["aliases"] == ["오키드 리스", "orchid-lease"]


def test_lint_promotion_judged_related_creates_page_without_links(monkeypatch) -> None:
    judge = _FakeConceptJudge(
        {
            "orchid-lease": {
                "incoming_slug": "orchid-lease",
                "decision": "link_to",
                "canonical_slug": "supply-contract",
                "link_targets": ["supply-contract"],
                "link_confidences": {"supply-contract": 0.95},
            }
        }
    )

    result, _upserts, links = _materialize_with_judge(monkeypatch, judge)

    # 관련 개념 판정으로 링크를 덧붙이지 않는다(ADR 0042). 연결은 하위 항목 배정과 참조가 맡는다.
    assert result["promotions"] == [{"cluster_id": "orchid-lease", "concept_slug": "orchid-lease", "page_id": "page-orchid"}]
    assert links == []
    assert "added_links" not in result["page_changes"][0]


def test_lint_promotion_without_jev_judgment_creates_page(monkeypatch) -> None:
    from app.core.jev_client import JevUnavailable

    for judge in (None, _FakeConceptJudge(error=JevUnavailable("down"))):
        result, _upserts, links = _materialize_with_judge(monkeypatch, judge)

        assert [item["concept_slug"] for item in result["promotions"]] == ["orchid-lease"]
        assert result["merged_promotions"] == []
        assert links == []


def test_lint_promotion_generated_slug_collision_uses_allocator_not_existing_identity(monkeypatch) -> None:
    result, upserts, _links = _materialize_with_judge(monkeypatch, None, page_slug="Supply Contract")

    assert result["promotions"] == [
        {"cluster_id": "orchid-lease", "concept_slug": "supply-contract-2", "page_id": "page-orchid"}
    ]
    assert result["merged_promotions"] == []
    [upsert] = upserts
    assert upsert[2:5] == ("concept", "Orchid Lease", "supply-contract-2")
    assert result["page_changes"][0]["content_action"] == "create"
    assert "promoted_to: concept:supply-contract-2" in result["archived_sections"][0]


def test_lint_promotion_raw_cluster_slug_collision_reaches_common_allocator(monkeypatch) -> None:
    cluster = {
        "id": "c",
        "representative": "C#",
        "promotion_status": "candidate",
        "promotion_source_refs": ["doc:B1"],
        "claims": [{"id": "claim-1", "claim": "C# evidence", "refs": ["doc:B1"]}],
        "detail_items": [],
    }
    generated = []
    allocated = []
    monkeypatch.setattr(repository, "_reaches_promotion", lambda _cluster: True)
    monkeypatch.setattr(repository, "_load_existing_concept_ids_by_slug", lambda *_args: {"c": "page-c"})
    monkeypatch.setattr(repository, "_load_concept_identity_index", lambda *_args: [
        {"id": "page-c", "slug": "c", "title": "C", "identity_aliases": [], "status": "active"},
    ])
    monkeypatch.setattr(repository, "_read_optional_text_object", lambda _key: "active")
    monkeypatch.setattr(repository, "_parse_active_cluster_lint", lambda _markdown: [cluster])
    monkeypatch.setattr(repository, "_cluster_sections_by_id", lambda _markdown: {})
    monkeypatch.setattr(repository, "_source_blocks_for_refs", lambda *_args: [])
    monkeypatch.setattr(repository, "_lock_concept_persistence", lambda *_args: None)
    monkeypatch.setattr(repository, "_add_promotion_entries", lambda _conn, _workspace, _page, markdown, *_args: markdown)
    monkeypatch.setattr(repository, "_upload_wiki_markdown", lambda _markdown, key: key)
    monkeypatch.setattr(repository, "_upsert_wiki_page", lambda *_args: None)
    monkeypatch.setattr(repository, "_persist_embedding_units", lambda *_args: None)
    monkeypatch.setattr(repository, "sync_source_concept_links", lambda *_args, **_kwargs: None)

    def resolve(_conn, _user, _workspace, *, title, identity_aliases, preferred_slug):
        allocated.append((title, preferred_slug))
        return "page-c-sharp", "c-2", "new"

    monkeypatch.setattr(repository, "_resolve_concept_page_identity", resolve)

    def generate(payload):
        generated.append(payload["id"])
        return {"slug": "c", "title": "C#", "markdown": "# C#\n\n## Definition\nC# language\n"}

    result = repository._materialize_promotion_candidates(
        object(), "user", "workspace", [cluster], "active", generate,
    )

    assert generated == ["c"]
    assert allocated == [("C#", "c")]
    assert result["merged_promotions"] == []
    assert result["promotions"] == [
        {"cluster_id": "c", "concept_slug": "c-2", "page_id": "page-c-sharp"},
    ]


def test_lint_restore_returns_archived_cluster_to_active(monkeypatch) -> None:
    from contextlib import nullcontext

    from app.modules.wiki_ingestion.application.models import LintOperationRestoreCommand
    from app.modules.wiki_ingestion.application.restore_wiki_pages import RestoreWikiPagesUseCase
    from app.modules.wiki_ingestion.infrastructure.wiki_page_restore import ObjectStorageWikiPageRestore

    judge = _FakeConceptJudge(
        {"orchid-lease": {"incoming_slug": "orchid-lease", "decision": "merge_into", "canonical_slug": "supply-contract"}}
    )
    result, _upserts, _links = _materialize_with_judge(monkeypatch, judge)
    objects = {"wiki/ws/clusters/active.md": result["active_markdown"]}
    monkeypatch.setattr(
        repository,
        "write_text_object",
        lambda key, text, content_type="": objects.__setitem__(key, text) or key,
    )
    monkeypatch.setattr(repository, "_read_optional_text_object", lambda key: objects.get(key, ""))
    monkeypatch.setattr(repository, "concept_write_lock", lambda *_args: nullcontext())
    repository.persist_lint_operation_result("ws", "lint-op-1", {"_lint_page_changes": result["page_changes"]})
    repository.apply_lint_object_changes(
        {
            "workspace_id": "ws",
            "_lint_object_changes": {
                "active_path": "wiki/ws/clusters/active.md",
                "active_markdown": result["active_markdown"],
                "archived_sections": result["archived_sections"],
            },
        }
    )
    # 이후 다른 클러스터가 보관돼도 그 항목은 남긴다.
    objects["wiki/ws/clusters/archived.md"] += "\n## cluster: other\n\n### Archived\nmerged_to: concept:x\n"
    assert "orchid-lease" not in objects["wiki/ws/clusters/active.md"]

    use_case = RestoreWikiPagesUseCase(
        ObjectStorageWikiPageRestore(
            objects.__getitem__,
            lambda key, text, content_type: objects.__setitem__(key, text) or key,
            lambda *_args: [],
            repository.restore_archived_clusters,
        )
    )

    def restore(operation_id: str) -> dict:
        return use_case.execute_lint(
            LintOperationRestoreCommand(
                operation_id=operation_id,
                target_operation_id="lint-op-1",
                workspace_id="ws",
                rebuild_pages=(),
                deleted_pages=("page-supply",),
            )
        )

    assert restore("restore-1")["cluster_changes"] == {
        "written_object_keys": ["wiki/ws/clusters/active.md", "wiki/ws/clusters/archived.md"],
        "restored_cluster_ids": ["orchid-lease"],
        "skipped_cluster_ids": [],
    }
    assert parse_active_cluster_lint(objects["wiki/ws/clusters/active.md"])[0]["promotion_status"] == "candidate"
    assert "orchid-lease" not in objects["wiki/ws/clusters/archived.md"]
    assert "## cluster: other" in objects["wiki/ws/clusters/archived.md"]

    # 같은 id의 클러스터가 다시 생겼으면 덮어쓰지 않고 건너뛴 id를 알린다.
    objects["wiki/ws/clusters/archived.md"] += "\n\n" + result["archived_sections"][0] + "\n"
    objects["wiki/ws/clusters/active.md"] = "# Active Meaning Clusters\n\n## cluster: orchid-lease\n\nrepresentative: 새 클러스터\n"
    assert restore("restore-2")["cluster_changes"] == {
        "written_object_keys": [],
        "restored_cluster_ids": [],
        "skipped_cluster_ids": ["orchid-lease"],
    }
    assert "새 클러스터" in objects["wiki/ws/clusters/active.md"]
