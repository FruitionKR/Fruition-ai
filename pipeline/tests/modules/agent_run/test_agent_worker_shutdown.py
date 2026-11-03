"""agent worker 종료(SIGTERM drain)와 시도 한도에서 중단된 job 정리를 실제 PostgreSQL로 검증한다.

TEST_AGENT_DATABASE_URL을 지정하면 격리 스키마에 현행 ai_schema.sql을 적용한다. 종료 시나리오는 실제 진입점을
별도 프로세스(main thread에 signal 처리기 설치)로 띄우고 실제 SIGTERM을 보낸다.
"""

import os
import select
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from app.modules.agent_run.domain.entities import AgentRun

_PIPELINE_ROOT = Path(__file__).resolve().parents[3]

# 실제 agent_worker.main을 띄운다. 외부 의존(document-svc·LLM)만 바꾸고 job claim·AgentWorker.process·
# LangGraph·사용량 원장·lease 처리는 그대로 쓴다. 계획 생성은 TEST_PLAN_SECONDS 동안 모델 호출 중인 것처럼 멈춘다.
_WORKER_SCRIPT = '''
import os, sys, time
from unittest.mock import MagicMock
import psycopg
from psycopg.rows import dict_row

import agent_worker
from app.core import ai_database
from app.modules.agent_run.domain.plan import AgentPlanIntentClarificationRequired
from app.modules.agent_run.infrastructure.agent_worker import AgentWorker
from app.modules.model_usage.infrastructure.usage_ledger import track_call
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database


def connect():
    return psycopg.connect(os.environ["TEST_AGENT_DATABASE_URL"], row_factory=dict_row,
                           options="-csearch_path=" + os.environ["TEST_SCHEMA"])


class SlowPlanGenerator:
    def generate(self, *, run_id, **kwargs):
        print("generating " + run_id, flush=True)
        with track_call("openai", "gpt-test"):
            time.sleep(float(os.environ["TEST_PLAN_SECONDS"]))
        raise AgentPlanIntentClarificationRequired("무엇을 바꿀지 알려 주세요.")


database.connect_ai = connect
ai_database.connect_ai = connect
database.verify_schema = lambda: None
database.verify_agent_schema = lambda: None
agent_worker.PostgresSaver = lambda *args, **kwargs: None
agent_worker.build_backend_tool_gateway = MagicMock
agent_worker.build_plan_generator = lambda **kwargs: SlowPlanGenerator()
AgentWorker._inspect_hierarchy = lambda self, context: []
agent_worker.main(agent_worker.ShutdownRequest(agent_worker._shutdown_budget_seconds()).install())
print("exited", flush=True)
'''


@pytest.fixture
def ai_db(monkeypatch) -> Iterator[tuple[str, str, object]]:
    dsn = os.environ.get("TEST_AGENT_DATABASE_URL")
    if not dsn:
        pytest.skip("격리 PostgreSQL 테스트 URL이 지정되지 않았습니다.")
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database

    schema = "test_agent_shutdown_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f"-csearch_path={schema}")

    with connect() as conn:
        conn.execute((_PIPELINE_ROOT / "db/ai_schema.sql").read_text())
    monkeypatch.setattr(database, "connect_ai", connect)
    try:
        yield dsn, schema, connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _queue_run(run_id: str) -> None:
    from app.modules.agent_run.infrastructure.postgres_agent_run_repository import PostgresAgentRunRepository

    PostgresAgentRunRepository().create_with_planning_job(
        AgentRun(
            id=run_id,
            workspace_id="ws",
            user_id="user",
            action="folder_organize",
            skill_version_id=None,
            status="queued",
            request_summary="정리",
            provider="openai",
            model="gpt-test",
        ),
        f"{run_id}-planning",
    )


def _job(connect, job_id: str) -> dict:
    with connect() as conn:
        return conn.execute(
            "SELECT status, attempt_count, lease_token, leased_until FROM agent_jobs WHERE id = %s", (job_id,)
        ).fetchone()


