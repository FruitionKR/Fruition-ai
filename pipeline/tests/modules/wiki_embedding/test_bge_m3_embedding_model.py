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
