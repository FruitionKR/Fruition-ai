"""승인 화면에 보여 줄 문서 변경 diff. 줄 번호는 편집 대상 범위와 같은 기준(markdown_line_count)이다."""

import re
from difflib import SequenceMatcher

_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_CONTEXT_LINES = 3


def build_edit_preview(before: str, after: str) -> dict[str, object]:
    before_lines = [] if before == "" else _LINE_BREAK.split(before)
    after_lines = [] if after == "" else _LINE_BREAK.split(after)
    # autojunk는 자주 나오는 줄(빈 줄 등)을 비교에서 빼 diff를 부풀리므로 끈다.
    matcher = SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    added_lines = 0
    removed_lines = 0
    hunks: list[dict[str, object]] = []
    for group in matcher.get_grouped_opcodes(_CONTEXT_LINES):
        lines: list[dict[str, str]] = []
        for tag, before_start, before_end, after_start, after_end in group:
            if tag == "equal":
                lines.extend({"type": "context", "text": text} for text in before_lines[before_start:before_end])
                continue
            lines.extend({"type": "delete", "text": text} for text in before_lines[before_start:before_end])
            lines.extend({"type": "insert", "text": text} for text in after_lines[after_start:after_end])
            removed_lines += before_end - before_start
            added_lines += after_end - after_start
        hunks.append({"before_start": group[0][1] + 1, "after_start": group[0][3] + 1, "lines": lines})
    return {"added_lines": added_lines, "removed_lines": removed_lines, "hunks": hunks}
