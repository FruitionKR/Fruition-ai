"""RAG 평가용 워크스페이스 준비: 새 계정·워크스페이스에 원문 11개를 올리고, 최신 dev 코드로 Haiku 편입·후속 편입을 한다.
개념 판정은 기본으로 운영 계획대로 Jev를 쓴다(--concept-judge llm이면 LLM).

운영 편입 워커는 워크스페이스 공용 설정에서 Claude가 오면 Gemini로 바꾼다. 평가 요청대로 Haiku로 편입하려고
이 프로세스에서만 그 대체를 끈다(운영 코드는 바꾸지 않는다).
사용: PYTHONPATH=<dev>/pipeline python rag_prepare.py   → results/<날짜>/rag/workspace.json
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from common import EVAL_DIR, HAIKU, results_dir, verify_gold  # noqa: E402
from stack_env import load_container_env  # noqa: E402

ACCESS = os.environ.get("JEV_EVAL_ACCESS_URL", "http://localhost:8081")
DOCUMENT = os.environ.get("JEV_EVAL_DOCUMENT_URL", "http://localhost:8080")
TERMS = "2026-10-01"  # 로컬 access-svc의 현재 약관 버전


def call(method, url, payload=None, token=None, headers=None, expected=(200,)):
    body = None if payload is None else json.dumps(payload).encode()
    h = {"Content-Type": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode()
            if r.status not in expected:
                raise RuntimeError(f"{method} {url}: {r.status} {raw}")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {url}: {e.code} {e.read().decode(errors='replace')}") from e


def signup() -> tuple[str, str]:
    run = time.strftime("%Y%m%d%H%M%S")
    email, password = f"jev-eval-{run}@example.com", "jev-eval-password"
    v = call("POST", f"{ACCESS}/api/auth/email-verifications", {"email": email, "purpose": "signup"}, expected=(202,))
    # 로컬 access-svc는 dev stub이라 인증번호를 로그로만 남긴다.
    logs = subprocess.run(["docker", "logs", "fruition-access-svc-dev"], capture_output=True, text=True).stdout
    code = re.findall(rf"email={re.escape(email)} purpose=signup code=(\S+)", logs)[-1]
    c = call("POST", f"{ACCESS}/api/auth/email-verifications/{v['verification_id']}/confirm", {"code": code})
    call("POST", f"{ACCESS}/api/auth/signup", {
        "email": email, "password": password, "display_name": "Jev Eval",
        "verification_token": c["verification_token"], "age_confirmed": True, "terms_version": TERMS,
    }, expected=(201,))
    return email, password


def jwt_subject(token: str) -> str:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["sub"]


def use_local_embedding_for_jev_concepts() -> None:
    """운영처럼 Jev 개념 판정을 켠다. 임베딩 서버 포트가 밖으로 열려 있지 않아 같은 BGE-M3를 이 프로세스에 올려 쓴다(평가 전용)."""
    from threading import Lock

    from app.core.jev_client import JEV_CONCEPT_MERGE_ENABLED_ENV
    from app.modules.wiki_embedding.infrastructure.bge_m3_embedding_model import BgeM3EmbeddingModel
    from app.modules.wiki_generation.infrastructure import jev_concept_resolver

    os.environ[JEV_CONCEPT_MERGE_ENABLED_ENV] = "true"
    model, lock = BgeM3EmbeddingModel(), Lock()

    class SerializedModel:
        # 로컬 모델은 스레드 동시 호출에서 죽어서(segfault) 호출을 직렬화한다.
        model_name = model.model_name

        @staticmethod
        def embed(texts):
            with lock:
                return model.embed(texts)

    jev_concept_resolver.embedding_server_model = lambda: SerializedModel()
    assert jev_concept_resolver.jev_concept_judge() is not None, "Jev 개념 판정기를 만들지 못했습니다"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="rag", help="결과 폴더 이름(편입 방식 전후 비교용)")
    parser.add_argument("--resume", help="이어서 편입할 workspace.json 경로. 끝난 문서는 건너뛰고 결과도 그 폴더에 쓴다")
    parser.add_argument("--sources", help="쉼표로 구분한 corpus 문서 이름만 편입(확인용)")
    parser.add_argument("--concept-judge", choices=("jev", "llm"), default="jev", help="편입·후속 편입의 개념 판정기")
    args = parser.parse_args()
    verify_gold()
    load_container_env("fruition-ingest-worker-dev")
    # 임베딩 서버 주소는 도커 네트워크 안에서만 닿는다. 이 프로세스는 같은 BGE-M3를 직접 올려 쓴다(평가 전용).
    os.environ.pop("EMBEDDING_SERVICE_URL", None)
    os.environ["AI_ALLOWED_LLM_MODELS"] = "*"
    import app.modules.wiki_ingestion.interfaces.http.schemas as ingest_schemas
    import app.workers.task_worker as task_worker
    from app.core.llm_env import resolve_llm_selection
    from app.workers import ingest_worker

    # 평가 전용: 워크스페이스 공용 설정의 Claude → Gemini 대체를 끈다.
    ingest_schemas.resolve_workspace_llm_selection = resolve_llm_selection
    task_worker.resolve_workspace_llm_selection = resolve_llm_selection
    # 평가 전용: Claude 기본 출력 한도 4096은 편입 추출 JSON을 잘라 먹는다. 운영 편입은 Claude를 쓰지 않는다.
    from app.modules.wiki_generation.infrastructure import chat_completions_llm

    original_init = chat_completions_llm.ChatCompletionsJsonClient.__init__

    def init_with_output_room(self, config):
        if config.max_tokens is None:
            config.max_tokens = 32000
        # 출력 한도를 늘린 만큼 긴 응답이 기본 180초를 넘긴다.
        config.timeout_seconds = max(config.timeout_seconds, 600)
        original_init(self, config)

    chat_completions_llm.ChatCompletionsJsonClient.__init__ = init_with_output_room
    # 평가 전용: 편입 평가 구간 12만 자는 Gemini(1M) 기준이라 Haiku(200k) 입력 한도를 넘는다.
    from app.modules.wiki_generation.application import evaluate_generation

    evaluate_generation.EVALUATION_WINDOW_CHARS = 40_000
    if args.concept_judge == "jev":
        use_local_embedding_for_jev_concepts()

    out = Path(args.resume).resolve().parent if args.resume else results_dir(args.name)
    if args.resume:
        state = json.loads(Path(args.resume).read_text())
        email, password, ws, user_id = state["email"], "jev-eval-password", state["workspace_id"], state["user_id"]
    else:
        email, password = signup()
    token = lambda: call("POST", f"{ACCESS}/api/auth/login", {"email": email, "password": password})["access_token"]  # noqa: E731
    if not args.resume:
        user_id = jwt_subject(token())
        ws = call("POST", f"{ACCESS}/api/workspaces", {"name": "Jev 켬/끔 RAG 평가"}, token(), expected=(201,))["id"]
        state = {"workspace_id": ws, "user_id": user_id, "email": email, "documents": []}
        # 첫 문서에서 실패해도 만든 계정·워크스페이스로 이어갈 수 있게 바로 남긴다.
        (out / "workspace.json").write_text(json.dumps(state, ensure_ascii=False, indent=1))
    done = {doc["source_id"] for doc in state["documents"]}
    for i, path in enumerate(sorted((EVAL_DIR / "corpus").glob("*.md"))):
        if path.stem in done or (args.sources and path.stem not in args.sources.split(",")):
            continue
        # PDF 수식 변환 잔재 NUL은 PostgreSQL text에 들어가지 않는다. 정답 인용문에는 없다.
        md = path.read_text(encoding="utf-8").replace("\x00", "")
        title = next((line[2:].strip() for line in md.splitlines() if line.startswith("# ")), path.stem)
        doc = call("POST", f"{DOCUMENT}/api/workspaces/{ws}/documents/markdown", {"display_name": title, "markdown": md},
                   token(), {"Idempotency-Key": f"jev-eval-{ws}-{i}"}, expected=(201,))
        command = {
            "run_id": str(uuid.uuid4()), "kind": "ingest", "document_id": doc["id"], "workspace_id": ws,
            "user_id": user_id, "display_name": title, "provider": HAIKU[0], "model": HAIKU[1],
        }
        started = time.monotonic()
        ingest_worker._handle(command)
        post = ingest_worker._post_ingest_command(command)
        if post:
            task_worker._handle_post_ingest(post)
        state["documents"].append({"source_id": path.stem, "document_id": doc["id"], "run_id": command["run_id"],
                                   "seconds": round(time.monotonic() - started, 1)})
        print("ingested", path.stem, state["documents"][-1]["seconds"], "s", flush=True)
        (out / "workspace.json").write_text(json.dumps(state, ensure_ascii=False, indent=1))
    print("workspace", ws, "saved to", out / "workspace.json")


if __name__ == "__main__":
    main()
