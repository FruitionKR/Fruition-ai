import base64
import json
import logging
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
from collections import OrderedDict
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import unquote, urlsplit

from app.core.llm_env import DEFAULT_LLM_MODELS, resolve_llm_selection
from app.core.pipeline_control import (
    DeadlineExceededError,
    PipelineRunCancelledError,
    remaining_seconds,
    request_deadline,
)
from app.modules.model_usage.infrastructure.usage_ledger import SCOPE_ENV


logger = logging.getLogger("converter")

PDF_DIAGNOSTIC_TIMEOUT_SECONDS = int(os.getenv("PDF_DIAGNOSTIC_TIMEOUT_SECONDS", "60"))
# 한 번의 변환 시도(쪽 범위 하나)의 제한 시간이다. 넘기면 범위를 절반으로 나눠 다시 시도한다(ADR 0034).
ATTEMPT_TIMEOUT_SECONDS = int(os.getenv("CONVERT_ATTEMPT_TIMEOUT_SECONDS", "1500"))
RESTORATION_COMMAND = os.getenv("RESTORATION_COMMAND", "document-restoration")


class InputRejectedError(ValueError):
    """암호가 걸렸거나 손상된 PDF처럼 입력 때문에 실패한 변환이다. 범위를 나눠도 같으므로 재시도하지 않는다."""


class ConversionFailedError(RuntimeError):
    """변환 하위 프로세스가 비정상 종료했다. 범위를 나눠 다시 시도할 수 있다."""


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


def _run_command(command: list[str], working_dir: Path, timeout_seconds: int,
                 stdout: Any, cancelled: Event | None, env: dict[str, str] | None = None) -> int:
    if cancelled is not None and cancelled.is_set():
        raise PipelineRunCancelledError("Conversion cancelled")
    # 프로세스 그룹 전체를 종료해야 변환기가 만든 자식 프로세스도 임시 파일 쓰기를 멈춘다.
    with subprocess.Popen(command, cwd=working_dir, stdout=stdout, stderr=subprocess.STDOUT,
                          text=True, start_new_session=True, env=env) as process:
        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                if cancelled is not None and cancelled.is_set():
                    raise PipelineRunCancelledError("Conversion cancelled")
                if time.monotonic() >= deadline:
                    raise DeadlineExceededError(f"Command timeout: {command[0]}")
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


def _within_deadline(timeout_seconds: float) -> float:
    """요청 마감이 있으면 단계별 timeout을 남은 시간으로 줄이고, 이미 지났으면 단계를 시작하지 않는다."""
    remaining = remaining_seconds()
    if remaining is None:
        return timeout_seconds
    if remaining <= 0:
        raise DeadlineExceededError("Conversion deadline exceeded")
    return min(timeout_seconds, remaining)


def run_to_file(command: list[str], output_file: Path, working_dir: Path,
                timeout_seconds: int, log_file: Path, cancelled: Event | None = None) -> None:
    with output_file.open("w", encoding="utf-8") as stdout, log_file.open("a", encoding="utf-8") as log:
        log.write(f"$ {' '.join(command)}\n")
        code = _run_command(command, working_dir, timeout_seconds, stdout, cancelled)
        log.write(f"exit={code}\n\n")
        if code != 0:
            raise ConversionFailedError(f"Command failed: {command[0]}")


def run(command: list[str], working_dir: Path, timeout_seconds: int,
        log_file: Path, cancelled: Event | None = None, env: dict[str, str] | None = None) -> None:
    with log_file.open("a", encoding="utf-8") as log:
        log.write(f"$ {' '.join(command)}\n")
        log.flush()
        code = _run_command(command, working_dir, timeout_seconds, log, cancelled, env)
        log.write(f"\nexit={code}\n\n")
        if code != 0:
            raise ConversionFailedError(f"Command failed: {command[0]}")


def process_pdf(
    content: bytes | Path,
    provider: str = "gemini",
    model: str = DEFAULT_LLM_MODELS["gemini"],
    cancelled: Event | None = None,
    usage_actor: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    try:
        provider, model = resolve_llm_selection(provider, model)
    except ValueError as exc:
        raise InputRejectedError(str(exc)) from exc
    if missing := missing_commands():
        raise RuntimeError(f"Missing converter commands: {', '.join(missing)}")

    # 하위 프로세스의 귀속 경고는 요청별 process.log에만 남으므로 운영 로그에도 남긴다.
    if not all((usage_actor or {}).get(key) for key in ("run_id", "workspace_id", "user_id")):
        logger.warning("사용자 귀속 없는 변환 요청: usage_actor=%s", usage_actor)
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
            _within_deadline(PDF_DIAGNOSTIC_TIMEOUT_SECONDS),
            process_log,
            cancelled,
        )
        run_to_file(
            ["pdffonts", input_pdf.name],
            fonts_txt,
            job_dir,
            _within_deadline(PDF_DIAGNOSTIC_TIMEOUT_SECONDS),
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
            _within_deadline(ATTEMPT_TIMEOUT_SECONDS),
            process_log,
            cancelled,
            # selective repair 손자 프로세스까지 상속돼 공급사 호출이 이 actor로 기록된다.
            {**os.environ, SCOPE_ENV: json.dumps({**(usage_actor or {}), "kind": "document_conversion"})},
        )

        if cancelled is not None and cancelled.is_set():
            raise PipelineRunCancelledError("Conversion cancelled")
        return {
            "markdown": embed_local_image_links(
                output_md.read_text(encoding="utf-8"), output_md, output_dir
            ),
            "pdfinfo": info_txt.read_text(encoding="utf-8", errors="replace"),
            "pdffonts": fonts_txt.read_text(encoding="utf-8", errors="replace"),
            "process_log": process_log.read_text(encoding="utf-8", errors="replace"),
            "repair_summary": json.loads(repair_summary.read_text(encoding="utf-8")),
        }




