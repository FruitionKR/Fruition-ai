"""MCP 외부 AI 변환: 기존 AnyDoc 전처리 결과로 기존 복원 요청을 창으로 만들고, 기존 검증·조립을 거치는지 본다."""

import argparse
import json
import tempfile
from pathlib import Path
from unittest import mock

import fitz
import pytest

from app.modules.document_restoration.infrastructure import crop_first_with_anydoc as crop_first
from app.modules.document_restoration.infrastructure import external_restoration as external


def _prepared(root: Path, *, body: str, table_line: str = "REGION\t0\t1\ttable\t0.9\t72\t200\t400\t300"):
    pdf_file = root / "batch.pdf"
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "본문 원문")
        page.insert_text((80, 250), "a b c")
        document.save(pdf_file)
    output_dir = root / "out"
    manifest = output_dir / "layout" / "auto" / "batch.crop_first_manifest.json"
    detected = output_dir / "final" / "batch.detected.md"

    def fake_anydoc(command, input_file, output_file, log_file):
        output_file.write_text(body, encoding="utf-8")
        log_file.write_text("ok", encoding="utf-8")
        return 0

    args = argparse.Namespace(pdf_file=pdf_file, manifest_file=manifest, detected_markdown=detected,
                              output_dir=output_dir, anydoc_command="anydoc", heron_command="heron",
                              heron_model=None, pdfium_library=None, body_ai_budget=0.3)
    regions = crop_first.parse_heron_regions(table_line)
    with mock.patch.object(crop_first, "run_heron", return_value=(regions, "")), \
            mock.patch.object(crop_first, "run_anydoc", side_effect=fake_anydoc):
        crop_first.prepare(args)
    return pdf_file, output_dir, manifest, detected


def test_windows_carry_existing_repair_inputs_and_assembly_uses_only_accepted_results():
    with tempfile.TemporaryDirectory() as directory:
        pdf, out, manifest, detected = _prepared(Path(directory), body="본문 원문\n\nXQ001QX\n")
        windows = external.build_windows(pdf, out, manifest, detected, describe_figures=False)

        table = next(window for window in windows if window["lane"] == "table")
        assert table["result_block_ids"] == ["heron_table_p001_001"]
        assert table["payload"]["blocks"][0]["required_tokens"] == []
        assert table["instructions"] == external.lane_prompt("table")
        # 기존 요청처럼 쪽 전체 이미지와 crop을 함께 준다. 이미지는 묶음 안 PNG 파일이다.
        assert len(table["images"]) == 2
        assert all((out / image).read_bytes().startswith(b"\x89PNG") for image in table["images"])
        assert json.loads((out / external.WINDOWS_FILE).read_text(encoding="utf-8")) == windows

        blocks, fragments = external.load_context(manifest, detected)
        bad = {"results": [{"block_id": "heron_table_p001_001", "action": "replace", "replacement": "표 아님"}]}
        assert external.validate_result(table, bad, blocks, fragments) == ["heron_table_p001_001"]
        with pytest.raises(ValueError):
            external.validate_result(table, {"results": [{"block_id": "other", "action": "keep", "replacement": ""}]},
                                     blocks, fragments)

        good = {"results": [{"block_id": "heron_table_p001_001", "action": "replace",
                             "replacement": "| a | b |\n| --- | --- |\n| 1 | 2 |"}]}
        assert external.validate_result(table, good, blocks, fragments) == []
        markdown, summary = external.assemble_external(out, manifest, detected, [(table, good)])
        assert "| a | b |" in markdown and "XQ001QX" not in markdown
        assert summary["rejected_block_ids"] == []


def test_rejected_result_falls_back_visibly_instead_of_counting_as_repaired():
    with tempfile.TemporaryDirectory() as directory:
        pdf, out, manifest, detected = _prepared(Path(directory), body="본문 원문\n\nXQ001QX\n")
        table = next(w for w in external.build_windows(pdf, out, manifest, detected, False) if w["lane"] == "table")
        bad = {"results": [{"block_id": "heron_table_p001_001", "action": "replace", "replacement": "표 아님"}]}

        markdown, summary = external.assemble_external(out, manifest, detected, [(table, bad)])

        assert "표 자동 복원 실패" in markdown and "표 아님" not in markdown
        assert summary["rejected_block_ids"] == ["heron_table_p001_001"]


def test_rejected_block_gets_a_single_block_followup_window_like_existing_retry():
    with tempfile.TemporaryDirectory() as directory:
        pdf, out, manifest, detected = _prepared(Path(directory), body="본문 원문\n\nXQ001QX\n")
        table = next(w for w in external.build_windows(pdf, out, manifest, detected, False) if w["lane"] == "table")
        block = "heron_table_p001_001"
        blocks, fragments = external.load_context(manifest, detected)
        bad = {"results": [{"block_id": block, "action": "replace", "replacement": "표 아님"}]}
        good = {"results": [{"block_id": block, "action": "replace",
                             "replacement": "| a | b |\n| --- | --- |\n| 1 | 2 |"}]}

        # 기존 복원처럼 받아들인 블록은 다시 묻지 않고, 떨어진 블록만 한 블록 요청으로 한 번 더 묻는다.
        assert external.followup_block_ids(table, good, blocks, fragments) == []
        assert external.followup_block_ids(table, bad, blocks, fragments) == [block]
        followup = external.followup_window(table, block)
        assert followup["result_block_ids"] == [block] and followup["chain_block_ids"] == []
        assert [item["block_id"] for item in followup["payload"]["blocks"]] == [block]
        assert followup["instructions"] == table["instructions"]
        # 같은 쪽 이미지는 한 번만 저장한다.
        assert followup["images"] == table["images"]
        # 재시도 창은 더 묻지 않는다.
        assert external.followup_block_ids(followup, bad, blocks, fragments) == []

        markdown, summary = external.assemble_external(out, manifest, detected, [(table, bad), (followup, good)])
        assert "| a | b |" in markdown and "표 자동 복원 실패" not in markdown
        assert summary["rejected_block_ids"] == []
