from __future__ import annotations

from typing import List

from app.modules.wiki_generation.domain.entities import SemanticPacket, SourceBlock


class SemanticPacketBuilder:
    """Builds LLM reading packets with short [B0001] anchors.

    The model gets enough context to understand meaning, but not long ref ids or
    locator data. Normalized pipeline refs use short block ids such as B0001.
    """

    def __init__(self, max_chars: int = 7000, overlap_blocks: int = 1) -> None:
        self.max_chars = max_chars
        self.overlap_blocks = overlap_blocks

    def build(self, document_id: str, blocks: list[SourceBlock]) -> list[SemanticPacket]:
        packets: list[SemanticPacket] = []
        current: list[SourceBlock] = []
        current_len = 0

        def emit() -> None:
            nonlocal current, current_len
            if not current:
                return
            chunk_id = f"chunk_{len(packets) + 1:04d}"
            text = "\n".join(b.to_llm_line() for b in current)
            packets.append(
                SemanticPacket(
                    chunk_id=chunk_id,
                    document_id=document_id,
                    block_ids=[b.block_id for b in current],
                    text=text,
                )
            )
            if self.overlap_blocks > 0:
                current = current[-self.overlap_blocks :]
                current_len = sum(len(b.text) + len(b.block_id) + 4 for b in current)
            else:
                current = []
                current_len = 0

        for b in blocks:
            add_len = len(b.text) + len(b.block_id) + 4
            if current and current_len + add_len > self.max_chars:
                emit()
            current.append(b)
            current_len += add_len
        emit()
        return packets
