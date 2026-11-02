"""개념 페이지 하위 항목(ADR 0042 1단계): 기여 재생·참조 링크·복구·본문 목록."""
from __future__ import annotations

from unittest.mock import Mock, patch

from app.modules.wiki_ingestion.domain.concept_entries import (
    render_entry_index,
    replay_concept_entries,
)
from app.modules.wiki_ingestion.domain.operation_recovery import (
    PageContribution,
    plan_page_recovery,
)
from app.modules.wiki_ingestion.domain.contribution_identity import globalize_contribution_identity
from app.modules.wiki_ingestion.domain.orphan_link_lint import find_orphan_links, replay_supported_links
from app.modules.wiki_ingestion.infrastructure import (
    postgres_wiki_ingestion_repository as database,
)
from app.modules.wiki_ingestion.infrastructure.concept_contribution_rebuild import (
    rebuild_concept_page,
)


def _add(entry_id: str, name: str, parent: str | None = None, refs: list[str] | None = None) -> dict:
    return {"op": "add", "entry_id": entry_id, "name": name, "parent_entry_id": parent,
            "anchor_reference_ids": refs or [], "evidence_claim_ids": []}


def _contribution(
    page_id: str,
    operation_id: str,
    sequence: int,
    *,
    entry_changes: list[dict] | None = None,
    links: list[dict] | None = None,
    concept: bool = True,
) -> PageContribution:
    payload: dict = {
        "schema_version": 1,
        "operation_id": operation_id,
        "document_id": f"doc-{operation_id}",
        "page_id": page_id,
        "evidence_units": [],
        "source_blocks": [],
        "links": links or [],
        "entry_changes": entry_changes or [],
    }
    if concept:
        payload["concept"] = {
            "slug": page_id.lower(),
            "title": page_id,
            "definition": f"{page_id} 정의",
            "source_document_ids": [f"doc-{operation_id}"],
        }
    return PageContribution(
        page_id=page_id,
        page_type="concept",
        operation_id=operation_id,
        sequence=sequence,
        markdown_key=f"wiki/ws/pages/{page_id}/ops/{operation_id}.md",
        contribution=payload,
    )


def _reference(source: str, target: str, entry_id: str, name: str, parent: str | None = None) -> dict:
    return {"source": f"concept:{source}", "target": f"concept:{target}", "relation": "entry_reference",
            "entry_id": entry_id, "name": name, "parent_entry_id": parent}


def test_replay_adds_moves_and_removes_entries_by_id() -> None:
    entries = replay_concept_entries([
        {"entry_changes": [_add("e-rollout", "Rollout"), _add("e-history", "History", "e-rollout"),
                           _add("e-undo", "Undo", "e-history")]},
        # 같은 항목을 다시 추가하면 근거만 더한다. 이름·위치는 처음 기여를 따른다.
        {"entry_changes": [_add("e-rollout", "다른 이름", "e-undo", ["doc-2:B1"]),
                           {"op": "move", "entry_id": "e-undo", "parent_entry_id": None},
                           # 자기 아래로 옮기면 순환이라 무시한다.
                           {"op": "move", "entry_id": "e-rollout", "parent_entry_id": "e-history"}]},
        {"entry_changes": [{"op": "remove", "entry_id": "e-rollout"}]},
    ])

    # Rollout을 지우면 그 아래 History도 함께 빠진다. 먼저 옮긴 Undo는 남는다.
    assert entries == [{"entry_id": "e-undo", "name": "Undo", "definition": "", "parent_entry_id": None,
                        "anchor_reference_ids": [], "evidence_claim_ids": [], "slugs": [], "aliases": [], "tags": []}]


def test_rebuild_globalizes_entry_evidence_and_renders_nested_index() -> None:
    rebuilt = rebuild_concept_page([
        _contribution("Deploy", "A", 1, entry_changes=[_add("e-rollout", "Rollout", refs=["B0001"]),
                                                       _add("e-history", "History", "e-rollout")]),
        _contribution("Deploy", "B", 2, entry_changes=[_add("e-scale", "Scale")]),
    ])

    assert [(entry["entry_id"], entry["parent_entry_id"]) for entry in rebuilt.entries] == [
        ("e-rollout", None), ("e-history", "e-rollout"), ("e-scale", None)]
    assert rebuilt.entries[0]["anchor_reference_ids"] == ["doc-A:B0001"]
    assert "## Sub-entries\n- Rollout\n  - History\n- Scale\n\n## Related Concepts" in rebuilt.markdown


