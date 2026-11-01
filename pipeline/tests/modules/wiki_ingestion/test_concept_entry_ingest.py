"""편입 배정과 승격(ADR 0042 2단계): 이름 규칙·part_of 배정, K 승격, 같은 하위 항목 찾기, 기여·복구·임베딩 단위."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from app.core.jev_client import JevUnavailable
from app.modules.wiki_generation.infrastructure.assemble import ConceptPageAssembler, LinkBuilder
from app.modules.wiki_ingestion.application.concept_contribution import build_concept_contributions
from app.modules.wiki_ingestion.application.concept_entry_ingest import apply_concept_entries
from app.modules.wiki_ingestion.domain.operation_recovery import PageContribution, plan_page_recovery
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_output_persistence as persistence
from app.modules.wiki_ingestion.infrastructure.concept_contribution_rebuild import rebuild_concept_page
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_writer import persist_embedding_units

DSN = os.environ.get("TEST_AGENT_DATABASE_URL") or os.environ.get("CONCEPT_LOCK_TEST_DSN")
AI_SCHEMA_SQL = (Path(__file__).resolve().parents[3] / "db" / "ai_schema.sql").read_text(encoding="utf-8")


def _concept(slug: str, title: str, block: str, aliases: list[str] | None = None) -> dict:
    return {"slug": slug, "title": title, "aliases": aliases or [title], "definition": f"{title} 정의",
            "anchor_reference_ids": [block], "evidence_claim_ids": [f"ev-{slug}"],
            "source_document_ids": ["doc-1"], "importance_score": 1.0}


def _normalized(concepts: list[dict], existing: list[dict] | None = None) -> dict:
    return {
        "document": {"document_id": "doc-1", "title": "문서"},
        "concept_ledger": concepts,
        "existing_concept_index": existing or [],
        "evidence_units": [
            {"evidence_id": concept["evidence_claim_ids"][0], "claim": f"{concept['title']} 근거",
             "anchor_reference_ids": concept["anchor_reference_ids"], "related_concept_slugs": [concept["slug"]],
             "source_document_id": "doc-1"}
            for concept in concepts
        ],
        "concept_slug_map": {concept["slug"]: concept["slug"] for concept in concepts},
        "concept_update_decisions": [],
    }


DEPLOYMENT = [
    _concept("deployment", "Deployment", "B0001"),
    _concept("deployment-rollout", "Deployment Rollout", "B0002"),
    _concept("deployment-rollback", "Deployment Rollback", "B0003"),
    _concept("label-selector-deployment", "Label Selector (Deployment)", "B0004"),
]


def _entries(result: dict, page: str) -> dict[str, dict]:
    return {change["name"]: change for change in result["entry_changes"][page]}


def test_name_rule_assigns_details_under_head_that_reaches_k() -> None:
    result = apply_concept_entries(_normalized(DEPLOYMENT))

    assert [concept["slug"] for concept in result["concept_ledger"]] == ["deployment"]
    entries = _entries(result, "deployment")
    assert set(entries) == {"Deployment Rollout", "Deployment Rollback", "Label Selector (Deployment)"}
    rollout = entries["Deployment Rollout"]
    assert (rollout["parent_entry_id"], rollout["anchor_reference_ids"], rollout["evidence_claim_ids"]) == (
        None, ["B0002"], ["ev-deployment-rollout"])
    # 세부 항목 근거는 집 페이지 개념의 근거가 되고, 근거·연결은 집 페이지를 가리킨다.
    head = result["concept_ledger"][0]
    assert head["evidence_claim_ids"] == ["ev-deployment", "ev-deployment-rollout", "ev-deployment-rollback",
                                         "ev-label-selector-deployment"]
    assert {slug for item in result["evidence_units"] for slug in item["related_concept_slugs"]} == {"deployment"}
    assert result["concept_slug_map"]["deployment-rollout"] == "deployment"
    assert result["entry_references"] == []


def test_head_below_k_keeps_todays_pages() -> None:
    normalized = _normalized(DEPLOYMENT[:3])

    assert apply_concept_entries(normalized) is normalized


def test_existing_page_is_a_head_without_k_and_joins_the_ledger() -> None:
    existing = [{"slug": "deployment", "title": "Deployment", "aliases": [], "definition": "기존 정의", "entries": []}]

    result = apply_concept_entries(_normalized([DEPLOYMENT[1]], existing))

    assert [(item["slug"], item["title"], item["definition"]) for item in result["concept_ledger"]] == [
        ("deployment", "Deployment", "기존 정의")]
    assert list(_entries(result, "deployment")) == ["Deployment Rollout"]


def test_nested_detail_keeps_its_parent_entry() -> None:
    concepts = [*DEPLOYMENT, _concept("deployment-rollout-history", "Deployment Rollout History", "B0005")]

    entries = _entries(apply_concept_entries(_normalized(concepts)), "deployment")

    assert entries["Deployment Rollout History"]["parent_entry_id"] == entries["Deployment Rollout"]["entry_id"]
    assert entries["Deployment Rollout"]["parent_entry_id"] is None


class FakePartOf:
    def __init__(self, answers: dict[tuple[str, str], float]) -> None:
        self.answers = answers
        self.pairs: list[tuple[str, str]] = []

    def __call__(self, pairs):
        self.pairs = [(head["slug"], detail["slug"]) for head, detail in pairs]
        return {pair: {"part_of": probability >= 0.9, "probability": probability}
                for pair, probability in self.answers.items() if pair in self.pairs}


POD = [
    _concept("pod", "Pod", "B0001"),
    _concept("liveness-probe", "Liveness Probe", "B0002"),
    _concept("readiness-probe", "Readiness Probe", "B0003"),
    _concept("restart-policy", "Restart Policy", "B0004"),
    _concept("container-image", "Container Image", "B0005"),
]


def test_part_of_judge_assigns_only_confident_one_way_answers() -> None:
    judge = FakePartOf({
        ("pod", "liveness-probe"): 0.97, ("pod", "readiness-probe"): 0.95, ("pod", "restart-policy"): 0.93,
        # 애매하면(0.9 미만) 배정하지 않는다.
        ("pod", "container-image"): 0.6,
        # 서로가 서로에 딸린다는 판정은 버린다.
        ("liveness-probe", "readiness-probe"): 0.99, ("readiness-probe", "liveness-probe"): 0.99,
    })

    result = apply_concept_entries(_normalized(POD), judge_part_of=judge)

    assert ("pod", "liveness-probe") in judge.pairs
    assert sorted(concept["slug"] for concept in result["concept_ledger"]) == ["container-image", "pod"]
    entries = _entries(result, "pod")
    assert set(entries) == {"Liveness Probe", "Readiness Probe", "Restart Policy"}
    assert all(entry["parent_entry_id"] is None for entry in entries.values())


def test_unavailable_part_of_judge_leaves_name_rule_only() -> None:
    def unavailable(_pairs):
        raise JevUnavailable("down")

    normalized = _normalized(POD)

    assert apply_concept_entries(normalized, judge_part_of=unavailable) is normalized


def _with_kubectl_entry(definition: str = "Pod 수를 바꾼다") -> list[dict]:
    return [
        {"slug": "kubectl", "title": "kubectl", "aliases": [], "definition": "CLI",
         "entries": [{"entry_id": "e-scale", "name": "scale", "definition": definition, "parent_entry_id": None}]},
    ]


class FakeSame:
    def __init__(self, decision: str | None) -> None:
        self.decision = decision
        self.calls: list[tuple[list[str], list[str]]] = []

    def __call__(self, concepts, pool):
        self.calls.append(([concept["title"] for concept in concepts], [item["title"] for item in pool]))
        if self.decision is None:
            return {}
        return {concept["slug"]: {"incoming_slug": concept["slug"], "decision": self.decision, "canonical_slug": "e-scale"}
                for concept in concepts if concept["title"] == "Deployment > Deployment Scale"}


def test_same_entry_on_another_page_adds_evidence_there_and_references_it() -> None:
    concepts = [*DEPLOYMENT[:3], _concept("deployment-scale", "Deployment Scale", "B0009")]
    judge = FakeSame("merge_into")

    result = apply_concept_entries(_normalized(concepts, _with_kubectl_entry()), judge_same=judge)

    assert judge.calls[0][1] == ["kubectl > scale"]
    assert "Deployment Scale" not in _entries(result, "deployment")
    # 기존 항목의 이름·정의·위치를 따르고, 근거는 집 페이지에 더한다.
    assert result["entry_changes"]["kubectl"] == [{
        "op": "add", "entry_id": "e-scale", "name": "scale", "definition": "Pod 수를 바꾼다", "parent_entry_id": None,
        "anchor_reference_ids": ["B0009"], "evidence_claim_ids": ["ev-deployment-scale"]}]
    kubectl = next(concept for concept in result["concept_ledger"] if concept["slug"] == "kubectl")
    assert kubectl["evidence_claim_ids"] == ["ev-deployment-scale"]
    assert result["entry_references"] == [{
        "source": "concept:deployment", "target": "concept:kubectl", "relation": "entry_reference",
        "decided_by": "concept_entry", "entry_id": "e-scale", "name": "scale", "parent_entry_id": None}]


@pytest.mark.parametrize("decision", ["link_to", None])
def test_unsure_same_entry_stays_a_separate_new_entry(decision) -> None:
    concepts = [*DEPLOYMENT[:3], _concept("deployment-scale", "Deployment Scale", "B0009")]

    result = apply_concept_entries(_normalized(concepts, _with_kubectl_entry()), judge_same=FakeSame(decision))

    assert _entries(result, "deployment")["Deployment Scale"]["entry_id"].startswith("entry:")
    assert "kubectl" not in result["entry_changes"]
    assert result["entry_references"] == []


def test_reingest_matches_its_own_entry_by_name_without_judge() -> None:
    first = _entries(apply_concept_entries(_normalized(DEPLOYMENT)), "deployment")
    existing = [{"slug": "deployment", "title": "Deployment", "aliases": [], "definition": "정의", "entries": [
        {"entry_id": entry["entry_id"], "name": entry["name"], "definition": entry["definition"], "parent_entry_id": None}
        for entry in first.values()]}]

    second = _entries(apply_concept_entries(_normalized(DEPLOYMENT, existing)), "deployment")

    assert {name: entry["entry_id"] for name, entry in second.items()} == {
        name: entry["entry_id"] for name, entry in first.items()}


def _ingest_contributions(operation_id: str, result: dict) -> dict[str, dict]:
    return build_concept_contributions(
        operation_id=operation_id,
        normalized=result,
        source_blocks=[{"block_id": f"B000{index}", "text": f"블록 {index}"} for index in range(1, 10)],
        links=LinkBuilder().build(result),
    )


def _page(page_id: str, operation_id: str, sequence: int, contribution: dict) -> PageContribution:
    return PageContribution(page_id=page_id, page_type="concept", operation_id=operation_id, sequence=sequence,
                            markdown_key=f"wiki/ws/pages/{page_id}/ops/{operation_id}.md",
                            contribution={**contribution, "page_id": page_id})


def test_ingest_contribution_replays_entries_and_restore_drops_them() -> None:
    concepts = [*DEPLOYMENT, _concept("deployment-scale", "Deployment Scale", "B0009")]
    contributions = _ingest_contributions(
        "op-x", apply_concept_entries(_normalized(concepts, _with_kubectl_entry()), judge_same=FakeSame("merge_into")))
    kubectl_base = _ingest_contributions("op-w", _normalized([_concept("kubectl", "kubectl", "B0008")]))["kubectl"]
    deployment = [_page("page-deploy", "op-x", 1, contributions["deployment"])]
    kubectl = [_page("page-kubectl", "op-w", 1, kubectl_base), _page("page-kubectl", "op-x", 2, contributions["kubectl"])]

    rebuilt = rebuild_concept_page(deployment)
    assert {entry["name"]: entry["anchor_reference_ids"] for entry in rebuilt.entries} == {
        "Deployment Rollout": ["doc-1:B0002"], "Deployment Rollback": ["doc-1:B0003"],
        "Label Selector (Deployment)": ["doc-1:B0004"]}
    assert [link["entry_id"] for link in rebuilt.supported_links if link["relation"] == "entry_reference"] == ["e-scale"]
    assert "- [[kubectl|scale]]" in rebuilt.markdown and "- Deployment Rollout" in rebuilt.markdown
    assert [entry["evidence_claim_ids"] for entry in rebuild_concept_page(kubectl).entries] == [["op-x:ev-deployment-scale"]]

    # 편입 X를 되돌리면 X가 둔 항목·참조·근거가 함께 빠진다.
    assert plan_page_recovery(deployment, {"op-x"}).action == "delete"
    restored_kubectl = rebuild_concept_page(plan_page_recovery(kubectl, {"op-x"}).kept_contributions)
    assert restored_kubectl.entries == ()
    assert "Deployment Scale 근거" not in restored_kubectl.markdown


class EntryConnection:
    def __init__(self, entries: list[dict]) -> None:
        self.entries = entries
        self.units: list[tuple] = []

    def execute(self, query: str, params: tuple):
        rows: list[dict] = []
        if "FROM wiki_page_entries entry" in query:
            rows = self.entries
        elif "INSERT INTO wiki_embedding_vectors" in query:
            rows = [{"id": params[0]}]
        elif "INSERT INTO wiki_embedding_units" in query:
            self.units.append(params)
        return type("Cursor", (), {"fetchall": lambda _self: rows, "fetchone": lambda _self: rows[0]})()


def test_embedding_units_have_sub_entry_units_and_entry_ids_on_their_evidence() -> None:
    conn = EntryConnection([
        {"entry_id": "e-rollout", "name": "Rollout", "definition": "새 버전으로 바꾼다", "block_refs": ["doc-1:B0002"],
         "parent_name": "Deployment"},
    ])

    persist_embedding_units(conn, "page-deploy", "doc-1", (
        "## Evidence\n- 디플로이먼트 근거 [doc-1:B0001]\n- 롤아웃 근거 [doc-1:B0002]\n- 둘 다 [doc-1:B0001, doc-1:B0002]\n"))

    rows = {(row[4], row[6]): row[8] for row in conn.units}
    assert rows == {
        ("source_block", "디플로이먼트 근거"): None,
        ("source_block", "롤아웃 근거"): "e-rollout",
        ("source_block", "둘 다"): None,
        ("sub_entry", "Deployment > Rollout: 새 버전으로 바꾼다"): "e-rollout",
    }


@pytest.fixture
def scoped_dsn():
    if not DSN:
        pytest.skip("격리 PostgreSQL 테스트 URL이 지정되지 않았습니다.")
    name = "test_entry_ingest_" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    separator = "&" if "?" in DSN else "?"
    scoped = f"{DSN}{separator}options={quote(f'-csearch_path={name}')}"
    with psycopg.connect(scoped) as conn:
        conn.execute(AI_SCHEMA_SQL)
    try:
        yield scoped
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def _stored_markdown(monkeypatch) -> dict[str, str]:
    stored: dict[str, str] = {}

    def write(markdown: str, key: str) -> str:
        stored[key] = markdown
        return key

    monkeypatch.setattr(persistence, "upload_wiki_markdown", write)
    monkeypatch.setattr(persistence, "read_optional_text_object", lambda key: stored.get(key, ""))
    monkeypatch.setattr(persistence, "write_text_object", lambda key, text, *_args: write(text, key))
    return stored


def manifest(operation_id: str, normalized: dict, existing: list[dict] | None = None, judge_same=None,
             entry_changes: dict | None = None) -> dict:
    result = apply_concept_entries({**normalized, "existing_concept_index": existing or []}, judge_same=judge_same)
    if entry_changes:
        result = {**result, "entry_changes": entry_changes}
    links = LinkBuilder().build(result)
    return {
        "operation_id": operation_id, "workspace_id": "ws-1", "user_id": "user-1", "normalized": result,
        "source_page": {"markdown": "# 문서\n"}, "source_blocks": [],
        "concept_pages": [{"slug": concept["slug"], "markdown": f"# {concept['title']}\n\n## Evidence\n"
                           + "".join(f"- {item['claim']} [doc-1:{item['anchor_reference_ids'][0]}]\n"
                                     for item in result["evidence_units"] if concept["slug"] in item["related_concept_slugs"])
                           + "\n## Related Concepts\n- 관련 개념 없음\n"}
                          for concept in result["concept_ledger"]],
        "concept_contributions": _ingest_contributions(operation_id, result), "links": links,
    }


def _section(markdown: str) -> str:
    start = markdown.index("## Sub-entries")
    end = markdown.find("\n## ", start + 1)
    return markdown[start:end if end >= 0 else None].strip()


def test_ingest_persists_entries_references_and_entry_units(scoped_dsn, monkeypatch) -> None:
    stored = _stored_markdown(monkeypatch)

    with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
        persistence.persist_wiki_outputs(conn, "doc-0", manifest("op-w", _normalized([_concept("kubectl", "kubectl", "B0008")])))
        conn.execute("INSERT INTO wiki_page_entries (workspace_id, entry_id, page_id, name, definition) "
                     "SELECT 'ws-1', 'e-scale', id, 'scale', 'Pod 수를 바꾼다' FROM wiki_pages WHERE slug = 'kubectl'")
    concepts = [*DEPLOYMENT, _concept("deployment-scale", "Deployment Scale", "B0009")]
    with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
        persistence.persist_wiki_outputs(conn, "doc-1", manifest(
            "op-x", _normalized(concepts), _with_kubectl_entry(), judge_same=FakeSame("merge_into")))
        entries = conn.execute(
            "SELECT p.slug, e.name, e.parent_entry_id, e.block_refs FROM wiki_page_entries e "
            "JOIN wiki_pages p ON p.id = e.page_id ORDER BY p.slug, e.name").fetchall()
        units = conn.execute(
            "SELECT p.slug, u.unit_type, u.text, u.entry_id FROM wiki_embedding_units u "
            "JOIN wiki_pages p ON p.id = u.page_id WHERE u.entry_id IS NOT NULL ORDER BY u.unit_type, u.text").fetchall()
        references = conn.execute(
            "SELECT f.slug AS source, t.slug AS target FROM wiki_page_links l JOIN wiki_pages f ON f.id = l.from_page_id "
            "JOIN wiki_pages t ON t.id = l.to_page_id WHERE l.link_type = 'entry_reference'").fetchall()
        markdown_uri = conn.execute("SELECT markdown_uri FROM wiki_pages WHERE slug = 'deployment'").fetchone()["markdown_uri"]

    assert [(row["slug"], row["name"], row["block_refs"]) for row in entries] == [
        ("deployment", "Deployment Rollback", ["doc-1:B0003"]),
        ("deployment", "Deployment Rollout", ["doc-1:B0002"]),
        ("deployment", "Label Selector (Deployment)", ["doc-1:B0004"]),
        ("kubectl", "scale", ["doc-1:B0009"]),
    ]
    assert references == [{"source": "deployment", "target": "kubectl"}]
    assert ("deployment", "sub_entry", "Deployment > Deployment Rollout: Deployment Rollout 정의") in {
        (row["slug"], row["unit_type"], row["text"]) for row in units}
    # 같은 항목으로 본 근거는 집 페이지(kubectl) 근거로 붙고 그 항목 id를 가진다.
    assert [(row["unit_type"], row["entry_id"]) for row in units if row["slug"] == "kubectl"] == [
        ("source_block", "e-scale"), ("sub_entry", "e-scale")]
    assert "Deployment Scale 근거" in next(row["text"] for row in units if row["slug"] == "kubectl")
    # 참조는 벡터를 두지 않는다.
    assert not any(row["slug"] == "deployment" and row["entry_id"] == "e-scale" for row in units)
    markdown = stored[markdown_uri]
    assert "## Sub-entries\n- Deployment Rollout\n- Deployment Rollback\n- Label Selector (Deployment)\n- [[kubectl|scale]]\n" in markdown


NESTED = [*DEPLOYMENT[:3], _concept("deployment-scale", "Deployment Scale", "B0009"),
          _concept("deployment-scale-limit", "Deployment Scale Limit", "B0007")]


def test_child_of_parent_merged_into_another_page_lives_under_it_there() -> None:
    result = apply_concept_entries(_normalized(NESTED, _with_kubectl_entry()), judge_same=FakeSame("merge_into"))

    kubectl = {change["name"]: change for change in result["entry_changes"]["kubectl"]}
    assert set(kubectl) == {"scale", "Deployment Scale Limit"}
    assert kubectl["Deployment Scale Limit"]["parent_entry_id"] == "e-scale"
    assert "Deployment Scale Limit" not in _entries(result, "deployment")
    # 참조는 같은 항목으로 본 위 항목 하나뿐이다. 아래 항목은 그 항목의 집에서 함께 보인다.
    assert [link["entry_id"] for link in result["entry_references"]] == ["e-scale"]
    assert result["concept_slug_map"]["deployment-scale-limit"] == "kubectl"


def test_nested_merge_stored_state_equals_replay(scoped_dsn, monkeypatch) -> None:
    stored = _stored_markdown(monkeypatch)
    scale = {"op": "add", "entry_id": "e-scale", "name": "scale", "definition": "Pod 수를 바꾼다",
             "parent_entry_id": None, "anchor_reference_ids": ["B0008"], "evidence_claim_ids": []}
    first = manifest("op-w", _normalized([_concept("kubectl", "kubectl", "B0008")]), entry_changes={"kubectl": [scale]})
    second = manifest("op-x", _normalized(NESTED), _with_kubectl_entry(), judge_same=FakeSame("merge_into"))
    with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
        persistence.persist_wiki_outputs(conn, "doc-0", first)
    with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
        persistence.persist_wiki_outputs(conn, "doc-1", second)
        page = conn.execute("SELECT id, markdown_uri FROM wiki_pages WHERE slug = 'kubectl'").fetchone()
        rows = conn.execute("SELECT entry_id, name, parent_entry_id FROM wiki_page_entries WHERE page_id = %s "
                            "ORDER BY position", (page["id"],)).fetchall()
        units = conn.execute("SELECT text FROM wiki_embedding_units WHERE unit_type = 'sub_entry' AND page_id = %s "
                             "ORDER BY text", (page["id"],)).fetchall()

    replayed = rebuild_concept_page([
        _page(page["id"], "op-w", 1, first["concept_contributions"]["kubectl"]),
        _page(page["id"], "op-x", 2, second["concept_contributions"]["kubectl"]),
    ])
    assert rows == [{key: entry[key] for key in ("entry_id", "name", "parent_entry_id")} for entry in replayed.entries]
    assert _section(stored[page["markdown_uri"]]) == _section(replayed.markdown) == (
        "## Sub-entries\n- scale\n  - Deployment Scale Limit")
    # 임베딩도 실제 바로 위 항목 이름을 쓴다.
    assert [row["text"] for row in units] == [
        "kubectl > scale: Pod 수를 바꾼다", "scale > Deployment Scale Limit: Deployment Scale Limit 정의"]


def test_child_added_to_existing_entry_is_drawn_under_it(scoped_dsn, monkeypatch) -> None:
    stored = _stored_markdown(monkeypatch)
    first = manifest("op-w", _normalized(DEPLOYMENT))
    with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
        persistence.persist_wiki_outputs(conn, "doc-0", first)
    existing = [{"slug": "deployment", "title": "Deployment", "aliases": [], "definition": "정의", "entries": [
        {"entry_id": change["entry_id"], "name": change["name"], "definition": change["definition"], "parent_entry_id": None}
        for change in first["normalized"]["entry_changes"]["deployment"]]}]
    # 다시 나온 Rollout은 이름으로 같은 항목이 되고, 새 History는 그 아래 항목이다.
    second = manifest("op-x", _normalized([DEPLOYMENT[0], DEPLOYMENT[1],
                                           _concept("deployment-rollout-history", "Deployment Rollout History", "B0005")]),
                      existing)
    with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
        persistence.persist_wiki_outputs(conn, "doc-1", second)
        markdown_uri = conn.execute("SELECT markdown_uri FROM wiki_pages WHERE slug = 'deployment'").fetchone()["markdown_uri"]

    assert _section(stored[markdown_uri]) == (
        "## Sub-entries\n- Deployment Rollout\n  - Deployment Rollout History\n- Deployment Rollback\n"
        "- Label Selector (Deployment)")


def test_relation_from_concept_moved_into_entry_starts_from_its_home_page() -> None:
    concepts = [*DEPLOYMENT, _concept("service", "Service", "B0006")]
    normalized = _normalized(concepts)
    normalized["concept_resolutions"] = [
        {"incoming_slug": "deployment-rollout", "decision": "link_to", "canonical_slug": "deployment-rollout",
         "link_targets": ["service", "deployment"], "confidence": 0.95},
    ]

    result = apply_concept_entries(normalized)
    links = [link for link in LinkBuilder().build(result) if link["relation"] == "concept_related_to"]
    contributions = _ingest_contributions("op-x", result)
    replayed = rebuild_concept_page([_page("page-deploy", "op-x", 1, contributions["deployment"])])

    # 집 페이지로 옮긴 연결만 남고, 집 페이지 자신을 가리키는 연결은 버린다.
    assert [(link["source"], link["target"]) for link in links] == [("concept:deployment", "concept:service")]
    assert [(link["source"], link["target"]) for link in replayed.supported_links
            if link["relation"] == "concept_related_to"] == [("concept:deployment", "concept:service")]
    deployment = next(page for page in ConceptPageAssembler().build_top(result, top_n=None)
                      if page["slug"] == "deployment")
    assert "[[service|Service]]" in deployment["markdown"]


def test_child_of_merged_parent_is_searched_under_the_matched_entry_name() -> None:
    existing = _with_kubectl_entry()
    existing[0]["entries"].append(
        {"entry_id": "e-limit", "name": "Deployment Scale Limit", "definition": "상한", "parent_entry_id": "e-scale"})

    result = apply_concept_entries(_normalized(NESTED, existing), judge_same=FakeSame("merge_into"))

    # 위 항목이 kubectl > scale과 같으면 아래 항목은 "scale > ..."로 찾아 기존 항목 id를 다시 쓴다.
    kubectl = {change["name"]: change for change in result["entry_changes"]["kubectl"]}
    assert kubectl["Deployment Scale Limit"]["entry_id"] == "e-limit"
    assert kubectl["Deployment Scale Limit"]["parent_entry_id"] == "e-scale"


def test_manually_deleted_reference_stays_deleted_after_next_ingest(scoped_dsn, monkeypatch) -> None:
    from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database

    stored = _stored_markdown(monkeypatch)
    monkeypatch.setattr(database, "storage_uri", lambda key: key)
    monkeypatch.setattr(database, "read_text_object", lambda key: stored[key])
    scale = {"op": "add", "entry_id": "e-scale", "name": "scale", "definition": "Pod 수를 바꾼다",
             "parent_entry_id": None, "anchor_reference_ids": ["B0008"], "evidence_claim_ids": []}
    first = manifest("op-w", _normalized([_concept("kubectl", "kubectl", "B0008")]), entry_changes={"kubectl": [scale]})
    concepts = [*DEPLOYMENT[:3], _concept("deployment-scale", "Deployment Scale", "B0009")]
    second = manifest("op-x", _normalized(concepts), _with_kubectl_entry(), judge_same=FakeSame("merge_into"))
    with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
        persistence.persist_wiki_outputs(conn, "doc-0", first)
    with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
        persistence.persist_wiki_outputs(conn, "doc-1", second)
        page = conn.execute("SELECT id, markdown_uri FROM wiki_pages WHERE slug = 'deployment'").fetchone()

    # 사람이 본문에서 참조 줄을 지운다. 수동 저장은 지운 [[kubectl]]의 간선 삭제를 이 반영 함수로 적용한다.
    manual_markdown = stored[page["markdown_uri"]].replace("- [[kubectl|scale]]\n", "")
    stored["manual.md"] = manual_markdown
    removed = [{"source": "concept:deployment", "target": "concept:kubectl", "relation": "entry_reference"}]
    with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
        database._apply_restored_wiki_state(
            "ws-1",
            [{"page_id": page["id"], "page_type": "concept", "markdown_key": "manual.md", "title": None,
              "summary": None, "source_document_ids": ["doc-1"]}],
            {"removed_links": removed, "restored_links": []},
            False,
            conn=conn,
        )
    existing = [{"slug": "deployment", "title": "Deployment", "aliases": [], "definition": "정의", "entries": []}]
    third = manifest("op-y", _normalized([DEPLOYMENT[0], _concept("deployment-strategy", "Deployment Strategy", "B0006")]),
                     existing)
    with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
        persistence.persist_wiki_outputs(conn, "doc-2", third)
        markdown_uri = conn.execute("SELECT markdown_uri FROM wiki_pages WHERE slug = 'deployment'").fetchone()["markdown_uri"]

    manual = _page(page["id"], "op-m", 2, {"schema_version": 1, "artifact_type": "manual", "operation_id": "op-m",
                                            "markdown": manual_markdown, "added_links": [], "removed_links": removed})
    replayed = rebuild_concept_page([
        _page(page["id"], "op-x", 1, second["concept_contributions"]["deployment"]),
        manual,
        _page(page["id"], "op-y", 3, third["concept_contributions"]["deployment"]),
    ])
    assert "[[kubectl|scale]]" not in _section(stored[markdown_uri])
    assert _section(stored[markdown_uri]) == _section(replayed.markdown)
