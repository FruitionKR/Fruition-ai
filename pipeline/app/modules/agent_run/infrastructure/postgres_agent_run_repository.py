from __future__ import annotations

import json
import hashlib
from typing import Any
from uuid import uuid4

from psycopg.types.json import Json

from app.core.pipeline_control import PipelineRunCancelledError

from app.modules.agent_run.application.ports import (
    AgentApprovalRepositoryPort,
    AgentRunRepositoryPort,
)
from app.modules.agent_run.domain.entities import AgentRun, StartAgentRunArtifact
from app.modules.agent_run.domain.plan import AgentPlan, AgentPlanOperation
from app.modules.wiki_ingestion.infrastructure.object_storage import (
    delete_object,
    read_text_object,
    write_text_object,
)
from app.modules.wiki_ingestion.infrastructure import (
    postgres_wiki_ingestion_repository as database,
)


def agent_command_hash(command: dict[str, Any]) -> str:
    canonical = json.dumps(command, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def register_cancelled_turn(command: dict[str, Any]) -> None:
    digest = agent_command_hash(command)
    with database.connect_ai() as conn:
        conn.execute("""
            INSERT INTO agent_runs(id, workspace_id, user_id, action, status, request_summary,
                                   command_envelope_hash, result)
            VALUES (%s, %s, %s, 'markdown_turn', 'queued', %s, %s, '{"action":"reject"}')
            ON CONFLICT (id) DO NOTHING
        """, (command["run_id"], command["workspace_id"], command["user_id"],
              str(command.get("message", ""))[:1000], digest))
        row = conn.execute("SELECT command_envelope_hash FROM agent_runs WHERE id = %s",
                           (command["run_id"],)).fetchone()
        if row is None or row["command_envelope_hash"] != digest:
            raise ValueError("Agent command identity mismatch.")


class PostgresAgentRunRepository(AgentRunRepositoryPort, AgentApprovalRepositoryPort):
    def __init__(self, parent_run_id: str | None = None) -> None:
        self._parent_run_id = parent_run_id

    def create_with_planning_job(
        self,
        run: AgentRun,
        job_id: str,
        artifact: StartAgentRunArtifact | None = None,
    ) -> AgentRun:
        object_key = _artifact_object_key(run.id, artifact.id) if artifact is not None else ""
        object_written = False
        try:
            with database.connect_ai() as conn:
                if self._parent_run_id is not None:
                    parent = _lock_user_run(conn, run.workspace_id, run.user_id, self._parent_run_id)
                    if parent is None or parent["action"] != "markdown_turn" or parent["status"] != "executing":
                        raise PipelineRunCancelledError("Parent turn no longer accepts a child run.")
                    child_id = (parent["result"] or {}).get("run_id")
                    if child_id:
                        child = conn.execute("SELECT * FROM agent_runs WHERE id = %s", (child_id,)).fetchone()
                        if child is None:
                            raise ValueError("Parent turn child run is missing.")
                        return _row_to_run(child)
                    # 하위 run 생성과 참조를 함께 커밋하여 취소·재전달 경합을 막는다.
                    conn.execute("UPDATE agent_runs SET result = %s, updated_at = now() WHERE id = %s",
                                 (Json({"run_id": run.id}), self._parent_run_id))
                if artifact is not None:
                    _validate_artifact_metadata(
                        artifact.purpose,
                        artifact.document_id,
                        artifact.base_version,
                        artifact.target,
                    )
                    _validate_content_hash(artifact.markdown, artifact.content_hash)
                    write_text_object(object_key, artifact.markdown)
                    object_written = True
                row = conn.execute(
                    """
                    INSERT INTO agent_runs (
                        id, workspace_id, user_id, action, skill_version_id, status, request_summary,
                        provider, model
                    ) VALUES (%s, %s, %s, %s, %s, 'queued', %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        run.id,
                        run.workspace_id,
                        run.user_id,
                        run.action,
                        run.skill_version_id,
                        run.request_summary,
                        run.provider,
                        run.model,
                    ),
                ).fetchone()
                if artifact is not None:
                    conn.execute(
                        """
                        INSERT INTO agent_run_artifacts (
                            id, run_id, workspace_id, user_id, content_hash, purpose, object_key,
                            document_id, base_version, target, expires_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                  now() + interval '1 day')
                        """,
                        (
                            artifact.id,
                            run.id,
                            run.workspace_id,
                            run.user_id,
                            artifact.content_hash,
                            artifact.purpose,
                            object_key,
                            artifact.document_id,
                            artifact.base_version,
                            Json(artifact.target) if artifact.target is not None else None,
                        ),
                    )
                conn.execute(
                    """
                    INSERT INTO agent_jobs (id, run_id, job_type, status)
                    VALUES (%s, %s, 'planning', 'queued')
                    """,
                    (job_id, run.id),
                )
        except Exception:
            if object_written:
                delete_object(object_key)
            raise
        return _row_to_run(row)

    def get_for_user(self, workspace_id: str, user_id: str, run_id: str) -> AgentRun | None:
        with database.connect_ai() as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_runs
                WHERE id = %s AND workspace_id = %s AND user_id = %s
                """,
                (run_id, workspace_id, user_id),
            ).fetchone()
        return _row_to_run(row) if row else None

    def authorize_tool_read(self, workspace_id: str, user_id: str, run_id: str) -> bool:
        with database.connect_ai() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM agent_runs
                WHERE id = %s AND workspace_id = %s AND user_id = %s
                """,
                (run_id, workspace_id, user_id),
            ).fetchone()
        return row is not None

    def authorize_tool_execute(
        self,
        *,
        run_id: str,
        workspace_id: str,
        user_id: str,
        plan_id: str,
        plan_version: int,
        operation_hash: str,
        operation_id: str,
        tool_name: str,
        arguments: dict[str, object],
    ) -> bool:
        with database.connect_ai() as conn:
            operation = conn.execute(
                """
                SELECT operation.arguments
                FROM agent_runs run
                JOIN agent_plans plan
                  ON plan.id = run.current_plan_id AND plan.run_id = run.id
                JOIN agent_plan_operations operation
                  ON operation.plan_id = plan.id
                WHERE run.id = %s AND run.workspace_id = %s AND run.user_id = %s
                  AND run.status IN ('executing', 'verifying')
                  AND plan.id = %s AND plan.version = %s AND plan.operation_hash = %s
                  AND plan.status = 'approved'
                  AND operation.id = %s AND operation.tool_name = %s
                  AND operation.status = 'running'
                  AND EXISTS (
                      SELECT 1 FROM agent_approvals approval
                      WHERE approval.run_id = run.id AND approval.plan_id = plan.id
                        AND approval.user_id = run.user_id
                        AND approval.decision = 'approved'
                        AND approval.plan_version = plan.version
                        AND approval.operation_hash = plan.operation_hash
                  )
                """,
                (
                    run_id,
                    workspace_id,
                    user_id,
                    plan_id,
                    plan_version,
                    operation_hash,
                    operation_id,
                    tool_name,
                ),
            ).fetchone()
            if operation is None:
                undo = conn.execute(
                    """
                    SELECT undo.response_metadata
                    FROM agent_tool_executions undo
                    JOIN agent_runs run ON run.id = undo.run_id
                    JOIN agent_plans plan ON plan.id = undo.plan_id AND plan.run_id = run.id
                    WHERE undo.id = %s AND undo.status = 'undo_running'
                      AND run.id = %s AND run.workspace_id = %s AND run.user_id = %s
                      AND run.status = 'rolling_back'
                      AND plan.id = %s AND plan.version = %s AND plan.operation_hash = %s
                      AND plan.status = 'approved'
                      AND EXISTS (
                          SELECT 1 FROM agent_approvals approval
                          WHERE approval.run_id = run.id AND approval.plan_id = plan.id
                            AND approval.user_id = run.user_id AND approval.decision = 'approved'
                            AND approval.plan_version = plan.version
                            AND approval.operation_hash = plan.operation_hash
                      )
                    """,
                    (operation_id, run_id, workspace_id, user_id, plan_id, plan_version, operation_hash),
                ).fetchone()
                if undo is None:
                    return False
                recorded = undo["response_metadata"]
                return (recorded.get("undo_tool") == tool_name
                        and _canonical_json(recorded.get("undo_arguments")) == _canonical_json(arguments))
            result_rows = conn.execute(
                """
                SELECT DISTINCT ON (execution.operation_id)
                       execution.operation_id, execution.response_metadata
                FROM agent_tool_executions execution
                JOIN agent_plan_operations operation
                  ON operation.id = execution.operation_id
                 AND operation.plan_id = execution.plan_id
                WHERE execution.run_id = %s AND execution.plan_id = %s
                  AND execution.status = 'succeeded' AND operation.status = 'succeeded'
                ORDER BY execution.operation_id, execution.attempt DESC
                """,
                (run_id, plan_id),
            ).fetchall()
        results = {
            row["operation_id"]: row["response_metadata"] or {}
            for row in result_rows
        }
        try:
            expected = _resolve_approved_arguments(operation["arguments"], results)
            return _canonical_json(expected) == _canonical_json(arguments)
        except (TypeError, ValueError):
            return False

    def register_artifact(
        self,
        *,
        run_id: str,
        workspace_id: str,
        user_id: str,
        artifact_id: str,
        content_hash: str,
        purpose: str,
        document_id: str | None,
        base_version: int | None,
        target: dict[str, object] | None,
        markdown: str,
    ) -> dict[str, object]:
        _validate_artifact_metadata(purpose, document_id, base_version, target)
        _validate_content_hash(markdown, content_hash)
        object_key = _artifact_object_key(run_id, artifact_id)
        object_written = False
        try:
            with database.connect_ai() as conn:
                run = conn.execute(
                    "SELECT 1 FROM agent_runs WHERE id = %s AND workspace_id = %s AND user_id = %s",
                    (run_id, workspace_id, user_id),
                ).fetchone()
                if run is None:
                    raise KeyError("AgentRun not found.")
                existing = conn.execute(
                    "SELECT * FROM agent_run_artifacts WHERE id = %s FOR UPDATE",
                    (artifact_id,),
                ).fetchone()
                if existing is not None:
                    if not _same_artifact(existing, run_id, workspace_id, user_id, content_hash, purpose,
                                          document_id, base_version, target):
                        raise ValueError("Agent artifact registration conflicts with an existing artifact.")
                    return _artifact_metadata(existing)
                write_text_object(object_key, markdown)
                object_written = True
                row = conn.execute(
                    """
                    INSERT INTO agent_run_artifacts (
                        id, run_id, workspace_id, user_id, content_hash, purpose, object_key,
                        document_id, base_version, target, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now() + interval '1 day')
                    RETURNING *
                    """,
                    (artifact_id, run_id, workspace_id, user_id, content_hash, purpose, object_key,
                     document_id, base_version, Json(target) if target is not None else None),
                ).fetchone()
        except Exception:
            if object_written:
                delete_object(object_key)
            raise
        return _artifact_metadata(row)

    def list_artifacts(self, workspace_id: str, user_id: str, run_id: str) -> list[dict[str, object]]:
        with database.connect_ai() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_run_artifacts
                WHERE run_id = %s AND workspace_id = %s AND user_id = %s
                  AND (expires_at IS NULL OR expires_at > now())
                ORDER BY created_at, id
                """,
                (run_id, workspace_id, user_id),
            ).fetchall()
        return [_artifact_metadata(row) for row in rows]

    def resolve_artifact(
        self,
        *,
        run_id: str,
        workspace_id: str,
        user_id: str,
        artifact_id: str,
        content_hash: str,
        purpose: str,
        document_id: str | None,
        base_version: int | None,
        target: dict[str, object] | None,
    ) -> dict[str, object] | None:
        _validate_artifact_metadata(purpose, document_id, base_version, target)
        with database.connect_ai() as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_run_artifacts
                WHERE id = %s AND run_id = %s AND workspace_id = %s AND user_id = %s
                  AND (expires_at IS NULL OR expires_at > now())
                """,
                (artifact_id, run_id, workspace_id, user_id),
            ).fetchone()
        if row is None:
            return None
        if not _same_artifact(row, run_id, workspace_id, user_id, content_hash, purpose,
                              document_id, base_version, target):
            raise ValueError("Agent artifact metadata does not match the approved operation.")
        if not row["object_key"]:
            raise ValueError("Agent artifact object is not registered.")
        markdown = read_text_object(row["object_key"])
        _validate_content_hash(markdown, row["content_hash"])
        return {**_artifact_metadata(row), "markdown": markdown}

    def get_markdown_turn_status(
        self, workspace_id: str, user_id: str, run_id: str
    ) -> dict[str, object] | None:
        with database.connect_ai() as conn:
            row = conn.execute(
                """
                SELECT id, document_id, base_version, apply_operation_id,
                       status, result, error_code
                FROM agent_runs
                WHERE id = %s AND workspace_id = %s AND user_id = %s
                  AND action = 'markdown_turn'
                """,
                (run_id, workspace_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def get_current_plan_for_user(
        self, workspace_id: str, user_id: str, run_id: str
    ) -> tuple[AgentRun, AgentPlan] | None:
        with database.connect_ai() as conn:
            run_row = conn.execute(
                """
                SELECT * FROM agent_runs
                WHERE id = %s AND workspace_id = %s AND user_id = %s
                """,
                (run_id, workspace_id, user_id),
            ).fetchone()
            if run_row is None or run_row["current_plan_id"] is None:
                return None
            plan_row = conn.execute(
                "SELECT * FROM agent_plans WHERE id = %s AND run_id = %s",
                (run_row["current_plan_id"], run_id),
            ).fetchone()
            operation_rows = conn.execute(
                """
                SELECT * FROM agent_plan_operations
                WHERE plan_id = %s ORDER BY sequence
                """,
                (run_row["current_plan_id"],),
            ).fetchall()
        if plan_row is None:
            return None
        return _row_to_run(run_row), _rows_to_plan(plan_row, operation_rows)

    def save_plan(self, run_id: str, plan: AgentPlan) -> None:
        with database.connect_ai() as conn:
            run = conn.execute("SELECT status FROM agent_runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
            if run is None or run["status"] not in {"queued", "planning", "clarification_required"}:
                raise ValueError("AgentRun cannot accept a new plan.")
            conn.execute(
                "UPDATE agent_plans SET status = 'superseded' WHERE run_id = %s AND status <> 'approved'",
                (run_id,),
            )
            conn.execute(
                """
                INSERT INTO agent_plans (id, run_id, version, summary, operation_hash, status)
                VALUES (%s, %s, %s, %s, %s, 'awaiting_approval')
                """,
                (plan.id, run_id, plan.version, plan.summary, plan.operation_hash),
            )
            for operation in plan.operations:
                conn.execute(
                    """
                    INSERT INTO agent_plan_operations (
                        id, plan_id, sequence, tool_name, target_type, target_id, base_version,
                        source_parent_id, destination_parent_id, arguments, reason, depends_on, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
                    """,
                    (
                        operation.id,
                        plan.id,
                        operation.sequence,
                        operation.tool_name,
                        operation.target_type,
                        operation.target_id,
                        operation.base_version,
                        operation.source_parent_id,
                        operation.destination_parent_id,
                        Json(operation.arguments),
                        operation.reason,
                        list(operation.depends_on),
                    ),
                )
            conn.execute(
                """
                UPDATE agent_runs
                SET current_plan_id = %s, status = 'awaiting_approval', updated_at = now()
                WHERE id = %s
                """,
                (plan.id, run_id),
            )

    def approve_and_enqueue(
        self,
        run: AgentRun,
        plan: AgentPlan,
        approval_id: str,
        job_id: str,
    ) -> AgentRun:
        with database.connect_ai() as conn:
            locked = conn.execute(
                "SELECT * FROM agent_runs WHERE id = %s AND user_id = %s FOR UPDATE",
                (run.id, run.user_id),
            ).fetchone()
            current_plan = conn.execute(
                "SELECT * FROM agent_plans WHERE id = %s AND run_id = %s FOR UPDATE",
                (plan.id, run.id),
            ).fetchone()
            if (
                locked is None
                or current_plan is None
                or locked["status"] != "awaiting_approval"
                or locked["current_plan_id"] != plan.id
                or current_plan["status"] != "awaiting_approval"
                or current_plan["version"] != plan.version
                or current_plan["operation_hash"] != plan.operation_hash
            ):
                raise ValueError("Agent plan changed and must be reviewed again.")
            conn.execute(
                """
                INSERT INTO agent_approvals (
                    id, run_id, plan_id, plan_version, operation_hash, user_id, decision
                ) VALUES (%s, %s, %s, %s, %s, %s, 'approved')
                """,
                (approval_id, run.id, plan.id, plan.version, plan.operation_hash, run.user_id),
            )
            conn.execute("UPDATE agent_plans SET status = 'approved' WHERE id = %s", (plan.id,))
            updated = conn.execute(
                """
                UPDATE agent_runs SET status = 'executing', updated_at = now()
                WHERE id = %s RETURNING *
                """,
                (run.id,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO agent_jobs (id, run_id, job_type, status)
                VALUES (%s, %s, 'execution', 'queued')
                """,
                (job_id, run.id),
            )
        return _row_to_run(updated)

    def reject(self, workspace_id: str, user_id: str, run_id: str, approval_id: str) -> AgentRun:
        with database.connect_ai() as conn:
            run = _lock_user_run(conn, workspace_id, user_id, run_id)
            if run is None or run["status"] != "awaiting_approval" or run["current_plan_id"] is None:
                raise ValueError("Agent plan is not awaiting approval.")
            plan = conn.execute(
                "SELECT * FROM agent_plans WHERE id = %s AND run_id = %s FOR UPDATE",
                (run["current_plan_id"], run_id),
            ).fetchone()
            if plan is None or plan["status"] != "awaiting_approval":
                raise ValueError("Agent plan is not awaiting approval.")
            conn.execute(
                """
                INSERT INTO agent_approvals (
                    id, run_id, plan_id, plan_version, operation_hash, user_id, decision
                ) VALUES (%s, %s, %s, %s, %s, %s, 'rejected')
                """,
                (approval_id, run_id, plan["id"], plan["version"], plan["operation_hash"], user_id),
            )
            conn.execute("UPDATE agent_plans SET status = 'rejected' WHERE id = %s", (plan["id"],))
            updated = conn.execute(
                """
                UPDATE agent_runs
                SET status = 'rejected', updated_at = now(), finished_at = now()
                WHERE id = %s RETURNING *
                """,
                (run_id,),
            ).fetchone()
        return _row_to_run(updated)

    def cancel(self, workspace_id: str, user_id: str, run_id: str) -> AgentRun:
        with database.connect_ai() as conn:
            run = _lock_user_run(conn, workspace_id, user_id, run_id)
            if run is None:
                raise ValueError("AgentRun not found.")
            if run["status"] in {"cancel_requested", "rolling_back", "cancelled"}:
                return _row_to_run(run)
            child_id = (run["result"] or {}).get("run_id") if run["action"] == "markdown_turn" else None
            if run["status"] == "rejected" or (run["status"] == "completed" and run["action"] != "markdown_turn"):
                raise ValueError("Completed AgentRun cannot be cancelled.")
            if child_id:
                child = _lock_user_run(conn, workspace_id, user_id, child_id)
                if child is None or child["action"] == "markdown_turn":
                    raise ValueError("Parent turn child run is invalid.")
                if child["status"] not in {"cancel_requested", "rolling_back", "cancelled"}:
                    _request_rollback(conn, child_id)
            updated = _request_rollback(conn, run_id)
        return _row_to_run(updated)

    def revise(
        self,
        workspace_id: str,
        user_id: str,
        run_id: str,
        instruction: str,
        job_id: str,
    ) -> AgentRun:
        with database.connect_ai() as conn:
            run = _lock_user_run(conn, workspace_id, user_id, run_id)
            if run is None or run["status"] not in {"awaiting_approval", "clarification_required"}:
                raise ValueError("AgentRun cannot be revised in its current state.")
            if run["current_plan_id"]:
                conn.execute(
                    "UPDATE agent_plans SET status = 'superseded' WHERE id = %s",
                    (run["current_plan_id"],),
                )
            updated = conn.execute(
                """
                UPDATE agent_runs
                SET status = 'queued', current_plan_id = NULL, request_summary = %s,
                    error_code = NULL, updated_at = now(), finished_at = NULL
                WHERE id = %s RETURNING *
                """,
                (instruction.strip(), run_id),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO agent_jobs (id, run_id, job_type, status)
                VALUES (%s, %s, 'planning', 'queued')
                """,
                (job_id, run_id),
            )
        return _row_to_run(updated)


def _row_to_run(row: dict[str, Any]) -> AgentRun:
    return AgentRun(
        id=row["id"],
        workspace_id=row["workspace_id"],
        user_id=row["user_id"],
        action=row["action"],
        skill_version_id=row["skill_version_id"],
        status=row["status"],
        request_summary=row["request_summary"],
        provider=row.get("provider"),
        model=row.get("model"),
        current_plan_id=row["current_plan_id"],
        error_code=row["error_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
    )


def _rows_to_plan(plan_row: dict[str, Any], operation_rows: list[dict[str, Any]]) -> AgentPlan:
    return AgentPlan(
        id=plan_row["id"],
        run_id=plan_row["run_id"],
        version=plan_row["version"],
        summary=plan_row["summary"],
        operation_hash=plan_row["operation_hash"],
        status=plan_row["status"],
        operations=tuple(
            AgentPlanOperation(
                id=row["id"],
                sequence=row["sequence"],
                tool_name=row["tool_name"],
                target_type=row["target_type"],
                target_id=row["target_id"],
                base_version=row["base_version"],
                source_parent_id=row["source_parent_id"],
                destination_parent_id=row["destination_parent_id"],
                arguments=row["arguments"] or {},
                reason=row["reason"],
                depends_on=tuple(row["depends_on"] or ()),
                status=row["status"],
                error_code=row["error_code"],
            )
            for row in operation_rows
        ),
    )


def _lock_user_run(
    conn: Any,
    workspace_id: str,
    user_id: str,
    run_id: str,
) -> dict[str, Any] | None:
    return conn.execute(
        """
        SELECT * FROM agent_runs
        WHERE id = %s AND workspace_id = %s AND user_id = %s
        FOR UPDATE
        """,
        (run_id, workspace_id, user_id),
    ).fetchone()


def _resolve_approved_arguments(value: object, results: dict[str, dict[str, object]]) -> object:
    if isinstance(value, dict):
        if set(value) == {"$operation_result", "field"}:
            operation_id = value["$operation_result"]
            field = value["field"]
            if (
                not isinstance(operation_id, str)
                or not isinstance(field, str)
                or operation_id not in results
                or field not in results[operation_id]
            ):
                raise ValueError("Approved Agent operation result cannot be resolved.")
            return results[operation_id][field]
        return {
            key: _resolve_approved_arguments(item, results)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_resolve_approved_arguments(item, results) for item in value]
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _artifact_object_key(run_id: str, artifact_id: str) -> str:
    scope = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    suffix = hashlib.sha256(f"{run_id}:{artifact_id}".encode("utf-8")).hexdigest()
    return f"agent-runs/{scope}/artifacts/{suffix}.md"


def _validate_content_hash(markdown: str, content_hash: str) -> None:
    if len(markdown.encode("utf-8")) > 5 * 1024 * 1024:
        raise ValueError("Agent artifact Markdown is too large.")
    expected = f"sha256:{hashlib.sha256(markdown.encode('utf-8')).hexdigest()}"
    if content_hash != expected:
        raise ValueError("Agent artifact content hash does not match its content.")


def _validate_artifact_metadata(
    purpose: str,
    document_id: str | None,
    base_version: int | None,
    target: dict[str, object] | None,
) -> None:
    if purpose == "create_document":
        if document_id is not None or base_version is not None or target is not None:
            raise ValueError("Document creation artifact target is invalid.")
        return
    if purpose != "apply_document_edit":
        raise ValueError("Agent artifact purpose is invalid.")
    if not document_id or base_version is None or not _is_document_target(target):
        raise ValueError("Document edit artifact target is invalid.")


def _is_document_target(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"type", "start_line", "end_line"}:
        return False
    start_line = value.get("start_line")
    end_line = value.get("end_line")
    return (
        value.get("type") in {"selection", "current_section", "whole_document"}
        and isinstance(start_line, int)
        and not isinstance(start_line, bool)
        and isinstance(end_line, int)
        and not isinstance(end_line, bool)
        and start_line >= 1
        and end_line >= start_line
    )


def _same_artifact(
    row: Any,
    run_id: str,
    workspace_id: str,
    user_id: str,
    content_hash: str,
    purpose: str,
    document_id: str | None,
    base_version: int | None,
    target: dict[str, object] | None,
) -> bool:
    return (
        row["run_id"] == run_id
        and row["workspace_id"] == workspace_id
        and row["user_id"] == user_id
        and row["content_hash"] == content_hash
        and row["purpose"] == purpose
        and row["document_id"] == document_id
        and row["base_version"] == base_version
        and (row["target"] or None) == (target or None)
    )


def _artifact_metadata(row: Any) -> dict[str, object]:
    return {
        "id": row["id"],
        "content_hash": row["content_hash"],
        "purpose": row["purpose"],
        "document_id": row["document_id"],
        "base_version": row["base_version"],
        "target": row["target"],
    }


def _request_rollback(conn: Any, run_id: str) -> dict[str, Any]:
    conn.execute("UPDATE agent_jobs SET status = 'cancelled', updated_at = now() "
                 "WHERE run_id = %s AND status = 'queued'", (run_id,))
    conn.execute(
        "UPDATE agent_plan_operations SET status = 'cancelled', updated_at = now() "
        "WHERE plan_id IN (SELECT id FROM agent_plans WHERE run_id = %s) AND status = 'pending'", (run_id,),
    )
    updated = conn.execute(
        "UPDATE agent_runs SET status = 'cancel_requested', error_code = NULL, updated_at = now(), "
        "finished_at = NULL WHERE id = %s RETURNING *", (run_id,),
    ).fetchone()
    conn.execute("INSERT INTO agent_jobs (id, run_id, job_type, status) VALUES (%s, %s, 'rollback', 'queued')",
                 (str(uuid4()), run_id))
    return updated
