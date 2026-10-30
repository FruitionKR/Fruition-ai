import json
from pathlib import Path

from app.modules.wiki_generation.domain.entities import SourceBlock, SourceDocument
from app.modules.wiki_generation.infrastructure.assemble import (
    GeneratedConceptPageAssembler,
    LinkBuilder,
    MeaningClusterArtifactAssembler,
    SourcePageAssembler,
)
from app.modules.wiki_generation.infrastructure.normalize import SemanticNormalizer
from app.modules.wiki_generation.infrastructure.source_context_merge import source_page_context_normalized


def test_source_extraction_artifact_uses_embedding_friendly_terms(tmp_path: Path) -> None:
    document = SourceDocument(
        document_id="doc_test",
        title="테스트 문서",
        source_path="examples/test.md",
        content_sha1="abc123",
    )
    blocks = [
        SourceBlock("doc_test", "B0001", "ref_test_md_b0001", "핵심 개념은 검색 인덱스이다.", 1, 1),
        SourceBlock("doc_test", "B0002", "ref_test_md_b0002", "토큰화는 검색 인덱스를 설명하는 하위 절이다.", 2, 2),
        SourceBlock("doc_test", "B0003", "ref_test_md_b0003", "BM25는 예시로 언급된다.", 3, 3),
    ]
    notes = [
        {
            "chunk_id": "chunk_0001",
            "semantic_summary": "검색 인덱스를 설명하는 문서이다.",
            "key_points": [{"text": "검색 인덱스가 핵심이다.", "anchor_block_ids": ["B0001"]}],
            "observations": [
                {
                    "type": "qa_episode",
                    "title": "검색 인덱스의 역할 질문",
                    "query_text": "검색 인덱스가 왜 필요해?",
                    "summary": "검색 인덱스는 문서를 검색 가능한 구조로 바꿔 후속 질의의 후보가 된다.",
                    "claims": ["검색 인덱스는 후속 질의 후보를 찾는 데 쓰인다."],
                    "related_concept_hints": ["search-index"],
                    "anchor_block_ids": ["B0001", "B0002"],
                }
            ],
            "categories": [{"name": "technology"}],
            "core_concepts": [
                {
                    "title": "검색 인덱스",
                    "slug_hint": "search-index",
                    "aliases": ["search index"],
                    "definition": "검색 가능한 구조화 표현이다.",
                    "why_page_worthy": "문서 이해의 중심 개념이다.",
                    "evidence_block_ids": ["B0001"],
                }
            ],
            "section_candidates": [
                {
                    "title": "토큰화",
                    "slug_hint": "tokenization",
                    "context": "검색 인덱스를 설명하는 하위 항목이다.",
                    "evidence_block_ids": ["B0002"],
                }
            ],
            "mentions": [
                {
                    "name": "BM25",
                    "slug_hint": "bm25",
                    "context": "검색 예시로 언급된다.",
                    "evidence_block_ids": ["B0003"],
                }
            ],
            "evidence_claims": [],
            "needs_neighbor_context": False,
            "context_problem": None,
        }
    ]

    normalized = SemanticNormalizer(document, blocks).normalize_notes(notes)
    source_page = SourcePageAssembler().assemble(normalized, tmp_path)
    artifact_path = Path(source_page).with_suffix(".json")
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))

    assert artifact["schema_version"] == "source-extraction.v1"
    assert artifact["categories"] == ["technology"]
    assert artifact["observations"][0]["observation_id"] == "O001"
    assert artifact["observations"][0]["type"] == "qa_episode"
    assert artifact["observations"][0]["anchor_reference_ids"] == ["B0001", "B0002"]
    assert artifact["core_concepts"][0]["term"] == "검색 인덱스"
    assert artifact["core_concepts"][0]["slug"] == "search-index"
    assert artifact["core_concepts"][0]["context"] == "검색 가능한 구조화 표현이다."
    assert artifact["section_candidates"][0]["context"] == "검색 인덱스를 설명하는 하위 항목이다."
    assert artifact["mentions"][0]["term"] == "BM25"
    assert "bucket" not in artifact["core_concepts"][0]
    assert "source_reference_ids" not in artifact["core_concepts"][0]
    assert "terms" not in artifact
    source_markdown = Path(source_page).read_text(encoding="utf-8")
    assert "## Observations" in source_markdown
    assert "O001 (qa_episode)" in source_markdown
    assert "doc_test:B0001" in source_markdown


