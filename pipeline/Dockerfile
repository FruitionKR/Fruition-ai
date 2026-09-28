# Debian 13(trixie). ort-sys가 받아오는 프리빌트 ONNX Runtime이 GLIBCXX_3.4.31을 요구하는데
# Debian 12(bookworm)의 libstdc++는 3.4.30까지만 제공해 링크가 실패한다.
# 런타임 이미지도 같은 이유로 trixie여야 한다. 둘은 함께 움직인다.
FROM rust:1.88.0-slim-trixie AS heron-builder

WORKDIR /build/heron-special-regions

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        clang \
        cmake \
        g++ \
        git \
        libssl-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY tools/heron-special-regions/Cargo.toml tools/heron-special-regions/Cargo.lock ./
COPY tools/heron-special-regions/src ./src

RUN cargo build --release --locked

FROM node:20-slim AS anydoc

RUN npm install --global @firecrawl/anydoc@0.1.7

# heron-builder가 만든 raw-special-regions가 GLIBCXX_3.4.31을 요구하므로 trixie를 쓴다.
FROM python:3.12-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DEFAULT_TIMEOUT=300
ENV DOCLING_RS_ASSET_DIR=/opt/docling-rs
ENV DOCLING_LAYOUT_ONNX=/opt/docling-rs/models/layout_heron_int8.onnx
ENV PDFIUM_DYNAMIC_LIB_PATH=/usr/local/lib/python3.12/site-packages/pypdfium2_raw

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        torch torchvision \
    && pip install --no-cache-dir -r requirements.txt

COPY --from=heron-builder /build/heron-special-regions/target/release/raw-special-regions /usr/local/bin/raw-special-regions
COPY --from=anydoc /usr/local/bin/node /usr/local/bin/node
COPY --from=anydoc /usr/local/bin/npm /usr/local/bin/npm
COPY --from=anydoc /usr/local/bin/npx /usr/local/bin/npx
COPY --from=anydoc /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@firecrawl/anydoc/cli.js /usr/local/bin/anydoc

RUN mkdir -p /opt/docling-rs/models \
    && curl -fsSL \
        https://github.com/docling-project/docling.rs/releases/download/models-v1/layout_heron_int8.onnx \
        -o /opt/docling-rs/models/layout_heron_int8.onnx

# 질의·에이전트·유지보수 작업자가 쓰는 BGE-M3를 이미지 레이어로 넣는다. 레이어는 노드마다 한 번만
# 저장되므로 Pod별 다운로드·캐시(약 2.1GB)가 없고, Pod가 뜰 때 Hugging Face에 의존하지 않는다.
# main에는 pytorch_model.bin만 있다. revision을 고정하고 refs/main을 같은 commit으로 적어
# 코드의 모델 이름(BAAI/bge-m3)을 그대로 오프라인에서 해석하게 한다. 저장 임베딩의 모델 키가 이 이름이다.
# huggingface_hub가 root 전용으로 만드는 tree 캐시를 앱 사용자(10001)도 읽게 해 로드 경고를 없앤다.
ENV HF_HOME=/opt/huggingface
ARG BGE_M3_REVISION=5617a9f61b028005a4858fdac845db406aefb181
RUN python -c "from huggingface_hub import snapshot_download; snapshot_download('BAAI/bge-m3', revision='${BGE_M3_REVISION}', allow_patterns=['*.json', 'sentencepiece.bpe.model', 'pytorch_model.bin', '1_Pooling/*'], ignore_patterns=['onnx/*'])" \
    && mkdir -p "${HF_HOME}/hub/models--BAAI--bge-m3/refs" \
    && printf '%s' "${BGE_M3_REVISION}" > "${HF_HOME}/hub/models--BAAI--bge-m3/refs/main" \
    && find "${HF_HOME}" ! -perm -o+r -exec chmod a+r {} +
# 실행 중에는 Hub에 묻지 않는다. 온라인이면 transformers가 safetensors 변환본(약 2.3GB)을 추가로 받는다.
ENV HF_HUB_OFFLINE=1

COPY agent_worker.py api.py run_lab.py wiki_db_cutover.py ./
COPY app ./app
COPY db ./db
COPY prompts ./prompts

# Fixed numeric identity lets Kubernetes verify runAsNonRoot without image lookup.
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/appuser --shell /usr/sbin/nologin appuser
ENV HOME=/home/appuser
ENV XDG_CACHE_HOME=/home/appuser/.cache
RUN mkdir -p /app/runs /home/appuser/.cache \
    && chown 10001:10001 /app/runs /home/appuser/.cache

# Application source and installed binaries remain root-owned/read-only to appuser.
USER 10001:10001

EXPOSE 8000

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