def _run(connect, run_id: str) -> dict:
    with connect() as conn:
        return conn.execute(
            "SELECT status, error_code, finished_at FROM agent_runs WHERE id = %s", (run_id,)
        ).fetchone()


def _start_worker(dsn: str, schema: str, *, plan_seconds: float, budget_seconds: float) -> subprocess.Popen:
    env = {
        **os.environ,
        "TEST_AGENT_DATABASE_URL": dsn,
        "TEST_SCHEMA": schema,
        "TEST_PLAN_SECONDS": str(plan_seconds),
        "AGENT_WORKER_SHUTDOWN_BUDGET_SECONDS": str(budget_seconds),
        "AGENT_WORKER_POLL_SECONDS": "0.1",
    }
    return subprocess.Popen(
        [sys.executable, "-u", "-c", _WORKER_SCRIPT],
        env=env,
        cwd=_PIPELINE_ROOT,
        stdout=subprocess.PIPE,
        text=True,
    )


def _read_line(process: subprocess.Popen, timeout: float) -> str:
    assert select.select([process.stdout], [], [], timeout)[0], "worker 출력 대기 시간 초과"
    return process.stdout.readline().strip()


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)
    process.stdout.close()


def test_sigterm_stops_claiming_and_lets_in_flight_job_finish(ai_db) -> None:
    dsn, schema, connect = ai_db
    _queue_run("run-a")
    _queue_run("run-b")
    process = _start_worker(dsn, schema, plan_seconds=2, budget_seconds=60)
    try:
        assert _read_line(process, 30) == "generating run-a"
        process.send_signal(signal.SIGTERM)

        assert process.wait(timeout=30) == 0
        assert process.stdout.read().split() == ["exited"]
    finally:
        _stop(process)

    # 진행 중이던 job은 끝까지 처리되고(계획 질문으로 완료), 종료 요청 뒤에는 다음 job을 잡지 않는다.
    assert _job(connect, "run-a-planning")["status"] == "completed"
    assert _run(connect, "run-a")["status"] == "clarification_required"
    waiting = _job(connect, "run-b-planning")
    assert (waiting["status"], waiting["attempt_count"]) == ("queued", 0)
    with connect() as conn:
        calls = conn.execute("SELECT run_id, status FROM ai_model_usage ORDER BY started_at").fetchall()
    assert calls == [{"run_id": "run-a", "status": "succeeded"}]


def test_shutdown_budget_exceeded_requeues_job_and_cancels_usage_call(ai_db) -> None:
    dsn, schema, connect = ai_db
    _queue_run("run-a")
    process = _start_worker(dsn, schema, plan_seconds=600, budget_seconds=1)
    try:
        assert _read_line(process, 30) == "generating run-a"
        process.send_signal(signal.SIGTERM)

        # 모델 호출이 끝나기를 600초 기다리지 않고 예산(1초) 뒤에 정리하고 0으로 끝난다.
        assert process.wait(timeout=30) == 0
        assert process.stdout.read().split() == ["exited"]
    finally:
        _stop(process)

    # Pod 종료는 job 실패가 아니므로 시도 횟수를 되돌리고 lease를 놓아 다른 worker가 바로 이어 받는다.
    job = _job(connect, "run-a-planning")
    assert (job["status"], job["attempt_count"], job["lease_token"], job["leased_until"]) == ("queued", 0, None, None)
    assert _run(connect, "run-a")["status"] == "planning"
    # 사용량 원장 호출은 started로 남지 않고 cancelled로 닫힌다(1시간 뒤 abandoned 처리·토큰 유실 방지).
    with connect() as conn:
        calls = conn.execute("SELECT status, finished_at FROM ai_model_usage").fetchall()
    assert len(calls) == 1 and calls[0]["status"] == "cancelled" and calls[0]["finished_at"] is not None

    from app.modules.agent_run.infrastructure.postgres_agent_job_repository import PostgresAgentJobRepository

    reclaimed = PostgresAgentJobRepository().claim_next("next-worker")
    assert reclaimed is not None and reclaimed.id == "run-a-planning" and reclaimed.attempt_count == 1


