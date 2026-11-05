import unittest
from dataclasses import replace

from app.modules.markdown_edit.domain.entities import MarkdownEditDetail, MarkdownEditRequest, MarkdownEditTarget
from app.modules.markdown_edit.domain.markdown_output_contract import (
    protect_markdown,
    repair_markdown_output,
    validate_markdown_output,
)


TARGET = MarkdownEditTarget(type="whole_document", start_line=1, end_line=20)


RUNBOOK = "# 배포 런북\n\n## 배포 전 확인\n테스트를 통과했는지 확인한다.\n\n## 롤백\n오류율이 오르면 롤백한다."


class MarkdownOutputContractTest(unittest.TestCase):
    def test_whole_document_list_conversion_keeps_source_headings(self) -> None:
        cases = {
            "checklist": ("이 문서를 목록으로 바꿔줘", "- [ ] ", "checklist must contain items starting with `- [ ] `"),
            "bullet_list": ("이 문서를 목록으로 바꿔줘", "- ", "bullet list must contain plain bullet items"),
            "convert_format": (
                "이 문서를 번호 목록으로 바꿔줘", "1. ", "numbered list must contain items starting with `1.`, `2.`, and so on",
            ),
        }
        for goal, (instruction, marker, failure) in cases.items():
            with self.subTest(goal=goal):
                request = MarkdownEditRequest(
                    instruction=instruction,
                    markdown=RUNBOOK,
                    target=TARGET,
                    edit_goal=goal,
                    detail=MarkdownEditDetail(requested_formats=("numbered_list",) if goal == "convert_format" else ()),
                )
                converted = (
                    f"# 배포 런북\n\n## 배포 전 확인\n{marker}테스트를 통과했는지 확인한다.\n\n"
                    f"## 롤백\n{marker}오류율이 오르면 롤백한다."
                )

                self.assertNotIn(failure, validate_markdown_output(request, converted))
                # 목록 항목이 하나도 없는 출력은 목록이 아니다.
                self.assertIn(failure, validate_markdown_output(request, "# 배포 런북\n\n## 롤백"))

    def test_list_conversion_allows_headings_intro_and_bold_labels_around_items(self) -> None:
        cases = {"checklist": ("- [ ] ", "- "), "bullet_list": ("- ", "1. ")}
        for goal, (marker, wrong_marker) in cases.items():
            with self.subTest(goal=goal):
                request = MarkdownEditRequest(instruction="이 섹션을 목록으로 바꿔줘", markdown=RUNBOOK, target=TARGET, edit_goal=goal)
                converted = f"## 배포 점검\n\n배포 전에 아래를 확인한다.\n\n**배포 전**\n\n{marker}테스트 통과\n{marker}승인 확인"
                failures = validate_markdown_output(request, converted)

                self.assertFalse([f for f in failures if "must contain" in f])
                # 요청한 목록이 있으면 다른 항목이 섞여도 코드는 막지 않는다. 그 판단은 LLM 평가기가 한다.
                mixed = validate_markdown_output(request, converted + f"\n{wrong_marker}롤백 준비")
                self.assertFalse([f for f in mixed if "must contain" in f])
                # 요청한 목록이 하나도 없으면 막는다.
                absent = validate_markdown_output(request, f"배포 점검\n{wrong_marker}롤백 준비")
                self.assertTrue([f for f in absent if "must contain" in f])

    def test_numbered_list_only_needs_to_be_present(self) -> None:
        request = MarkdownEditRequest(
            instruction="절차를 번호 목록으로 바꿔줘", markdown=RUNBOOK, target=TARGET, edit_goal="convert_format",
            detail=MarkdownEditDetail(requested_formats=("numbered_list",)),
        )

        self.assertFalse([f for f in validate_markdown_output(request, "아래 순서로 한다.\n\n1. 설치\n2. 테스트") if "must contain" in f])
        self.assertTrue([f for f in validate_markdown_output(request, "설치하고 테스트한다.") if "must contain" in f])

    def test_translation_protects_code_but_not_links_or_tables(self) -> None:
        source = "설치는 `pip install fruition`으로 한다.\n\n```sql\nSELECT 1;\n```\n\n[설치 안내](/docs/install) 참고"
        request = MarkdownEditRequest(
            instruction="영어로 번역해줘.",
            markdown=source,
            target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=7),
            edit_goal="translate",
        )

        kinds = {fragment.kind for fragment in protect_markdown(request).fragments}
        translated = "Install with `pip install fruition`.\n\n```sql\nSELECT 1;\n```\n\nSee [installation guide](/docs/install)"
        changed_code = translated.replace("SELECT 1;", "선택 1;")

        self.assertEqual(kinds, {"inline_code", "code_fence"})
        self.assertFalse([f for f in validate_markdown_output(request, translated) if f.startswith("protected")])
        self.assertTrue([f for f in validate_markdown_output(request, changed_code) if f.startswith("protected code_fence")])

    def test_appended_summary_is_not_checked_as_shortening(self) -> None:
        source = "# 정보 검색\n\n1945년 버니바 부시가 아이디어를 대중화했다. 1992년 TREC가 시작되었다."
        request = MarkdownEditRequest(
            instruction="맨 아래에 3줄로 요약해서 붙여줘.",
            markdown=source,
            target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=3),
            edit_goal="shorten",
            edit_operation="insert_after",
            edit_destination="document_end",
        )

        failures = validate_markdown_output(request, "## 요약\n- 정보 검색의 역사를 다룬다.")

        self.assertFalse([failure for failure in failures if failure.startswith("shortening")])

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

    def test_locks_current_section_heading_unless_structure_changes(self) -> None:
        # 끊긴 불릿을 합칠 때 모델이 섹션 제목을 빼먹어 편집이 실패했다(Jev edit-11).
        source = "### ② 사회의 `공공선` 원칙\n\n- 접근성을\n\n- 보장한다."
        section = MarkdownEditTarget(type="current_section", start_line=1, end_line=5)
        request = MarkdownEditRequest(instruction="끊긴 불릿을 합쳐줘.", markdown=source, target=section, edit_goal="cleanup")

        protected = protect_markdown(request)

        self.assertNotIn("사회의", protected.markdown)
        restored, failures = protected.restore(protected.markdown.replace("- 접근성을\n\n- 보장한다.", "- 접근성을 보장한다."))
        self.assertEqual(failures, [])
        self.assertEqual(restored, "### ② 사회의 `공공선` 원칙\n\n- 접근성을 보장한다.")
        self.assertIn("사회의", protect_markdown(replace(request, detail=MarkdownEditDetail(structure_change=True))).markdown)
        self.assertIn("사회의", protect_markdown(replace(request, target=TARGET)).markdown)
        self.assertIn("사회의", protect_markdown(replace(request, detail=MarkdownEditDetail(requested_formats=("heading",)))).markdown)

    def test_section_heading_fragments_are_not_counted_twice(self) -> None:
        source = "## `API` 안내\n\n`API`를 확인을 한다."
        section = MarkdownEditTarget(type="current_section", start_line=1, end_line=3)
        request = MarkdownEditRequest(instruction="문장을 다듬어줘.", markdown=source, target=section, edit_goal="cleanup")

        self.assertNotIn("protected", " ".join(validate_markdown_output(request, "## `API` 안내\n\n`API`를 확인한다.")))
        self.assertIn(
            "protected section_heading must stay the first line",
            validate_markdown_output(request, "`API`를 확인한다."),
        )

    def test_section_heading_lock_ignores_subheadings_containing_it(self) -> None:
        # `### 설치 방법`이 `## 설치`를 부분 문자열로 품어도 원문 그대로의 결과를 실패로 보지 않는다.
        source = "## 설치\n\n설치를 한다.\n\n### 설치 방법\n\n명령을 실행한다."
        section = MarkdownEditTarget(type="current_section", start_line=1, end_line=7)
        request = MarkdownEditRequest(instruction="문장을 다듬어줘.", markdown=source, target=section, edit_goal="cleanup")

        for output in (source, source.replace("설치를 한다.", "설치한다.")):
            with self.subTest(output=output):
                self.assertNotIn("protected", " ".join(validate_markdown_output(request, output)))
        self.assertIn(
            "protected section_heading must stay the first line",
            validate_markdown_output(request, "설치한다.\n\n### 설치 방법\n\n명령을 실행한다."),
        )

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
            detail=MarkdownEditDetail(structure_change=True),
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
            detail=MarkdownEditDetail(requested_formats=("display_math",)),
        )

        repaired = repair_markdown_output(request, "```markdown\n$$ E = mc^2 $$\n```")

        self.assertEqual(repaired, "$$ E = mc^2 $$")

    def test_validates_meeting_note_sections(self) -> None:
        request = MarkdownEditRequest(
            instruction="회의록으로 정리해줘.",
            markdown="정책을 논의하고 적용하기로 했다.",
            target=TARGET,
            edit_goal="convert_format",
            detail=MarkdownEditDetail(requested_formats=("meeting_notes",)),
        )

        failures = validate_markdown_output(request, "## 회의록\n\n정책을 논의했다.")

        self.assertIn("meeting notes must contain `## 논의 사항`", failures)

    def test_meeting_notes_mentioned_as_reference_does_not_require_sections(self) -> None:
        # 형식 검사는 요청 문장의 단어가 아니라 세부 분류 결과만 본다. 회의록을 참고 자료로 쓴 요청은 형식을 요구하지 않는다.
        request = MarkdownEditRequest(
            instruction="연구실 세미나 회의록을 반영해서 발표 구성의 각 항목 아래에 준비할 내용을 한 줄씩 채워줘",
            markdown="**발표 구성**\n\n1. 연구 질문\n2. 다음 단계",
            target=TARGET,
            edit_goal="other",
        )

        failures = validate_markdown_output(
            request,
            "**발표 구성**\n\n1. 연구 질문\n   - 좁힌 연구 질문을 소개한다.\n2. 다음 단계\n   - 후속 질문 3개를 준비한다.",
        )

        self.assertFalse([failure for failure in failures if failure.startswith("meeting notes")])

    def test_validates_requested_inline_styles_even_when_route_is_style_change(self) -> None:
        request = MarkdownEditRequest(
            instruction="제목을 추가하고 회귀 테스트는 굵게, 문서 미리보기는 기울임, 수동 배포는 취소선으로 표시해줘.",
            markdown="릴리스 정책. 회귀 테스트. 문서 미리보기. 수동 배포.",
            target=TARGET,
            edit_goal="style_change",
            detail=MarkdownEditDetail(requested_formats=("heading", "inline_style")),
        )

        failures = validate_markdown_output(request, "# 릴리스 정책\n회귀 테스트, 문서 미리보기, 수동 배포")

        self.assertIn("inline style must use `**`, `*`, or `~~` delimiters", failures)

    def test_allows_sentence_only_selection_when_title_must_be_preserved(self) -> None:
        request = MarkdownEditRequest(
            instruction="3번째 줄의 원래 문장만 교체하세요. 제목과 나머지 Markdown은 그대로 보존하세요.",
            markdown="# 제목\n첫 번째 문장입니다.\n원래 문장입니다.",
            target=MarkdownEditTarget(type="selection", start_line=3, end_line=3),
            edit_goal="cleanup",
        )

        self.assertEqual(validate_markdown_output(request, "교체된 문장입니다."), [])

    def test_requires_heading_only_when_classified(self) -> None:
        # "제목을 유지해줘"와 "제목을 추가해줘"의 구분은 세부 분류기가 한다. 검사는 분류 결과만 따른다.
        for formats, expects_heading in (((), False), (("heading",), True)):
            with self.subTest(formats=formats):
                request = MarkdownEditRequest(
                    instruction="제목을 추가해줘.",
                    markdown="본문입니다.",
                    target=TARGET,
                    edit_goal="convert_format",
                    detail=MarkdownEditDetail(requested_formats=formats),
                )

                failures = validate_markdown_output(request, "본문입니다.")

                self.assertEqual("heading must start with `# ` through `###### `" in failures, expects_heading)

    def test_validates_exact_format_contracts(self) -> None:
        numbered_request = MarkdownEditRequest(
            instruction="번호 목록으로 바꿔줘.",
            markdown="설치. 테스트.",
            target=TARGET,
            edit_goal="convert_format",
            detail=MarkdownEditDetail(requested_formats=("numbered_list",)),
        )
        math_request = MarkdownEditRequest(
            instruction="display math로 바꿔줘.",
            markdown="E = mc^2",
            target=TARGET,
            edit_goal="convert_format",
            detail=MarkdownEditDetail(requested_formats=("display_math",)),
        )

        self.assertEqual(validate_markdown_output(numbered_request, "1. 설치\n2. 테스트"), [])
        self.assertIn(
            "numbered list must contain items starting with `1.`, `2.`, and so on",
            validate_markdown_output(numbered_request, "- 1. 설치\n- 2. 테스트"),
        )
        self.assertEqual(validate_markdown_output(math_request, "$$\nE = mc^2\n$$"), [])
        self.assertIn(
            "display math must not be wrapped in a code fence",
            validate_markdown_output(math_request, "```markdown\n$$E = mc^2$$\n```"),
        )

    def test_validates_classified_table_and_code_block(self) -> None:
        request = MarkdownEditRequest(
            instruction="방법 초안을 정리해줘.",
            markdown="반구조화 인터뷰. npm test",
            target=TARGET,
            edit_goal="convert_format",
            detail=MarkdownEditDetail(requested_formats=("table", "code_block")),
        )

        self.assertEqual(
            validate_markdown_output(request, "| 구분 | 내용 |\n| :--- | --- |\n| 방법 | 인터뷰 |\n\n```bash\nnpm test\n```"),
            [],
        )
        self.assertNotIn(
            "table must use a GFM table delimiter row",
            validate_markdown_output(request, "| 방법 |\n| --- |\n| 인터뷰 |\n\n```bash\nnpm test\n```"),
        )
        self.assertIn(
            "table must use a GFM table delimiter row",
            validate_markdown_output(request, "방법\n\n---\n\n```bash\nnpm test\n```"),
        )
        failures = validate_markdown_output(request, "구분: 방법, 내용: 인터뷰. npm test")
        self.assertIn("table must use a GFM table delimiter row", failures)
        self.assertIn("code block must use a fenced code block", failures)

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
            detail=MarkdownEditDetail(addition_only=True),
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

    def test_heading_addition_with_explicit_format_change_is_not_addition_only(self) -> None:
        for instruction in ("제목을 추가하고 회귀 테스트는 굵게 표시해줘.",
                            "Add a heading and format 회귀 테스트 in bold."):
            with self.subTest(instruction=instruction):
                request = MarkdownEditRequest(
                    instruction=instruction, markdown="회귀 테스트를 한다.",
                    target=TARGET, edit_goal="convert_format",
                    detail=MarkdownEditDetail(requested_formats=("heading", "inline_style")),
                )
                self.assertEqual(validate_markdown_output(request, "# 릴리스\n\n**회귀 테스트**를 한다."), [])

    def test_classified_addition_only_preserves_existing_lines(self) -> None:
        request = MarkdownEditRequest(
            instruction="Append information to this document.", markdown="기존 내용",
            target=TARGET, edit_goal="convert_format",
            detail=MarkdownEditDetail(addition_only=True),
        )
        self.assertIn("additive edit must preserve every existing non-empty line in order",
                      validate_markdown_output(request, "새 정보만 남김"))

    def test_section_addition_preserves_existing_document_for_convert_format(self) -> None:
        request = MarkdownEditRequest(
            instruction="기존 문서에 문제 해결 섹션을 추가하고 예시를 작성해줘.",
            markdown="# 설치 안내\n\n기존 설치 절차입니다.",
            target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=3),
            edit_goal="convert_format",
            detail=MarkdownEditDetail(addition_only=True),
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

    def test_validates_shorten_length_and_literal_anchors(self) -> None:
        request = MarkdownEditRequest(
            instruction="한 문장으로 짧게 줄여줘.",
            markdown="API 응답의 TTL은 10분이다. 이 설정을 간단히 설명한다.",
            target=TARGET,
            edit_goal="shorten",
        )

        failures = validate_markdown_output(request, "설정을 설명한다.\nTTL은 5분이다.")

        self.assertIn("shortening must not add or alter literal anchor: 5분", failures)
        # 줄이면서 원문 고정어(API, 10분)가 빠지는 것은 실패가 아니다.
        self.assertFalse(any("literal anchor: API" in failure or "literal anchor: 10분" in failure for failure in failures))

    def test_shorten_allows_dropped_anchors_and_flags_altered_ones(self) -> None:
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
        altered_failures = validate_markdown_output(request, "NTP, MQTT, TLS 1.4로 10분마다 8884 포트에 연결하고 retry_cnt 값을 기록한다.")

        self.assertEqual(failures, [])
        self.assertEqual(missing_failures, [])
        for anchor in ("1.4", "10분", "8884", "retry_cnt"):
            self.assertIn(f"shortening must not add or alter literal anchor: {anchor}", altered_failures)
        for anchor in ("NTP", "MQTT", "TLS"):
            self.assertNotIn(f"shortening must not add or alter literal anchor: {anchor}", altered_failures)

    def test_shorten_detects_altered_independent_decimal_number(self) -> None:
        request = MarkdownEditRequest(
            instruction="짧게 줄여줘.",
            markdown="독립 소수 1.2를 사용한다.",
            target=TARGET,
            edit_goal="shorten",
        )

        self.assertEqual(validate_markdown_output(request, "소수 1.2를 사용한다."), [])
        self.assertEqual(validate_markdown_output(request, "독립 소수를 사용한다."), [])
        altered_failures = validate_markdown_output(request, "독립 소수 1.3을 사용한다.")

        self.assertIn("shortening must not add or alter literal anchor: 1.3", altered_failures)

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
