"""계획이 승인 대기에 들어가거나 AI 일이 멈추면 worker가 document-svc에 알린다.

자동 승인 판단과 사용량 수집은 document-svc가 한다.
"""

import json
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.modules.agent_run.domain.entities import AgentJob, AgentRunContext
from app.modules.agent_run.infrastructure.agent_worker import AgentWorker
from app.modules.agent_run.infrastructure.backend_tool_gateway import BackendToolGateway
from app.modules.agent_run.interfaces.http.schemas import AgentPlanResponse, ApproveAgentPlanRequest
from tests.modules.agent_run.test_agent_worker import _approved_plan, _executing_context


def _job(job_type: str = "planning") -> AgentJob:
    return AgentJob(
        id="job-1",
        run_id="run-1",
        job_type=job_type,
        attempt_count=1,
        lease_token="lease",
        leased_until=None,  # type: ignore[arg-type]
    )


def _context(status: str, action: str | None = None) -> AgentRunContext:
    context = _executing_context()
    run = replace(context.run, status=status, action=action or context.run.action)
    return replace(context, run=run)


def _worker(status: str, notifier: MagicMock | None, action: str | None = None) -> tuple[AgentWorker, MagicMock]:
    repository = MagicMock()
    repository.load_context.return_value = _context(status, action)
    worker = AgentWorker(repository, MagicMock(), MagicMock(), MagicMock(), plan_ready_notifier=notifier)
    return worker, repository


def test_planning_job_that_ends_awaiting_approval_notifies_document_service() -> None:
    notifier = MagicMock()
    worker, repository = _worker("awaiting_approval", notifier)

    with patch.object(worker, "_run_job"):
        worker.process(_job())

    repository.complete.assert_called_once()
    notifier.notify_plan_ready.assert_called_once_with(
        run_id="run-1", workspace_id="workspace-1", user_id="user-1"
    )


# document-svc AUTONOMOUS_TERMINAL과 같은 집합이다. cancel_requested·rolling_back은 곧 cancelled가 되므로 뺀다.
TERMINAL_STATUSES = ["completed", "partial_failed", "failed", "conflicted", "rejected", "cancelled", "rollback_failed"]


@pytest.mark.parametrize("status", [*TERMINAL_STATUSES, "clarification_required"])
@pytest.mark.parametrize("job_type", ["planning", "execution", "verification", "rollback"])
def test_job_that_leaves_run_stopped_notifies_once_for_usage_collection(job_type: str, status: str) -> None:
    notifier = MagicMock()
    worker, repository = _worker(status, notifier)

    with patch.object(worker, "_run_job"):
        worker.process(_job(job_type))

    repository.complete.assert_called_once()
    notifier.notify_plan_ready.assert_called_once_with(
        run_id="run-1", workspace_id="workspace-1", user_id="user-1"
    )


@pytest.mark.parametrize("status", ["queued", "planning", "executing", "verifying", "cancel_requested", "rolling_back"])
@pytest.mark.parametrize("job_type", ["planning", "execution"])
def test_no_notification_while_run_can_still_call_models(job_type: str, status: str) -> None:
    notifier = MagicMock()
    worker, _ = _worker(status, notifier)

    with patch.object(worker, "_run_job"):
        worker.process(_job(job_type))

    notifier.notify_plan_ready.assert_not_called()


def test_job_failure_that_closed_run_notifies_without_changing_job_result() -> None:
    notifier = MagicMock()
    notifier.notify_plan_ready.side_effect = RuntimeError("document-svc down")
    worker, repository = _worker("failed", notifier)

    with patch.object(worker, "_run_job", side_effect=ValueError("boom")):
        worker.process(_job())

    repository.fail.assert_called_once()
    repository.complete.assert_not_called()
    notifier.notify_plan_ready.assert_called_once()


def test_job_failure_that_will_retry_does_not_notify() -> None:
    notifier = MagicMock()
    worker, repository = _worker("planning", notifier)

    with patch.object(worker, "_run_job", side_effect=ValueError("boom")):
        worker.process(_job())

    repository.fail.assert_called_once()
    notifier.notify_plan_ready.assert_not_called()


def test_markdown_turn_recovery_does_not_notify() -> None:
    # 턴 사용량은 Kafka 결과·취소 흐름이 수집한다. document-svc의 AgentRun 조회 대상도 아니다.
    notifier = MagicMock()
    worker, _ = _worker("cancelled", notifier, action="markdown_turn")

    with patch.object(worker, "_run_job"):
        worker.process(_job("rollback"))

    notifier.notify_plan_ready.assert_not_called()


def test_notification_failure_on_stopped_run_keeps_job_completed_and_does_not_retry() -> None:
    notifier = MagicMock()
    notifier.notify_plan_ready.side_effect = RuntimeError("document-svc down")
    worker, repository = _worker("clarification_required", notifier)

    with patch.object(worker, "_run_job"):
        worker.process(_job())

    repository.complete.assert_called_once()
    repository.fail.assert_not_called()
    notifier.notify_plan_ready.assert_called_once()


def test_execution_and_failed_jobs_do_not_notify() -> None:
    notifier = MagicMock()
    worker, repository = _worker("awaiting_approval", notifier)

    with patch.object(worker, "_run_job"):
        worker.process(_job("execution"))
    with patch.object(worker, "_run_job", side_effect=ValueError("boom")):
        worker.process(_job())

    repository.fail.assert_called_once()
    notifier.notify_plan_ready.assert_not_called()


def test_notification_failure_keeps_plan_for_manual_approval_and_job_completed() -> None:
    notifier = MagicMock()
    notifier.notify_plan_ready.side_effect = RuntimeError("document-svc down")
    worker, repository = _worker("awaiting_approval", notifier)

    with patch.object(worker, "_run_job"):
        worker.process(_job())

    repository.complete.assert_called_once()
    repository.fail.assert_not_called()


def test_gateway_posts_plan_ready_with_agent_token() -> None:
    response = MagicMock()
    response.read.return_value = b'{"status":"executing"}'
    response.__enter__.return_value = response

    with patch(
        "app.modules.agent_run.infrastructure.backend_tool_gateway.urlopen",
        return_value=response,
    ) as urlopen:
        BackendToolGateway("http://document-svc:8080", "agent-token").notify_plan_ready(
            run_id="run-1", workspace_id="workspace-1", user_id="user-1"
        )

    request = urlopen.call_args.args[0]
    assert request.full_url == "http://document-svc:8080/internal/agent/runs/run-1/plan-ready"
    assert request.get_header("X-agent-service-token") == "agent-token"
    assert json.loads(request.data.decode("utf-8")) == {"workspace_id": "workspace-1", "user_id": "user-1"}


def test_approve_request_accepts_only_known_approvers() -> None:
    base = {"workspace_id": "workspace-1", "user_id": "user-1", "plan_version": 1, "operation_hash": "a" * 64}

    assert ApproveAgentPlanRequest(**base).approved_by == "user"
    assert ApproveAgentPlanRequest(**base, approved_by="auto:user-setting").approved_by == "auto:user-setting"
    with pytest.raises(ValidationError):
        ApproveAgentPlanRequest(**base, approved_by="auto:anything")


def test_plan_response_exposes_approver() -> None:
    plan = replace(_approved_plan(), approved_by="auto:user-setting")

    assert AgentPlanResponse.from_domain(plan).approved_by == "auto:user-setting"
    assert AgentPlanResponse.from_domain(_approved_plan()).approved_by is None
