import json
import os
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from app.modules.wiki_ingestion.domain.manual_wiki_edit import (
    manual_link_changes,
    wikilink_slugs,
)
from app.modules.wiki_ingestion.domain.operation_recovery import PageContribution
from app.modules.wiki_ingestion.domain.orphan_link_lint import replay_supported_links
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as repository
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_output_persistence as persistence
from app.modules.wiki_ingestion.infrastructure.concept_contribution_rebuild import (
    load_concept_contributions,
    rebuild_concept_page,
)
from app.modules.wiki_ingestion.application.models import SourceSnapshotRestoreCommand
from app.modules.wiki_ingestion.infrastructure.wiki_page_restore import ObjectStorageWikiPageRestore


def _ingest(operation_id: str, evidence: str, links: list[dict] | None = None) -> dict:
    return {
        "schema_version": 1,
        "operation_id": operation_id,
        "document_id": f"doc-{operation_id}",
        "page_id": "C1",
        "concept": {
            "slug": "cache",
            "title": "캐시",
            "definition": evidence,
            "source_document_ids": [f"doc-{operation_id}"],
            "evidence_claim_ids": [f"ev-{operation_id}"],
        },
        "evidence_units": [
            {
                "evidence_id": f"ev-{operation_id}",
                "claim": evidence,
                "anchor_reference_ids": [],
                "source_document_id": f"doc-{operation_id}",
            }
        ],
        "source_blocks": [],
        "links": links or [],
    }


def _manual(operation_id: str, *, added=None, removed=None) -> dict:
    return {
        "schema_version": 1,
        "artifact_type": "manual",
        "operation_id": operation_id,
        "page_id": "C1",
        "page_type": "concept",
        "added_links": added or [],
        "removed_links": removed or [],
    }


CDN = {"source": "concept:cache", "target": "concept:cdn", "relation": "related_to"}
REDIS = {"source": "concept:cache", "target": "concept:redis", "relation": "related_to"}
MEMCACHED = {"source": "concept:cache", "target": "concept:memcached", "relation": "related_to"}


def test_wikilink_slugs_reads_plain_and_labeled_links_once() -> None:
    markdown = "- [[redis|Redis]]\n- [[cdn]] 그리고 다시 [[redis|레디스]]\n- [[ ]] [1, 2]"

    assert wikilink_slugs(markdown) == ["redis", "cdn"]


def test_manual_link_changes_apply_only_edited_wikilinks() -> None:
    added, removed = manual_link_changes(
        page_ref="concept:cache",
        old_markdown="- [[redis|Redis]]\n- [[cdn|CDN]]",
        new_markdown="- [[redis|Redis]]\n- [[memcached|Memcached]]\n- [[ghost]]\n- [[cache]]",
        current_links=[
            {"target": "concept:redis", "relation": "related_to"},
            {"target": "concept:cdn", "relation": "related_to"},
            {"target": "concept:cdn", "relation": "part_of"},
            # 본문에 없던 edge는 사람이 지운 것이 아니므로 남긴다.
            {"target": "concept:lru", "relation": "related_to"},
        ],
        target_refs={
            "redis": "concept:redis",
            "memcached": "concept:memcached",
            "cache": "concept:cache",
        },
    )

    assert added == [MEMCACHED]
    assert removed == [CDN, {**CDN, "relation": "part_of"}]


def test_manual_link_changes_from_source_page_mentions_concept() -> None:
    added, removed = manual_link_changes(
        page_ref="source:doc-1",
        old_markdown="",
        new_markdown="[[redis]]",
        current_links=[],
        target_refs={"redis": "concept:redis"},
    )

    assert added == [{"source": "source:doc-1", "target": "concept:redis", "relation": "source_mentions_concept"}]
    assert removed == []


def test_replay_applies_manual_link_removal_after_ingest_link() -> None:
    links = replay_supported_links([
        _ingest("A", "근거", [CDN, REDIS]),
        _manual("M", added=[MEMCACHED], removed=[CDN]),
    ])

    assert links == [REDIS, MEMCACHED]


def _load(objects: dict[str, str], operation_ids: list[str]) -> list[PageContribution]:
    return load_concept_contributions(
        workspace_id="ws",
        page_id="C1",
        keep_contributions=[
            {"operation_id": operation_id, "sequence": sequence}
            for sequence, operation_id in enumerate(operation_ids, start=1)
        ],
        read_text=objects.__getitem__,
    )


