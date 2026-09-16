from __future__ import annotations

from dataclasses import replace
from typing import Any

from psycopg.types.json import Json

from app.core.pipeline_control import PipelineRunCancelledError

from app.modules.skill.application.ports import ManageSkillRepositoryPort, SkillRepositoryPort
from app.modules.skill.domain.entities import Skill, SkillVersion
from app.modules.skill.infrastructure.workspace_authorization import get_workspace_role
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database


SKILL_SELECT = """
    SELECT
        s.*,
        ev.id AS ev_id,
        ev.skill_id AS ev_skill_id,
        ev.version AS ev_version,
        ev.name AS ev_name,
        ev.description AS ev_description,
        ev.instructions_markdown AS ev_instructions_markdown,
        ev.capabilities AS ev_capabilities,
        ev.allowed_tools AS ev_allowed_tools,
        ev.safety_result AS ev_safety_result,
        ev.status AS ev_status,
        ev.created_by AS ev_created_by,
        ev.created_at AS ev_created_at,
        ev.published_at AS ev_published_at,
        lv.id AS lv_id,
        lv.skill_id AS lv_skill_id,
        lv.version AS lv_version,
        lv.name AS lv_name,
        lv.description AS lv_description,
        lv.instructions_markdown AS lv_instructions_markdown,
        lv.capabilities AS lv_capabilities,
        lv.allowed_tools AS lv_allowed_tools,
        lv.safety_result AS lv_safety_result,
        lv.status AS lv_status,
        lv.created_by AS lv_created_by,
        lv.created_at AS lv_created_at,
        lv.published_at AS lv_published_at
    FROM skills s
    LEFT JOIN skill_versions ev ON ev.id = s.enabled_version_id
    LEFT JOIN LATERAL (
        SELECT * FROM skill_versions candidate
        WHERE candidate.skill_id = s.id
        ORDER BY candidate.version DESC
        LIMIT 1
    ) lv ON TRUE
"""


