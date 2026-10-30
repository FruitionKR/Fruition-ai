"""개념 병합 Jev 켬/끔 비교. 기존 개념 58개 + 새 개념 1개로 운영 판정기를 부르고, 운영 정규화를 거친 결정을 채점한다.

사용: PYTHONPATH=<dev>/pipeline python run_concept.py [--dry-run] [--limit N]
- 끔: LLM 병합(Haiku). 운영 편입 단계는 Claude를 쓰지 않으니 평가 전용 구성이다.
- 켬: 쌍별 판정(#142). 임베딩은 임베딩 서버와 같은 BGE-M3를 이 프로세스에 올려 쓴다(평가 전용).
"""
import argparse
import json
import os
import pathlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

sys.path.insert(0, os.path.dirname(__file__))
from common import EVAL_DIR, HAIKU, RUNS, load_keys, results_dir, verify_gold, write_jsonl  # noqa: E402

from app.modules.wiki_generation.infrastructure.concept_resolution import normalize_resolution_output  # noqa: E402


class RecordingJev:
    """후보별 Jev 답(선택·확률)을 incoming slug별로 모은다."""

    def __init__(self, client) -> None:
        self._client = client
        self._lock = Lock()
        self.answers: dict[str, dict] = {}

    def choose(self, state, questions):
        answers = self._client.choose(state, questions)
        incoming = json.loads(state)["incoming"]["slug"]
        with self._lock:
            self.answers.setdefault(incoming, {}).update(answers)
        return answers


def decision(resolutions: list[dict]) -> dict:
    item = next(iter(resolutions), None)
    if not item or item["decision"] == "create_new":
        return {"decision": "keep_new", "targets": []}
    if item["decision"] == "merge_into":
        return {"decision": "merge_into", "targets": [item["canonical_slug"]]}
    return {"decision": "link_to", "targets": item.get("link_targets") or []}


def score(case: dict, got: dict) -> dict:
    # 기대 답이나 허용 답 중 하나와 맞으면 정답이다. 추가 연결은 가장 잘 맞는 쪽 기준으로 센다.
    results = [_score(expected, got) for expected in [case["expected"], *case.get("acceptable", [])]]
    return min(results, key=lambda item: (not item["ok"], len(item["extra_links"])))


def _score(expected: dict, got: dict) -> dict:
    if expected["decision"] == "merge_into":
        ok = got == {"decision": "merge_into", "targets": [expected["canonical_slug"]]}
        extra = []
    elif expected["decision"] == "link_to":
        allowed = {expected["canonical_slug"], *expected.get("also_link", [])}
        ok = got["decision"] == "link_to" and bool(allowed & set(got["targets"]))
        extra = [t for t in got["targets"] if t not in allowed]
    else:
        ok = got["decision"] == "keep_new"
        extra = got["targets"]
    return {"ok": ok, "extra_links": extra}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--name", default="concept", help="결과 폴더 이름(판정 규칙 전후 비교용)")
    args = parser.parse_args()
    verify_gold()
    data = json.loads((EVAL_DIR / "concept.json").read_text())
    existing, cases = data["existing_concepts"], data["cases"]
    cases = cases[: args.limit] if args.limit else cases
    if args.dry_run:
        from app.modules.wiki_generation.infrastructure import jev_concept_resolver as r

        for case in cases:
            pool = [c for c in existing if c["definition"].strip()]
            req = r._request({**case["incoming"], "aliases": []}, [{**c} for c in pool])
            print(case["id"], case["expected"]["decision"], "후보", len(pool), "질문", len(req["questions"]))
        return

    load_keys("ANTHROPIC_API_KEY", "TYPESAFE_API_KEY")
    os.environ["AI_ALLOWED_LLM_MODELS"] = "*"
    from app.core.jev_client import JEV_CONCEPT_MERGE_ENABLED_ENV, build_jev_client
    from app.modules.wiki_embedding.infrastructure.bge_m3_embedding_model import BgeM3EmbeddingModel
    from app.modules.wiki_generation.infrastructure.chat_completions_llm import (
        ApiConceptResolver,
        ChatClientConfig,
        ChatCompletionsJsonClient,
    )
    from app.modules.wiki_generation.infrastructure.jev_concept_resolver import JevConceptResolver

    import app
    # 실행하는 dev 코드와 같은 체크아웃의 프롬프트를 쓴다.
    prompt_text = (pathlib.Path(app.__file__).resolve().parents[1] / "prompts" / "concept_resolution.system.md").read_text()
    api = ChatCompletionsJsonClient(ChatClientConfig(
        api_key=os.environ["ANTHROPIC_API_KEY"], model=HAIKU[1], temperature=None, timeout_seconds=120,
        max_tokens=None, json_mode=True, provider=HAIKU[0],
    ))
    llm = ApiConceptResolver(api, prompt_text)
    os.environ[JEV_CONCEPT_MERGE_ENABLED_ENV] = "true"
    client = build_jev_client(JEV_CONCEPT_MERGE_ENABLED_ENV)
    assert client is not None
    # 로컬 모델은 스레드 동시 호출에서 죽어서(segfault) 한 번 올린 뒤 호출을 직렬화한다.
    model, embed_lock = BgeM3EmbeddingModel(), Lock()
    model.embed(["warmup"])

    class embedder:
        model_name = model.model_name

        @staticmethod
        def embed(texts):
            with embed_lock:
                return model.embed(texts)

    def run(job):
        case, variant, run_index = job
        incoming = [{**case["incoming"], "aliases": []}]
        recorder = RecordingJev(client)
        resolver = JevConceptResolver(recorder, llm, embedder) if variant == "on" else llm
        started = time.monotonic()
        try:
            raw = resolver.resolve(incoming, existing, [])
            resolutions = normalize_resolution_output(raw, incoming, existing, [])
            got, error = decision([r for r in resolutions if r["decision"] != "create_new"]), None
        except Exception as exc:
            got, error = {"decision": "error", "targets": []}, f"{type(exc).__name__}: {str(exc)[:200]}"
        answers = recorder.answers.get(case["incoming"]["slug"], {})
        return {
            "id": case["id"], "category": case["category"], "variant": variant, "run": run_index,
            "expected": case["expected"], "got": got, **score(case, got), "error": error,
            # 켬에서 Jev에 보낸 후보와 후보별 선택·확률. 비었으면 정의가 없거나 이름 일치·대체 경로다.
            "jev_answers": {k: {"choice": v["choice"], "probabilities": v.get("probabilities")} for k, v in answers.items()},
            "seconds": round(time.monotonic() - started, 3),
        }

    jobs = [(case, variant, r) for case in cases for variant in ("off", "on") for r in range(RUNS)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(executor.map(run, jobs))
    out = results_dir(args.name)
    write_jsonl(out / "rows.jsonl", rows)
    for variant in ("off", "on"):
        mine = [row for row in rows if row["variant"] == variant]
        print(variant, sum(row["ok"] for row in mine), "/", len(mine), "errors", sum(bool(row["error"]) for row in mine),
              "extra_links", sum(len(row["extra_links"]) for row in mine))
    print("saved to", out)


if __name__ == "__main__":
    main()
