"""로컬 스택 컨테이너의 환경을 이 프로세스로 옮긴다. 값은 출력하지 않는다.

컨테이너 안 호스트 이름을 localhost 포트로 바꿔, 최신 dev 코드가 스택의 DB·저장소·문서 서비스를 그대로 쓰게 한다.
"""
import json
import os
import subprocess

HOSTS = {
    "postgresql:5432": "localhost:5432",
    "minio:9000": "localhost:9000",
    "document-svc:8080": "localhost:8080",
    "access-svc:8081": "localhost:8081",
    "kafka:19092": "localhost:9092",
    "embedding-server:8000": "localhost:18000",
}
SKIP = {"PATH", "HOME", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256", "XDG_CACHE_HOME", "HF_HOME",
        "TIKTOKEN_CACHE_DIR", "DOCLING_RS_ASSET_DIR", "DOCLING_LAYOUT_ONNX", "PDFIUM_DYNAMIC_LIB_PATH",
        "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "PIP_DEFAULT_TIMEOUT", "HF_HUB_OFFLINE",
        # 평가 프로세스는 분석 이벤트를 보내지 않는다.
        "ANALYTICS_ENABLED", "POSTHOG_PROJECT_TOKEN", "POSTHOG_HOST", "LANGSMITH_TRACING", "LANGSMITH_API_KEY"}


def load_container_env(container: str) -> None:
    raw = subprocess.run(["docker", "inspect", container, "--format", "{{json .Config.Env}}"],
                         capture_output=True, text=True, check=True).stdout
    for item in json.loads(raw):
        key, _, value = item.partition("=")
        if key in SKIP:
            continue
        for old, new in HOSTS.items():
            value = value.replace(old, new)
        if key == "REDIS_HOST" and value == "redis":
            value = "localhost"
        os.environ[key] = value
    os.environ["ANALYTICS_ENABLED"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"
