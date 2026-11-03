"""계획이 승인 대기에 들어가면 worker가 document-svc에 알린다. 자동 승인 판단은 document-svc가 한다."""

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


def _context(status: str) -> AgentRunContext:
    context = _executing_context()
    return replace(context, run=replace(context.run, status=status))


def _worker(status: str, notifier: MagicMock | None) -> tuple[AgentWorker, MagicMock]:
    repository = MagicMock()
    repository.load_context.return_value = _context(status)
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


@pytest.mark.parametrize("status", ["clarification_required", "failed", "executing"])
def test_no_notification_when_plan_is_not_awaiting_approval(status: str) -> None:
    notifier = MagicMock()
    worker, _ = _worker(status, notifier)

    with patch.object(worker, "_run_job"):
        worker.process(_job())

    notifier.notify_plan_ready.assert_not_called()


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
