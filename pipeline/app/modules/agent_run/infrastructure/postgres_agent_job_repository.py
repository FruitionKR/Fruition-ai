from __future__ import annotations

from contextlib import contextmanager
import json
import unicodedata
from typing import Any
from uuid import uuid4

from psycopg.types.json import Json

from app.modules.agent_run.domain.compensation import mcp_receipt_result
from app.modules.agent_run.domain.entities import (
    AgentJob,
    AgentRunContext,
    McpExecutionAttempt,
    McpExecutionIdentity,
)
from app.modules.agent_run.domain.plan import (
    NON_RETRYABLE_PLAN_FAILURE_CODES,
    NOT_SELECTED_ERROR_CODE,
    ROLLBACK_DOCUMENT_PURGED,
    AgentPlan,
    AgentPlanOperation,
)
from app.modules.agent_run.infrastructure.postgres_agent_run_repository import _row_to_run, _rows_to_plan
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database

# claim_next가 job을 다시 고르는 시도 한도. 마지막 시도에서 worker가 죽으면 fail_exhausted_leases가 닫는다.
MAX_JOB_ATTEMPTS = 3
# document-svc가 이미 "다시 시도해 주세요" 문구로 보여 주는 기존 코드(AiErrorMessages)를 그대로 쓴다.
INTERRUPTED_ATTEMPTS_EXHAUSTED = "interrupted_attempts_exhausted"
_EXPIRED_RUN_STATUSES = ["completed", "partial_failed", "failed", "conflicted", "rejected", "cancelled"]
# 90일 정리 대상. 영구 삭제로 끝난 rollback_failed는 다시 시도해도 같아 끝난 run으로 본다. 다른 rollback_failed는 복구를 기다린다.
_EXPIRED_RUN_CONDITION = """
    finished_at < now() - interval '90 days'
    AND (status = ANY(%(statuses)s) OR (status = 'rollback_failed' AND error_code = %(purged)s))
    AND NOT EXISTS (
        SELECT 1 FROM agent_mcp_tool_attempts attempt
        WHERE attempt.run_id = agent_runs.id AND attempt.status IN ('started', 'unknown')
    )
"""


def _mcp_create_identity(conn, run_id: str, tool_name: str, arguments: dict[str, object]):
    if tool_name not in {"create_folder", "create_document"}:
        return None
    run = conn.execute(
        "SELECT workspace_id, user_id FROM agent_runs WHERE id = %s", (run_id,)
    ).fetchone()
    if run is None:
        raise ValueError("MCP run was not found.")
    name_key = "name" if tool_name == "create_folder" else "display_name"
    destination_key = "parent_folder_id" if tool_name == "create_folder" else "folder_id"
    name = arguments.get(name_key)
    destination = arguments.get(destination_key)
    if not isinstance(name, str) or (destination is not None and not isinstance(destination, str)):
        raise ValueError("Resolved create identity is invalid.")
    normalized = unicodedata.normalize("NFC", name).strip().casefold()
    if tool_name == "create_document":
        normalized = normalized.removesuffix(".md").strip()
    return {
        "workspace_id": run["workspace_id"],
        "user_id": run["user_id"],
        "tool_name": tool_name,
        "destination": destination or "<root>",
        "normalized_name": normalized,
        "content_hash": arguments.get("content_hash") if tool_name == "create_document" else None,
    }


def _mcp_create_lock_keys(identity: dict[str, object]) -> tuple[str, ...]:
    common = {
        key: identity[key]
        for key in ("workspace_id", "user_id", "tool_name", "destination")
    }
    values = [json.dumps({**common, "name": identity["normalized_name"]}, sort_keys=True)]
    if identity.get("content_hash") is not None:
        values.append(json.dumps({**common, "content_hash": identity["content_hash"]}, sort_keys=True))
    return tuple(sorted("mcp-create:" + value for value in values))


