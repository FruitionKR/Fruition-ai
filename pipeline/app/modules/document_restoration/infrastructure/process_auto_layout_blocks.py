from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from app.modules.document_restoration.domain.bounding_box import (
    bbox_center_inside,
    bbox_match_score,
    bbox_overlap_ratio,
)
from app.modules.document_restoration.domain.markdown_text import (
    is_valid_markdown_table,
    strip_markdown_fence,
)
from app.modules.document_restoration.domain.text_quality import (
    looks_glyph_encoded as generic_looks_glyph_encoded,
)
from app.modules.document_restoration.infrastructure.docling_io import docling_bbox_to_top_left


BASE_DIR = Path(__file__).resolve().parents[1]
DOCUMENT_SLUG = "document"

MANIFEST_FILE = BASE_DIR / "layout" / "auto" / f"{DOCUMENT_SLUG}.auto_block_manifest.json"
RECOVERED_BLOCK_DIR = BASE_DIR / "layout" / "auto" / "recovered_blocks"
EVALUATION_DIR = BASE_DIR / "layout" / "auto" / "evaluations"
TEXT_RECOVERED_DIR = BASE_DIR / "layout" / "auto" / "text_recovered"
TEXT_EVALUATION_DIR = BASE_DIR / "layout" / "auto" / "text_evaluations"
OUTPUT_FILE = BASE_DIR / "final" / f"{DOCUMENT_SLUG}.block_processed_draft.md"
REPORT_FILE = BASE_DIR / "layout" / "auto" / f"{DOCUMENT_SLUG}.block_processing_report.md"
DOCUMENT_TITLE = DOCUMENT_SLUG.replace("_", " ")
SOURCE_NAME = f"{DOCUMENT_SLUG}.pdf"
DOCLING_FORMULAS: list[dict[str, Any]] | None = None
USE_RECOVERED_RESULTS = True


def load_blocks() -> list[dict[str, Any]]:
    blocks = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    return sorted(blocks, key=lambda block: (block["page"], block["order"]))


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def looks_glyph_encoded_candidate(text: str) -> bool:
    return generic_looks_glyph_encoded(text)


def accepted_text_recovery(block: dict[str, Any]) -> str | None:
    if not USE_RECOVERED_RESULTS or block["type"] not in {"paragraph", "heading"}:
        return None
    evaluation_file = TEXT_EVALUATION_DIR / f"{block['id']}.json"
    recovered_file = TEXT_RECOVERED_DIR / f"{block['id']}.md"
    if not evaluation_file.exists() or not recovered_file.exists():
        return None
    evaluation = json.loads(evaluation_file.read_text(encoding="utf-8"))
    if not evaluation.get("accepted"):
        return None
    text = normalize_text(recovered_file.read_text(encoding="utf-8"))
    if looks_glyph_encoded_candidate(text):
        return None
    return text or None


def best_crop_ocr_candidate(block: dict[str, Any], source_text: str) -> str | None:
    if block["type"] not in {"paragraph", "heading"} or not looks_glyph_encoded_candidate(source_text):
        return None
    candidates = []
    for candidate in block.get("text_candidates") or []:
        if not str(candidate.get("source", "")).startswith("crop_ocr"):
            continue
        text = normalize_text(str(candidate.get("text", "")))
        if not text or looks_glyph_encoded_candidate(text):
            continue
        if block["type"] == "paragraph" and len(text) < max(30, len(source_text) * 0.25):
            continue
        quality = candidate.get("quality")
        score = quality if isinstance(quality, (int, float)) else 0.0
        candidates.append((score, len(text), text))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def block_source_text(block: dict[str, Any]) -> str:
    source_text = normalize_text(block.get("source_text", ""))
    recovered = accepted_text_recovery(block)
    if recovered is not None:
        return recovered
    crop_candidate = best_crop_ocr_candidate(block, source_text)
    return crop_candidate or source_text


def asset_link(block: dict[str, Any]) -> str | None:
    asset = block.get("asset")
    if not asset:
        return None
    return f"../{asset}"


def block_comment(block: dict[str, Any]) -> str:
    return f"<!-- {block['id']} type={block['type']} bbox={block['bbox']} confidence={block['confidence']} -->"


