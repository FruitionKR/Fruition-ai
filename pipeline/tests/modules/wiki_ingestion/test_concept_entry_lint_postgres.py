"""lint 하위 항목 재검사(ADR 0042 3단계)를 실제 PostgreSQL과 기록 재생으로 확인한다.

document-svc 기여 원장은 가짜다. lint 결과의 `ledger_contributions`를 원장에 더하고 `deleted_pages`의 기여를
비활성으로 바꾸며, 복원 계획은 지금 planner처럼 만든다(lint가 만든 페이지는 삭제, 바꾼 페이지는 그 lint 기록을 뺀
활성 기여로 재조립). 이 계약은 document-svc에 아직 없다.
"""
from __future__ import annotations

import json
import os
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Json

from app.modules.wiki_ingestion.application.models import (
    LintOperationRestoreCommand,
    RebuildPageCommand,
    RestoreContributionCommand,
    WikiMaintenanceCommand,
)
from app.modules.wiki_ingestion.application.restore_wiki_pages import RestoreWikiPagesUseCase
from app.modules.wiki_ingestion.infrastructure import (
    postgres_wiki_ingestion_repository as database,
)
from app.modules.wiki_ingestion.infrastructure import wiki_maintenance
from app.modules.wiki_ingestion.infrastructure.concept_contribution_rebuild import (
    load_concept_contributions,
    rebuild_concept_page,
)
from app.modules.wiki_ingestion.infrastructure.wiki_page_restore import ObjectStorageWikiPageRestore
from app.modules.wiki_ingestion.interfaces.http.schemas import WikiLintOut

