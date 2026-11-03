from app.modules.markdown_edit.domain.entities import MarkdownEditTarget
from app.modules.skill.domain.instruction_placeholders import (
    EDIT_PAYLOAD_DOCUMENT_REFERENCE,
    MAX_INLINE_DOCUMENT_CHARS,
    NO_DOCUMENT,
    NO_SELECTION,
    REFERENCE_CONTEXT_DOCUMENT_REFERENCE,
    TRUNCATED_SUFFIX,
    edit_target_selection_reference,
    render_skill_instructions,
)
from tests.modules.skill.test_skill_test_run import MARKDOWN, build, run

TAG_INSTRUCTIONS = "문서 맨 끝에 다음 한 줄을 그대로 추가한다:\nTAG[{요청}] DOC[{문서}] SEL[{선택한 부분}]"


def test_fills_request_selection_and_references_document_already_in_payload():
    rendered = render_skill_instructions(
        TAG_INSTRUCTIONS,
        request="태그 줄을 붙여 줘",
        selection="둘째 문장이다.",
        document="# 본문\n긴 내용",
        document_reference=EDIT_PAYLOAD_DOCUMENT_REFERENCE,
    )

    assert rendered.endswith(f"TAG[태그 줄을 붙여 줘] DOC[{EDIT_PAYLOAD_DOCUMENT_REFERENCE}] SEL[둘째 문장이다.]")
    assert "긴 내용" not in rendered


def test_uses_no_selection_marker_and_inlines_document_when_not_in_payload():
    rendered = render_skill_instructions(TAG_INSTRUCTIONS, request="정리", document="# 본문\n긴 내용")

    assert "DOC[# 본문\n긴 내용]" in rendered
    assert f"SEL[{NO_SELECTION}]" in rendered


def test_marks_missing_document_and_clips_long_document():
    assert f"DOC[{NO_DOCUMENT}]" in render_skill_instructions(TAG_INSTRUCTIONS, request="정리")

    long_document = "가" * (MAX_INLINE_DOCUMENT_CHARS + 10)
    rendered = render_skill_instructions("{문서}", request="정리", document=long_document)

    assert rendered == "가" * MAX_INLINE_DOCUMENT_CHARS + TRUNCATED_SUFFIX


def test_leaves_unknown_placeholders_and_braces_and_does_not_resubstitute_values():
    rendered = render_skill_instructions(
        "{이름} {{FRUITION}} {요청} {문서 } {선택한부분}",
        request="{문서}를 요약해 줘",
        document_reference=EDIT_PAYLOAD_DOCUMENT_REFERENCE,
    )

    assert rendered == "{이름} {{FRUITION}} {문서}를 요약해 줘 {문서 } {선택한부분}"


def test_edit_test_run_substitutes_placeholders_for_selection():
    use_case, _, editor, _ = build()

    run(
        use_case,
        target=MarkdownEditTarget(type="selection", start_line=2, end_line=3),
        instructions="{선택한 부분}을 요약한다. 전체: {문서} / 요청: {요청}",
    )

    instructions = editor.edit_requests[0].skill_instructions
    # 선택 범위는 편집 payload의 대상이므로 본문을 다시 싣지 않고 범위를 가리킨다.
    assert instructions.startswith(f"{edit_target_selection_reference(2, 3)}을 요약한다.")
    assert "둘째 문장이다." not in instructions
    assert f"전체: {EDIT_PAYLOAD_DOCUMENT_REFERENCE}" in instructions
    assert editor.edit_requests[0].instruction in instructions
    assert MARKDOWN not in instructions


def test_create_test_run_references_source_document_context():
    use_case, _, editor, _ = build()

    run(use_case, capabilities=("document-create",), instructions="{문서}를 참고해 요약 문서를 만든다. {선택한 부분}")

    instructions = editor.create_requests[0].skill_instructions
    assert instructions == f"{REFERENCE_CONTEXT_DOCUMENT_REFERENCE}를 참고해 요약 문서를 만든다. {NO_SELECTION}"


def test_selected_skill_prompt_tells_model_to_follow_literal_lines():
    from app.core.llm_prompt import with_schema_and_skill_prompt

    prompt = with_schema_and_skill_prompt("system", "", "다음 한 줄을 그대로 추가한다: TAG[x]")

    assert "사용자가 이 Skill을 직접 선택했다" in prompt
    assert "적힌 대로 출력한다" in prompt


def test_selection_reference_keeps_instruction_sentence_whole():
    """선택 본문이 지침 문장 한가운데 끼면 '선택한 부분의 제목 줄은 그대로 둔다'가 본문 끝에 붙어 깨진다."""
    instructions = "- {선택한 부분}의 제목 줄은 그대로 둔다.\n- 제목 아래 설명을 체크리스트로 바꾼다."
    selection = "### 워크로드 리소스\n\n파드를 직접 만들 필요가 없다."

    inlined = render_skill_instructions(instructions, request="적용", selection=selection)
    referenced = render_skill_instructions(
        instructions, request="적용", selection=selection, selection_reference=edit_target_selection_reference(16, 28)
    )

    assert "없다.의 제목 줄은 그대로 둔다." in inlined
    assert referenced.startswith("- [선택한 부분: 요청 payload의 편집 대상 16~28줄]의 제목 줄은 그대로 둔다.\n")
    assert "### 워크로드 리소스" not in referenced
