import logging
import os
import signal
import time
from uuid import uuid4

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.modules.agent.infrastructure.backend_folder_source_reader import BackendFolderSourceReader
from app.modules.agent_run.domain.entities import AgentJob
from app.modules.agent_run.infrastructure.agent_worker import AgentWorker
from app.modules.agent_run.infrastructure.backend_tool_gateway import agent_backend_url, build_backend_tool_gateway
from app.modules.agent_run.infrastructure.chat_completions_plan_generator import build_plan_generator
from app.modules.agent_run.infrastructure.postgres_agent_job_repository import PostgresAgentJobRepository
from app.modules.agent_run.infrastructure.postgres_agent_run_repository import PostgresAgentRunRepository
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database


logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60
# 마지막 시도 중 죽은 job(lease만 남음)을 닫는 주기. lease가 90초라 그보다 자주 볼 필요는 없다.
_EXHAUSTED_LEASE_SWEEP_INTERVAL_SECONDS = 60
# SIGTERM 뒤 진행 중 job을 기다리는 상한. Pod terminationGracePeriodSeconds(900) - 60초 여유.
_DEFAULT_SHUTDOWN_BUDGET_SECONDS = 840


class ShutdownBudgetExceeded(BaseException):
    """종료 예산 안에 진행 중 job이 끝나지 않아 main thread를 멈춘다.

    Exception이 아니므로 job 실패 처리(AgentWorker.process의 except Exception)와 LLM 재시도를 건너뛰고
    finally만 실행된다. 그래서 사용량 원장(track_call)은 호출을 cancelled로 닫고, main은 job을 다시 대기열에 놓는다.
    """


class ShutdownRequest:
    """SIGTERM·SIGINT를 받으면 새 job을 더 받지 않고, 예산이 지나면 진행 중 job을 중단한다.

    python이 PID 1이라 처리기를 두지 않으면 SIGTERM이 무시되고 grace 끝에 SIGKILL로 끊긴다(2026-11-02 사고).
    """

    def __init__(self, budget_seconds: float) -> None:
        self.budget_seconds = budget_seconds
        self.requested = False

    def install(self) -> "ShutdownRequest":
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._request)
        signal.signal(signal.SIGALRM, _raise_budget_exceeded)
        return self

    def _request(self, signum: int, frame: object) -> None:
        if self.requested:
            return
        self.requested = True
        logger.info(
            "Agent worker 종료 요청(signal=%s): 새 job을 받지 않고 진행 중 job을 최대 %s초 기다립니다.",
            signum,
            self.budget_seconds,
        )
        signal.setitimer(signal.ITIMER_REAL, self.budget_seconds)


def _raise_budget_exceeded(signum: int, frame: object) -> None:
    raise ShutdownBudgetExceeded()


def _shutdown_budget_seconds() -> float:
    return float(os.environ.get("AGENT_WORKER_SHUTDOWN_BUDGET_SECONDS", str(_DEFAULT_SHUTDOWN_BUDGET_SECONDS)))


def _cleanup_expired_runs_if_due(
    repository: PostgresAgentJobRepository,
    checkpointer: PostgresSaver,
    next_cleanup_at: float,
    now: float,
) -> float:
    if now < next_cleanup_at:
        return next_cleanup_at
    run_ids = repository.list_expired_run_ids()
    for run_id in run_ids:
        checkpointer.delete_thread(run_id)
    deleted_count = repository.delete_expired_runs(run_ids)
    logger.info("Deleted %s expired AgentRun records.", deleted_count)
    return now + _CLEANUP_INTERVAL_SECONDS


def _fail_exhausted_leases_if_due(
    repository: PostgresAgentJobRepository,
    next_sweep_at: float,
    now: float,
) -> float:
    if now < next_sweep_at:
        return next_sweep_at
    closed = repository.fail_exhausted_leases()
    if closed:
        logger.warning("시도 한도에서 중단된 Agent job %s건을 실패로 닫았습니다.", closed)
    return now + _EXHAUSTED_LEASE_SWEEP_INTERVAL_SECONDS


