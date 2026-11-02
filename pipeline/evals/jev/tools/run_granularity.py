"""개념 크기 측정(ADR-0042 0단계): 같은 corpus 11개를 모델마다 새 워크스페이스에 편입하고 개념 페이지 수와 이름 겹침을 잰다.

사용: PYTHONPATH=<dev>/pipeline python run_granularity.py [--models claude:claude-haiku-4-5-20251001,claude:<다른 모델>]
      PYTHONPATH=<dev>/pipeline python run_granularity.py --workspaces <a>/workspace.json,<b>/workspace.json
- 편입은 모델마다 rag_prepare.py --model 을 따로 실행한다(results/<날짜>/granularity-<모델>/workspace.json).
- --workspaces 를 주면 편입 없이 이미 편입한 워크스페이스만 잰다.
- 결과: results/<날짜>/granularity/report.json
"""
import argparse
import json
import os
import re
import subprocess
import sys
from itertools import permutations
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from common import HAIKU, results_dir, verify_gold  # noqa: E402
from stack_env import load_container_env  # noqa: E402


def _key(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold())


def names(concept: dict) -> set[str]:
    """비교용 이름: 제목·별칭에서 대소문자·기호·공백을 지운 것. 괄호 한정어를 뗀 이름도 더한다."""
    raw = [concept["title"], *concept.get("aliases", [])]
    raw += [re.sub(r"\(.*?\)", "", name) for name in raw]
    return {key for name in raw if (key := _key(name))}


def heads(title: str) -> set[str]:
    """머리말: 제목의 첫 단어와 괄호 안 말. `Deployment Rollout`과 `Label Selector (Deployment)`는 둘 다 deployment다."""
    words = title.split()[:1] + re.findall(r"\((.*?)\)", title)
    return {key for word in words if (key := _key(word))}


def head_groups(concepts: list[dict]) -> dict[str, list[str]]:
    """머리말이 같은 개념 페이지가 2개 이상인 묶음."""
    groups: dict[str, list[str]] = {}
    for concept in concepts:
        for head in heads(concept["title"]):
            groups.setdefault(head, []).append(concept["title"])
    return {head: sorted(titles) for head, titles in sorted(groups.items()) if len(titles) > 1}


def alignment(a: list[dict], b: list[dict]) -> float | None:
    """a의 개념 중 이름·별칭이 b의 어떤 개념 이름·별칭과 하나라도 같은 비율. 방향이 있다(a→b ≠ b→a)."""
    b_names = set().union(*(names(c) for c in b)) if b else set()
    return round(sum(bool(names(c) & b_names) for c in a) / len(a), 3) if a else None


def summarize(concepts: list[dict], slugs_by_source: dict[str, set[str]]) -> dict:
    documents = {}
    for source_id, slugs in sorted(slugs_by_source.items()):
        mine = [c for c in concepts if c["slug"] in slugs]
        documents[source_id] = {"pages": len(mine), "head_groups": head_groups(mine)}
    linked = set().union(*slugs_by_source.values()) if slugs_by_source else set()
    return {
        "pages": len(concepts),
        # 문서 연결(document_wiki_links)이 없는 개념 페이지. lint 승격 페이지에서 나온다(ADR-0042 맥락).
        "unlinked_pages": sum(c["slug"] not in linked for c in concepts),
        "head_groups": head_groups(concepts),
        "documents": documents,
    }


def compare(runs: dict[str, dict]) -> dict:
    """모델 간 페이지 수 편차(최대-최소)와 방향별 일치율."""
    def spread(counts: list[int]) -> dict:
        return {"min": min(counts), "max": max(counts), "spread": max(counts) - min(counts)}

    sources = sorted(set().union(*(run["summary"]["documents"] for run in runs.values())))
    return {
        "pages": spread([run["summary"]["pages"] for run in runs.values()]),
        "documents": {s: spread([run["summary"]["documents"].get(s, {}).get("pages", 0) for run in runs.values()])
                      for s in sources},
        "alignment": {f"{a}->{b}": alignment(runs[a]["concepts"], runs[b]["concepts"]) for a, b in permutations(runs, 2)},
    }


def measure(workspace_path: Path) -> dict:
    from app.core.ai_database import connect_ai
    from app.modules.wiki_ingestion.infrastructure.postgres_wiki_ingestion_repository import list_active_concept_index

    state = json.loads(workspace_path.read_text())
    ws = state["workspace_id"]
    source_by_document = {d["document_id"]: d["source_id"] for d in state["documents"]}
    concepts = [{k: c[k] for k in ("slug", "title", "aliases")} for c in list_active_concept_index(ws, use_cache=False)]
    with connect_ai() as conn:
        rows = conn.execute(
            """
            SELECT l.document_id, p.slug FROM document_wiki_links l JOIN wiki_pages p ON p.id = l.wiki_page_id
            WHERE l.workspace_id = %s AND l.relation_type = 'extracted_concept'
              AND p.page_type = 'concept' AND p.status = 'active'
            """,
            (ws,),
        ).fetchall()
    slugs_by_source: dict[str, set[str]] = {s: set() for s in source_by_document.values()}
    for row in rows:
        if row["document_id"] in source_by_document:
            slugs_by_source[source_by_document[row["document_id"]]].add(row["slug"])
    return {"workspace": str(workspace_path), "model": state.get("model"), "concepts": concepts,
            "summary": summarize(concepts, slugs_by_source)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default=":".join(HAIKU), help="쉼표로 구분한 provider:model. 모델마다 새 워크스페이스에 편입한다")
    parser.add_argument("--workspaces", help="쉼표로 구분한 workspace.json. 주면 편입하지 않고 잰다")
    args = parser.parse_args()
    verify_gold()
    if args.workspaces:
        paths = [Path(p).resolve() for p in args.workspaces.split(",")]
    else:
        paths = []
        for model in args.models.split(","):
            name = "granularity-" + _key(model.split(":", 1)[1])
            subprocess.run([sys.executable, "rag_prepare.py", "--name", name, "--model", model],
                           cwd=os.path.dirname(os.path.abspath(__file__)), check=True)
            # ponytail: 날짜 폴더를 다시 계산한다. 자정을 넘겨 편입이 끝나면 --workspaces 로 다시 잰다.
            paths.append(results_dir(name) / "workspace.json")

    load_container_env("fruition-ingest-worker-dev")
    runs = {}
    for path in paths:
        run = measure(path)
        runs[run["model"] or path.parent.name] = run
    report = {"runs": runs, "comparison": compare(runs) if len(runs) > 1 else None}
    out = results_dir("granularity")
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    for label, run in runs.items():
        s = run["summary"]
        print(label, "pages", s["pages"], "unlinked", s["unlinked_pages"], "head_groups",
              {h: len(t) for h, t in s["head_groups"].items()})
    if report["comparison"]:
        print("spread", report["comparison"]["pages"], "alignment", report["comparison"]["alignment"])
    print("saved to", out / "report.json")


if __name__ == "__main__":
    main()
