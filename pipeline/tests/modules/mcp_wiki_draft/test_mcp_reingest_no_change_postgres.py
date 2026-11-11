"""같은 내용 재편입은 시작에서 거절(ALREADY_INGESTED)하고, 고친 문서는 다시 편입한다.
고친 문서라도 공개 상태가 그대로면 승인 적용은 아무것도 쓰지 않는 NO_CHANGE(changed=false, 무료)다."""
from hashlib import sha256
from uuid import uuid4

import pytest

import test_mcp_wiki_draft as h
from test_mcp_wiki_draft import isolated_state  # noqa: F401
from app.modules.wiki_ingestion.infrastructure.object_storage import client as storage_client


class _EmbeddingJob:
    def __init__(self):
        self.calls = []

    def start(self, run_id, page_ids):
        self.calls.append((run_id, list(page_ids)))


class _EditedSource(h.Source):
    """블록 텍스트는 같고 문서 본문 해시만 다른 편집(예: 블록 사이 공백)."""

    def read(self, actor, document_id, limits):
        value = super().read(actor, document_id, limits)
        value["source_content_hash"] = "sha256:" + sha256(b"edited-" + value["source_content_hash"].encode()).hexdigest()
        return value


def _acknowledged_ledger(*_args):
    """document-svc가 앞선 발행의 대기 기여를 원장에 올린 상태를 흉내 낸다."""
    with h._connect() as conn:
        rows = conn.execute(
            "SELECT page_id,operation_id,document_id,object_key,linked_by_operation_id "
            "FROM wiki_pending_contributions WHERE superseded_at IS NULL ORDER BY created_at"
        ).fetchall()
    return [
        {
            "page_id": row["page_id"], "ingest_operation_id": row["operation_id"],
            "source_document_id": row["document_id"], "sequence_revision": index, "active": True,
            "object_key": row["object_key"], "lint_operation_id": row["linked_by_operation_id"],
        }
        for index, row in enumerate(rows, start=1)
    ]


def _apply(grant, finalized):
    payload = h._apply_payload(grant, finalized)
    job = _EmbeddingJob()
    result = h.McpWikiDraftApply(h.PostgresMcpWikiApplyRepository(job), h._ValidApprovalAuthority()).apply(
        payload.draft_id, payload
    )
    return result, job, payload


def _ingest(grant, start_key, source=None):
    intake = h._intake(source)
    started = intake.start(h._start_payload(grant, key=start_key))
    actor = h.McpWikiActorRequest.model_validate(h._actor("get_window", grant))
    for ordinal, concept, text in ((1, "Alpha", "Alpha concept has evidence."), (2, "Beta", "Beta concept has support.")):
        window = intake.read_window(started["run_id"], actor, ordinal)
        key = f"{start_key}-submit-{ordinal}"
        submission = h._submission(
            grant, started, window, key=key, concept=concept, text=text, quote=text, block_id=f"B000{ordinal}",
        )
        intake.submit(started["run_id"], window["window_id"], submission)
    finalized = intake.finalize(started["run_id"], h._finalize_payload(grant, started, key=f"finalize-{start_key}"))
    return _apply(grant, finalized)


def _public_snapshot():
    with h._connect() as conn:
        pages = [dict(row) for row in conn.execute(
            "SELECT id,markdown_uri,updated_at,status FROM wiki_pages ORDER BY id"
        ).fetchall()]
        counts = {
            table: conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
            for table in ("wiki_pending_contributions", "wiki_embedding_units", "document_wiki_links",
                          "wiki_page_links", "source_blocks", "wiki_publication_objects")
        }
    objects = sorted(item.object_name for item in storage_client().list_objects("fruition-storage", recursive=True)
                     if item.object_name.startswith("wiki/workspace-1/"))
    return pages, counts, objects


def test_mcp_start_refuses_content_already_ingested_by_mcp(monkeypatch):
    monkeypatch.setattr(h.calculation, "read_contributions", _acknowledged_ledger)
    grant = str(uuid4())
    first, _job, _payload = _ingest(grant, "start-1")
    assert first["state"] == "PUBLISHED"
    with pytest.raises(h.McpWikiDraftError) as refused:
        h._intake().start(h._start_payload(grant, key="start-2"))
    assert (refused.value.status_code, refused.value.code) == (409, "ALREADY_INGESTED")
    with h._connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM mcp_wiki_ingest_runs").fetchone()["n"] == 1


