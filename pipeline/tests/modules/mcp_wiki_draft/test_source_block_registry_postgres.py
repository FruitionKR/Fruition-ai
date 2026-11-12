"""MCP 원문 블록은 웹 편입과 같은 분할·같은 영구 블록 ID(source_blocks 레지스트리)를 쓴다."""
from hashlib import sha256
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

import api
import test_mcp_wiki_draft as h
from test_mcp_wiki_draft import isolated_state  # noqa: F401
from run_lab import _source_block_records
from app.modules.mcp_wiki_draft.application import McpWikiDraftError, load_intake_limits
from app.modules.mcp_wiki_draft.infrastructure.backend_source_reader import _validate
from app.modules.mcp_wiki_draft.source_block_registry import resolve_source_blocks
from app.modules.wiki_generation.infrastructure.extract import MarkdownBlockExtractor
from app.modules.wiki_ingestion.domain.source_block_changes import compare_source_blocks
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as wiki_repository
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_output_persistence as output_persistence


DOCUMENT_ID = "doc_1"
WEB_V1 = (
    "# Title\n\nAlpha concept has evidence.\n\nBeta concept original.\n\n"
    "- Gamma item one\n- Gamma item two\n\nDelta removed."
)
# 이동(Alpha·목록), 공백만 바뀐 블록(목록), 수정(Beta, Delta 자리의 코드 블록), 추가(Intro).
WEB_V2 = (
    "# Title\n\nIntro  paragraph\nspans two lines.\n\nAlpha concept has evidence.\n\nBeta concept has support.\n\n"
    "- Gamma item one\n-   Gamma item two\n\n```\ncode\n\nblock\n```"
)


def _web_commit(document_id: str, markdown: str) -> list[str]:
    """웹 편입의 블록 단계를 그대로 실행해 레지스트리에 반영한다(run_lab 재편입 비교 + `_persist_source_blocks`)."""
    existing = wiki_repository.list_source_blocks(document_id)
    artifact = wiki_repository.latest_source_extraction_artifact(document_id) or {}
    _document, blocks = MarkdownBlockExtractor().extract_text(markdown, source_path="doc.md")
    for block in blocks:
        block.document_id = document_id
    changes = compare_source_blocks(existing, blocks, int(artifact.get("max_block_number", 0) or 0))
    records = _source_block_records(changes.blocks, with_line_ranges=True)
    with h._connect() as conn:
        output_persistence._persist_source_blocks(conn, document_id, {
            "source_blocks": records, "source_content_hash": sha256(markdown.encode()).hexdigest(),
        })
        conn.execute(
            "INSERT INTO pipeline_runs(id,document_id,workspace_id,input_source,output_dir,mode,status,manifest,finished_at) "
            "VALUES (%s,%s,'workspace-1','test','out','test','succeeded',%s,now())",
            (str(uuid4()), document_id, Jsonb({"source_extraction_artifact": {"max_block_number": changes.max_block_number}})),
        )
    return [block.block_id for block in changes.blocks]


def _registry(document_id: str = DOCUMENT_ID) -> list[dict]:
    with h._connect() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT block_id,text,position,line_start,line_end,block_type FROM source_blocks "
            "WHERE document_id=%s ORDER BY position", (document_id,),
        ).fetchall()]


def _resolve(client, markdown: str, **changes):
    body = {"schema_version": "mcp.source-blocks.v1", "workspace_id": "workspace-1",
            "document_id": DOCUMENT_ID, "markdown": markdown, **changes}
    return client.post(
        "/internal/mcp/source-blocks/resolve", json=body, headers={"X-Agent-Service-Token": "agent-test-token"},
    )


def test_resolve_assigns_exactly_the_ids_a_web_ingest_commit_assigns():
    _web_commit(DOCUMENT_ID, WEB_V1)
    with TestClient(api.app) as client:
        first = _resolve(client, WEB_V2)
        again = _resolve(client, WEB_V2)
    assert first.status_code == 200 and first.json() == again.json()
    body = first.json()
    assert body["schema_version"] == "mcp.source-blocks.v1" and body["document_id"] == DOCUMENT_ID
    resolved = body["blocks"]
    # 아무것도 저장하지 않는다.
    assert [row["block_id"] for row in _registry()] == ["B0001", "B0002", "B0003", "B0004", "B0005"]

    web_ids = _web_commit(DOCUMENT_ID, WEB_V2)
    assert [item["block_id"] for item in resolved] == web_ids
    # 제목·Alpha·목록(공백만 다름)은 기존 ID, 수정 블록(Beta·코드)도 기존 ID, 새 블록은 이전 최대 번호 다음이다.
    assert web_ids == ["B0001", "B0006", "B0002", "B0003", "B0004", "B0005"]
    lines = WEB_V2.split("\n")
    for ordinal, (item, row) in enumerate(zip(resolved, _registry(), strict=True), start=1):
        assert item["ordinal"] == ordinal == row["position"]
        assert (item["block_id"], item["text"], item["line_start"], item["line_end"], item["block_type"]) == (
            row["block_id"], row["text"], row["line_start"], row["line_end"], row["block_type"],
        )
        assert item["raw_text"] == "\n".join(lines[item["line_start"] - 1:item["line_end"]]).strip("\n")
    assert resolved[1]["raw_text"] == "Intro  paragraph\nspans two lines."
    assert resolved[1]["text"] == "Intro paragraph spans two lines."
    assert resolved[5]["block_type"] == "code" and resolved[5]["raw_text"] == "```\ncode\n\nblock\n```"


