import unittest

from app.modules.markdown_edit.domain.entities import MarkdownEditRequest, MarkdownEditTarget
from app.modules.markdown_edit.infrastructure.markdown_source_range import (
    apply_source_range_response,
    build_source_range_plan,
)


TARGET = MarkdownEditTarget(type="whole_document", start_line=1, end_line=30)


class MarkdownSourceRangeTest(unittest.TestCase):
    def test_edits_text_without_serializing_markdown_structure(self) -> None:
        source = (
            "---\ntitle: 운영 점검\n---\n\n"
            "# 배포 가이드\n\n"
            "자세한 내용은 [설치 문서](https://example.com/install)를 **확인한다**.[^1]\n\n"
            "```bash\nnpm install\n```\n\n"
            "| 환경 | 상태 |\n| --- | --- |\n| prod | ready |\n\n"
            "[^1]: 공식 문서"
        )
        request = MarkdownEditRequest(
            instruction="문장만 자연스럽게 다듬고 Markdown 구조는 유지해줘.",
            markdown=source,
            target=TARGET,
            edit_goal="cleanup",
        )

        plan = build_source_range_plan(request)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertNotIn("배포 가이드", {segment.text for segment in plan.segments})
        segment = next(segment for segment in plan.segments if segment.text == "확인한다")
        edited, failures = apply_source_range_response(
            plan,
            [{"id": segment.id, "replacement": "확인하세요"}],
        )
        self.assertEqual(failures, [])
        self.assertEqual(edited, source.replace("확인한다", "확인하세요"))
        self.assertIn("[설치 문서](https://example.com/install)", edited)
        self.assertIn("```bash\nnpm install\n```", edited)
        self.assertIn("| prod | ready |", edited)
        self.assertIn("[^1]: 공식 문서", edited)

    def test_rejects_markdown_structure_in_text_replacement(self) -> None:
        request = MarkdownEditRequest(
            instruction="문장을 자연스럽게 다듬어줘.",
            markdown="배포 전에 테스트한다.",
            target=TARGET,
            edit_goal="style_change",
        )
        plan = build_source_range_plan(request)
        assert plan is not None

        _, failures = apply_source_range_response(
            plan,
            [{"id": plan.segments[0].id, "replacement": "**배포 전에 테스트한다.**"}],
        )

        self.assertIn("source range edits must not change Markdown structure", failures)

    def test_locks_ascii_literal_inside_korean_sentence(self) -> None:
        request = MarkdownEditRequest(
            instruction="문장을 자연스럽게 다듬어줘.",
            markdown="배포를 하기 전 smoke test가 필요하다.",
            target=TARGET,
            edit_goal="cleanup",
        )

        plan = build_source_range_plan(request)

        assert plan is not None
        self.assertNotIn("smoke test", {segment.text for segment in plan.segments})
        self.assertIn("smoke test", plan.masked_markdown)

    def test_locks_empty_crlf_task_markers_in_nested_lists(self) -> None:
        source = "Intro\r\n- [ ]\r\n  * [x]\r\n- [X]\r\nOutro\r\n"
        request = MarkdownEditRequest(
            instruction="Polish the sentences.",
            markdown=source,
            target=TARGET,
            edit_goal="cleanup",
        )

        plan = build_source_range_plan(request)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual([segment.text for segment in plan.segments], ["Intro", "Outro"])
        self.assertEqual(
            plan.masked_markdown,
            "{{FRUITION_TEXT_0001}}\r\n- [ ]\r\n  * [x]\r\n- [X]\r\n{{FRUITION_TEXT_0002}}\r\n",
        )

    def test_keeps_lf_task_markers_locked(self) -> None:
        source = "Intro\n- [ ]\n  * [x]\n- [X]\nOutro\n"
        request = MarkdownEditRequest(
            instruction="Polish the sentences.",
            markdown=source,
            target=TARGET,
            edit_goal="cleanup",
        )

        plan = build_source_range_plan(request)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual([segment.text for segment in plan.segments], ["Intro", "Outro"])

    def test_rejects_unknown_or_duplicate_segment_ids(self) -> None:
        request = MarkdownEditRequest(
            instruction="문장을 자연스럽게 다듬어줘.",
            markdown="배포 전에 테스트한다.",
            target=TARGET,
            edit_goal="cleanup",
        )
        plan = build_source_range_plan(request)
        assert plan is not None
        segment_id = plan.segments[0].id

        _, failures = apply_source_range_response(
            plan,
            [
                {"id": segment_id, "replacement": "배포 전 테스트한다."},
                {"id": segment_id, "replacement": "배포 전에 시험한다."},
                {"id": "text-9999", "replacement": "알 수 없는 범위"},
            ],
        )

        self.assertIn(f"duplicate source range segment id: {segment_id}", failures)
        self.assertIn("unknown source range segment id: text-9999", failures)

    def test_skips_source_range_path_for_structure_conversion(self) -> None:
        request = MarkdownEditRequest(
            instruction="회귀 테스트는 굵게 표시해줘.",
            markdown="회귀 테스트가 중요하다.",
            target=TARGET,
            edit_goal="style_change",
        )

        self.assertIsNone(build_source_range_plan(request))

    def test_falls_back_when_parser_text_cannot_map_to_original_source(self) -> None:
        request = MarkdownEditRequest(
            instruction="문장을 자연스럽게 다듬어줘.",
            markdown="AT&amp;T 서비스를 확인한다.",
            target=TARGET,
            edit_goal="cleanup",
        )

        self.assertIsNone(build_source_range_plan(request))

    def test_translate_unlocks_visible_text_and_locks_markdown_structure(self) -> None:
        source = (
            "---\ntitle: guide\n---\n\n"
            "# Deploy guide\n\n"
            "Read the [install guide](https://example.com/install).[^1]\n\n"
            "```bash\nnpm install\n```\n\n"
            "| Environment | Status |\n| --- | --- |\n| Production | Ready |\n\n"
            "[^1]: Official documentation"
        )
        request = MarkdownEditRequest(
            instruction="한국어로 번역해줘.",
            markdown=source,
            target=TARGET,
            edit_goal="translate",
        )

        plan = build_source_range_plan(request)

        assert plan is not None
        texts = {segment.text for segment in plan.segments}
        self.assertIn("Deploy guide", texts)
        self.assertIn("install guide", texts)
        self.assertIn("Environment", texts)
        self.assertIn("Official documentation", texts)
        self.assertNotIn("https://example.com/install", texts)
        self.assertNotIn("npm install", texts)
        self.assertIn("https://example.com/install", plan.masked_markdown)
        self.assertIn("```bash\nnpm install\n```", plan.masked_markdown)
        self.assertEqual(set(plan.required_segment_ids), {segment.id for segment in plan.segments})

        _, missing_failures = apply_source_range_response(plan, [])
        self.assertIn("required source range segment is missing: text-0001", missing_failures)

        _, url_failures = apply_source_range_response(
            plan,
            [{"id": plan.segments[0].id, "replacement": "https://example.com/translated"}],
        )
        self.assertIn("source range replacement must not contain a URL: text-0001", url_failures)


if __name__ == "__main__":
    unittest.main()