def split_subheading_body(text: str) -> tuple[str, str | None]:
    if not re.match(r"^[A-D]\.\s+", text):
        return text, None

    words = text.split()
    body_starters = {"The", "A", "An", "It", "Once", "After", "To", "In", "For", "When", "This", "Firstly"}
    for index, word in enumerate(words[3:], start=3):
        if word in body_starters:
            return " ".join(words[:index]), " ".join(words[index:])
    return text, None


def recovered_markdown(block: dict[str, Any]) -> str | None:
    if not USE_RECOVERED_RESULTS:
        return None
    evaluation_file = EVALUATION_DIR / f"{block['id']}.json"
    if not evaluation_file.exists():
        return None
    evaluation = json.loads(evaluation_file.read_text(encoding="utf-8"))
    if evaluation.get("layout_decision") in {"discard_debris"}:
        return None
    if not evaluation.get("accepted"):
        return None

    recovered_file = RECOVERED_BLOCK_DIR / f"{block['id']}.md"
    if not recovered_file.exists():
        return None
    text = recovered_file.read_text(encoding="utf-8").strip()
    text = strip_markdown_fence(text)
    if block["type"] == "table_candidate" and not is_valid_markdown_table(text):
        return None
    if block["type"] == "equation_candidate":
        text = normalize_display_math(text)
    if block["type"] == "figure_candidate" and looks_glyph_encoded_candidate(text):
        return None
    return text or None


def normalize_display_math(text: str) -> str:
    stripped = text.strip()
    if "$$" in stripped:
        parts = stripped.split("$$")
        content = next((part.strip() for part in parts[1:] if part.strip()), "")
    else:
        content = stripped
    content = content.replace("$", "").strip()
    if not content:
        return ""
    return f"$$\n{content}\n$$"


def skip_layout_adjudicated_block(block: dict[str, Any]) -> bool:
    if not USE_RECOVERED_RESULTS:
        return False
    if block["type"] not in {"equation_candidate", "table_candidate"}:
        return False
    evaluation_file = EVALUATION_DIR / f"{block['id']}.json"
    if not evaluation_file.exists():
        return False
    evaluation = json.loads(evaluation_file.read_text(encoding="utf-8"))
    return evaluation.get("layout_decision") in {"discard_debris"}


def layout_decision(block: dict[str, Any]) -> str:
    if not USE_RECOVERED_RESULTS:
        return ""
    if block["type"] not in {"equation_candidate", "table_candidate"}:
        return ""
    evaluation_file = EVALUATION_DIR / f"{block['id']}.json"
    if not evaluation_file.exists():
        return ""
    evaluation = json.loads(evaluation_file.read_text(encoding="utf-8"))
    return str(evaluation.get("layout_decision") or "")


def layout_reason(block: dict[str, Any]) -> str:
    if not USE_RECOVERED_RESULTS:
        return ""
    if block["type"] not in {"equation_candidate", "table_candidate"}:
        return ""
    evaluation_file = EVALUATION_DIR / f"{block['id']}.json"
    if not evaluation_file.exists():
        return ""
    evaluation = json.loads(evaluation_file.read_text(encoding="utf-8"))
    return str(evaluation.get("layout_reason") or "")


def docling_formulas() -> list[dict[str, Any]]:
    global DOCLING_FORMULAS
    if DOCLING_FORMULAS is not None:
        return DOCLING_FORMULAS
    summary_file = BASE_DIR / "layout" / "auto" / "docling_equation_candidates" / "summary.json"
    if not summary_file.exists():
        DOCLING_FORMULAS = []
        return DOCLING_FORMULAS
    data = json.loads(summary_file.read_text(encoding="utf-8"))
    DOCLING_FORMULAS = data.get("formulas", [])
    return DOCLING_FORMULAS


def skip_docling_formula_fragment(block: dict[str, Any]) -> bool:
    if block["type"] != "paragraph":
        return False
    bbox = tuple(float(value) for value in block["bbox"])
    for formula in docling_formulas():
        if formula.get("page") != block["page"]:
            continue
        matched_ids = formula.get("matched_manifest_ids", [])
        if not any(recovered_markdown({"id": block_id, "type": "equation_candidate"}) for block_id in matched_ids):
            continue
        formula_bbox = tuple(float(value) for value in formula.get("padded_bbox") or formula.get("bbox"))
        if bbox_center_inside(bbox, formula_bbox) or bbox_overlap_ratio(bbox, formula_bbox) >= 0.5:
            return True
    return False


