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

COPY agent_worker.py api.py run_lab.py wiki_db_cutover.py ./
COPY app ./app
COPY db ./db
COPY prompts ./prompts

RUN mkdir -p runs

EXPOSE 8000

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
