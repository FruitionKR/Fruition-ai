"""PDF 비용 특징을 격리된 하위 프로세스에서 센다.

신뢰할 수 없는 PDF를 API 프로세스 밖에서 파싱하려고 `python -I`로 이 파일만 실행한다. app 패키지를 import하지 않는다.
사용법: pdf_cost_features.py <path> <max_pages> <cpu_seconds> <memory_bytes>
성공하면 stdout에 JSON 한 줄을 쓰고, 읽을 수 없는 PDF면 종료 코드 3으로 끝난다.
"""
import json
import resource
import sys

UNREADABLE_EXIT = 3


def limit_resources(cpu_seconds: int, memory_bytes: int) -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    try:
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    except (ValueError, OSError):
        # macOS는 RLIMIT_AS를 강제하지 않는다. 부모의 실행 시간 제한은 그대로 적용된다.
        pass


def pdf_features(path: str, max_pages: int) -> dict[str, int]:
    """앞쪽 max_pages쪽만 읽고, 나머지 쪽은 읽은 쪽의 비율로 외삽한다."""
    import pymupdf

    with pymupdf.open(path) as pdf:
        if pdf.needs_pass or not len(pdf):
            raise ValueError("PDF is encrypted or empty")
        pages = len(pdf)
        inspected = min(pages, max_pages)
        text_bytes = images = scanned_pages = 0
        for index in range(inspected):
            page = pdf[index]
            text = page.get_text()
            text_bytes += len(text.encode("utf-8"))
            images += len(page.get_images())
            scanned_pages += int(len(text.strip()) < 40)

    def scale(value: int) -> int:
        return -(-value * pages // inspected)

    return dict(pages=pages, text_bytes=scale(text_bytes), images=scale(images),
                scanned_pages=min(pages, scale(scanned_pages)))


if __name__ == "__main__":
    source, max_pages, cpu_seconds, memory_bytes = sys.argv[1], *map(int, sys.argv[2:5])
    limit_resources(cpu_seconds, memory_bytes)
    try:
        features = pdf_features(source, max_pages)
    except Exception:  # 파서가 던지는 모든 오류는 "읽을 수 없는 PDF"다. 원문은 남기지 않는다.
        sys.exit(UNREADABLE_EXIT)
    print(json.dumps(features))