DSN = os.environ.get("TEST_AGENT_DATABASE_URL") or os.environ.get("CONCEPT_LOCK_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="격리 PostgreSQL 테스트 URL이 지정되지 않았습니다.")

AI_SCHEMA_SQL = (Path(__file__).resolve().parents[3] / "db" / "ai_schema.sql").read_text(encoding="utf-8")
WS = "ws-entry"


@pytest.fixture
def world(monkeypatch):
    name = "test_entry_lint_" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    separator = "&" if "?" in DSN else "?"
    scoped = f"{DSN}{separator}options={quote(f'-csearch_path={name}')}"
    with psycopg.connect(scoped) as conn:
        conn.execute(AI_SCHEMA_SQL)
    try:
        yield World(scoped, monkeypatch)
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def _add(entry_id: str, name: str, parent: str | None = None, refs: list[str] | None = None) -> dict:
    return {"op": "add", "entry_id": entry_id, "name": name, "parent_entry_id": parent,
            "anchor_reference_ids": refs or [], "evidence_claim_ids": []}


def _reference(source: str, target: str, entry_id: str, name: str) -> dict:
    return {"source": f"concept:{source}", "target": f"concept:{target}", "relation": "entry_reference",
            "entry_id": entry_id, "name": name, "parent_entry_id": None}


class World:
    def __init__(self, dsn: str, monkeypatch) -> None:
        self.dsn = dsn
        self.objects: dict[str, str] = {}
        self.ledger: list[dict] = []
        self.created: dict[str, list[str]] = {}
        self.runs = 0
        monkeypatch.setenv("S3_BUCKET", "wiki")
        monkeypatch.setattr(database, "connect", self.connect)
        monkeypatch.setattr(database, "read_text_object", self.read)
        monkeypatch.setattr(database, "write_text_object", self.write)
        monkeypatch.setattr(database, "_read_optional_text_object", lambda key: self.objects.get(self._key(key or ""), ""))
        monkeypatch.setattr(database, "read_contributions", self.read_contributions)
        monkeypatch.setattr(database, "invalidate_concept_index", lambda _workspace_id: None)
        monkeypatch.setattr(database, "concept_write_lock", lambda *_args: nullcontext())
        monkeypatch.setattr(wiki_maintenance, "concept_write_lock", lambda *_args: nullcontext())
        monkeypatch.setattr(wiki_maintenance, "_snapshot_object", lambda _key: None)
        monkeypatch.setattr(wiki_maintenance, "_restore_object", lambda _key, _before: None)
        self.restore = ObjectStorageWikiPageRestore(
            self.read,
            lambda key, text, _content_type: self.write(key, text),
            database.apply_restored_wiki_state_and_cleanup,
            lambda *_args: {"written_object_keys": [], "restored_cluster_ids": [], "skipped_cluster_ids": []},
        )

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _key(key: str) -> str:
        return key.removeprefix("s3://wiki/")

    def read(self, key: str) -> str:
        return self.objects[self._key(key)]

    def write(self, key: str, text: str, _content_type: str | None = None) -> str:
        self.objects[self._key(key)] = text
        return key

    def read_contributions(self, page_ids: list[str], _workspace_id: str) -> list[dict]:
        return [dict(row) for row in self.ledger if row["page_id"] in page_ids]

    def _record(self, page_id: str, operation_id: str, document_id: str | None) -> None:
        self.ledger.append({"page_id": page_id, "ingest_operation_id": operation_id, "document_id": document_id,
                            "sequence_revision": len(self.ledger) + 1, "active": True,
                            "object_key": f"wiki/{WS}/pages/{page_id}/ops/{operation_id}.json"})

    def page_id(self, slug: str) -> str:
        with self.connect() as conn:
            return str(conn.execute("SELECT id FROM wiki_pages WHERE slug = %s", (slug,)).fetchone()["id"])

    def ingest(self, operation_id: str, document_id: str, pages: dict[str, dict]) -> None:
        """편입 한 번. 기여 JSON을 쓰고 원장에 더한 뒤 기록 재생으로 현재 상태를 만든다."""
        page_ids = []
        for slug, data in pages.items():
            with self.connect() as conn:
                page_id = database._resolve_or_create_wiki_page_id(conn, "user", WS, "concept", slug)
            evidence = [
                {"evidence_id": f"ev-{entry['entry_id']}", "claim": f"{entry['name']} 근거({document_id})",
                 "anchor_reference_ids": entry["anchor_reference_ids"], "related_concept_slugs": [slug],
                 "source_document_id": document_id}
                for entry in data.get("entry_changes", [])
            ]
            for entry, unit in zip(data.get("entry_changes", []), evidence):
                entry["evidence_claim_ids"] = [unit["evidence_id"]]
            payload = {
                "schema_version": 1, "operation_id": operation_id, "document_id": document_id, "page_id": page_id,
                "concept": {"slug": slug, "title": data.get("title", slug.title()), "definition": f"{slug} 정의",
                            "source_document_ids": [document_id]},
                "evidence_units": evidence, "source_blocks": [], "links": data.get("links", []),
                "entry_changes": data.get("entry_changes", []),
            }
            self.write(f"wiki/{WS}/pages/{page_id}/ops/{operation_id}.json", json.dumps(payload, ensure_ascii=False))
            self._record(page_id, operation_id, document_id)
            page_ids.append(page_id)
        changed = [self._rebuild(f"sync-{operation_id}", page_id) for page_id in page_ids]
        database.apply_restored_wiki_state(
            WS, changed, {"removed_links": [], "restored_links": [link for page in changed for link in page["supported_links"]]},
            True,
        )

    def _keep(self, page_id: str, without: str | None = None) -> tuple[RestoreContributionCommand, ...]:
        return tuple(
            RestoreContributionCommand(row["ingest_operation_id"], row["document_id"])
            for row in self.ledger
            if row["page_id"] == page_id and row["active"] and row["ingest_operation_id"] != without
        )

    def _rebuild(self, operation_id: str, page_id: str, without: str | None = None) -> dict:
        return self.restore.rebuild_page(operation_id, WS, RebuildPageCommand(page_id, self._keep(page_id, without)))

    def _run(self, mode: str, task_result: dict | None) -> str:
        run_id = str(uuid4())
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO pipeline_runs (id, workspace_id, input_source, output_dir, mode, status, manifest) "
                "VALUES (%s, %s, 'test', 'test', %s, %s, %s)",
                (run_id, WS, mode, "succeeded" if task_result is not None else "running",
                 Json({"task_result": task_result}) if task_result is not None else None),
            )
        return run_id

    def mark_linted(self) -> None:
        """여기까지 lint가 본 것으로 둔다."""
        self._run("lint", {"operation_id": f"lint-{uuid4().hex}"})

    def lint(self, operation_id: str) -> dict:
        run_id = self._run("lint", None)
        result = wiki_maintenance.PostgresWikiMaintenance().lint(
            WikiMaintenanceCommand(user_id="user", workspace_id=WS, provider="openai", model="gpt-5-nano",
                                   operation_id=operation_id, dry_run=False)
        )
        result = WikiLintOut.model_validate(result).model_dump(mode="json")
        with self.connect() as conn:
            conn.execute("UPDATE pipeline_runs SET status = 'succeeded', manifest = %s WHERE id = %s",
                         (Json({"task_result": result}), run_id))
        known = {row["page_id"] for row in self.ledger}
        for row in result["ledger_contributions"]:
            self._record(row["page_id"], row["operation_id"], row["document_id"])
        self.created[operation_id] = sorted(
            {row["page_id"] for row in result["ledger_contributions"] if row["operation_id"] == operation_id} - known
        )
        self._deactivate(lambda row: row["page_id"] in result["deleted_pages"])
        return result

    def _deactivate(self, predicate) -> None:
        for row in self.ledger:
            if predicate(row):
                row["active"] = False

    def restore_ingest(self, operation_id: str) -> None:
        run = f"restore-{operation_id}"
        page_ids = list(dict.fromkeys(
            row["page_id"] for row in self.ledger if row["ingest_operation_id"] == operation_id and row["active"]
        ))
        changed = [self._rebuild(run, page_id, operation_id) for page_id in page_ids if self._keep(page_id, operation_id)]
        deleted = [page_id for page_id in page_ids if not self._keep(page_id, operation_id)]
        database.apply_restored_wiki_state_and_cleanup(
            run, WS, changed,
            {"removed_links": [], "restored_links": [link for page in changed for link in page["supported_links"]]},
            True, deleted,
        )
        self._deactivate(lambda row: row["ingest_operation_id"] == operation_id)
        self._run("restore_ingest", {"changed_pages": [{"page_id": page["page_id"]} for page in changed]})

    def restore_lint(self, operation_id: str) -> dict:
        updated = sorted({
            row["page_id"] for row in self.ledger
            if row["ingest_operation_id"] == operation_id and row["page_id"] not in self.created[operation_id]
        })
        result = RestoreWikiPagesUseCase(self.restore).execute_lint(
            LintOperationRestoreCommand(
                operation_id=f"restore-{operation_id}", target_operation_id=operation_id, workspace_id=WS,
                rebuild_pages=tuple(RebuildPageCommand(page_id, self._keep(page_id, operation_id)) for page_id in updated),
                deleted_pages=tuple(self.created[operation_id]),
            )
        )
        self._deactivate(lambda row: row["ingest_operation_id"] == operation_id or row["page_id"] in self.created[operation_id])
        self._run("restore_lint", {"changed_pages": result["changed_pages"]})
        return result

    def state(self) -> dict:
        """활성 페이지·본문·링크·항목·문서 연결. 페이지는 slug로 적어 id가 달라도 비교한다."""
        with self.connect() as conn:
            pages = conn.execute(
                "SELECT id, slug, title, summary, markdown_uri FROM wiki_pages WHERE status = 'active' ORDER BY slug"
            ).fetchall()
            slug = {row["id"]: row["slug"] for row in conn.execute("SELECT id, slug FROM wiki_pages").fetchall()}
            links = conn.execute("SELECT from_page_id, to_page_id, link_type FROM wiki_page_links").fetchall()
            entries = conn.execute("SELECT entry_id, page_id, name, parent_entry_id FROM wiki_page_entries").fetchall()
            documents = conn.execute(
                "SELECT document_id, wiki_page_id FROM document_wiki_links WHERE relation_type = 'extracted_concept'"
            ).fetchall()
        return {
            "pages": {row["slug"]: (row["title"], row["summary"], self.read(row["markdown_uri"])) for row in pages},
            "links": sorted((slug[row["from_page_id"]], slug[row["to_page_id"]], row["link_type"]) for row in links),
            "entries": sorted((row["entry_id"], slug[row["page_id"]], row["name"], row["parent_entry_id"]) for row in entries),
            "documents": sorted((row["document_id"], slug[row["wiki_page_id"]]) for row in documents),
        }

    def ledger_rebuild(self, slug: str):
        page_id = self.page_id(slug)
        rows = [row for row in self.ledger if row["page_id"] == page_id and row["active"]]
        return rebuild_concept_page(load_concept_contributions(
            workspace_id=WS, page_id=page_id,
            keep_contributions=[{"operation_id": row["ingest_operation_id"], "sequence": index}
                                for index, row in enumerate(rows, start=1)],
            read_text=self.read,
        ))

    def contribution(self, slug: str, operation_id: str) -> dict:
        return json.loads(self.read(f"wiki/{WS}/pages/{self.page_id(slug)}/ops/{operation_id}.json"))


