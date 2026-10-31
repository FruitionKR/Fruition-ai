"""RAG 근거 선택 Jev 켬/끔 비교. 운영 질의 경로(후보 검색 → 그래프 → 근거 선택 → 답변)를 이 프로세스에서 부른다.

사용: PYTHONPATH=<dev>/pipeline python run_rag.py <workspace.json> [--limit N]
- 끔: 기존 근거 선택기 / 켬: JEV_EVIDENCE_ENABLED=true. 답변은 둘 다 Haiku, 웹 검색은 끈다.
- 질의 워커 환경을 따르되, 임베딩 서버 포트가 밖으로 열려 있지 않아 같은 BGE-M3를 이 프로세스에 올려 쓴다(평가 전용).
- 근거 채점(정답 문장 포함 여부)은 여기서 하고, 답변 채점은 grade_rag.py(Codex)가 한다.
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from common import EVAL_DIR, HAIKU, RUNS, results_dir, verify_gold, write_jsonl  # noqa: E402
from stack_env import load_container_env  # noqa: E402


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_`#>\[\]]", "", text)).strip().lower()


def evidence_hits(case: dict, snippets: list[dict], source_by_document: dict[str, str]) -> dict:
    """정답 근거 문장이 고른 근거에 들어 있는지(문장 단위), 고른 근거 중 정답 문서에서 온 비율(문서 단위)."""
    texts = [normalize(s["text"]) for s in snippets]
    found = [any(normalize(g["quote"]) in text for text in texts) for g in case["gold"]]
    gold_sources = {g["source_id"] for g in case["gold"]}
    from_gold = [source_by_document.get(s["source_document_id"] or "") in gold_sources for s in snippets]
    return {
        "gold_found": found,
        "recall": sum(found) / len(found) if found else None,
        "precision_doc": sum(from_gold) / len(from_gold) if from_gold else None,
        "evidence_count": len(snippets),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--name", default="rag", help="결과 폴더 이름(편입 방식 전후 비교용)")
    parser.add_argument("--ids", help="쉼표로 구분한 질문 id만 실행(일부 문서 편입 확인용)")
    args = parser.parse_args()
    verify_gold()
    workspace = json.loads(Path(args.workspace).read_text())
    source_by_document = {d["document_id"]: d["source_id"] for d in workspace["documents"]}
    cases = json.loads((EVAL_DIR / "rag.json").read_text())
    cases = cases[: args.limit] if args.limit else cases
    cases = [c for c in cases if c["id"] in args.ids.split(",")] if args.ids else cases

    load_container_env("fruition-query-task-worker-dev")
    os.environ.pop("EMBEDDING_SERVICE_URL", None)
    os.environ["AI_ALLOWED_LLM_MODELS"] = "*"
    from app.core.jev_client import JEV_EVIDENCE_ENABLED_ENV
    from functools import lru_cache

    from app.modules.query.infrastructure.stored_wiki_page_embedding_search import StoredWikiPageEmbeddingSearch
    from app.modules.query.interfaces.http import dependencies
    from app.modules.query.interfaces.http.dependencies import build_answer_query_use_case
    from app.modules.wiki_embedding.infrastructure.remote_embedding_model import MAX_TEXT_CHARS
    from app.modules.wiki_embedding.infrastructure.embedding_model import embedding_model_from_env

    # 로컬 모델은 스레드 동시 호출에서 죽는다(segfault). 한 번 올린 뒤 호출을 직렬화한다(평가 전용).
    from threading import Lock

    from app.modules.wiki_embedding.infrastructure.bge_m3_embedding_model import BgeM3EmbeddingModel

    embed_lock, original_embed = Lock(), BgeM3EmbeddingModel.embed

    def serialized_embed(self, texts):
        with embed_lock:
            return original_embed(self, texts)

    BgeM3EmbeddingModel.embed = serialized_embed
    BgeM3EmbeddingModel().embed(["warmup"])

    # 운영은 임베딩 서버가 있어 저장 벡터가 없는 긴 텍스트를 질문 시점에 임베딩하지 않는다(MAX_TEXT_CHARS).
    # 평가는 같은 모델을 이 프로세스에 올리므로 그 한도를 직접 맞춘다.
    dependencies._stored_embedding_search = lru_cache(maxsize=1)(
        lambda: StoredWikiPageEmbeddingSearch(embedding_model=embedding_model_from_env(), online_text_limit=MAX_TEXT_CHARS)
    )

    use_cases = {}
    for variant, enabled in (("off", "false"), ("on", "true")):
        os.environ[JEV_EVIDENCE_ENABLED_ENV] = enabled
        use_cases[variant] = build_answer_query_use_case(provider=HAIKU[0], model=HAIKU[1], allow_web_search=False)
    # 최종 결과의 근거는 답변이 인용한 것만 남는다(renumber_used_evidence). 선택기가 고른 근거와
    # 답변 프롬프트를 따로 남기려고 질의 문맥 생성을 감싸 스레드별로 마지막 문맥을 기록한다.
    import threading

    recorded = threading.local()
    for use_case in use_cases.values():
        builder = use_case._build_query_context
        original_execute = builder.execute

        def recording_execute(*a, _original=original_execute, **kw):
            context = _original(*a, **kw)
            recorded.context = context
            return context

        builder.execute = recording_execute
    selector = type(use_cases["on"]._build_query_context._evidence_selector).__name__
    assert selector == "JevEvidenceSelector", f"Jev 근거 선택기를 만들지 못했습니다: {selector}"

    def run(job):
        case, variant, run_index = job
        started = time.monotonic()
        recorded.context = None
        try:
            result = use_cases[variant].execute(case["question"], workspace_id=workspace["workspace_id"],
                                                user_id=workspace["user_id"], allow_web_search=False)
            snippets = [
                {"text": s.text, "source_document_id": s.source_document_id, "block_ids": list(s.source_block_ids)}
                for s in result.evidence_snippets
            ]
            # 답변 생성기는 근거 조각과 함께 관련 페이지 요약도 받는다. 근거 충실도 채점에 같이 넘긴다.
            pages = [{"title": item.page.title, "summary": item.page.summary} for item in result.related_pages[:8]]  # AnswerContextFormatter 기본값
            answer, error = result.answer.content, None
        except Exception as exc:
            snippets, pages, answer, error = [], [], None, f"{type(exc).__name__}: {str(exc)[:200]}"
        context = recorded.context
        selected = [
            {"text": s.text, "source_document_id": s.source_document_id, "block_ids": list(s.source_block_ids)}
            for s in (context.evidence_snippets if context else [])
        ]
        return {
            "id": case["id"], "category": case["category"], "answerable": case["answerable"], "variant": variant,
            "run": run_index, "answer": answer, "cited_evidence": snippets, "evidence": selected, "pages": pages,
            "answer_context": context.answer_context if context else None, "error": error,
            **evidence_hits(case, selected, source_by_document), "seconds": round(time.monotonic() - started, 2),
        }

    jobs = [(case, variant, r) for case in cases for variant in ("off", "on") for r in range(RUNS)]
    out = results_dir(args.name)
    rows = []
    with ThreadPoolExecutor(max_workers=4) as executor, (out / "rows.partial.jsonl").open("w") as partial:
        for row in executor.map(run, jobs):
            rows.append(row)
            partial.write(json.dumps(row, ensure_ascii=False) + "\n")
            partial.flush()
            if len(rows) % 20 == 0:
                print("done", len(rows), "/", len(jobs), flush=True)
    write_jsonl(out / "rows.jsonl", rows)
    for variant in ("off", "on"):
        mine = [row for row in rows if row["variant"] == variant and row["recall"] is not None]
        print(variant, "근거 재현율 평균", round(sum(r["recall"] for r in mine) / max(1, len(mine)), 3),
              "errors", sum(bool(r["error"]) for r in rows if r["variant"] == variant))
    print("saved to", out)


if __name__ == "__main__":
    main()