def _lease(connect, job_id: str, *, attempt_count: int, expires_in: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE agent_jobs
            SET status = 'leased', attempt_count = %s, lease_owner = 'dead-worker', lease_token = %s,
                leased_until = now() + %s::interval, heartbeat_at = now()
            WHERE id = %s
            """,
            (attempt_count, f"token-{job_id}", expires_in, job_id),
        )


def test_sweep_closes_exhausted_expired_job_once_and_leaves_active_leases(ai_db) -> None:
    from app.modules.agent_run.infrastructure.postgres_agent_job_repository import PostgresAgentJobRepository
    from app.modules.agent_run.infrastructure.postgres_agent_run_repository import PostgresAgentRunRepository

    _, _, connect = ai_db
    for run_id in ("dead", "alive", "retrying"):
        _queue_run(run_id)
    _lease(connect, "dead-planning", attempt_count=3, expires_in="-1 minute")
    _lease(connect, "alive-planning", attempt_count=3, expires_in="60 seconds")
    _lease(connect, "retrying-planning", attempt_count=2, expires_in="-1 minute")
    jobs = PostgresAgentJobRepository()
    # claim_next는 시도 한도에 닿은 job을 다시 고르지 않는다. 정리 전에는 이 run이 영원히 끝나지 않는다.
    assert jobs.claim_next("probe") is not None  # retrying만 다시 잡힌다.
    assert jobs.claim_next("probe") is None

    assert jobs.fail_exhausted_leases() == 1
    assert jobs.fail_exhausted_leases() == 0

    assert _job(connect, "dead-planning")["status"] == "failed"
    # document-svc는 AgentRun 상태 조회에서 종료 상태를 보고 크레딧 예약을 정산한다. 정상 실패와 같은 failed로 닫힌다.
    dead = PostgresAgentRunRepository().get_for_user("ws", "user", "dead")
    assert (dead.status, dead.error_code) == ("failed", "interrupted_attempts_exhausted")
    assert dead.finished_at is not None
    assert _job(connect, "alive-planning")["status"] == "leased"
    assert _run(connect, "alive")["status"] == "queued"


def test_concurrent_sweeps_close_each_job_exactly_once(ai_db) -> None:
    from app.modules.agent_run.infrastructure.postgres_agent_job_repository import PostgresAgentJobRepository

    _, _, connect = ai_db
    run_ids = [f"dead-{index}" for index in range(5)]
    for run_id in run_ids:
        _queue_run(run_id)
        _lease(connect, f"{run_id}-planning", attempt_count=3, expires_in="-1 minute")
    barrier = threading.Barrier(4)
    results: list[int] = []

    def sweep() -> None:
        barrier.wait()
        results.append(PostgresAgentJobRepository().fail_exhausted_leases())

    threads = [threading.Thread(target=sweep) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sum(results) == len(run_ids)
    assert all(_run(connect, run_id)["status"] == "failed" for run_id in run_ids)


def test_release_for_shutdown_is_conditional_on_the_lease(ai_db) -> None:
    from app.modules.agent_run.infrastructure.postgres_agent_job_repository import PostgresAgentJobRepository

    _, _, connect = ai_db
    _queue_run("run-a")
    jobs = PostgresAgentJobRepository()
    job = jobs.claim_next("worker-1")

    assert jobs.release_for_shutdown(job) is True
    assert jobs.release_for_shutdown(job) is False  # 이미 놓은 lease는 다시 건드리지 않는다.
    again = jobs.claim_next("worker-2")
    assert again.attempt_count == 1
    assert jobs.release_for_shutdown(job) is False  # 다른 worker가 잡은 lease는 이전 token으로 놓지 못한다.
    assert _job(connect, "run-a-planning")["status"] == "leased"
