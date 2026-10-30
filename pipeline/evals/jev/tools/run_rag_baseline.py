"""Wiki 없이 원문 청크만 검색하는 일반 RAG 대조군. 같은 질문·답변 모델·답변 프롬프트로 Wiki 경로(run_rag.py)와 비교한다.

사용: PYTHONPATH=<dev>/pipeline python run_rag_baseline.py [--limit N] [--name rag_baseline]
- 청크: corpus 문서를 운영 블록 추출기로 나눈 뒤 이어지는 블록을 CHUNK_CHARS 이하로 묶는다.
- 검색: 운영 근거 후보와 같은 하이브리드 점수(BGE-M3 0.75 + BM25 0.25) 상위 TOP_K개.
- 답변: 운영 답변 생성기(QueryChatAnswerGenerator)와 답변 문맥 형식(AnswerContextFormatter). 관련 페이지·그래프는 없다.
결과 행은 run_rag.py와 같은 형식(variant="baseline")이라 grade_rag.py·analyze_rag.py로 채점한다.
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

sys.path.insert(0, os.path.dirname(__file__))
from common import EVAL_DIR, HAIKU, RUNS, load_keys, results_dir, verify_gold, write_jsonl  # noqa: E402
from run_rag import evidence_hits  # noqa: E402

# 답변 문맥이 근거 조각을 900자에서 자르므로(AnswerContextFormatter) 청크도 그 안에 들어가게 한다.
CHUNK_CHARS = 900
TOP_K = 8  # 운영 QUERY_EVIDENCE_LIMIT 기본값
EMBEDDING_WEIGHT = 0.75


def chunks(source_id: str, markdown: str, extractor) -> list[dict]:
    _, blocks = extractor.extract_text(markdown, source_path=f"{source_id}.md", fallback_title=source_id)
    out, current, ids = [], "", []
    for block in blocks:
        text = block.text.strip()
        if current and len(current) + len(text) + 1 > CHUNK_CHARS:
            out.append({"source_id": source_id, "block_ids": ids, "text": current})
            current, ids = "", []
        # 블록 하나가 한도보다 길면 한도 단위로 자른다.
        while len(text) > CHUNK_CHARS:
            out.append({"source_id": source_id, "block_ids": [block.block_id], "text": text[:CHUNK_CHARS]})
            text = text[CHUNK_CHARS:]
        current, ids = (f"{current}\n{text}" if current else text), [*ids, block.block_id]
    if current:
        out.append({"source_id": source_id, "block_ids": ids, "text": current})
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    parser.add_argument("--name", default="rag_baseline")
    args = parser.parse_args()
    verify_gold()
    cases = json.loads((EVAL_DIR / "rag.json").read_text())
    cases = cases[: args.limit] if args.limit else cases
    load_keys("ANTHROPIC_API_KEY")
    os.environ["AI_ALLOWED_LLM_MODELS"] = "*"
    from app.modules.query.application.answer_context_formatter import AnswerContextFormatter
    from app.modules.query.domain.entities import EvidenceSnippet, GraphContext, QueryContext
    from app.modules.query.domain.scoring import hybrid_score
    from app.modules.query.infrastructure.bm25_searcher import Bm25Searcher
    from app.modules.query.application.query_answer_assembler import QueryAnswerAssembler
    from app.modules.query.infrastructure.query_chat_answer_generator import build_query_chat_answer_generator
    from app.modules.wiki_embedding.infrastructure.bge_m3_embedding_model import BgeM3EmbeddingModel
    from app.modules.wiki_generation.infrastructure.extract import MarkdownBlockExtractor

    extractor = MarkdownBlockExtractor()
    corpus = [
        chunk
        for path in sorted((EVAL_DIR / "corpus").glob("*.md"))
        for chunk in chunks(path.stem, path.read_text().replace("\x00", ""), extractor)
    ]
    model, embed_lock = BgeM3EmbeddingModel(), Lock()
    vectors = model.embed([chunk["text"] for chunk in corpus])
    bm25 = Bm25Searcher()
    formatter = AnswerContextFormatter()
    # 운영 질의 답변과 같은 생성기·프롬프트. 워크스페이스·사용자가 없어 스키마 프롬프트는 비어 있다.
    generator = build_query_chat_answer_generator(provider=HAIKU[0], model=HAIKU[1])
    assembler = QueryAnswerAssembler(generator)
    print("청크", len(corpus), flush=True)

    def retrieve(question: str) -> list[dict]:
        with embed_lock:  # 로컬 모델은 동시 호출에서 죽는다.
            query = model.embed([question])[0]
        dense = [sum(a * b for a, b in zip(query, vector)) for vector in vectors]
        lexical = bm25.score(question, [chunk["text"] for chunk in corpus])
        scores = [hybrid_score(d, t, EMBEDDING_WEIGHT) for d, t in zip(dense, lexical)]
        order = sorted(range(len(corpus)), key=lambda i: -scores[i])[:TOP_K]
        return [corpus[i] for i in order]

    def run(job):
        case, run_index = job
        started = time.monotonic()
        try:
            picked = retrieve(case["question"])
            snippets = [EvidenceSnippet(rank=i, source_document_id=c["source_id"], source_block_ids=c["block_ids"], text=c["text"])
                        for i, c in enumerate(picked, start=1)]
            context = QueryContext(
                question=case["question"], graph_context=GraphContext(), traversal_paths=[], related_pages=[],
                evidence_snippets=snippets, answer_context=formatter.format(case["question"], [], [], snippets),
            )
            # 운영처럼 답변이 인용한 근거만 남기고 번호를 다시 매긴다.
            generated, cited = assembler.renumber_used_evidence(generator.generate_answer(context), snippets)
            answer, error = generated.content, None
        except Exception as exc:
            picked, cited, context, answer, error = [], [], None, None, f"{type(exc).__name__}: {str(exc)[:200]}"
        selected = [{"text": c["text"], "source_document_id": c["source_id"], "block_ids": c["block_ids"]} for c in picked]
        cited_rows = [{"text": s.text, "source_document_id": s.source_document_id, "block_ids": list(s.source_block_ids)} for s in cited]
        return {
            "id": case["id"], "category": case["category"], "answerable": case["answerable"], "variant": "baseline",
            "run": run_index, "answer": answer, "cited_evidence": cited_rows, "evidence": selected, "pages": [],
            "answer_context": context.answer_context if context else None, "error": error,
            **evidence_hits(case, selected, {c["source_id"]: c["source_id"] for c in corpus}),
            "seconds": round(time.monotonic() - started, 2),
        }

    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(executor.map(run, [(case, r) for case in cases for r in range(RUNS)]))
    out = results_dir(args.name)
    write_jsonl(out / "rows.jsonl", rows)
    answerable = [r for r in rows if r["recall"] is not None]
    print("근거 재현율 평균", round(sum(r["recall"] for r in answerable) / max(1, len(answerable)), 3), "errors", sum(bool(r["error"]) for r in rows))
    print("saved to", out)


if __name__ == "__main__":
    main()
