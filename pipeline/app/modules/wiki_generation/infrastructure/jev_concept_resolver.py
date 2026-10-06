"""들어온 개념마다 후보 3개를 추리고 Jev가 같은 개념인지 판정하는 개념 병합기.

평가(output/reports/09_concept-merge-jev-vs-llm-final.md)와 같이 요청 하나에 판정 하나만 담고,
정의만 근거로 후보 3개와 keep_new 중 하나를 고른다. 정의가 없는 개념은 이름만으로 병합하는 오류가
관찰돼 Jev에 보내지 않고 새 개념으로 둔다. Jev는 missing hint를 판정하지 않으므로 hint가 있으면
기존 LLM 결과의 hint_resolutions만 쓴다. Jev를 쓸 수 없으면 기존 LLM 결과를 그대로 쓴다.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

from app.core.jev_client import JEV_CONCEPT_MERGE_ENABLED_ENV, JevClient, JevUnavailable, build_jev_client
from app.modules.query.infrastructure.bm25_searcher import Bm25Searcher
from app.modules.wiki_generation.application.ports import ConceptResolver, JsonDict

CONCEPT_MERGE_RULE = (
    "Resolve concept identity using only the supplied definitions. Merge only synonyms without losing "
    "distinct meaning or scope. A related, broader, narrower, composite or insufficiently defined concept "
    "stays keep_new. Compare each task only to its own candidates. A listed candidate may be existing or "
    "incoming. Choose one allowed option. Return no explanation."
)
KEEP_NEW = "keep_new"
CANDIDATE_LIMIT = 3
MAX_WORKERS = 4
logger = logging.getLogger(__name__)


def with_jev_concept_merge(resolver: ConceptResolver) -> ConceptResolver:
    """`JEV_CONCEPT_MERGE_ENABLED`와 API 키가 있으면 Jev로 병합을 판정하고, 아니면 기존 판정기를 쓴다."""
    client = build_jev_client(JEV_CONCEPT_MERGE_ENABLED_ENV)
    if client is None:
        return resolver
    return JevConceptResolver(client, resolver)


class JevConceptResolver(ConceptResolver):
    def __init__(self, client: JevClient, fallback: ConceptResolver) -> None:
        self._client = client
        self._fallback = fallback
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
            *({**concept, "definition": concept.get("definition") or "", "location": "incoming"} for concept in incoming),
            # DB 개념 인덱스는 definition, 파일 개념 인덱스는 summary에 정의를 담는다.
            *(
                {**concept, "definition": concept.get("definition") or concept.get("summary") or "", "location": "existing"}
                for concept in existing
            ),
        ]
        pool = [concept for concept in pool if concept.get("slug") and str(concept["definition"]).strip()]
        tasks = [
            (concept, candidates)
            for concept in incoming
            if str(concept.get("definition") or "").strip()
            and (candidates := self._candidates(concept, pool))
        ]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(copy_context().run, self._judge, concept, candidates) for concept, candidates in tasks]
            choices = {concept["slug"]: future.result() for (concept, _), future in zip(tasks, futures, strict=True)}
        merges = {slug: target for slug, target in choices.items() if target != KEEP_NEW}
        # 병합 대상인 incoming 개념이 다시 다른 개념으로 병합되면 연쇄·순환이 생기므로 그 병합은 하지 않는다.
        return [
            {"incoming_slug": slug, "decision": "merge_into", "canonical_slug": target, "alias_to_add": None}
            for slug, target in merges.items()
            if target not in merges
        ]

    def _candidates(self, concept: JsonDict, pool: list[JsonDict]) -> list[JsonDict]:
        others = [candidate for candidate in pool if candidate["slug"] != concept["slug"]]
        if not others:
            return []
        scores = self._text_search.score(_concept_text(concept, concept.get("definition")), [
            _concept_text(candidate, candidate["definition"]) for candidate in others
        ])
        ranked = sorted(zip(scores, others, strict=True), key=lambda row: (-row[0], row[1]["slug"]))
        return [candidate for score, candidate in ranked[:CANDIDATE_LIMIT] if score > 0]

    def _judge(self, concept: JsonDict, candidates: list[JsonDict]) -> str:
        state = {
            "q0": {
                "incoming": {
                    "slug": concept["slug"],
                    "title": concept.get("title"),
                    "definition": concept.get("definition"),
                },
                "candidates": [
                    {
                        "slug": candidate["slug"],
                        "title": candidate.get("title"),
                        "aliases": candidate.get("aliases") or [],
                        "definition": candidate["definition"],
                        "location": candidate["location"],
                    }
                    for candidate in candidates
                ],
            }
        }
        options = [candidate["slug"] for candidate in candidates] + [KEEP_NEW]
        answers = self._client.choose(
            json.dumps(state, ensure_ascii=False),
            {
                "q0": {
                    "type": "choice",
                    "instructions": f"{CONCEPT_MERGE_RULE} Evaluate task q0 only.",
                    "criteria": {
                        option: "Keep incoming separate" if option == KEEP_NEW else f"Merge into candidate {option}"
                        for option in options
                    },
                }
            },
        )
        return answers["q0"]["choice"]


def _concept_text(concept: JsonDict, definition: object) -> str:
    return " ".join(
        str(part)
        for part in (concept.get("title"), *(concept.get("aliases") or []), definition)
        if part
    )
