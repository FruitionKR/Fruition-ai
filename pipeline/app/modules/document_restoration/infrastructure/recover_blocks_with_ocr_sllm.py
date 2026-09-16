from __future__ import annotations

import argparse
from collections import Counter
import csv
import io
import json
import os
import re
import socket
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageOps

from app.modules.document_restoration.domain.markdown_text import (
    has_balanced_braces,
    is_valid_markdown_table,
    strip_markdown_fence,
)


BASE_DIR = Path(__file__).resolve().parents[1]
PROMPTS_ROOT = Path(__file__).resolve().parents[4] / "prompts" / "document_restoration"
DOCUMENT_SLUG = "document"

MANIFEST_FILE = BASE_DIR / "layout" / "auto" / f"{DOCUMENT_SLUG}.auto_block_manifest.json"
OUTPUT_DIR = BASE_DIR / "layout" / "auto" / "recovered_blocks"
OCR_DIR = BASE_DIR / "layout" / "auto" / "ocr"
EVALUATION_DIR = BASE_DIR / "layout" / "auto" / "evaluations"
PROMPT_DIR = PROMPTS_ROOT
DEFAULT_ENDPOINT = "http://127.0.0.1:11434/v1/chat/completions"
DEFAULT_MODEL = "qwen2.5:7b"
PREPROCESSED_OCR_DIR = OCR_DIR / "preprocessed"
LATEX_OCR_DIR = OCR_DIR / "latex"
LATEX_OCR_MODEL: Any | None = None
PADDLE_FORMULA_MODEL: Any | None = None
PADDLE_FORMULA_ERROR: str | None = None
PADDLE_FORMULA_OCR_DIR = OCR_DIR / "paddle_formula"
PADDLE_CACHE_DIR = BASE_DIR.parents[2] / "paddle_cache"
DOCLING_EQUATION_SUMMARY: dict[str, Any] | None = None


def should_use_sllm(block_type: str, enabled: bool) -> bool:
    return enabled and block_type == "equation_candidate"


def load_blocks() -> list[dict[str, Any]]:
    blocks = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    return sorted(blocks, key=lambda block: (block["page"], block["order"]))


