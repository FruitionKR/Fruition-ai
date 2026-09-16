from __future__ import annotations

from collections.abc import Callable
from typing import TypedDict

from langgraph.graph import END, StateGraph

from app.core.langsmith_tracing import configured_langsmith_tracing
from app.modules.wiki_generation.application.models import GenerationEvaluation
from app.modules.wiki_generation.application.ports import (
    EvaluationArtifactPort,
    GenerationEvaluatorPort,
    GenerationRepairPort,
    JsonDict,
    PipelineEventPort,
    SemanticGenerationPort,
    SemanticNormalizerPort,
)
from app.modules.wiki_generation.application.run_generation_loop import (
    generation_evaluation_finished,
    generation_retry_block_ids,
    generation_retry_prompt,
)


class WikiGenerationEvaluatorState(TypedDict, total=False):
    semantic_system_prompt: str
    prompt: str
    source_context: JsonDict | None
    evaluation_enabled: bool
    max_attempts: int
    attempt: int
    notes: list[JsonDict]
    normalized: JsonDict
    evaluation: GenerationEvaluation
    evaluations: list[GenerationEvaluation]
    repair_operations: list[str]
    evaluation_sequence: list[GenerationEvaluation]
    evaluation_index: int
    retry_block_ids: list[str] | None
    patch_applied: bool


GraphNode = Callable[[WikiGenerationEvaluatorState], WikiGenerationEvaluatorState]
GraphRoute = Callable[[WikiGenerationEvaluatorState], str]


def build_wiki_generation_evaluator_graph(
    *,
    semantic_generation: GraphNode,
    normalize: GraphNode,
    evaluate: GraphNode,
    repair: GraphNode,
    reevaluate: GraphNode,
    prepare_retry: GraphNode,
    targeted_patch: GraphNode,
    route_after_normalization: GraphRoute,
    route_after_repair: GraphRoute,
    route_after_evaluation: GraphRoute,
    route_after_retry_preparation: GraphRoute,
    route_after_targeted_patch: GraphRoute,
) -> StateGraph:
    graph = StateGraph(WikiGenerationEvaluatorState)
    graph.add_node("semantic_generation", semantic_generation)
    graph.add_node("normalize", normalize)
    graph.add_node("evaluate", evaluate)
    graph.add_node("repair", repair)
    graph.add_node("reevaluate", reevaluate)
    graph.add_node("prepare_retry", prepare_retry)
    graph.add_node("targeted_patch", targeted_patch)
    graph.set_entry_point("semantic_generation")
    graph.add_edge("semantic_generation", "normalize")
    graph.add_conditional_edges(
        "normalize",
        route_after_normalization,
        {"evaluate": "evaluate", "finished": END},
    )
    graph.add_edge("evaluate", "repair")
    graph.add_conditional_edges(
        "repair",
        route_after_repair,
        {"reevaluate": "reevaluate", "retry": "prepare_retry", "finished": END},
    )
    graph.add_conditional_edges(
        "reevaluate",
        route_after_evaluation,
        {"retry": "prepare_retry", "finished": END},
    )
    graph.add_conditional_edges(
        "prepare_retry",
        route_after_retry_preparation,
        {"patch": "targeted_patch", "regenerate": "semantic_generation"},
    )
    graph.add_conditional_edges(
        "targeted_patch",
        route_after_targeted_patch,
        {"normalize": "normalize", "regenerate": "semantic_generation"},
    )
    return graph


