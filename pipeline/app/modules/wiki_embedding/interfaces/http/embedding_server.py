"""짧은 텍스트 전용 BGE-M3 임베딩 서버.

모델(약 2.5GB)을 이 프로세스 하나에만 올리고 query·agent·ingest 워커는 HTTP로 요청한다.
긴 페이지 임베딩은 실시간 요청을 밀어내므로 받지 않고, maintenance 워커가 직접 처리한다.

실행: uvicorn app.modules.wiki_embedding.interfaces.http.embedding_server:app
"""

from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from hmac import compare_digest

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.modules.wiki_embedding.infrastructure.bge_m3_embedding_model import BgeM3EmbeddingModel
from app.modules.wiki_embedding.infrastructure.remote_embedding_model import MAX_TEXT_CHARS, MAX_TEXTS_PER_REQUEST


class EmbeddingRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=MAX_TEXTS_PER_REQUEST)


class EmbeddingResponse(BaseModel):
    model: str
    vectors: list[list[float]]


_model = BgeM3EmbeddingModel()
_ready = threading.Event()
# 한 프로세스의 모델을 여러 요청이 동시에 돌리면 CPU를 나눠 쓰며 모두 느려지므로 차례로 처리한다.
_encode_lock = threading.Lock()


def _load_model() -> None:
    _model.embed(["warmup"])
    _ready.set()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    threading.Thread(target=_load_model, name="embedding-model-load", daemon=True).start()
    yield


app = FastAPI(title="Fruition Embedding Server", version="0.1.0", lifespan=lifespan)


def require_internal_token(token: str | None = Header(default=None, alias="X-Internal-Token")) -> None:
    """pipeline API와 같은 내부 토큰을 쓴다. 미설정이면 전체 거부(fail-closed)한다."""
    expected = os.environ.get("INTERNAL_CALLBACK_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="Internal token authentication is not configured.")
    if token is None or not compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid internal token.")


# async로 두어 encode 잠금을 기다리는 요청이 스레드 풀을 채워도 liveness 검사가 막히지 않게 한다.
@app.get("/health")
async def health() -> dict[str, str]:
    if not _ready.is_set():
        raise HTTPException(status_code=503, detail="Embedding model is loading.")
    return {"status": "ok", "model": _model.model_name}


@app.post("/embeddings", dependencies=[Depends(require_internal_token)])
def embeddings(request: EmbeddingRequest) -> EmbeddingResponse:
    if not _ready.is_set():
        raise HTTPException(status_code=503, detail="Embedding model is loading.")
    if any(len(text) > MAX_TEXT_CHARS for text in request.texts):
        raise HTTPException(status_code=413, detail=f"Each text must be at most {MAX_TEXT_CHARS} characters.")
    with _encode_lock:
        vectors = _model.embed(request.texts)
    return EmbeddingResponse(model=_model.model_name, vectors=vectors)
