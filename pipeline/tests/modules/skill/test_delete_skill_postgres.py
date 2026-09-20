"""SKILL_TEST_DSN이 있으면 임시 스키마에서 삭제·권한·FK를 검증하고 전부 롤백한다."""
import os
from contextlib import nullcontext
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
import pytest

from app.modules.skill.infrastructure import postgres_skill_repository as storage


@pytest.mark.skipif(not os.environ.get("SKILL_TEST_DSN"), reason="SKILL_TEST_DSN 미설정")
def test_delete_preserves_runs_and_enforces_personal_and_team_scope(monkeypatch):
    with psycopg.connect(os.environ["SKILL_TEST_DSN"], row_factory=dict_row) as conn:
        try:
            schema = "skill_delete_test_" + uuid4().hex
            conn.execute(f'CREATE SCHEMA "{schema}"')
            conn.execute(f'SET LOCAL search_path TO "{schema}"')
            # 운영 migration의 삭제 관련 FK 계약만 재현한다.
            conn.execute("CREATE TABLE skills (id text PRIMARY KEY, scope_type text, owner_user_id text, workspace_id text, enabled_version_id text)")
            conn.execute("CREATE TABLE skill_versions (id text PRIMARY KEY, skill_id text REFERENCES skills(id) ON DELETE CASCADE)")
            conn.execute("ALTER TABLE skills ADD FOREIGN KEY (enabled_version_id) REFERENCES skill_versions(id) DEFERRABLE INITIALLY DEFERRED")
            conn.execute("CREATE TABLE agent_runs (id text PRIMARY KEY, skill_version_id text REFERENCES skill_versions(id) ON DELETE SET NULL)")
            conn.execute("CREATE TABLE skill_version_sources (id text PRIMARY KEY, skill_version_id text REFERENCES skill_versions(id) ON DELETE CASCADE)")
            for skill_id, scope, owner, workspace in [
                ("personal", "personal", "user-1", None),
                ("other-personal", "personal", "user-2", None),
                ("team", "team", None, "ws-1"),
                ("other-team", "team", None, "ws-2"),
            ]:
                conn.execute("INSERT INTO skills VALUES (%s, %s, %s, %s, %s)", (skill_id, scope, owner, workspace, skill_id + "-v1"))
                conn.execute("INSERT INTO skill_versions VALUES (%s, %s)", (skill_id + "-v1", skill_id))
                conn.execute("INSERT INTO agent_runs VALUES (%s, %s)", (skill_id + "-run", skill_id + "-v1"))
                conn.execute("INSERT INTO skill_version_sources VALUES (%s, %s)", (skill_id + "-source", skill_id + "-v1"))
            conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
            monkeypatch.setattr(storage.database, "connect_ai", lambda: nullcontext(conn))
            monkeypatch.setattr(storage, "_is_team_owner", lambda workspace, user: workspace == "ws-1" and user == "owner")
            repository = storage.PostgresSkillRepository()
            for user, skill_id in [("user-1", "other-personal"), ("user-1", "team"), ("owner", "other-team"), ("user-1", "missing")]:
                with pytest.raises(ValueError, match="not manageable"):
                    repository.delete("ws-1", user, skill_id)
            repository.delete("ws-1", "user-1", "personal")
            repository.delete("ws-1", "owner", "team")
            assert conn.execute("SELECT id FROM skills ORDER BY id").fetchall() == [{"id": "other-personal"}, {"id": "other-team"}]
            assert conn.execute("SELECT count(*) AS count FROM skill_versions").fetchone()["count"] == 2
            assert conn.execute("SELECT count(*) AS count FROM skill_version_sources").fetchone()["count"] == 2
            assert conn.execute("SELECT count(*) AS count FROM agent_runs").fetchone()["count"] == 4
            assert conn.execute("SELECT count(*) AS count FROM agent_runs WHERE skill_version_id IS NULL").fetchone()["count"] == 2
            with pytest.raises(ValueError, match="not manageable"):
                repository.delete("ws-1", "user-1", "personal")
        finally:
            conn.rollback()
