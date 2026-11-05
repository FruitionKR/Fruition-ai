from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from app.modules.markdown_edit.domain.entities import GeneratedMarkdownDocument, MarkdownEditRequest


PROTECTED_EDIT_GOALS = {"cleanup", "style_change", "shorten"}
# 번역은 글자를 바꾸는 편집이라 표·링크 글자는 번역하고, 코드·수식처럼 번역하면 안 되는 조각만 보호한다.
CODE_PROTECTED_EDIT_GOALS = {"translate"}
CODE_FRAGMENT_KINDS = {"frontmatter", "code_fence", "indented_code", "display_math", "inline_code"}
TASK_LIST_EDIT_GOALS = {"cleanup", "style_change", "translate", "shorten"}
PLAIN_TEXT_EDIT_GOALS = {"cleanup", "style_change", "translate", "shorten"}
TASK_LIST_MARKER_PATTERN = r"(?m)^[ \t]*[-*+][ \t]+\[[ xX]\](?:[ \t]+|[ \t]*\r?$)"
PROTECTED_TOKEN_PATTERN = r"\{\{FRUITION_PROTECTED_\d{4}\}\}"


@dataclass(frozen=True)
class ProtectedFragment:
    kind: str
    token: str
    markdown: str


@dataclass(frozen=True)
class ProtectedMarkdown:
    markdown: str
    fragments: tuple[ProtectedFragment, ...]

    def restore(self, replacement: str) -> tuple[str, list[str]]:
        restored = replacement
        failures: list[str] = []
        for fragment in self.fragments:
            count = restored.count(fragment.token)
            if count == 0 and fragment.kind == "frontmatter":
                restored = f"{fragment.markdown}\n\n{restored.lstrip()}"
                continue
            if count != 1:
                failures.append(f"protected token count mismatch: {fragment.token}={count}")
                continue
            if fragment.kind == "footnote_definition":
                before, _, after = restored.partition(fragment.token)
                suffix = f"\n\n{after.lstrip()}" if after.strip() else ""
                restored = f"{before.rstrip()}\n\n{fragment.markdown}{suffix}"
                continue
            restored = restored.replace(fragment.token, fragment.markdown)
        return restored, failures


class MarkdownOutputContractError(ValueError):
    def __init__(self, failures: list[str], replacement_markdown: str) -> None:
        super().__init__("Markdown output contract failed: " + "; ".join(failures))
        self.failures = failures
        self.replacement_markdown = replacement_markdown


class MarkdownCreateOutputContractError(ValueError):
    def __init__(self, failures: list[str], output: dict[str, object]) -> None:
        super().__init__("Markdown create output contract failed: " + "; ".join(failures))
        self.failures = failures
        self.output = output


def validate_markdown_create_output(document: GeneratedMarkdownDocument) -> list[str]:
    failures: list[str] = []
    if not document.title.strip():
        failures.append("title must not be empty")
    if not document.summary.strip():
        failures.append("summary must not be empty")
    if not document.markdown.strip():
        failures.append("markdown must not be empty")
    return failures


