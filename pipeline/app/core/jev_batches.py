"""Jev 요청 토큰을 추정해 질문을 요청별로 묶는다.

평가 실행기(run_jev_controlled.batches)와 같이 model을 포함한 요청 본문 전체를 cl100k_base로 센다.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from functools import lru_cache
from typing import Any, TypeVar

import tiktoken

from app.core.jev_client import JEV_MODEL, JevUnavailable, with_security_boundary

# Jev는 state와 가장 긴 질문 합계에 32k 제한이 있어 tokenizer 차이 여유를 둔다.
MAX_BATCH_TOKENS = 28_000
BATCH_TOKEN_MARGIN = 64
T = TypeVar("T")


def token_batches(
    items: Sequence[T],
    build_request: Callable[[list[T]], dict[str, Any]],
    *,
    max_tokens: int = MAX_BATCH_TOKENS,
    max_items: int,
) -> list[list[T]]:
    """빈 요청 크기에 항목 하나짜리 요청 크기를 더해 가며 토큰·개수 한도 안에서 묶는다."""
    header = request_tokens(build_request([])) + BATCH_TOKEN_MARGIN
    batches: list[list[T]] = []
    current: list[T] = []
    estimate = header
    for item in items:
        size = request_tokens(build_request([item])) + BATCH_TOKEN_MARGIN
        if current and (len(current) >= max_items or estimate + size > max_tokens):
            batches.append(current)
            current, estimate = [], header
        current.append(item)
        estimate += size
    if current:
        batches.append(current)
    return batches


def request_tokens(request: dict[str, Any]) -> int:
    try:
        encoding = _encoding()
    except Exception as exc:  # tokenizer 파일을 못 읽으면 크기를 알 수 없어 Jev를 쓰지 않는다.
        raise JevUnavailable(f"tokenizer를 불러오지 못했습니다: {type(exc).__name__}") from None
    # 클라이언트가 보내는 그대로 state에 보안 경계를 넣고 센다.
    body = {"model": JEV_MODEL, **request, "state": with_security_boundary(request["state"])}
    return len(encoding.encode(json.dumps(body, ensure_ascii=False, sort_keys=True)))


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding("cl100k_base")
