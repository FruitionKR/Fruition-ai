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
    content: bytes | Path,
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

        if isinstance(content, Path):
            shutil.copyfile(content, input_pdf)
        else:
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


class S3RangeReader:
    """PDF의 seek/read를 S3 Range GET으로 변환한다. 캐시는 파일 크기와 무관하게 16MiB이다."""
    def __init__(self, url: str, size: int, cancelled: Event):
        from collections import OrderedDict
        from urllib.request import build_opener, HTTPRedirectHandler
        parsed = urlsplit(url)
        allowed = set(filter(None, os.getenv("CONVERTER_SOURCE_HOSTS", "").split(",")))
        if parsed.scheme != "https" or parsed.hostname not in allowed or parsed.username or parsed.password:
            raise ValueError("PDF source must use an approved HTTPS storage host")
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.opener = build_opener(NoRedirect())
        self.url, self.size, self.cancelled = url, size, cancelled
        self.position = 0
        self.cache = OrderedDict()
        self.block_size = 1024 * 1024

    def seek(self, offset, whence=0):
        target = offset if whence == 0 else self.position + offset if whence == 1 else self.size + offset
        if target < 0 or whence not in (0, 1, 2):
            raise ValueError("Invalid PDF seek")
        self.position = target
        return target

    def tell(self):
        return self.position

    def read(self, size=-1):
        from urllib.request import Request as HttpRequest
        size = max(0, min(self.size - self.position, self.size if size < 0 else size))
        # 파일 전체가 아닌 PDF 단일 object의 파서 메모리 한도. 손상된 xref의 전체 파일 재탐색을 막는다.
        if size > 64 * 1024 * 1024:
            raise ValueError("A PDF object exceeds the parser working-memory limit")
        output = bytearray()
        while size:
            if self.cancelled.is_set():
                raise HTTPException(status_code=499, detail="Conversion cancelled")
            index = self.position // self.block_size
            if index not in self.cache:
                first = index * self.block_size
                last = min(first + self.block_size, self.size) - 1
                try:
                    with self.opener.open(HttpRequest(self.url, headers={"Range": f"bytes={first}-{last}"}), timeout=30) as response:
                        if response.status != 206 or response.headers.get("Content-Range") != f"bytes {first}-{last}/{self.size}":
                            raise ValueError("Storage must return the exact requested PDF byte range")
                        block = response.read(self.block_size + 1)
                except Exception as error:
                    # 서명 URL에는 임시 credentials가 들어 있으므로 로그·응답에 원문을 노출하지 않는다.
                    raise ValueError("Could not read the PDF storage range") from None
                if len(block) != last - first + 1:
                    raise ValueError("Incomplete PDF storage range")
                self.cache[index] = block
                if len(self.cache) > 16:
                    self.cache.popitem(last=False)
            self.cache.move_to_end(index)
            offset = self.position % self.block_size
            value = self.cache[index][offset:offset + size]
            output.extend(value)
            size -= len(value)
            self.position += len(value)
        return bytes(output)

    def close(self):
        self.cache.clear()

    def readable(self): return True
    def seekable(self): return True


def process_source_batch(source_url: str, byte_size: int, start_page: int,
                         provider: str, model: str, cancelled: Event):
    from pypdf import PdfReader, PdfWriter
    stream = S3RangeReader(source_url, byte_size, cancelled)
    try:
        reader = PdfReader(stream, strict=True)
        if reader.is_encrypted:
            raise ValueError("Password-protected PDFs must be unlocked before conversion")
        total = len(reader.pages)
        if total == 0:
            raise ValueError("PDF contains no pages")
        if start_page < 0 or start_page > total:
            raise ValueError("Invalid conversion checkpoint")
        if start_page == total:
            return {"page_start": start_page + 1, "page_end": total, "total_pages": total, "markdown": "", "done": True}
        count = max(1, min(50, int(os.getenv("PDF_PAGES_PER_BATCH", "10"))))
        end = min(start_page + count, total)
        with tempfile.TemporaryDirectory(prefix="fruition-pdf-range-") as directory:
            pdf = Path(directory) / "batch.pdf"
            writer = PdfWriter()
            for index in range(start_page, end):
                writer.add_page(reader.pages[index])
            with pdf.open("wb") as output:
                writer.write(output)
            writer.close()
            result = process_pdf(pdf, provider, model, cancelled)
        return {"page_start": start_page + 1, "page_end": end, "total_pages": total,
                "markdown": result["markdown"], "done": end == total}
    finally:
        stream.close()


from pydantic import BaseModel, Field

class SourceBatchRequest(BaseModel):
    source_url: str
    byte_size: int = Field(gt=0)
    start_page: int = Field(ge=0, default=0)
    provider: str = "gemini"
    model: str = "gemini-3.1-flash-lite"


_source_conversion_slots = asyncio.Semaphore(max(1, int(os.getenv("PDF_BATCH_CONCURRENCY", "1"))))

@app.post("/convert-source-batch")
async def convert_source_batch(body: SourceBatchRequest, request: Request):
    async with _source_conversion_slots:
        return await _convert_source_batch(body, request)

async def _convert_source_batch(body: SourceBatchRequest, request: Request):
    cancelled = Event()
    worker = asyncio.create_task(asyncio.to_thread(process_source_batch, body.source_url,
        body.byte_size, body.start_page, body.provider, body.model, cancelled))
    try:
        while not worker.done():
            if await request.is_disconnected(): cancelled.set()
            await asyncio.wait({worker}, timeout=0.1)
        return worker.result()
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        cancelled.set()
        if not worker.done():
            try: await asyncio.shield(worker)
            except Exception: pass
