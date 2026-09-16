import asyncio
import base64
import json
import mimetypes
import os
import re
import shlex
import shutil
import signal
import time
import subprocess
import tempfile
import uuid
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import unquote, urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile

from app.core.llm_env import resolve_llm_selection


app = FastAPI(title="Fruition PDF Converter")

RESTORATION_TIMEOUT_SECONDS = int(os.getenv("RESTORATION_TIMEOUT_SECONDS", "900"))
PDF_DIAGNOSTIC_TIMEOUT_SECONDS = int(os.getenv("PDF_DIAGNOSTIC_TIMEOUT_SECONDS", "60"))
RESTORATION_COMMAND = os.getenv("RESTORATION_COMMAND", "document-restoration")


def max_upload_bytes() -> int:
    return int(os.getenv("MAX_UPLOAD_MB", "50")) * 1024 * 1024


def required_commands() -> list[str]:
    return ["pdfinfo", "pdffonts", shlex.split(RESTORATION_COMMAND)[0]]


def missing_commands() -> list[str]:
    return [command for command in required_commands() if shutil.which(command) is None]


def embed_local_image_links(markdown: str, markdown_file: Path, output_dir: Path) -> str:
    pattern = re.compile(
        r"(?P<prefix>!?\[[^\]]*\]\()(?P<target><[^>]+>|[^)\s]+)(?P<suffix>[^)]*\))"
    )
    output_root = output_dir.resolve()

    def replace(match: re.Match[str]) -> str:
        target = match.group("target")
        target = target[1:-1] if target.startswith("<") else target
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            return match.group(0)
        asset = (markdown_file.parent / unquote(parsed.path)).resolve()
        try:
            asset.relative_to(output_root)
        except ValueError:
            return match.group(0)
        mime_type = mimetypes.guess_type(asset.name)[0]
        if not asset.is_file() or not mime_type or not mime_type.startswith("image/"):
            return match.group(0)
        encoded = base64.b64encode(asset.read_bytes()).decode("ascii")
        return f"{match.group('prefix')}data:{mime_type};base64,{encoded}{match.group('suffix')}"

    return pattern.sub(replace, markdown)


@app.get("/health")
def health() -> dict[str, Any]:
    missing = missing_commands()
    return {
        "status": "ok" if not missing else "degraded",
        "missing_commands": missing,
    }


