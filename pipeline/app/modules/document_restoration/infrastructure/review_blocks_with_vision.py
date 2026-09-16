from __future__ import annotations

import argparse
import base64
import json
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from app.modules.document_restoration.domain.evidence_text import clip_evidence
from app.modules.document_restoration.domain.markdown_text import (
    has_balanced_braces,
    strip_markdown_fence,
)
from app.modules.document_restoration.domain.text_quality import (
    looks_glyph_encoded as generic_looks_glyph_encoded,
)


BASE_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PROMPT_DIR = Path(__file__).resolve().parents[4] / "prompts" / "document_restoration"
DOCUMENT_SLUG = "document"

MANIFEST_FILE = BASE_DIR / "layout" / "auto" / f"{DOCUMENT_SLUG}.docling_primary_manifest.json"
PROMPT_DIR = SCRIPT_PROMPT_DIR
DEFAULT_ENDPOINT = "http://127.0.0.1:11434/v1/chat/completions"
DEFAULT_MODEL = "qwen2.5vl:7b"
def load_blocks() -> list[dict[str, Any]]:
    blocks = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    return sorted(blocks, key=lambda block: (block["page"], block["order"]))


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def is_rejected_result(text: str) -> bool:
    return re.match(r"^\[?\s*rejected\s*:", text.strip(), flags=re.IGNORECASE) is not None


def looks_glyph_encoded(text: str) -> bool:
    return generic_looks_glyph_encoded(text)


def output_paths(block: dict[str, Any]) -> tuple[Path, Path, Path]:
    block_id = block["id"]
    if block["type"] in {"paragraph", "heading"}:
        recovered = BASE_DIR / "layout" / "auto" / "text_recovered" / f"{block_id}.md"
        evaluation = BASE_DIR / "layout" / "auto" / "text_evaluations" / f"{block_id}.json"
        ocr = BASE_DIR / "layout" / "auto" / "text_ocr" / f"{block_id}.txt"
        return recovered, evaluation, ocr
    recovered = BASE_DIR / "layout" / "auto" / "recovered_blocks" / f"{block_id}.md"
    evaluation = BASE_DIR / "layout" / "auto" / "evaluations" / f"{block_id}.json"
    ocr = BASE_DIR / "layout" / "auto" / "ocr" / f"{block_id}.txt"
    return recovered, evaluation, ocr


def read_optional(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def candidate_image_files(block: dict[str, Any]) -> list[Path]:
    block_id = block["id"]
    files = []
    if block["type"] in {"paragraph", "heading"}:
        files.append(BASE_DIR / "layout" / "auto" / "assets" / "text_ocr" / f"{block_id}.png")
    else:
        asset = block.get("asset")
        if asset:
            files.append(BASE_DIR / asset)
    result = []
    seen = set()
    for file in files:
        if not file.exists():
            continue
        resolved = file.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(file)
    return result


def prompt_file_for_type(block_type: str) -> Path:
    if block_type in {"paragraph", "heading"}:
        return PROMPT_DIR / "block_text_vision.md"
    if block_type == "table_candidate":
        return PROMPT_DIR / "block_table_vision.md"
    if block_type == "equation_candidate":
        return PROMPT_DIR / "block_equation_vision.md"
    raise ValueError(f"unsupported block type: {block_type}")


def prompt_for_block(block: dict[str, Any], image_file: Path, ocr_text: str, sllm_candidate: str, feedback: str = "") -> str:
    task = prompt_file_for_type(block["type"]).read_text(encoding="utf-8").strip()
    parts = [
        task,
        "",
        f"Block id: {block['id']}",
        f"Block type: {block['type']}",
        f"Image file: {image_file.name}",
        "",
        "PDF extracted source/hint text:",
        "```text",
        clip_evidence(block.get("source_text", "")),
        "```",
        "",
        "OCR text from the same crop:",
        "```text",
        clip_evidence(ocr_text),
        "```",
        "",
        "SLLM candidate to verify against the image:",
        "```markdown",
        clip_evidence(sllm_candidate),
        "```",
    ]
    if feedback:
        parts.extend(
            [
                "",
                "Previous evaluator feedback:",
                feedback,
                "",
                "Revise once using only visible image evidence. If the correction is not clearly supported, reject.",
            ]
        )
    return "\n".join(parts)


def call_vision(endpoint: str, model: str, prompt: str, image_file: Path) -> str:
    image_b64 = base64.b64encode(image_file.read_bytes()).decode("ascii")
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 1536,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                ],
            }
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
        raise RuntimeError(f"Vision endpoint request failed: {exc}") from exc
    return data["choices"][0]["message"]["content"].strip()


