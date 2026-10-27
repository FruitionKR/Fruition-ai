"""작업 탭 폴더 내용 조회(folder_lookup). 메시지에 나온 폴더 이름으로 폴더를 찾아 바로 아래 항목 이름만 보여 준다.

문서 본문은 읽지 않는다. 폴더 트리는 document-svc가 사이드바와 같은 기준으로 돌려준다.
"""

from dataclasses import dataclass

MAX_LISTED_ITEMS = 50
FOLDER_LOOKUP_READ_FAILED_MESSAGE = "폴더 목록을 읽지 못했어요. 잠시 후 다시 시도해 주세요."
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
    """메시지에 이름이 나온 폴더. 더 긴 이름에 포함되는 짧은 이름('회의록' ⊂ '2026 회의록')은 뺀다.

    이름이 나온 폴더가 없으면 사이드바에서 고른 폴더를 쓴다.
    """
    text = _normalized(message)
    named = [entry for entry in entries if entry.name.strip() and _normalized(entry.name) in text]
    longest = [
        entry for entry in named
        if not any(_normalized(entry.name) in _normalized(other.name) and len(other.name) > len(entry.name) for other in named)
    ]
    if longest:
        return longest
    return [entry for entry in entries if entry.id == selected_folder_id]


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