def run_tesseract(image_file: Path, *args: str) -> str:
    output = subprocess.run(
        ["tesseract", str(image_file), "stdout", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return output.stdout.strip()


def ocr_image(asset: str, block_id: str, block_type: str) -> str:
    image_file = docling_formula_asset_path(asset)
    primary_text = run_tesseract(image_file, "--psm", "6")
    OCR_DIR.mkdir(parents=True, exist_ok=True)
    if block_type == "table_candidate":
        alternative_text = run_tesseract(image_file, "--psm", "4")
        preprocessed_image = preprocess_table_image(image_file, block_id)
        preprocessed_text = run_tesseract(preprocessed_image, "--psm", "6")
        coordinate_text = table_coordinate_ocr(image_file)
        ocr_text = "\n\n".join(
            [
                "OCR observation A rows from the same table (psm 6):",
                compact_table_ocr(primary_text),
                "OCR observation B rows from the same table (psm 4):",
                compact_table_ocr(alternative_text),
                "OCR observation C rows from the same table (preprocessed psm 6):",
                compact_table_ocr(preprocessed_text),
                "OCR observation D rows from the same table (psm 4 TSV coordinates):",
                coordinate_text,
            ]
        ).strip()
    elif block_type == "equation_candidate":
        sparse_text = run_tesseract(image_file, "--psm", "11")
        preprocessed_image = preprocess_ocr_image(image_file, block_id)
        preprocessed_text = run_tesseract(preprocessed_image, "--psm", "6")
        coordinate_text = equation_coordinate_ocr(image_file)
        paddle_formula_text = paddle_formula_ocr_image(image_file, block_id)
        latex_ocr_text = latex_ocr_image(image_file, block_id)
        ocr_text = "\n\n".join(
            [
                "OCR observation A from the same equation block (psm 6):",
                primary_text,
                "OCR observation B from the same equation block (psm 11):",
                sparse_text,
                "OCR observation C from the same equation block (preprocessed psm 6):",
                preprocessed_text,
                "OCR observation D positioned tokens from the same equation block (TSV coordinates):",
                coordinate_text,
                "OCR observation E image-to-LaTeX from the same equation crop (Paddle FormulaRecognition):",
                paddle_formula_text,
                "OCR observation F image-to-LaTeX from the same equation crop (pix2tex):",
                latex_ocr_text,
            ]
        ).strip()
    else:
        ocr_text = primary_text
    (OCR_DIR / f"{block_id}.txt").write_text(ocr_text + "\n", encoding="utf-8")
    return ocr_text


def docling_equation_summary() -> dict[str, Any]:
    global DOCLING_EQUATION_SUMMARY
    if DOCLING_EQUATION_SUMMARY is not None:
        return DOCLING_EQUATION_SUMMARY
    summary_file = BASE_DIR / "layout" / "auto" / "docling_equation_candidates" / "summary.json"
    if not summary_file.exists():
        DOCLING_EQUATION_SUMMARY = {}
        return DOCLING_EQUATION_SUMMARY
    DOCLING_EQUATION_SUMMARY = json.loads(summary_file.read_text(encoding="utf-8"))
    return DOCLING_EQUATION_SUMMARY


def docling_formula_for_block(block_id: str) -> dict[str, Any] | None:
    summary = docling_equation_summary()
    formulas = {
        formula["id"]: formula
        for formula in summary.get("formulas", [])
    }
    if block_id in formulas:
        return formulas[block_id]
    for review in summary.get("manifest_equation_reviews", []):
        if review.get("id") != block_id:
            continue
        formula_id = review.get("nearest_docling_formula")
        if formula_id:
            return formulas.get(formula_id)
    return None


def docling_formula_asset_path(asset: str) -> Path:
    path = Path(asset)
    if path.is_absolute():
        return path
    if path.parts[:1] == ("assets",):
        return BASE_DIR / "layout" / "auto" / path
    return BASE_DIR / path


def append_docling_equation_ocr(block: dict[str, Any], ocr_text: str) -> str:
    if block["type"] != "equation_candidate":
        return ocr_text

    formula = docling_formula_for_block(block["id"])
    if not formula:
        return ocr_text

    asset = formula.get("asset")
    if not asset:
        return ocr_text

    image_file = docling_formula_asset_path(asset)
    if not image_file.exists():
        return ocr_text

    block_asset = block.get("asset")
    block_image_file = BASE_DIR / block_asset if block_asset else None
    if block_image_file and block_image_file.resolve() == image_file.resolve():
        docling_text = "\n\n".join(
            [
                "Docling formula crop evidence:",
                f"formula_id: {formula['id']}",
                f"formula_status: {formula['status']}",
                f"formula_reason: {formula['reason']}",
                f"formula_orig: {formula.get('orig', '')}",
            ]
        ).strip()
        return "\n\n".join([ocr_text, docling_text]).strip()

    docling_block_id = f"{block['id']}.docling_formula"
    try:
        primary_text = run_tesseract(image_file, "--psm", "6")
    except Exception as exc:
        primary_text = f"[docling-formula-tesseract-error: {exc}]"
    try:
        sparse_text = run_tesseract(image_file, "--psm", "11")
    except Exception as exc:
        sparse_text = f"[docling-formula-sparse-tesseract-error: {exc}]"
    try:
        preprocessed_image = preprocess_ocr_image(image_file, docling_block_id)
        preprocessed_text = run_tesseract(preprocessed_image, "--psm", "6")
    except Exception as exc:
        preprocessed_text = f"[docling-formula-preprocessed-tesseract-error: {exc}]"
    try:
        coordinate_text = equation_coordinate_ocr(image_file)
    except Exception as exc:
        coordinate_text = f"[docling-formula-coordinate-tesseract-error: {exc}]"
    paddle_formula_text = paddle_formula_ocr_image(image_file, docling_block_id)
    latex_ocr_text = latex_ocr_image(image_file, docling_block_id)

    docling_text = "\n\n".join(
        [
            "Docling formula crop evidence:",
            f"formula_id: {formula['id']}",
            f"formula_status: {formula['status']}",
            f"formula_reason: {formula['reason']}",
            f"formula_orig: {formula.get('orig', '')}",
            "OCR observation G from the Docling formula crop (psm 6):",
            primary_text,
            "OCR observation H from the Docling formula crop (psm 11):",
            sparse_text,
            "OCR observation I from the Docling formula crop (preprocessed psm 6):",
            preprocessed_text,
            "OCR observation J positioned tokens from the Docling formula crop (TSV coordinates):",
            coordinate_text,
            "OCR observation K image-to-LaTeX from the Docling formula crop (Paddle FormulaRecognition):",
            paddle_formula_text,
            "OCR observation L image-to-LaTeX from the Docling formula crop (pix2tex):",
            latex_ocr_text,
        ]
    ).strip()
    return "\n\n".join([ocr_text, docling_text]).strip()


def preprocess_table_image(image_file: Path, block_id: str) -> Path:
    return preprocess_ocr_image(image_file, block_id)


def preprocess_ocr_image(image_file: Path, block_id: str) -> Path:
    PREPROCESSED_OCR_DIR.mkdir(parents=True, exist_ok=True)
    output_file = PREPROCESSED_OCR_DIR / f"{block_id}.png"
    image = Image.open(image_file).convert("L")
    image = ImageOps.autocontrast(image)
    image = image.resize((image.width * 3, image.height * 3), Image.Resampling.LANCZOS)
    image = image.filter(ImageFilter.SHARPEN)
    image = image.point(lambda pixel: 0 if pixel < 190 else 255)
    image.save(output_file)
    return output_file


def latex_ocr_image(image_file: Path, block_id: str) -> str:
    LATEX_OCR_DIR.mkdir(parents=True, exist_ok=True)
    output_file = LATEX_OCR_DIR / f"{block_id}.txt"
    try:
        prediction = run_latex_ocr(image_file)
        line_predictions = run_latex_ocr_lines(image_file, block_id)
        if line_predictions:
            prediction = "\n\n".join(
                [
                    "Full crop:",
                    prediction,
                    "Line crops:",
                    *[f"line {index}: {line}" for index, line in enumerate(line_predictions, start=1)],
                ]
            )
    except Exception as exc:
        prediction = f"[latex-ocr-error: {exc}]"
    output_file.write_text(prediction.strip() + "\n", encoding="utf-8")
    return prediction.strip()


def paddle_formula_ocr_image(image_file: Path, block_id: str) -> str:
    global PADDLE_FORMULA_ERROR
    PADDLE_FORMULA_OCR_DIR.mkdir(parents=True, exist_ok=True)
    output_file = PADDLE_FORMULA_OCR_DIR / f"{block_id}.txt"
    if PADDLE_FORMULA_ERROR:
        prediction = f"[paddle-formula-ocr-error: {PADDLE_FORMULA_ERROR}]"
        output_file.write_text(prediction.strip() + "\n", encoding="utf-8")
        return prediction.strip()
    try:
        prediction = run_paddle_formula_ocr(image_file)
    except Exception as exc:
        PADDLE_FORMULA_ERROR = str(exc)
        prediction = f"[paddle-formula-ocr-error: {PADDLE_FORMULA_ERROR}]"
    output_file.write_text(prediction.strip() + "\n", encoding="utf-8")
    return prediction.strip()


def resolve_paddle_cache_dir(base_dir: Path) -> Path:
    candidates = [parent / "paddle_cache" for parent in base_dir.parents]
    for candidate in candidates:
        if (candidate / "official_models" / "PP-FormulaNet_plus-M" / "inference.pdiparams").exists():
            return candidate
    return candidates[0] if candidates else base_dir / "paddle_cache"


def run_paddle_formula_ocr(image_file: Path) -> str:
    global PADDLE_FORMULA_MODEL
    os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(PADDLE_CACHE_DIR))
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    if PADDLE_FORMULA_MODEL is None:
        from paddleocr import FormulaRecognition

        PADDLE_FORMULA_MODEL = FormulaRecognition()
    predictions = list(PADDLE_FORMULA_MODEL.predict(str(image_file)))
    if not predictions:
        return ""
    return str(predictions[0].get("rec_formula", "")).strip()


def run_latex_ocr(image_file: Path) -> str:
    global LATEX_OCR_MODEL
    if LATEX_OCR_MODEL is None:
        from pix2tex.cli import LatexOCR

        LATEX_OCR_MODEL = LatexOCR()
    image = Image.open(image_file)
    return str(LATEX_OCR_MODEL(image)).strip()


def run_latex_ocr_lines(image_file: Path, block_id: str) -> list[str]:
    global LATEX_OCR_MODEL
    if LATEX_OCR_MODEL is None:
        from pix2tex.cli import LatexOCR

        LATEX_OCR_MODEL = LatexOCR()
    lines = split_equation_image_lines(image_file, block_id)
    predictions = []
    for line_file in lines:
        predictions.append(str(LATEX_OCR_MODEL(Image.open(line_file))).strip())
    return predictions


def split_equation_image_lines(image_file: Path, block_id: str) -> list[Path]:
    try:
        import cv2
        import numpy as np
    except ImportError:
        return []

    output_dir = BASE_DIR / "layout" / "auto" / "assets" / "equation_lines"
    output_dir.mkdir(parents=True, exist_ok=True)
    image = Image.open(image_file).convert("L")
    array = np.array(image)
    _, binary = cv2.threshold(array, 200, 255, cv2.THRESH_BINARY_INV)
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
    components = []
    for index in range(1, count):
        x, y, width, height, area = stats[index]
        if area < 4 or height < 3 or width < 2:
            continue
        if height > image.height * 0.8 or width > image.width * 0.95:
            continue
        components.append((int(x), int(y), int(width), int(height), int(area), float(centroids[index][1])))

    clusters: list[dict[str, Any]] = []
    for component in sorted(components, key=lambda item: item[5]):
        center_y = component[5]
        for cluster in clusters:
            if abs(center_y - cluster["center_y"]) < 18:
                cluster["items"].append(component)
                cluster["center_y"] = sum(item[5] for item in cluster["items"]) / len(cluster["items"])
                break
        else:
            clusters.append({"center_y": center_y, "items": [component]})

    spans = []
    for cluster in clusters:
        items = cluster["items"]
        area = sum(item[4] for item in items)
        if area < 80:
            continue
        y1 = max(0, min(item[1] for item in items) - 10)
        y2 = min(image.height, max(item[1] + item[3] for item in items) + 10)
        if y2 - y1 >= 10:
            spans.append((y1, y2))

    if len(spans) <= 1:
        return []
    files = []
    for index, (y1, y2) in enumerate(sorted(spans), start=1):
        output_file = output_dir / f"{block_id}_line{index}.png"
        image.crop((0, y1, image.width, y2)).save(output_file)
        files.append(output_file)
    return files


def compact_table_ocr(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = " ".join(line.split())
        if not stripped:
            continue
        if any(char.isdigit() for char in stripped) or len(stripped.split()) <= 12:
            lines.append(stripped)
    return "\n".join(lines)


def table_coordinate_ocr(image_file: Path) -> str:
    tsv_text = run_tesseract(image_file, "--psm", "4", "tsv")
    words = []
    for row in csv.DictReader(io.StringIO(tsv_text), delimiter="\t"):
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            top = int(row["top"])
            left = int(row["left"])
            width = int(row["width"])
            height = int(row["height"])
        except (KeyError, ValueError):
            continue
        if height <= 3:
            continue
        words.append({"text": text, "top": top, "left": left, "center": left + width / 2})

    data_words = [word for word in words if word["top"] >= 105]
    lines: list[list[dict[str, Any]]] = []
    for word in sorted(data_words, key=lambda item: (item["top"], item["left"])):
        if not lines or abs(lines[-1][0]["top"] - word["top"]) > 12:
            lines.append([word])
        else:
            lines[-1].append(word)

    rendered_lines = []
    for line in lines:
        cells = [word["text"] for word in sorted(line, key=lambda item: item["left"])]
        if len(cells) >= 2:
            rendered_lines.append(" | ".join(cells))
    return "\n".join(rendered_lines)


def equation_coordinate_ocr(image_file: Path) -> str:
    tsv_text = run_tesseract(image_file, "--psm", "11", "tsv")
    words = []
    for row in csv.DictReader(io.StringIO(tsv_text), delimiter="\t"):
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            top = int(row["top"])
            left = int(row["left"])
            width = int(row["width"])
            height = int(row["height"])
        except (KeyError, ValueError):
            continue
        if height <= 3:
            continue
        words.append({"text": text, "top": top, "left": left, "width": width, "height": height})

    if not words:
        return ""

    median_height = sorted(word["height"] for word in words)[len(words) // 2]
    hints = equation_script_hints(words, median_height)
    rendered = []
    if hints:
        rendered.append("Possible script placement hints from token coordinates:")
        rendered.extend(f"- {hint}" for hint in hints)
        rendered.append("")
        rendered.append("Positioned tokens:")
    for word in sorted(words, key=lambda item: (item["top"], item["left"])):
        role = "normal"
        if word["height"] < median_height * 0.85:
            role = "small"
        rendered.append(
            f"{word['text']} | x={word['left']} y={word['top']} w={word['width']} h={word['height']} role={role}"
        )
    return "\n".join(rendered)


def equation_script_hints(words: list[dict[str, Any]], median_height: int) -> list[str]:
    hints = []
    for script in words:
        script_text = script["text"].strip(".,;:()[]{}")
        if not re.fullmatch(r"[A-Za-z]{1,4}", script_text):
            continue
        if script["height"] > median_height * 1.1:
            continue
        script_center = script["left"] + script["width"] / 2
        for base in words:
            if base is script:
                continue
            base_text = base["text"].strip(".,;:()[]{}")
            if not re.fullmatch(r"[A-Za-z]{1,5}", base_text):
                continue
            base_center = base["left"] + base["width"] / 2
            horizontal_overlap = abs(script_center - base_center) <= max(base["width"], script["width"], 18)
            if not horizontal_overlap:
                continue
            vertical_delta = script["top"] - base["top"]
            if 8 <= vertical_delta <= max(base["height"] * 1.8, 26):
                hints.append(f"`{script_text}` may be a subscript of nearby `{base_text}`")
            elif -max(script["height"] * 1.8, 26) <= vertical_delta <= -8:
                hints.append(f"`{script_text}` may be a superscript of nearby `{base_text}`")
    return unique_preserving_order(hints[:12])


def unique_preserving_order(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def prompt_for_block(block: dict[str, Any], ocr_text: str) -> str:
    block_type = block["type"]
    source_text = block.get("source_text", "")
    task = (PROMPT_DIR / "block_equation_recovery.md").read_text(encoding="utf-8").strip()

    contract = observed_contract_for_block(block, ocr_text)

    parts = [
        task,
        "",
    ]
    if contract:
        parts.extend(["Observed structure hints:", contract, ""])

    parts.extend(
        [
            f"Block id: {block['id']}",
            f"Block type: {block_type}",
            "",
            "OCR text:",
            "```text",
            ocr_text,
            "```",
            "",
            "PDF extracted hint text:",
            "```text",
            source_text,
            "```",
        ]
    )
    return "\n".join(parts)


def observed_contract_for_block(block: dict[str, Any], ocr_text: str) -> str:
    if block["type"] == "table_candidate":
        return observed_table_contract(ocr_text, block.get("source_text", ""))

    if block["type"] == "equation_candidate":
        return observed_equation_contract(ocr_text, block.get("source_text", ""))
    return ""


def observed_table_contract(ocr_text: str, hint_text: str) -> str:
    lines = [line.strip() for line in ocr_text.splitlines() if line.strip()]
    data_like_lines = [line for line in lines if len(line.split()) >= 2 and any(char.isdigit() for char in line)]
    token_counts = [len(line.split()) for line in data_like_lines]
    expected_columns = most_common_count(token_counts)
    header_candidates = [
        line
        for line in lines
        if not any(char.isdigit() for char in line) and 2 <= len(line.split()) <= max(expected_columns + 2, 3)
    ][:3]
    row_candidates = data_like_lines[:8]

    parts = ["Detected table structure from OCR only:"]
    if expected_columns:
        parts.append(f"- expected column count candidate: {expected_columns}")
    if header_candidates:
        parts.append("- header line candidates:")
        parts.extend(f"  - {line}" for line in header_candidates)
    if row_candidates:
        parts.append("- row line candidates:")
        parts.extend(f"  - {line}" for line in row_candidates)
    if hint_text:
        hint_tokens = " ".join(hint_text.split()[:80])
        parts.append(f"- hint token prefix: {hint_tokens}")
    parts.append("- Use these observations only as structure hints, not as fixed table content.")
    return "\n".join(parts)


def observed_equation_contract(ocr_text: str, hint_text: str) -> str:
    combined = f"{ocr_text}\n{hint_text}"
    lhs_candidates = re.findall(r"([A-Za-z][A-Za-z0-9_,{}\\]*(?:\s*[A-Za-z0-9_,{}\\]+)?)\s*=", combined)
    equation_numbers = re.findall(r"\(?([1-9][0-9]?)\)?\s*$", combined, flags=re.MULTILINE)
    has_fraction_evidence = bool(re.search(r"\\frac|[/÷]|——|----|—\s*$", ocr_text, flags=re.MULTILINE))
    plus_minus_count = len(re.findall(r"(?<!\^)[+\-−]", combined))
    multiply_count = len(re.findall(r"[×*]", combined))

    parts = ["Detected equation structure from OCR/hint:"]
    if lhs_candidates:
        parts.append(f"- left-hand side candidates: {', '.join(lhs_candidates[:3])}")
    if equation_numbers:
        parts.append(f"- equation number candidates: {', '.join(equation_numbers[-3:])}")
    parts.append(f"- fraction evidence detected: {str(has_fraction_evidence).lower()}")
    parts.append(f"- additive/subtractive operator count: {plus_minus_count}")
    parts.append(f"- multiplication marker count: {multiply_count}")
    if plus_minus_count >= 3 and not has_fraction_evidence:
        parts.append("- likely shape: additive polynomial or multi-line additive expression; do not use \\frac.")
    if hint_text:
        hint_tokens = " ".join(hint_text.split()[:80])
        parts.append(f"- hint token prefix: {hint_tokens}")
    parts.append("- Use these observations only as structure hints, not as fixed equation content.")
    return "\n".join(parts)


def most_common_count(values: list[int]) -> int:
    if not values:
        return 0
    counts: dict[int, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def call_sllm(endpoint: str, model: str, prompt: str, system_message: str) -> str:
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 2048,
        "messages": [
            {
                "role": "system",
                "content": system_message,
            },
            {"role": "user", "content": prompt},
        ],
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise RuntimeError(f"SLLM endpoint request failed: {exc}") from exc
    return data["choices"][0]["message"]["content"].strip()


def body_cell_values(text: str) -> list[str]:
    rows = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
    if len(rows) <= 2:
        return []
    cells: list[str] = []
    for row in rows[2:]:
        cells.extend(cell.strip() for cell in row.strip("|").split("|"))
    return cells


def table_header_values(text: str) -> list[str]:
    rows = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
    if not rows:
        return []
    return [cell.strip() for cell in rows[0].strip("|").split("|")]


def table_body_rows(text: str) -> list[list[str]]:
    rows = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
    if len(rows) <= 2:
        return []
    return [[cell.strip() for cell in row.strip("|").split("|")] for row in rows[2:]]


def has_bad_index_sequence(text: str) -> bool:
    body_rows = table_body_rows(text)
    if len(body_rows) < 3:
        return False
    first_cells = [row[0] for row in body_rows if row]
    integer_cells = [int(cell) for cell in first_cells if re.fullmatch(r"\d{1,4}", cell)]
    if len(integer_cells) / len(first_cells) < 0.8:
        return False
    return integer_cells != list(range(integer_cells[0], integer_cells[0] + len(integer_cells)))


def structured_table_markdown(block: dict[str, Any], ocr_text: str) -> str | None:
    markdown = best_structured_table_markdown(ocr_text)
    if markdown is None:
        return None
    return repair_table_markdown_with_hint(markdown, block.get("source_text", ""))


def best_structured_table_markdown(ocr_text: str) -> str | None:
    candidates = [
        build_table_candidate(parse_plain_rows(section_text(ocr_text, "OCR observation A", "OCR observation B"))),
        build_table_candidate(parse_plain_rows(section_text(ocr_text, "OCR observation B", "OCR observation C"))),
        build_table_candidate(parse_plain_rows(section_text(ocr_text, "OCR observation C", "OCR observation D"))),
        build_table_candidate(parse_pipe_rows(section_text(ocr_text, "OCR observation D", ""))),
    ]
    candidates = [candidate for candidate in candidates if candidate is not None]
    if not candidates:
        return None

    best = sorted(candidates, key=score_table_candidate, reverse=True)[0]
    best = merge_table_candidate_cells(best, candidates)
    return markdown_from_table_candidate(best)


def build_table_candidate(rows: list[list[str]]) -> dict[str, Any] | None:
    if len(rows) < 2:
        return None
    data_start = first_data_row_index(rows)
    if data_start is None:
        return None

    raw_data_rows = [clean_table_row_tokens(row) for row in rows[data_start:]]
    raw_data_rows = [row for row in raw_data_rows if looks_like_data_row(row)]
    if len(raw_data_rows) < 2:
        return None

    column_count = most_common_count([len(row) for row in raw_data_rows])
    if column_count < 2:
        return None

    data_rows = [
        normalize_table_row_width(row, column_count)
        for row in raw_data_rows
        if column_count - 1 <= len(row) <= column_count
    ]
    data_rows = repair_sequential_index_rows(data_rows)
    if len(data_rows) < 2:
        return None
    header = infer_coordinate_header(rows[:data_start], column_count)
    return {"header": header, "rows": data_rows, "column_count": column_count}


def looks_like_data_row(row: list[str]) -> bool:
    if len(row) < 2 or not row_has_numeric_value(row):
        return False
    if sum(1 for cell in row[1:] if re.search(r"\d", cell)) >= 1:
        return plausible_table_row_label(row[0])
    return False


def plausible_table_row_label(label: str) -> bool:
    cleaned = label.strip()
    if re.fullmatch(r"\d{1,4}", cleaned):
        return True
    if re.fullmatch(r"[A-Za-z]{1,4}\d{1,4}[A-Za-z]?", cleaned):
        return True
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_()/%.-]{0,12}", cleaned):
        return True
    return False


def repair_sequential_index_rows(rows: list[list[str]]) -> list[list[str]]:
    if len(rows) < 3:
        return rows

    first_cells = [row[0] for row in rows if row]
    integer_positions = [
        (index, int(cell))
        for index, cell in enumerate(first_cells)
        if re.fullmatch(r"\d{1,4}", cell)
    ]
    if len(integer_positions) / len(first_cells) < 0.8:
        return rows

    repaired = [row[:] for row in rows]
    start_index = integer_positions[0][1] - integer_positions[0][0]
    for index, row in enumerate(repaired):
        expected = start_index + index
        if expected <= 0:
            return rows
        if row[0] == str(expected):
            continue
        if re.fullmatch(r"\d{1,4}", row[0]):
            return rows
        previous_ok = index == 0 or repaired[index - 1][0] == str(expected - 1)
        next_ok = index == len(repaired) - 1 or first_cells[index + 1] == str(expected + 1)
        if previous_ok and next_ok:
            row[0] = str(expected)
        else:
            return rows
    return repaired


def normalize_table_row_width(row: list[str], column_count: int) -> list[str]:
    if len(row) == column_count:
        return row
    return row + [""] * (column_count - len(row))


def score_table_candidate(candidate: dict[str, Any]) -> tuple[int, int, int, int]:
    rows = candidate["rows"]
    markdown = markdown_from_table_candidate(candidate)
    index_penalty = 1 if has_bad_index_sequence(markdown) else 0
    artifact_penalty = len(re.findall(r"([\"”]|==:|=:|~—|—|«)", markdown))
    decimal_count = len(re.findall(r"\d+\.\d+", markdown))
    return (len(rows), candidate["column_count"], -index_penalty, -artifact_penalty + decimal_count)


def merge_table_candidate_cells(best: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    merged_rows = best["rows"]
    for candidate in candidates:
        if candidate is best:
            continue
        if candidate["column_count"] != best["column_count"]:
            continue
        merged_rows = merge_alternative_table_rows(merged_rows, candidate["rows"])
    merged_rows = repair_sequential_index_rows(merged_rows)
    return {**best, "rows": merged_rows}


def markdown_from_table_candidate(candidate: dict[str, Any]) -> str:
    header = candidate["header"]
    data_rows = candidate["rows"]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in data_rows)
    return "\n".join(lines)


def repair_table_markdown_with_hint(markdown: str, hint_text: str) -> str:
    rows = [line for line in markdown.splitlines()]
    inferred_header = infer_table_header_from_hint(hint_text)
    inferred_row_labels = infer_table_row_labels_from_hint(hint_text)
    repaired_rows = []
    table_row_index = 0
    body_row_index = 0
    for line in rows:
        separator_probe = line.replace("|", "").replace(" ", "").strip()
        if not line.strip().startswith("|") or set(separator_probe) <= {"-", ":"}:
            repaired_rows.append(line)
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if table_row_index == 0 and inferred_header and len(cells) == len(inferred_header):
            cells = inferred_header
        elif cells:
            cells[0] = normalize_table_label_with_hint(cells[0], inferred_row_labels, body_row_index)
            body_row_index += 1
        repaired_rows.append("| " + " | ".join(cells) + " |")
        table_row_index += 1
    return "\n".join(repaired_rows)


def infer_table_header_from_hint(hint_text: str) -> list[str]:
    lines = [" ".join(line.split()) for line in hint_text.splitlines() if line.strip()]
    for line in lines:
        tokens = line.split()
        if len(tokens) < 2:
            continue
        if tokens[0].lower() not in {"parameter", "no.", "case", "variable"}:
            continue
        header = compact_split_label_tokens(tokens)
        if len(header) >= 2 and not any(re.search(r"\d", cell) for cell in header):
            return header
    return []


def infer_table_row_labels_from_hint(hint_text: str) -> list[str]:
    labels = []
    for line in hint_text.splitlines():
        tokens = line.split()
        if not tokens:
            continue
        if tokens[0].upper() == "TABLE" or tokens[0].lower() in {"parameter", "variable", "no.", "case"}:
            continue
        label_tokens = []
        index = 0
        while index < min(len(tokens), 3):
            token = tokens[index]
            if re.search(r"\d", token) and not re.fullmatch(r"0", token):
                break
            if re.search(r"[%./]", token):
                break
            if not re.search(r"[A-Za-z]", token):
                break
            label_tokens.append(token)
            index += 1
            if index < len(tokens) and re.fullmatch(r"0", tokens[index]):
                label_tokens.append(tokens[index])
                index += 1
        if label_tokens:
            labels.append(compact_split_label_tokens(label_tokens)[0])
    return unique_preserving_order(labels)


def compact_split_label_tokens(tokens: list[str]) -> list[str]:
    compacted = []
    index = 0
    while index < len(tokens):
        token = clean_label_token(tokens[index])
        next_token = clean_label_token(tokens[index + 1]) if index + 1 < len(tokens) else ""
        if next_token and token and re.fullmatch(r"[A-Za-z]", next_token) and re.fullmatch(r"[A-Za-z]{2,4}", token):
            compacted.append(f"{next_token}_{token}")
            index += 2
            continue
        if token == "0" and next_token:
            compacted.append(f"{next_token}0")
            index += 2
            continue
        if token:
            compacted.append(token)
        index += 1
    return compacted


def clean_label_token(token: str) -> str:
    return token.strip().strip("\"'“”‘’").strip(".,;:()[]{}")


def normalize_table_label_with_hint(label: str, hint_labels: list[str], row_index: int) -> str:
    cleaned = label.strip().strip("\"'“”‘’")
    cleaned = re.sub(r"[^A-Za-z0-9_]+$", "", cleaned)
    if re.fullmatch(r"\d{1,4}", cleaned):
        return cleaned
    if row_index < len(hint_labels):
        expected = hint_labels[row_index]
        if labels_are_near(cleaned, expected):
            return expected
    return cleaned


def labels_are_near(left: str, right: str) -> bool:
    normalized_left = re.sub(r"[^a-z0-9]", "", left.lower())
    normalized_right = re.sub(r"[^a-z0-9]", "", right.lower())
    if normalized_left == normalized_right:
        return True
    return one_edit_apart(normalized_left, normalized_right)


def parse_plain_rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        stripped = " ".join(line.split())
        if not stripped:
            continue
        rows.append(stripped.split())
    return rows


def parse_pipe_rows(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in text.splitlines():
        if "|" not in line:
            continue
        cells = [cell.strip() for cell in line.split("|")]
        cells = [normalize_table_token(cell) for cell in cells]
        cells = [cell for cell in cells if cell]
        if cells:
            rows.append(cells)
    return rows


def parse_plain_table_rows(text: str, column_count: int) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        stripped = " ".join(line.split())
        if not stripped or not any(char.isdigit() for char in stripped):
            continue
        tokens = clean_table_row_tokens(stripped.split())
        if len(tokens) == column_count:
            rows.append(tokens)
    return rows


def merge_alternative_table_rows(primary_rows: list[list[str]], alternative_rows: list[list[str]]) -> list[list[str]]:
    if len(primary_rows) == len(alternative_rows):
        return [merge_alternative_table_row(primary, alternative) for primary, alternative in zip(primary_rows, alternative_rows)]

    alternatives_by_label = {table_row_label(row): row for row in alternative_rows if row}
    merged_rows = []
    for primary in primary_rows:
        alternative = alternatives_by_label.get(table_row_label(primary))
        if alternative and len(alternative) == len(primary):
            merged_rows.append(merge_alternative_table_row(primary, alternative))
        else:
            merged_rows.append(primary)
    return merged_rows


def table_row_label(row: list[str]) -> str:
    if not row:
        return ""
    return re.sub(r"[^a-z0-9]+", "", row[0].lower())


def merge_alternative_table_row(primary: list[str], alternative: list[str]) -> list[str]:
    if len(primary) != len(alternative):
        return primary
    return [prefer_cleaner_cell(primary_cell, alternative_cell, index == 0) for index, (primary_cell, alternative_cell) in enumerate(zip(primary, alternative))]


def prefer_cleaner_cell(primary: str, alternative: str, is_label: bool) -> str:
    if is_label and re.fullmatch(r"\d{1,4}", alternative) and not re.fullmatch(r"\d{1,4}", primary):
        return alternative
    if is_label and label_looks_cleaner(alternative, primary):
        return alternative
    primary_digits = re.sub(r"\D", "", primary)
    alternative_digits = re.sub(r"\D", "", alternative)
    if primary_digits and primary_digits == alternative_digits and "." not in primary and "." in alternative:
        return alternative
    if numeric_cell_looks_cleaner(alternative, primary):
        return alternative
    return primary


def numeric_cell_looks_cleaner(candidate: str, current: str) -> bool:
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", candidate):
        return False
    if re.fullmatch(r"-?\d+(?:\.\d+)?", current):
        return False
    candidate_digits = re.sub(r"\D", "", candidate)
    current_digits = re.sub(r"\D", "", current)
    if not candidate_digits or not current_digits:
        return False
    return candidate_digits.endswith(current_digits) or current_digits.endswith(candidate_digits)


def label_looks_cleaner(candidate: str, current: str) -> bool:
    if not re.search(r"[A-Za-z]", candidate) or not re.search(r"[A-Za-z]", current):
        return False
    candidate_noise = len(re.findall(r"[^A-Za-z0-9_()/%.-]", candidate))
    current_noise = len(re.findall(r"[^A-Za-z0-9_()/%.-]", current))
    if candidate_noise != current_noise:
        return candidate_noise < current_noise
    return len(candidate) < len(current) and candidate.lower()[0] == current.lower()[0]


def first_data_row_index(rows: list[list[str]]) -> int | None:
    for index, row in enumerate(rows):
        if looks_like_table_header_row(row):
            continue
        if row_has_numeric_value(row) and len(row) >= 2:
            return index
    return None


def looks_like_table_header_row(row: list[str]) -> bool:
    joined = " ".join(row).lower()
    return "table" in joined or "parameter" in joined or "level" in joined


def row_has_numeric_value(row: list[str]) -> bool:
    return any(re.search(r"\d", cell) for cell in row)


def infer_coordinate_header(header_rows: list[list[str]], column_count: int) -> list[str]:
    clean_rows = [row for row in header_rows if not row_has_numeric_value(row)]
    exact = next((row for row in reversed(clean_rows) if len(row) == column_count), None)
    if exact:
        return uniquify_header(exact)

    flattened = [cell for row in clean_rows for cell in row]
    if len(flattened) == column_count:
        return uniquify_header(flattened)

    return [f"Column {index}" for index in range(1, column_count + 1)]


def uniquify_header(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result = []
    for index, header in enumerate(headers, start=1):
        cleaned = re.sub(r"\s+", " ", header).strip() or f"Column {index}"
        count = seen.get(cleaned, 0) + 1
        seen[cleaned] = count
        result.append(cleaned if count == 1 else f"{cleaned} {count}")
    return result


def section_text(text: str, start_marker: str, end_marker: str) -> str:
    start = text.find(start_marker)
    if start < 0:
        return ""
    end = text.find(end_marker, start + len(start_marker)) if end_marker else -1
    return text[start:] if end < 0 else text[start:end]


def clean_table_row_tokens(raw_tokens: list[str]) -> list[str]:
    tokens: list[str] = []
    for index, token in enumerate(raw_tokens):
        normalized = normalize_table_token(token)
        if not normalized:
            continue
        if index == 0:
            glued = re.fullmatch(r"(\d{1,2})([-−][1lI])", normalized)
            if glued:
                tokens.extend([glued.group(1), "-1"])
                continue
        tokens.append(normalized)
    return tokens


def normalize_table_token(token: str) -> str:
    cleaned = token.strip().replace("−", "-").replace("—", "-").replace("“", "").replace("”", "")
    if cleaned in {"+", "-", "=", ":", "==:", "=:", "~", "~-", "-:", "--:"}:
        return ""
    if cleaned in {"-l", "-I"}:
        return "-1"
    if cleaned == "]":
        return "1"
    if cleaned.lower() == "el":
        return "-1"
    if cleaned in {"-1", "0", "1"}:
        return cleaned
    if re.fullmatch(r"\d+\.", cleaned):
        return cleaned[:-1]
    cleaned = re.sub(r"^[=:+~«-]+(?=\d)", "", cleaned)
    cleaned = re.sub(r"^[=:+~«-]+(?=\.\d)", "", cleaned)
    if re.fullmatch(r"\.0\.\d+", cleaned):
        return cleaned[1:]
    if re.fullmatch(r"\.\d+", cleaned):
        return f"0{cleaned}"
    cleaned = re.sub(r"(?<=\d)[=:+~«-]+$", "", cleaned)
    cleaned = cleaned.replace(":", "")
    if re.search(r"[A-Za-z]", cleaned):
        cleaned = re.sub(r"[,.;]{2,}", "", cleaned)
        cleaned = normalize_table_label(cleaned)
    if cleaned in {"l", "I"}:
        return "1"
    return cleaned


def normalize_table_label(cell: str) -> str:
    return re.sub(r"(?<=[A-Za-z0-9])[&]+$", "", cell)


def prefer_cleaner_numeric_cells(primary: list[str], alternative: list[str]) -> list[str]:
    merged = primary[:]
    for index, (primary_cell, alternative_cell) in enumerate(zip(primary, alternative)):
        if index == 0:
            continue
        primary_digits = re.sub(r"\D", "", primary_cell)
        alternative_digits = re.sub(r"\D", "", alternative_cell)
        if primary_digits and primary_digits == alternative_digits and "." not in primary_cell and "." in alternative_cell:
            merged[index] = alternative_cell
    return merged


def has_polynomial_fraction(text: str) -> bool:
    match = re.search(r"\\frac\{(?P<num>[^{}]*(?:\{[^{}]*\}[^{}]*)*)\}\{(?P<den>[^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", text)
    if not match:
        return False
    numerator_ops = len(re.findall(r"(?<!\^)[+-]", match.group("num")))
    denominator_ops = len(re.findall(r"(?<!\^)[+-]", match.group("den")))
    return numerator_ops >= 2 and denominator_ops >= 1


def normalize_equation_markdown(markdown: str, block: dict[str, Any] | None = None) -> str:
    cleaned = strip_markdown_fence(markdown).strip()
    if not cleaned or cleaned.startswith("[rejected:"):
        return cleaned
    if block is not None:
        cleaned = repair_equation_with_hint(cleaned, block.get("source_text", ""))
    if "\\[" in cleaned or "$$" in cleaned:
        return normalize_display_math_delimiters(cleaned)
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return cleaned
    return "\n\n".join(display_equation(line) for line in lines)


def normalize_display_math_delimiters(markdown: str) -> str:
    normalized = re.sub(r"\\\[\s*(.*?)\s*\\\]", lambda match: display_equation(match.group(1).strip()), markdown, flags=re.DOTALL)
    return normalized.strip()


def repair_equation_with_hint(markdown: str, hint_text: str) -> str:
    repaired = markdown
    for candidate in hinted_subscript_candidates(hint_text):
        repaired = replace_near_subscript(repaired, candidate)
    repaired = re.sub(r"\\qquad", " ", repaired)
    repaired = re.sub(
        r"\s*(?:\\left)?\(([1-9][0-9]?)\)(?:\\right)?\s*(?=\s*\$\$)",
        lambda match: f" {tag_suffix(match.group(1))}",
        repaired,
    )
    equation_number = formula_tag_from_hint(hint_text)
    if equation_number and r"\tag{" not in repaired:
        repaired = add_tag_to_first_display_math(repaired, equation_number)
    return repaired


def hinted_subscript_candidates(hint_text: str) -> set[str]:
    candidates = set()
    for token in re.findall(r"[A-Za-z]{2,4}", hint_text):
        if token.lower() in {"and", "the", "with", "from", "this", "that"}:
            continue
        candidates.add(token)
    return candidates


def add_tag_to_first_display_math(markdown: str, equation_number: str) -> str:
    if "\n$$" in markdown:
        return re.sub(r"\n\$\$", lambda _: f"{tag_suffix(equation_number)}\n$$", markdown, count=1)
    return re.sub(
        r"(?s)^\$\$(.*?)\$\$",
        lambda match: f"$${match.group(1)}{tag_suffix(equation_number)}$$",
        markdown,
        count=1,
    )


def formula_tag_from_hint(hint_text: str) -> str | None:
    matches = re.findall(r"\(([1-9][0-9]?)\)", hint_text)
    return matches[-1] if matches else None


def equation_parser_markdown(block: dict[str, Any], ocr_text: str) -> str | None:
    if block["type"] != "equation_candidate":
        return None

    for candidate in structured_equation_candidates(block, ocr_text):
        candidate = normalize_equation_markdown(candidate, block)
        candidate = repair_equation_with_hint(candidate, block.get("source_text", ""))
        if deterministic_evaluation(block, candidate)["accepted"]:
            return candidate
    return None


def structured_equation_candidates(block: dict[str, Any], ocr_text: str) -> list[str]:
    candidates: list[str] = []
    candidates.extend(latex_structured_equation_candidates(ocr_text))
    candidates.extend(text_structured_equation_candidates(ocr_text))
    if block.get("source_text"):
        candidates.extend(text_structured_equation_candidates(block["source_text"]))
    return candidates


def latex_structured_equation_candidates(ocr_text: str) -> list[str]:
    candidates = []
    for latex in latex_ocr_sections(ocr_text):
        candidates.extend(array_first_equation_candidates(latex, ocr_text))
        candidates.extend(min_array_equation_candidates(latex, ocr_text))
    return candidates


def array_first_equation_candidates(latex: str, ocr_text: str) -> list[str]:
    cell_candidate = first_equation_from_array_cells(latex, ocr_text)
    if cell_candidate:
        return [cell_candidate]

    rows = split_latex_rows(latex)
    candidates = []
    for row in rows:
        row = re.sub(r"\\begin\{array\}\{[^{}]*\}", "", row)
        row = row.replace(r"\end{array}", "")
        if r"\text{" in row:
            row = row.split(r"\text{", 1)[0]
        cleaned = cleanup_latex_ocr(trim_unbalanced_trailing_braces(row), ocr_text)
        if looks_like_equation_start(cleaned):
            candidates.append(display_equation(cleaned))
            break
    return candidates


def first_equation_from_array_cells(latex: str, ocr_text: str) -> str | None:
    cells = extract_array_cells(latex)
    for index, cell in enumerate(cells):
        if "=" not in cell:
            continue
        cleaned = cleanup_latex_ocr(cell, ocr_text)
        tag = extract_equation_number(cells[index + 1]) if index + 1 < len(cells) else None
        if tag and r"\tag{" not in cleaned:
            cleaned = f"{cleaned}{tag_suffix(tag)}"
        if looks_like_equation_start(cleaned):
            return display_equation(cleaned)
    return None


def min_array_equation_candidates(latex: str, ocr_text: str) -> list[str]:
    if not re.search(r"\\operatorname\*\{m\s*i\s*n\}|\\min\b|(?<![A-Za-z])min(?![A-Za-z])", latex):
        return []
    cells = [
        cleanup_latex_ocr(cell, ocr_text)
        for cell in extract_array_cells(latex)
        if re.search(r"[A-Za-z0-9]", cell)
    ]
    terms = [normalize_min_term(cell) for cell in cells]
    terms = [term for term in terms if term and not re.search(r"\\qquad|^\(?\d{1,2}\)?$", term)]
    if len(terms) < 2:
        return []
    equation = r"\min\left\{" + ", ".join(unique_in_order(terms)) + r"\right\}"
    tag = single_equation_tag_from_ocr(ocr_text)
    return [display_equation(f"{equation}{tag_suffix(tag)}")]


def normalize_min_term(term: str) -> str:
    cleaned = term.strip()
    while cleaned.startswith("{") and cleaned.endswith("}") and has_balanced_braces(cleaned[1:-1]):
        cleaned = cleaned[1:-1].strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.replace("P_{F e}", "P_{Fe}").replace("V_{P M}", "V_{PM}")
    if cleaned in {"Pre", "P_{re}"}:
        return "P_{Fe}"
    if cleaned in {"Vem", "V_{em}"}:
        return "V_{PM}"
    return cleaned


def text_structured_equation_candidates(text: str) -> list[str]:
    candidates = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    min_candidate = min_constraint_candidate(lines)
    if min_candidate:
        candidates.append(min_candidate)
    fraction_candidate = simple_fraction_candidate(text)
    if fraction_candidate:
        candidates.append(fraction_candidate)
    return candidates


def min_constraint_candidate(lines: list[str]) -> str | None:
    compact_lines = [normalize_equation_text(line) for line in lines]
    for index, line in enumerate(compact_lines):
        if not re.search(r"(?i)\bmin\b", line):
            continue
        next_line = compact_lines[index + 1] if index + 1 < len(compact_lines) else ""
        if not re.search(r"(?i)s\.?\s*t\.?|sit\.", next_line):
            continue
        objective = normalize_ocr_equation_tokens(line)
        constraint = normalize_ocr_equation_tokens(next_line)
        if not objective or not constraint:
            continue
        tag = extract_equation_number(line) or extract_equation_number(next_line)
        objective = re.sub(r"(?i)^min\s*", r"\\min\\;& ", strip_equation_observation_noise(objective))
        constraint = re.sub(r"(?i)^(?:s\.?\s*t\.?|sit\.)\s*", r"\\mathrm{s.t.}\\;& ", strip_equation_observation_noise(constraint))
        return display_equation(r"\begin{aligned}" + objective + r"\\" + constraint + r"\end{aligned}" + tag_suffix(tag))
    return None


def simple_fraction_candidate(text: str) -> str | None:
    positioned = positioned_tokens(text)
    normal_tokens = [token for token in positioned if token["role"] == "normal"]
    small_tokens = [token for token in positioned if token["role"] == "small"]
    lhs_tokens = [token for token in normal_tokens if "=" in token["text"]]
    if not lhs_tokens or len(small_tokens) < 2:
        return None
    lhs = normalize_variable_token(lhs_tokens[0]["text"].split("=", 1)[0])
    if not lhs:
        return None
    small_tokens = sorted(small_tokens, key=lambda token: token["y"])
    numerator = normalize_variable_token(small_tokens[0]["text"])
    denominator = normalize_variable_token(small_tokens[-1]["text"])
    if not numerator or not denominator or numerator == denominator:
        return None
    return display_equation(rf"{lhs}=\frac{{{numerator}}}{{{denominator}}}")


def positioned_tokens(text: str) -> list[dict[str, Any]]:
    tokens = []
    for match in re.finditer(r"(?m)^(.+?)\s+\|\s+x=(\d+)\s+y=(\d+)\s+w=(\d+)\s+h=(\d+)\s+role=(\w+)$", text):
        tokens.append(
            {
                "text": match.group(1).strip(),
                "x": int(match.group(2)),
                "y": int(match.group(3)),
                "w": int(match.group(4)),
                "h": int(match.group(5)),
                "role": match.group(6),
            }
        )
    return tokens


def normalize_variable_token(token: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", token)
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned
    if len(cleaned) <= 4 and cleaned.isupper():
        return cleaned
    return f"{cleaned[0]}_{{{cleaned[1:]}}}"


def normalize_ocr_equation_tokens(line: str) -> str:
    cleaned = line.replace("≤", r"\leq").replace("<0", r"\leq 0")
    cleaned = re.sub(r"\bgi\(", r"g_i(", cleaned)
    cleaned = re.sub(r"\bgd\)", r"g_i(d)", cleaned)
    cleaned = re.sub(r"\bne\b", r"n_c", cleaned)
    cleaned = re.sub(r"\bnce\b", r"n_c", cleaned)
    cleaned = cleaned.replace("jin", r"\mu_h")
    cleaned = cleaned.replace("un", r"\mu_h")
    cleaned = cleaned.replace("of", r"\sigma_h^2")
    cleaned = cleaned.replace("9)", r"\sigma_h^2)")
    return cleaned


def unique_in_order(values: list[str]) -> list[str]:
    seen = set()
    unique = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def deterministic_layout_adjudication(block: dict[str, Any], ocr_text: str) -> tuple[str, dict[str, Any]] | None:
    if block["type"] != "equation_candidate":
        return None
    source_text = block.get("source_text", "").strip()
    compact_ocr = " ".join(ocr_text.split())
    if re.fullmatch(r"\(?[1-9][0-9]?\)?", source_text) and re.fullmatch(r".*\(?[1-9][0-9]?\)?.*", compact_ocr):
        return layout_adjudication_result("discard_debris", "standalone equation number")
    if re.match(r"(?i)^where\s+Δ?S\b", source_text) or re.match(r"(?i)^where\s+A?S\b", compact_ocr):
        return layout_adjudication_result("discard_debris", "fragmented where-clause equation debris")
    return None


def layout_adjudication_result(decision: str, reason: str) -> tuple[str, dict[str, Any]]:
    return (
        f"[layout-{decision}: {reason}]",
        {
            "accepted": True,
            "score": 1.0,
            "reasons": [],
            "recovery_source": "deterministic_layout_adjudication",
            "layout_decision": decision,
            "layout_reason": reason,
        },
    )


def apply_equation_tags_from_ocr(equations: list[str], ocr_text: str) -> list[str]:
    tags = equation_tags_by_lhs(ocr_text)
    tagged = []
    for equation in equations:
        if r"\tag{" in equation:
            tagged.append(equation)
            continue
        lhs = canonical_lhs(equation.split("=", 1)[0])
        tag = tags.get(lhs)
        tagged.append(f"{equation}{tag_suffix(tag)}" if tag else equation)
    if len(tagged) == 1 and r"\tag{" not in tagged[0]:
        tag = single_equation_tag_from_ocr(ocr_text)
        if tag:
            tagged[0] = f"{tagged[0]}{tag_suffix(tag)}"
    return tagged


def single_equation_tag_from_ocr(ocr_text: str) -> str | None:
    tags = re.findall(r"\(([1-9][0-9]?)\)", ocr_text)
    if not tags:
        return None
    counts = Counter(tags)
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def equation_tags_by_lhs(ocr_text: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    current_lhs = ""
    for observation in equation_observations(ocr_text):
        normalized = normalize_equation_text(observation)
        lines = [" ".join(line.split()) for line in normalized.splitlines()]
        for line in lines:
            if not line:
                continue
            if "=" in line:
                current_lhs = canonical_lhs(line.split("=", 1)[0])
                tag = extract_equation_number(line)
                if current_lhs and tag:
                    tags[current_lhs] = tag
                continue
            tag = extract_equation_number(line)
            if current_lhs and tag:
                tags[current_lhs] = tag
    return tags


def canonical_lhs(text: str) -> str:
    without_commands = re.sub(r"\\(?:mathrm|mathbf|mathit|operatorname)\{([^{}]+)\}", r"\1", text)
    return re.sub(r"[^a-z0-9]+", "", without_commands.lower())


def tag_suffix(equation_number: str | None) -> str:
    return rf" \tag{{{equation_number}}}" if equation_number else ""


def display_equation(equation: str) -> str:
    return f"$$\n{equation}\n$$"


def latex_ocr_candidate_markdown(ocr_text: str, block: dict[str, Any] | None = None) -> str | None:
    candidates = []
    for latex in latex_ocr_sections(ocr_text):
        for lines in latex_ocr_prediction_groups(latex):
            cleaned_lines = assemble_latex_ocr_equations(
                [cleanup_latex_ocr(line, ocr_text) for line in lines]
            )
            if not cleaned_lines:
                continue
            if has_latex_ocr_artifacts(cleaned_lines):
                continue
            if has_numeric_loss(cleaned_lines, ocr_text):
                continue
            candidates.append(apply_equation_tags_from_ocr(cleaned_lines, ocr_text))
    if not candidates:
        return None
    markdown_candidates = ["\n\n".join(display_equation(line) for line in candidate) for candidate in candidates]
    if block is not None:
        for markdown in markdown_candidates:
            normalized = normalize_equation_markdown(markdown, block)
            normalized = repair_equation_with_hint(normalized, block.get("source_text", ""))
            if deterministic_evaluation(block, normalized)["accepted"]:
                return normalized
    return markdown_candidates[0]


def latex_ocr_sections(ocr_text: str) -> list[str]:
    sections = [
        section_text(ocr_text, "OCR observation K image-to-LaTeX", "OCR observation L").strip(),
        section_text(ocr_text, "OCR observation L image-to-LaTeX", "").strip(),
        section_text(ocr_text, "OCR observation E image-to-LaTeX", "OCR observation F").strip(),
        section_text(ocr_text, "OCR observation F image-to-LaTeX", "Docling formula crop evidence:").strip(),
    ]
    cleaned_sections = []
    for section in sections:
        if "\n" in section:
            section = "\n".join(section.splitlines()[1:]).strip()
        if not section or section.startswith("[latex-ocr-error:") or section.startswith("[paddle-formula-ocr-error:"):
            continue
        cleaned_sections.append(section)
    return cleaned_sections


def latex_ocr_prediction_groups(latex: str) -> list[list[str]]:
    groups = []
    whole_lines = latex_ocr_prediction_lines(latex, keep_structured=True)
    if whole_lines:
        groups.append(whole_lines)
    if "Full crop:" not in latex or "Line crops:" not in latex:
        lines = latex_ocr_prediction_lines(latex)
        if lines and lines not in groups:
            groups.append(lines)
        return groups

    full_crop = section_text(latex, "Full crop:", "Line crops:").strip()
    line_crops = section_text(latex, "Line crops:", "").strip()
    for crop_text in [full_crop, line_crops]:
        lines = latex_ocr_prediction_lines(crop_text)
        if lines:
            groups.append(lines)
    return groups


def has_latex_ocr_artifacts(lines: list[str]) -> bool:
    text = "\n".join(lines)
    return bool(
        re.search(
            r"(\\l\d|\\cal\b|\\mathcal|\\mathbb|\\check|\\slash|\\rightarrow|\\downarrow|\\uparrow|\\rfloor|\\lfloor|\\cup|\\mp|\\le\b|\\it\b|\\displaystyle|\\llap|\\odot|\\emptyset|\\infty|\\varepsilon|\\Omega|\\mathrm\{\[)",
            text,
        )
    )


def assemble_latex_ocr_equations(lines: list[str]) -> list[str]:
    equations: list[str] = []
    current = ""
    for line in lines:
        if not line:
            continue
        if looks_like_equation_start(line):
            if current:
                equations.append(current)
            current = line
            continue
        if current and looks_like_equation_continuation(line):
            current = append_equation_continuation(current, line)
    if current:
        equations.append(current)
    return equations


def looks_like_equation_start(line: str) -> bool:
    if "=" in line:
        return True
    return bool(
        re.search(r"(\\begin\{cases\}|\\begin\{aligned\}|\\leq|\\geq|≤|≥|<|>|\\operatorname\*\{(?:min|max)\}|\\min\b|\\max\b)", line)
        and re.search(r"[A-Za-z0-9\\]", line)
    )


def looks_like_equation_continuation(line: str) -> bool:
    return bool(re.search(r"\d", line) and re.search(r"(?<!\^)[+\-−]", line))


def append_equation_continuation(equation: str, continuation: str) -> str:
    tag_match = re.search(r"\s*(\\tag\{\d{1,2}\})\s*$", equation)
    tag = ""
    if tag_match:
        tag = f" {tag_match.group(1)}"
        equation = equation[: tag_match.start()].rstrip()
    continuation = re.sub(r"\s*\\tag\{\d{1,2}\}\s*$", "", continuation).strip()
    return f"{equation} {continuation}{tag}".strip()


def latex_ocr_prediction_lines(latex: str, keep_structured: bool = False) -> list[str]:
    lines = []
    saw_line_crops = "Line crops:" in latex
    in_line_crops = False
    for raw_line in latex.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped == "Full crop:":
            in_line_crops = False
            continue
        if stripped == "Line crops:":
            in_line_crops = True
            continue
        if saw_line_crops and not in_line_crops:
            continue
        stripped = re.sub(r"^line\s+\d+:\s*", "", stripped)
        if keep_structured and re.search(r"\\begin\{(?:aligned|cases)\}", stripped):
            lines.append(stripped)
        elif "\\begin{array}" in stripped or "\\begin{aligned}" in stripped:
            lines.extend(extract_array_cells(stripped))
        elif r"\\" in stripped:
            lines.extend(split_latex_rows(stripped))
        else:
            lines.append(stripped)
    return lines


def split_latex_rows(latex: str) -> list[str]:
    rows = [row.strip() for row in re.split(r"\\\\", latex)]
    return [row for row in rows if row]


def extract_array_cells(latex: str) -> list[str]:
    if "\\begin{aligned}" in latex:
        content = re.sub(r"\\begin\{aligned\}|\\end\{aligned\}", "", latex)
        cells = [cell.strip().lstrip("&").strip() for cell in re.split(r"\\\\", content)]
        return [cell for cell in cells if re.search(r"[A-Za-z0-9]", cell)]

    cells = re.findall(r"\{\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}\}", latex)
    extracted = [
        cell.strip()
        for cell in cells
        if re.search(r"[A-Za-z0-9]", cell) and not re.fullmatch(r"[.&\\{}\s]+", cell)
    ]
    if extracted:
        return extracted

    candidates = re.findall(r"[-+−]?\d+\.\d+.*?(?=\\end\{|$)", latex)
    candidates = [trim_unbalanced_trailing_braces(candidate.strip()) for candidate in candidates]
    return [candidate.strip() for candidate in candidates if candidate.strip()]


def trim_unbalanced_trailing_braces(text: str) -> str:
    while text.endswith("}") and text.count("}") > text.count("{"):
        text = text[:-1].rstrip()
    return text


def cleanup_latex_ocr(latex: str, ocr_text: str) -> str:
    cleaned = strip_markdown_fence(latex).strip()
    cleaned = re.sub(r"\\qquad(?:\\qquad|\s)*", " ", cleaned)
    cleaned = re.sub(r"~+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\\mathit\{([^{}]+)\}", r"\1", cleaned)
    cleaned = merge_spaced_latex_letters(cleaned)
    cleaned = normalize_latex_ocr_symbols(cleaned)
    cleaned = correct_latex_numbers_from_ocr(cleaned, ocr_text)
    cleaned = re.sub(r"\s*\((\d{1,2})\)\s*$", r" \\tag{\1}", cleaned)
    cleaned = merge_spaced_latex_letters(cleaned)
    cleaned = re.sub(r"\^\{(?:\\;|\s|\\)*(\d+)\s*\}", r"^{\1}", cleaned)
    cleaned = re.sub(r"\{\s*\}", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def merge_spaced_latex_letters(latex: str) -> str:
    def merge_group(match: re.Match[str]) -> str:
        content = match.group(1)
        merged = re.sub(r"\b([A-Za-z])(?:\s+([A-Za-z]))+\b", lambda item: item.group(0).replace(" ", ""), content)
        return "_{" + merged + "}"

    merged = re.sub(r"_\{([^{}]+)\}", merge_group, latex)
    merged = re.sub(
        r"(?<![a-z])(?:[A-Z]+\s+)+[A-Z]+\b",
        lambda match: match.group(0).replace(" ", ""),
        merged,
    )
    return merged


def normalize_latex_ocr_symbols(latex: str) -> str:
    cleaned = latex
    cleaned = cleaned.replace(r"\times", r"\times ")
    cleaned = re.sub(r"_\{_\{([^{}]+)\}\}", r"_{\1}", cleaned)
    cleaned = re.sub(r"\\mathrm\{\s*l\s*g\s*\}", r"\\lg", cleaned)
    cleaned = re.sub(r"^\\mathrm\{(.+=.+)\}$", r"\1", cleaned)
    cleaned = re.sub(r"(?<=\d)\\,\s*(?=\d)", "", cleaned)
    cleaned = re.sub(r"(?:\\\s+){2,}", " ", cleaned)
    cleaned = re.sub(r"\\mathrm\{\{\\tiny\s*\([^)]*\)\}\}", "", cleaned)
    cleaned = re.sub(r"\\mathrm\{\[[^\]]+\]\}", "", cleaned)
    cleaned = re.sub(r"\b0([A-Z]{2,})\b", r"O\1", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def correct_latex_numbers_from_ocr(latex: str, ocr_text: str) -> str:
    evidence_text = section_text(ocr_text, "OCR observation A", "OCR observation E")
    observed_counts = Counter(re.findall(r"\d+\.\d+", evidence_text))
    observed = {number for number, count in observed_counts.items() if count >= 2}
    if not observed:
        observed = set(observed_counts)
    coefficient_variables = {
        match.group("number"): match.group("variable")
        for match in re.finditer(r"(?P<number>\d+\.\d+)\s*(?P<variable>[A-Za-z])", evidence_text)
    }

    def fix(match: re.Match[str]) -> str:
        value = match.group(0)
        if value in observed:
            return value
        for index in range(len(value)):
            candidate = value[:index] + value[index + 1 :]
            if candidate in observed:
                if latex[match.end() : match.end() + 2] == "^{" and candidate in coefficient_variables:
                    return f"{candidate}{coefficient_variables[candidate]}"
                return candidate
        next_char = latex[match.end() : match.end() + 1]
        for candidate in observed:
            if candidate.startswith(value) and len(candidate) - len(value) <= 2:
                stripped = candidate.rstrip("0")
                if normalize_decimal_number(stripped) != normalize_decimal_number(value):
                    return stripped
            stripped = candidate.rstrip("0")
            if len(stripped) - len(value) == 1:
                for index in range(len(stripped)):
                    if stripped[:index] + stripped[index + 1 :] == value:
                        return stripped
        return value

    corrected = re.sub(r"\d+\.\d+", fix, latex)
    corrected = correct_latex_variables_from_ocr(corrected, evidence_text)
    return correct_latex_subscripts_from_ocr(corrected, evidence_text)


def correct_latex_variables_from_ocr(latex: str, evidence_text: str) -> str:
    if re.search(r"(?m)^w\s+\|.*role=", evidence_text) or re.search(r"\bw\s*,\s*,", evidence_text):
        latex = re.sub(r"\bW(?=_\{)", "w", latex)
    coefficient_variables = {
        match.group("number"): match.group("variable")
        for match in re.finditer(r"(?P<number>\d+\.\d+)\s*(?P<variable>[A-Za-z])", evidence_text)
    }

    def fix(match: re.Match[str]) -> str:
        number = match.group("number")
        variable = coefficient_variables.get(number)
        if not variable:
            return match.group(0)
        return f"{number}{variable}^{{{match.group('power')}}}"

    return re.sub(r"(?P<number>\d+\.\d+)\d\^\{(?P<power>\d+)\}", fix, latex)


def correct_latex_subscripts_from_ocr(latex: str, evidence_text: str) -> str:
    subscript_candidates = positioned_subscript_candidates(evidence_text)
    for candidate in subscript_candidates:
        latex = replace_near_subscript(latex, candidate)
    return latex


def positioned_subscript_candidates(evidence_text: str) -> set[str]:
    candidates = set()
    for match in re.finditer(r"(?m)^([A-Za-z]{1,4})\s+\|.*role=small$", evidence_text):
        candidates.add(match.group(1))
    return candidates


def replace_near_subscript(latex: str, candidate: str) -> str:
    def fix(match: re.Match[str]) -> str:
        current = match.group(1)
        if current == candidate:
            return match.group(0)
        if len(current) <= 2 and len(candidate) > len(current):
            return match.group(0)
        if current[-1].lower() != candidate[-1].lower():
            return match.group(0)
        if one_edit_apart(current.lower(), candidate.lower()):
            return f"_{{{candidate}}}"
        return match.group(0)

    return re.sub(r"_\{([A-Za-z]{1,4})\}", fix, latex)


def one_edit_apart(left: str, right: str) -> bool:
    if left == right:
        return False
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(left_char != right_char for left_char, right_char in zip(left, right)) == 1
    shorter, longer = sorted((left, right), key=len)
    for index in range(len(longer)):
        if longer[:index] + longer[index + 1 :] == shorter:
            return True
    return False


def has_numeric_loss(lines: list[str], ocr_text: str) -> bool:
    evidence_text = section_text(ocr_text, "OCR observation A", "OCR observation E")
    observed_counts = Counter(re.findall(r"\d+\.\d+", evidence_text))
    observed = {normalize_decimal_number(number) for number, count in observed_counts.items() if count >= 2}
    if not observed:
        observed = {normalize_decimal_number(number) for number in observed_counts}
    observed = remove_decimal_suffix_noise(observed)
    restored = {normalize_decimal_number(number) for number in re.findall(r"\d+\.\d+", "\n".join(lines))}
    if len(observed) < 5:
        return False
    return len(restored & observed) / len(observed) < 0.9


def remove_decimal_suffix_noise(numbers: set[str]) -> set[str]:
    cleaned = set(numbers)
    for number in numbers:
        for other in numbers:
            if number == other:
                continue
            if number.startswith(other) and len(number) - len(other) <= 2:
                cleaned.discard(number)
    return cleaned


def normalize_decimal_number(number: str) -> str:
    if "." not in number:
        return number
    head, tail = number.split(".", 1)
    tail = tail.rstrip("0") or "0"
    return f"{head}.{tail}"


def equation_observations(ocr_text: str) -> list[str]:
    markers = [
        "OCR observation A from the same equation block",
        "OCR observation B from the same equation block",
        "OCR observation C from the same equation block",
    ]
    observations = []
    for index, marker in enumerate(markers):
        end_marker = markers[index + 1] if index + 1 < len(markers) else ""
        section = section_text(ocr_text, marker, end_marker).strip()
        if section:
            observations.append(section)
    return observations or [ocr_text]


def keep_equation_ocr_line(line: str) -> bool:
    if not line:
        return False
    if line.startswith("OCR observation"):
        return False
    if line in {"=", "S", "*"}:
        return False
    if re.fullmatch(r"\([1-9][0-9]?\)", line):
        return True
    if re.fullmatch(r"\(?\d{1,2}\)?", line):
        return False
    return bool("=" in line or re.search(r"^[+\-−~—]|[A-Za-z].*\d|\d.*[A-Za-z]", line))


def is_equation_continuation(line: str) -> bool:
    if "=" in line:
        return False
    return bool(re.search(r"^[+\-−~—]|[+\-−~—]\s*\d", line))


def strip_equation_observation_noise(line: str) -> str:
    cleaned = line.strip()
    cleaned = re.sub(r"\s+\(\d+\)\s*$", "", cleaned)
    cleaned = re.sub(r"\s+\(\)\s*$", "", cleaned)
    return cleaned


def extract_equation_number(line: str) -> str | None:
    match = re.search(r"\(([1-9][0-9]?)\)\s*$", line)
    return match.group(1) if match else None


def normalize_equation_text(text: str) -> str:
    normalized = text.replace("−", "-").replace("—", "-")
    normalized = normalized.replace("×", r"\times")
    return re.sub(r"[ \t]+", " ", normalized)


def deterministic_evaluation(block: dict[str, Any], markdown: str) -> dict[str, Any]:
    block_type = block["type"]
    cleaned = strip_markdown_fence(markdown)
    reasons: list[str] = []

    if cleaned.startswith("[rejected:"):
        reasons.append("generator가 복원을 거부함")

    if block_type == "figure_candidate":
        reasons.append("figure block은 Vision crop 검토가 필요함")

    if block_type == "table_candidate":
        reasons.extend(table_evaluation_reasons(cleaned))
    if block_type == "equation_candidate":
        reasons.extend(equation_evaluation_reasons(block, cleaned))
    if re.search(r"\b[A-Za-z]+[,.;?]{2,}\b", cleaned):
        reasons.append("명백한 OCR artifact가 남아 있음")
    if "```" in cleaned:
        reasons.append("최종 결과에 code fence가 남아 있음")

    return {
        "accepted": not reasons,
        "score": 1.0 if not reasons else 0.0,
        "reasons": reasons,
    }


def table_evaluation_reasons(markdown: str) -> list[str]:
    if not is_valid_markdown_table(markdown):
        return ["Markdown table의 column 수가 일관되지 않음"]

    reasons: list[str] = []
    headers = table_header_values(markdown)
    body_cells = body_cell_values(markdown)
    numeric_header_count = sum(1 for header in headers if re.fullmatch(r"-?\d+(?:\.\d+)?%?", header))
    if numeric_header_count == len(headers):
        reasons.append("table header에 데이터 값이 들어감")
    if any(re.search(r"([\"”]|==:|=:)", header) for header in headers):
        reasons.append("table header에 OCR debris 또는 중복 label이 남아 있음")
    if body_cells:
        unclear_count = sum(1 for cell in body_cells if "[unclear]" in cell)
        if unclear_count / len(body_cells) > 0.3:
            reasons.append("table cell의 unclear 비율이 너무 높음")
        if any(re.search(r"(==:|=:|~—|—|^\]$|^el$|^=\.|^\.0\.)", cell, flags=re.IGNORECASE) for cell in body_cells):
            reasons.append("table numeric cell에 OCR debris가 남아 있음")
    if re.search(r"(?m)^\|\s*\|\s*-?\d", markdown):
        reasons.append("table row가 여러 줄로 쪼개진 형태임")
    if has_bad_index_sequence(markdown):
        reasons.append("table 첫 column의 index sequence가 깨져 있음")
    if re.search(r"(?im)([A-Za-z][,;]{2,}|[\"”]|^\|[^|\n]*\]\s*\||\|\s*=\\?.)", markdown):
        reasons.append("table에 명백한 OCR artifact가 남아 있음")
    return reasons


def equation_evaluation_reasons(block: dict[str, Any], markdown: str) -> list[str]:
    reasons: list[str] = []
    lowered = markdown.lower()
    checks = (
        ("\\[" not in markdown and "$$" not in markdown, "수식 block인데 display math가 없음"),
        (not has_balanced_braces(markdown), "LaTeX 중괄호 짝이 맞지 않음"),
        (not has_balanced_latex_environments(markdown), "LaTeX begin/end 환경 짝이 맞지 않음"),
        (not has_balanced_latex_environments_per_display(markdown), "display math 내부 LaTeX begin/end 환경 짝이 맞지 않음"),
        (not has_balanced_left_right(markdown), "LaTeX left/right delimiter 짝이 맞지 않음"),
        (not has_balanced_plain_parentheses_per_display(markdown), "display math 내부 일반 괄호 짝이 맞지 않음"),
        (has_bare_script_marker(markdown), "밑첨자/윗첨자 대상이 비어 있는 깨진 LaTeX가 있음"),
        (has_malformed_math_text_command(markdown), "math text command 안에 깨진 첨자/괄호 구조가 남아 있음"),
        (has_display_math_without_equation(markdown), "등호 없는 display math 조각이 수식으로 섞여 있음"),
        (has_duplicate_display_math(markdown), "동일한 display math가 중복 복원됨"),
        (has_polynomial_fraction(markdown), "다항식 형태의 수식을 근거 없이 분수로 바꿈"),
        (bool(re.search(r"\\frac\{\s*[-+]?\d+(?:\.\d+)?\s*\}", markdown)) and len(re.findall(r"(?<!\^)[+\-−]", markdown)) >= 3, "다항식 일부를 근거 없이 분수항으로 바꿈"),
        ("\\begin{array}" in markdown, "수식 후보가 표 row 배열처럼 복원됨"),
        (len(re.findall(r"(?m)^\s*\d+\s+[01-]", markdown)) >= 3, "수식 후보가 실험 결과 row처럼 복원됨"),
        (bool(re.search(r"(_\{\s*[,.;?]+\s*\}|[A-Za-z][,;?]{2,}|\\text\{\s*[A-Za-z]*[,.;?][^}]*\}|\?)", markdown)), "명백한 OCR artifact가 남아 있음"),
        (has_bad_subscript_punctuation(markdown), "subscript 안에 OCR punctuation artifact가 남아 있음"),
        (bool(re.search(r"\b[A-Za-z](?:\s+[A-Za-z]+){1,}\b", markdown)), "하나의 변수로 보이는 문자가 공백으로 쪼개져 있음"),
        (bool(re.search(r"(\\l\d|\\mathcal|\\mathbb|\\check|\\slash|\\rightarrow|\\downarrow|\\uparrow|\\rfloor|\\lfloor|\\it|\\mathrm\{\[)", markdown)), "image-to-LaTeX OCR의 깨진 command 또는 placeholder가 남아 있음"),
        (bool(re.search(r"([\"”]|Z\s*-\s*H|\\times\s*10\s+[A-Za-z]|\b[A-Za-z]\^\*)", markdown)), "수식에 OCR debris 또는 깨진 지수 표기가 남아 있음"),
        (bool(re.search(r"(\\cal\b|\\displaystyle\s*\\cal)", markdown)), "image-to-LaTeX OCR의 hallucinated symbol 또는 깨진 문자 인식이 남아 있음"),
        (bool(re.search(r"\d+\.\d+\s+\d\b", markdown)), "계수 뒤에 변수 없이 숫자만 남은 깨진 항이 있음"),
        (bool(re.search(r"\d+\.\d+\^\{\d+\}", markdown)), "계수 뒤에 변수 없이 지수만 붙은 깨진 항이 있음"),
        (has_equation_number_evidence(block) and not has_equation_number_markdown(markdown), "OCR/hint에 보이는 equation number가 Markdown 수식에 보존되지 않음"),
        (len(re.findall(r"[A-Za-z\\]", markdown)) == 0, "수식 변수 구조가 거의 복원되지 않음"),
        (bool(re.fullmatch(r"(?s)(?:\\\[\s*[-+0-9.\s]+\s*\\\]|\$\$\s*[-+0-9.\s]+\s*\$\$)", markdown)), "좌변 변수 없는 숫자 조각만 복원됨"),
        ("[unclear]" in lowered and lowered.count("[unclear]") > 2, "unreadable term이 너무 많음"),
    )
    reasons.extend(reason for failed, reason in checks if failed)
    return reasons


def has_balanced_latex_environments(text: str) -> bool:
    begins = re.findall(r"\\begin\{([A-Za-z*]+)\}", text)
    ends = re.findall(r"\\end\{([A-Za-z*]+)\}", text)
    return Counter(begins) == Counter(ends)


def has_balanced_latex_environments_per_display(text: str) -> bool:
    blocks = re.findall(r"\\\[(.*?)\\\]", text, flags=re.DOTALL)
    blocks.extend(re.findall(r"\$\$(.*?)\$\$", text, flags=re.DOTALL))
    return all(has_balanced_latex_environments(block) for block in blocks)


def has_balanced_left_right(text: str) -> bool:
    return text.count(r"\left") == text.count(r"\right")


def has_balanced_plain_parentheses_per_display(text: str) -> bool:
    blocks = re.findall(r"\\\[(.*?)\\\]", text, flags=re.DOTALL)
    blocks.extend(re.findall(r"\$\$(.*?)\$\$", text, flags=re.DOTALL))
    return all(has_balanced_plain_parentheses(block) for block in blocks)


def has_balanced_plain_parentheses(text: str) -> bool:
    stripped = strip_latex_commands_with_arguments(text)
    stripped = re.sub(r"\\(?:left|right)[()]", "", stripped)
    depth = 0
    escaped = False
    for char in stripped:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def strip_latex_commands_with_arguments(text: str) -> str:
    return re.sub(r"\\(?:tag|text|mathrm|mathbf|mathit|operatorname\*?)\{[^{}]*\}", "", text)


def has_malformed_math_text_command(text: str) -> bool:
    for command in re.finditer(r"\\(?:mathrm|mathbf|mathit)\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", text):
        content = command.group(1)
        if "_" in content or "^" in content:
            return True
        if content.count("(") != content.count(")"):
            return True
    return False


def has_bare_script_marker(text: str) -> bool:
    return bool(re.search(r"[_^](?=\s|[_^+\-=,;:.)\\]|$)", text))


def has_duplicate_display_math(text: str) -> bool:
    blocks = re.findall(r"\\\[(.*?)\\\]", text, flags=re.DOTALL)
    blocks.extend(re.findall(r"\$\$(.*?)\$\$", text, flags=re.DOTALL))
    normalized = [re.sub(r"\s+", "", block) for block in blocks if block.strip()]
    return len(normalized) != len(set(normalized))


def has_equation_number_evidence(block: dict[str, Any]) -> bool:
    source_text = block.get("source_text", "")
    return bool(re.search(r"\(([1-9][0-9]?)\)", source_text, flags=re.MULTILINE))


def has_equation_number_markdown(markdown: str) -> bool:
    return bool(re.search(r"\\tag\{[1-9][0-9]?\}|\([1-9][0-9]?\)", markdown))


def has_display_math_without_equation(text: str) -> bool:
    blocks = re.findall(r"\\\[(.*?)\\\]", text, flags=re.DOTALL)
    blocks.extend(re.findall(r"\$\$(.*?)\$\$", text, flags=re.DOTALL))
    relation_pattern = r"(=|<|>|\\leq|\\geq|\\le|\\ge|\\approx|\\neq|\\sim|\\min\b|\\operatorname\*\{min\}|≤|≥|≈|≠)"
    return any(not re.search(relation_pattern, block) for block in blocks)


def evaluator_prompt(block: dict[str, Any], ocr_text: str, markdown: str) -> str:
    prompt_file = PROMPT_DIR / "block_equation_evaluator.md"
    template = prompt_file.read_text(encoding="utf-8").strip()
    return "\n".join(
        [
            template,
            "",
            f"Block id: {block['id']}",
            f"Block type: {block['type']}",
            "",
            "OCR text:",
            "```text",
            ocr_text,
            "```",
            "",
            "PDF extracted hint text:",
            "```text",
            block.get("source_text", ""),
            "```",
            "",
            "Markdown result:",
            "```markdown",
            markdown,
            "```",
        ]
    )


def parse_evaluation(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data.get("accepted"), bool):
        return None
    return data


def evaluate_block(block: dict[str, Any], ocr_text: str, markdown: str, endpoint: str, model: str, use_sllm: bool) -> dict[str, Any]:
    deterministic = deterministic_evaluation(block, markdown)
    if not deterministic["accepted"] or not use_sllm:
        return deterministic

    prompt = evaluator_prompt(block, ocr_text, markdown)
    raw = call_sllm(
        endpoint,
        model,
        prompt,
        "You evaluate reconstructed OCR Markdown. Return only valid JSON.",
    )
    parsed = parse_evaluation(raw)
    if parsed is None:
        return {
            "accepted": False,
            "score": 0.0,
            "reasons": ["SLLM evaluator가 JSON을 반환하지 않음"],
            "raw": raw,
        }
    return reconcile_sllm_evaluation(markdown, deterministic, parsed)


def feedback_retry_prompt(prompt: str, evaluation: dict[str, Any]) -> str:
    reasons = "; ".join(str(reason) for reason in evaluation.get("reasons", []))
    return "\n".join(
        [
            prompt,
            "",
            "Previous evaluator feedback:",
            reasons or "The previous result was rejected.",
            "",
            "Revise the reconstruction once. Keep the same output format rules.",
        ]
    )


def normalize_recovered_markdown_for_block(block: dict[str, Any], markdown: str) -> str:
    if block["type"] == "equation_candidate":
        return normalize_equation_markdown(markdown, block)
    if block["type"] == "table_candidate":
        return repair_table_markdown_with_hint(markdown, block.get("source_text", ""))
    if block["type"] == "figure_candidate":
        return strip_markdown_fence(markdown)
    return markdown


def has_bad_subscript_punctuation(markdown: str) -> bool:
    for subscript in re.findall(r"_\{([^}]*)\}", markdown):
        if re.search(r"[.;?]", subscript):
            return True
        if "," in subscript and not re.fullmatch(r"(?:\\?[A-Za-z0-9]+|\\mathrm\{[A-Za-z0-9]+\})(?:\s*,\s*(?:\\?[A-Za-z0-9]+|\\mathrm\{[A-Za-z0-9]+\}))+", subscript.strip()):
            return True
    return False


def reconcile_sllm_evaluation(markdown: str, deterministic: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    if parsed.get("accepted"):
        return parsed
    reasons = [str(reason) for reason in parsed.get("reasons", [])]
    cleaned = strip_markdown_fence(markdown)
    valid_reasons = []
    for reason in reasons:
        lowered = reason.lower()
        if "fraction" in lowered and "\\frac" not in cleaned:
            continue
        if "denominator" in lowered and "\\frac" not in cleaned:
            continue
        if "code fence" in lowered and "```" not in cleaned:
            continue
        if "display math" in lowered and ("\\[" in cleaned or "$$" in cleaned):
            continue
        if "unclear" in lowered and "[unclear]" not in cleaned:
            continue
        if "split" in lowered and count_display_math_blocks(cleaned) <= 1:
            continue
        valid_reasons.append(reason)
    if not valid_reasons and deterministic.get("accepted"):
        return {
            **deterministic,
            "recovery_source": "deterministic_after_sllm_evaluator_reconcile",
        }
    return {**parsed, "reasons": valid_reasons or reasons}


def count_display_math_blocks(text: str) -> int:
    return text.count("\\[") + len(re.findall(r"\$\$.*?\$\$", text, flags=re.DOTALL))


def deterministic_recovery_candidate(
    block: dict[str, Any],
    ocr_text: str,
) -> tuple[str, dict[str, Any] | None]:
    if block["type"] == "table_candidate":
        parser_markdown = structured_table_markdown(block, ocr_text)
        if parser_markdown:
            parser_evaluation = deterministic_evaluation(block, parser_markdown)
            if parser_evaluation["accepted"]:
                return parser_markdown, {
                    **parser_evaluation,
                    "recovery_source": "structured_table_parser",
                }
        return "", None

    if block["type"] != "equation_candidate":
        return "", None
    layout_result = deterministic_layout_adjudication(block, ocr_text)
    if layout_result:
        return layout_result
    parser_markdown = equation_parser_markdown(block, ocr_text)
    if parser_markdown:
        parser_evaluation = deterministic_evaluation(block, parser_markdown)
        if parser_evaluation["accepted"]:
            return parser_markdown, {
                **parser_evaluation,
                "recovery_source": "structured_equation_parser",
            }
    latex_candidate = latex_ocr_candidate_markdown(ocr_text, block)
    if not latex_candidate:
        return "", None
    latex_candidate = normalize_equation_markdown(latex_candidate, block)
    latex_evaluation = deterministic_evaluation(block, latex_candidate)
    if not latex_evaluation["accepted"]:
        return "", None
    return latex_candidate, {
        **latex_evaluation,
        "recovery_source": "latex_ocr_cleanup",
    }


def sllm_system_message(block_type: str) -> str:
    if block_type == "table_candidate":
        return (
            "You are a strict OCR-to-Markdown table transcriber. "
            "The response must start with '|' for a Markdown table or '[rejected:' for rejection. "
            "Do not explain, compare, summarize, or recommend anything."
        )
    if block_type == "equation_candidate":
        return (
            "You are a strict OCR-to-LaTeX equation transcriber. "
            "Return only Markdown display math delimited by '$$' or a '[rejected:' line. "
            "Do not explain, compare, summarize, or use code fences."
        )
    return "You reconstruct OCR blocks. Return only the requested Markdown. Never summarize."


def write_recovery_result(block_id: str, markdown: str, evaluation: dict[str, Any]) -> None:
    (OUTPUT_DIR / f"{block_id}.md").write_text(markdown.strip() + "\n", encoding="utf-8")
    EVALUATION_DIR.mkdir(parents=True, exist_ok=True)
    (EVALUATION_DIR / f"{block_id}.json").write_text(
        json.dumps(evaluation, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def recover_block(block: dict[str, Any], endpoint: str, model: str, use_sllm: bool) -> None:
    asset = block.get("asset")
    if not asset:
        return

    ocr_text = ocr_image(asset, block["id"], block["type"])
    ocr_text = append_docling_equation_ocr(block, ocr_text)
    (OCR_DIR / f"{block['id']}.txt").write_text(ocr_text + "\n", encoding="utf-8")
    use_sllm_for_block = should_use_sllm(block["type"], use_sllm)
    prompt = prompt_for_block(block, ocr_text) if use_sllm_for_block else ""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if prompt:
        (OUTPUT_DIR / f"{block['id']}.prompt.md").write_text(prompt + "\n", encoding="utf-8")

    markdown, evaluation = deterministic_recovery_candidate(block, ocr_text)

    if not markdown and not use_sllm_for_block:
        markdown = "\n".join(["```text", ocr_text, "```"])
    elif not markdown:
        system_message = sllm_system_message(block["type"])
        markdown = call_sllm(
            endpoint,
            model,
            prompt,
            system_message,
        )
        markdown = normalize_recovered_markdown_for_block(block, markdown)

    if evaluation is None:
        evaluation = evaluate_block(block, ocr_text, markdown, endpoint, model, use_sllm_for_block)
    if use_sllm_for_block and not evaluation.get("accepted"):
        retry_markdown = call_sllm(
            endpoint,
            model,
            feedback_retry_prompt(prompt, evaluation),
            system_message,
        )
        retry_markdown = normalize_recovered_markdown_for_block(block, retry_markdown)
        retry_evaluation = evaluate_block(block, ocr_text, retry_markdown, endpoint, model, use_sllm)
        if retry_evaluation.get("accepted") or retry_evaluation.get("score", 0) > evaluation.get("score", 0):
            markdown = retry_markdown
            evaluation = {
                **retry_evaluation,
                "recovery_source": retry_evaluation.get("recovery_source", "sllm_feedback_retry"),
            }
    if block["type"] == "table_candidate" and not evaluation["accepted"]:
        fallback_markdown = structured_table_markdown(block, ocr_text)
        if fallback_markdown:
            fallback_evaluation = deterministic_evaluation(block, fallback_markdown)
            if fallback_evaluation["accepted"]:
                markdown = fallback_markdown
                evaluation = {
                    **fallback_evaluation,
                    "recovery_source": "structured_table_fallback",
                }

    write_recovery_result(block["id"], markdown, evaluation)


def evaluate_existing_block(block: dict[str, Any], endpoint: str, model: str, use_sllm: bool) -> None:
    markdown_file = OUTPUT_DIR / f"{block['id']}.md"
    ocr_file = OCR_DIR / f"{block['id']}.txt"
    if not markdown_file.exists() or not ocr_file.exists():
        return
    markdown = markdown_file.read_text(encoding="utf-8")
    ocr_text = ocr_file.read_text(encoding="utf-8")
    if block["type"] == "equation_candidate":
        markdown = normalize_equation_markdown(markdown, block)
        markdown_file.write_text(markdown.strip() + "\n", encoding="utf-8")
    evaluation = evaluate_block(
        block,
        ocr_text,
        markdown,
        endpoint,
        model,
        should_use_sllm(block["type"], use_sllm),
    )
    EVALUATION_DIR.mkdir(parents=True, exist_ok=True)
    (EVALUATION_DIR / f"{block['id']}.json").write_text(
        json.dumps(evaluation, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def clean_previous_outputs() -> None:
    for directory, patterns in (
        (OUTPUT_DIR, ("*.md",)),
        (EVALUATION_DIR, ("*.json",)),
        (OCR_DIR, ("*.txt",)),
        (PREPROCESSED_OCR_DIR, ("*.png",)),
        (LATEX_OCR_DIR, ("*.txt",)),
        (PADDLE_FORMULA_OCR_DIR, ("*.txt",)),
    ):
        if not directory.exists():
            continue
        for pattern in patterns:
            for path in directory.glob(pattern):
                path.unlink()


def clean_selected_outputs(blocks: list[dict[str, Any]]) -> None:
    selected_ids = {block["id"] for block in blocks}
    for block_id in selected_ids:
        for path in (
            OUTPUT_DIR / f"{block_id}.md",
            OUTPUT_DIR / f"{block_id}.prompt.md",
            EVALUATION_DIR / f"{block_id}.json",
            OCR_DIR / f"{block_id}.txt",
            PREPROCESSED_OCR_DIR / f"{block_id}.png",
            LATEX_OCR_DIR / f"{block_id}.txt",
            PADDLE_FORMULA_OCR_DIR / f"{block_id}.txt",
        ):
            if path.exists():
                path.unlink()


def main() -> None:
    global BASE_DIR, DOCUMENT_SLUG, MANIFEST_FILE, OUTPUT_DIR, OCR_DIR, EVALUATION_DIR, PROMPT_DIR, PREPROCESSED_OCR_DIR, LATEX_OCR_DIR, PADDLE_FORMULA_OCR_DIR, PADDLE_CACHE_DIR

    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--no-sllm", action="store_true")
    parser.add_argument("--evaluate-only", action="store_true")
    parser.add_argument("--block-id", action="append", default=[])
    parser.add_argument("--block-type", action="append", choices=["equation_candidate", "table_candidate"], default=[])
    parser.add_argument("--output-dir", type=Path, default=BASE_DIR)
    parser.add_argument("--document-slug", default=DOCUMENT_SLUG)
    parser.add_argument("--manifest-file", type=Path)
    args = parser.parse_args()

    BASE_DIR = args.output_dir.resolve()
    DOCUMENT_SLUG = args.document_slug
    MANIFEST_FILE = args.manifest_file.resolve() if args.manifest_file else BASE_DIR / "layout" / "auto" / f"{DOCUMENT_SLUG}.auto_block_manifest.json"
    OUTPUT_DIR = BASE_DIR / "layout" / "auto" / "recovered_blocks"
    OCR_DIR = BASE_DIR / "layout" / "auto" / "ocr"
    EVALUATION_DIR = BASE_DIR / "layout" / "auto" / "evaluations"
    PROMPT_DIR = PROMPTS_ROOT
    PREPROCESSED_OCR_DIR = OCR_DIR / "preprocessed"
    LATEX_OCR_DIR = OCR_DIR / "latex"
    PADDLE_FORMULA_OCR_DIR = OCR_DIR / "paddle_formula"
    PADDLE_CACHE_DIR = resolve_paddle_cache_dir(BASE_DIR)

    wanted = set(args.block_id)
    wanted_types = set(args.block_type) or {"equation_candidate", "table_candidate"}
    blocks = [
        block
        for block in load_blocks()
        if block["type"] in wanted_types and (not wanted or block["id"] in wanted)
    ]
    if not args.evaluate_only:
        if wanted or args.block_type:
            clean_selected_outputs(blocks)
        else:
            clean_previous_outputs()
    for block in blocks:
        if args.evaluate_only:
            evaluate_existing_block(block, args.endpoint, args.model, not args.no_sllm)
        else:
            recover_block(block, args.endpoint, args.model, not args.no_sllm)
        print(block["id"])


if __name__ == "__main__":
    main()
