from app.modules.wiki_generation.infrastructure.wiki_generation_evaluator_studio_graph import graph


def test_studio_graph_exposes_ingest_evaluator_topology() -> None:
    node_names = set(graph.get_graph().nodes)

    assert {
        "semantic_generation",
        "normalize",
        "evaluate",
        "repair",
        "reevaluate",
        "prepare_retry",
        "targeted_patch",
    }.issubset(node_names)


def test_studio_graph_invokes_with_default_evaluation() -> None:
    result = graph.invoke({})

    assert result["attempt"] == 1
    assert result["evaluation"]["passed"] is True
    assert len(result["evaluations"]) == 1
