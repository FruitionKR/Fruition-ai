import re

from app.modules.wiki_generation.domain.text_utils import unique_keep_order


def ref_label(ref_id: str) -> str:
    match = re.search(r"_b(\d{4})$", ref_id)
    if match:
        return f"B{match.group(1)}"
    return ref_id


def cite_refs(refs: list[str], document_id: str | None = None) -> str:
    values = global_refs(document_id, refs) if document_id else unique_keep_order([ref for ref in refs if ref])
    return f" [{', '.join(display_ref(ref) for ref in values)}]" if values else ""


def display_ref(ref: str) -> str:
    if ":" not in ref:
        return ref_label(ref)
    document_id, block_id = ref.split(":", 1)
    return f"{document_id}:{ref_label(block_id)}"


def global_refs(document_id: str | None, refs: list[str]) -> list[str]:
    if not document_id:
        return unique_keep_order([str(ref) for ref in refs if ref])
    return unique_keep_order(
        [
            ref if ":" in str(ref) else f"{document_id}:{ref}"
            for ref in refs
            if ref
        ]
    )


def cite_global_refs(refs: list[str]) -> str:
    values = unique_keep_order([str(ref) for ref in refs if ref])
    return f" [{', '.join(values)}]" if values else ""
