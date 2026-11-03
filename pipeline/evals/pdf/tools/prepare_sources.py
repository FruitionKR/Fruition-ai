"""원본 PDF에서 평가용 발췌본(sources/<id>.pdf)을 만든다. 원본 PDF는 저장소에 넣지 않는다.

사용: python prepare_sources.py <원본 폴더>
- 원본 폴더에는 cases.json의 source_url에서 받은 원본을 `<id>.pdf`로 둔다(같은 원본을 쓰는 사례는 같은 파일을 복사한다).
- 원본 sha256(original_sha256)이 다르면 그 사례는 건너뛴다. 법령·첨부처럼 원본이 바뀌었을 수 있다.
- original_pages 쪽만 잘라 저장하고, derivation.rasterize가 있으면 글자 정보 없는 흑백 JPEG 이미지 PDF로 만든다.
- 만든 파일의 내용 기준 sha256(run_pdf.digest: 쪽별 글자와 이미지)이 gold.lock.json과 다르면 알린다.
"""
import hashlib
import json
import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).parent))
from run_pdf import digest  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parents[1]


def main() -> None:
    originals = Path(sys.argv[1])
    cases = json.loads((EVAL_DIR / "cases.json").read_text())
    locked = json.loads((EVAL_DIR / "gold.lock.json").read_text())["files"]
    out_dir = EVAL_DIR / "sources"
    out_dir.mkdir(exist_ok=True)
    for case in cases:
        original = originals / f"{case['id']}.pdf"
        if not original.exists() or hashlib.sha256(original.read_bytes()).hexdigest() != case["original_sha256"]:
            print(case["id"], "원본 없음 또는 sha256 다름: 건너뜀")
            continue
        start, end = map(int, case["original_pages"].split("-"))
        doc, out = fitz.open(original), fitz.open()
        raster = (case.get("derivation") or {}).get("rasterize")
        for index in range(start - 1, end):
            page = doc[index]
            if raster:
                pix = page.get_pixmap(dpi=raster["dpi"], colorspace=fitz.csGRAY)
                new = out.new_page(width=page.rect.width, height=page.rect.height)
                new.insert_image(new.rect, stream=pix.tobytes("jpeg", jpg_quality=raster["jpeg_quality"]))
            else:
                out.insert_pdf(doc, from_page=index, to_page=index)
        target = out_dir / case["file"]
        out.save(target, garbage=4, deflate=True)
        same = digest(target) == locked.get(f"sources/{case['file']}")
        print(case["id"], "만듦", "고정한 내용과 같음" if same else "고정한 내용과 다름")


if __name__ == "__main__":
    main()
