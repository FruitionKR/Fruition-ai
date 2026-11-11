"""Real PostgreSQL regressions for immutable draft intake and admission."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import re
from threading import Event
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
import pytest

from app.modules.mcp_wiki_draft import application as app
from app.modules.mcp_wiki_draft.infrastructure import postgres_repository as db
from app.modules.mcp_wiki_draft.interfaces.http.schemas import McpWikiActorRequest, StartIngestRequest, SubmitWindowRequest

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_AGENT_DATABASE_URL"), reason="requires isolated PostgreSQL")


@pytest.fixture
def intake(monkeypatch):
    url = os.environ["TEST_AGENT_DATABASE_URL"]
    schema = "draft_review_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    scoped = make_conninfo(url, options=f"-csearch_path={schema}")
    def connect():
        return psycopg.connect(scoped, row_factory=dict_row)
    try:
        with connect() as conn:
            conn.execute((Path(__file__).parents[3] / "db/ai_schema.sql").read_text())
        monkeypatch.setattr(db, "connect", connect)
        limits = app.IntakeLimits(10000, 10, 5000, 1, 1000, 5, 5, 10000, 3600)
        monkeypatch.setattr(app, "load_intake_limits", lambda: limits)
        policy = app.SubmissionPolicy(10, 10, 10, 10, 100, 100, 100, 0.5, (re.compile("NEVER_MATCH"),), "review-v1", "review-eval")
        monkeypatch.setattr(app, "load_submission_policy", lambda: policy)
        source = {"source_version": 1, "source_content_hash": "sha256:" + "a" * 64, "display_name": "Review Doc", "blocks": [
            {"block_id": f"B{i}", "ordinal": i, "text": text, "content_hash": "sha256:" + hashlib.sha256(text.encode()).hexdigest()}
            for i, text in enumerate(("alpha", "beta"), 1)
        ]}
        class Reader:
            def read(self, *args):
                return source
        actor = McpWikiActorRequest(invocation_id=str(uuid4()), grant_id=str(uuid4()), user_id="user", workspace_id="workspace", client_id="https://client.example", policy_generation=1, origin="MCP_CLIENT", spec_version="2025-11-25", tool_name="start_ingest")
        repo = db.PostgresMcpWikiDraftRepository()
        yield app.McpWikiDraftIntake(repo, Reader()), repo, actor, connect, source, limits
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def start_payload(actor, document="doc_review"):
    command = {"schema_version": "mcp.wiki-draft.v1", "actor": app.request_actor(actor), "document_id": document}
    return StartIngestRequest(**{**command, "actor": actor}, idempotency_key=document, request_hash=app.canonical_hash(command))


def submission(repo, actor, run_id, window, merge_target=None):
    actor = actor.model_copy(update={"tool_name": "submit_window"})
    context = repo.resume(run_id, actor, window_id=window["window_id"])
    block = window["blocks"][0]
    contributions = [{"concept_name": "Concept", "statements": [{"statement_id": "s1", "text": block["text"], "evidence": [{"snapshot_id": window["snapshot_id"], "block_id": block["block_id"], "quote": block["text"]}]}], "links": []}]
    if merge_target is not None:
        contributions[0]["merge_target_page_id"] = merge_target
    command = {"schema_version": "mcp.wiki-draft.v1", "actor": app.request_actor(actor), "run_id": run_id, "window_id": window["window_id"], "snapshot_id": window["snapshot_id"], "window_hash": window["window_hash"], "source_snapshots": [{key: context[key] for key in ("snapshot_id", "document_id", "source_version", "source_content_hash")}], "contributions": contributions}
    return SubmitWindowRequest(schema_version=command["schema_version"], actor=actor, idempotency_key=window["window_id"], request_hash=app.canonical_hash(command), snapshot_id=window["snapshot_id"], window_hash=window["window_hash"], contributions=contributions)


@pytest.mark.parametrize("workspace,status,page_type,accepted", [
    ("workspace", "active", "concept", True),
    ("other", "active", "concept", False),
    ("workspace", "deleted", "concept", False),
    ("workspace", "active", "source", False),
])
def test_semantic_merge_target_scope_and_submission_hash(intake, workspace, status, page_type, accepted):
    service, repo, actor, connect, _, _ = intake
    with connect() as conn:
        conn.execute(
            "INSERT INTO wiki_pages(id,page_type,title,slug,user_id,workspace_id,status,created_at,updated_at) VALUES ('target',%s,'Different name','target','user',%s,%s,now(),now())",
            (page_type, workspace, status),
        )
    run = service.start(start_payload(actor))["run_id"]
    window = service.read_window(run, actor.model_copy(update={"tool_name": "get_window"}), 1)
    assert window["concept_resolution"]["mode"] == "CLIENT_LLM_SEMANTIC"
    payload = submission(repo, actor, run, window, merge_target="target")
    legacy = submission(repo, actor, run, window)
    assert payload.request_hash != legacy.request_hash
    if not accepted:
        with pytest.raises(app.McpWikiDraftError) as error:
            service.submit(run, window["window_id"], payload)
        assert error.value.code == "MERGE_TARGET_CHANGED"
        with connect() as conn:
            assert conn.execute("SELECT count(*) AS n FROM mcp_wiki_window_submissions").fetchone()["n"] == 0
        return
    result = service.submit(run, window["window_id"], payload)
    assert service.submit(run, window["window_id"], payload) == result
    with connect() as conn:
        stored = conn.execute("SELECT contributions FROM mcp_wiki_window_submissions").fetchone()["contributions"]
    assert stored[0]["merge_target_page_id"] == "target"
    with pytest.raises(app.McpWikiDraftError) as error:
        service.submit(run, window["window_id"], legacy)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_submit_retry_revalidates_original_window_before_and_after_completion(intake):
    service, repo, actor, connect, _, _ = intake
    run = service.start(start_payload(actor))["run_id"]
    calls = []
    for ordinal in (1, 2):
        window = service.read_window(run, actor.model_copy(update={"tool_name": "get_window"}), ordinal)
        payload = submission(repo, actor, run, window)
        result = service.submit(run, window["window_id"], payload)
        calls.append((window["window_id"], payload, result))
        for window_id, previous, expected in calls:
            assert service.submit(run, window_id, previous) == expected
    assert result["state"] == "READY_TO_FINALIZE"
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM mcp_wiki_source_blocks").fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM mcp_wiki_window_submissions").fetchone()["n"] == 2


def test_replay_still_rejects_changed_actor_and_expired_run(intake):
    service, repo, actor, connect, _, _ = intake
    start = start_payload(actor)
    run = service.start(start)["run_id"]
    window = service.read_window(run, actor.model_copy(update={"tool_name": "get_window"}), 1)
    payload = submission(repo, actor, run, window)
    service.submit(run, window["window_id"], payload)
    for field, value in (("user_id", "other"), ("policy_generation", 2)):
        changed = payload.model_copy(update={"actor": payload.actor.model_copy(update={field: value})})
        with pytest.raises(app.McpWikiDraftError) as exc:
            service.submit(run, window["window_id"], changed)
        assert exc.value.status_code in (403, 404)
    with connect() as conn:
        conn.execute("UPDATE mcp_wiki_ingest_runs SET expires_at=now()-interval '1 second' WHERE run_id=%s", (run,))
    for action in (lambda: service.start(start), lambda: service.submit(run, window["window_id"], payload), lambda: repo.resume(run, actor)):
        with pytest.raises(app.McpWikiDraftError) as exc:
            action()
        assert exc.value.status_code == 410


def test_concurrent_documents_cannot_overbook_workspace_quota(intake, monkeypatch):
    _, repo, actor, connect, source, limits = intake
    limits = replace(limits, active_per_workspace=1)
    counted, release, second_started = Event(), Event(), Event()
    class Connection:
        def __init__(self):
            self.conn = connect()
        def __enter__(self):
            self.conn.__enter__()
            return self
        def __exit__(self, *args):
            return self.conn.__exit__(*args)
        def cursor(self):
            return self.conn.cursor()
        def execute(self, query, params=None):
            result = self.conn.execute(query, params)
            if "SELECT count(*) FILTER" in query and not counted.is_set():
                counted.set()
                assert release.wait(5), "test did not release quota admission"
            return result
    monkeypatch.setattr(db, "connect", Connection)
    def start(document, second=False):
        if second:
            second_started.set()
        payload = start_payload(actor, document)
        return repo.start(payload, source, limits, payload.request_hash)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(start, "doc_first")
        try:
            assert counted.wait(5)
            second = pool.submit(start, "doc_second", True)
            assert second_started.wait(5)
            with pytest.raises(TimeoutError):
                second.result(timeout=0.2)
        finally:
            release.set()
        first.result(timeout=5)
        with pytest.raises(app.McpWikiDraftError) as exc:
            second.result(timeout=5)
        assert exc.value.code == "ACTIVE_LIMIT_EXCEEDED"
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM mcp_wiki_ingest_runs").fetchone()["n"] == 1