def test_mcp_start_refuses_content_already_ingested_by_web():
    source = h.Source()
    body = "".join(text for _block, text in source.blocks)
    with h._connect() as conn:
        # 웹 편입이 남긴 마지막 성공 상태: 소스 페이지 연결, 블록, 입력 Markdown 해시 스냅샷.
        conn.execute(
            "INSERT INTO wiki_pages(id,page_type,title,slug,markdown_uri,user_id,workspace_id,status,created_at,updated_at) "
            "VALUES ('web-source','source','Source document','doc-1','s3://fruition-storage/x.md','user-1','workspace-1','active',now(),now())"
        )
        conn.execute(
            "INSERT INTO document_wiki_links(document_id,wiki_page_id,relation_type,workspace_id,created_at) "
            "VALUES ('doc_1','web-source','source_of','workspace-1',now())"
        )
        conn.execute("INSERT INTO source_blocks(document_id,block_id,text,position) VALUES ('doc_1','B0001','x',1)")
        conn.execute(
            "INSERT INTO source_block_snapshots(document_id,source_content_hash) VALUES ('doc_1',%s)",
            (sha256(body.encode()).hexdigest(),),
        )
    with pytest.raises(h.McpWikiDraftError) as refused:
        h._intake(source).start(h._start_payload(str(uuid4())))
    assert (refused.value.status_code, refused.value.code) == (409, "ALREADY_INGESTED")


def test_edited_document_can_be_ingested_again(monkeypatch):
    monkeypatch.setattr(h.calculation, "read_contributions", _acknowledged_ledger)
    grant = str(uuid4())
    first, _job, _payload = _ingest(grant, "start-1")
    assert first["state"] == "PUBLISHED"
    edited = h.Source([("B0001", "Alpha concept has evidence."), ("B0002", "Beta concept has new support.")])
    intake = h._intake(edited)
    started = intake.start(h._start_payload(grant, key="start-2"))
    actor = h.McpWikiActorRequest.model_validate(h._actor("get_window", grant))
    window = intake.read_window(started["run_id"], actor, 1)
    intake.submit(started["run_id"], window["window_id"], h._submission(grant, started, window, key="s2-1"))
    window = intake.read_window(started["run_id"], actor, 2)
    intake.submit(started["run_id"], window["window_id"], h._submission(
        grant, started, window, key="s2-2", concept="Beta", text="Beta concept has new support.",
        quote="Beta concept has new support.", block_id="B0002",
    ))
    finalized = intake.finalize(started["run_id"], h._finalize_payload(grant, started, key="finalize-start-2"))
    second, job, _payload = _apply(grant, finalized)
    assert second["state"] == "PUBLISHED" and second["changed"] is True
    assert job.calls


def test_edit_without_public_change_applies_as_free_no_change(monkeypatch):
    monkeypatch.setattr(h.calculation, "read_contributions", _acknowledged_ledger)
    grant = str(uuid4())
    first, _job, _payload = _ingest(grant, "start-1")
    assert first["state"] == "PUBLISHED"
    before = _public_snapshot()
    # 웹 편입은 같은 문장도 작업 id가 붙은 근거로 다시 덧붙여, 실제 재편입에서 공개 상태가 그대로인 경우는 드물다.
    # 반영 전·후 공개 상태가 같다고 보이게 고정해, 그때의 승인 적용 경로(NO_CHANGE)를 확인한다.
    with h._connect() as conn:
        frozen = h.calculation._public_rows(conn, "workspace-1", "doc_1")
    monkeypatch.setattr(h.calculation, "_public_rows", lambda *_args: frozen)

    second, job, payload = _ingest(grant, "start-2", _EditedSource())

    assert second["state"] == "NO_CHANGE"
    assert second["changed"] is False
    assert second["error_code"] is None and second["published_reference_hash"]
    assert job.calls == []
    assert _public_snapshot() == before, "no page version, object, link, block or contribution"
    with h._connect() as conn:
        event = conn.execute(
            "SELECT payload FROM mcp_wiki_publication_outbox WHERE approval_id=%s", (payload.approval_id,)
        ).fetchone()["payload"]
        publication = conn.execute(
            "SELECT state,outcome FROM wiki_publications WHERE operation_id=%s", (payload.operation_id,)
        ).fetchone()
    # Document는 이 영수증으로 정산한다. changed=false면 무료다.
    assert event["outcome"] == "NO_CHANGE" and event["changed"] is False
    assert dict(publication) == {"state": "COMMITTED", "outcome": "NO_CHANGE"}
