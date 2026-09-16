import unittest

from app.modules.markdown_edit.domain.entities import MarkdownEditRequest, MarkdownEditTarget
from app.modules.markdown_edit.domain.markdown_output_contract import (
    protect_markdown,
    repair_markdown_output,
    validate_markdown_output,
)


TARGET = MarkdownEditTarget(type="whole_document", start_line=1, end_line=20)


class MarkdownOutputContractTest(unittest.TestCase):
    def test_rejects_insert_after_output_that_repeats_current_section_heading(self) -> None:
        request = MarkdownEditRequest(
            instruction="이 섹션 아래에 문제 해결 절을 추가해줘.",
            markdown="## 설치\n\n설치 방법입니다.",
            target=MarkdownEditTarget(type="current_section", start_line=1, end_line=3),
            edit_goal="other",
            edit_operation="insert_after",
        )

        failures = validate_markdown_output(
            request,
            "추가 내용을 안내합니다.\n\n## 설치\n\n설치 방법입니다.\n\n## 문제 해결",
        )

        self.assertIn("insert_after output must not repeat the current section heading", failures)

    def test_allows_current_section_heading_inside_fenced_code_example(self) -> None:
        request = MarkdownEditRequest(
            instruction="이 섹션 아래에 Markdown 예시를 추가해줘.",
            markdown="## 설치\n\n설치 방법입니다.",
            target=MarkdownEditTarget(type="current_section", start_line=1, end_line=3),
            edit_goal="other",
            edit_operation="insert_after",
        )

        failures = validate_markdown_output(request, "```markdown\n## 설치\n```")

        self.assertNotIn("insert_after output must not repeat the current section heading", failures)

    def test_does_not_close_fence_when_fence_like_content_has_info_string(self) -> None:
        request = MarkdownEditRequest(
            instruction="이 섹션 아래에 Markdown 예시를 추가해줘.",
            markdown="## 설치\n\n설치 방법입니다.",
            target=MarkdownEditTarget(type="current_section", start_line=1, end_line=3),
            edit_goal="other",
            edit_operation="insert_after",
        )

        failures = validate_markdown_output(request, "```text\n```python\n## 설치\n```")

        self.assertNotIn("insert_after output must not repeat the current section heading", failures)

    def test_does_not_close_fence_with_mixed_fence_characters(self) -> None:
        request = MarkdownEditRequest(
            instruction="이 섹션 아래에 Markdown 예시를 추가해줘.",
            markdown="## 설치\n\n설치 방법입니다.",
            target=MarkdownEditTarget(type="current_section", start_line=1, end_line=3),
            edit_goal="other",
            edit_operation="insert_after",
        )

        failures = validate_markdown_output(request, "~~~text\n~~~```\n## 설치\n~~~")

        self.assertNotIn("insert_after output must not repeat the current section heading", failures)

    def test_rejects_insert_after_that_repeats_the_document_with_escaped_newlines(self) -> None:
        request = MarkdownEditRequest(
            instruction="문서 아래에 추가해줘.",
            markdown="# 제목\n기존 본문",
            target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=2),
            edit_goal="other",
            edit_operation="insert_after",
            edit_destination="document_end",
        )

        failures = validate_markdown_output(request, "# 제목\\n기존 본문")

        self.assertIn("insert_after output must not repeat the existing target", failures)

    def test_protects_and_restores_structured_markdown_for_cleanup(self) -> None:
        source = (
            "---\ntitle: 운영 점검\nstatus: draft\n---\n\n"
            "본문을 확인을 한다.[^1]\n\n"
            "![구조](https://example.com/diagram.png)\n\n"
            "```bash\n./deploy.sh\n```\n\n"
            "| 환경 | 상태 |\n| --- | --- |\n| prod | ready |\n\n"
            "[^1]: 승인 후 실행한다."
        )
        request = MarkdownEditRequest(
            instruction="문장만 자연스럽게 다듬고 구조는 그대로 유지해줘.",
            markdown=source,
            target=TARGET,
            edit_goal="style_change",
        )

        protected = protect_markdown(request)

        self.assertNotIn("title: 운영 점검", protected.markdown)
        self.assertNotIn("status: draft", protected.markdown)
        self.assertNotIn("./deploy.sh", protected.markdown)
        self.assertNotIn("prod | ready", protected.markdown)
        restored, failures = protected.restore(protected.markdown.replace("본문을 확인을 한다.", "본문을 확인한다."))
        self.assertEqual(failures, [])
        self.assertIn("---\ntitle: 운영 점검\nstatus: draft\n---", restored)
        self.assertIn("![구조](https://example.com/diagram.png)", restored)
        self.assertIn("```bash\n./deploy.sh\n```", restored)
        self.assertIn("| prod | ready |", restored)
        self.assertIn("[^1]: 승인 후 실행한다.", restored)

    def test_restores_footnote_definition_at_line_start(self) -> None:
        request = MarkdownEditRequest(
            instruction="문장을 다듬고 각주는 유지해줘.",
            markdown="본문.[^1]\n\n[^1]: 근거",
            target=TARGET,
            edit_goal="cleanup",
        )
        protected = protect_markdown(request)

        restored, failures = protected.restore(protected.markdown.replace("\n\n{{", "{{"))

        self.assertEqual(failures, [])
        self.assertIn("본문.[^1]\n\n[^1]: 근거", restored)

    def test_protects_additional_code_math_and_gfm_table_forms_for_shorten(self) -> None:
        source = (
            "`deploy()`를 실행한다.\n\n"
            "    ./deploy.sh --prod\n\n"
            "$$\nE = mc^2\n$$\n\n"
            "환경 | 상태\n--- | ---\nprod | ready"
        )
        request = MarkdownEditRequest(
            instruction="설명을 짧게 줄이되 구조는 유지해줘.",
            markdown=source,
            target=TARGET,
            edit_goal="shorten",
        )

        protected = protect_markdown(request)

        for literal in ("`deploy()`", "./deploy.sh --prod", "E = mc^2", "prod | ready"):
            self.assertNotIn(literal, protected.markdown)
        restored, failures = protected.restore(protected.markdown)
        self.assertEqual(failures, [])
        self.assertEqual(restored, source)

    def test_reserves_literal_tokens_for_nested_mixed_task_markers(self) -> None:
        source = (
            "설명 {{FRUITION_PROTECTED_0001}}\n\n"
            "- [ ] 열린 작업\n"
            "  * [x] 완료된 하위 작업\n"
            "  + [X] 대문자 완료 하위 작업\n"
            "- [ ] 두 번째 열린 작업"
        )
        request = MarkdownEditRequest(
            instruction="문장만 자연스럽게 다듬어줘.",
            markdown=source,
            target=TARGET,
            edit_goal="style_change",
        )

        protected = protect_markdown(request)

        self.assertEqual(
            [fragment.token for fragment in protected.fragments],
            [
                "{{FRUITION_PROTECTED_0002}}",
                "{{FRUITION_PROTECTED_0003}}",
                "{{FRUITION_PROTECTED_0004}}",
                "{{FRUITION_PROTECTED_0005}}",
            ],
        )
        restored, failures = protected.restore(protected.markdown)
        self.assertEqual(failures, [])
        self.assertEqual(restored, source)
        self.assertNotIn("{{FRUITION_PROTECTED_0002}}", restored)
        self.assertEqual(restored.count("{{FRUITION_PROTECTED_0001}}"), 1)

    def test_protects_empty_task_markers_with_crlf_in_nested_lists(self) -> None:
        source = (
            "설명\r\n"
            "- [ ]\r\n"
            "  * [x]\r\n"
            "  + [X]\r\n"
            "- [ ]\r\n"
        )
        request = MarkdownEditRequest(
            instruction="문장만 자연스럽게 다듬어줘.",
            markdown=source,
            target=TARGET,
            edit_goal="style_change",
        )

        protected = protect_markdown(request)

        self.assertEqual(len(protected.fragments), 4)
        restored, failures = protected.restore(protected.markdown)
        self.assertEqual(failures, [])
        self.assertEqual(restored, source)

    def test_keeps_lf_task_marker_behavior_unchanged(self) -> None:
        source = "- [ ]\n  * [x]\n- [X]\n"
        request = MarkdownEditRequest(
            instruction="문장만 자연스럽게 다듬어줘.",
            markdown=source,
            target=TARGET,
            edit_goal="style_change",
        )

        protected = protect_markdown(request)

        self.assertEqual(len(protected.fragments), 3)
        restored, failures = protected.restore(protected.markdown)
        self.assertEqual(failures, [])
        self.assertEqual(restored, source)

    def test_does_not_protect_task_markers_for_explicit_structure_change(self) -> None:
        source = "- [ ] 열린 작업\n  - [x] 완료된 하위 작업"
        request = MarkdownEditRequest(
            instruction="체크박스를 완료 상태로 표시해줘.",
            markdown=source,
            target=TARGET,
            edit_goal="style_change",
        )

        protected = protect_markdown(request)

        self.assertEqual(protected.markdown, source)
        self.assertEqual(protected.fragments, ())

    def test_reports_missing_protected_token(self) -> None:
        request = MarkdownEditRequest(
            instruction="본문만 다듬고 이미지는 유지해줘.",
            markdown="본문\n\n![구조](https://example.com/diagram.png)",
            target=TARGET,
            edit_goal="cleanup",
        )
        protected = protect_markdown(request)

        _, failures = protected.restore("본문")

        self.assertEqual(len(failures), 1)
        self.assertIn("protected token count mismatch", failures[0])

    def test_restores_missing_frontmatter_at_document_start(self) -> None:
        request = MarkdownEditRequest(
            instruction="본문만 다듬고 frontmatter는 유지해줘.",
            markdown="---\ntitle: 문서\n---\n\n본문",
            target=TARGET,
            edit_goal="cleanup",
        )
        protected = protect_markdown(request)

        restored, failures = protected.restore("다듬은 본문")

        self.assertEqual(failures, [])
        self.assertEqual(restored, "---\ntitle: 문서\n---\n\n다듬은 본문")

    def test_validates_footnote_reference_count(self) -> None:
        request = MarkdownEditRequest(
            instruction="문장만 자연스럽게 다듬고 각주는 유지해줘.",
            markdown="근거가 있다.[^1]\n\n[^1]: 공식 문서",
            target=TARGET,
            edit_goal="style_change",
        )

        self.assertIn(
            "footnote reference count must be preserved: [^1]",
            validate_markdown_output(request, "근거가 있다.\n\n[^1]: 공식 문서"),
        )

    def test_repairs_known_footnote_marker_corruption(self) -> None:
        request = MarkdownEditRequest(
            instruction="문장만 자연스럽게 다듬고 각주는 유지해줘.",
            markdown="근거가 있다.[^1]\n\n[^1]: 공식 문서",
            target=TARGET,
            edit_goal="style_change",
        )

        repaired = repair_markdown_output(request, "근거가 있습니다.[[1]]\n\n[^1]: 공식 문서")

        self.assertEqual(repaired, "근거가 있습니다.[^1]\n\n[^1]: 공식 문서")

    def test_repairs_display_math_code_fence(self) -> None:
        request = MarkdownEditRequest(
            instruction="관계식을 display math로 표시해줘.",
            markdown="E = mc^2",
            target=TARGET,
            edit_goal="convert_format",
        )

        repaired = repair_markdown_output(request, "```markdown\n$$ E = mc^2 $$\n```")

        self.assertEqual(repaired, "$$ E = mc^2 $$")

    def test_validates_meeting_note_sections(self) -> None:
        request = MarkdownEditRequest(
            instruction="회의록으로 정리해줘.",
            markdown="정책을 논의하고 적용하기로 했다.",
            target=TARGET,
            edit_goal="convert_format",
        )

        failures = validate_markdown_output(request, "## 회의록\n\n정책을 논의했다.")

        self.assertIn("meeting notes must contain `## 논의 사항`", failures)

    def test_validates_requested_inline_styles_even_when_route_is_style_change(self) -> None:
        request = MarkdownEditRequest(
            instruction="제목을 추가하고 회귀 테스트는 굵게, 문서 미리보기는 기울임, 수동 배포는 취소선으로 표시해줘.",
            markdown="릴리스 정책. 회귀 테스트. 문서 미리보기. 수동 배포.",
            target=TARGET,
            edit_goal="style_change",
        )

        failures = validate_markdown_output(request, "# 릴리스 정책\n회귀 테스트, 문서 미리보기, ~~수동 배포~~")

        self.assertIn("bold text must use `**` delimiters", failures)
        self.assertIn("italic text must use `*` delimiters", failures)

    def test_allows_sentence_only_selection_when_title_must_be_preserved(self) -> None:
        request = MarkdownEditRequest(
            instruction="3번째 줄의 원래 문장만 교체하세요. 제목과 나머지 Markdown은 그대로 보존하세요.",
            markdown="# 제목\n첫 번째 문장입니다.\n원래 문장입니다.",
            target=MarkdownEditTarget(type="selection", start_line=3, end_line=3),
            edit_goal="cleanup",
        )

        self.assertEqual(validate_markdown_output(request, "교체된 문장입니다."), [])

    def test_requires_heading_only_for_heading_actions(self) -> None:
        cases = (
            ("제목은 그대로 유지해줘.", False),
            ("제목은 그대로 유지하고 본문만 수정해줘.", False),
            ("본문을 변경하고 제목은 유지해줘.", False),
            ("Keep the heading unchanged.", False),
            ("Keep the heading unchanged, but change the body.", False),
            ("Change the body, but keep the heading unchanged.", False),
            ("Do not change the heading; keep it unchanged.", False),
            ("제목을 추가하지 말고 본문만 수정해줘.", False),
            ("제목을 추가해줘.", True),
            ("제목을 생성해줘.", True),
            ("제목을 변경해줘.", True),
            ("기존 제목은 유지하고 새 제목을 추가해줘.", True),
            ("Add a heading.", True),
            ("Create a heading.", True),
            ("Change the heading.", True),
            ("Keep the heading unchanged, but add a heading.", True),
        )

        for instruction, expects_heading in cases:
            with self.subTest(instruction=instruction):
                request = MarkdownEditRequest(
                    instruction=instruction,
                    markdown="본문입니다.",
                    target=TARGET,
                    edit_goal="convert_format",
                )

                failures = validate_markdown_output(request, "본문입니다.")

                self.assertEqual("heading must start with `# ` through `###### `" in failures, expects_heading)

    def test_validates_exact_format_contracts(self) -> None:
        numbered_request = MarkdownEditRequest(
            instruction="번호 목록으로 바꿔줘.",
            markdown="설치. 테스트.",
            target=TARGET,
            edit_goal="convert_format",
        )
        math_request = MarkdownEditRequest(
            instruction="display math로 바꿔줘.",
            markdown="E = mc^2",
            target=TARGET,
            edit_goal="convert_format",
        )

        self.assertEqual(validate_markdown_output(numbered_request, "1. 설치\n2. 테스트"), [])
        self.assertIn(
            "numbered list items must start directly with `1.`, `2.`, and so on",
            validate_markdown_output(numbered_request, "- 1. 설치\n- 2. 테스트"),
        )
        self.assertEqual(validate_markdown_output(math_request, "$$\nE = mc^2\n$$"), [])
        self.assertIn(
            "display math must not be wrapped in a code fence",
            validate_markdown_output(math_request, "```markdown\n$$E = mc^2$$\n```"),
        )

    def test_rejects_unrequested_list_marker_for_cleanup(self) -> None:
        request = MarkdownEditRequest(
            instruction="문장을 자연스럽게 다듬어줘.",
            markdown="배포 전 테스트가 필요하다.",
            target=TARGET,
            edit_goal="style_change",
        )

        self.assertIn(
            "plain-text edit must not add a list marker",
            validate_markdown_output(request, "- 배포 전 테스트가 필요하다."),
        )

    def test_additive_edit_preserves_existing_lines_in_order(self) -> None:
        request = MarkdownEditRequest(
            instruction="기존 문서에 배포 절차를 추가해줘.",
            markdown="# 안내\n\n기존 내용입니다.\n\n- 유지할 항목",
            target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=5),
            edit_goal="other",
        )

        self.assertIn(
            "additive edit must preserve every existing non-empty line in order",
            validate_markdown_output(request, "## 배포 절차\n\n새 내용입니다."),
        )
        self.assertEqual(
            validate_markdown_output(
                request,
                "# 안내\n\n기존 내용입니다.\n\n## 배포 절차\n\n새 내용입니다.\n\n- 유지할 항목",
            ),
            [],
        )

    def test_additive_preservation_does_not_block_explicit_content_change(self) -> None:
        request = MarkdownEditRequest(
            instruction="배포 절차를 추가하고 기존 설명도 수정해줘.",
            markdown="# 안내\n\n기존 내용입니다.",
            target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=3),
            edit_goal="other",
        )

        self.assertEqual(validate_markdown_output(request, "# 안내\n\n수정한 내용과 배포 절차입니다."), [])

    def test_section_addition_preserves_existing_document_for_convert_format(self) -> None:
        request = MarkdownEditRequest(
            instruction="기존 문서에 문제 해결 섹션을 추가하고 예시를 작성해줘.",
            markdown="# 설치 안내\n\n기존 설치 절차입니다.",
            target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=3),
            edit_goal="convert_format",
        )

        self.assertIn(
            "additive edit must preserve every existing non-empty line in order",
            validate_markdown_output(request, "## 문제 해결\n\n문제가 생기면 로그를 확인합니다."),
        )

    def test_rejects_unrequested_list_marker_for_shorten(self) -> None:
        request = MarkdownEditRequest(
            instruction="한 문장으로 줄여줘.",
            markdown="시작 안내를 합치고 용어를 통일해야 한다.",
            target=TARGET,
            edit_goal="shorten",
        )

        self.assertIn(
            "plain-text edit must not add a list marker",
            validate_markdown_output(request, "- 시작 안내와 용어를 통합해야 한다."),
        )

    def test_validates_shorten_length_line_count_and_literal_anchors(self) -> None:
        request = MarkdownEditRequest(
            instruction="한 문장으로 짧게 줄여줘.",
            markdown="API 응답의 TTL은 10분이다. 이 설정을 간단히 설명한다.",
            target=TARGET,
            edit_goal="shorten",
        )

        failures = validate_markdown_output(request, "설정을 설명한다.\nTTL은 짧다.")

        self.assertIn("one-sentence shortening must stay on one line", failures)
        self.assertIn("shortening must preserve literal anchor: API", failures)
        self.assertIn("shortening must preserve literal anchor: 10분", failures)

    def test_shorten_ignores_short_unitless_numbers_only(self) -> None:
        request = MarkdownEditRequest(
            instruction="짧게 줄여줘.",
            markdown=(
                "NTP, MQTT, TLS 1.3과 1.2.3 버전을 사용한다. 5분마다 30초 동안 8883 포트로 연결하며 "
                "123abc 토큰과 02, 3, 64는 내부 순번이다. retry_count 값을 기록한다."
            ),
            target=TARGET,
            edit_goal="shorten",
        )

        failures = validate_markdown_output(
            request,
            "NTP, MQTT, TLS 1.3과 1.2.3으로 5분마다 30초 동안 8883 포트에 연결하고 retry_count 값을 기록한다.",
        )
        missing_failures = validate_markdown_output(request, "연결 설정과 내부 순번을 기록한다.")

        self.assertEqual(failures, [])
        for anchor in ("NTP", "MQTT", "TLS", "1.3", "1.2.3", "5분", "30초", "8883", "retry_count"):
            self.assertIn(f"shortening must preserve literal anchor: {anchor}", missing_failures)
        for anchor in ("1.2", "123", "02", "3", "64"):
            self.assertNotIn(f"shortening must preserve literal anchor: {anchor}", missing_failures)

    def test_shorten_preserves_independent_decimal_number(self) -> None:
        request = MarkdownEditRequest(
            instruction="짧게 줄여줘.",
            markdown="독립 소수 1.2를 사용한다.",
            target=TARGET,
            edit_goal="shorten",
        )

        self.assertEqual(validate_markdown_output(request, "소수 1.2를 사용한다."), [])
        missing_failures = validate_markdown_output(request, "독립 소수를 사용한다.")

        self.assertIn("shortening must preserve literal anchor: 1.2", missing_failures)

    def test_rejects_unexpected_han_characters_in_korean_cleanup(self) -> None:
        request = MarkdownEditRequest(
            instruction="문장을 자연스럽게 다듬어줘.",
            markdown="배포하기 전에 테스트를 한다.",
            target=MarkdownEditTarget(type="selection", start_line=1, end_line=1),
            edit_goal="cleanup",
        )

        failures = validate_markdown_output(request, "테스트后再测试之前")

        self.assertIn("Korean text edit must not introduce Han characters absent from the source", failures)


if __name__ == "__main__":
    unittest.main()
