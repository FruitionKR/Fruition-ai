from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.modules.meeting_notes.application.generate_meeting_notes import (
    GenerateMeetingNotes,
)
from app.modules.meeting_notes.domain.entities import (
    InvalidMeetingNotesError,
    TranscriptSegment,
)
from app.modules.meeting_notes.interfaces.http.dependencies import get_meeting_notes
from app.modules.meeting_notes.interfaces.http.schemas import (
    MeetingNotesRequest,
    MeetingNotesResponse,
)
from app.modules.speech.interfaces.http.dependencies import authorize_speech

router = APIRouter(prefix="/meeting-notes", tags=["meeting-notes"])


@router.post("/preview", response_model=MeetingNotesResponse)
def preview_meeting_notes(
    payload: MeetingNotesRequest,
    use_case: Annotated[GenerateMeetingNotes, Depends(get_meeting_notes)],
) -> MeetingNotesResponse:
    authorize_speech(payload.workspace_id, payload.user_id)
    try:
        result = use_case.execute(
            payload.display_name,
            [TranscriptSegment(s.id, s.text) for s in payload.segments],
        )
    except InvalidMeetingNotesError as exc:
        raise HTTPException(
            502, "회의록 근거 검증에 실패했습니다. 다시 생성해주세요."
        ) from exc
    except Exception as exc:
        raise HTTPException(
            502, "회의록을 생성하지 못했습니다. 전사를 보관하고 다시 시도해주세요."
        ) from exc
    return MeetingNotesResponse(**result)