def test_page_without_entries_keeps_markdown_without_index() -> None:
    markdown = "# C\n\n## Evidence\n- 근거\n"

    assert render_entry_index(markdown, [], []) == markdown
    # 항목이 모두 빠지면 예전 목록도 지운다.
    assert render_entry_index("# C\n\n## Sub-entries\n- 옛 항목\n\n## Evidence\n- 근거\n", [], []) == markdown


def test_reference_to_entry_on_another_page_is_a_link_per_entry() -> None:
    rebuilt = rebuild_concept_page([
        _contribution("Kubectl", "A", 1, entry_changes=[_add("e-cmd", "Commands")], links=[
            _reference("kubectl", "deploy", "e-rollout", "Rollout", "e-cmd"),
            _reference("kubectl", "deploy", "e-scale", "Scale"),
        ]),
    ])

    # 같은 두 페이지 사이라도 항목마다 참조가 남는다. DB 간선은 페이지 단위 하나다.
    assert [link["entry_id"] for link in rebuilt.supported_links] == ["e-rollout", "e-scale"]
    assert "- Commands\n  - [[deploy|Rollout]]\n- [[deploy|Scale]]\n" in rebuilt.markdown


def test_manual_removal_without_entry_id_removes_all_references_of_the_page_pair() -> None:
    supported = replay_supported_links([
        {"artifact_type": "ingest", "links": [
            _reference("kubectl", "deploy", "e-rollout", "Rollout"),
            _reference("kubectl", "deploy", "e-scale", "Scale"),
        ]},
        # 수동 편집은 페이지 간선 기준이라 entry_id 없이 지운다.
        {"artifact_type": "manual", "removed_links": [
            {"source": "concept:kubectl", "target": "concept:deploy", "relation": "entry_reference"},
        ]},
    ])

    assert supported == []


def test_entry_evidence_from_another_operation_keeps_its_global_id() -> None:
    globalized = globalize_contribution_identity({
        "document_id": "doc-s", "operation_id": "S",
        "entry_changes": [{**_add("e-rollout", "Rollout"), "evidence_claim_ids": ["X:E1", "E2"]}],
    })

    assert globalized["entry_changes"][0]["evidence_claim_ids"] == ["X:E1", "S:E2"]


def test_restoring_entry_operation_removes_entry_and_orphans_its_reference() -> None:
    reference = _reference("kubectl", "deploy", "e-rollout", "Rollout")
    deploy = [
        _contribution("Deploy", "X", 1, entry_changes=[_add("e-rollout", "Rollout")]),
        _contribution("Deploy", "Z", 2),
    ]
    kubectl = [
        _contribution("Kubectl", "W", 1),
        _contribution("Kubectl", "X", 2, links=[reference]),
    ]

    deploy_plan = plan_page_recovery(deploy, {"X"})
    kubectl_plan = plan_page_recovery(kubectl, {"X"})
    rebuilt_deploy = rebuild_concept_page(deploy_plan.kept_contributions)
    rebuilt_kubectl = rebuild_concept_page(kubectl_plan.kept_contributions)

    assert rebuilt_deploy.entries == ()
    assert "## Sub-entries" not in rebuilt_deploy.markdown
    assert rebuilt_kubectl.supported_links == ()
    page_link = {key: reference[key] for key in ("source", "target", "relation")}
    assert find_orphan_links(
        current_links=[page_link],
        active_contribution_json=[item.contribution for item in kubectl_plan.kept_contributions],
        managed_contribution_json=[item.contribution for item in kubectl],
        deleted_page_refs=set(),
    ) == [{**page_link, "reason": "no_active_support"}]


