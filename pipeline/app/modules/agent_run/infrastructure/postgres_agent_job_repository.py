from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from uuid import uuid4

from psycopg.types.json import Json

from app.modules.agent_run.domain.entities import AgentJob, AgentRunContext
from app.modules.agent_run.domain.plan import AgentPlan, AgentPlanOperation
from app.modules.agent_run.infrastructure.postgres_agent_run_repository import _row_to_run, _rows_to_plan
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database

class PostgresAgentJobRepository:
    def list_expired_run_ids(self) -> tuple[str, ...]:
        with database.connect_ai() as conn:
            rows = conn.execute(
                """
                SELECT id FROM agent_runs
                WHERE finished_at < now() - interval '90 days'
                  AND status = ANY(%s)
                """,
                (["completed", "partial_failed", "failed", "conflicted", "rejected", "cancelled"],),
            ).fetchall()
        return tuple(row["id"] for row in rows)

    def delete_expired_runs(self, run_ids: tuple[str, ...]) -> int:
        if not run_ids:
            return 0
        with database.connect_ai() as conn:
            rows = conn.execute(
                """
                DELETE FROM agent_runs
                WHERE id = ANY(%s)
                  AND finished_at < now() - interval '90 days'
                  AND status = ANY(%s)
                RETURNING id
                """,
                (
                    list(run_ids),
                    ["completed", "partial_failed", "failed", "conflicted", "rejected", "cancelled"],
                ),
            ).fetchall()
        return len(rows)

    def claim_next(self, worker_id: str) -> AgentJob | None:
        lease_token = str(uuid4())
        with database.connect_ai() as conn:
            row = conn.execute(
                """
                WITH candidate AS (
                    SELECT pending.id
                    FROM agent_jobs pending
                    WHERE (
                          pending.attempt_count < 3
                          OR (pending.job_type = 'rollback' AND pending.status = 'leased'
                              AND pending.leased_until < now())
                      )
                      AND pending.job_type IN ('planning', 'execution', 'rollback')
                      AND pending.available_at <= now()
                      AND (
                          pending.status = 'queued'
                          OR (pending.status = 'leased' AND pending.leased_until < now())
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM agent_runs parent
                          JOIN agent_runs child ON child.id = parent.result->>'run_id'
                          WHERE parent.id = pending.run_id AND parent.action = 'markdown_turn'
                            AND pending.job_type = 'rollback'
                            AND child.status IN ('cancel_requested', 'rolling_back')
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM agent_jobs predecessor
                          WHERE predecessor.run_id = pending.run_id
                            AND (predecessor.attempt_count < 3 OR predecessor.job_type = 'rollback')
                            AND predecessor.status IN ('queued', 'leased')
                            AND (predecessor.created_at, predecessor.id)
                                < (pending.created_at, pending.id)
                      )
                    ORDER BY pending.created_at, pending.id
                    FOR UPDATE OF pending SKIP LOCKED
                    LIMIT 1
                )
                UPDATE agent_jobs job
                SET status = 'leased', lease_owner = %s, lease_token = %s,
                    leased_until = now() + interval '90 seconds', heartbeat_at = now(),
                    attempt_count = attempt_count + 1, updated_at = now()
                FROM candidate
                WHERE job.id = candidate.id
                RETURNING job.*
                """,
                (worker_id, lease_token),
            ).fetchone()
        if row is None:
            return None
        return AgentJob(
            id=row["id"],
            run_id=row["run_id"],
            job_type=row["job_type"],
            attempt_count=row["attempt_count"],
            lease_token=row["lease_token"],
            leased_until=row["leased_until"],
        )

    def heartbeat(self, job: AgentJob) -> bool:
        with database.connect_ai() as conn:
            row = conn.execute(
                """
                UPDATE agent_jobs
                SET leased_until = now() + interval '90 seconds', heartbeat_at = now(), updated_at = now()
                WHERE id = %s AND status = 'leased' AND lease_token = %s
                RETURNING id
                """,
                (job.id, job.lease_token),
            ).fetchone()
        return row is not None

    def complete(self, job: AgentJob) -> None:
        with database.connect_ai() as conn:
            updated = conn.execute(
                """
                UPDATE agent_jobs SET status = 'completed', updated_at = now()
                WHERE id = %s AND status = 'leased' AND lease_token = %s
                RETURNING id
                """,
                (job.id, job.lease_token),
            ).fetchone()
            if updated is None:
                raise ValueError("Agent job lease was lost.")

    def fail(self, job: AgentJob, error_code: str) -> None:
        terminal = job.attempt_count >= 3
        with database.connect_ai() as conn:
            updated = conn.execute(
                """
                UPDATE agent_jobs
                SET status = %s, available_at = now() + interval '5 seconds', updated_at = now()
                WHERE id = %s AND status = 'leased' AND lease_token = %s
                RETURNING id
                """,
                ("failed" if terminal else "queued", job.id, job.lease_token),
            ).fetchone()
            if updated is None:
                return
            if terminal and job.job_type == "rollback":
                conn.execute(
                    "UPDATE agent_runs SET status = 'rollback_failed', error_code = %s, updated_at = now() "
                    "WHERE id = %s AND status IN ('cancel_requested', 'rolling_back')",
                    (error_code, job.run_id),
                )
            if terminal:
                conn.execute(
                    """
                    WITH active_run AS (
                        SELECT current_plan_id
                        FROM agent_runs
                        WHERE id = %s
                          AND status NOT IN (
                              'completed', 'partial_failed', 'failed', 'conflicted', 'rejected', 'cancelled',
                              'cancel_requested', 'rolling_back', 'rollback_failed'
                          )
                    )
                    UPDATE agent_plan_operations
                    SET status = 'failed', error_code = %s, updated_at = now()
                    WHERE plan_id = (SELECT current_plan_id FROM active_run)
                      AND status = 'running'
                    """,
                    (job.run_id, error_code),
                )
                conn.execute(
                    """
                    UPDATE agent_runs
                    SET status = 'failed', error_code = %s, updated_at = now(), finished_at = now()
                    WHERE id = %s
                      AND status NOT IN (
                          'completed', 'partial_failed', 'failed', 'conflicted', 'rejected', 'cancelled',
                              'cancel_requested', 'rolling_back', 'rollback_failed'
                      )
                    """,
                    (error_code, job.run_id),
                )

    @contextmanager
    def execution_lock(self, run_id: str):
        # lease가 만료돼도 이전 worker가 살아 있으면 같은 run을 동시에 실행하지 않는다.
        with database.connect_ai() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"agent:{run_id}",))
            yield

    def get_undo_record(self, run_id: str, operation_id: str) -> dict[str, Any] | None:
        with database.connect_ai() as conn:
            return conn.execute(
                "SELECT * FROM agent_tool_executions WHERE run_id = %s AND idempotency_key = %s",
                (run_id, f"undo:{operation_id}"),
            ).fetchone()

    def prepare_undo(self, run_id: str, plan_id: str, operation_id: str,
                     tool_name: str, arguments: dict[str, Any], before: dict[str, Any]) -> bool:
        with database.connect_ai() as conn:
            run = conn.execute(
                "SELECT status FROM agent_runs WHERE id = %s FOR UPDATE", (run_id,),
            ).fetchone()
            if run is None or run["status"] != "executing":
                return False
            conn.execute(
                """
                INSERT INTO agent_tool_executions (
                    id, run_id, plan_id, operation_id, tool_name, idempotency_key,
                    attempt, status, response_metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, 0, 'undo_pending', %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (str(uuid4()), run_id, plan_id, operation_id, tool_name,
                 f"undo:{operation_id}", Json({"before": before, "arguments": arguments})),
            )
        return True

    def load_undo_records(self, run_id: str) -> list[dict[str, Any]]:
        with database.connect_ai() as conn:
            return conn.execute(
                """
                SELECT undo.id, undo.status, undo.response_metadata,
                       operation.id AS operation_id, operation.tool_name,
                       plan.id AS plan_id, plan.version AS plan_version, plan.operation_hash,
                       forward.status AS forward_status, forward.response_metadata AS forward_response,
                       forward.attempt AS forward_attempt, forward.error_code AS forward_error_code
                FROM agent_plans plan
                JOIN agent_plan_operations operation ON operation.plan_id = plan.id
                LEFT JOIN agent_tool_executions undo
                  ON undo.run_id = plan.run_id AND undo.idempotency_key = 'undo:' || operation.id
                LEFT JOIN agent_tool_executions forward
                  ON forward.run_id = plan.run_id
                 AND forward.idempotency_key = 'agent:' || plan.run_id || ':' || plan.id || ':' || operation.id
                WHERE plan.run_id = %s
                  AND (undo.id IS NOT NULL OR forward.id IS NOT NULL
                       OR operation.status IN ('running', 'succeeded', 'verification_failed'))
                ORDER BY plan.version DESC, operation.sequence DESC
                """,
                (run_id,),
            ).fetchall()

    def save_undo_request(self, record_id: str, tool_name: str, arguments: dict[str, Any]) -> None:
        with database.connect_ai() as conn:
            conn.execute(
                """
                UPDATE agent_tool_executions
                SET status = 'undo_running', response_metadata = response_metadata || %s::jsonb
                WHERE id = %s AND status IN ('undo_pending', 'undo_running')
                """,
                (Json({"undo_tool": tool_name, "undo_arguments": arguments}), record_id),
            )

            if arguments.get("content_artifact_id"):
                conn.execute(
                    """
                    UPDATE agent_run_artifacts SET expires_at = NULL
                    WHERE id = %s AND run_id = (SELECT run_id FROM agent_tool_executions WHERE id = %s)
                    """,
                    (arguments["content_artifact_id"], record_id),
                )

    def complete_undo(self, record_id: str, response: dict[str, Any]) -> None:
        with database.connect_ai() as conn:
            row = conn.execute(
                """
                UPDATE agent_tool_executions
                SET status = 'undo_done', response_metadata = response_metadata || %s::jsonb, finished_at = now()
                WHERE id = %s AND status IN ('undo_pending', 'undo_running')
                RETURNING operation_id
                """,
                (Json({"undo_response": response}), record_id),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE agent_plan_operations SET status = 'rolled_back', updated_at = now() WHERE id = %s",
                    (row["operation_id"],),
                )

    def parent_rollback_error(self, run_id: str) -> str | None:
        with database.connect_ai() as conn:
            row = conn.execute(
                "SELECT parent.result, child.status AS child_status FROM agent_runs parent "
                "LEFT JOIN agent_runs child ON child.id = parent.result->>'run_id' "
                "AND child.workspace_id = parent.workspace_id AND child.user_id = parent.user_id "
                "WHERE parent.id = %s", (run_id,),
            ).fetchone()
        result = row["result"] or {}
        if result.get("skill_undo"):
            return self._rollback_skill_publication(run_id)
        if result.get("run_id"):
            return None if row["child_status"] == "cancelled" else "rollback_child_incomplete"
        if result.get("action") in {"chat_answer", "conversation_reply", "markdown_edit", "markdown_create", "clarify", "reject"}:
            return None
        # 이 경로의 변경은 자식 생성 또는 Skill 저장과 같은 트랜잭션에 기록된다.
        # 실행 job이 종료된 뒤에도 두 기록이 없으면 취소 시점까지 저장한 변경이 없다.
        return None

    def _rollback_skill_publication(self, run_id: str) -> str | None:
        with database.connect_ai() as conn:
            run = conn.execute("SELECT result FROM agent_runs WHERE id = %s AND status = 'rolling_back' FOR UPDATE",
                               (run_id,)).fetchone()
            if run is None:
                return "rollback_turn_not_active"
            undo = run["result"]["skill_undo"]
            if undo["done"]:
                return None
            row = conn.execute("SELECT to_jsonb(s) AS snapshot FROM skills s WHERE id = %s FOR UPDATE",
                               (undo["skill_id"],)).fetchone()
            if row is None or row["snapshot"] != undo["after"]:
                return "rollback_skill_conflict"
            latest = conn.execute("SELECT id FROM skill_versions WHERE skill_id = %s ORDER BY version DESC LIMIT 1 FOR UPDATE",
                                  (undo["skill_id"],)).fetchone()
            used = conn.execute("SELECT 1 FROM agent_runs WHERE skill_version_id = %s LIMIT 1",
                                (undo["version_id"],)).fetchone()
            if latest is None or latest["id"] != undo["version_id"] or used:
                return "rollback_skill_conflict"
            before = undo["before"]
            if before is None:
                conn.execute("UPDATE skills SET enabled_version_id = NULL WHERE id = %s", (undo["skill_id"],))
                conn.execute("DELETE FROM skills WHERE id = %s", (undo["skill_id"],))
            else:
                conn.execute("UPDATE skills SET command = %s, enabled_version_id = %s, status = %s, "
                             "updated_at = %s::timestamptz WHERE id = %s",
                             (before["command"], before["enabled_version_id"], before["status"], before["updated_at"], undo["skill_id"]))
                conn.execute("DELETE FROM skill_versions WHERE id = %s", (undo["version_id"],))
            conn.execute("UPDATE agent_runs SET result = jsonb_set(result, '{skill_undo,done}', 'true'::jsonb), "
                         "updated_at = now() WHERE id = %s", (run_id,))
        return None

    def finish_rollback(self, run_id: str, error_code: str | None = None) -> None:
        with database.connect_ai() as conn:
            conn.execute(
                """
                UPDATE agent_runs SET status = %s, error_code = %s, updated_at = now(),
                    finished_at = CASE WHEN %s::text IS NULL THEN now() ELSE NULL END
                WHERE id = %s AND status = 'rolling_back'
                """,
                ("rollback_failed" if error_code else "cancelled", error_code, error_code, run_id),
            )

    def load_context(self, run_id: str) -> AgentRunContext:
        with database.connect_ai() as conn:
            row = conn.execute(
                """
                SELECT run.*, version.instructions_markdown, version.allowed_tools
                FROM agent_runs run
                LEFT JOIN skill_versions version ON version.id = run.skill_version_id
                WHERE run.id = %s
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            raise ValueError("AgentRun not found.")
        return AgentRunContext(
            run=_row_to_run(row),
            skill_instructions=row["instructions_markdown"],
            allowed_tools=tuple(row["allowed_tools"] or ()),
        )

    def mark_run_status(self, run_id: str, expected: tuple[str, ...], status: str) -> bool:
        with database.connect_ai() as conn:
            row = conn.execute(
                """
                UPDATE agent_runs SET status = %s, updated_at = now()
                WHERE id = %s AND status = ANY(%s) RETURNING id
                """,
                (status, run_id, list(expected)),
            ).fetchone()
        return row is not None

    def reserve_tool_call(self, run_id: str) -> bool:
        with database.connect_ai() as conn:
            row = conn.execute(
                """
                UPDATE agent_runs
                SET tool_call_count = tool_call_count + 1, updated_at = now()
                WHERE id = %s AND tool_call_count < 40 RETURNING tool_call_count
                """,
                (run_id,),
            ).fetchone()
        return row is not None

    def remaining_tool_calls(self, run_id: str) -> int:
        with database.connect_ai() as conn:
            row = conn.execute(
                "SELECT GREATEST(40 - tool_call_count, 0) AS remaining FROM agent_runs WHERE id = %s",
                (run_id,),
            ).fetchone()
        if row is None:
            raise ValueError("AgentRun not found.")
        return row["remaining"]

    def request_clarification(self, run_id: str, error_code: str) -> bool:
        with database.connect_ai() as conn:
            row = conn.execute(
                """
                UPDATE agent_runs
                SET status = 'clarification_required', error_code = %s, updated_at = now()
                WHERE id = %s AND status IN ('planning', 'executing') RETURNING id
                """,
                (error_code, run_id),
            ).fetchone()
        return row is not None

    def next_plan_version(self, run_id: str) -> int:
        with database.connect_ai() as conn:
            row = conn.execute(
                "SELECT COALESCE(max(version), 0) + 1 AS version FROM agent_plans WHERE run_id = %s",
                (run_id,),
            ).fetchone()
        return row["version"]

    def load_current_plan(self, run_id: str) -> AgentPlan:
        with database.connect_ai() as conn:
            run = conn.execute("SELECT current_plan_id FROM agent_runs WHERE id = %s", (run_id,)).fetchone()
            if run is None or run["current_plan_id"] is None:
                raise ValueError("AgentRun current plan not found.")
            plan = conn.execute("SELECT * FROM agent_plans WHERE id = %s", (run["current_plan_id"],)).fetchone()
            operations = conn.execute(
                "SELECT * FROM agent_plan_operations WHERE plan_id = %s ORDER BY sequence",
                (run["current_plan_id"],),
            ).fetchall()
        return _rows_to_plan(plan, operations)

    def mark_operation(self, operation_id: str, from_statuses: tuple[str, ...], status: str, error_code: str | None = None) -> bool:
        with database.connect_ai() as conn:
            row = conn.execute(
                """
                UPDATE agent_plan_operations
                SET status = %s, error_code = %s, updated_at = now()
                WHERE id = %s AND status = ANY(%s) RETURNING id
                """,
                (status, error_code, operation_id, list(from_statuses)),
            ).fetchone()
        return row is not None

    def save_tool_execution(
        self,
        *,
        run_id: str,
        plan_id: str,
        operation_id: str,
        tool_name: str,
        idempotency_key: str,
        attempt: int,
        status: str,
        response_metadata: dict[str, object],
        error_code: str | None,
    ) -> None:
        with database.connect_ai() as conn:
            conn.execute(
                """
                INSERT INTO agent_tool_executions (
                    id, run_id, plan_id, operation_id, tool_name, idempotency_key,
                    attempt, status, response_metadata, error_code, finished_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s = 'running' THEN NULL ELSE now() END)
                ON CONFLICT (idempotency_key) DO UPDATE
                SET attempt = CASE WHEN EXCLUDED.status = 'running'
                                   THEN agent_tool_executions.attempt + 1
                                   ELSE agent_tool_executions.attempt END,
                    status = EXCLUDED.status,
                    response_metadata = EXCLUDED.response_metadata,
                    error_code = EXCLUDED.error_code, finished_at = EXCLUDED.finished_at
                WHERE agent_tool_executions.status <> 'succeeded'
                """,
                (
                    str(uuid4()), run_id, plan_id, operation_id, tool_name, idempotency_key,
                    attempt, status, Json(response_metadata), error_code, status,
                ),
            )

    def load_operation_results(self, run_id: str, plan_id: str) -> dict[str, dict[str, object]]:
        with database.connect_ai() as conn:
            rows = conn.execute(
                """
                SELECT operation_id, response_metadata
                FROM agent_tool_executions
                WHERE run_id = %s AND plan_id = %s AND status = 'succeeded'
                """,
                (run_id, plan_id),
            ).fetchall()
        return {row["operation_id"]: row["response_metadata"] or {} for row in rows}

    def finish_run_from_operations(self, run_id: str) -> None:
        with database.connect_ai() as conn:
            counts = conn.execute(
                """
                SELECT operation.status, count(*) AS count
                FROM agent_plan_operations operation
                JOIN agent_runs run ON run.current_plan_id = operation.plan_id
                WHERE run.id = %s GROUP BY operation.status
                """,
                (run_id,),
            ).fetchall()
            values = {row["status"]: row["count"] for row in counts}
            if values.get("conflicted", 0):
                status = "conflicted"
            elif any(values.get(key, 0) for key in ("failed", "forbidden", "verification_failed", "skipped")):
                status = "partial_failed" if values.get("succeeded", 0) else "failed"
            else:
                status = "completed"
            conn.execute(
                """
                UPDATE agent_runs SET status = %s, updated_at = now(), finished_at = now()
                WHERE id = %s AND status = 'verifying'
                """,
                (status, run_id),
            )