def valid_markdown_table(text: str) -> bool:
    rows = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
    if len(rows) < 2:
        return False
    counts = {row.count("|") for row in rows}
    return len(counts) == 1


def balanced_plain_parentheses(text: str) -> bool:
    probe = re.sub(r"\\(?:left|right)?[()]", "", text)
    probe = re.sub(r"\\[A-Za-z]+\{[^{}]*\}", "", probe)
    depth = 0
    for char in probe:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def normalize_vision_result(block: dict[str, Any], markdown: str) -> str:
    normalized = strip_markdown_fence(markdown)
    if block["type"] == "equation_candidate":
        normalized = re.sub(r"\s*\\tag\{(?!\d+\})[^}]+\}", "", normalized)
        if normalized and not is_rejected_result(normalized) and "$$" not in normalized:
            normalized = f"$$\n{normalized}\n$$"
    return normalized.strip()


def evaluate_result(block: dict[str, Any], markdown: str) -> dict[str, Any]:
    block_type = block["type"]
    text = strip_markdown_fence(markdown).strip()
    reasons = []
    if not text:
        reasons.append("vision 결과가 비어 있음")
    if is_rejected_result(text):
        reasons.append("vision 모델이 crop 검토를 거부함")
    if "```" in text:
        reasons.append("code fence가 남아 있음")
    if block_type in {"paragraph", "heading"} and looks_glyph_encoded(text):
        reasons.append("glyph-encoded 텍스트가 남아 있음")

    if block_type in {"paragraph", "heading"}:
        if "\n" in text.strip():
            reasons.append("본문/제목 결과가 한 줄 형식이 아님")
        if block_type == "heading" and len(text.split()) > 14:
            reasons.append("heading으로 보기에는 너무 김")
        if block_type == "paragraph" and len(text) < 8:
            reasons.append("paragraph 복원 결과가 너무 짧음")
    elif block_type == "table_candidate":
        if not valid_markdown_table(text):
            reasons.append("유효한 Markdown table이 아님")
        if re.search(r"\b(?:approximately|likely|unknown|unclear)\b", text, flags=re.IGNORECASE):
            reasons.append("표 값에 추정/불확실 표현이 남아 있음")
    elif block_type == "equation_candidate":
        if "$$" not in text:
            reasons.append("display math delimiter가 없음")
        if text.count("$$") % 2 != 0:
            reasons.append("display math delimiter 균형이 맞지 않음")
        if not has_balanced_braces(text):
            reasons.append("LaTeX brace 균형이 맞지 않음")
        if not balanced_plain_parentheses(text):
            reasons.append("display math 내부 일반 괄호 짝이 맞지 않음")
        if re.search(r"\\mathrm\{[^{}]*[_^][^{}]*\}|\\mathrm\{[^{}]*[()][^{}]*\}", text):
            reasons.append("math text command 안에 깨진 첨자/괄호 구조가 남아 있음")
        if re.search(r"\b(?:approximately|likely|maybe)\b", text, flags=re.IGNORECASE):
            reasons.append("수식에 추정 표현이 포함됨")
        if re.search(r"\\tag\{(?!\d+\})[^}]+\}", text):
            reasons.append("수식 번호 placeholder가 남아 있음")
    return {
        "accepted": not reasons,
        "score": 1.0 if not reasons else 0.0,
        "reasons": reasons,
        "recovery_source": "vision_review",
    }


def existing_needs_review(block: dict[str, Any]) -> bool:
    recovered_file, evaluation_file, _ = output_paths(block)
    if block["type"] in {"paragraph", "heading"}:
        source_text = normalize_text(block.get("source_text", ""))
        if block.get("text_decision") == "needs_text_adjudication":
            return True
        if recovered_file.exists():
            recovered = recovered_file.read_text(encoding="utf-8")
            return looks_glyph_encoded(recovered)
        return looks_glyph_encoded(source_text)
    if not recovered_file.exists() or not evaluation_file.exists():
        return True
    evaluation = json.loads(evaluation_file.read_text(encoding="utf-8"))
    recovered = recovered_file.read_text(encoding="utf-8")
    if not evaluation.get("accepted"):
        return True
    return looks_glyph_encoded(recovered)