class PostgresSkillRepository(SkillRepositoryPort, ManageSkillRepositoryPort):
    def __init__(self, parent_run_id: str | None = None) -> None:
        self._parent_run_id = parent_run_id

    def _lock_parent(self, conn: Any, actor_id: str) -> None:
        if self._parent_run_id is None:
            return
        parent = conn.execute(
            "SELECT status, result FROM agent_runs WHERE id = %s AND user_id = %s "
            "AND action = 'markdown_turn' FOR UPDATE", (self._parent_run_id, actor_id),
        ).fetchone()
        if parent is None or parent["status"] != "executing":
            raise PipelineRunCancelledError("Parent turn no longer accepts a Skill change.")
        if (parent["result"] or {}).get("skill_undo"):
            raise ValueError("Parent turn already published a Skill.")

    def _record_publication(self, conn: Any, skill_id: str, version_id: str, before: dict | None) -> None:
        if self._parent_run_id is None:
            return
        after = conn.execute("SELECT to_jsonb(s) AS snapshot FROM skills s WHERE id = %s", (skill_id,)).fetchone()["snapshot"]
        conn.execute(
            "UPDATE agent_runs SET result = COALESCE(result, '{}'::jsonb) || %s::jsonb WHERE id = %s",
            (Json({"skill_undo": {"skill_id": skill_id, "version_id": version_id, "before": before,
                                  "after": after, "done": False}}), self._parent_run_id),
        )

    def list_accessible_enabled(self, workspace_id: str, user_id: str) -> list[Skill]:
        team_member = _is_team_member(workspace_id, user_id)
        with database.connect_ai() as conn:
            rows = conn.execute(
                SKILL_SELECT
                + """
                WHERE s.status = 'enabled'
                  AND s.enabled_version_id IS NOT NULL
                  AND (
                      (s.scope_type = 'personal' AND s.owner_user_id = %s)
                      OR (
                          s.scope_type = 'team'
                          AND s.workspace_id = %s
                          AND %s = TRUE
                      )
                  )
                ORDER BY s.scope_type, COALESCE(ev.name, ''), s.id
                """,
                (user_id, workspace_id, team_member),
            ).fetchall()
        return [_row_to_skill(row) for row in rows]

    def list_accessible(self, workspace_id: str, user_id: str) -> list[Skill]:
        team_member = _is_team_member(workspace_id, user_id)
        with database.connect_ai() as conn:
            rows = conn.execute(
                SKILL_SELECT
                + """
                WHERE (
                      (s.scope_type = 'personal' AND s.owner_user_id = %s)
                      OR (
                          s.scope_type = 'team'
                          AND s.workspace_id = %s
                          AND %s = TRUE
                      )
                  )
                ORDER BY s.scope_type, COALESCE(ev.name, lv.name, ''), s.id
                """,
                (user_id, workspace_id, team_member),
            ).fetchall()
        return [_row_to_skill(row) for row in rows]

    def get_accessible(self, workspace_id: str, user_id: str, skill_id: str) -> Skill | None:
        return self._get_with_access_filter(workspace_id, user_id, "s.id = %s", skill_id)

    def get_accessible_by_slug(self, workspace_id: str, user_id: str, slug: str) -> Skill | None:
        return self._get_with_access_filter(workspace_id, user_id, "s.command = %s", slug)

    def get_manageable(self, workspace_id: str | None, user_id: str, skill_id: str) -> Skill | None:
        team_owner = _is_team_owner(workspace_id, user_id)
        with database.connect_ai() as conn:
            row = conn.execute(
                SKILL_SELECT
                + """
                WHERE s.id = %s
                  AND (
                      (s.scope_type = 'personal' AND s.owner_user_id = %s)
                      OR (
                          s.scope_type = 'team'
                          AND s.workspace_id = %s
                          AND %s = TRUE
                      )
                  )
                """,
                (skill_id, user_id, workspace_id, team_owner),
            ).fetchone()
        return _row_to_skill(row) if row else None

    def create_published(self, skill: Skill, version: SkillVersion) -> Skill:
        _require_manage_scope(skill.workspace_id, version.created_by or "", skill.scope_type)
        with database.connect_ai() as conn:
            self._lock_parent(conn, version.created_by or "")
            _lock_slug_scope(conn, skill, skill.slug)
            _ensure_slug_available(conn, skill, skill.slug)
            conn.execute(
                """
                INSERT INTO skills (id, workspace_id, scope_type, owner_user_id, command, status)
                VALUES (%s, %s, %s, %s, %s, 'enabled')
                """,
                (skill.id, skill.workspace_id, skill.scope_type, skill.owner_user_id, skill.slug),
            )
            _insert_version(conn, version)
            conn.execute(
                "UPDATE skills SET enabled_version_id = %s, updated_at = now() WHERE id = %s",
                (version.id, skill.id),
            )
            self._record_publication(conn, skill.id, version.id, None)
        saved = self.get_manageable(skill.workspace_id, version.created_by or "", skill.id)
        if saved is None:
            raise ValueError("Published Skill could not be loaded.")
        return saved

    def save_published_version(self, skill: Skill, version: SkillVersion) -> Skill:
        team_owner = _is_team_owner(skill.workspace_id, version.created_by or "")
        with database.connect_ai() as conn:
            self._lock_parent(conn, version.created_by or "")
            if _lock_manageable(
                conn,
                skill.workspace_id,
                version.created_by or "",
                skill.id,
                team_owner,
            ) is None:
                raise ValueError("Skill not found or not manageable.")
            before = (conn.execute("SELECT to_jsonb(s) AS snapshot FROM skills s WHERE id = %s",
                                   (skill.id,)).fetchone()["snapshot"] if self._parent_run_id is not None else None)
            version = _with_next_version(conn, version)
            _lock_slug_scope(conn, skill, version.name)
            _ensure_slug_available(conn, skill, version.name, exclude_skill_id=skill.id)
            _insert_version(conn, version)
            conn.execute(
                """
                UPDATE skills
                SET command = %s, enabled_version_id = %s, updated_at = now()
                WHERE id = %s
                """,
                (version.name, version.id, skill.id),
            )
            self._record_publication(conn, skill.id, version.id, before)
        saved = self.get_manageable(skill.workspace_id, version.created_by or "", skill.id)
        if saved is None:
            raise ValueError("Updated Skill could not be loaded.")
        return saved

    def set_enabled(self, workspace_id: str, user_id: str, skill_id: str, enabled: bool) -> Skill:
        team_owner = _is_team_owner(workspace_id, user_id)
        with database.connect_ai() as conn:
            manageable = _lock_manageable(conn, workspace_id, user_id, skill_id, team_owner)
            if manageable is None:
                raise ValueError("Skill not found or not manageable.")
            row = conn.execute(
                """
                UPDATE skills
                SET status = %s, updated_at = now()
                WHERE id = %s AND (%s = false OR enabled_version_id IS NOT NULL)
                RETURNING id
                """,
                ("enabled" if enabled else "disabled", skill_id, enabled),
            ).fetchone()
            if row is None:
                raise ValueError("Publish a Skill version before enabling it.")
        saved = self.get_manageable(workspace_id, user_id, skill_id)
        if saved is None:
            raise ValueError("Updated Skill could not be loaded.")
        return saved

    def _get_with_access_filter(
        self,
        workspace_id: str | None,
        user_id: str,
        reference_clause: str,
        reference: str,
    ) -> Skill | None:
        team_member = _is_team_member(workspace_id, user_id)
        with database.connect_ai() as conn:
            row = conn.execute(
                SKILL_SELECT
                + f"""
                WHERE {reference_clause}
                  AND (
                      (s.scope_type = 'personal' AND s.owner_user_id = %s)
                      OR (
                          s.scope_type = 'team'
                          AND s.workspace_id = %s
                          AND %s = TRUE
                      )
                  )
                ORDER BY CASE WHEN s.scope_type = 'personal' THEN 0 ELSE 1 END, s.id
                """,
                (reference, user_id, workspace_id, team_member),
            ).fetchone()
        return _row_to_skill(row) if row else None


