import asyncio
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
import pytest

from app.core import analytics
from app.modules.skill.domain.entities import Skill, SkillVersion, SkillAuthoringResult


def test_envelope_allowlist_uuid_time_and_identity():
    occurred = datetime(2026, 10, 22, 12, tzinfo=timezone(timedelta(hours=9)))
    payload = analytics.skill_created(skill_id="skill-1", user_id="actor", scope="team",
                                     workspace_id="original-workspace", occurred_at=occurred,
                                     settings=analytics.Settings())
    assert payload["uuid"] == str(uuid5(NAMESPACE_URL, "fruition.analytics.v1:skill_created:skill-1"))
    assert payload["timestamp"] == "2026-10-22T03:00:00+00:00"
    assert payload["distinct_id"] == "actor"
    assert payload["properties"]["$groups"] == {"workspace": "original-workspace"}
    assert set(payload) == {"uuid", "event", "distinct_id", "timestamp", "properties"}
    assert set(payload["properties"]) == {"event_id", "schema_version", "event_source", "environment",
                                          "app_version", "skill_id", "scope", "user_id", "$groups"}
    assert payload["properties"]["event_source"] == "worker"
    with pytest.raises(TypeError):
        analytics.skill_created(question="private")


def test_disabled_defaults_and_lifecycle(monkeypatch):
    for key in ("ANALYTICS_ENABLED", "POSTHOG_PROJECT_TOKEN", "ANALYTICS_ENVIRONMENT"):
        monkeypatch.delenv(key, raising=False)
    assert analytics.Settings.load() == analytics.Settings()
    def forbidden(*args, **kwargs):
        raise AssertionError("Disabled analytics must not connect")
    monkeypatch.setattr(analytics.httpx, "AsyncClient", forbidden)
    analytics.record_skill_created(None, None, "")
    async def check():
        async with analytics.analytics_lifespan():
            pass
    asyncio.run(check())
    monkeypatch.setenv("ANALYTICS_ENABLED", "true")
    asyncio.run(check())  # Blank public token disables relay, not recording.


@pytest.fixture
def db(monkeypatch):
    dsn = os.getenv("ANALYTICS_TEST_DSN")
    if not dsn:
        pytest.skip("ANALYTICS_TEST_DSN required for isolated PostgreSQL integration")
    schema = "analytics_test_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    scoped = make_conninfo(dsn, options=f"-csearch_path={schema}")
    monkeypatch.setenv("AI_DATABASE_URL", scoped)
    monkeypatch.setenv("ANALYTICS_ENABLED", "true")
    monkeypatch.setenv("POSTHOG_PROJECT_TOKEN", "")
    monkeypatch.setenv("ANALYTICS_ENVIRONMENT", "local")
    ddl = (Path(__file__).parents[1] / "db/ai_schema.sql").read_text()
    with psycopg.connect(scoped) as conn:
        conn.execute(ddl)  # Use and reapply the actual production migration.
        conn.execute(ddl)
    try:
        yield scoped
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


def sample(scope="personal"):
    skill = Skill("skill-1", "workspace-1" if scope == "team" else None, scope,
                  "actor" if scope == "personal" else None, "test-skill", "enabled")
    version = SkillVersion("version-1", skill.id, 1, "test-skill", "private description",
                           "private instructions", (), status="published", created_by="actor")
    return skill, version


def counts(dsn):
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return tuple(conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
                     for table in ("skills", "analytics_outbox"))


@pytest.mark.parametrize("scope", ["personal", "team"])
def test_real_creation_and_original_creator_contract(db, monkeypatch, scope):
    from app.modules.skill.infrastructure import postgres_skill_repository as storage
    from app.modules.skill.interfaces.http.schemas import SkillResponse, SkillAuthoringResponse
    monkeypatch.setattr(storage, "_require_manage_scope", lambda *a: None)
    monkeypatch.setattr(storage, "_can_manage_team", lambda *a: True)
    monkeypatch.setattr(storage, "_is_team_member", lambda *a: True)
    repo = storage.PostgresSkillRepository()
    skill, version = sample(scope)
    saved = repo.create_published(skill, version)
    assert counts(db) == (1, 1)
    assert saved.created_by == "actor"
    # Another editor publishes version 2. Creation event and original creator stay unchanged.
    editor = "editor" if scope == "team" else "actor"
    repo.save_published_version(skill, replace(version, id="version-2", created_by=editor))
    assert counts(db) == (1, 1)
    saved = repo.get_accessible(skill.workspace_id, "actor", skill.id)
    assert saved.enabled_version.created_by == editor
    for item in repo.list_accessible(skill.workspace_id, "actor"):
        assert SkillResponse.from_domain(item).created_by == "actor"
    response = SkillAuthoringResponse.from_domain(SkillAuthoringResult("published", skill=saved))
    assert response.created_by == "actor" and response.scope_type == scope
    with psycopg.connect(db, row_factory=dict_row) as conn:
        payload = conn.execute("SELECT payload FROM analytics_outbox").fetchone()["payload"]
        assert payload["distinct_id"] == "actor"
        assert ("$groups" in payload["properties"]) == (scope == "team")
        analytics.record_skill_created(conn, skill, "editor")
    with psycopg.connect(db, row_factory=dict_row) as conn:
        assert conn.execute("SELECT payload FROM analytics_outbox").fetchone()["payload"] == payload