class StorageRangeReader:
    """PDF의 seek/read를 object storage 범위 읽기로 바꾼다. 캐시는 파일 크기와 무관하게 16MiB이다."""

    def __init__(self, client: Any, bucket: str, key: str, size: int, cancelled: Event):
        self.client, self.bucket, self.key, self.size, self.cancelled = client, bucket, key, size, cancelled
        self.position = 0
        self.cache: OrderedDict[int, bytes] = OrderedDict()
        self.block_size = 1024 * 1024

    def seek(self, offset, whence=0):
        target = offset if whence == 0 else self.position + offset if whence == 1 else self.size + offset
        if target < 0 or whence not in (0, 1, 2):
            raise ValueError("Invalid PDF seek")
        self.position = target
        return target

    def tell(self):
        return self.position

    def fetch_range(self, first: int, last: int) -> bytes:
        response = self.client.get_object(self.bucket, self.key, offset=first, length=last - first + 1)
        try:
            block = response.read()
        finally:
            response.close()
            response.release_conn()
        if len(block) != last - first + 1:
            logger.warning("PDF storage range incomplete: first=%d last=%d received=%d", first, last, len(block))
            raise ConversionFailedError(
                f"Incomplete PDF storage range (expected {last - first + 1} bytes, received {len(block)})")
        return block

    def read(self, size=-1):
        size = max(0, min(self.size - self.position, self.size if size < 0 else size))
        # 파일 전체가 아닌 PDF 단일 object의 파서 메모리 한도. 손상된 xref의 전체 파일 재탐색을 막는다.
        if size > 64 * 1024 * 1024:
            raise InputRejectedError("A PDF object exceeds the parser working-memory limit")
        output = bytearray()
        while size:
            if self.cancelled.is_set():
                raise PipelineRunCancelledError("Conversion cancelled")
            index = self.position // self.block_size
            if index not in self.cache:
                first = index * self.block_size
                last = min(first + self.block_size, self.size) - 1
                self.cache[index] = self.fetch_range(first, last)
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


def open_pdf(stream: StorageRangeReader):
    """PDF를 열어 (reader, 전체 쪽수)를 돌려준다. 열 수 없거나 암호가 걸렸으면 InputRejectedError이다."""
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError
    try:
        reader = PdfReader(stream, strict=True)
        if reader.is_encrypted:
            raise InputRejectedError("Password-protected PDFs must be unlocked before conversion")
        total = len(reader.pages)
    except PyPdfError as error:
        raise InputRejectedError(f"Invalid PDF: {type(error).__name__}") from error
    if total == 0:
        raise InputRejectedError("PDF contains no pages")
    return reader, total


def convert_pages(reader: Any, start: int, end: int, provider: str, model: str, cancelled: Event,
                  usage_actor: dict[str, str | None] | None = None) -> str:
    """[start, end) 쪽(0부터)을 Markdown으로 변환한다.

    시도가 실패하거나 ATTEMPT_TIMEOUT_SECONDS를 넘기면 범위를 절반으로 나눠 각각 다시 시도하고, 1쪽까지
    실패하면 그 예외를 올린다. 입력 거절·취소·전체 마감은 나눠도 같으므로 나누지 않는다.
    """
    from pypdf import PdfWriter
    if cancelled.is_set():
        raise PipelineRunCancelledError("Conversion cancelled")
    try:
        with request_deadline(ATTEMPT_TIMEOUT_SECONDS), \
                tempfile.TemporaryDirectory(prefix="fruition-pdf-range-") as directory:
            pdf = Path(directory) / "batch.pdf"
            writer = PdfWriter()
            for index in range(start, end):
                writer.add_page(reader.pages[index])
            with pdf.open("wb") as output:
                writer.write(output)
            writer.close()
            return process_pdf(pdf, provider, model, cancelled, usage_actor)["markdown"]
    except Exception as error:
        # 시도 마감은 위 with를 벗어났으므로 여기서 보이는 마감은 호출자(batch 전체) 것이다.
        remaining = remaining_seconds()
        if remaining is not None and remaining <= 0:
            raise DeadlineExceededError("Conversion deadline exceeded") from error
        if (isinstance(error, InputRejectedError) or cancelled.is_set() or end - start == 1
                or (isinstance(error, PipelineRunCancelledError) and not isinstance(error, DeadlineExceededError))):
            raise
        middle = start + (end - start) // 2
        logger.warning("변환 시도 실패, 범위를 나눠 재시도: pages=%d-%d error_type=%s", start, end, type(error).__name__)
    first = convert_pages(reader, start, middle, provider, model, cancelled, usage_actor)
    second = convert_pages(reader, middle, end, provider, model, cancelled, usage_actor)
    return "\n\n".join(part for part in (first.rstrip(), second) if part)
