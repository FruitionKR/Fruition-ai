"""Resolve uncertain attempts through receipt reads, including after run termination."""

import logging

from app.modules.agent_run.infrastructure.agent_worker import AgentWorker

logger = logging.getLogger(__name__)


def reconcile_mcp_attempts(repository, gateway) -> int:
    resolved = 0
    for attempt in repository.claim_mcp_reconciliation_batch():
        try:
            with repository.execution_lock(attempt["run_id"], wait=False) as locked:
                if not locked:
                    continue
                context = repository.load_context(attempt["run_id"])
                plan = repository.load_current_plan(attempt["run_id"])
                if context.run.origin != "MCP_CLIENT" or plan.id != attempt["plan_id"]:
                    continue
                operation = next(op for op in plan.operations if op.id == attempt["operation_id"])
                identity = repository.load_mcp_execution_identity(context.run.id, plan.id, operation.id)
                if identity is None:
                    continue
                lookup = gateway.lookup_mcp_receipt(
                    identity, operation.id, context.run.workspace_id, context.run.user_id,
                )
                if not isinstance(lookup, dict) or set(lookup) != {"status", "receipt"}:
                    continue
                status, receipt = lookup["status"], lookup["receipt"]
                if status == "FAILED" and receipt is None:
                    repository.record_mcp_reconciliation(attempt["attempt_id"], "failed", None)
                elif status in {"COMMITTED", "NO_CHANGE"}:
                    undo = repository.get_undo_record(context.run.id, operation.id)
                    if undo is None:
                        continue
                    receipt = AgentWorker._validate_mcp_receipt(
                        receipt, context, operation, undo["response_metadata"]["arguments"],
                        identity, attempt["attempt_id"],
                    )
                    if receipt["outcome"] != status:
                        continue
                    repository.record_mcp_reconciliation(
                        attempt["attempt_id"], "committed" if status == "COMMITTED" else "no_change", receipt,
                    )
                else:
                    continue
                resolved += 1
        except Exception as exc:
            # Claiming advances updated_at so an unavailable receipt cannot starve later attempts.
            logger.warning("MCP receipt reconciliation deferred: attempt_id=%s type=%s",
                           attempt["attempt_id"], type(exc).__name__)
    return resolved