def skip_duplicate_docling_formula_equation(block: dict[str, Any]) -> bool:
    if block["type"] != "equation_candidate" or not recovered_markdown(block):
        return False
    for formula in docling_formulas():
        matched_ids = formula.get("matched_manifest_ids", [])
        if block["id"] not in matched_ids:
            continue
        accepted_ids = [
            block_id
            for block_id in matched_ids
            if recovered_markdown({"id": block_id, "type": "equation_candidate"})
        ]
        return bool(accepted_ids and block["id"] != accepted_ids[0])
    return False


def process_heading(block: dict[str, Any]) -> list[str]:
    text = block_source_text(block)
    if re.match(r"^#{1,6}\s+\S", text):
        return [block_comment(block), text]
    heading, body = split_subheading_body(text)
    markdown = f"## {heading}" if re.match(r"^[IVX]+\.\s+", heading) else f"### {heading}"
    lines = [block_comment(block), markdown]
    if body:
        lines.extend(["", body])
    return lines


def process_paragraph(block: dict[str, Any]) -> list[str]:
    text = block_source_text(block)
    return [block_comment(block), text] if text else []


def process_candidate(block: dict[str, Any], label: str) -> list[str]:
    recovered = recovered_markdown(block)
    if recovered:
        return [block_comment(block), recovered]

    decision = layout_decision(block)
    text = block.get("source_text", "").strip()
    link = asset_link(block)
    if decision in {"table_boundary_too_large", "table_boundary_too_small"}:
        reason = layout_reason(block)
        message = f"> {label} 경계 재검출 필요: `{decision}`"
        if reason:
            message += f" - {reason}"
    else:
        message = f"> {label} 자동 복원 실패"
    lines = [block_comment(block), message, ""]
    if link:
        lines.append(f"[source crop]({link})")
        lines.append("")
    if text:
        lines.extend(["```text", text, "```"])
    return lines


def process_figure(block: dict[str, Any]) -> list[str]:
    link = asset_link(block)
    caption = block_source_text(block)
    bad_caption = looks_glyph_encoded_candidate(caption)
    alt_text = f"figure candidate {block['id']}" if bad_caption else caption or f"figure candidate {block['id']}"
    lines = [block_comment(block)]
    if link:
        lines.append(f"![{alt_text}]({link})")
    if caption and not bad_caption:
        lines.extend(["", caption])
        return lines
    recovered = recovered_markdown(block)
    if recovered:
        lines.extend(["", recovered])
        return lines
    caption_expected = block.get("caption_expected", bool(caption))
    if bad_caption or (caption_expected and not caption):
        lines.extend(["", "> 그림 caption 자동 복원 실패"])
        return lines
    return lines


def process_block(block: dict[str, Any]) -> list[str]:
    block_type = block["type"]
    if block_type == "footer":
        return []
    if skip_layout_adjudicated_block(block):
        return []
    if layout_decision(block) == "convert_to_paragraph":
        return process_paragraph(block)
    if skip_duplicate_docling_formula_equation(block):
        return []
    if skip_docling_formula_fragment(block):
        return []
    if block_type == "heading":
        return process_heading(block)
    if block_type == "paragraph":
        return process_paragraph(block)
    if block_type == "equation_candidate":
        return process_candidate(block, "수식 후보")
    if block_type == "table_candidate":
        return process_candidate(block, "표 후보")
    if block_type == "figure_candidate":
        return process_figure(block)
    return [block_comment(block), block.get("markdown", "").strip()]


