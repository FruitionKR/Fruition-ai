"""lint 하위 항목 재검사(ADR 0042 3단계)를 실제 PostgreSQL과 기록 재생으로 확인한다.

document-svc 기여 원장은 가짜다. lint 결과의 `ledger_contributions`를 원장에 더하고, 어떤 행도 일괄 비활성화하거나
다시 켜지 않는다. lint 복원은 대상 lint 기록과 그 lint가 이은 기록(`linked_by_operation_id`)만 끄고, 그 행이 있던
페이지를 남은 기여로 재조립한다. 남은 행이 없는 페이지만 지우고, 남은 행이 lint 기록뿐이어도 재조립에 넘긴다(클러스터
승격 페이지는 lint 기록이 근거다). 재생 결과 삭제로 끝난 페이지는 복원 결과의 `deleted_pages`로 받는다.
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
from app.modules.wiki_ingestion.domain.operation_recovery import RestoreConflictError
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

    def _record(self, page_id: str, operation_id: str, document_id: str | None, linked_by: str | None = None) -> None:
        self.ledger.append({"page_id": page_id, "ingest_operation_id": operation_id, "document_id": document_id,
                            "linked_by_operation_id": linked_by,
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
            # 근거 블록이 없는 항목은 근거 없이 둔다.
            adds = [entry for entry in data.get("entry_changes", []) if entry["op"] == "add" and entry["anchor_reference_ids"]]
            evidence = [
                {"evidence_id": f"ev-{entry['entry_id']}", "claim": f"{entry['name']} 근거({document_id})",
                 "anchor_reference_ids": entry["anchor_reference_ids"], "related_concept_slugs": [slug],
                 "source_document_id": document_id}
                for entry in adds
            ]
            for entry, unit in zip(adds, evidence):
                entry["evidence_claim_ids"] = [unit["evidence_id"]]
            payload = {
                "schema_version": 1, "operation_id": operation_id, "document_id": document_id, "page_id": page_id,
                "concept": {"slug": slug, "title": data.get("title", slug.title()), "definition": f"{slug} 정의",
                            "source_document_ids": [document_id],
                            "anchor_reference_ids": data.get("concept_refs", [])},
                # evidence: 항목에 딸리지 않은 페이지 근거(블록 이름 목록).
                "evidence_units": evidence + [
                    {"evidence_id": f"ev-{operation_id}-{block}", "claim": f"{slug} 근거 {block}({document_id})",
                     "anchor_reference_ids": [block], "related_concept_slugs": [slug], "source_document_id": document_id}
                    for block in data.get("evidence", [])
                ],
                "source_blocks": [], "links": data.get("links", []),
                "source_key_points": [{"text": text, "anchor_reference_ids": [block]}
                                      for text, block in data.get("key_points", [])],
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
            if row["page_id"] == page_id and row["active"]
            and (without is None or without not in (row["ingest_operation_id"], row["linked_by_operation_id"]))
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
        for row in result["ledger_contributions"]:
            self._record(row["page_id"], row["operation_id"], row["document_id"], row["linked_by_operation_id"])
        return result

    def _deactivate(self, predicate) -> None:
        for row in self.ledger:
            if predicate(row):
                row["active"] = False

    def _supported(self, page_id: str, without: str) -> bool:
        """남은 활성 행이 있으면 재조립 대상이다. 페이지를 지울지는 ai 재생이 정한다(docs/api/wiki.md 복원 계약)."""
        return any(
            row["page_id"] == page_id and row["active"]
            and without not in (row["ingest_operation_id"], row["linked_by_operation_id"])
            for row in self.ledger
        )

    def restore_ingest(self, operation_id: str) -> dict:
        run = f"restore-{operation_id}"
        page_ids = list(dict.fromkeys(
            row["page_id"] for row in self.ledger if row["ingest_operation_id"] == operation_id and row["active"]
        ))
        changed = [self._rebuild(run, page_id, operation_id) for page_id in page_ids if self._supported(page_id, operation_id)]
        deleted = [page_id for page_id in page_ids if not self._supported(page_id, operation_id)]
        deleted += [page["page_id"] for page in changed if page.get("deleted")]
        changed = [page for page in changed if not page.get("deleted")]
        database.apply_restored_wiki_state_and_cleanup(
            run, WS, changed,
            {"removed_links": [], "restored_links": [link for page in changed for link in page["supported_links"]]},
            True, deleted,
        )
        self._deactivate(lambda row: row["ingest_operation_id"] == operation_id)
        self._run("restore_ingest", {"changed_pages": [{"page_id": page["page_id"]} for page in changed]})
        return {"changed_pages": changed, "deleted_pages": deleted}

    def restore_lint(self, operation_id: str) -> dict:
        """document-svc처럼 대상 lint 행만 끄고, 그 lint가 바꾼 페이지를 남은 기여로 재조립한다. 다시 켜는 행은 없다."""
        page_ids = sorted({
            row["page_id"] for row in self.ledger
            if row["active"] and operation_id in (row["ingest_operation_id"], row["linked_by_operation_id"])
        })
        result = RestoreWikiPagesUseCase(self.restore).execute_lint(
            LintOperationRestoreCommand(
                operation_id=f"restore-{operation_id}", target_operation_id=operation_id, workspace_id=WS,
                rebuild_pages=tuple(
                    RebuildPageCommand(page_id, self._keep(page_id, operation_id))
                    for page_id in page_ids if self._supported(page_id, operation_id)
                ),
                deleted_pages=tuple(page_id for page_id in page_ids if not self._supported(page_id, operation_id)),
            )
        )
        self._deactivate(lambda row: operation_id in (row["ingest_operation_id"], row["linked_by_operation_id"]))
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
            references = conn.execute(
                "SELECT page_id, entry_id, target_slug, name, parent_entry_id FROM wiki_page_entry_references"
            ).fetchall()
            documents = conn.execute(
                "SELECT document_id, wiki_page_id FROM document_wiki_links WHERE relation_type = 'extracted_concept'"
            ).fetchall()
        return {
            "pages": {row["slug"]: (row["title"], row["summary"], self.read(row["markdown_uri"])) for row in pages},
            "links": sorted((slug[row["from_page_id"]], slug[row["to_page_id"]], row["link_type"]) for row in links),
            "entries": sorted((row["entry_id"], slug[row["page_id"]], row["name"], row["parent_entry_id"]) for row in entries),
            "references": sorted(
                (slug[row["page_id"]], row["entry_id"], row["target_slug"], row["name"], row["parent_entry_id"])
                for row in references
            ),
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

    # 처음부터 사라진 항목(e-gone)을 가리키던 kubectl 참조는 복원이 kubectl을 다시 그리며 함께 정리한다.
    after = world.state()
    assert after["references"] == [row for row in before["references"] if row[1] != "e-gone"]
    assert "|Gone]]" in before["pages"]["kubectl"][2] and "|Gone]]" not in after["pages"]["kubectl"][2]
    assert {key: value for key, value in after.items() if key not in ("references", "pages")} == {
        key: value for key, value in before.items() if key not in ("references", "pages")}
    assert {slug: page for slug, page in after["pages"].items() if slug != "kubectl"} == {
        slug: page for slug, page in before["pages"].items() if slug != "kubectl"}


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


def test_merge_back_on_first_lint_after_restore_removes_links_to_the_child_by_record(world: World) -> None:
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

    # 되돌린 직후 첫 lint가 줄어든 근거로 판단한다(추가 지연 없음).
    merged = world.lint("lint-merge")

    rollout_id = world.page_id("rollout")
    assert [(item["action"], item["page_id"]) for item in merged["entry_decisions"]] == [("merge_back", rollout_id)]
    assert merged["deleted_pages"] == [rollout_id]
    parent = world.contribution("deployment", "lint-merge")
    assert parent["entry_changes"] == [{"op": "revive", "entry_id": "e-rollout"}, {"op": "revive", "entry_id": "e-history"}]
    # 방향 전환 없이, 자식으로 오던 링크는 그 링크를 낸 페이지의 lint 기록이 지운다.
    assert world.contribution("service", "lint-merge")["removed_links"] == [
        {"source": "concept:service", "target": "concept:rollout", "relation": "concept_related_to"}]
    state = world.state()
    assert "rollout" not in state["pages"]
    assert ("e-history", "deployment", "History", None) in state["entries"]
    assert "doc-y:B20" in state["pages"]["deployment"][2]
    assert state["pages"]["deployment"][2] == world.ledger_rebuild("deployment").markdown
    assert not [link for link in state["links"] if "rollout" in link or link[0] == "service"]
    with world.connect() as conn:
        columns = {row["column_name"] for row in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'wiki_pages'").fetchall()}
    assert "merged_into_page_id" not in columns

    # 다시 재생해도 지운 자식으로도 부모로도 잇지 않는다.
    service = world._rebuild("resync", world.page_id("service"))
    database.apply_restored_wiki_state(WS, [service], {"removed_links": [], "restored_links": service["supported_links"]}, True)
    assert not [link for link in world.state()["links"] if link[0] == "service"]


def test_split_restore_drops_links_made_to_the_child_after_split(world: World) -> None:
    _deployment_with_thick_rollout(world)
    world.lint("lint-split")
    world.ingest("op-z", "doc-z", {"service": {"title": "Service", "links": [
        {"source": "concept:service", "target": "concept:rollout", "relation": "concept_related_to"},
    ]}})

    result = world.restore_lint("lint-split")

    assert "redirects" not in result["link_changes"]
    links = world.state()["links"]
    # 지운 자식을 가리키던 뒤 링크는 부모로 옮기지 않고 사라진다(ADR 0042 결정 1).
    assert not [link for link in links if link[0] == "service"]
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


def _thick(entry_id: str, name: str, refs: int) -> list[dict]:
    """세부 항목 5개를 가진 항목(분리 기준 C=5)."""
    return [_add(entry_id, name, refs=[f"B{refs}"]),
            *(_add(f"{entry_id}-{index}", f"{name} {index}", entry_id, [f"B{refs + index + 1}"]) for index in range(5))]


def test_merging_two_children_at_once_combines_records_of_one_operation(world: World) -> None:
    world.ingest("op-x", "doc-x", {"deployment": {"title": "Deployment", "entry_changes": [
        *_thick("e-alpha", "Alpha", 1), *_thick("e-beta", "Beta", 20)]}})
    world.ingest("op-d", "doc-d", {"deployment": {"title": "Deployment", "evidence": ["B99"]}})
    world.lint("lint-split")
    # 분리 뒤 한 문서가 두 자식에만 기여한다.
    world.ingest("op-z", "doc-z", {
        "alpha": {"title": "Alpha", "entry_changes": [_add("e-alpha-z", "Alpha Z", "e-alpha", ["B40"])]},
        "beta": {"title": "Beta", "entry_changes": [_add("e-beta-z", "Beta Z", "e-beta", ["B41"])]},
    })
    world.restore_ingest("op-x")

    merged = world.lint("lint-merge")
    
    assert sorted(item["action"] for item in merged["entry_decisions"]) == ["merge_back", "merge_back"]
    linked = [row for row in merged["ledger_contributions"] if row["operation_id"] == "op-z"]
    assert linked == [{"page_id": world.page_id("deployment"), "operation_id": "op-z", "document_id": "doc-z",
                       "contribution_key": f"wiki/{WS}/pages/{world.page_id('deployment')}/ops/op-z.json",
                       "linked_by_operation_id": "lint-merge"}]
    state = world.state()
    replayed = world.ledger_rebuild("deployment")
    assert state["pages"]["deployment"][2] == replayed.markdown
    assert {entry["entry_id"] for entry in replayed.entries} == {"e-alpha-z", "e-beta-z"}
    assert {entry[0] for entry in state["entries"] if entry[1] == "deployment"} == {"e-alpha-z", "e-beta-z"}


def test_nested_entries_split_parent_first_even_when_child_id_sorts_first(world: World) -> None:
    world.ingest("op-x", "doc-x", {"deployment": {"title": "Deployment", "entry_changes": [
        _add("z-parent", "Parent", refs=["B1"]),
        *(_add(f"m-sibling-{index}", f"Sibling {index}", "z-parent", [f"B{index + 2}"]) for index in range(4)),
        *_thick("a-child", "Child", 10),
    ]}})
    world.ingest("op-y", "doc-y", {"deployment": {"title": "Deployment", "entry_changes": [
        {"op": "move", "entry_id": "a-child", "parent_entry_id": "z-parent"}]}})

    result = world.lint("lint-split")

    assert [(item["action"], item["entry_id"], item.get("skipped_reason")) for item in result["entry_decisions"]] == [
        ("split", "z-parent", None), ("split", "a-child", "ancestor_moved")]
    homes = {}
    for entry_id, slug, _name, _parent in world.state()["entries"]:
        homes.setdefault(entry_id, []).append(slug)
    assert all(len(pages) == 1 for pages in homes.values())
    assert homes["a-child-0"] == ["parent"]
    for slug in ("deployment", "parent"):
        assert {entry["entry_id"] for entry in world.ledger_rebuild(slug).entries} == {
            entry[0] for entry in world.state()["entries"] if entry[1] == slug}


def test_lint_restore_of_merge_back_recreates_child_with_its_entries_and_links(world: World) -> None:
    _deployment_with_thick_rollout(world)
    world.ingest("op-d", "doc-d", {"deployment": {"title": "Deployment", "evidence": ["B99"]}})
    world.lint("lint-split")
    # 분리 뒤 자식에만 기여한 편입과 자식으로 가는 링크.
    world.ingest("op-w", "doc-w", {"rollout": {"title": "Rollout", "entry_changes": [
        _add("e-canary", "Canary", refs=["B30"])]}})
    world.ingest("op-z", "doc-z", {"service": {"title": "Service", "links": [
        {"source": "concept:service", "target": "concept:rollout", "relation": "concept_related_to"},
    ]}})
    world.restore_ingest("op-x")
    before = world.state()

    merged = world.lint("lint-merge")
    assert merged["deleted_pages"] == [world.page_id("rollout")]
    assert "rollout" not in world.state()["pages"]
    assert world.contribution("rollout", "lint-merge")["content_action"] == "delete"

    world.restore_lint("lint-merge")

    # 자식이 다시 재생되고, 다른 페이지 기록이 지웠던 자식으로 오는 링크도 원래 기록이 아직 이어 되살아난다.
    assert world.state() == before


def test_lint_restore_of_merge_back_keeps_parent_link_added_later_by_ingest(world: World) -> None:
    _deployment_with_thick_rollout(world)
    world.ingest("op-y", "doc-y", {"deployment": {"title": "Deployment", "entry_changes": [
        _add("e-history", "History", "e-rollout", ["B20"])]}})
    world.lint("lint-split")
    world.ingest("op-z", "doc-z", {"service": {"title": "Service", "links": [
        {"source": "concept:service", "target": "concept:rollout", "relation": "concept_related_to"},
    ]}})
    world.restore_ingest("op-x")
    world.lint("lint-merge")
    # 합친 뒤 다른 편입이 같은 간선을 직접 잇는다.
    world.ingest("op-q", "doc-q", {"service": {"title": "Service", "links": [
        {"source": "concept:service", "target": "concept:deployment", "relation": "concept_related_to"},
    ]}})

    world.restore_lint("lint-merge")

    links = world.state()["links"]
    assert ("service", "rollout", "concept_related_to") in links
    assert ("service", "deployment", "concept_related_to") in links


def test_references_added_from_two_pages_get_the_untouched_home_promoted(world: World) -> None:
    world.ingest("op-x", "doc-x", {"deployment": {"title": "Deployment", "entry_changes": [
        _add("e-selector", "Label Selector", refs=["B1"])]}})
    world.lint("lint-first")
    # 집 페이지는 그대로 두고 다른 두 페이지만 참조를 더한다.
    world.ingest("op-a", "doc-a", {"service": {"title": "Service", "links": [
        _reference("service", "deployment", "e-selector", "Label Selector")]}})
    world.ingest("op-b", "doc-b", {"replicaset": {"title": "ReplicaSet", "links": [
        _reference("replicaset", "deployment", "e-selector", "Label Selector")]}})

    result = world.lint("lint-promote")

    assert [(item["action"], item["entry_id"]) for item in result["entry_decisions"]] == [("promote", "e-selector")]
    assert "label-selector" in world.state()["pages"]


def test_child_left_without_support_is_deleted_by_the_restore_itself(world: World) -> None:
    world.ingest("op-x", "doc-x", {"deployment": {"title": "Deployment", "entry_changes": _thick("e-rollout", "Rollout", 1)}})
    world.ingest("op-w", "doc-w", {"deployment": {"title": "Deployment", "entry_changes": [
        _add("e-scale", "Scale", refs=["B30"])]}})
    world.lint("lint-split")
    rollout_id = world.page_id("rollout")

    # 자식에 남는 것은 분리 lint 기록뿐이다. lint 메타데이터만으로 페이지를 남기지 않는다(ADR 0042 결정 3).
    result = world.restore_ingest("op-x")

    assert result["deleted_pages"] == [rollout_id]
    # 지운 자식을 가리키던 부모의 요약 줄(참조)도 같은 복원에서 정리한다.
    state = world.state()
    assert "rollout" not in state["pages"]
    assert "[[rollout|" not in state["pages"]["deployment"][2]
    assert not [row for row in state["references"] if row[2] == "rollout"]

    # 기록은 다음 lint가 지워 원장 재생과 맞춘다.
    world.lint("lint-clean")

    assert world.state()["pages"]["deployment"][2] == world.ledger_rebuild("deployment").markdown


def test_split_restore_keeps_child_with_independent_later_contribution(world: World) -> None:
    _deployment_with_thick_rollout(world)
    world.lint("lint-split")
    # 분리 뒤 자식에만 기여한 독립 편입.
    world.ingest("op-w", "doc-w", {"rollout": {"title": "Rollout", "entry_changes": [
        _add("e-canary", "Canary", refs=["B30"])]}})

    result = world.restore_lint("lint-split")

    # 분리로 만들었다는 이유로 지우지 않는다. 분리 효과(옮긴 항목)만 부모로 돌아간다.
    state = world.state()
    assert world.page_id("rollout") not in result["deleted_pages"]
    assert [entry for entry in state["entries"] if entry[1] == "rollout"] == [("e-canary", "rollout", "Canary", None)]
    assert ("e-history", "deployment", "History", "e-rollout") in state["entries"]
    assert "[[rollout|" not in state["pages"]["deployment"][2]


def test_split_restore_that_would_put_one_entry_on_two_pages_is_rejected_before_writing(world: World) -> None:
    _deployment_with_thick_rollout(world)
    world.lint("lint-split")
    # 분리 뒤 다른 편입이 자식의 History에 근거를 더했다. 분리를 빼면 History가 부모와 자식 양쪽에 재생된다.
    world.ingest("op-w", "doc-w", {"rollout": {"title": "Rollout", "entry_changes": [
        _add("e-history", "History", "e-rollout", ["B30"])]}})
    before = world.state()

    with pytest.raises(RestoreConflictError):
        world.restore_lint("lint-split")

    assert world.state() == before


def test_split_keeps_entry_reference_copy_through_later_ingest(world: World) -> None:
    _deployment_with_thick_rollout(world)
    world.lint("lint-split")
    reference = ("deployment", "e-rollout", "rollout", "Rollout", None)
    assert reference in world.state()["references"]

    world.ingest("op-w", "doc-w", {"deployment": {"title": "Deployment", "entry_changes": [
        _add("e-strategy", "Strategy", refs=["B30"])]}})

    state = world.state()
    assert reference in state["references"]
    assert "[[rollout|Rollout]]" in state["pages"]["deployment"][2]


def test_merge_and_split_of_the_receiving_parent_in_one_lint_defers_the_split(world: World) -> None:
    # A 아래 E가 두꺼워 E만 나뉜다(근거 없는 세부 항목이라 A 전체 근거는 기준 아래).
    world.ingest("op-x", "doc-x", {"deployment": {"title": "Deployment", "entry_changes": [
        _add("e-a", "Area", refs=["B1"]),
        *(_add(f"e-a{index}", f"Area {index}", "e-a") for index in range(1, 4)),
        _add("e-e", "Edge", "e-a", ["B9"]),
        {**_add("e-e5", "Edge 5", "e-e"), "anchor_reference_ids": []},
    ]}})
    world.ingest("op-y", "doc-y", {"deployment": {"title": "Deployment", "entry_changes": [
        {**_add(f"e-e{index}", f"Edge {index}", "e-e"), "anchor_reference_ids": []} for index in range(1, 5)]}})
    split = world.lint("lint-split")
    assert [(item["action"], item["entry_id"]) for item in split["entry_decisions"]] == [("split", "e-e")]
    world.restore_ingest("op-y")
    # 합치기와 같은 lint에서 A도 분리 기준(자식 5개)을 넘는다.
    world.ingest("op-z", "doc-z", {"deployment": {"title": "Deployment", "entry_changes": [
        _add("e-a4", "Area 4", "e-a", ["B20"]), _add("e-a5", "Area 5", "e-a", ["B21"])]}})

    merged = world.lint("lint-merge")

    assert [(item["action"], item.get("skipped_reason")) for item in merged["entry_decisions"]] == [
        ("merge_back", None), ("split", "parent_merged")]
    assert world.page_id("deployment") in merged["deferred_entry_pages"]
    assert ("e-e", "deployment", "Edge", "e-a") in world.state()["entries"]

    world.lint("lint-next")

    entries = {entry[0]: entry[1] for entry in world.state()["entries"]}
    assert entries["e-e"] == entries["e-a"] == "area"
    assert {entry["entry_id"] for entry in world.ledger_rebuild("area").entries} >= {"e-a", "e-e", "e-e5"}


def test_lint_restore_of_merge_back_does_not_revive_edge_whose_ingest_was_rolled_back(world: World) -> None:
    _deployment_with_thick_rollout(world)
    world.ingest("op-y", "doc-y", {"deployment": {"title": "Deployment", "entry_changes": [
        _add("e-history", "History", "e-rollout", ["B20"])]}})
    world.lint("lint-split")
    world.ingest("op-s", "doc-s", {"service": {"title": "Service"}})
    world.ingest("op-z", "doc-z", {"service": {"title": "Service", "links": [
        {"source": "concept:service", "target": "concept:rollout", "relation": "concept_related_to"},
    ]}})
    world.restore_ingest("op-x")
    world.lint("lint-merge")
    # 자식으로 가던 간선을 낸 편입을 되돌린다. Service는 다른 편입으로 남는다.
    world.restore_ingest("op-z")

    world.restore_lint("lint-merge")

    state = world.state()
    assert "service" in state["pages"] and "rollout" in state["pages"]
    assert not [link for link in state["links"] if link[0] == "service"]


def _merged_back_rollout(world: World) -> None:
    """Rollout을 나눴다가 doc-x 롤백으로 줄어 부모로 다시 합친 상태."""
    _deployment_with_thick_rollout(world)
    world.ingest("op-y", "doc-y", {"deployment": {"title": "Deployment", "entry_changes": [
        _add("e-history", "History", "e-rollout", ["B20"])]}})
    world.lint("lint-split")
    world.restore_ingest("op-x")


def test_resplit_after_merge_back_does_not_reuse_the_deleted_child_page(world: World) -> None:
    _merged_back_rollout(world)
    world.lint("lint-merge")
    old_id = world.page_id("rollout")
    old_records = {key: text for key, text in world.objects.items() if f"/pages/{old_id}/" in key}
    world.ingest("op-v", "doc-v", {"deployment": {"title": "Deployment", "entry_changes": _thick("e-rollout-2", "Rollout", 50)}})

    result = world.lint("lint-resplit")

    assert [(item["action"], item["entry_id"]) for item in result["entry_decisions"]] == [("split", "e-rollout-2")]
    assert "deployment-rollout" in world.state()["pages"]
    assert {key: text for key, text in world.objects.items() if f"/pages/{old_id}/" in key} == old_records
    with world.connect() as conn:
        assert conn.execute("SELECT status FROM wiki_pages WHERE id = %s", (old_id,)).fetchone()["status"] == "deleted"


def test_split_reuses_vectors_of_moved_entries(world: World) -> None:
    _deployment_with_thick_rollout(world)
    moved = [f"e-{step}" for step in ROLLOUT_STEPS]

    def vectors() -> set:
        with world.connect() as conn:
            return {
                (row["id"], row["status"], row["representation_text"])
                for row in conn.execute(
                    """
                    SELECT vector.id, vector.status, vector.representation_text
                    FROM wiki_embedding_units unit
                    JOIN wiki_embedding_vectors vector ON vector.id = unit.embedding_vector_id
                    WHERE unit.unit_type = 'sub_entry' AND unit.entry_id = ANY(%s)
                    """,
                    (moved,),
                ).fetchall()
            }

    with world.connect() as conn:
        conn.execute("UPDATE wiki_embedding_vectors SET status = 'ready'")
    before = vectors()
    assert len(before) == len(moved)

    world.lint("lint-split")

    assert vectors() == before


def test_merge_back_carries_plain_child_evidence_to_the_parent(world: World) -> None:
    _merged_back_rollout(world)
    world.ingest("op-p", "doc-p", {"rollout": {"title": "Rollout", "evidence": ["B70"]}})

    merged = world.lint("lint-merge")

    assert merged["deleted_pages"] == [world.page_id("rollout")]
    carried = [row for row in merged["ledger_contributions"] if row["operation_id"] == "op-p"]
    assert [(row["page_id"], row["linked_by_operation_id"]) for row in carried] == [
        (world.page_id("deployment"), "lint-merge")]
    body = world.state()["pages"]["deployment"][2]
    assert "doc-p:B70" in body
    assert body == world.ledger_rebuild("deployment").markdown


def test_child_with_much_own_evidence_is_not_merged_back(world: World) -> None:
    _merged_back_rollout(world)
    world.ingest("op-p", "doc-p", {"rollout": {"title": "Rollout", "evidence": [f"B{70 + index}" for index in range(6)]}})

    result = world.lint("lint-merge")

    assert result["deleted_pages"] == []
    assert "rollout" in world.state()["pages"]


def test_references_between_entries_split_in_the_same_lint_follow_both_moves(world: World) -> None:
    world.ingest("op-x", "doc-x", {
        "alpha": {"title": "Alpha", "entry_changes": _thick("e-xen", "Xen", 1),
                  "links": [{**_reference("alpha", "beta", "e-yak", "Yak"), "parent_entry_id": "e-xen"}]},
        "beta": {"title": "Beta", "entry_changes": _thick("e-yak", "Yak", 20)},
    })

    result = world.lint("lint-split")

    assert sorted(item["entry_id"] for item in result["entry_decisions"]) == ["e-xen", "e-yak"]
    references = {link for link in world.state()["links"] if link[2] == "entry_reference"}
    assert references == {("alpha", "xen", "entry_reference"), ("beta", "yak", "entry_reference"),
                          ("xen", "yak", "entry_reference")}
    for slug in ("alpha", "xen"):
        assert world.state()["pages"][slug][2] == world.ledger_rebuild(slug).markdown


def test_references_to_a_page_that_lost_its_last_entry_are_removed(world: World) -> None:
    world.ingest("op-x", "doc-x", {"deployment": {"title": "Deployment", "entry_changes": [
        _add("e-scale", "Scale", refs=["B1"])]}})
    world.ingest("op-w", "doc-w", {"deployment": {"title": "Deployment", "evidence": ["B2"]}})
    world.ingest("op-q", "doc-q", {"kubectl": {"title": "kubectl", "links": [
        _reference("kubectl", "deployment", "e-scale", "Scale")]}})
    world.lint("lint-first")

    result = world.restore_ingest("op-x")

    # 페이지는 doc-w로 남고 항목만 사라졌다. 같은 복원이 다른 페이지의 참조 사본·본문 목록·간선을 함께 정리한다.
    state = world.state()
    assert "deployment" in state["pages"]
    assert world.page_id("kubectl") in {page["page_id"] for page in result["changed_pages"]}
    assert ("kubectl", "deployment", "entry_reference") not in state["links"]
    assert not [row for row in state["references"] if row[0] == "kubectl"]
    assert "[[deployment|" not in state["pages"]["kubectl"][2]

    # 기록은 다음 lint가 지워, 그 뒤에는 원장 재생과 본문이 같다.
    world.lint("lint-clean")

    state = world.state()
    assert world.contribution("kubectl", "lint-clean")["removed_links"][0]["entry_id"] == "e-scale"
    assert state["pages"]["kubectl"][2] == world.ledger_rebuild("kubectl").markdown


def test_split_drops_key_points_of_moved_entries_from_parent_body_and_units(world: World) -> None:
    world.ingest("op-x", "doc-x", {"deployment": {
        "title": "Deployment",
        "entry_changes": [*_thick("e-rollout", "Rollout", 1), _add("e-scale", "Scale", refs=["B9"])],
        # B3는 옮길 Rollout 아래 항목만, B9는 남는 Scale이 인용한다.
        "key_points": [("롤아웃 단계 요약", "B3"), ("스케일 요약", "B9")],
        # 편입은 개념 인용에 모든 근거 블록을 둔다.
        "concept_refs": ["B3", "B9"],
    }})
    assert "롤아웃 단계 요약" in world.state()["pages"]["deployment"][2]

    world.lint("lint-split")

    body = world.state()["pages"]["deployment"][2]
    assert "롤아웃 단계 요약" not in body
    assert "스케일 요약" in body
    with world.connect() as conn:
        texts = [row["text"] for row in conn.execute(
            "SELECT text FROM wiki_embedding_units WHERE page_id = %s", (world.page_id("deployment"),)).fetchall()]
    assert texts and not [text for text in texts if "롤아웃 단계 요약" in text]
