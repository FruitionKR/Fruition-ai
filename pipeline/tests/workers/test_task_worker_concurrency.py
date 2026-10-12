"""task worker의 동시 처리: key별 순서, 연속 offset 커밋, 용량 제한, 재전달 대기, 리밸런스."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiokafka import TopicPartition

from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal
from app.workers import task_worker
from app.workers.task_worker import OrderedDispatcher

TP = TopicPartition("t", 0)


def _msg(offset: int, key: bytes | None = None, run_id: str | None = None, partition: int = 0):
    return SimpleNamespace(
        topic="t", partition=partition, offset=offset, key=key,
        value={"kind": "lint", "run_id": run_id or f"run-{offset}"},
    )


# --- OrderedDispatcher: Kafka 없이 순서·커밋 규칙만 확인한다 ---

def test_dispatcher_runs_different_keys_together_and_same_key_in_offset_order() -> None:
    d = OrderedDispatcher()
    a0, b1, a2, a3 = _msg(0, b"a"), _msg(1, b"b"), _msg(2, b"a"), _msg(3, b"a")

    assert [d.add(m) for m in (a0, b1, a2, a3)] == [True, True, False, False]
    assert d.outstanding == 4
    assert d.finish(a0) is a2
    assert d.finish(a2) is a3
    assert d.finish(a3) is None
    assert d.finish(b1) is None
    assert d.outstanding == 0


def test_dispatcher_commit_never_passes_unfinished_offset() -> None:
    d = OrderedDispatcher()
    m = [_msg(i, bytes([i])) for i in range(4)]
    for message in m:
        d.add(message)

    d.finish(m[2])
    d.finish(m[1])
    assert d.take_commits() == {}
    d.finish(m[0])
    assert d.take_commits() == {TP: 3}
    assert d.take_commits() == {}
    d.finish(m[3])
    assert d.take_commits() == {TP: 4}


def test_dispatcher_messages_without_key_do_not_serialize() -> None:
    d = OrderedDispatcher()

    assert [d.add(_msg(i)) for i in range(3)] == [True, True, True]


def test_dispatcher_revoke_returns_contiguous_commit_and_drops_queued() -> None:
    d = OrderedDispatcher()
    done, running, queued, other = _msg(0, b"x"), _msg(1, b"y"), _msg(2, b"y"), _msg(0, b"z", partition=1)
    for message in (done, running, queued, other):
        d.add(message)
    d.finish(done)

    assert d.revoke({TP}) == {TP: 1}
    # 실행 중이던 메시지가 늦게 끝나도 커밋 대상이 아니고, 대기하던 메시지는 시작되지 않는다.
    assert d.finish(running) is None
    assert d.take_commits() == {}
    assert d.outstanding == 1  # 다른 partition의 other만 남는다


# --- consume(): 가짜 consumer·producer로 전체 루프를 돌린다 ---

class _Harness:
    def __init__(self, messages, handler, concurrency):
        self.pending = list(messages)
        self.handler = handler
        self.concurrency = concurrency
        self.events: list[tuple[str, str]] = []
        self.commits: list[dict] = []
        self.paused = False
        self.listener = None
        self.stop_callbacks: list = []
        self.consumer = SimpleNamespace(
            start=AsyncMock(), stop=AsyncMock(), seek=MagicMock(),
            commit=AsyncMock(side_effect=lambda offsets: self.commits.append(dict(offsets))),
            subscribe=lambda topics, listener=None: setattr(self, "listener", listener),
            assignment=lambda: {TP},
            paused=lambda: {TP} if self.paused else set(),
            pause=lambda *tps: setattr(self, "paused", True),
            resume=lambda *tps: setattr(self, "paused", False),
            getmany=self._getmany,
        )
        self.producer = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), send_and_wait=self._send)

    async def _getmany(self, timeout_ms, max_records):
        if self.paused or not self.pending:
            await asyncio.sleep(0.001)
            return {}
        batch, self.pending = self.pending[:max_records], self.pending[max_records:]
        return {TP: batch}

    async def _send(self, topic, event, **kwargs):
        self.events.append((event["run_id"], event["status"]))

    async def _run_blocking(self, _executor, function, *args):
        if function is task_worker._handle_controlled:
            return await self.handler(args[0])
        return None

    def stop(self) -> None:
        self.stop_callbacks[0]()

    async def until(self, condition) -> None:
        for _ in range(1000):
            if condition():
                return
            await asyncio.sleep(0.001)
        raise AssertionError("조건이 충족되지 않았다")


@contextmanager
def _patched(h: _Harness):
    loop = asyncio.get_running_loop()
    with patch.object(task_worker.database, "ensure_ai_schema"), \
            patch.object(task_worker, "COMMAND_TOPIC", "test.maintenance.command"), \
            patch.object(task_worker, "CONCURRENCY", h.concurrency), \
            patch.object(task_worker, "AIOKafkaConsumer", return_value=h.consumer), \
            patch.object(task_worker, "AIOKafkaProducer", return_value=h.producer), \
            patch.object(task_worker, "get_wiki_embedding_job"), \
            patch.object(task_worker, "_run_blocking", side_effect=h._run_blocking), \
            patch.object(asyncio, "to_thread", side_effect=AsyncMock(return_value=None)), \
            patch.object(journal, "REDELIVERY_BACKOFF_SECONDS", 0.001), \
            patch.object(loop, "add_signal_handler", side_effect=lambda sig, cb: h.stop_callbacks.append(cb)):
        yield


def _run(messages, handler, scenario, concurrency=3, expect_error=None):
    """scenario(h)가 끝나면 SIGTERM과 같게 정지시키고 consume()의 종료를 기다린다."""
    async def main():
        h = _Harness(messages, handler, concurrency)
        with _patched(h):
            consume = asyncio.create_task(task_worker.consume())
            await h.until(lambda: h.stop_callbacks)
            try:
                await asyncio.wait_for(scenario(h), timeout=5)
            finally:
                if not consume.done():
                    h.stop()
            if expect_error:
                with pytest.raises(expect_error):
                    await asyncio.wait_for(consume, timeout=5)
            else:
                await asyncio.wait_for(consume, timeout=5)
        return h

    return asyncio.run(main())


def test_different_keys_run_concurrently_up_to_capacity() -> None:
    started: list[str] = []
    state = {"gate": asyncio.Event()}

    async def handler(command):
        started.append(command["run_id"])
        await state["gate"].wait()
        return {}

    async def scenario(h):
        # 세 건이 모두 시작돼야 풀린다: 순차 처리라면 여기서 멈춘다.
        await h.until(lambda: len(started) == 3)
        state["gate"].set()
        await h.until(lambda: len(h.events) == 3)

    h = _run([_msg(i, bytes([i])) for i in range(3)], handler, scenario)

    assert sorted(started) == ["run-0", "run-1", "run-2"]
    assert h.commits[-1] == {TP: 3}


def test_same_key_runs_sequentially_in_offset_order() -> None:
    log: list[str] = []
    active = 0
    peak = 0

    async def handler(command):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        log.append(command["run_id"])
        await asyncio.sleep(0.01)
        active -= 1
        return {}

    async def scenario(h):
        await h.until(lambda: len(h.events) == 3)

    _run([_msg(i, b"same") for i in range(3)], handler, scenario)

    assert log == ["run-0", "run-1", "run-2"]
    assert peak == 1


def test_commit_waits_for_earlier_unfinished_offset() -> None:
    gates = {"run-0": asyncio.Event(), "run-1": asyncio.Event()}

    async def handler(command):
        await gates[command["run_id"]].wait()
        return {}

    async def scenario(h):
        gates["run-1"].set()  # 뒤 offset이 먼저 끝난다
        await h.until(lambda: ("run-1", "succeeded") in h.events)
        await asyncio.sleep(0.02)
        assert h.commits == []
        gates["run-0"].set()
        await h.until(lambda: h.commits)

    h = _run([_msg(0, b"a"), _msg(1, b"b")], handler, scenario)

    assert h.commits == [{TP: 2}]


def test_busy_run_retries_without_seek_and_does_not_block_other_keys() -> None:
    attempts = 0
    owner_done = asyncio.Event()

    async def handler(command):
        nonlocal attempts
        if command["run_id"] == "run-0":
            attempts += 1
            if not owner_done.is_set():
                raise journal.TaskAlreadyExecutingError("busy")
        return {}

    async def scenario(h):
        await h.until(lambda: ("run-1", "succeeded") in h.events)
        await h.until(lambda: attempts >= 2)  # 기다리며 다시 시도한다
        # 대기 중인 offset 0 뒤의 커밋만 막힌다.
        assert h.commits == []
        owner_done.set()
        await h.until(lambda: h.commits == [{TP: 2}])

    h = _run([_msg(0, b"a"), _msg(1, b"b")], handler, scenario)

    h.consumer.seek.assert_not_called()
    assert set(h.events) == {("run-0", "succeeded"), ("run-1", "succeeded")}


def test_non_durable_failure_stops_without_committing_that_offset() -> None:
    gate = {"event": asyncio.Event()}

    async def handler(command):
        if command["run_id"] == "run-0":
            await gate["event"].wait()
            raise RuntimeError("일시 장애")
        return {}

    async def scenario(h):
        await h.until(lambda: ("run-1", "succeeded") in h.events)
        gate["event"].set()
        await h.until(lambda: h.stop_callbacks and h.producer.stop.await_count == 1)

    with patch.object(task_worker, "_failure_is_durable", return_value=False):
        h = _run([_msg(0, b"a"), _msg(1, b"b")], handler, scenario, expect_error=RuntimeError)

    # 끝난 offset 1은 offset 0이 안 끝났으므로 커밋되지 않고, 실패한 메시지의 failed 이벤트도 없다.
    assert h.commits == []
    assert ("run-0", "failed") not in h.events


def test_revoke_commits_contiguous_done_offsets_and_skips_later_commit() -> None:
    gates = {f"run-{i}": asyncio.Event() for i in range(3)}

    async def handler(command):
        await gates[command["run_id"]].wait()
        return {}

    async def scenario(h):
        gates["run-0"].set()
        await h.until(lambda: h.commits == [{TP: 1}])
        await h.listener.on_partitions_revoked({TP})
        gates["run-1"].set()
        gates["run-2"].set()
        await h.until(lambda: len(h.events) == 3)
        await asyncio.sleep(0.02)

    h = _run([_msg(i, bytes([i])) for i in range(3)], handler, scenario)

    # 회수된 뒤에 끝난 in-flight 메시지는 결과를 발행하되 커밋은 하지 않는다.
    assert h.commits == [{TP: 1}]


def test_revoke_drops_messages_that_have_not_started() -> None:
    started: list[str] = []
    gate = {"event": asyncio.Event()}

    async def handler(command):
        started.append(command["run_id"])
        await gate["event"].wait()
        return {}

    async def scenario(h):
        await h.until(lambda: started == ["run-0"])
        await asyncio.sleep(0.01)  # run-1은 같은 key라 대기 중이다
        await h.listener.on_partitions_revoked({TP})
        gate["event"].set()
        await h.until(lambda: ("run-0", "succeeded") in h.events)
        await asyncio.sleep(0.02)

    h = _run([_msg(0, b"k"), _msg(1, b"k")], handler, scenario)

    assert started == ["run-0"]
    assert h.commits == []


def test_fetch_stops_at_capacity_and_resumes() -> None:
    active = 0
    peak = 0
    gate = {"event": asyncio.Event()}
    saw_pause = []

    async def handler(command):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await gate["event"].wait()
        active -= 1
        return {}

    async def scenario(h):
        await h.until(lambda: active == 2)
        await h.until(lambda: h.paused)  # 용량이 차면 partition을 멈추고 poll만 계속한다
        saw_pause.append(True)
        assert len(h.pending) == 3
        gate["event"].set()
        await h.until(lambda: len(h.events) == 5)

    h = _run([_msg(i, bytes([i])) for i in range(5)], handler, scenario, concurrency=2)

    assert peak == 2 and saw_pause
    assert h.commits[-1] == {TP: 5}