def test_resolve_without_registry_numbers_blocks_from_one_and_matches_first_web_ingest():
    with h._connect() as conn:
        resolved = resolve_source_blocks(conn, DOCUMENT_ID, WEB_V1)
    assert [item["block_id"] for item in resolved] == _web_commit(DOCUMENT_ID, WEB_V1)
    assert [item["block_id"] for item in resolved] == ["B0001", "B0002", "B0003", "B0004", "B0005"]


def test_resolve_rejects_unknown_fields_oversized_markdown_and_missing_token():
    with TestClient(api.app) as client:
        assert _resolve(client, "a", extra=True).status_code == 400
        assert _resolve(client, "a", document_id="not-a-document").status_code == 400
        limit = load_intake_limits().source_text_bytes
        oversized = _resolve(client, "x" * (limit + 1))
        assert oversized.status_code == 413 and oversized.json()["error"]["code"] == "SOURCE_LIMIT_EXCEEDED"
        assert client.post("/internal/mcp/source-blocks/resolve", json={}).status_code == 401


class RegistrySource(h.Source):
    """document가 resolve 결과로 만드는 원문 응답: 영구 ID, 원문 조각(text), 줄 범위·블록 종류."""

    def __init__(self, markdown: str):
        super().__init__()
        self.markdown = markdown

    def read(self, actor, document_id, _limits):
        self.calls += 1
        with h._connect() as conn:
            blocks = resolve_source_blocks(conn, document_id, self.markdown)
        records = [
            {
                "block_id": item["block_id"], "ordinal": item["ordinal"], "text": item["raw_text"],
                "content_hash": "sha256:" + sha256(item["raw_text"].encode()).hexdigest(),
                "line_start": item["line_start"], "line_end": item["line_end"], "block_type": item["block_type"],
            }
            for item in blocks
        ]
        return {
            "schema_version": "mcp.wiki-source.v1", "document_id": document_id, "display_name": "Source document",
            "workspace_id": actor.workspace_id, "source_version": 7,
            "source_content_hash": "sha256:" + sha256(self.markdown.encode()).hexdigest(),
            "mime_type": "text/markdown", "blocks": records,
        }


MCP_V1 = "Alpha concept has evidence.\n\nBeta concept original.\n\nGamma concept stays.\n\nDelta removed."
MCP_V2 = "New concept intro.\n\nAlpha concept has evidence.\n\nBeta concept has support.\n\nGamma concept stays."


def _submit_all(intake, grant: str, started: dict) -> None:
    actor = h.McpWikiActorRequest.model_validate(h._actor("get_window", grant))
    ordinal = 1
    while True:
        window = intake.read_window(started["run_id"], actor, ordinal)
        block = window["blocks"][0]
        concept = block["text"].split()[0]
        intake.submit(started["run_id"], window["window_id"], h._submission(
            grant, started, window, key=f"submit-{ordinal}", concept=concept,
            text=block["text"], quote=block["text"], block_id=block["block_id"],
        ))
        if not window["has_more"]:
            return
        ordinal += 1


def _start_and_finalize(monkeypatch, source, *, before_finalize=None):
    monkeypatch.setattr(h.calculation, "read_contributions", lambda *_args: [])
    grant = str(uuid4())
    intake = h._intake(source)
    started = intake.start(h._start_payload(grant))
    _submit_all(intake, grant, started)
    if before_finalize is not None:
        before_finalize()
    return grant, started, intake.finalize(started["run_id"], h._finalize_payload(grant, started))