def protect_markdown(request: MarkdownEditRequest) -> ProtectedMarkdown:
    if request.edit_operation != "replace":
        return ProtectedMarkdown(markdown=request.markdown, fragments=())

    preserves_structured_markdown = request.edit_goal in PROTECTED_EDIT_GOALS
    preserves_code = preserves_structured_markdown or request.edit_goal in CODE_PROTECTED_EDIT_GOALS
    preserves_task_markers = request.edit_goal in TASK_LIST_EDIT_GOALS
    if (not preserves_code and not preserves_task_markers) or request.detail.structure_change:
        return ProtectedMarkdown(markdown=request.markdown, fragments=())

    markdown = request.markdown
    fragments: list[ProtectedFragment] = []
    used_tokens = set(re.findall(PROTECTED_TOKEN_PATTERN, markdown))
    patterns = (
        ("frontmatter", r"(?s)\A---\r?\n.*?\r?\n---(?=\r?\n|\Z)"),
        ("code_fence", r"(?ms)^(`{3,}|~{3,})[^\n]*\n.*?^\1\s*$"),
        ("indented_code", r"(?m)(?:^(?: {4}|\t)[^\n]*(?:\r?\n|$))+"),
        ("display_math", r"(?s)\$\$.*?\$\$"),
        ("inline_code", r"(?<!`)(`+)[^`\n]+\1(?!`)"),
        (
            "table",
            r"(?m)^(?=[^\r\n]*\|)[^\r\n]+\r?\n"
            r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|?[ \t]*"
            r"(?:\r?\n(?=[^\r\n]*\|)[^\r\n]*)*",
        ),
        ("image", r"!\[[^\]\n]*\]\([^\)\n]+\)"),
        ("link", r"(?<!!)\[[^\]\n]+\]\([^\)\n]+\)"),
        ("footnote_definition", r"(?m)^\[\^[^\]\n]+\]:[^\n]*(?:\r?\n(?: {2,}|\t)[^\n]*)*"),
        ("divider", r"(?m)^(?:---|\*\*\*)$"),
    ) if preserves_code else ()
    if not preserves_structured_markdown:
        patterns = tuple(item for item in patterns if item[0] in CODE_FRAGMENT_KINDS)
    if preserves_task_markers:
        patterns += (("task_list_marker", TASK_LIST_MARKER_PATTERN),)
    # 섹션을 고칠 때 모델이 맨 위 제목을 빼먹는 일이 잦다. 구조를 바꾸지 않고 제목도 요청하지 않은 편집이면 섹션 제목을 잠근다.
    # 다른 조각보다 먼저 잠가야 제목 안 인라인 코드가 따로 잠기지 않는다.
    if (
        preserves_structured_markdown
        and request.target.type == "current_section"
        and "heading" not in request.detail.requested_formats
    ):
        patterns = (("section_heading", r"\A[ \t]*#{1,6}[ \t][^\r\n]*"), *patterns)
    for kind, pattern in patterns:
        markdown = re.sub(
            pattern,
            lambda match, kind=kind: _protect_match(kind, match.group(0), fragments, used_tokens),
            markdown,
        )
    return ProtectedMarkdown(markdown=markdown, fragments=tuple(fragments))


LIST_ITEM_PATTERN = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def validate_markdown_output(request: MarkdownEditRequest, replacement: str) -> list[str]:
    """요청 문장은 해석하지 않는다. 라우터의 edit_goal·edit_operation과 세부 분류(request.detail)만 보고 모양을 검사한다."""
    nonempty_lines = [line for line in replacement.splitlines() if line.strip()]
    # 문서 전체를 목록으로 바꿀 때 원문 제목은 그대로 둔다. 목록 형식은 제목을 뺀 줄에만 요구한다.
    source_headings = set(_atx_heading_lines(request.markdown))
    item_lines = [line for line in nonempty_lines if line.strip() not in source_headings]
    failures = _edit_goal_shape_failures(request, replacement, item_lines)
    failures.extend(_requested_markdown_failures(request.detail.requested_formats, replacement, nonempty_lines, item_lines))

    if request.edit_goal in PLAIN_TEXT_EDIT_GOALS and not _source_starts_with_list(request.markdown):
        if re.match(r"^\s*(?:[-*+]\s+|\d+\.\s+)", replacement):
            failures.append("plain-text edit must not add a list marker")

    if request.edit_operation == "replace" and request.edit_goal in PROTECTED_EDIT_GOALS:
        failures.extend(_protected_content_failures(request.markdown, replacement))
    if request.edit_operation == "replace" and request.edit_goal in PROTECTED_EDIT_GOALS | CODE_PROTECTED_EDIT_GOALS:
        failures.extend(_protected_fragment_failures(request, replacement))

    # 줄이기 규칙은 기존 내용을 줄여 바꿀 때만 쓴다. 요약을 덧붙이는 insert_after는 원문 길이·고정어와 비교하지 않는다.
    if request.edit_goal == "shorten" and request.edit_operation == "replace":
        failures.extend(_shortening_failures(request.markdown, replacement))
    if request.edit_operation == "insert_after":
        failures.extend(_insert_after_repeat_failures(request.markdown, replacement))
    if request.edit_operation == "replace" and request.detail.addition_only:
        failures.extend(_additive_preservation_failures(request.markdown, replacement))

    return failures


def _edit_goal_shape_failures(request: MarkdownEditRequest, replacement: str, item_lines: list[str]) -> list[str]:
    failures: list[str] = []
    # 목록 형식은 목록 항목 줄에만 요구한다. 제목·도입 문장·굵은 소제목 같은 목록 밖 줄은 둘 수 있다.
    list_lines = [line for line in item_lines if LIST_ITEM_PATTERN.match(line)]
    if request.edit_goal == "checklist":
        if not list_lines or not all(re.match(r"^\s*- \[ \] ", line) for line in list_lines):
            failures.append("checklist items must all start with `- [ ] `")
    if request.edit_operation == "insert_after":
        source_heading = next(iter(_atx_heading_lines(request.markdown)), "")
        if source_heading and source_heading in _atx_heading_lines(replacement):
            failures.append("insert_after output must not repeat the current section heading")
    if request.edit_goal == "bullet_list":
        if not list_lines or not all(re.match(r"^\s*[-*+]\s+", line) for line in list_lines):
            failures.append("bullet list items must use plain bullet markers")
        if any(re.match(r"^\s*[-*+]\s+\[[ xX]\]\s+", line) for line in list_lines):
            failures.append("plain bullet list must not contain checkboxes")
    return failures