def test_concept_aliases_preserve_llm_aliases_without_backend_expansion() -> None:
    document = SourceDocument(
        document_id="doc_alias",
        title="Alias 문서",
        source_path="examples/alias.md",
        content_sha1="def456",
    )
    blocks = [
        SourceBlock("doc_alias", "B0001", "ref_alias_md_b0001", "3계층 아키텍처를 설명한다.", 1, 1),
    ]
    notes = [
        {
            "chunk_id": "chunk_0001",
            "semantic_summary": "alias 테스트 문서이다.",
            "key_points": [],
            "observations": [],
            "categories": [],
            "core_concepts": [
                {
                    "title": "Three-Layer Architecture",
                    "slug_hint": "three-layer-architecture",
                    "aliases": ["3계층 아키텍처", "3-layer architecture"],
                    "definition": "원시 문서, 위키, 스키마로 구성된 구조이다.",
                    "why_page_worthy": "시스템 구조를 설명하는 핵심 개념이다.",
                    "evidence_block_ids": ["B0001"],
                }
            ],
            "section_candidates": [],
            "mentions": [],
            "evidence_claims": [],
            "needs_neighbor_context": False,
            "context_problem": None,
        }
    ]

    normalized = SemanticNormalizer(document, blocks).normalize_notes(notes)
    aliases = normalized["concept_ledger"][0]["aliases"]

    assert "Three-Layer Architecture" in aliases
    assert "3-layer architecture" in aliases
    assert "3계층 아키텍처" in aliases
    assert "three-layer architecture" not in aliases
    assert "three-layer-architecture" not in aliases


def test_source_extraction_artifact_round_trips_category_display_names() -> None:
    normalized = {
        "document": {"document_id": "doc_categories", "title": "Categories", "source_path": "categories.md"},
        "semantic_notes": [],
        "concept_ledger": [],
        "section_candidates": [],
        "mentions": [],
        "categories": [{"name": " C++ "}, {"term": "C"}, {"name": "C++"}],
        "observations": [],
        "evidence_units": [],
    }

    page = SourcePageAssembler().build(normalized)
    artifact = page["source_extraction_artifact"]
    assert artifact["categories"] == ["C++", "C"]

    restored = source_page_context_normalized(
        {
            **normalized,
            "categories": [],
        },
        artifact,
    )
    restored_page = SourcePageAssembler().build(restored)

    assert [item["name"] for item in restored["categories"]] == ["C++", "C"]
    assert "- C++" in restored_page["markdown"]
    assert "- C\n" in restored_page["markdown"]
    assert restored_page["source_extraction_artifact"]["categories"] == ["C++", "C"]


def test_generated_concept_page_assembler_keeps_assemble_import_contract() -> None:
    source_blocks = [
        SourceBlock("doc_generated", "B0001", "ref_generated_md_b0001", "생성형 concept page 근거이다.", 1, 1),
    ]
    concept = {
        "slug": "generated-concept",
        "title": "Generated Concept",
        "source_document_ids": ["doc_generated"],
        "display_reference_ids": ["B0001"],
    }
    raw_page = {
        "definition": {"text": "다듬은 정의", "anchor_block_ids": ["B0001", "B9999"]},
        "key_points": [{"text": "핵심 포인트", "anchor_block_ids": ["B0001"]}],
        "evidence": [],
        "confidence": 0.9,
    }
    warnings: list[str] = []

    normalized = GeneratedConceptPageAssembler().normalize_generated_output(
        concept,
        raw_page,
        source_blocks,
        warnings,
    )
    pages = GeneratedConceptPageAssembler().build_pages([normalized])

    assert normalized["definition"]["anchor_reference_ids"] == ["B0001"]
    assert warnings == ["generated-concept.definition: unknown concept-page anchor_block_id B9999"]
    assert "다듬은 정의" in pages[0]["markdown"]
    assert "핵심 포인트" in pages[0]["markdown"]


