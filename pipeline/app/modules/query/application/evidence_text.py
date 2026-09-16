import re

from app.modules.query.application.source_references import is_block_ref_only, source_block_ids


def split_structured_evidence_units(paragraph: str) -> list[tuple[str, str, float]]:
    section = section_heading(paragraph) or "paragraph"
    weight = section_weight(section)
    units = split_evidence_units(paragraph)
    return [(unit, section, weight) for unit in units]


def split_evidence_units(paragraph: str) -> list[str]:
    bullet_lines = [
        clean_sentence(line)
        for line in paragraph.splitlines()
        if re.match(r"^\s*[-*]\s+", line) and source_block_ids(line)
    ]
    if bullet_lines:
        return bullet_lines
    return split_sentences(paragraph)


def section_weight(section: str) -> float:
    normalized = section.strip().lower()
    weights = {
        "key points": 1.35,
        "observations": 1.30,
        "observation": 1.30,
        "core concepts": 1.20,
        "section candidates": 1.15,
        "mentions": 1.05,
        "categories": 0.95,
    }
    return weights.get(normalized, 1.0)


def specificity_bonus(text: str) -> float:
    variable_markers = len(re.findall(r"\b[A-Z]\s*[:(]", text))
    numeric_ranges = len(re.findall(r"\d+(?:\.\d+)?\s*[-–]\s*\d+(?:\.\d+)?", text))
    units = len(re.findall(r"\d+(?:\.\d+)?\s*(?:mm|°|%|rpm|kw|v)\b", text, flags=re.IGNORECASE))
    list_separators = len(re.findall(r"[;,]", text))
    bonus = min(0.6, variable_markers * 0.08 + numeric_ranges * 0.08 + units * 0.04 + list_separators * 0.01)
    if re.search(r"\b(example|table|수준|범위|정의|조합)\b", text, flags=re.IGNORECASE):
        bonus += 0.15
    return min(0.8, bonus)


def section_heading(paragraph: str) -> str | None:
    for line in paragraph.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = re.match(r"^##\s+(.+?)\s*$", stripped)
        if match:
            return match.group(1).strip().lower()
        return None
    return None


def split_paragraphs(text: str | None) -> list[str]:
    if not text:
        return []
    normalized = "\n".join(line.rstrip() for line in text.strip().splitlines())
    return [
        chunk.strip()
        for chunk in re.split(r"\n\s*\n", normalized)
        if chunk.strip() and not is_heading_only(chunk) and not is_frontmatter(chunk)
    ]


def is_heading_only(text: str) -> bool:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return bool(lines) and all(line.startswith("#") for line in lines)


def is_frontmatter(text: str) -> bool:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return len(lines) >= 2 and lines[0] == "---" and lines[-1] == "---"


def split_sentences(paragraph: str) -> list[str]:
    normalized = " ".join(line.strip() for line in paragraph.strip().splitlines() if line.strip())
    if not normalized:
        return []
    raw_chunks = [
        chunk.strip()
        for chunk in re.split(r"(?<=[.!?。！？])\s+|(?<=[다요죠니다까]\.)\s*", normalized)
        if chunk.strip()
    ]
    chunks: list[str] = []
    for chunk in raw_chunks:
        if chunks and is_block_ref_only(chunk):
            chunks[-1] = f"{chunks[-1]} {chunk}"
        else:
            chunks.append(chunk)
    sentences = [clean_sentence(chunk) for chunk in chunks if clean_sentence(chunk)]
    return sentences or [clean_sentence(normalized)]


def clean_sentence(sentence: str) -> str:
    cleaned = sentence.strip()
    cleaned = re.sub(r"^#+\s*[^-–—:：]*\s*[-–—:：]\s*", "", cleaned)
    cleaned = re.sub(r"^#+\s*", "", cleaned)
    cleaned = re.sub(r"^[-*]\s+", "", cleaned)
    cleaned = re.sub(r"^(Summary|Definition|Why It Matters|Key Points|Evidence)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(?:\[[A-Za-z0-9_,\s-]+\]\s*)+", "", cleaned)
    cleaned = re.sub(r"^[-*]\s+", "", cleaned)
    return cleaned.strip()


def tokens(text: str) -> list[str]:
    return [
        token
        for raw_token in re.findall(r"[A-Za-z0-9가-힣_.-]+", text.lower())
        if (token := normalize_token(raw_token))
    ]


def normalize_token(token: str) -> str:
    for suffix in ["에서는", "으로부터", "로부터", "에게서", "한테서", "에게", "한테", "으로", "로", "이랑", "랑", "이나", "나", "은", "는", "이", "가", "을", "를", "에", "의", "도", "만", "와", "과"]:
        if token.endswith(suffix):
            return token[: -len(suffix)] if len(token) > len(suffix) + 1 else token
    return token