ROLLOUT_STEPS = ["history", "undo", "pause", "resume", "status"]


def _deployment_with_thick_rollout(world: World) -> None:
    """Rollout 아래 세부 항목이 5개라 분리 기준(C=5)을 넘는다. kubectl은 그중 하나와 사라진 항목을 참조한다."""
    world.ingest("op-x", "doc-x", {
        "deployment": {"title": "Deployment", "entry_changes": [
            _add("e-rollout", "Rollout", refs=["B1"]),
            *(_add(f"e-{step}", step.title(), "e-rollout", [f"B{index + 2}"]) for index, step in enumerate(ROLLOUT_STEPS)),
            _add("e-scale", "Scale", refs=["B9"]),
        ]},
        "kubectl": {"title": "kubectl", "entry_changes": [_add("e-cmd", "Commands", refs=["B10"])], "links": [
            _reference("kubectl", "deployment", "e-history", "History"),
            _reference("kubectl", "deployment", "e-gone", "Gone"),
        ]},
    })


def test_split_writes_both_pages_and_lint_restore_returns_to_pre_split_state(world: World) -> None:
    _deployment_with_thick_rollout(world)
    before = world.state()

    result = world.lint("lint-split")

    assert [(item["action"], item["entry_id"]) for item in result["entry_decisions"]] == [("split", "e-rollout")]
    moved = ["e-rollout", *(f"e-{step}" for step in ROLLOUT_STEPS)]
    parent = world.contribution("deployment", "lint-split")
    assert parent["artifact_type"] == "lint"
    assert parent["entry_changes"] == [{"op": "remove", "entry_id": entry_id} for entry_id in moved]
    assert parent["added_links"] == [{"source": "concept:deployment", "target": "concept:rollout",
                                      "relation": "entry_reference", "entry_id": "e-rollout", "name": "Rollout",
                                      "parent_entry_id": None}]
    child = world.contribution("rollout", "lint-split")
    assert child["content_action"] == "create"
    assert child["concept"]["title"] == "Rollout"
    assert child["split_from"] == {"page_id": world.page_id("deployment"), "slug": "deployment", "entry_id": "e-rollout"}
    assert child["linked_operation_ids"] == ["op-x"]
    # 원래 편입 기록을 같은 작업 id로 자식에 잇는다. 개념 없이 옮긴 항목과 그 근거만 가진다.
    linked = world.contribution("rollout", "op-x")
    assert "concept" not in linked
    assert [change["entry_id"] for change in linked["entry_changes"]] == moved
    assert {unit["evidence_id"] for unit in linked["evidence_units"]} == {f"ev-{entry_id}" for entry_id in moved}
    # 참조는 옮긴 항목을 따라가고, 사라진 항목을 가리키던 참조는 지운다.
    kubectl = world.contribution("kubectl", "lint-split")
    assert [link["entry_id"] for link in kubectl["removed_links"]] == ["e-history", "e-gone"]
    assert [(link["target"], link["entry_id"]) for link in kubectl["added_links"]] == [("concept:rollout", "e-history")]

    after = world.state()
    # 원장으로 다시 재생해도 lint가 쓴 본문과 같다.
    for slug in ("deployment", "rollout", "kubectl"):
        assert after["pages"][slug][2] == world.ledger_rebuild(slug).markdown
    assert "## Sub-entries\n- Scale\n- [[rollout|Rollout]]\n" in after["pages"]["deployment"][2]
    assert "## Sub-entries\n- Rollout\n  - History\n" in after["pages"]["rollout"][2]
    assert ("e-history", "rollout", "History", "e-rollout") in after["entries"]
    assert ("deployment", "rollout", "entry_reference") in after["links"]
    assert ("kubectl", "rollout", "entry_reference") in after["links"]
    assert ("kubectl", "deployment", "entry_reference") not in after["links"]
    assert ("doc-x", "rollout") in after["documents"]

    world.restore_lint("lint-split")

    assert world.state() == before