def _objects(**contributions: dict) -> dict[str, str]:
    return {
        f"wiki/ws/pages/C1/ops/{operation_id}.json": json.dumps(contribution, ensure_ascii=False)
        for operation_id, contribution in contributions.items()
    }


def test_rebuild_keeps_manual_body_and_appends_later_evidence() -> None:
    manual_body = "# 내가 고친 캐시\n\n사람이 쓴 설명 [링크](https://example.com)\n\n## Evidence\n- 사람이 남긴 근거\n"
    objects = _objects(
        A=_ingest("A", "A 근거", [CDN, REDIS]),
        M=_manual("M", removed=[CDN]),
        B=_ingest("B", "B 근거 [외부](https://attacker.example/x)"),
    )
    objects["wiki/ws/pages/C1/ops/M.md"] = manual_body

    rebuilt = rebuild_concept_page(_load(objects, ["A", "M", "B"]))

    assert rebuilt.markdown.startswith("# 내가 고친 캐시\n\n사람이 쓴 설명 [링크](https://example.com)")
    assert "A 근거" not in rebuilt.markdown
    assert "- ev-B: B 근거" in rebuilt.markdown
    assert "https://attacker.example" not in rebuilt.markdown
    assert rebuilt.title is None and rebuilt.summary is None
    assert rebuilt.supported_links == (REDIS,)
    assert rebuilt.source_document_ids == ("doc-A", "doc-B")


def test_rebuild_without_manual_contribution_regenerates_from_json() -> None:
    objects = _objects(A=_ingest("A", "A 근거"))

    rebuilt = rebuild_concept_page(_load(objects, ["A"]))

    assert "# 캐시" in rebuilt.markdown
    assert rebuilt.title == "캐시"


def test_restoring_source_page_to_manual_snapshot_keeps_title_and_summary() -> None:
    objects = {
        "wiki/ws/pages/S1/ops/M.md": "사람이 고친 본문\n",
        "wiki/ws/pages/S1/ops/M.json": json.dumps(_manual("M")),
        "wiki/ws/pages/S1/ops/A.md": "# AI 제목\n\n## Summary\nAI 요약\n",
    }
    restore = ObjectStorageWikiPageRestore(objects.__getitem__, lambda *_args: "", Mock())
    source = SourceSnapshotRestoreCommand("S1", "doc-1")

    manual = restore.restore_source_page("R1", "M", "ws", source)
    generated = restore.restore_source_page("R2", "A", "ws", source)

    assert (manual["title"], manual["summary"]) == (None, None)
    assert (generated["title"], generated["summary"]) == ("AI 제목", "AI 요약")


def test_active_manual_markdown_reads_latest_active_manual_contribution(monkeypatch) -> None:
    conn = Mock()
    conn.execute.return_value.fetchone.return_value = {"markdown_uri": "s3://bucket/source.md"}
    rows = [
        {"sequence_revision": 1, "active": True, "object_key": "ops/A.json"},
        {"sequence_revision": 2, "active": True, "object_key": "ops/M1.json"},
        {"sequence_revision": 3, "active": True, "object_key": "ops/B.json"},
        {"sequence_revision": 4, "active": False, "object_key": "ops/M2.json"},
    ]
    objects = {
        "ops/M1.json": json.dumps(_manual("M1")),
        "ops/M1.md": "첫 수동 본문",
        "ops/M2.json": json.dumps(_manual("M2")),
        "ops/M2.md": "되돌린 수동 본문",
    }
    monkeypatch.setattr(persistence, "read_contributions", lambda *_args: rows)
    monkeypatch.setattr(persistence, "read_optional_text_object", lambda key: objects.get(key, ""))

    assert persistence._active_manual_markdown(conn, "S1", "ws") == "첫 수동 본문"


def test_active_manual_markdown_skips_lookup_for_new_page(monkeypatch) -> None:
    conn = Mock()
    conn.execute.return_value.fetchone.return_value = {"markdown_uri": None}
    monkeypatch.setattr(persistence, "read_contributions", Mock(side_effect=AssertionError))

    assert persistence._active_manual_markdown(conn, "S1", "ws") is None


