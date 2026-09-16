from __future__ import annotations

from hmac import compare_digest
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator, metrics

from app.modules.task_cancellation.interfaces.http.routes import router as task_cancellation_router
from app.modules.agent.interfaces.http.routes import router as agent_router
from app.modules.agent_run.interfaces.http.routes import internal_router as agent_run_status_router
from app.modules.agent_run.interfaces.http.routes import router as agent_run_router
from app.modules.query.interfaces.http.routes import router as query_router
from app.modules.skill.interfaces.http.routes import agent_router as agent_skill_router
from app.modules.skill.interfaces.http.routes import router as skill_router
from app.modules.wiki_ingestion.interfaces.http.routes import router as pipeline_router
from app.modules.wiki_ingestion.interfaces.http.schemas import (
    CHAT_SEMANTIC_PROMPT,
    DOCUMENT_SEMANTIC_PROMPT,
    ChatWikiRunIn,
    PipelineRunIn,
    PipelineRunOut,
    ReingestRunIn,
)
from app.modules.wiki_schema.interfaces.http.routes import router as wiki_schema_router
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database

logger = logging.getLogger(__name__)
AGENT_SKILLS_ENABLED = os.environ.get("AGENT_SKILLS_ENABLED", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
SKILL_API_ENABLED = os.environ.get("SKILL_API_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def require_internal_token(
    token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    expected = os.environ.get("INTERNAL_CALLBACK_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="Internal service authentication is not configured.")
    if token is None or not compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid internal service token.")


def require_agent_service_token(
    token: str | None = Header(default=None, alias="X-Agent-Service-Token"),
) -> None:
    expected = os.environ.get("AGENT_INTERNAL_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="Agent service authentication is not configured.")
    if token is None or not compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid Agent service token.")


def require_internal_token(
    token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    """backend와 공유하는 내부 토큰 검증. 미설정이면 전체 거부(fail-closed)한다."""
    expected = os.environ.get("INTERNAL_CALLBACK_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="Internal token authentication is not configured.")
    if token is None or not compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid internal token.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        database.ensure_ai_schema()
        logger.info("[startup] ai_db 스키마 확인 완료")
    except Exception:
        logger.exception("[startup] ai_db 스키마 확인 실패")
        raise
    if AGENT_SKILLS_ENABLED:
        try:
            database.verify_agent_schema()
            logger.info("[startup] core_db Agent 스키마 확인 완료")
        except Exception:
            logger.exception("[startup] core_db Agent 스키마 확인 실패")
            raise
    yield


app = FastAPI(title="Fruition Pipeline Lab API", version="0.1.0", lifespan=lifespan)


# 내부 토큰이 필요한 route의 경로 패턴. include_internal_router가 채우므로
# 라우터 등록과 middleware 검사 대상이 갈라지지 않는다.
INTERNAL_TOKEN_ROUTE_PATTERNS: list[Any] = []


def include_internal_router(router, dependencies) -> None:
    """내부 토큰이 필요한 라우터를 등록하고 그 경로를 middleware 검사 대상에 함께 넣는다."""
    app.include_router(router, dependencies=dependencies)
    INTERNAL_TOKEN_ROUTE_PATTERNS.extend(route.path_regex for route in router.routes)


@app.middleware("http")
async def authenticate_internal_request(request: Request, call_next):
    """본문 파싱(422)보다 내부 토큰 검증(401/503)이 먼저 판정되도록 middleware에서 막는다."""
    path = request.url.path
    if any(pattern.match(path) for pattern in INTERNAL_TOKEN_ROUTE_PATTERNS):
        try:
            require_internal_token(request.headers.get("X-Internal-Token"))
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
            )
    return await call_next(request)


internal_token_dependencies = [Depends(require_internal_token)]
# agent turn은 Skill 기능이 켜져 있어도 내부 토큰이 필요하다 (그 위에 agent service token이 더 붙는다).
include_internal_router(
    agent_router,
    internal_token_dependencies + [Depends(require_agent_service_token)]
    if AGENT_SKILLS_ENABLED
    else internal_token_dependencies,
)
include_internal_router(query_router, internal_token_dependencies)
include_internal_router(pipeline_router, internal_token_dependencies)
include_internal_router(task_cancellation_router, internal_token_dependencies)
include_internal_router(wiki_schema_router, internal_token_dependencies)
include_internal_router(agent_run_status_router, internal_token_dependencies)
agent_service_dependencies = [Depends(require_agent_service_token)]
if AGENT_SKILLS_ENABLED:
    # agent run API는 내부 토큰이 아니라 agent service token으로만 보호한다.
    app.include_router(agent_run_router, dependencies=agent_service_dependencies)
    app.include_router(agent_skill_router, dependencies=agent_service_dependencies)
if SKILL_API_ENABLED:
    app.include_router(skill_router, dependencies=agent_service_dependencies)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/documents/{document_id}", dependencies=internal_token_dependencies)
def get_document(document_id: str) -> dict:
    try:
        document = database.get_document(document_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    return document


# app에 직접 붙인 route는 include_internal_router를 타지 않으므로 여기서 검사 대상에 넣는다.
# 인덱스(app.routes[-1])는 아래에서 /metrics를 더 등록하는 순간 다른 route를 가리킨다.
# 인증 자체는 route의 dependencies가 담당하고 이 등록은 판정 순서(401 먼저)를 맞출 뿐이지만,
# 어긋나면 조용히 무의미해지므로 경로로 못박는다.
_DOCUMENT_ROUTE_PATH = "/documents/{document_id}"
_document_route = next(
    (route for route in app.routes if getattr(route, "path", None) == _DOCUMENT_ROUTE_PATH),
    None,
)
if _document_route is None:
    raise RuntimeError(f"내부 토큰 검사 대상 route를 찾지 못했습니다: {_DOCUMENT_ROUTE_PATH}")
INTERNAL_TOKEN_ROUTE_PATTERNS.append(_document_route.path_regex)


# Prometheus 지표 수집과 /metrics 노출.
# 파일 맨 끝에서 등록하는 이유: FastAPI middleware는 나중에 추가한 것이 바깥에서 돈다.
# 내부 토큰 middleware가 401로 끊는 요청도 지표에 남기려면 계측이 바깥에 있어야 한다.
# /metrics 자체는 INTERNAL_TOKEN_ROUTE_PATTERNS에 넣지 않으므로 /health와 같이 인증 없이 열린다.
#
# 버킷을 기본값에서 바꾸는 이유: handler별 기본 버킷이 0.1/0.5/1초 세 개뿐이라
# 1초를 넘는 요청이 전부 +Inf로 뭉쳐 p95를 낼 수 없다.
# 범위를 5ms~60초로 넓게 잡는다. 이 API는 두 종류의 요청이 섞여 있다 —
# LLM을 끼는 무거운 처리(document-svc의 타임아웃 기준 query 30초, wiki-schema 60초)와
# document-svc가 3초 주기로 폴링하는 가벼운 상태 조회(타임아웃 5초)다.
# 하한이 높으면 후자의 p95가 첫 버킷 보간값으로 고정돼 쓸모가 없어진다.
_LATENCY_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0,
)

Instrumentator(excluded_handlers=["/metrics"]).add(
    metrics.default(
        latency_highr_buckets=_LATENCY_BUCKETS,
        latency_lowr_buckets=_LATENCY_BUCKETS,
    )
).instrument(app).expose(app, include_in_schema=False)