def test_ingest_restore_after_split_removes_document_from_parent_and_child(world: World) -> None:
    _deployment_with_thick_rollout(world)
    world.ingest("op-y", "doc-y", {"deployment": {"title": "Deployment", "entry_changes": [
        _add("e-history", "History", "e-rollout", ["B20"]),
        _add("e-strategy", "Strategy", refs=["B21"]),
    ]}})
    world.lint("lint-split")
    assert world.contribution("rollout", "lint-split")["linked_operation_ids"] == ["op-x", "op-y"]

    world.restore_ingest("op-x")

    state = world.state()
    deployment, rollout = state["pages"]["deployment"][2], state["pages"]["rollout"][2]
    assert "doc-x" not in deployment + rollout
    assert "doc-y:B21" in deployment and "doc-y:B20" not in deployment
    # History는 doc-y 근거로 자식에 남고, 뿌리 Rollout이 빠져 페이지 바로 아래로 올라간다.
    assert [entry for entry in state["entries"] if entry[1] == "rollout"] == [("e-history", "rollout", "History", None)]
    assert [entry for entry in state["entries"] if entry[1] == "deployment"] == [
        ("e-strategy", "deployment", "Strategy", None)]
    assert ("doc-x", "rollout") not in state["documents"]


def test_merge_back_waits_one_lint_after_restore_then_redirects_links(world: World) -> None:
    _deployment_with_thick_rollout(world)
    world.ingest("op-y", "doc-y", {"deployment": {"title": "Deployment", "entry_changes": [
        _add("e-history", "History", "e-rollout", ["B20"]),
    ]}})
    world.lint("lint-split")
    # 분리 뒤 다른 페이지가 자식 페이지로 링크를 건다.
    world.ingest("op-z", "doc-z", {"service": {"title": "Service", "links": [
        {"source": "concept:service", "target": "concept:rollout", "relation": "concept_related_to"},
    ]}})
    world.restore_ingest("op-x")

    # 롤백 직후에는 줄어든 근거로 바로 합치지 않는다.
    waited = world.lint("lint-wait")
    assert waited["deleted_pages"] == []
    assert waited["deferred_entry_pages"] == [world.page_id("rollout")]

    merged = world.lint("lint-merge")

    rollout_id = world.page_id("rollout")
    assert [(item["action"], item["page_id"]) for item in merged["entry_decisions"]] == [("merge_back", rollout_id)]
    assert merged["deleted_pages"] == [rollout_id]
    parent = world.contribution("deployment", "lint-merge")
    assert parent["entry_changes"] == [{"op": "revive", "entry_id": "e-rollout"}, {"op": "revive", "entry_id": "e-history"}]
    assert parent["merged_pages"] == [{"page_id": rollout_id, "slug": "rollout"}]
    state = world.state()
    assert "rollout" not in state["pages"]
    assert ("e-history", "deployment", "History", None) in state["entries"]
    assert "doc-y:B20" in state["pages"]["deployment"][2]
    assert state["pages"]["deployment"][2] == world.ledger_rebuild("deployment").markdown
    assert ("service", "deployment", "concept_related_to") in state["links"]
    assert not [link for link in state["links"] if "rollout" in link]
    with world.connect() as conn:
        assert conn.execute("SELECT merged_into_page_id FROM wiki_pages WHERE id = %s",
                            (rollout_id,)).fetchone()["merged_into_page_id"] == world.page_id("deployment")

    # 지운 자식을 가리키는 기록을 다시 재생해도 방향 전환을 따라 부모로 잇는다.
    service = world._rebuild("resync", world.page_id("service"))
    database.apply_restored_wiki_state(WS, [service], {"removed_links": [], "restored_links": service["supported_links"]}, True)
    assert ("service", "deployment", "concept_related_to") in world.state()["links"]