def _run_command(command: list[str], working_dir: Path, timeout_seconds: int,
                 stdout: Any, cancelled: Event | None) -> int:
    if cancelled is not None and cancelled.is_set():
        raise HTTPException(status_code=499, detail="Conversion cancelled")
    # 프로세스 그룹 전체를 종료해야 변환기가 만든 자식 프로세스도 임시 파일 쓰기를 멈춘다.
    with subprocess.Popen(command, cwd=working_dir, stdout=stdout, stderr=subprocess.STDOUT,
                          text=True, start_new_session=True) as process:
        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                if cancelled is not None and cancelled.is_set():
                    raise HTTPException(status_code=499, detail="Conversion cancelled")
                if time.monotonic() >= deadline:
                    raise HTTPException(status_code=504, detail=f"Command timeout: {command[0]}")
                try:
                    return process.wait(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    continue
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def run_to_file(command: list[str], output_file: Path, working_dir: Path,
                timeout_seconds: int, log_file: Path, cancelled: Event | None = None) -> None:
    with output_file.open("w", encoding="utf-8") as stdout, log_file.open("a", encoding="utf-8") as log:
        log.write(f"$ {' '.join(command)}\n")
        code = _run_command(command, working_dir, timeout_seconds, stdout, cancelled)
        log.write(f"exit={code}\n\n")
        if code != 0:
            raise HTTPException(status_code=422, detail=f"Command failed: {command[0]}")


def run(command: list[str], working_dir: Path, timeout_seconds: int,
        log_file: Path, cancelled: Event | None = None) -> None:
    with log_file.open("a", encoding="utf-8") as log:
        log.write(f"$ {' '.join(command)}\n")
        log.flush()
        code = _run_command(command, working_dir, timeout_seconds, log, cancelled)
        log.write(f"\nexit={code}\n\n")
        if code != 0:
            raise HTTPException(status_code=422, detail=f"Command failed: {command[0]}")


def process_pdf(
    content: bytes,
    provider: str = "gemini",
    model: str = "gemini-3.1-flash-lite",
    cancelled: Event | None = None,
) -> dict[str, Any]:
    try:
        provider, model = resolve_llm_selection(provider, model)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if missing := missing_commands():
        raise HTTPException(
            status_code=503,
            detail=f"Missing converter commands: {', '.join(missing)}",
        )

    with tempfile.TemporaryDirectory(prefix="fruition-pdf-") as temp_dir:
        job_dir = Path(temp_dir) / str(uuid.uuid4())
        job_dir.mkdir(mode=0o700)

        input_pdf = job_dir / "input.pdf"
        info_txt = job_dir / "info.txt"
        fonts_txt = job_dir / "fonts.txt"
        output_dir = job_dir / "restoration"
        output_md = output_dir / "final" / f"{job_dir.name}.restored.md"
        repair_summary = output_dir / "final" / "selective_repair_summary.json"
        process_log = job_dir / "process.log"

        input_pdf.write_bytes(content)

        run_to_file(
            ["pdfinfo", input_pdf.name],
            info_txt,
            job_dir,
            PDF_DIAGNOSTIC_TIMEOUT_SECONDS,
            process_log,
            cancelled,
        )
        run_to_file(
            ["pdffonts", input_pdf.name],
            fonts_txt,
            job_dir,
            PDF_DIAGNOSTIC_TIMEOUT_SECONDS,
            process_log,
            cancelled,
        )
        run(
            [
                *shlex.split(RESTORATION_COMMAND),
                "--pdf-file",
                str(input_pdf),
                "--output-dir",
                str(output_dir),
                "--document-slug",
                job_dir.name,
                "--mode",
                "crop-first",
                "--selective-provider",
                provider,
                "--selective-model",
                model,
            ],
            job_dir,
            RESTORATION_TIMEOUT_SECONDS,
            process_log,
            cancelled,
        )

        if cancelled is not None and cancelled.is_set():
            raise HTTPException(status_code=499, detail="Conversion cancelled")
        return {
            "markdown": embed_local_image_links(
                output_md.read_text(encoding="utf-8"), output_md, output_dir
            ),
            "pdfinfo": info_txt.read_text(encoding="utf-8", errors="replace"),
            "pdffonts": fonts_txt.read_text(encoding="utf-8", errors="replace"),
            "process_log": process_log.read_text(encoding="utf-8", errors="replace"),
            "repair_summary": json.loads(repair_summary.read_text(encoding="utf-8")),
        }


@app.post("/convert")
async def convert(
    request: Request,
    file: UploadFile = File(...),
    provider: str = Form("gemini"),
    model: str = Form("gemini-3.1-flash-lite"),
) -> dict[str, Any]:
    content = await file.read()
    if len(content) > max_upload_bytes():
        raise HTTPException(status_code=413, detail="File is too large")

    suffix = Path(file.filename or "").suffix.lower()
    if suffix != ".pdf" and file.content_type != "application/pdf":
        raise HTTPException(status_code=415, detail="Only PDF files are supported in the MVP")

    cancelled = Event()
    worker = asyncio.create_task(asyncio.to_thread(process_pdf, content, provider, model, cancelled))
    try:
        while not worker.done():
            if await request.is_disconnected():
                cancelled.set()
            await asyncio.wait({worker}, timeout=0.1)
        result = worker.result()
    finally:
        cancelled.set()
        # 취소 응답 전에 프로세스 종료와 TemporaryDirectory 정리를 기다린다.
        if not worker.done():
            try:
                await asyncio.shield(worker)
            except HTTPException:
                pass

    return {
        "filename": file.filename or "document.pdf",
        "content_type": file.content_type or "application/pdf",
        **result,
    }