class PostgresAgentJobRepository:
    def list_expired_run_ids(self, limit: int = 100) -> tuple[str, ...]:
        with database.connect_ai() as conn:
            rows = conn.execute(
                f"SELECT id FROM agent_runs WHERE {_EXPIRED_RUN_CONDITION} ORDER BY finished_at, id LIMIT %(limit)s",
                {"statuses": _EXPIRED_RUN_STATUSES, "purged": ROLLBACK_DOCUMENT_PURGED, "limit": limit},
            ).fetchall()
        return tuple(row["id"] for row in rows)

    def delete_expired_runs(self, run_ids: tuple[str, ...]) -> int:
        if not run_ids:
            return 0
        with database.connect_ai() as conn:
            rows = conn.execute(
                f"DELETE FROM agent_runs WHERE id = ANY(%(ids)s) AND {_EXPIRED_RUN_CONDITION} RETURNING id",
                {"ids": list(run_ids), "statuses": _EXPIRED_RUN_STATUSES, "purged": ROLLBACK_DOCUMENT_PURGED},
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
        with database.connect_ai() as conn:
            _fail_leased_job(conn, job, error_code)

    def release_for_shutdown(self, job: AgentJob) -> bool:
        """종료 예산 안에 끝내지 못한 job을 다른 worker가 바로 이어 받게 놓는다.

        Pod 종료는 job 실패가 아니므로 claim 때 늘린 attempt_count를 되돌린다. 그러지 않으면 rollout이
        여러 번 겹친 job이 재시도 한도를 다 쓰고 실패로 닫힌다. lease가 이미 바뀌었으면 아무것도 하지 않는다.
        """
        with database.connect_ai() as conn:
            row = conn.execute(
                """
                UPDATE agent_jobs
                SET status = 'queued', lease_owner = NULL, lease_token = NULL, leased_until = NULL,
                    heartbeat_at = NULL, attempt_count = GREATEST(attempt_count - 1, 0),
                    available_at = now(), updated_at = now()
                WHERE id = %s AND status = 'leased' AND lease_token = %s
                RETURNING id
                """,
                (job.id, job.lease_token),
            ).fetchone()
        return row is not None

    def fail_exhausted_leases(self, error_code: str = INTERRUPTED_ATTEMPTS_EXHAUSTED, limit: int = 50) -> int:
        """마지막 시도 중 worker가 죽어 lease만 남은 job을 정상 실패와 같은 경로로 닫는다.

        claim_next는 시도 한도에 닿은 job을 다시 고르지 않으므로(rollback 제외) 이 job과 run은 스스로
        끝나지 않는다. SKIP LOCKED와 lease_token 조건부 UPDATE로 여러 worker가 동시에 돌아도 한 번만 닫는다.
        """
        with database.connect_ai() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM agent_jobs
                WHERE status = 'leased' AND leased_until < now()
                  AND attempt_count >= %s
                  AND job_type IN ('planning', 'execution')
                ORDER BY leased_until, id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
                """,
                (MAX_JOB_ATTEMPTS, limit),
            ).fetchall()
            return sum(_fail_leased_job(conn, _row_to_job(row), error_code) for row in rows)

    @contextmanager
    def execution_lock(self, run_id: str, *, wait: bool = True):
        # lease가 만료돼도 이전 worker가 살아 있으면 같은 run을 동시에 실행하지 않는다.
        with database.connect_ai() as conn:
            if wait:
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"agent:{run_id}",))
                yield True
            else:
                locked = conn.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS locked",
                    (f"agent:{run_id}",),
                ).fetchone()["locked"]
                yield locked

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
                "SELECT parent.result, child.status AS child_status, child.error_code AS child_error_code FROM agent_runs parent "
                "LEFT JOIN agent_runs child ON child.id = parent.result->>'run_id' "
                "AND child.workspace_id = parent.workspace_id AND child.user_id = parent.user_id "
                "WHERE parent.id = %s", (run_id,),
            ).fetchone()
        result = row["result"] or {}
        if result.get("skill_undo"):
            return self._rollback_skill_publication(run_id)
        if result.get("run_id"):
            if row["child_status"] == "rollback_failed" and row["child_error_code"] == ROLLBACK_DOCUMENT_PURGED:
                return ROLLBACK_DOCUMENT_PURGED
            return None if row["child_status"] == "cancelled" else "rollback_child_incomplete"
        if result.get("action") in {"chat_answer", "conversation_reply", "product_help", "markdown_edit", "markdown_create", "clarify", "reject"}:
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
                    finished_at = CASE WHEN %s::text IS NULL OR %s::text = %s THEN now() ELSE NULL END
                WHERE id = %s AND status = 'rolling_back'
                """,
                ("rollback_failed" if error_code else "cancelled", error_code, error_code, error_code,
                 ROLLBACK_DOCUMENT_PURGED, run_id),
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

    def request_clarification(
        self,
        run_id: str,
        error_code: str,
        question: str | None = None,
    ) -> bool:
        with database.connect_ai() as conn:
            row = conn.execute(
                """
                UPDATE agent_runs
                SET status = 'clarification_required', error_code = %s,
                    clarification_question = %s, updated_at = now()
                WHERE id = %s AND status IN ('planning', 'executing') RETURNING id
                """,
                (error_code, question, run_id),
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

    def load_mcp_execution_identity(
        self, run_id: str, plan_id: str, operation_id: str
    ) -> McpExecutionIdentity | None:
        with database.connect_ai() as conn:
            row = conn.execute(
                """
                SELECT approval.id AS approval_id, approval.grant_id, approval.client_id,
                       approval.policy_generation, approval.operation_ids
                FROM agent_approvals approval
                JOIN agent_runs run ON run.id = approval.run_id
                WHERE approval.run_id = %s AND approval.plan_id = %s
                  AND %s = ANY(approval.operation_ids)
                  AND approval.decision = 'approved'
                  AND approval.schema_version = 'mcp.agent-approval.v1'
                  AND run.origin = 'MCP_CLIENT'
                """,
                (run_id, plan_id, operation_id),
            ).fetchone()
        return McpExecutionIdentity(
            approval_id=row["approval_id"],
            grant_id=row["grant_id"],
            client_id=row["client_id"],
            policy_generation=row["policy_generation"],
            operation_ids=tuple(row["operation_ids"]),
        ) if row else None

    def get_or_create_mcp_attempt(
        self, run_id: str, plan_id: str, operation_id: str, tool_name: str,
        arguments: dict[str, object],
    ) -> McpExecutionAttempt:
        created = False
        with database.connect_ai() as conn:
            row = conn.execute(
                """SELECT attempt_id, attempt, status FROM agent_mcp_tool_attempts
                   WHERE run_id = %s AND plan_id = %s AND operation_id = %s
                   ORDER BY attempt LIMIT 1 FOR UPDATE""",
                (run_id, plan_id, operation_id),
            ).fetchone()
            if row is None:
                approval = conn.execute(
                    """SELECT approval.id FROM agent_approvals approval
                       JOIN agent_runs run ON run.id = approval.run_id
                       JOIN agent_plan_operations operation ON operation.plan_id = approval.plan_id
                       WHERE approval.run_id = %s AND approval.plan_id = %s
                         AND %s = ANY(approval.operation_ids)
                         AND approval.schema_version = 'mcp.agent-approval.v1'
                         AND operation.id = %s AND operation.tool_name = %s
                         AND operation.status = 'running' AND run.status = 'executing'
                       FOR UPDATE OF run""",
                    (run_id, plan_id, operation_id, operation_id, tool_name),
                ).fetchone()
                if approval is None:
                    raise ValueError("Approved MCP execution identity was not found.")
                create_identity = _mcp_create_identity(conn, run_id, tool_name, arguments)
                if create_identity is not None:
                    for key in _mcp_create_lock_keys(create_identity):
                        conn.execute(
                            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,)
                        )
                    prior = conn.execute(
                        """SELECT prior.attempt_id, prior.status, prior.receipt
                           FROM agent_mcp_tool_attempts prior
                           WHERE prior.operation_id <> %s
                             AND (prior.status IN ('started', 'unknown') OR (
                               prior.status = 'committed' AND prior.attempt_id = (
                                 SELECT response_metadata->>'mcp_blocked_attempt_id'
                                 FROM agent_tool_executions
                                 WHERE run_id = %s AND plan_id = %s AND operation_id = %s
                                   AND idempotency_key = 'undo:' || operation_id
                               )
                             ))
                             AND prior.create_identity->>'workspace_id' = %s
                             AND prior.create_identity->>'user_id' = %s
                             AND prior.create_identity->>'tool_name' = %s
                             AND prior.create_identity->>'destination' = %s
                             AND (
                               prior.create_identity->>'normalized_name' = %s
                               OR (%s::text IS NOT NULL AND prior.create_identity->>'content_hash' = %s)
                             )
                           ORDER BY (prior.status = 'committed') DESC, prior.created_at
                           LIMIT 1 FOR UPDATE""",
                        (
                            operation_id, run_id, plan_id, operation_id,
                            create_identity["workspace_id"], create_identity["user_id"],
                            tool_name, create_identity["destination"],
                            create_identity["normalized_name"], create_identity.get("content_hash"),
                            create_identity.get("content_hash"),
                        ),
                    ).fetchone()
                    if prior is not None:
                        if prior["status"] != "committed":
                            linked = conn.execute(
                                """UPDATE agent_tool_executions
                                   SET response_metadata = response_metadata || %s::jsonb
                                   WHERE run_id = %s AND plan_id = %s AND operation_id = %s
                                     AND idempotency_key = 'undo:' || operation_id
                                   RETURNING id""",
                                (Json({"mcp_blocked_attempt_id": prior["attempt_id"]}),
                                 run_id, plan_id, operation_id),
                            ).fetchone()
                            if linked is None:
                                raise ValueError("MCP create hold requires its prepared undo record.")
                        return McpExecutionAttempt(
                            prior["attempt_id"], 0,
                            "existing" if prior["status"] == "committed" else "held",
                            False, prior["receipt"],
                        )
                row = conn.execute(
                    """INSERT INTO agent_mcp_tool_attempts (
                           attempt_id, approval_id, run_id, plan_id, operation_id, tool_name,
                           create_identity, attempt, status
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 'started')
                       RETURNING attempt_id, attempt, status""",
                    (
                        str(uuid4()), approval["id"], run_id, plan_id, operation_id, tool_name,
                        Json(create_identity) if create_identity is not None else None,
                    ),
                ).fetchone()
                created = True
        return McpExecutionAttempt(**row, created=created)

    def finish_mcp_attempt(
        self, attempt_id: str, status: str, receipt: dict[str, object] | None,
        error_code: str | None,
    ) -> None:
        with database.connect_ai() as conn:
            conn.execute(
                """UPDATE agent_mcp_tool_attempts
                   SET status = %s, receipt = %s, error_code = %s, updated_at = now()
                   WHERE attempt_id = %s AND status IN ('started', 'unknown')""",
                (status, Json(receipt) if receipt is not None else None, error_code, attempt_id),
            )

    def claim_mcp_reconciliation_batch(self, limit: int = 20) -> list[dict[str, Any]]:
        with database.connect_ai() as conn:
            return conn.execute(
                """WITH candidates AS (
                       SELECT attempt_id FROM agent_mcp_tool_attempts
                       WHERE status IN ('started', 'unknown')
                         AND updated_at < now() - interval '1 minute'
                       ORDER BY updated_at, attempt_id LIMIT %s FOR UPDATE SKIP LOCKED
                   )
                   UPDATE agent_mcp_tool_attempts attempt SET updated_at = now()
                   FROM candidates WHERE attempt.attempt_id = candidates.attempt_id
                   RETURNING attempt.*""", (limit,),
            ).fetchall()

    def record_mcp_reconciliation(
        self, attempt_id: str, status: str, receipt: dict[str, object] | None,
    ) -> None:
        if status not in {"committed", "no_change", "failed"}:
            raise ValueError("Only a definitive MCP lookup outcome can be reconciled.")
        with database.connect_ai() as conn:
            attempt = conn.execute(
                """UPDATE agent_mcp_tool_attempts SET status = %s, receipt = %s,
                          error_code = %s, updated_at = now()
                   WHERE attempt_id = %s AND status IN ('started', 'unknown') RETURNING *""",
                (status, Json(receipt) if receipt is not None else None,
                 "mcp_execution_failed" if status == "failed" else None, attempt_id),
            ).fetchone()
            if attempt is None or receipt is None:
                return
            # Receipt and forward evidence commit together; never restart or relabel a terminal run.
            conn.execute(
                """UPDATE agent_tool_executions SET status = 'succeeded', response_metadata = %s,
                          error_code = NULL, finished_at = now()
                   WHERE run_id = %s AND plan_id = %s AND operation_id = %s
                     AND idempotency_key = %s AND status <> 'succeeded'""",
                (Json(mcp_receipt_result(attempt["tool_name"], receipt)), attempt["run_id"],
                 attempt["plan_id"], attempt["operation_id"],
                 f"agent:{attempt['run_id']}:{attempt['plan_id']}:{attempt['operation_id']}"),
            )

    def halt_mcp_run(self, run_id: str, reason: str) -> None:
        with database.connect_ai() as conn:
            conn.execute(
                """UPDATE agent_plan_operations SET status = 'forbidden', error_code = %s, updated_at = now()
                   WHERE plan_id = (SELECT current_plan_id FROM agent_runs WHERE id = %s)
                     AND status IN ('pending', 'running')""",
                (reason, run_id),
            )
            conn.execute(
                """UPDATE agent_runs SET status = 'halted', error_code = %s,
                          updated_at = now(), finished_at = now()
                   WHERE id = %s AND origin = 'MCP_CLIENT' AND status IN ('executing', 'verifying')""",
                (reason, run_id),
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
            # 부분 승인에서 고르지 않은 작업(skipped·not_selected)은 실패가 아니므로 세지 않는다.
            counts = conn.execute(
                """
                SELECT operation.status, count(*) AS count
                FROM agent_plan_operations operation
                JOIN agent_runs run ON run.current_plan_id = operation.plan_id
                WHERE run.id = %s
                  AND NOT (operation.status = 'skipped' AND operation.error_code IS NOT DISTINCT FROM %s)
                GROUP BY operation.status
                """,
                (run_id, NOT_SELECTED_ERROR_CODE),
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


def _row_to_job(row: dict[str, Any]) -> AgentJob:
    return AgentJob(
        id=row["id"],
        run_id=row["run_id"],
        job_type=row["job_type"],
        attempt_count=row["attempt_count"],
        lease_token=row["lease_token"],
        leased_until=row["leased_until"],
    )


def _fail_leased_job(conn: Any, job: AgentJob, error_code: str) -> bool:
    """lease를 쥔 job의 실패를 기록한다. 재시도 한도에 닿았으면 run까지 실패로 닫는다. 기록했으면 True."""
    terminal = job.attempt_count >= MAX_JOB_ATTEMPTS or error_code in NON_RETRYABLE_PLAN_FAILURE_CODES
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
        return False
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
                      'cancel_requested', 'rolling_back', 'rollback_failed', 'halted'
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
                      'cancel_requested', 'rolling_back', 'rollback_failed', 'halted'
              )
            """,
            (error_code, job.run_id),
        )
    return True
