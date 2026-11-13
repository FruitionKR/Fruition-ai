"""Read PDF structure without making a paid provider call."""
from tempfile import TemporaryDirectory
from pathlib import Path

import pymupdf
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.modules.wiki_ingestion.infrastructure.object_storage import bucket_name, client

router = APIRouter(prefix="/internal/documents", tags=["document-cost"])


class PdfSource(BaseModel):
    source_key: str = Field(pattern=r"^sources/documents/[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}/original$")


def pdf_features(path: str) -> dict[str, int]:
    with pymupdf.open(path) as pdf:
        if pdf.needs_pass or not len(pdf):
            raise ValueError("PDF is encrypted or empty")
        text_bytes = images = scanned_pages = 0
        for page in pdf:
            text = page.get_text()
            text_bytes += len(text.encode("utf-8"))
            images += len(page.get_images())
            scanned_pages += int(len(text.strip()) < 40)
        return dict(pages=len(pdf), text_bytes=text_bytes, images=images, scanned_pages=scanned_pages)


@router.post("/pdf-cost-features")
def inspect_pdf(source: PdfSource) -> dict[str, int]:
    try:
        with TemporaryDirectory(prefix="pdf-cost-") as directory:
            path = str(Path(directory) / "source.pdf")
            client().fget_object(bucket_name(), source.source_key, path)
            return pdf_features(path)
    except (ValueError, pymupdf.FileDataError):
        raise HTTPException(status_code=422, detail="PDF structure could not be read.") from None