def test_meaning_cluster_artifact_accumulates_promote_hint_without_immediate_promotion(tmp_path: Path) -> None:
    normalized = {
        "document": {
            "document_id": "doc_a",
            "source_path": "examples/wiki-schema.md",
        },
        "section_candidates": [
            {
                "term": "Schema",
                "slug": "wiki-schema",
                "context": "위키 구조, 규칙, workflow를 정의하는 구성 파일이다.",
                "anchor_reference_ids": ["B0015"],
            }
        ],
        "mentions": [],
        "evidence_units": [
            {
                "evidence_id": "ev_0001",
                "claim": "Wiki Schema는 LLM이 ingest/query 시 따르는 규칙 문서다.",
                "anchor_reference_ids": ["B0008"],
                "related_concept_slugs": ["wiki-schema"],
                "source_document_id": "doc_b",
            }
        ],
        "unresolved_related_concept_hints": [
            {
                "hint_slug": "wiki-schema",
                "decision": "promote_new_concept",
                "canonical_slug": "wiki-schema",
                "evidence_ids": ["ev_0001"],
                "reason": "여러 source에서 독립 구성 요소로 반복 언급됨",
            }
        ],
    }

    artifact = MeaningClusterArtifactAssembler().assemble(
        normalized,
        tmp_path,
        user_id="user_1",
        workspace_id="workspace_1",
    )

    active_path = tmp_path / artifact["active_path"]
    log_path = tmp_path / artifact["log_path"]
    active_markdown = active_path.read_text(encoding="utf-8")
    log_markdown = log_path.read_text(encoding="utf-8")

    assert artifact["active_path"] == "wiki/workspace_1/clusters/active.md"
    assert active_path.exists()
    assert log_path.exists()
    assert "### Evidence Claims" in active_markdown
    assert "### Observations" not in active_markdown
    assert "### Promotion" not in active_markdown
    assert "doc_a:B0015" in active_markdown
    assert "doc_b:B0008" in active_markdown
    assert "### Cluster Decisions" in log_markdown
    assert "### Promotion Decisions" in log_markdown
    assert "- promotion decision 없음" in log_markdown


def test_meaning_cluster_artifact_excludes_existing_concept_updates(tmp_path: Path) -> None:
    normalized = {
        "document": {
            "document_id": "doc_a",
            "source_path": "examples/motor.md",
        },
        "section_candidates": [
            {
                "term": "Back EMF",
                "slug": "back-emf",
                "context": "전동기 성능 평가의 핵심 지표다.",
                "anchor_reference_ids": ["B0001"],
            },
            {
                "term": "Torque Ripple",
                "slug": "torque-ripple",
                "context": "회전 토크의 주기적 변동이다.",
                "anchor_reference_ids": ["B0002"],
            },
        ],
        "mentions": [],
        "evidence_units": [],
        "unresolved_related_concept_hints": [],
    }

    artifact = MeaningClusterArtifactAssembler().assemble(
        normalized,
        tmp_path,
        user_id="user_1",
        workspace_id="workspace_1",
        concept_update_decisions=[
            {
                "candidate_id": "cand_001",
                "claim_id": "claim_doc_001",
                "decision": "same_concept",
                "concept_slug": "back-emf",
                "reason": "이미 core concept로 존재함",
            }
        ],
    )

    active_markdown = (tmp_path / artifact["active_path"]).read_text(encoding="utf-8")
    log_markdown = (tmp_path / artifact["log_path"]).read_text(encoding="utf-8")

    assert "## cluster: back-emf" not in active_markdown
    assert "## cluster: torque-ripple" in active_markdown
    assert "claim_doc_001 -> concept:back-emf" in log_markdown


def test_meaning_cluster_artifact_records_core_relation_candidates(tmp_path: Path) -> None:
    normalized = {
        "document": {
            "document_id": "doc_a",
            "source_path": "examples/motor.md",
        },
        "section_candidates": [
            {
                "term": "Rotor Magnet",
                "slug": "rotor-magnet",
                "context": "SPMSM의 회전자 자석 구성 요소다.",
                "anchor_reference_ids": ["B0001"],
            },
        ],
        "mentions": [],
        "evidence_units": [],
        "unresolved_related_concept_hints": [],
    }

    artifact = MeaningClusterArtifactAssembler().assemble(
        normalized,
        tmp_path,
        user_id="user_1",
        workspace_id="workspace_1",
        cluster_decisions=[
            {
                "candidate_id": "cand_001",
                "decision": "new_cluster",
                "target_cluster_id": "rotor-magnet",
                "representative": "Rotor Magnet",
                "reason": "새 개념 후보",
            }
        ],
        core_relation_decisions=[
            {
                "candidate_id": "cand_001",
                "decision": "relation_candidate",
                "concept_slug": "spmsm",
                "relation": "part_of",
                "reason": "회전자 자석은 SPMSM 구성 요소로 설명됨",
            }
        ],
    )

    active_markdown = (tmp_path / artifact["active_path"]).read_text(encoding="utf-8")
    log_markdown = (tmp_path / artifact["log_path"]).read_text(encoding="utf-8")

    assert "### Core Relation Candidates" in active_markdown
    assert "target: concept:spmsm" in active_markdown
    assert "relation: part_of" in active_markdown
    assert "### Relation Candidates" in log_markdown
    assert "cluster:rotor-magnet -> concept:spmsm" in log_markdown