def test_mcp_publish_keeps_registry_ids_and_invalidates_only_modified_and_deleted_blocks(monkeypatch):
    assert _web_commit(DOCUMENT_ID, MCP_V1) == ["B0001", "B0002", "B0003", "B0004"]
    grant, started, finalized = _start_and_finalize(monkeypatch, RegistrySource(MCP_V2))
    with h._connect() as conn:
        snapshot = [dict(row) for row in conn.execute(
            "SELECT block_id,ordinal,text,line_start,line_end,block_type FROM mcp_wiki_source_blocks "
            "WHERE snapshot_id=%s ORDER BY ordinal", (started["source_snapshot"]["snapshot_id"],),
        ).fetchall()]
    assert [item["block_id"] for item in snapshot] == ["B0005", "B0001", "B0002", "B0003"]
    assert [(item["line_start"], item["line_end"], item["block_type"]) for item in snapshot] == [
        (1, 1, "paragraph"), (3, 3, "paragraph"), (5, 5, "paragraph"), (7, 7, "paragraph"),
    ]
    with h._connect() as conn:
        changes = conn.execute(
            "SELECT changes FROM mcp_wiki_drafts WHERE draft_id=%s", (finalized["draft_id"],),
        ).fetchone()["changes"]
    invalidations = {item["subject_id"] for item in changes if item["kind"] == "EVIDENCE_INVALIDATION"}
    assert invalidations == {"B0002", "B0004"}

    result = _apply(grant, finalized)
    assert result["state"] == "PUBLISHED"
    assert [(row["block_id"], row["position"], row["line_start"], row["block_type"]) for row in _registry()] == [
        ("B0005", 1, 1, "paragraph"), ("B0001", 2, 3, "paragraph"),
        ("B0002", 3, 5, "paragraph"), ("B0003", 4, 7, "paragraph"),
    ]
    # 다음 웹 재편입도 같은 내용이면 같은 ID를 유지한다.
    assert _web_commit(DOCUMENT_ID, MCP_V2) == ["B0005", "B0001", "B0002", "B0003"]


def _apply(grant, finalized):
    payload = h._apply_payload(grant, finalized)
    result = h.McpWikiDraftApply(h.PostgresMcpWikiApplyRepository(), h._ValidApprovalAuthority()).apply(
        payload.draft_id, payload
    )
    return result


def test_finalize_conflicts_when_registry_changed_after_start(monkeypatch):
    _web_commit(DOCUMENT_ID, MCP_V1)
    with pytest.raises(McpWikiDraftError) as error:
        _start_and_finalize(
            monkeypatch, RegistrySource(MCP_V2),
            before_finalize=lambda: _web_commit(DOCUMENT_ID, "Alpha concept has evidence.\n\nBeta concept has support."),
        )
    assert error.value.status_code == 409 and error.value.code == "SOURCE_CHANGED"


def test_apply_conflicts_when_registry_changed_after_finalize(monkeypatch):
    _web_commit(DOCUMENT_ID, MCP_V1)
    grant, _started, finalized = _start_and_finalize(monkeypatch, RegistrySource(MCP_V2))
    _web_commit(DOCUMENT_ID, "Alpha concept has evidence.\n\nBeta concept has support.")
    registry_before = _registry()
    result = _apply(grant, finalized)
    assert result["state"] == "CONFLICTED" and result["error_code"] == "SOURCE_CHANGED"
    assert _registry() == registry_before


def _source_response(blocks):
    return {
        "schema_version": "mcp.wiki-source.v1", "document_id": DOCUMENT_ID, "display_name": "Doc",
        "workspace_id": "workspace-1", "source_version": 1, "source_content_hash": "sha256:" + "0" * 64,
        "mime_type": "text/markdown", "blocks": blocks,
    }


def _block(**changes):
    text = "Alpha"
    return {"block_id": "B0007", "ordinal": 1, "text": text,
            "content_hash": "sha256:" + sha256(text.encode()).hexdigest(), **changes}


@pytest.mark.parametrize("location, accepted", [
    ({}, True),
    ({"line_start": 3, "line_end": 4, "block_type": "list"}, True),
    ({"line_start": None, "line_end": None, "block_type": None}, True),
    ({"line_start": 4, "line_end": 3, "block_type": "list"}, False),
    ({"line_start": 0, "line_end": 3, "block_type": "list"}, False),
    ({"line_start": 1, "line_end": 1, "block_type": "table"}, False),
    ({"line_start": 1, "line_end": 1}, False),
])
def test_source_callback_accepts_registry_locations(location, accepted):
    actor = h.McpWikiActorRequest.model_validate(h._actor("start_ingest", str(uuid4())))
    value = _source_response([_block(**location)])
    if accepted:
        assert _validate(value, actor, DOCUMENT_ID, load_intake_limits())["blocks"][0]["block_id"] == "B0007"
    else:
        with pytest.raises(McpWikiDraftError):
            _validate(value, actor, DOCUMENT_ID, load_intake_limits())