def test_split_restore_redirects_links_made_to_the_child_after_split(world: World) -> None:
    _deployment_with_thick_rollout(world)
    world.lint("lint-split")
    world.ingest("op-z", "doc-z", {"service": {"title": "Service", "links": [
        {"source": "concept:service", "target": "concept:rollout", "relation": "concept_related_to"},
    ]}})

    result = world.restore_lint("lint-split")

    assert result["link_changes"]["redirects"] == [
        {"from_page_id": world.page_id("rollout"), "to_page_id": world.page_id("deployment")}]
    links = world.state()["links"]
    assert ("service", "deployment", "concept_related_to") in links
    assert not [link for link in links if "rollout" in link]


def test_entry_referenced_from_two_other_pages_is_promoted_to_its_own_page(world: World) -> None:
    world.ingest("op-x", "doc-x", {
        "deployment": {"title": "Deployment", "entry_changes": [_add("e-selector", "Label Selector", refs=["B1"])]},
        "service": {"title": "Service", "links": [_reference("service", "deployment", "e-selector", "Label Selector")]},
        "replicaset": {"title": "ReplicaSet", "links": [_reference("replicaset", "deployment", "e-selector", "Label Selector")]},
    })

    preview = wiki_maintenance.PostgresWikiMaintenance().lint(
        WikiMaintenanceCommand(user_id="user", workspace_id=WS, provider="openai", model="gpt-5-nano", dry_run=True)
    )
    assert [(item["action"], item["entry_id"]) for item in preview["entry_decisions"]] == [("promote", "e-selector")]
    assert "label-selector" not in world.state()["pages"]

    result = world.lint("lint-promote")

    assert [(item["action"], item["entry_id"]) for item in result["entry_decisions"]] == [("promote", "e-selector")]
    links = world.state()["links"]
    assert {link for link in links if link[2] == "entry_reference"} == {
        ("deployment", "label-selector", "entry_reference"),
        ("service", "label-selector", "entry_reference"),
        ("replicaset", "label-selector", "entry_reference"),
    }
    assert ("e-selector", "label-selector", "Label Selector", None) in world.state()["entries"]


def test_only_pages_changed_since_last_lint_are_rechecked(world: World) -> None:
    _deployment_with_thick_rollout(world)
    world.mark_linted()
    world.ingest("op-t", "doc-t", {"service": {"title": "Service", "entry_changes": [
        _add("e-port", "Port", refs=["B1"]),
        *(_add(f"e-port-{index}", f"Port {index}", "e-port", [f"B{index + 1}"]) for index in range(5)),
    ]}})

    result = world.lint("lint-changed")

    # Deployment도 분리 기준을 넘지만 지난 lint 뒤 바뀌지 않아 보지 않는다.
    assert [(item["action"], item["entry_id"]) for item in result["entry_decisions"]] == [("split", "e-port")]
    assert "rollout" not in world.state()["pages"]