def _requested_markdown_failures(
    formats: tuple[str, ...],
    replacement: str,
    nonempty_lines: list[str],
    item_lines: list[str],
) -> list[str]:
    failures: list[str] = []
    if "numbered_list" in formats:
        if not item_lines or not all(re.match(r"^\d+\.\s+", line) for line in item_lines):
            failures.append("numbered list items must start directly with `1.`, `2.`, and so on")
    if "blockquote" in formats and not any(line.startswith("> ") for line in nonempty_lines):
        failures.append("blockquote marker `> ` must start its line")
    if "heading" in formats and not any(re.match(r"^#{1,6}\s+", line) for line in nonempty_lines):
        failures.append("heading must start with `# ` through `###### `")
    if "inline_style" in formats and not re.search(r"\*[^*\n]+\*|~~[^~\n]+~~", replacement):
        failures.append("inline style must use `**`, `*`, or `~~` delimiters")
    if "display_math" in formats:
        if not re.search(r"\$\$[\s\S]+\$\$", replacement):
            failures.append("display math must use `$$` delimiters")
        if "```" in replacement or "~~~" in replacement:
            failures.append("display math must not be wrapped in a code fence")
    if "mermaid" in formats:
        if not re.search(r"```mermaid\s+[\s\S]+```", replacement):
            failures.append("Mermaid output must use a `mermaid` code fence")
        if "flowchart" not in replacement or "-->" not in replacement:
            failures.append("Mermaid output must contain flowchart edges")
    if "table" in formats and not re.search(
        # 열이 하나인 표(`| --- |`)도 받되, 파이프 없는 `---`(구분선)는 표로 보지 않는다.
        r"(?m)^(?=[^\n]*\|)[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)*\|?[ \t]*$", replacement
    ):
        failures.append("table must use a GFM table delimiter row")
    if "code_block" in formats and not re.search(r"(?m)^ {0,3}(?:`{3,}|~{3,})", replacement):
        failures.append("code block must use a fenced code block")
    if "meeting_notes" in formats:
        for heading in ("## 논의 사항", "## 결정 사항", "## 다음 작업"):
            if heading not in replacement:
                failures.append(f"meeting notes must contain `{heading}`")
    return failures


def _protected_content_failures(source: str, replacement: str) -> list[str]:
    failures: list[str] = []
    for marker in set(re.findall(r"\[\^[^\]\n]+\]", source)):
        if replacement.count(marker) < source.count(marker):
            failures.append(f"footnote reference count must be preserved: {marker}")
    if not re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", source) and re.search(
        r"[\u3400-\u4dbf\u4e00-\u9fff]", replacement
    ):
        failures.append("Korean text edit must not introduce Han characters absent from the source")
    return failures


def _protected_fragment_failures(
    request: MarkdownEditRequest,
    replacement: str,
) -> list[str]:
    fragments = protect_markdown(request).fragments
    expected = Counter((fragment.kind, fragment.markdown) for fragment in fragments)
    failures: list[str] = []
    # 섹션 제목은 맨 첫 줄을 통째로 잠근다. 하위 제목(`### 설치 방법`)이 `## 설치`를 부분 문자열로 품으므로
    # 개수가 아니라 첫 줄로 확인한다. 그 안의 코드·링크는 따로 세지 않았으니 결과에서도 첫 줄을 빼고 센다.
    for fragment in fragments:
        if fragment.kind == "section_heading":
            del expected[(fragment.kind, fragment.markdown)]
            first_line, _, rest = replacement.lstrip("\r\n").partition("\n")
            if first_line.rstrip("\r") == fragment.markdown:
                replacement = rest
            else:
                failures.append("protected section_heading must stay the first line")
    for (kind, markdown), expected_count in expected.items():
        actual_count = replacement.count(markdown)
        if actual_count != expected_count:
            failures.append(
                f"protected {kind} count must be preserved: expected {expected_count}, got {actual_count}"
            )
    return failures