def test_reingest_keeps_manual_source_body(monkeypatch) -> None:
    uploaded: dict[str, str] = {}
    written: dict[str, str] = {}
    embedded: list[str] = []
    manifest = {
        "normalized": {"document": {"title": "문서"}, "semantic_notes": [], "concept_ledger": []},
        "source_page": {"slug": "doc-1", "title": "문서", "markdown": "# AI가 새로 만든 본문\n"},
        "source_blocks": [],
        "concept_pages": [],
        "links": [],
        "user_id": "user-1",
        "workspace_id": "ws",
        "operation_id": "op-2",
    }
    monkeypatch.setattr(persistence, "resolve_or_create_wiki_page_id", lambda *_args: "S1")
    monkeypatch.setattr(persistence, "_active_manual_markdown", lambda *_args: "사람 본문 [링크](https://example.com)\n")
    monkeypatch.setattr(persistence, "_persist_source_blocks", lambda *_args: None)
    monkeypatch.setattr(persistence, "upload_wiki_markdown", lambda markdown, key: uploaded.setdefault(key, markdown))
    monkeypatch.setattr(persistence, "upsert_wiki_page", lambda *_args: None)
    monkeypatch.setattr(persistence, "upsert_document_wiki_link", lambda *_args: None)
    monkeypatch.setattr(persistence, "persist_embedding_units", lambda _conn, _page, _doc, markdown, *_rest: embedded.append(markdown))
    monkeypatch.setattr(persistence, "lock_concept_persistence", lambda *_args: None)
    monkeypatch.setattr(persistence, "load_existing_concept_ids_by_slug", lambda *_args: {})
    monkeypatch.setattr(persistence, "delete_source_related_links", lambda *_args: None)
    monkeypatch.setattr(persistence, "_persist_meaning_cluster_artifacts", lambda *_args: [])
    monkeypatch.setattr(persistence, "write_text_object", lambda key, text, _type: written.setdefault(key, text))

    persistence.persist_wiki_outputs(Mock(), "doc-1", manifest)

    body = "사람 본문 [링크](https://example.com)\n"
    assert list(uploaded.values()) == [body]
    assert embedded == [body]
    assert written["wiki/ws/pages/S1/ops/op-2.md"] == body


