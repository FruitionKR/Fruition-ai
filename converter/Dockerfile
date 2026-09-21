# ONNX Runtime이 요구하는 GLIBCXX_3.4.31을 제공하는 Debian 13을 사용한다.
FROM rust:1.88.0-slim-trixie AS heron-builder

WORKDIR /build/heron-special-regions

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        clang \
        cmake \
        git \
        libssl-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY pipeline/tools/heron-special-regions/Cargo.toml pipeline/tools/heron-special-regions/Cargo.lock ./
COPY pipeline/tools/heron-special-regions/src ./src

RUN cargo build --release --locked

FROM node:20-slim AS anydoc

RUN npm install --global @firecrawl/anydoc@0.1.7

# Heron 실행 파일도 같은 libstdc++ ABI가 필요하다.
FROM python:3.12-slim-trixie

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PYTHONPATH=/app
ENV RESTORATION_COMMAND="python -m app.modules.document_restoration.interfaces.cli"
ENV DOCLING_RS_ASSET_DIR=/opt/docling-rs
ENV DOCLING_LAYOUT_ONNX=/opt/docling-rs/models/layout_heron_int8.onnx
ENV PDFIUM_DYNAMIC_LIB_PATH=/usr/local/lib/python3.12/site-packages/pypdfium2_raw

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ghostscript \
        ocrmypdf \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-kor \
        unpaper \
        libgl1 \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Fixed numeric identity lets Kubernetes verify runAsNonRoot without image lookup.
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/appuser --shell /usr/sbin/nologin appuser

COPY converter/requirements.txt converter/check_cpu_only.py ./
# requirements.txt pins +cpu torch wheels from the official CPU index; the check
# fails the build if any nvidia/cuda/triton package or a non-CPU torch slipped in.
RUN pip install --no-cache-dir -r requirements.txt \
    && python check_cpu_only.py \
    && pip check

COPY --from=heron-builder /build/heron-special-regions/target/release/raw-special-regions /usr/local/bin/raw-special-regions
COPY --from=anydoc /usr/local/bin/node /usr/local/bin/node
COPY --from=anydoc /usr/local/bin/npm /usr/local/bin/npm
COPY --from=anydoc /usr/local/bin/npx /usr/local/bin/npx
COPY --from=anydoc /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@firecrawl/anydoc/cli.js /usr/local/bin/anydoc

RUN mkdir -p /opt/docling-rs/models \
    && curl -fsSL \
        https://github.com/docling-project/docling.rs/releases/download/models-v1/layout_heron_int8.onnx \
        -o /opt/docling-rs/models/layout_heron_int8.onnx \
    && rm -rf /var/lib/apt/lists/*

COPY converter/app.py ./converter_app.py
COPY pipeline/app ./app
COPY pipeline/prompts ./prompts

# Converter route and restoration CLI must import on the CPU runtime.
RUN python -c "import converter_app, app.modules.document_restoration.interfaces.cli"

# AWS mounts writable /tmp while the root filesystem remains read-only.
ENV HOME=/tmp
ENV XDG_CACHE_HOME=/tmp/.cache
ENV PYTHONDONTWRITEBYTECODE=1
USER 10001:10001

EXPOSE 8000

CMD ["uvicorn", "converter_app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