def test_product_rollback_also_rolls_back_outbox(db, monkeypatch):
    from app.modules.skill.infrastructure import postgres_skill_repository as storage
    with psycopg.connect(db, row_factory=dict_row) as conn:
        monkeypatch.setattr(storage.database, "connect_ai", lambda: nullcontext(conn))
        repo = storage.PostgresSkillRepository()
        with pytest.raises(RuntimeError):
            with conn.transaction():
                repo.create_published(*sample())
                assert conn.execute("SELECT count(*) AS n FROM analytics_outbox").fetchone()["n"] == 1
                raise RuntimeError("product failure after analytics")
    assert counts(db) == (0, 0)


def test_savepoint_database_error_does_not_abort_creation(db, caplog):
    from app.modules.skill.infrastructure.postgres_skill_repository import PostgresSkillRepository
    with psycopg.connect(db) as conn:
        conn.execute("ALTER TABLE analytics_outbox ADD CONSTRAINT force_failure CHECK (false)")
    saved = PostgresSkillRepository().create_published(*sample())
    assert saved.id == "skill-1"
    assert counts(db) == (1, 0)
    assert "analytics_outbox_insert_failed" in caplog.text
    assert "private instructions" not in caplog.text


def test_retry_timeout_dead_state_and_lifecycle(db, monkeypatch, caplog):
    from app.modules.skill.infrastructure.postgres_skill_repository import PostgresSkillRepository
    PostgresSkillRepository().create_published(*sample())
    attempts = []
    def handler(request):
        import json
        attempts.append(json.loads(request.content)["batch"][0])
        if len(attempts) == 1:
            raise httpx.ReadTimeout("private error must not be logged")
        if len(attempts) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"status": 1})
    settings = analytics.Settings(True, "test-public-token")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await analytics.relay_once(settings, client)
            assert not await analytics.relay_once(settings, client)  # bounded backoff
            with psycopg.connect(db) as conn:
                conn.execute("UPDATE analytics_outbox SET next_attempt_at = now(), attempts = 7")
            assert await analytics.relay_once(settings, client)
            assert not await analytics.relay_once(settings, client)  # dead rows are observable, not retried
            with psycopg.connect(db, row_factory=dict_row) as conn:
                assert conn.execute("SELECT state FROM analytics_outbox").fetchone()["state"] == "dead"
                conn.execute("UPDATE analytics_outbox SET state = 'pending', next_attempt_at = now()")
            assert await analytics.relay_once(settings, client)
            assert not await analytics.relay_once(settings, client)
    asyncio.run(run())
    assert len(attempts) == 3 and attempts[0] == attempts[1] == attempts[2]
    assert "state=dead" in caplog.text and "private error" not in caplog.text
    # Real lifecycle with mock HTTP; exit cancels polling and closes its client.
    with psycopg.connect(db) as conn:
        conn.execute("UPDATE analytics_outbox SET state = 'pending', next_attempt_at = now()")
    monkeypatch.setenv("POSTHOG_PROJECT_TOKEN", "test-public-token")
    real_client = httpx.AsyncClient
    clients = []
    def factory():
        client = real_client(transport=httpx.MockTransport(handler))
        clients.append(client)
        return client
    monkeypatch.setattr(analytics.httpx, "AsyncClient", factory)
    async def lifecycle():
        async with analytics.analytics_lifespan():
            for _ in range(100):
                if len(attempts) == 4:
                    break
                await asyncio.sleep(.01)
            assert len(attempts) == 4
        assert not any(t.get_name() == "analytics-relay" for t in asyncio.all_tasks())
    asyncio.run(lifecycle())
    assert clients[0].is_closed


def test_uncommitted_rows_are_not_relayed_and_cancelled_send_retries(db):
    skill, _ = sample()
    payloads = []
    def cancel_after_send(request):
        import json
        payloads.append(json.loads(request.content)["batch"][0])
        raise asyncio.CancelledError()
    def success(request):
        import json
        payloads.append(json.loads(request.content)["batch"][0])
        return httpx.Response(200)
    settings = analytics.Settings(True, "test-public-token")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(success)) as client:
            with psycopg.connect(db) as conn:
                conn.execute("SELECT 1")  # Start product transaction before nested savepoint.
                analytics.record_skill_created(conn, skill, "actor")
                assert not await analytics.relay_once(settings, client)
            assert not payloads
        async with httpx.AsyncClient(transport=httpx.MockTransport(cancel_after_send)) as client:
            with pytest.raises(asyncio.CancelledError):
                await analytics.relay_once(settings, client)
        async with httpx.AsyncClient(transport=httpx.MockTransport(success)) as client:
            assert await analytics.relay_once(settings, client)
    asyncio.run(run())
    assert len(payloads) == 2 and payloads[0] == payloads[1]


def test_api_lifespan_enters_and_exits_relay(monkeypatch):
    import api
    from contextlib import asynccontextmanager
    calls = []
    monkeypatch.setattr(api.database, "ensure_ai_schema", lambda: None)
    monkeypatch.setattr(api, "AGENT_SKILLS_ENABLED", False)
    @asynccontextmanager
    async def relay():
        calls.append("start")
        try:
            yield
        finally:
            calls.append("stop")
    monkeypatch.setattr(api, "analytics_lifespan", relay)
    async def run():
        async with api.lifespan(api.app):
            assert calls == ["start"]
    asyncio.run(run())
    assert calls == ["start", "stop"]
