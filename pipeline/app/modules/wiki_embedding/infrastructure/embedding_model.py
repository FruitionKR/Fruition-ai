import os

from app.modules.wiki_embedding.application.ports import EmbeddingModelPort
from app.modules.wiki_embedding.infrastructure.bge_m3_embedding_model import BgeM3EmbeddingModel
from app.modules.wiki_embedding.infrastructure.remote_embedding_model import RemoteEmbeddingModel


def embedding_model_from_env() -> EmbeddingModelPort:
    """`EMBEDDING_SERVICE_URL`이 있으면 임베딩 서버를, 없으면 프로세스 안의 BGE-M3를 쓴다."""
    if os.environ.get("EMBEDDING_SERVICE_URL"):
        return RemoteEmbeddingModel.from_env()
    return BgeM3EmbeddingModel()
