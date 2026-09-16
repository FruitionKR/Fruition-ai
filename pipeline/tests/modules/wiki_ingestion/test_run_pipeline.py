import unittest
from collections.abc import Callable
from threading import Event, Lock, Thread

from app.modules.wiki_ingestion.application.run_pipeline import (
    PipelineRunCommand as _PipelineRunCommand,
    PipelineRunRegistration,
    RunPipelineUseCase,
)


def PipelineRunCommand(**data: object) -> _PipelineRunCommand:
    data.setdefault("provider", "openai")
    data.setdefault("model", "gpt-5-nano")
    return _PipelineRunCommand(**data)


class FakeRunner:
    def __init__(self, calls: list[object], error: Exception | None = None) -> None:
        self.calls = calls
        self.error = error

    def run(
        self,
        command: PipelineRunCommand,
        progress_callback: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        self.calls.append(("run", command))
        if self.error is not None:
            raise self.error
        return {"manifest": "value"}


class FakeRepository:
    def __init__(
        self,
        calls: list[object],
        active_results: list[bool] | None = None,
    ) -> None:
        self.calls = calls
        self.active_results = list(active_results or [])

    def create(
        self,
        run_id: str,
        document_id: str | None,
        user_id: str,
        workspace_id: str,
        input_source: str,
        output_dir: str,
        mode: str,
    ) -> None:
        self.calls.append(
            (
                "create",
                run_id,
                document_id,
                user_id,
                workspace_id,
                input_source,
                output_dir,
                mode,
            )
        )

    def finish(
        self,
        run_id: str,
        manifest: dict[str, object],
        expected_source_hash: str | None = None,
    ) -> list[str]:
        self.calls.append(("finish", run_id, manifest, expected_source_hash))
        return ["page-1"]

    def fail(self, run_id: str, error: str) -> None:
        self.calls.append(("fail", run_id, error))

    def touch(self, run_id: str) -> bool:
        self.calls.append(("touch", run_id))
        if self.active_results:
            return self.active_results.pop(0)
        return True

    def concept_write_lock(self, _workspace_id: str, _run_id: str):
        return Lock()


class FakeEmbeddingJob:
    def __init__(self, calls: list[object]) -> None:
        self.calls = calls

    def start(self, run_id: str, page_ids: list[str]) -> None:
        self.calls.append(("embedding", run_id, page_ids))


class RunPipelineUseCaseTest(unittest.TestCase):
    def test_register_and_execute_preserve_pipeline_state_order(self) -> None:
        calls: list[object] = []
        use_case = RunPipelineUseCase(
            runner=FakeRunner(calls),
            repository=FakeRepository(calls),
            embedding_job=FakeEmbeddingJob(calls),
        )
        registration = PipelineRunRegistration(
            run_id="run-1",
            document_id="doc-1",
            user_id="user-1",
            workspace_id="workspace-1",
            input_source="storage:doc-1.md",
            output_dir="runs/run-1",
            mode="api",
        )
        command = PipelineRunCommand(
            run_id="run-1",
            input="input.md",
            input_name="input.md",
            out="runs/run-1",
            user_id="user-1",
            workspace_id="workspace-1",
        )

        use_case.register(registration)
        manifest = use_case.execute("run-1", command)

        self.assertEqual(manifest, {"manifest": "value"})
        self.assertEqual(
            calls,
            [
                (
                    "create",
                    "run-1",
                    "doc-1",
                    "user-1",
                    "workspace-1",
                    "storage:doc-1.md",
                    "runs/run-1",
                    "api",
                ),
                ("touch", "run-1"),
                ("run", command),
                ("touch", "run-1"),
                ("finish", "run-1", {"manifest": "value"}, None),
                ("embedding", "run-1", ["page-1"]),
            ],
        )

    def test_execute_marks_pipeline_failed_before_propagating_error(self) -> None:
        calls: list[object] = []
        use_case = RunPipelineUseCase(
            runner=FakeRunner(calls, RuntimeError("pipeline failed")),
            repository=FakeRepository(calls),
            embedding_job=FakeEmbeddingJob(calls),
        )
        command = PipelineRunCommand(
            run_id="run-1",
            input="input.md",
            input_name="input.md",
            out="runs/run-1",
            user_id="user-1",
            workspace_id="workspace-1",
        )

        with self.assertRaisesRegex(RuntimeError, "pipeline failed"):
            use_case.execute("run-1", command)

        self.assertEqual(
            calls,
            [
                ("touch", "run-1"),
                ("run", command),
                ("fail", "run-1", "pipeline failed"),
            ],
        )

    def test_execute_connects_pipeline_progress_to_run_heartbeat(self) -> None:
        class ProgressRunner:
            def run(
                self,
                command: PipelineRunCommand,
                progress_callback: Callable[[], None] | None = None,
            ) -> dict[str, object]:
                assert progress_callback is not None
                progress_callback()
                return {"manifest": "value"}

        calls: list[object] = []
        use_case = RunPipelineUseCase(
            runner=ProgressRunner(),
            repository=FakeRepository(calls),
            embedding_job=FakeEmbeddingJob(calls),
        )
        command = PipelineRunCommand(
            run_id="run-1",
            input="input.md",
            input_name="input.md",
            out="runs/run-1",
            user_id="user-1",
            workspace_id="workspace-1",
        )

        use_case.execute("run-1", command)

        self.assertEqual(
            calls[:3],
            [
                ("touch", "run-1"),
                ("touch", "run-1"),
                ("touch", "run-1"),
            ],
        )

    def test_execute_stops_before_runner_for_inactive_target(self) -> None:
        calls: list[object] = []
        use_case = RunPipelineUseCase(
            runner=FakeRunner(calls),
            repository=FakeRepository(calls, active_results=[False]),
            embedding_job=FakeEmbeddingJob(calls),
        )
        command = PipelineRunCommand(
            run_id="run-1",
            input="input.md",
            input_name="input.md",
            out="runs/run-1",
            user_id="user-1",
            workspace_id="workspace-1",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "document or workspace is inactive",
        ):
            use_case.execute("run-1", command)

        self.assertEqual(
            calls,
            [
                ("touch", "run-1"),
                (
                    "fail",
                    "run-1",
                    "Pipeline run cancelled because its document or workspace is inactive.",
                ),
            ],
        )

    def test_execute_stops_before_finish_when_target_becomes_inactive(self) -> None:
        calls: list[object] = []
        use_case = RunPipelineUseCase(
            runner=FakeRunner(calls),
            repository=FakeRepository(calls, active_results=[True, False]),
            embedding_job=FakeEmbeddingJob(calls),
        )
        command = PipelineRunCommand(
            run_id="run-1",
            input="input.md",
            input_name="input.md",
            out="runs/run-1",
            user_id="user-1",
            workspace_id="workspace-1",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "document or workspace is inactive",
        ):
            use_case.execute("run-1", command)

        self.assertEqual(
            calls,
            [
                ("touch", "run-1"),
                ("run", command),
                ("touch", "run-1"),
                (
                    "fail",
                    "run-1",
                    "Pipeline run cancelled because its document or workspace is inactive.",
                ),
            ],
        )

    def test_execute_does_not_serialize_pipeline_analysis(self) -> None:
        first_entered = Event()
        release_first = Event()
        second_entered = Event()

        class BlockingRunner:
            def run(
                self,
                command: PipelineRunCommand,
                progress_callback: Callable[[], None] | None = None,
            ) -> dict[str, object]:
                if command.run_id == "run-1":
                    first_entered.set()
                    release_first.wait(timeout=1)
                else:
                    second_entered.set()
                return {"manifest": command.run_id}

        calls: list[object] = []
        first_use_case = RunPipelineUseCase(
            runner=BlockingRunner(),
            repository=FakeRepository(calls),
            embedding_job=FakeEmbeddingJob(calls),
        )
        second_use_case = RunPipelineUseCase(
            runner=BlockingRunner(),
            repository=FakeRepository(calls),
            embedding_job=FakeEmbeddingJob(calls),
        )
        first = Thread(
            target=first_use_case.execute,
            args=(
                "run-1",
                PipelineRunCommand(
                    run_id="run-1",
                    input="one.md",
                    input_name="one.md",
                    out="runs/one",
                    user_id="user-1",
                    workspace_id="workspace-1",
                ),
            ),
        )
        second = Thread(
            target=second_use_case.execute,
            args=(
                "run-2",
                PipelineRunCommand(
                    run_id="run-2",
                    input="two.md",
                    input_name="two.md",
                    out="runs/two",
                    user_id="user-1",
                    workspace_id="workspace-1",
                ),
            ),
        )

        first.start()
        self.assertTrue(first_entered.wait(timeout=1))
        second.start()
        self.assertTrue(second_entered.wait(timeout=1))
        release_first.set()
        first.join(timeout=1)
        second.join(timeout=1)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_entered.is_set())

    def test_document_runs_overlap_heavy_phase_and_serialize_deferred_finish(self) -> None:
        heavy_runs: list[str] = []
        active = ""
        seen_active: list[tuple[str, str]] = []
        calls: list[object] = []
        state_lock = Lock()
        workspace_lock = Lock()
        both_heavy = Event()
        release_heavy = Event()

        class SharedRepository(FakeRepository):
            def concept_write_lock(self, workspace_id: str, run_id: str):
                assert workspace_id == "workspace-1"
                assert run_id in {"run-1", "run-2"}
                return workspace_lock

            def finish(
                self,
                run_id: str,
                manifest: dict[str, object],
                expected_source_hash: str | None = None,
            ) -> list[str]:
                nonlocal active
                with state_lock:
                    active += run_id
                return super().finish(run_id, manifest, expected_source_hash)

        class DeferredRunner:
            def run(
                self,
                command: PipelineRunCommand,
                progress_callback: Callable[[], None] | None = None,
                finalization_callback: Callable[
                    [Callable[[], dict[str, object]]], dict[str, object]
                ] | None = None,
            ) -> dict[str, object]:
                with state_lock:
                    heavy_runs.append(str(command.run_id))
                    if len(heavy_runs) == 2:
                        both_heavy.set()
                assert release_heavy.wait(timeout=2)

                def build_manifest() -> dict[str, object]:
                    with state_lock:
                        seen_active.append((str(command.run_id), active))
                    return {"run": command.run_id}

                assert finalization_callback is not None
                return finalization_callback(build_manifest)

        repository = SharedRepository(calls)
        runner = DeferredRunner()
        use_cases = [
            RunPipelineUseCase(runner, repository, FakeEmbeddingJob(calls)),
            RunPipelineUseCase(runner, repository, FakeEmbeddingJob(calls)),
        ]
        commands = [
            PipelineRunCommand(
                run_id=run_id,
                input=f"{run_id}.md",
                input_name=f"{run_id}.md",
                out=f"runs/{run_id}",
                user_id="user-1",
                workspace_id="workspace-1",
                source_document_id=run_id,
            )
            for run_id in ("run-1", "run-2")
        ]
        errors: list[Exception] = []

        def execute(index: int) -> None:
            try:
                use_cases[index].execute(commands[index].run_id or "", commands[index])
            except Exception as exc:  # pragma: no cover - assertion below reports it
                errors.append(exc)

        threads = [Thread(target=execute, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        self.assertTrue(both_heavy.wait(timeout=2))
        release_heavy.set()
        for thread in threads:
            thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(seen_active), 2)
        self.assertFalse(errors)
        self.assertEqual(set(heavy_runs), {"run-1", "run-2"})
        self.assertEqual(seen_active[0][1], "")
        self.assertEqual(seen_active[1][1], "run-1" if seen_active[1][0] == "run-2" else "run-2")

    def test_deferred_cancellation_skips_finish_and_shared_write(self) -> None:
        calls: list[object] = []

        class CancelledRepository(FakeRepository):
            def concept_write_lock(self, _workspace_id: str, _run_id: str):
                return Lock()

        class DeferredRunner:
            def run(
                self,
                command: PipelineRunCommand,
                progress_callback: Callable[[], None] | None = None,
                finalization_callback: Callable[
                    [Callable[[], dict[str, object]]], dict[str, object]
                ] | None = None,
            ) -> dict[str, object]:
                assert finalization_callback is not None
                return finalization_callback(lambda: {"run": command.run_id})

        repository = CancelledRepository(calls, active_results=[True, True, True, False])
        use_case = RunPipelineUseCase(
            DeferredRunner(),
            repository,
            FakeEmbeddingJob(calls),
        )
        command = PipelineRunCommand(
            run_id="run-1",
            input="run-1.md",
            input_name="run-1.md",
            out="runs/run-1",
            user_id="user-1",
            workspace_id="workspace-1",
            source_document_id="doc-1",
        )

        with self.assertRaisesRegex(RuntimeError, "document or workspace is inactive"):
            use_case.execute("run-1", command)

        self.assertFalse(any(call[0] == "finish" for call in calls if isinstance(call, tuple)))
        self.assertTrue(any(call[0] == "fail" for call in calls if isinstance(call, tuple)))

    def test_stale_source_before_deferred_lock_skips_finish(self) -> None:
        calls: list[object] = []

        class StaleRepository(FakeRepository):
            def concept_write_lock(self, _workspace_id: str, _run_id: str):
                return Lock()

            def get_document(self, _document_id: str) -> dict[str, object]:
                return {"source_revision": 2}

        class DeferredRunner:
            def run(
                self,
                command: PipelineRunCommand,
                progress_callback: Callable[[], None] | None = None,
                finalization_callback: Callable[
                    [Callable[[], dict[str, object]]], dict[str, object]
                ] | None = None,
            ) -> dict[str, object]:
                assert finalization_callback is not None
                return finalization_callback(lambda: {"run": command.run_id})

        repository = StaleRepository(calls)
        use_case = RunPipelineUseCase(
            DeferredRunner(),
            repository,
            FakeEmbeddingJob(calls),
        )
        command = PipelineRunCommand(
            run_id="run-1",
            input="run-1.md",
            input_name="run-1.md",
            out="runs/run-1",
            user_id="user-1",
            workspace_id="workspace-1",
            source_document_id="doc-1",
            source_revision=1,
        )

        with self.assertRaisesRegex(ValueError, "source revision is stale"):
            use_case.execute("run-1", command)

        self.assertFalse(any(call[0] == "finish" for call in calls if isinstance(call, tuple)))

if __name__ == "__main__":
    unittest.main()
