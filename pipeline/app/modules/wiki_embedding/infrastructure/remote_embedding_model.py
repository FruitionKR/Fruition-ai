from __future__ import annotations

import os

import httpx

from app.modules.wiki_embedding.application.ports import EmbeddingModelPort

DEFAULT_MODEL_NAME = "BAAI/bge-m3"


class RemoteEmbeddingModel(EmbeddingModelPort):
    """임베딩 서버(`embedding_server`)에 짧은 텍스트 임베딩을 요청한다. 워커는 모델을 올리지 않는다."""

    def __init__(
        self,
        base_url: str,
        token: str,
        model_name: str = DEFAULT_MODEL_NAME,
        timeout_seconds: float = 10.0,
        attempts: int = 2,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._model_name = model_name
        self._attempts = max(1, attempts)
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"X-Internal-Token": token},
            timeout=timeout_seconds,
            transport=transport,
        )

    @classmethod
    def from_env(cls) -> RemoteEmbeddingModel:
        token = os.environ.get("INTERNAL_CALLBACK_TOKEN")
        if not token:
            raise RuntimeError("Set INTERNAL_CALLBACK_TOKEN to call the embedding server.")
        return cls(
            os.environ["EMBEDDING_SERVICE_URL"],
            token,
            model_name=os.environ.get("EMBEDDING_MODEL_NAME") or DEFAULT_MODEL_NAME,
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = self._post(texts)
        # 저장 벡터는 모델 이름으로 조회하므로 서버 모델이 다르면 섞이지 않게 실패시킨다.
        if payload.get("model") != self._model_name:
            raise RuntimeError(
                f"Embedding server model {payload.get('model')!r} does not match {self._model_name!r}."
            )
        vectors = payload.get("vectors")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise RuntimeError("Embedding server returned an unexpected number of vectors.")
        return vectors

    def _post(self, texts: list[str]) -> dict:
        # 서버 재시작(모델 적재 중 503)이나 연결 끊김은 짧게 한 번 더 시도한다. 입력 오류(4xx)는 재시도하지 않는다.
        for attempt in range(1, self._attempts + 1):
            try:
                response = self._client.post("/embeddings", json={"texts": texts})
                if response.status_code < 500 or attempt == self._attempts:
                    response.raise_for_status()
                    return response.json()
            except httpx.TransportError:
                if attempt == self._attempts:
                    raise
        raise AssertionError("unreachable")