@pytest.fixture
def ai_database(monkeypatch):
    dsn = os.environ.get("TEST_AGENT_DATABASE_URL")
    if not dsn:
        pytest.skip("격리 PostgreSQL 테스트 URL이 지정되지 않았습니다.")
    schema = "test_manual_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f"-csearch_path={schema}")

    objects: dict[str, str] = {}
    try:
        with connect() as conn:
            conn.execute(Path(__file__).resolve().parents[3].joinpath("db/ai_schema.sql").read_text())
            for page_id, slug, title in (("C1", "cache", "캐시"), ("C2", "cdn", "CDN"), ("C3", "memcached", "Memcached")):
                conn.execute(
                    "INSERT INTO wiki_pages(id, page_type, title, slug, summary, markdown_uri, user_id, workspace_id, "
                    "status, created_at, updated_at) VALUES (%s, 'concept', %s, %s, '요약', %s, 'user', 'ws', 'active', now(), now())",
                    (page_id, title, slug, f"s3://bucket/{slug}.md"),
                )
            conn.execute(
                "INSERT INTO wiki_page_links(from_page_id, to_page_id, link_type, workspace_id, created_at, updated_at) "
                "VALUES ('C1', 'C2', 'related_to', 'ws', now(), now())"
            )
            conn.execute(
                "INSERT INTO document_wiki_links(document_id, wiki_page_id, relation_type, workspace_id, created_at) "
                "VALUES ('doc-1', 'C1', 'extracted_concept', 'ws', now())"
            )
        objects["s3://bucket/cache.md"] = "# 캐시\n\n## Related Concepts\n- [[cdn|CDN]]\n"
        monkeypatch.setattr(repository, "connect_ai", connect)
        monkeypatch.setattr(repository, "concept_write_lock", lambda *_args: nullcontext())
        monkeypatch.setattr(repository, "invalidate_concept_index", lambda *_args: None)
        monkeypatch.setattr(repository, "storage_uri", lambda key: f"s3://bucket/{key}")
        monkeypatch.setattr(repository, "write_text_object", lambda key, text, _type: objects.__setitem__(f"s3://bucket/{key}", text))
        monkeypatch.setattr(repository, "read_text_object", objects.__getitem__)
        monkeypatch.setattr(repository, "_read_optional_text_object", lambda key: objects.get(key, ""))
        yield connect, objects
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_save_manual_edit_updates_body_links_and_keeps_title(ai_database) -> None:
    connect, objects = ai_database
    markdown = (
        "# 캐싱\n\n## Evidence\n- 사람이 다시 쓴 근거 [doc-1:B0002]\n- 인용 없는 문장\n\n"
        "## Related Concepts\n- [[memcached|Memcached]]\n- [[ghost]]\n"
    )

    result = repository.save_manual_wiki_edit("C1", "user", "ws", "op-m", markdown)

    assert result["markdown_key"] == "wiki/ws/pages/C1/ops/op-m.md"
    assert result["contribution_key"] == "wiki/ws/pages/C1/ops/op-m.json"
    assert objects["s3://bucket/wiki/ws/pages/C1/ops/op-m.md"] == markdown
    artifact = json.loads(objects["s3://bucket/wiki/ws/pages/C1/ops/op-m.json"])
    assert artifact["artifact_type"] == "manual"
    assert artifact["added_links"] == [MEMCACHED]
    assert artifact["removed_links"] == [CDN]
    with connect() as conn:
        page = conn.execute("SELECT title, summary, markdown_uri FROM wiki_pages WHERE id = 'C1'").fetchone()
        links = conn.execute("SELECT to_page_id, link_type FROM wiki_page_links WHERE from_page_id = 'C1'").fetchall()
        units = conn.execute("SELECT source_document_id, block_refs, text FROM wiki_embedding_units WHERE page_id = 'C1'").fetchall()
        sources = conn.execute("SELECT document_id FROM document_wiki_links WHERE wiki_page_id = 'C1'").fetchall()
    assert page == {"title": "캐시", "summary": "요약", "markdown_uri": "s3://bucket/wiki/ws/pages/C1/ops/op-m.md"}
    assert links == [{"to_page_id": "C3", "link_type": "related_to"}]
    # 인용은 본문의 블록 참조로 저장되므로 검색 근거가 고친 본문에서 다시 계산된다.
    assert units == [{"source_document_id": "doc-1", "block_refs": ["doc-1:B0002"], "text": "사람이 다시 쓴 근거"}]
    assert sources == [{"document_id": "doc-1"}]

    # 같은 작업의 재요청은 다시 쓰지 않고 같은 결과를 돌려준다.
    objects["s3://bucket/wiki/ws/pages/C1/ops/op-m.json"] = "재요청이 덮어쓰면 안 된다"
    assert repository.save_manual_wiki_edit("C1", "user", "ws", "op-m", markdown) == result
    assert objects["s3://bucket/wiki/ws/pages/C1/ops/op-m.json"] == "재요청이 덮어쓰면 안 된다"


def test_save_manual_edit_rejects_other_users_page(ai_database) -> None:
    assert repository.save_manual_wiki_edit("C1", "other", "ws", "op-m", "본문") is None


def test_manual_edit_route_starts_embedding_and_returns_404_for_missing_page(monkeypatch) -> None:
    from fastapi import HTTPException

    from app.modules.wiki_ingestion.interfaces.http import routes
    from app.modules.wiki_ingestion.interfaces.http.schemas import WikiPageManualEditIn

    job = Mock()
    monkeypatch.setattr(routes, "get_wiki_embedding_job", lambda: job)
    payload = WikiPageManualEditIn(user_id="user", workspace_id="ws", operation_id="op-m", markdown="본문")
    monkeypatch.setattr(routes.database, "save_manual_wiki_edit", lambda *_args: {"page_id": "C1"})

    assert routes.save_manual_wiki_edit("C1", payload) == {"page_id": "C1"}
    job.start.assert_called_once_with("op-m", ["C1"])

    monkeypatch.setattr(routes.database, "save_manual_wiki_edit", lambda *_args: None)
    with pytest.raises(HTTPException) as error:
        routes.save_manual_wiki_edit("C1", payload)
    assert error.value.status_code == 404
