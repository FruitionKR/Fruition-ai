from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.modules.document_restoration.domain.bounding_box import BBox


def load_docling(base_dir: Path) -> dict[str, Any]:
    docling_file = base_dir / "layout" / "auto" / "docling_ocr_baseline" / "docling.json"
    return json.loads(docling_file.read_text(encoding="utf-8"))


def resolve_pdf_file(base_dir: Path, docling: dict[str, Any], document_slug: str) -> Path:
    filename = (docling.get("origin") or {}).get("filename") or f"{document_slug}.pdf"
    for candidate in (base_dir.parent / filename, base_dir / filename):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"PDF file not found for {document_slug}: {filename}")


def docling_bbox_to_top_left(bbox: dict[str, Any], page_height: float) -> BBox | None:
    try:
        left = float(bbox["l"])
        right = float(bbox["r"])
        top = float(bbox["t"])
        bottom = float(bbox["b"])
    except (KeyError, TypeError, ValueError):
        return None
    if bbox.get("coord_origin") == "BOTTOMLEFT" and page_height > 0:
        return (left, page_height - top, right, page_height - bottom)
    return (left, top, right, bottom)


def item_page_and_bbox(item: dict[str, Any], pages: dict[str, Any]) -> tuple[int, BBox] | None:
    prov = item.get("prov") or []
    if not prov:
        return None
    page = int(prov[0].get("page_no", 0))
    page_info = pages.get(str(page), {})
    page_height = float((page_info.get("size") or {}).get("height", 0.0))
    bbox = docling_bbox_to_top_left(prov[0].get("bbox") or {}, page_height)
    if not bbox:
        return None
    return page, bbox
