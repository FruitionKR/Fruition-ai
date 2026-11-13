"""유료 공급사 호출 없이 PDF 구조만 읽는다. 파싱은 시간·메모리·쪽 수를 제한한 하위 프로세스에서 한다."""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.modules.document_restoration.infrastructure import pdf_cost_features
from app.modules.wiki_ingestion.infrastructure.object_storage import bucket_name, client

router = APIRouter(prefix="/internal/documents", tags=["document-cost"])

# document 업로드 상한(100MB)보다 크게 둔다. 넘으면 내려받지 않는다.
MAX_BYTES = int(os.environ.get("PDF_COST_MAX_BYTES", str(128 * 1024 * 1024)))
# 앞쪽 이만큼만 읽고 나머지는 비율로 외삽한다.
MAX_PAGES = int(os.environ.get("PDF_COST_MAX_PAGES", "300"))
TIMEOUT_SECONDS = int(os.environ.get("PDF_COST_TIMEOUT_SECONDS", "20"))
MEMORY_BYTES = int(os.environ.get("PDF_COST_MEMORY_BYTES", str(1024 * 1024 * 1024)))
# 동시에 파싱하는 PDF 수. 자리가 나지 않으면 503으로 돌려 document가 다시 시도하게 한다.
_slots = threading.BoundedSemaphore(int(os.environ.get("PDF_COST_CONCURRENCY", "2")))
SLOT_WAIT_SECONDS = 10


class PdfSource(BaseModel):
    source_key: str = Field(pattern=r"^sources/documents/[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}/original$")


class UnreadablePdf(Exception):
    pass


def parser_command(path: str) -> list[str]:
    return [sys.executable, "-I", pdf_cost_features.__file__, path, str(MAX_PAGES),
            str(TIMEOUT_SECONDS), str(MEMORY_BYTES)]


def inspect_file(path: str) -> dict[str, int]:
    """파서가 실패·크래시·시간 초과로 끝나면 읽을 수 없는 PDF로 본다. API 프로세스는 영향을 받지 않는다."""
    try:
        result = subprocess.run(parser_command(path), capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise UnreadablePdf from None
    if result.returncode != 0:
        raise UnreadablePdf
    features = json.loads(result.stdout)
    keys = {"pages", "text_bytes", "images", "scanned_pages"}
    if set(features) != keys or not all(type(features[key]) is int and features[key] >= 0 for key in keys):
        raise UnreadablePdf
    return features


@router.post("/pdf-cost-features")
def inspect_pdf(source: PdfSource) -> dict[str, int]:
    if not _slots.acquire(timeout=SLOT_WAIT_SECONDS):
        raise HTTPException(status_code=503, detail="PDF inspection is busy.")
    try:
        storage = client()
        if storage.stat_object(bucket_name(), source.source_key).size > MAX_BYTES:
            raise HTTPException(status_code=413, detail="PDF is too large to inspect.")
        with TemporaryDirectory(prefix="pdf-cost-") as directory:
            path = str(Path(directory) / "source.pdf")
            storage.fget_object(bucket_name(), source.source_key, path)
            return inspect_file(path)
    except UnreadablePdf:
        raise HTTPException(status_code=422, detail="PDF structure could not be read.") from None
    finally:
        _slots.release()