def _shortening_failures(source: str, replacement: str) -> list[str]:
    failures: list[str] = []
    if len(replacement.strip()) >= len(source.strip()):
        failures.append("shortening result must be shorter than the source")
    # 줄이면 고정어(숫자·약어·URL)도 일부 빠지는 게 정상이다. 원문에 없는 고정어가 생기면 숫자·이름을 바꾼 것이다.
    source_anchors = _literal_anchors(source)
    for anchor in sorted(_literal_anchors(replacement) - source_anchors):
        failures.append(f"shortening must not add or alter literal anchor: {anchor}")
    return failures


def _additive_preservation_failures(source: str, replacement: str) -> list[str]:
    replacement_lines = iter(line for line in replacement.splitlines() if line.strip())
    for source_line in (line for line in source.splitlines() if line.strip()):
        if not any(line == source_line for line in replacement_lines):
            return ["additive edit must preserve every existing non-empty line in order"]
    return []


def _insert_after_repeat_failures(source: str, replacement: str) -> list[str]:
    normalized_replacement = replacement.replace("\\r\\n", "\n").replace("\\n", "\n")
    if source.strip() and not _additive_preservation_failures(
        source,
        normalized_replacement,
    ):
        return ["insert_after output must not repeat the existing target"]
    return []


def repair_markdown_output(request: MarkdownEditRequest, replacement: str) -> str:
    if "display_math" in request.detail.requested_formats:
        fenced = re.fullmatch(r"```(?:markdown)?\s*\n([\s\S]*?)\n```", replacement.strip())
        if fenced and "$$" in fenced.group(1):
            replacement = fenced.group(1).strip()

    if request.edit_goal not in PROTECTED_EDIT_GOALS:
        return replacement

    repaired = replacement
    for marker in set(re.findall(r"\[\^[^\]\n]+\]", request.markdown)):
        deficit = request.markdown.count(marker) - repaired.count(marker)
        if deficit <= 0:
            continue
        label = marker[2:-1]
        for malformed in (f"[[{label}]]", f"[{label}]"):
            replacements = min(deficit, repaired.count(malformed))
            if replacements:
                repaired = repaired.replace(malformed, marker, replacements)
                deficit -= replacements
            if deficit == 0:
                break
    return repaired


def _protect_match(
    kind: str,
    markdown: str,
    fragments: list[ProtectedFragment],
    used_tokens: set[str],
) -> str:
    token_number = len(fragments) + 1
    token = f"{{{{FRUITION_PROTECTED_{token_number:04d}}}}}"
    while token in used_tokens:
        token_number += 1
        token = f"{{{{FRUITION_PROTECTED_{token_number:04d}}}}}"
    used_tokens.add(token)
    fragments.append(ProtectedFragment(kind=kind, token=token, markdown=markdown))
    return token


def _literal_anchors(markdown: str) -> set[str]:
    patterns = (
        r"https?://[^\s)\]>]+",
        r"(?<![\w.])(?:\d+(?:[.,]\d+)*(?:%|초|분|시간|일|주|개월|년|명|개|건|KB|MB|GB|TB)|\d+(?:[.,]\d+)+|\d{3,})(?![A-Za-z0-9_.])",
        r"\b[A-Z][A-Z0-9_]{1,}\b",
        r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b",
        r"\b[a-z]+(?:[A-Z][A-Za-z0-9]*)+\b",
    )
    return {match.group(0) for pattern in patterns for match in re.finditer(pattern, markdown)}


def _source_starts_with_list(markdown: str) -> bool:
    return bool(re.match(r"^\s*(?:[-*+]\s+|\d+\.\s+)", markdown))


def _atx_heading_lines(markdown: str) -> tuple[str, ...]:
    headings: list[str] = []
    fence_character: str | None = None
    fence_length = 0
    for line in markdown.splitlines():
        if fence_character is not None:
            closing_fence = re.match(r"^ {0,3}(`+|~+)[ \t]*$", line)
            if closing_fence:
                marker = closing_fence.group(1)
                if marker[0] == fence_character and len(marker) >= fence_length:
                    fence_character = None
                    fence_length = 0
            continue

        opening_fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if opening_fence:
            marker = opening_fence.group(1)
            info = opening_fence.group(2)
            if marker[0] != "`" or "`" not in info:
                fence_character = marker[0]
                fence_length = len(marker)
            continue

        if re.match(r"^ {0,3}#{1,6}(?:[ \t]+|$)", line):
            headings.append(line.strip())
    return tuple(headings)
