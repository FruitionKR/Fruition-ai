"""작업 탭 폴더 내용 조회(folder_lookup). 메시지에 나온 폴더 이름으로 폴더를 찾아 바로 아래 항목 이름만 보여 준다.

문서 본문은 읽지 않는다. 폴더 트리는 document-svc가 사이드바와 같은 기준으로 돌려준다.
"""

import re
from dataclasses import dataclass

MAX_LISTED_ITEMS = 50
FOLDER_LOOKUP_READ_FAILED_MESSAGE = "폴더 목록을 읽지 못했어요. 잠시 후 다시 시도해 주세요."
FOLDER_LOOKUP_FORBIDDEN_MESSAGE = "이 워크스페이스의 폴더 목록을 읽을 수 없어요."
FOLDER_NOT_FOUND_MESSAGE = "어느 폴더인지 찾지 못했어요. 폴더 이름을 정확히 적거나 사이드바에서 폴더를 고른 뒤 다시 요청해 주세요."
CONTENT_QUESTION_HINT = "문서 내용이 궁금하면 채팅 탭에서 질문해 주세요."


@dataclass(frozen=True)
class FolderEntry:
    id: str
    name: str
    # 루트부터 이 폴더까지의 이름. 같은 이름 폴더를 가릴 때 보여 준다.
    path: tuple[str, ...]
    child_folders: tuple[str, ...]
    child_documents: tuple[str, ...]


class FolderLookupUnavailableError(Exception):
    """폴더 트리를 읽을 수 없다. message는 사용자에게 보여 줄 문장이다."""


def folder_entries(items: list[object], path: tuple[str, ...] = ()) -> list[FolderEntry]:
    """document-svc 트리(`DocumentTreeResponse.items`)를 폴더 목록으로 펼친다."""
    entries: list[FolderEntry] = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "folder":
            continue
        name = str(item.get("name") or "")
        children = item.get("children") if isinstance(item.get("children"), list) else []
        entries.append(FolderEntry(
            id=str(item.get("id")),
            name=name,
            path=(*path, name),
            child_folders=tuple(str(child.get("name") or "") for child in children if _is(child, "folder")),
            child_documents=tuple(_document_name(child) for child in children if _is(child, "document")),
        ))
        entries.extend(folder_entries(children, (*path, name)))
    return entries


def find_folders(entries: list[FolderEntry], message: str, selected_folder_id: str | None) -> list[FolderEntry]:
    """메시지가 가리키는 폴더. 더 긴 이름에 포함되는 짧은 이름('회의록' ⊂ '2026 회의록')은 뺀다.

    사이드바에서 폴더를 골랐으면 그 폴더가 기본이다. 메시지가 '○○ 폴더'나 따옴표로 다른 폴더를 분명히
    가리킬 때만 바꾼다(질문에 흔히 나오는 '문서'·'자료' 같은 폴더 이름이 가로채지 않게).
    """
    selected = [entry for entry in entries if entry.id == selected_folder_id]
    named = _longest([entry for entry in entries if _mentions(message, entry.name, explicit=bool(selected))])
    if selected and (not named or selected[0] in named):
        return selected
    return named


def _longest(named: list[FolderEntry]) -> list[FolderEntry]:
    return [
        entry for entry in named
        if not any(
            _normalized(entry.name) in _normalized(other.name)
            and len(_normalized(other.name)) > len(_normalized(entry.name))
            for other in named
        )
    ]


# 이름 바로 뒤에 올 수 있는 말. 그 밖의 글자가 붙으면 다른 단어다('기획서'는 '기획' 폴더가 아니다).
# ponytail: 자주 쓰는 조사만 둔다. '회의록안에'처럼 목록에 없는 말이 붙으면 못 찾으니 필요하면 늘린다.
_FOLLOWERS = r"(?:폴더|에서|으로|이랑|은|는|이|가|을|를|에|의|도|로|만|과|와|랑)"


def _mentions(message: str, name: str, *, explicit: bool) -> bool:
    """name이 단어로 나오는지. explicit이면 '○○ 폴더'나 따옴표로 감싼 경우만 센다."""
    chars = [re.escape(ch) for ch in name if not ch.isspace()]
    if not chars:
        return False
    body = r"\s*".join(chars)
    start = r"(?<![0-9A-Za-z가-힣])"
    end = r"(?![0-9A-Za-z가-힣])"
    if explicit:
        pattern = rf"['\"‘“]\s*{body}\s*['\"’”]|{start}{body}\s*폴더"
    else:
        pattern = rf"{start}{body}(?:\s*{_FOLLOWERS}){{0,3}}{end}"
    return re.search(pattern, message, re.IGNORECASE) is not None


def folder_lookup_message(matches: list[FolderEntry]) -> str:
    if not matches:
        return FOLDER_NOT_FOUND_MESSAGE
    if len(matches) > 1:
        paths = "\n".join(f"- {' / '.join(entry.path)}" for entry in matches)
        return f"해당하는 폴더가 여러 개예요. 어느 폴더인지 경로를 적어 다시 요청해 주세요.\n\n{paths}"
    entry = matches[0]
    if not entry.child_folders and not entry.child_documents:
        return f"'{entry.name}' 폴더는 비어 있어요."
    lines = [
        f"'{entry.name}' 폴더({' / '.join(entry.path)})에 하위 폴더 {len(entry.child_folders)}개, "
        f"문서 {len(entry.child_documents)}개가 있어요."
    ]
    items = [f"- 📁 {name}" for name in entry.child_folders] + [f"- {name}" for name in entry.child_documents]
    lines.append("\n".join(items[:MAX_LISTED_ITEMS]))
    if len(items) > MAX_LISTED_ITEMS:
        lines.append(f"외 {len(items) - MAX_LISTED_ITEMS}개")
    lines.append(CONTENT_QUESTION_HINT)
    return "\n\n".join(lines)


def _is(item: object, kind: str) -> bool:
    return isinstance(item, dict) and item.get("type") == kind


def _document_name(item: dict) -> str:
    # 트리의 name은 파일명이다. 사람이 붙인 이름은 document.display_name에 있다.
    document = item.get("document") if isinstance(item.get("document"), dict) else {}
    return str(document.get("display_name") or item.get("name") or "")


def _normalized(text: str) -> str:
    return "".join(text.split()).casefold()
