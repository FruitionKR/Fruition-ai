import logging
import os
import time
from uuid import uuid4

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.modules.agent_run.infrastructure.agent_worker import AgentWorker
from app.modules.agent_run.infrastructure.backend_tool_gateway import build_backend_tool_gateway
from app.modules.agent_run.infrastructure.chat_completions_plan_generator import build_plan_generator
from app.modules.agent_run.infrastructure.postgres_agent_job_repository import PostgresAgentJobRepository
from app.modules.agent_run.infrastructure.postgres_agent_run_repository import PostgresAgentRunRepository
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database


logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60


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


def main() -> None:
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
        worker_id = os.environ.get("AGENT_WORKER_ID", f"agent-worker-{uuid4()}")
        poll_seconds = float(os.environ.get("AGENT_WORKER_POLL_SECONDS", "1"))
        next_cleanup_at = 0.0
        while True:
            next_cleanup_at = _cleanup_expired_runs_if_due(
                repository,
                checkpointer,
                next_cleanup_at,
                time.monotonic(),
            )
            job = repository.claim_next(worker_id)
            if job is None:
                time.sleep(poll_seconds)
                continue
            try:
                plan_generator = None
                if job.job_type != "rollback":
                    context = repository.load_context(job.run_id)
                    if context.run.provider is None or context.run.model is None:
                        repository.fail(job, "missing_llm_selection")
                        continue
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
                )
            except Exception as exc:
                logger.exception("Agent worker setup 실패: job_id=%s", job.id)
                repository.fail(job, type(exc).__name__)
                continue
            worker.process(job)


if __name__ == "__main__":
    main()
