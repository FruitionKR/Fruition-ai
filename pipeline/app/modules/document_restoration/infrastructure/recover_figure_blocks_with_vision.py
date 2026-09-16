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
from app.modules.document_restoration.domain.markdown_text import strip_markdown_fence
from app.modules.document_restoration.domain.text_quality import (
    looks_glyph_encoded as generic_looks_glyph_encoded,
)


BASE_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PROMPT_DIR = Path(__file__).resolve().parents[4] / "prompts" / "document_restoration"
DOCUMENT_SLUG = "document"

MANIFEST_FILE = BASE_DIR / "layout" / "auto" / f"{DOCUMENT_SLUG}.auto_block_manifest.json"
OUTPUT_DIR = BASE_DIR / "layout" / "auto" / "recovered_blocks"
OCR_DIR = BASE_DIR / "layout" / "auto" / "ocr"
EVALUATION_DIR = BASE_DIR / "layout" / "auto" / "evaluations"
PROMPT_DIR = SCRIPT_PROMPT_DIR
DEFAULT_ENDPOINT = "http://127.0.0.1:11434/v1/chat/completions"
DEFAULT_MODEL = "qwen2.5vl:7b"
def load_blocks() -> list[dict[str, Any]]:
    blocks = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    return sorted(blocks, key=lambda block: (block["page"], block["order"]))


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def looks_glyph_encoded_candidate(text: str) -> bool:
    return generic_looks_glyph_encoded(text)


def accepted_existing_recovery(block_id: str) -> bool:
    evaluation_file = EVALUATION_DIR / f"{block_id}.json"
    recovered_file = OUTPUT_DIR / f"{block_id}.md"
    if not evaluation_file.exists() or not recovered_file.exists():
        return False
    evaluation = json.loads(evaluation_file.read_text(encoding="utf-8"))
    if not evaluation.get("accepted"):
        return False
    recovered = recovered_file.read_text(encoding="utf-8")
    return bool(normalize_text(recovered)) and not looks_glyph_encoded_candidate(recovered)


def read_optional_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def sllm_candidate_for_block(block_id: str) -> str:
    return read_optional_text(OUTPUT_DIR / f"{block_id}.md")


def ocr_text_for_block(block_id: str) -> str:
    return read_optional_text(OCR_DIR / f"{block_id}.txt")


def needs_vision_recovery(block: dict[str, Any]) -> bool:
    if block["type"] != "figure_candidate":
        return False
    if accepted_existing_recovery(block["id"]):
        return False
    caption = normalize_text(block.get("source_text", ""))
    if not block.get("caption_expected", bool(caption)):
        return False
    if block.get("text_decision") == "needs_text_adjudication":
        return True
    if looks_glyph_encoded_candidate(caption):
        return True
    return not caption and bool(block.get("caption_asset"))


def candidate_image_files(block: dict[str, Any]) -> list[Path]:
    block_id = block["id"]
    files = []
    caption_asset = block.get("caption_asset")
    if caption_asset:
        files.append(BASE_DIR / caption_asset)
    files.append(BASE_DIR / "layout" / "auto" / "assets" / "text_ocr" / f"{block_id}.png")
    asset = block.get("asset")
    if asset:
        files.append(BASE_DIR / asset)
    result = []
    seen = set()
    for file in files:
        resolved = file.resolve()
        if resolved in seen or not file.exists():
            continue
        seen.add(resolved)
        result.append(file)
    return result


def call_vision(endpoint: str, model: str, prompt: str, image_file: Path) -> str:
    image_b64 = base64.b64encode(image_file.read_bytes()).decode("ascii")
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 1024,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
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


def prompt_for_block(block: dict[str, Any], image_file: Path, feedback: str = "") -> str:
    caption_only = image_file.parent.name == "figure_captions"
    prompt_name = "block_figure_caption_vision.md" if caption_only else "block_figure_vision.md"
    task = (PROMPT_DIR / prompt_name).read_text(encoding="utf-8").strip()
    source_text = block.get("source_text", "")
    ocr_text = "" if caption_only else ocr_text_for_block(block["id"])
    sllm_candidate = "" if caption_only else sllm_candidate_for_block(block["id"])
    parts = [
            task,
            "",
            f"Block id: {block['id']}",
            f"Block type: {block['type']}",
            f"Image file: {image_file.name}",
            "",
            "PDF extracted hint text:",
            "```text",
            clip_evidence(source_text),
            "```",
            "",
            "OCR text from the same crop:",
            "```text",
            clip_evidence(ocr_text),
            "```",
            "",
            "SLLM Markdown candidate to verify against the image:",
            "```markdown",
            clip_evidence(sllm_candidate),
            "```",
        ]
    if feedback:
        parts.extend(
            [
                "",
                "Previous format validation feedback:",
                feedback,
                "",
                "Return a corrected transcription using only visible image evidence.",
            ]
        )
    return "\n".join(parts)