def review_block(block: dict[str, Any], endpoint: str, model: str, dry_run: bool, max_attempts: int) -> dict[str, Any]:
    recovered_file, evaluation_file, ocr_file = output_paths(block)
    sllm_candidate = read_optional(recovered_file)
    ocr_text = read_optional(ocr_file)
    attempts = []
    best_markdown = ""
    best_evaluation = {
        "accepted": False,
        "score": 0.0,
        "reasons": ["사용 가능한 crop 이미지가 없음"],
        "recovery_source": "vision_review",
    }
    for image_file in candidate_image_files(block):
        feedback = ""
        for attempt in range(1, max_attempts + 1):
            prompt = prompt_for_block(block, image_file, ocr_text, sllm_candidate, feedback)
            try:
                raw = call_vision(endpoint, model, prompt, image_file)
            except RuntimeError as exc:
                evaluation = {
                    "accepted": False,
                    "score": 0.0,
                    "reasons": [str(exc)],
                    "recovery_source": "vision_review",
                    "image_file": str(image_file.relative_to(BASE_DIR)),
                    "attempt": attempt,
                }
                attempts.append(
                    {
                        "image_file": evaluation["image_file"],
                        "attempt": attempt,
                        "ocr_text": ocr_text,
                        "sllm_candidate": sllm_candidate,
                        "markdown": "",
                        "evaluation": evaluation,
                    }
                )
                best_evaluation = evaluation
                break
            markdown = normalize_vision_result(block, raw)
            evaluation = evaluate_result(block, markdown)
            evaluation["image_file"] = str(image_file.relative_to(BASE_DIR))
            evaluation["attempt"] = attempt
            attempts.append(
                {
                    "image_file": evaluation["image_file"],
                    "attempt": attempt,
                    "ocr_text": ocr_text,
                    "sllm_candidate": sllm_candidate,
                    "markdown": markdown,
                    "evaluation": evaluation,
                }
            )
            if len(attempts) == 1 or evaluation["accepted"] or evaluation["score"] > best_evaluation.get("score", 0.0):
                best_markdown = markdown
                best_evaluation = evaluation
            if evaluation["accepted"]:
                break
            feedback = "; ".join(str(reason) for reason in evaluation.get("reasons", []))
        if best_evaluation.get("accepted"):
            break

    best_evaluation = {**best_evaluation, "vision_attempts": attempts}
    if not dry_run:
        recovered_file.parent.mkdir(parents=True, exist_ok=True)
        evaluation_file.parent.mkdir(parents=True, exist_ok=True)
        recovered_file.write_text(best_markdown.strip() + "\n", encoding="utf-8")
        evaluation_file.write_text(json.dumps(best_evaluation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return best_evaluation


def main() -> None:
    global BASE_DIR, DOCUMENT_SLUG, MANIFEST_FILE, PROMPT_DIR

    parser = argparse.ArgumentParser()
    parser.add_argument("--base-dir", type=Path, default=BASE_DIR)
    parser.add_argument("--document-slug", default=DOCUMENT_SLUG)
    parser.add_argument("--manifest-file", type=Path)
    parser.add_argument("--prompt-dir", type=Path, default=SCRIPT_PROMPT_DIR)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--block-type", action="append", choices=["paragraph", "heading", "table_candidate", "equation_candidate"], default=[])
    parser.add_argument("--block-id", action="append", default=[])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    BASE_DIR = args.base_dir.resolve()
    DOCUMENT_SLUG = args.document_slug
    MANIFEST_FILE = args.manifest_file.resolve() if args.manifest_file else BASE_DIR / "layout" / "auto" / f"{DOCUMENT_SLUG}.docling_primary_manifest.json"
    PROMPT_DIR = args.prompt_dir.resolve()

    wanted_ids = set(args.block_id)
    wanted_types = set(args.block_type) or {"paragraph", "heading", "table_candidate", "equation_candidate"}
    targets = []
    for block in load_blocks():
        if block["type"] not in wanted_types:
            continue
        if wanted_ids and block["id"] not in wanted_ids:
            continue
        if args.all or wanted_ids or existing_needs_review(block):
            targets.append(block)

    accepted = 0
    rejected = 0
    for block in targets:
        evaluation = review_block(block, args.endpoint, args.model, args.dry_run, args.max_attempts)
        status = "accepted" if evaluation.get("accepted") else "rejected"
        accepted += int(status == "accepted")
        rejected += int(status == "rejected")
        reasons = "; ".join(str(reason) for reason in evaluation.get("reasons", []))
        print(f"{block['id']}: {status} {reasons}")
    print(f"targets={len(targets)} accepted={accepted} rejected={rejected}")


if __name__ == "__main__":
    main()