def write_processed_draft(blocks: list[dict[str, Any]]) -> None:
    lines = [
        f"# {DOCUMENT_TITLE}",
        "",
        f"Source: `{SOURCE_NAME}`",
        "",
        "Recovery note: block-processed draft. 본문은 원문/OCR 후보를 사용하고, 표·수식·그림은 block별 자동 복원 결과가 형식 검증을 통과하면 삽입한다.",
        "",
    ]
    current_page = 0
    for block in blocks:
        if block["page"] != current_page:
            current_page = block["page"]
            lines.extend([f"## Page {current_page}", ""])

        processed = process_block(block)
        if not processed:
            continue
        lines.extend(processed)
        lines.append("")

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_report(blocks: list[dict[str, Any]]) -> None:
    counts: dict[str, int] = {}
    recovered_count = 0
    for block in blocks:
        counts[block["type"]] = counts.get(block["type"], 0) + 1
        if (
            block["type"] in {"equation_candidate", "table_candidate"}
            and recovered_markdown(block)
            and not skip_duplicate_docling_formula_equation(block)
        ):
            recovered_count += 1

    lines = [
        "# Block processing report",
        "",
        "## Input",
        "",
        f"- Manifest: `{MANIFEST_FILE.relative_to(BASE_DIR)}`",
        f"- Recovered blocks: `{RECOVERED_BLOCK_DIR.relative_to(BASE_DIR)}`",
        f"- Format validation: `{EVALUATION_DIR.relative_to(BASE_DIR)}`",
        "",
        "## Output",
        "",
        f"- Draft: `{OUTPUT_FILE.relative_to(BASE_DIR)}`",
        "",
        "## Counts",
        "",
    ]
    for block_type, count in sorted(counts.items()):
        lines.append(f"- {block_type}: {count}")

    lines.extend(
        [
            f"- recovered table/equation blocks: {recovered_count}",
            "",
            "## Processing rule",
            "",
            "- `paragraph`, `heading`: glyph-decoded text를 그대로 정리한다.",
            "- `equation_candidate`, `table_candidate`: 자동 복원 결과가 최소 형식 검증을 통과할 때만 삽입한다.",
            "- 복원 파일이 없거나 형식 검증에 실패하면 source crop과 후보 텍스트를 남긴다.",
            "- `figure_candidate`: crop 이미지와 caption을 함께 남긴다.",
            "- `footer`: 최종 draft에서 제외한다.",
        ]
    )
    REPORT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> None:
    global BASE_DIR, DOCUMENT_SLUG, MANIFEST_FILE, RECOVERED_BLOCK_DIR, EVALUATION_DIR, TEXT_RECOVERED_DIR, TEXT_EVALUATION_DIR, OUTPUT_FILE, REPORT_FILE, DOCUMENT_TITLE, SOURCE_NAME, USE_RECOVERED_RESULTS

    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=BASE_DIR)
    parser.add_argument("--document-slug", default=DOCUMENT_SLUG)
    parser.add_argument("--manifest-file", type=Path)
    parser.add_argument("--output-file", type=Path)
    parser.add_argument("--report-file", type=Path)
    parser.add_argument("--title")
    parser.add_argument("--source-name")
    parser.add_argument("--ignore-recovered-results", action="store_true")
    args = parser.parse_args()

    BASE_DIR = args.output_dir.resolve()
    DOCUMENT_SLUG = args.document_slug
    MANIFEST_FILE = args.manifest_file.resolve() if args.manifest_file else BASE_DIR / "layout" / "auto" / f"{DOCUMENT_SLUG}.auto_block_manifest.json"
    RECOVERED_BLOCK_DIR = BASE_DIR / "layout" / "auto" / "recovered_blocks"
    EVALUATION_DIR = BASE_DIR / "layout" / "auto" / "evaluations"
    TEXT_RECOVERED_DIR = BASE_DIR / "layout" / "auto" / "text_recovered"
    TEXT_EVALUATION_DIR = BASE_DIR / "layout" / "auto" / "text_evaluations"
    OUTPUT_FILE = args.output_file.resolve() if args.output_file else BASE_DIR / "final" / f"{DOCUMENT_SLUG}.block_processed_draft.md"
    REPORT_FILE = args.report_file.resolve() if args.report_file else BASE_DIR / "layout" / "auto" / f"{DOCUMENT_SLUG}.block_processing_report.md"
    DOCUMENT_TITLE = args.title or DOCUMENT_SLUG.replace("_", " ")
    SOURCE_NAME = args.source_name or f"{DOCUMENT_SLUG}.pdf"
    USE_RECOVERED_RESULTS = not args.ignore_recovered_results

    blocks = load_blocks()
    write_processed_draft(blocks)
    write_report(blocks)
    print(OUTPUT_FILE)
    print(REPORT_FILE)


if __name__ == "__main__":
    main()