def evaluate_vision_markdown(markdown: str, image_file: Path) -> dict[str, Any]:
    text = strip_markdown_fence(markdown).strip()
    reasons = []
    if not text:
        reasons.append("vision 결과가 비어 있음")
    if text.startswith("[rejected:"):
        reasons.append("vision 모델이 crop을 읽지 못함")
    if looks_glyph_encoded_candidate(text):
        reasons.append("glyph-encoded caption이 남아 있음")
    if "```" in text:
        reasons.append("최종 결과에 code fence가 남아 있음")
    if re.search(r"\b(?:This figure|The figure|shows|illustrates)\b", text, flags=re.IGNORECASE):
        reasons.append("caption 전사가 아니라 설명문 형태로 요약됨")
    if re.search(r"^\s*Figure:\s+", text, flags=re.MULTILINE):
        reasons.append("보이는 figure caption 전사가 아니라 일반 설명형 제목을 생성함")
    if image_file.parent.name == "figures" and re.search(r"^Figure\s+\d+\s*:", text, flags=re.IGNORECASE):
        reasons.append("caption crop 없이 full figure에서 설명형 caption을 생성함")
    if image_file.parent.name == "figures" and re.search(r"^\s*\|.*\|\s*$", text, flags=re.MULTILINE):
        reasons.append("그래프/그림의 시각적 위치를 표 데이터로 추정함")
    figure_text_body = re.sub(r"(?is)^.*Figure text:\s*", "", text).strip()
    if "Figure text:" in text and figure_text_body and not re.search(r"[A-Za-z]{3,}", figure_text_body):
        reasons.append("figure text가 축 tick 숫자만 남아 의미 있는 텍스트 전사가 아님")
    if "Figure caption:" in text and len(normalize_text(text.replace("Figure caption:", ""))) < 8:
        reasons.append("caption 내용이 사실상 비어 있음")
    if not re.search(r"\bfig(?:ure)?\.?\s*\d+\b", text, flags=re.IGNORECASE) and "Figure text:" not in text:
        reasons.append("figure 번호/caption 또는 내부 텍스트가 확인되지 않음")
    return {
        "accepted": not reasons,
        "score": 1.0 if not reasons else 0.0,
        "reasons": reasons,
        "recovery_source": "vision_crop",
    }


def recover_block(
    block: dict[str, Any],
    endpoint: str,
    model: str,
    dry_run: bool,
    max_attempts: int,
) -> dict[str, Any]:
    attempts = []
    best_markdown = ""
    best_evaluation = {
        "accepted": False,
        "score": 0.0,
        "reasons": ["사용 가능한 figure/caption crop이 없음"],
        "recovery_source": "vision_crop",
    }
    for image_file in candidate_image_files(block):
        caption_only = image_file.parent.name == "figure_captions"
        sllm_candidate = "" if caption_only else sllm_candidate_for_block(block["id"])
        ocr_text = "" if caption_only else ocr_text_for_block(block["id"])
        feedback = ""
        for attempt in range(1, max_attempts + 1):
            prompt = prompt_for_block(block, image_file, feedback)
            try:
                raw = call_vision(endpoint, model, prompt, image_file)
            except RuntimeError as exc:
                evaluation = {
                    "accepted": False,
                    "score": 0.0,
                    "reasons": [str(exc)],
                    "recovery_source": "vision_crop",
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
            markdown = strip_markdown_fence(raw)
            evaluation = evaluate_vision_markdown(markdown, image_file)
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
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        EVALUATION_DIR.mkdir(parents=True, exist_ok=True)
        (OUTPUT_DIR / f"{block['id']}.md").write_text(best_markdown.strip() + "\n", encoding="utf-8")
        (EVALUATION_DIR / f"{block['id']}.json").write_text(
            json.dumps(best_evaluation, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return best_evaluation


def main() -> None:
    global BASE_DIR, DOCUMENT_SLUG, MANIFEST_FILE, OUTPUT_DIR, OCR_DIR, EVALUATION_DIR, PROMPT_DIR

    parser = argparse.ArgumentParser()
    parser.add_argument("--base-dir", type=Path, default=BASE_DIR)
    parser.add_argument("--document-slug", default=DOCUMENT_SLUG)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--manifest-file", type=Path)
    parser.add_argument("--prompt-dir", type=Path, default=SCRIPT_PROMPT_DIR)
    parser.add_argument("--block-id", action="append", default=[])
    parser.add_argument("--all-figures", action="store_true")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    BASE_DIR = args.base_dir.resolve()
    DOCUMENT_SLUG = args.document_slug
    MANIFEST_FILE = args.manifest_file.resolve() if args.manifest_file else BASE_DIR / "layout" / "auto" / f"{DOCUMENT_SLUG}.auto_block_manifest.json"
    OUTPUT_DIR = BASE_DIR / "layout" / "auto" / "recovered_blocks"
    OCR_DIR = BASE_DIR / "layout" / "auto" / "ocr"
    EVALUATION_DIR = BASE_DIR / "layout" / "auto" / "evaluations"
    PROMPT_DIR = args.prompt_dir.resolve()

    requested_ids = set(args.block_id)
    targets = []
    for block in load_blocks():
        if block["type"] != "figure_candidate":
            continue
        if requested_ids and block["id"] not in requested_ids:
            continue
        if args.all_figures or requested_ids or needs_vision_recovery(block):
            targets.append(block)

    accepted = 0
    rejected = 0
    for block in targets:
        evaluation = recover_block(block, args.endpoint, args.model, args.dry_run, args.max_attempts)
        if evaluation.get("accepted"):
            accepted += 1
            status = "accepted"
        else:
            rejected += 1
            status = "rejected"
        reasons = "; ".join(str(reason) for reason in evaluation.get("reasons", []))
        print(f"{block['id']}: {status} {reasons}")

    print(f"targets={len(targets)} accepted={accepted} rejected={rejected}")


if __name__ == "__main__":
    main()
