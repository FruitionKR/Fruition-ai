from app.core.langsmith_tracing import disable_unconfigured_langsmith_tracing
from app.modules.wiki_generation.application.run_generation_loop import (
    generation_evaluation_finished,
    generation_retry_block_ids,
    generation_retry_prompt,
)


disable_unconfigured_langsmith_tracing()
from app.modules.wiki_generation.application.models import GenerationEvaluation
from app.modules.wiki_generation.infrastructure.wiki_generation_evaluator_graph import (
    WikiGenerationEvaluatorState,
    build_wiki_generation_evaluator_graph,
)


def semantic_generation(state: WikiGenerationEvaluatorState) -> WikiGenerationEvaluatorState:
    attempt = int(state.get("attempt") or 1)
    semantic_system_prompt = str(state.get("semantic_system_prompt") or "Studio semantic prompt")
    return {
        "attempt": attempt,
        "semantic_system_prompt": semantic_system_prompt,
        "prompt": str(state.get("prompt") or semantic_system_prompt),
        "notes": list(state.get("notes") or [{"attempt": attempt}]),
        "evaluations": list(state.get("evaluations") or []),
        "evaluation_index": int(state.get("evaluation_index") or 0),
        "evaluation_enabled": bool(state.get("evaluation_enabled", True)),
        "max_attempts": max(1, int(state.get("max_attempts") or 2)),
    }


def normalize(state: WikiGenerationEvaluatorState) -> WikiGenerationEvaluatorState:
    normalized = state.get("normalized") or {"semantic_notes": state["notes"]}
    return {"normalized": normalized}


def evaluate(state: WikiGenerationEvaluatorState) -> WikiGenerationEvaluatorState:
    evaluation, evaluation_index = _next_evaluation(state)
    return {
        "evaluation": evaluation,
        "evaluations": [*state["evaluations"], evaluation],
        "evaluation_index": evaluation_index,
        "repair_operations": [],
    }


def repair(state: WikiGenerationEvaluatorState) -> WikiGenerationEvaluatorState:
    repair_operations = list(state["evaluation"].get("repair_operations") or [])
    return {"repair_operations": repair_operations}


def reevaluate(state: WikiGenerationEvaluatorState) -> WikiGenerationEvaluatorState:
    evaluation, evaluation_index = _next_evaluation(state)
    evaluation["repair_operations"] = state["repair_operations"]
    return {
        "evaluation": evaluation,
        "evaluations": [*state["evaluations"], evaluation],
        "evaluation_index": evaluation_index,
    }


def prepare_retry(state: WikiGenerationEvaluatorState) -> WikiGenerationEvaluatorState:
    retry_block_ids = generation_retry_block_ids(state["normalized"], state["evaluation"])
    result: WikiGenerationEvaluatorState = {
        "attempt": state["attempt"] + 1,
        "prompt": generation_retry_prompt(state["semantic_system_prompt"], state["evaluation"]),
        "notes": [{"attempt": state["attempt"] + 1}],
        "retry_block_ids": retry_block_ids,
        "patch_applied": False,
    }
    if retry_block_ids is None:
        evaluation = {**state["evaluation"], "retry_mode": "full_regeneration"}
        result["evaluation"] = evaluation
        result["evaluations"] = [*state["evaluations"][:-1], evaluation]
    return result


def targeted_patch(state: WikiGenerationEvaluatorState) -> WikiGenerationEvaluatorState:
    evaluation = {
        **state["evaluation"],
        "retry_mode": "targeted_patch",
        "applied_patch_operations": [],
    }
    return {
        "notes": list(state.get("notes") or []),
        "patch_applied": bool(state.get("retry_block_ids") is not None),
        "evaluation": evaluation,
        "evaluations": [*state["evaluations"][:-1], evaluation],
    }


def route_after_retry_preparation(state: WikiGenerationEvaluatorState) -> str:
    return "patch" if state.get("retry_block_ids") is not None else "regenerate"


def route_after_targeted_patch(state: WikiGenerationEvaluatorState) -> str:
    return "normalize" if state.get("patch_applied") else "regenerate"


def route_after_normalization(state: WikiGenerationEvaluatorState) -> str:
    return "evaluate" if state["evaluation_enabled"] else "finished"


def route_after_repair(state: WikiGenerationEvaluatorState) -> str:
    if state["repair_operations"]:
        return "reevaluate"
    return route_after_evaluation(state)


def route_after_evaluation(state: WikiGenerationEvaluatorState) -> str:
    if generation_evaluation_finished(state["evaluation"], state["attempt"], state["max_attempts"]):
        return "finished"
    return "retry"


def _next_evaluation(
    state: WikiGenerationEvaluatorState,
) -> tuple[GenerationEvaluation, int]:
    sequence = state.get("evaluation_sequence") or [
        {
            "passed": True,
            "retry_recommended": False,
            "retry_feedback": "",
            "scores": {"overall": 1.0},
            "issues": [],
        }
    ]
    index = min(int(state.get("evaluation_index") or 0), len(sequence) - 1)
    return dict(sequence[index]), index + 1


graph = build_wiki_generation_evaluator_graph(
    semantic_generation=semantic_generation,
    normalize=normalize,
    evaluate=evaluate,
    repair=repair,
    reevaluate=reevaluate,
    prepare_retry=prepare_retry,
    targeted_patch=targeted_patch,
    route_after_normalization=route_after_normalization,
    route_after_repair=route_after_repair,
    route_after_evaluation=route_after_evaluation,
    route_after_retry_preparation=route_after_retry_preparation,
    route_after_targeted_patch=route_after_targeted_patch,
).compile()