def test_meaning_cluster_artifact_excludes_refless_candidates_from_active(tmp_path: Path) -> None:
    normalized = {
        "document": {
            "document_id": "doc_a",
            "source_path": "examples/motor.md",
        },
        "section_candidates": [
            {
                "term": "Torque Ripple Ratio",
                "slug": "torque-ripple-ratio",
                "context": "평균 토크 대비 토크 리플의 비율이다.",
                "anchor_reference_ids": [],
            },
            {
                "term": "Back EMF",
                "slug": "back-emf",
                "context": "전동기 성능 평가 지표다.",
                "anchor_reference_ids": ["B0001"],
            },
        ],
        "mentions": [],
        "evidence_units": [],
        "unresolved_related_concept_hints": [],
    }

    artifact = MeaningClusterArtifactAssembler().assemble(
        normalized,
        tmp_path,
        user_id="user_1",
        workspace_id="workspace_1",
    )

    active_markdown = (tmp_path / artifact["active_path"]).read_text(encoding="utf-8")
    log_markdown = (tmp_path / artifact["log_path"]).read_text(encoding="utf-8")

    assert "## cluster: torque-ripple-ratio" not in active_markdown
    assert "## cluster: back-emf" in active_markdown
    assert artifact["maintenance_summary"]["invalid_candidate_count"] == 1
    assert artifact["maintenance_summary"]["invalid_candidates"][0]["slug"] == "torque-ripple-ratio"
    assert "### Invalid Candidates" in log_markdown
    assert "missing source refs" in log_markdown


def test_link_builder_does_not_materialize_weak_concept_edges() -> None:
    normalized = {
        "document": {"document_id": "doc_a"},
        "concept_ledger": [
            {"slug": "back-emf"},
            {"slug": "cogging-torque"},
        ],
        "evidence_units": [
            {
                "evidence_id": "ev_0001",
                "related_concept_slugs": ["back-emf", "cogging-torque"],
            }
        ],
        "concept_resolutions": [
            {
                "canonical_slug": "back-emf",
                "link_targets": ["cogging-torque"],
                "confidence": 0.8,
            }
        ],
    }

    links = LinkBuilder().build(normalized)

    assert {link["relation"] for link in links} == {"source_mentions_concept"}


def test_link_builder_links_concepts_judged_link_to_including_existing_ones() -> None:
    from app.modules.wiki_generation.infrastructure.concept_page_sections import concept_related_lines

    normalized = {
        "document": {"document_id": "doc_b"},
        "concept_ledger": [{"slug": "rolling-update", "title": "롤링 업데이트"}],
        "existing_concept_index": [{"slug": "deployment", "title": "Deployment"}],
        "concept_resolutions": [
            {"incoming_slug": "rolling-update", "decision": "link_to", "canonical_slug": "rolling-update",
             "link_targets": ["deployment"], "confidence": 0.0},
        ],
    }

    links = LinkBuilder().build(normalized)

    concept_links = [link for link in links if link["relation"] == "concept_related_to"]
    assert [(link["source"], link["target"]) for link in concept_links] == [("concept:rolling-update", "concept:deployment")]
    # 다른 문서의 기존 개념도 본문 Related Concepts에 제목과 함께 적는다.
    assert concept_related_lines("rolling-update", normalized, {"rolling-update": normalized["concept_ledger"][0]}) == [
        "- [[deployment|Deployment]]"
    ]


def test_link_to_resolution_keeps_the_concept_and_links_it_through_the_ingest_steps() -> None:
    from app.modules.wiki_generation.infrastructure.concept_resolution import (
        apply_concept_resolutions,
        normalize_resolution_output,
    )

    normalized = {
        "document": {"document_id": "doc_b"},
        "concept_ledger": [{"slug": "rolling-update", "title": "롤링 업데이트", "aliases": []}],
        "evidence_units": [],
    }
    existing = [{"slug": "deployment", "title": "Deployment", "definition": "파드 업데이트를 선언적으로 관리한다."}]
    raw = {"resolutions": [{"incoming_slug": "rolling-update", "decision": "link_to", "canonical_slug": "deployment"}]}

    resolutions = normalize_resolution_output(raw, normalized["concept_ledger"], existing, [])
    resolved = apply_concept_resolutions(normalized, resolutions, existing)
    links = LinkBuilder().build(resolved)

    assert [c["slug"] for c in resolved["concept_ledger"]] == ["rolling-update"]
    assert ("concept:rolling-update", "concept:deployment") in {
        (link["source"], link["target"]) for link in links if link["relation"] == "concept_related_to"
    }