class LangGraphWikiGenerationEvaluator:
    def __init__(
        self,
        semantic_generation: SemanticGenerationPort,
        normalizer: SemanticNormalizerPort,
        evaluator: GenerationEvaluatorPort,
        repairer: GenerationRepairPort,
        events: PipelineEventPort,
        evaluation_artifacts: EvaluationArtifactPort,
        source_block_ids: list[str] | None = None,
        packet_block_ids: dict[str, list[str]] | None = None,
    ) -> None:
        self.semantic_generation = semantic_generation
        self.normalizer = normalizer
        self.evaluator = evaluator
        self.repairer = repairer
        self.events = events
        self.evaluation_artifacts = evaluation_artifacts
        self.source_block_ids = source_block_ids
        self.packet_block_ids = packet_block_ids
        self.graph = self._build_graph().compile()

    def run(
        self,
        *,
        semantic_system_prompt: str,
        source_context: JsonDict | None,
        evaluation_enabled: bool,
        max_attempts: int,
    ) -> tuple[list[JsonDict], JsonDict, list[GenerationEvaluation]]:
        initial_state: WikiGenerationEvaluatorState = {
            "semantic_system_prompt": semantic_system_prompt,
            "prompt": semantic_system_prompt,
            "source_context": source_context,
            "evaluation_enabled": evaluation_enabled,
            "max_attempts": max_attempts,
            "attempt": 1,
            "evaluations": [],
        }
        with configured_langsmith_tracing():
            result = self.graph.invoke(initial_state, config={"run_name": "wiki_ingest_evaluator"})
        return result["notes"], result["normalized"], result["evaluations"]

    def _build_graph(self) -> StateGraph:
        return build_wiki_generation_evaluator_graph(
            semantic_generation=self._generate,
            normalize=self._normalize,
            evaluate=self._evaluate,
            repair=self._repair,
            reevaluate=self._reevaluate,
            prepare_retry=self._prepare_retry,
            targeted_patch=self._targeted_patch,
            route_after_normalization=self._route_after_normalization,
            route_after_repair=self._route_after_repair,
            route_after_evaluation=self._route_after_evaluation,
            route_after_retry_preparation=self._route_after_retry_preparation,
            route_after_targeted_patch=self._route_after_targeted_patch,
        )

    def _generate(self, state: WikiGenerationEvaluatorState) -> WikiGenerationEvaluatorState:
        notes = self.semantic_generation.generate(
            state["prompt"],
            state["attempt"],
            state.get("source_context"),
            state.get("notes") if state["attempt"] > 1 else None,
            state.get("retry_block_ids"),
        )
        self.events.emit(
            "3. 의미 추출 완료",
            "의미 노트 목록을 정규화 단계 입력으로 전달합니다.",
            {"시도": state["attempt"], "노트 수": len(notes)},
        )
        return {"notes": notes}

    def _normalize(self, state: WikiGenerationEvaluatorState) -> WikiGenerationEvaluatorState:
        return {"normalized": self.normalizer.normalize_notes(state["notes"])}

    def _targeted_patch(self, state: WikiGenerationEvaluatorState) -> WikiGenerationEvaluatorState:
        patch_result = self.semantic_generation.patch(
            state["attempt"],
            state["notes"],
            state["evaluation"],
            state["retry_block_ids"],
        )
        if patch_result is None:
            evaluation = {**state["evaluation"], "retry_mode": "targeted_chunk_regeneration"}
            self.evaluation_artifacts.write(state["attempt"] - 1, "retry", evaluation)
            return {
                "patch_applied": False,
                "evaluation": evaluation,
                "evaluations": [*state["evaluations"][:-1], evaluation],
            }
        notes, operations = patch_result
        evaluation = {
            **state["evaluation"],
            "retry_mode": "targeted_patch",
            "applied_patch_operations": operations,
        }
        self.evaluation_artifacts.write(state["attempt"] - 1, "retry", evaluation)
        return {
            "notes": notes,
            "patch_applied": True,
            "evaluation": evaluation,
            "evaluations": [*state["evaluations"][:-1], evaluation],
        }

    def _evaluate(self, state: WikiGenerationEvaluatorState) -> WikiGenerationEvaluatorState:
        evaluation = self.evaluator.evaluate(state["normalized"])
        self.evaluation_artifacts.write(state["attempt"], "evaluation", evaluation)
        self._emit_evaluation(state["attempt"], evaluation)
        return {
            "evaluation": evaluation,
            "evaluations": [*state["evaluations"], evaluation],
            "repair_operations": [],
        }

    def _repair(self, state: WikiGenerationEvaluatorState) -> WikiGenerationEvaluatorState:
        notes, repair_operations = self.repairer.repair(
            state["notes"],
            state["normalized"],
            state["evaluation"],
        )
        normalized = (
            self.normalizer.normalize_notes(notes)
            if repair_operations
            else state["normalized"]
        )
        return {
            "notes": notes,
            "normalized": normalized,
            "repair_operations": repair_operations,
        }

    def _reevaluate(self, state: WikiGenerationEvaluatorState) -> WikiGenerationEvaluatorState:
        evaluation = self.evaluator.evaluate(state["normalized"])
        evaluation["repair_operations"] = state["repair_operations"]
        self.evaluation_artifacts.write(state["attempt"], "repair", evaluation)
        self._emit_repair(state["attempt"], state["repair_operations"], evaluation)
        return {"evaluation": evaluation, "evaluations": [*state["evaluations"], evaluation]}

    @staticmethod
    def _route_after_normalization(state: WikiGenerationEvaluatorState) -> str:
        return "evaluate" if state["evaluation_enabled"] else "finished"

    def _route_after_repair(self, state: WikiGenerationEvaluatorState) -> str:
        if state["repair_operations"]:
            return "reevaluate"
        return self._route_after_evaluation(state)

    @staticmethod
    def _route_after_evaluation(state: WikiGenerationEvaluatorState) -> str:
        if generation_evaluation_finished(state["evaluation"], state["attempt"], state["max_attempts"]):
            return "finished"
        return "retry"

    def _prepare_retry(self, state: WikiGenerationEvaluatorState) -> WikiGenerationEvaluatorState:
        retry_block_ids = generation_retry_block_ids(
            state["normalized"],
            state["evaluation"],
            self.source_block_ids,
            self.packet_block_ids,
        )
        result: WikiGenerationEvaluatorState = {
            "attempt": state["attempt"] + 1,
            "prompt": generation_retry_prompt(state["semantic_system_prompt"], state["evaluation"]),
            "retry_block_ids": retry_block_ids,
            "patch_applied": False,
        }
        if retry_block_ids is None:
            evaluation = {**state["evaluation"], "retry_mode": "full_regeneration"}
            result["evaluation"] = evaluation
            result["evaluations"] = [*state["evaluations"][:-1], evaluation]
            self.evaluation_artifacts.write(state["attempt"], "retry", evaluation)
        return result

    @staticmethod
    def _route_after_retry_preparation(state: WikiGenerationEvaluatorState) -> str:
        return "patch" if state.get("retry_block_ids") is not None else "regenerate"

    @staticmethod
    def _route_after_targeted_patch(state: WikiGenerationEvaluatorState) -> str:
        return "normalize" if state.get("patch_applied") else "regenerate"

    def _emit_evaluation(
        self,
        attempt: int,
        evaluation: GenerationEvaluation,
    ) -> None:
        self.events.emit(
            "3-평가. Wiki 생성 평가",
            "정규화된 의미 구조를 평가했습니다.",
            {
                "시도": attempt,
                "passed": evaluation.get("passed"),
                "retry": evaluation.get("retry_recommended"),
                "overall": (evaluation.get("scores") or {}).get("overall"),
                "issue 수": len(evaluation.get("issues", [])),
            },
        )

    def _emit_repair(
        self,
        attempt: int,
        repair_operations: list[str],
        evaluation: GenerationEvaluation,
    ) -> None:
        self.events.emit(
            "3-평가-보정. Wiki 생성 보정",
            "평가 issue를 바탕으로 명확한 observation 문제를 자동 보정하고 다시 평가했습니다.",
            {
                "시도": attempt,
                "보정 수": len(repair_operations),
                "passed": evaluation.get("passed"),
                "retry": evaluation.get("retry_recommended"),
                "overall": (evaluation.get("scores") or {}).get("overall"),
                "issue 수": len(evaluation.get("issues", [])),
            },
        )