def test_reference_to_entry_on_deleted_home_page_is_endpoint_deleted() -> None:
    # 항목을 만든 작업이 집 페이지의 유일한 기여면 집이 지워진다. 다른 작업이 둔 참조는 끝점이 사라진다.
    reference = _reference("kubectl", "deploy", "e-rollout", "Rollout")
    deploy_plan = plan_page_recovery(
        [_contribution("Deploy", "X", 1, entry_changes=[_add("e-rollout", "Rollout")])], {"X"})
    kubectl = [_contribution("Kubectl", "Y", 1, links=[reference])]
    page_link = {key: reference[key] for key in ("source", "target", "relation")}

    assert deploy_plan.action == "delete"
    assert find_orphan_links(
        current_links=[page_link],
        active_contribution_json=[item.contribution for item in kubectl],
        managed_contribution_json=[item.contribution for item in kubectl],
        deleted_page_refs={"concept:deploy"},
    ) == [{**page_link, "reason": "endpoint_deleted"}]


def test_child_page_replays_original_ingest_linked_under_its_operation_id() -> None:
    # 분리(3단계)가 원래 편입 기록 X를 자식 페이지에 같은 작업 id로 잇는 형식. 페이지 identity는 page_id다.
    linked = _contribution("Rollout", "X", 1, concept=False,
                           entry_changes=[_add("e-history", "History"), _add("e-undo", "Undo", "e-history")])
    split = _contribution("Rollout", "S", 2)
    split.contribution.update({"artifact_type": "lint", "added_links": []})

    rebuilt = rebuild_concept_page([linked, split])
    without_x = rebuild_concept_page(plan_page_recovery([linked, split], {"X"}).kept_contributions)

    assert rebuilt.title == "Rollout"
    assert [entry["entry_id"] for entry in rebuilt.entries] == ["e-history", "e-undo"]
    assert without_x.entries == ()


def test_restore_replaces_queryable_entry_copy() -> None:
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)

    def execute(query, params=None):
        cursor = Mock()
        if " ".join(str(query).split()).startswith("SELECT id, page_type, slug"):
            cursor.fetchall.return_value = [{"id": "page-deploy", "page_type": "concept", "slug": "deploy"}]
        else:
            cursor.fetchall.return_value = []
        # 항목 충돌 검사: 다른 페이지가 집인 항목이 없다.
        cursor.fetchone.return_value = None
        return cursor

    connection.execute.side_effect = execute
    changed = [{
        "page_id": "page-deploy",
        "markdown_key": "wiki/ws-1/pages/page-deploy/ops/restore-1.md",
        "entries": [{"entry_id": "e-rollout", "name": "Rollout", "definition": "롤아웃 정의", "parent_entry_id": None,
                     "anchor_reference_ids": ["doc-1:B0001"]}],
    }]

    with (
        patch.object(database, "connect", return_value=connection),
        patch.object(database, "invalidate_concept_index"),
        patch.object(database, "read_text_object", return_value="# Deploy\n"),
    ):
        database.apply_restored_wiki_state("ws-1", changed, {"removed_links": [], "restored_links": []}, True)

    calls = [(" ".join(str(call.args[0]).split()), call.args[1]) for call in connection.execute.call_args_list]
    delete_index = calls.index(("DELETE FROM wiki_page_entries WHERE page_id = %s", ("page-deploy",)))
    insert = next(index for index, (query, _) in enumerate(calls) if query.startswith("INSERT INTO wiki_page_entries"))
    assert delete_index < insert
    assert calls[insert][1] == ("ws-1", "e-rollout", "page-deploy", "Rollout", None, "롤아웃 정의", ["doc-1:B0001"],
                                [], [], [])


def test_replay_adds_the_concept_identity_of_every_contribution() -> None:
    entries = replay_concept_entries([
        {"entry_changes": [{**_add("e-a", "A"), "slug": "a", "aliases": ["에이"], "tags": ["t1"]}]},
        {"entry_changes": [{**_add("e-a", "다른 이름"), "slug": "a-2", "aliases": ["에이", "A1"], "tags": []}]},
    ])

    assert [(e["name"], e["slugs"], e["aliases"], e["tags"]) for e in entries] == [
        ("A", ["a", "a-2"], ["에이", "A1"], ["t1"])]