def test_link_confidences_of_validated_targets_reach_concept_links() -> None:
    from app.modules.wiki_generation.infrastructure.concept_resolution import (
        apply_concept_resolutions,
        normalize_resolution_output,
    )

    normalized = {
        "document": {"document_id": "doc_b"},
        "concept_ledger": [{"slug": "rolling-update", "title": "롤링 업데이트", "aliases": []}],
        "evidence_units": [],
    }
    existing = [
        {"slug": "deployment", "title": "Deployment", "definition": "파드 업데이트를 선언적으로 관리한다."},
        {"slug": "replica-set", "title": "ReplicaSet", "definition": "파드 복제본 수를 유지한다."},
    ]
    raw = {"resolutions": [{
        "incoming_slug": "rolling-update",
        "decision": "link_to",
        "canonical_slug": "deployment",
        "link_targets": ["deployment", "replica-set", "missing"],
        "link_confidences": {"deployment": 0.8, "replica-set": 0.6, "missing": 0.9},
    }]}

    resolutions = normalize_resolution_output(raw, normalized["concept_ledger"], existing, [])
    links = LinkBuilder().build(apply_concept_resolutions(normalized, resolutions, existing))

    assert resolutions[0]["link_confidences"] == {"deployment": 0.8, "replica-set": 0.6}
    assert {
        link["target"]: link["confidence"] for link in links if link["relation"] == "concept_related_to"
    } == {"concept:deployment": 0.8, "concept:replica-set": 0.6}


def test_merge_resolution_with_null_link_targets_is_normalized() -> None:
    from app.modules.wiki_generation.infrastructure.concept_resolution import normalize_resolution_output

    existing = [{"slug": "deployment", "title": "Deployment", "definition": "파드 업데이트를 선언적으로 관리한다."}]
    raw = {"resolutions": [{
        "incoming_slug": "deploy",
        "decision": "merge_into",
        "canonical_slug": "deployment",
        "link_targets": None,
    }]}

    resolutions = normalize_resolution_output(raw, [{"slug": "deploy", "title": "Deploy"}], existing, [])

    assert resolutions[0]["decision"] == "merge_into"
    assert resolutions[0]["canonical_slug"] == "deployment"
    assert resolutions[0]["link_targets"] == []


def test_link_to_a_concept_merged_in_the_same_ingest_links_the_merged_concept() -> None:
    from app.modules.wiki_generation.infrastructure.concept_resolution import (
        apply_concept_resolutions,
        normalize_resolution_output,
    )

    normalized = {
        "document": {"document_id": "doc_b"},
        "concept_ledger": [
            {"slug": "weighted-mean", "title": "가중 평균", "aliases": []},
            {"slug": "average", "title": "평균", "aliases": []},
        ],
        "evidence_units": [],
    }
    existing = [{"slug": "mean", "title": "Mean", "definition": "합을 개수로 나눈 값이다."}]
    raw = {
        "resolutions": [
            {"incoming_slug": "weighted-mean", "decision": "link_to", "canonical_slug": "average"},
            {"incoming_slug": "average", "decision": "merge_into", "canonical_slug": "mean"},
        ]
    }

    from app.modules.wiki_generation.infrastructure.concept_page_sections import concept_related_lines

    resolutions = normalize_resolution_output(raw, normalized["concept_ledger"], existing, [])
    resolved = apply_concept_resolutions(normalized, resolutions, existing)
    resolved["existing_concept_index"] = existing
    links = LinkBuilder().build(resolved)

    assert [(link["source"], link["target"]) for link in links if link["relation"] == "concept_related_to"] == [
        ("concept:weighted-mean", "concept:mean")
    ]
    # 본문 Related Concepts도 병합된 개념을 적는다.
    ledger_by_slug = {c["slug"]: c for c in resolved["concept_ledger"]}
    assert concept_related_lines("weighted-mean", resolved, ledger_by_slug) == ["- [[mean|Mean]]"]
