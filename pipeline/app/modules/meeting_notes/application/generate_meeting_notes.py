from app.core.ai_markdown_sanitizer import sanitize_ai_markdown
from app.modules.meeting_notes.application.ports import MeetingNotesGeneratorPort
from app.modules.meeting_notes.domain.entities import (
    InvalidMeetingNotesError,
    TranscriptSegment,
)

# 근거 구간을 갖는 항목 배열. summary는 비어 있으면 안 된다.
ITEM_SECTIONS = ("summary", "decisions", "action_items", "next_schedule", "open_questions")
MAX_SECTION_ITEMS = 100
MAX_ITEM_CHARS = 2000
MAX_TOPICS = 30
MAX_TOPIC_CHARS = 200
MAX_ATTENDEES = 50
MAX_NAME_CHARS = 100
EMPTY_LINE = "- 확인된 내용 없음"
UNKNOWN = "확인되지 않음"


class GenerateMeetingNotes:
    def __init__(self, generator: MeetingNotesGeneratorPort) -> None:
        self._generator = generator

    def execute(
        self,
        display_name: str,
        segments: list[TranscriptSegment],
        *,
        meeting_date: str | None = None,
        attendees: tuple[str, ...] = (),
    ) -> dict:
        ids = {segment.id for segment in segments}
        if (
            not segments
            or len(ids) != len(segments)
            or not any(s.text.strip() for s in segments)
        ):
            raise ValueError("중복 없는 확정 전사 구간이 필요합니다.")
        result = self._generator.generate(display_name, segments)
        if not isinstance(result, dict):
            raise InvalidMeetingNotesError("회의록 형식이 올바르지 않습니다.")
        # 요약·논의·결정·할 일·일정은 모두 실제 구간을 근거로 가져야 한다.
        sections = {
            key: _items(result.get(key, []), ids, action=key == "action_items")
            for key in ITEM_SECTIONS
        }
        if not sections["summary"]:
            raise InvalidMeetingNotesError("회의 요약이 비어 있습니다.")
        discussion = _discussion(result.get("discussion", []), ids)
        names = _attendees((*attendees, *_names(result.get("attendees", []))))
        title = display_name.strip() or "회의록"
        date = (meeting_date or "").strip() or None
        return {
            "display_name": title,
            # 항목끼리 합쳐지면 참조 링크 정의와 사용이 이어져 새 링크가 생길 수 있다.
            "markdown": sanitize_ai_markdown(_render(title, date, names, sections, discussion)),
            "meeting_date": date,
            "attendees": names,
            "discussion": discussion,
            **sections,
        }


def _items(value: object, ids: set[str], *, action: bool = False) -> list[dict]:
    if not isinstance(value, list) or len(value) > MAX_SECTION_ITEMS:
        raise InvalidMeetingNotesError("회의록 항목 형식이 올바르지 않습니다.")
    return [_item(item, ids, action=action) for item in value]


def _item(item: object, ids: set[str], *, action: bool) -> dict:
    if not isinstance(item, dict):
        raise InvalidMeetingNotesError("회의록 항목이 올바르지 않습니다.")
    text, refs = item.get("text"), item.get("source_segment_ids")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_ITEM_CHARS:
        raise InvalidMeetingNotesError("회의록 문장이 올바르지 않습니다.")
    if (
        not isinstance(refs, list)
        or not refs
        or len(refs) > len(ids)
        or any(not isinstance(ref, str) or ref not in ids for ref in refs)
    ):
        raise InvalidMeetingNotesError("회의록의 전사 근거를 확인하지 못했습니다.")
    # 항목 배열도 그대로 반환되므로 markdown과 같이 외부 이미지·링크를 무력화한다.
    cleaned: dict = {"text": sanitize_ai_markdown(text), "source_segment_ids": list(refs)}
    if action:
        cleaned["owner"] = _optional_name(item.get("owner"))
        cleaned["due"] = _optional_name(item.get("due"))
    return cleaned


def _discussion(value: object, ids: set[str]) -> list[dict]:
    if not isinstance(value, list) or len(value) > MAX_TOPICS:
        raise InvalidMeetingNotesError("회의록 논의 주제 형식이 올바르지 않습니다.")
    topics = []
    for topic in value:
        if not isinstance(topic, dict):
            raise InvalidMeetingNotesError("회의록 논의 주제가 올바르지 않습니다.")
        title = topic.get("topic")
        if not isinstance(title, str) or not title.strip() or len(title) > MAX_TOPIC_CHARS:
            raise InvalidMeetingNotesError("회의록 논의 주제 이름이 올바르지 않습니다.")
        items = _items(topic.get("items"), ids)
        if items:
            topics.append({"topic": _single_line(sanitize_ai_markdown(title)), "items": items})
    return topics


def _optional_name(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_NAME_CHARS:
        raise InvalidMeetingNotesError("회의록 담당자·기한 형식이 올바르지 않습니다.")
    return _single_line(sanitize_ai_markdown(value)) or None


def _names(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_ATTENDEES:
        raise InvalidMeetingNotesError("회의록 참석자 형식이 올바르지 않습니다.")
    return [name for name in (_optional_name(item) for item in value) if name]


def _attendees(names: tuple[str, ...]) -> list[str]:
    """요청이 준 참석자를 먼저 두고, 전사에서 확인된 이름을 중복 없이 잇는다."""
    unique: list[str] = []
    for name in names:
        cleaned = _single_line(name)
        if cleaned and cleaned not in unique and len(unique) < MAX_ATTENDEES:
            unique.append(cleaned)
    return unique


def _single_line(text: str) -> str:
    return " ".join(text.split())


def _render(
    title: str,
    meeting_date: str | None,
    attendees: list[str],
    sections: dict[str, list[dict]],
    discussion: list[dict],
) -> str:
    """Figma 회의록 시안 구성. 근거 구간 ID는 본문에 싣지 않고 항목 배열로만 돌려준다."""
    lines = [
        f"# {title}",
        "",
        f"- 날짜: {meeting_date or UNKNOWN}",
        f"- 참석자: {', '.join(attendees) if attendees else UNKNOWN}",
        "",
        "### 회의 요약",
        *_bullets(sections["summary"]),
        "",
        "### 주요 논의",
    ]
    if not discussion:
        lines.append(EMPTY_LINE)
    for index, topic in enumerate(discussion):
        if index:
            lines.append("")
        lines.extend([f"#### {topic['topic']}", *_bullets(topic["items"])])
    lines.extend(["", "### 결정 사항", *_bullets(sections["decisions"])])
    lines.extend(["", "### 해야 할 일", *_action_lines(sections["action_items"])])
    lines.extend(["", "### 다음 일정", *_bullets(sections["next_schedule"])])
    return "\n".join(lines)


def _bullets(items: list[dict]) -> list[str]:
    return [f"- {_single_line(item['text'])}" for item in items] or [EMPTY_LINE]


def _action_lines(items: list[dict]) -> list[str]:
    """`- [ ] 할 일 · 담당자 · 기한`. 발언으로 확인되지 않은 담당자·기한은 미정으로 둔다."""
    return [
        f"- [ ] {_single_line(item['text'])} · {item['owner'] or '담당자 미정'} · {item['due'] or '기한 미정'}"
        for item in items
    ] or [EMPTY_LINE]