def _insert_version(conn: Any, version: SkillVersion) -> None:
    conn.execute(
        """
        INSERT INTO skill_versions (
            id, skill_id, version, name, description, instructions_markdown,
            capabilities, allowed_tools, safety_result, status, created_by, published_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  CASE WHEN %s = 'published' THEN now() ELSE NULL END)
        """,
        (
            version.id,
            version.skill_id,
            version.version,
            version.name,
            version.description,
            version.instructions_markdown,
            list(version.capabilities),
            list(version.allowed_tools),
            Json(version.lint_result or {}),
            version.status,
            version.created_by,
            version.status,
        ),
    )


def _with_next_version(conn: Any, version: SkillVersion) -> SkillVersion:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM skill_versions WHERE skill_id = %s",
        (version.skill_id,),
    ).fetchone()
    return replace(version, version=int(row["version"]))


def _require_manage_scope(workspace_id: str | None, user_id: str, scope_type: str) -> None:
    if scope_type == "personal":
        return
    if not workspace_id:
        raise ValueError("Team Skill requires a workspace.")
    if get_workspace_role(workspace_id, user_id) != "OWNER":
        raise ValueError("Workspace Skill management is not allowed.")


def _ensure_slug_available(
    conn: Any,
    skill: Skill,
    slug: str,
    *,
    exclude_skill_id: str | None = None,
) -> None:
    filters = ["s.command = %s"]
    params: list[object] = [slug]
    if skill.scope_type == "personal":
        filters.extend(("s.scope_type = 'personal'", "s.owner_user_id = %s"))
        params.append(skill.owner_user_id)
    else:
        if skill.workspace_id is None:
            raise ValueError("Team Skill requires a workspace.")
        filters.extend(("s.scope_type = 'team'", "s.workspace_id = %s"))
        params.append(skill.workspace_id)
    if exclude_skill_id is not None:
        filters.append("s.id <> %s")
        params.append(exclude_skill_id)
    if conn.execute(
        "SELECT 1 FROM skills s WHERE " + " AND ".join(filters) + " LIMIT 1",
        tuple(params),
    ).fetchone():
        raise ValueError("Skill command already exists in this scope.")


def _lock_slug_scope(conn: Any, skill: Skill, slug: str) -> None:
    owner = skill.owner_user_id if skill.scope_type == "personal" else skill.workspace_id
    if owner is None:
        raise ValueError("Skill scope owner is required.")
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"skill-command:{skill.scope_type}:{owner}:{slug}",),
    )


def _lock_manageable(
    conn: Any,
    workspace_id: str | None,
    user_id: str,
    skill_id: str,
    team_owner: bool,
) -> dict[str, Any] | None:
    return conn.execute(
        """
        SELECT s.*
        FROM skills s
        WHERE s.id = %s
          AND (
              (s.scope_type = 'personal' AND s.owner_user_id = %s)
              OR (
                  s.scope_type = 'team'
                  AND s.workspace_id = %s
                  AND %s = TRUE
              )
          )
        FOR UPDATE
        """,
        (skill_id, user_id, workspace_id, team_owner),
    ).fetchone()


def _is_team_member(workspace_id: str | None, user_id: str) -> bool:
    return bool(workspace_id and get_workspace_role(workspace_id, user_id) != "NONE")


def _is_team_owner(workspace_id: str | None, user_id: str) -> bool:
    return bool(workspace_id and get_workspace_role(workspace_id, user_id) == "OWNER")


def _row_to_skill(row: dict[str, Any]) -> Skill:
    return Skill(
        id=row["id"],
        workspace_id=row["workspace_id"],
        scope_type=row["scope_type"],
        owner_user_id=row["owner_user_id"],
        slug=row["command"],
        status=row["status"],
        enabled_version=_row_to_version(row, "ev"),
        latest_version=_row_to_version(row, "lv"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_version(row: dict[str, Any], prefix: str) -> SkillVersion | None:
    if row.get(f"{prefix}_id") is None:
        return None
    return SkillVersion(
        id=row[f"{prefix}_id"],
        skill_id=row[f"{prefix}_skill_id"],
        version=row[f"{prefix}_version"],
        name=row[f"{prefix}_name"],
        description=row[f"{prefix}_description"],
        instructions_markdown=row[f"{prefix}_instructions_markdown"],
        capabilities=tuple(row[f"{prefix}_capabilities"] or ()),
        allowed_tools=tuple(row[f"{prefix}_allowed_tools"] or ()),
        lint_result=row[f"{prefix}_safety_result"] or {},
        status=row[f"{prefix}_status"],
        created_by=row[f"{prefix}_created_by"],
        created_at=row[f"{prefix}_created_at"],
        published_at=row[f"{prefix}_published_at"],
    )
