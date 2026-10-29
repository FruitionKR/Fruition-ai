"""들어온 개념마다 후보 100개를 추리고 Jev가 후보 하나씩 같은 개념·관련 개념·무관을 판정하는 개념 병합기.

- 후보: 다른 incoming 개념과 기존 개념을 제목·별칭·정의로 BGE-M3 0.75 + BM25 0.25 점수 상위 100개
- 이름 일치: 제목이나 별칭이 후보의 slug·제목·별칭과 slug로 같으면 Jev 없이 병합한다.
- 판정: 판정 규칙은 state에 한 번만 두고, 후보마다 질문 하나(same/related/unrelated)를 요청 토큰 한도로 묶어 보낸다.
- 결정: same이 있으면 P(same)이 가장 높은 후보로 병합, 없고 related가 있으면 related 후보 모두와 연결한다.
정의가 없는 개념은 이름만으로 병합하는 오류가 관찰돼 Jev에 보내지 않고 새 개념으로 둔다. Jev는 missing hint를
판정하지 않으므로 hint가 있으면 기존 LLM 결과의 hint_resolutions만 쓴다. Jev나 임베딩을 쓸 수 없으면 기존
LLM 결과를 그대로 쓴다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
import unicodedata
from array import array
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import lru_cache
from typing import Any

from app.core.jev_batches import token_batches
from app.core.jev_client import (
    JEV_CONCEPT_MERGE_ENABLED_ENV,
    JevClient,
    JevUnavailable,
    build_jev_client,
    choice_probability,
)
from app.core.llm_prompt import with_llm_security_boundary
from app.modules.query.domain.scoring import hybrid_score
from app.modules.query.infrastructure.bm25_searcher import Bm25Searcher
from app.modules.wiki_embedding.application.ports import EmbeddingModelPort
from app.modules.wiki_embedding.infrastructure.remote_embedding_model import MAX_TEXT_CHARS, RemoteEmbeddingModel
from app.modules.wiki_generation.application.ports import ConceptResolver, JsonDict

CONCEPT_PAIR_RULE = (
    "Judge each question's candidate against the incoming concept using only the supplied titles, aliases, "
    "and definitions. same: the same concept under another name, such as a synonym, translation, abbreviation, "
    "or spelling variant, without losing distinct meaning or scope. related: a distinct concept that is clearly "
    "related: broader, narrower, part, stage, method, variant, a same-kind sibling under the same broader concept, "
    "or a directly dependent concept. unrelated: an unrelated concept, or one that shares only a domain or a "
    "source. Return no explanation."
)
PAIR_CRITERIA = {
    "same": "Same concept",
    "related": "Distinct but clearly related concept",
    "unrelated": "Unrelated, or only the same domain or source",
}
CANDIDATE_LIMIT = 100
CANDIDATE_EMBEDDING_WEIGHT = 0.75
MAX_WORKERS = 4
# ponytail: 프로세스 안 dict 캐시라 워커가 재시작하면 다시 임베딩한다. 한도를 넘으면 통째로 비운다.
EMBEDDING_CACHE_LIMIT = 20_000
_embedding_cache: dict[tuple[str, str], array] = {}
# 편입 작업은 워커 스레드에서 동시에 돌아 캐시 갱신을 한 번에 하나만 한다.
_embedding_cache_lock = threading.Lock()
logger = logging.getLogger(__name__)


def with_jev_concept_merge(resolver: ConceptResolver) -> ConceptResolver:
    """`JEV_CONCEPT_MERGE_ENABLED`와 API 키가 있으면 Jev로 병합을 판정하고, 아니면 기존 판정기를 쓴다."""
    return jev_concept_judge(resolver) or resolver


def jev_concept_judge(fallback: ConceptResolver | None = None) -> JevConceptResolver | None:
    """Jev 개념 판정기를 만든다. 쓸 수 없으면 None이다.

    `judge_concepts`만 쓰는 호출자(후속 편입·lint 승격)는 fallback 없이 만든다.
    """
    client = build_jev_client(JEV_CONCEPT_MERGE_ENABLED_ENV)
    if client is None:
        return None
    # 임베딩 서버가 없으면 후보를 거를 수 없어 Jev를 쓰지 않는다.
    embedding_model = embedding_server_model()
    if embedding_model is None:
        logger.warning("[Jev concept merge 꺼짐] 임베딩 서버를 쓸 수 없어 Jev 개념 판정을 쓰지 않습니다.")
        return None
    return JevConceptResolver(client, fallback, embedding_model)


def embedding_server_model() -> RemoteEmbeddingModel | None:
    """임베딩 서버 모델. 편입 워커는 BGE-M3를 직접 올리지 않으므로(ADR-0026) 서버가 없거나 설정이 잘못되면 None이다."""
    if not os.environ.get("EMBEDDING_SERVICE_URL"):
        return None
    try:
        return _remote_embedding_model()
    except RuntimeError as exc:
        logger.warning("[임베딩 서버 꺼짐] 설정 오류: %s", exc)
        return None


@lru_cache(maxsize=1)
def _remote_embedding_model() -> RemoteEmbeddingModel:
    return RemoteEmbeddingModel.from_env()


class JevConceptResolver(ConceptResolver):
    def __init__(
        self, client: JevClient, fallback: ConceptResolver | None, embedding_model: EmbeddingModelPort
    ) -> None:
        self._client = client
        self._fallback = fallback
        self._embedding_model = embedding_model
        self._text_search = Bm25Searcher()

    def resolve(
        self,
        incoming_concepts: list[JsonDict],
        existing_concepts: list[JsonDict],
        missing_related_hints: list[JsonDict] | None = None,
    ) -> JsonDict:
        try:
            resolutions = self._resolve_with_jev(incoming_concepts, existing_concepts)
        except JevUnavailable as exc:
            logger.warning("[Jev concept merge 대체] %s", exc)
            return self._fallback.resolve(incoming_concepts, existing_concepts, missing_related_hints)
        hint_resolutions = []
        if missing_related_hints:
            raw = self._fallback.resolve(incoming_concepts, existing_concepts, missing_related_hints)
            hint_resolutions = raw.get("hint_resolutions", [])
        return {"resolutions": resolutions, "hint_resolutions": hint_resolutions}

    def _resolve_with_jev(self, incoming: list[JsonDict], existing: list[JsonDict]) -> list[JsonDict]:
        pool = [
            *({**concept, "definition": concept.get("definition") or ""} for concept in incoming),
            # DB 개념 인덱스는 definition, 파일 개념 인덱스는 summary에 정의를 담는다.
            *({**concept, "definition": concept.get("definition") or concept.get("summary") or ""} for concept in existing),
        ]
        pool = [concept for concept in pool if concept.get("slug") and str(concept["definition"]).strip()]
        concepts = [concept for concept in incoming if str(concept.get("definition") or "").strip()]
        resolutions = self.judge_concepts(concepts, pool)
        # 병합 대상인 incoming 개념이 다시 다른 개념으로 병합되면 연쇄·순환이 생기므로 그 병합은 하지 않는다.
        merged = {slug for slug, item in resolutions.items() if item["decision"] == "merge_into"}
        return [
            item
            for item in resolutions.values()
            if item["decision"] != "merge_into" or item["canonical_slug"] not in merged
        ]

    def judge_concepts(self, concepts: list[JsonDict], pool: list[JsonDict]) -> dict[str, JsonDict]:
        """개념마다 pool(slug·title·aliases·definition)에서 후보를 추려 판정한다.

        병합·연결할 개념의 판정만 incoming slug별로 돌려준다. 연쇄 병합은 거르지 않는다.
        """
        # 후보 slug가 질문 키라 slug마다 하나만 둔다.
        pool = list({concept["slug"]: concept for concept in pool}.values())
        resolutions: dict[str, JsonDict] = {}
        tasks: list[tuple[JsonDict, list[JsonDict]]] = []
        incoming_slugs = {concept["slug"] for concept in concepts}
        for concept in concepts:
            others = [candidate for candidate in pool if candidate["slug"] != concept["slug"]]
            if target := _exact_name_match(concept, others, incoming_slugs):
                resolutions[concept["slug"]] = _merge(concept["slug"], target)
                logger.info("[Jev concept merge] %s 후보=%d 요청=0 결정=merge_into:%s(이름 일치)",
                            concept["slug"], len(others), target)
            elif others:
                tasks.append((concept, others))
        if not tasks:
            return resolutions
        vectors = self._embed([_concept_text(concept) for concept in [*pool, *concepts]])
        requests = [
            (concept, batch)
            for concept, others in tasks
            for batch in token_batches(
                _shuffled(concept, self._candidates(concept, others, vectors)),
                lambda batch, concept=concept: _request(concept, batch),
                max_items=CANDIDATE_LIMIT,
            )
        ]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [
                executor.submit(copy_context().run, self._client.choose, **_request(concept, batch))
                for concept, batch in requests
            ]
            answers: dict[str, dict[str, JsonDict]] = {}
            for (concept, _), future in zip(requests, futures, strict=True):
                answers.setdefault(concept["slug"], {}).update(future.result())
        for concept, others in tasks:
            slug = concept["slug"]
            request_count = sum(1 for item, _ in requests if item["slug"] == slug)
            resolution = _decide(slug, answers[slug])
            if resolution:
                resolutions[slug] = resolution
            logger.info(
                "[Jev concept merge] %s 후보=%d 요청=%d 결정=%s 상위=%s",
                slug,
                len(answers[slug]),
                request_count,
                resolution["decision"] + ":" + resolution["canonical_slug"] if resolution else "create_new",
                _top_probabilities(answers[slug]),
            )
        return resolutions

    def _candidates(self, concept: JsonDict, others: list[JsonDict], vectors: dict[str, array]) -> list[JsonDict]:
        query = _concept_text(concept)
        texts = [_concept_text(candidate) for candidate in others]
        scores = [
            hybrid_score(vector_similarity(vectors[query], vectors[text]), lexical, CANDIDATE_EMBEDDING_WEIGHT)
            for text, lexical in zip(texts, self._text_search.score(query, texts), strict=True)
        ]
        ranked = sorted(zip(scores, others, strict=True), key=lambda row: (-row[0], row[1]["slug"]))
        return [candidate for _, candidate in ranked[:CANDIDATE_LIMIT]]

    def _embed(self, texts: list[str]) -> dict[str, array]:
        try:
            return cached_embeddings(self._embedding_model, texts)
        except Exception as exc:  # 임베딩 없이 BM25만으로 후보를 고르지 않고 기존 판정기로 넘긴다.
            raise JevUnavailable(f"개념 임베딩 실패: {type(exc).__name__}") from None


def cached_embeddings(model: EmbeddingModelPort, texts: list[str]) -> dict[str, array]:
    """텍스트 벡터를 돌려준다. 기존 개념·클러스터는 편입마다 다시 오므로 (모델, 텍스트 해시)로 캐시한다."""
    model_name = model.model_name
    keys = {text: (model_name, hashlib.sha256(text.encode("utf-8")).hexdigest()) for text in texts}
    with _embedding_cache_lock:
        found = {text: _embedding_cache[key] for text, key in keys.items() if key in _embedding_cache}
    missing = [text for text in keys if text not in found]
    if missing:
        # 임베딩 서버는 텍스트당 MAX_TEXT_CHARS까지만 받는다. 개념 텍스트는 보통 그보다 훨씬 짧다.
        vectors = model.embed([text[:MAX_TEXT_CHARS] for text in missing])
        for text, vector in zip(missing, vectors, strict=True):
            found[text] = array("f", vector)
        # 결과는 위에서 모은 found로 돌려준다. 캐시를 비워도 이번 결과에는 영향이 없다.
        with _embedding_cache_lock:
            if len(_embedding_cache) + len(missing) > EMBEDDING_CACHE_LIMIT:
                _embedding_cache.clear()
            _embedding_cache.update({keys[text]: found[text] for text in missing})
    return found


def _exact_name_match(concept: JsonDict, candidates: list[JsonDict], incoming_slugs: set[str]) -> str | None:
    """제목·별칭이 후보의 slug·제목·별칭과 글자 그대로 같은 후보를 고른다.

    기존 개념을 먼저 고른다. 같은 편입의 개념끼리는 이름 일치가 양쪽으로 성립하므로, 서로 병합해
    둘 다 남지 않도록 slug가 앞선 쪽만 정본으로 둔다.
    """
    names = _names(concept.get("title"), *(concept.get("aliases") or []))
    matches = [
        candidate["slug"]
        for candidate in candidates
        if names & _names(candidate["slug"], candidate.get("title"), *(candidate.get("aliases") or []))
    ]
    existing = [slug for slug in matches if slug not in incoming_slugs]
    if existing:
        return min(existing)
    return min((slug for slug in matches if slug < concept["slug"]), default=None)


def _names(*names: object) -> set[str]:
    """대소문자·전각/반각·공백·하이픈만 맞춘 이름. 기호를 지우는 slugify로 비교하면 `C#`과 `C`가 같아진다."""
    normalized = {
        " ".join(unicodedata.normalize("NFKC", str(name)).casefold().replace("-", " ").replace("_", " ").split())
        for name in names
        if name
    }
    return normalized - {"", "untitled"}


def _shuffled(concept: JsonDict, candidates: list[JsonDict]) -> list[JsonDict]:
    # 평가와 같이 후보 순서를 고정하지 않아 순위가 단서가 되지 않게 한다.
    candidates = list(candidates)
    random.Random(hashlib.sha256(str(concept["slug"]).encode("utf-8")).hexdigest()).shuffle(candidates)
    return candidates


def _request(concept: JsonDict, candidates: list[JsonDict]) -> dict[str, Any]:
    # 규칙은 질문마다 반복하지 않고 state에 한 번만 둔다. 보안 경계는 클라이언트가 붙이는 그대로 토큰에 센다.
    state = {
        "rule": CONCEPT_PAIR_RULE,
        "incoming": {
            "slug": concept["slug"],
            "title": concept.get("title"),
            "aliases": concept.get("aliases") or [],
            "definition": concept.get("definition"),
        },
        "candidates": [
            {
                "id": candidate["slug"],
                "title": candidate.get("title"),
                "aliases": candidate.get("aliases") or [],
                "definition": candidate["definition"],
            }
            for candidate in candidates
        ],
    }
    return {
        "state": json.dumps(state, ensure_ascii=False),
        "questions": {
            candidate["slug"]: {
                "type": "choice",
                "instructions": with_llm_security_boundary(f"Judge candidate {candidate['slug']} against incoming."),
                "criteria": PAIR_CRITERIA,
            }
            for candidate in candidates
        },
    }


def _decide(slug: str, answers: dict[str, JsonDict]) -> JsonDict | None:
    same = {target: choice_probability(answer, "same") for target, answer in answers.items() if answer["choice"] == "same"}
    if same:
        return _merge(slug, min(same, key=lambda target: (-same[target], target)))
    related = {
        target: choice_probability(answer, "related") for target, answer in answers.items() if answer["choice"] == "related"
    }
    if not related:
        return None
    targets = sorted(related, key=lambda target: (-related[target], target))
    return {
        "incoming_slug": slug,
        "decision": "link_to",
        "canonical_slug": targets[0],
        "link_targets": targets,
        "link_confidences": {target: related[target] for target in targets},
        "alias_to_add": None,
    }


def _merge(slug: str, target: str) -> JsonDict:
    return {"incoming_slug": slug, "decision": "merge_into", "canonical_slug": target, "alias_to_add": None}


def _top_probabilities(answers: dict[str, JsonDict], limit: int = 3) -> str:
    def best(answer: JsonDict) -> float:
        return max(choice_probability(answer, "same"), choice_probability(answer, "related"))

    top = sorted(answers.items(), key=lambda row: -best(row[1]))[:limit]
    return ", ".join(
        f"{target}(same={choice_probability(answer, 'same'):.2f}, related={choice_probability(answer, 'related'):.2f})"
        for target, answer in top
    )


def vector_similarity(left: array, right: array) -> float:
    return max(0.0, min(1.0, float(sum(a * b for a, b in zip(left, right, strict=True)))))


def _concept_text(concept: JsonDict) -> str:
    # 후보 거르기 점수용 글이다. 태그는 이름이 전혀 다른 같은·관련 개념을 후보에 들게 하는 색인이고, Jev 판정 입력에는 넣지 않는다.
    return " ".join(
        str(part)
        for part in (
            concept.get("title"),
            *(concept.get("aliases") or []),
            *(concept.get("tags") or []),
            concept.get("definition"),
        )
        if part
    )
