import sys
from types import SimpleNamespace

from app.modules.wiki_embedding.infrastructure.bge_m3_embedding_model import (
    BgeM3EmbeddingModel,
    _load_sentence_transformer,
)


def test_model_is_reused_between_embedding_clients(monkeypatch) -> None:
    loaded_models = []

    class FakeSentenceTransformer:
        def __init__(self, model_name: str) -> None:
            loaded_models.append(model_name)

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=FakeSentenceTransformer),
    )
    _load_sentence_transformer.cache_clear()

    first = BgeM3EmbeddingModel("test-model")
    second = BgeM3EmbeddingModel("test-model")

    assert first._load_model() is second._load_model()
    assert loaded_models == ["test-model"]
    _load_sentence_transformer.cache_clear()


def test_embeds_one_text_at_a_time_to_bound_padding_memory() -> None:
    calls = []

    class FakeModel:
        def encode(self, texts, **kwargs):
            calls.append(kwargs["batch_size"])
            return [[1.0, 0.0] for _ in texts]

    model = BgeM3EmbeddingModel("test-model")
    model._model = FakeModel()

    assert model.embed(["짧은 문단", "긴 문단 " * 100]) == [[1.0, 0.0], [1.0, 0.0]]
    assert calls == [1]
