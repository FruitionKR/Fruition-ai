from app.modules.meeting_notes.application.ports import MeetingNotesGeneratorPort
from app.modules.meeting_notes.domain.entities import (
    InvalidMeetingNotesError,
    TranscriptSegment,
)


class GenerateMeetingNotes:
    def __init__(self, generator: MeetingNotesGeneratorPort) -> None:
        self._generator = generator

    def execute(self, display_name: str, segments: list[TranscriptSegment]) -> dict:
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
        # 요약·결정·할 일은 모두 실제 구간을 근거로 가져야 한다.
        sections = [
            ("summary", "요약"),
            ("decisions", "결정 사항"),
            ("action_items", "할 일"),
            ("open_questions", "미결 사항"),
        ]
        lines = [f"# {display_name.strip() or '회의록'}"]
        for key, heading in sections:
            items = result.get(key)
            if not isinstance(items, list) or len(items) > 100:
                raise InvalidMeetingNotesError("회의록 항목 형식이 올바르지 않습니다.")
            if key == "summary" and not items:
                raise InvalidMeetingNotesError("회의 요약이 비어 있습니다.")
            lines.extend(["", f"## {heading}"])
            if not items:
                lines.append("- 확인된 내용 없음")
            for item in items:
                if not isinstance(item, dict):
                    raise InvalidMeetingNotesError("회의록 항목이 올바르지 않습니다.")
                text, refs = item.get("text"), item.get("source_segment_ids")
                if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                    raise InvalidMeetingNotesError("회의록 문장이 올바르지 않습니다.")
                if (
                    not isinstance(refs, list)
                    or not refs
                    or len(refs) > len(ids)
                    or any(not isinstance(ref, str) or ref not in ids for ref in refs)
                ):
                    raise InvalidMeetingNotesError(
                        "회의록의 전사 근거를 확인하지 못했습니다."
                    )
                lines.append(f"- {text.strip()} ({', '.join(refs)})")
        return {
            "display_name": display_name.strip() or "회의록",
            "markdown": "\n".join(lines),
            **{key: result[key] for key, _ in sections},
        }