def _build_folder_content_reader() -> BackendFolderSourceReader:
    """폴더 정리 계획의 본문 중복 확인용. tool gateway와 같은 document-svc 주소(agent_backend_url)·서비스 토큰을 쓴다.

    로컬 compose처럼 이 프로세스에 DOCUMENT_INTERNAL_BASE_URL을 두지 않는 배포가 있어
    task worker의 build_folder_source_reader를 쓰지 않는다. 토큰이 있는지는 build_backend_tool_gateway가 먼저 확인한다.
    """
    return BackendFolderSourceReader(
        agent_backend_url(),
        os.environ.get("AGENT_INTERNAL_TOKEN", ""),
        int(os.environ.get("AGENT_TOOL_TIMEOUT_SECONDS", "30")),
    )


def main(shutdown: ShutdownRequest | None = None) -> None:
    shutdown = shutdown or ShutdownRequest(_shutdown_budget_seconds())
    database.verify_schema()
    database.verify_agent_schema()
    repository = PostgresAgentJobRepository()
    with database.connect_ai() as connection:
        connection.autocommit = True
        checkpointer = PostgresSaver(
            connection,
            serde=JsonPlusSerializer(allowed_msgpack_modules=()),
        )
        run_repository = PostgresAgentRunRepository()
        tool_gateway = build_backend_tool_gateway()
        folder_content_reader = _build_folder_content_reader()
        worker_id = os.environ.get("AGENT_WORKER_ID", f"agent-worker-{uuid4()}")
        poll_seconds = float(os.environ.get("AGENT_WORKER_POLL_SECONDS", "1"))
        next_cleanup_at = 0.0
        next_sweep_at = 0.0
        while not shutdown.requested:
            next_cleanup_at = _cleanup_expired_runs_if_due(
                repository,
                checkpointer,
                next_cleanup_at,
                time.monotonic(),
            )
            next_sweep_at = _fail_exhausted_leases_if_due(repository, next_sweep_at, time.monotonic())
            job = repository.claim_next(worker_id)
            if job is None:
                time.sleep(poll_seconds)
                continue
            try:
                _process_job(
                    job,
                    repository=repository,
                    run_repository=run_repository,
                    tool_gateway=tool_gateway,
                    checkpointer=checkpointer,
                    folder_content_reader=folder_content_reader,
                )
            except ShutdownBudgetExceeded:
                released = repository.release_for_shutdown(job)
                logger.warning(
                    "종료 예산 %s초 안에 끝내지 못한 Agent job을 대기열로 돌려놓았습니다: job_id=%s released=%s",
                    shutdown.budget_seconds,
                    job.id,
                    released,
                )
                break
    if shutdown.requested:
        signal.setitimer(signal.ITIMER_REAL, 0)
        logger.info("Agent worker를 종료합니다.")


def _process_job(
    job: AgentJob,
    *,
    repository: PostgresAgentJobRepository,
    run_repository: PostgresAgentRunRepository,
    tool_gateway,
    checkpointer: PostgresSaver,
    folder_content_reader: BackendFolderSourceReader,
) -> None:
    try:
        plan_generator = None
        if job.job_type != "rollback":
            context = repository.load_context(job.run_id)
            if context.run.provider is None or context.run.model is None:
                repository.fail(job, "missing_llm_selection")
                return
            plan_generator = build_plan_generator(
                provider=context.run.provider,
                model=context.run.model,
            )
        worker = AgentWorker(
            repository=repository,
            run_repository=run_repository,
            tool_gateway=tool_gateway,
            plan_generator=plan_generator,
            checkpointer=checkpointer,
            folder_content_reader=folder_content_reader,
            plan_ready_notifier=tool_gateway,
        )
    except Exception as exc:
        logger.exception("Agent worker setup 실패: job_id=%s", job.id)
        repository.fail(job, type(exc).__name__)
        return
    worker.process(job)


if __name__ == "__main__":
    # signal 처리기는 main thread에서만 둘 수 있어 진입점에서 설치한다.
    main(ShutdownRequest(_shutdown_budget_seconds()).install())
