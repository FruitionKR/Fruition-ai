from app.modules.query.application.ports import ConversationSummarizerPort
from app.modules.query.domain.entities import ConversationContext


RECENT_MESSAGE_LIMIT = 6


def contextualize_question(question: str, conversation_context: ConversationContext | None) -> str:
    if conversation_context is None:
        return question

    sections = []
    matched_referents = _matching_referent_values(question, conversation_context.reference_context)
    if matched_referents:
        sections.append(" ".join(matched_referents))

    reference_lines = _reference_context_lines(
        conversation_context.reference_context,
        excluded_values=set(matched_referents),
    )
    if reference_lines:
        sections.append(" ".join(reference_lines))

    if conversation_context.recent_conversation_summary:
        sections.append(conversation_context.recent_conversation_summary.strip())

    recent_messages = conversation_messages_text(conversation_context)
    if recent_messages:
        sections.append(recent_messages)

    sections.append(question)

    return "\n".join(section for section in sections if section.strip()).strip()


def update_conversation_summary(
    conversation_context: ConversationContext | None,
    summarizer: ConversationSummarizerPort | None,
) -> str | None:
    if (
        conversation_context is None
        or summarizer is None
        or len(conversation_context.recent_messages) < RECENT_MESSAGE_LIMIT
    ):
        return None
    try:
        updated_summary = summarizer.summarize(
            conversation_context.recent_conversation_summary,
            conversation_context.recent_messages,
        ).strip()
    except RuntimeError:
        return None
    return updated_summary or None


def conversation_messages_text(conversation_context: ConversationContext | None) -> str:
    if conversation_context is None:
        return ""
    labels = {"user": "사용자", "assistant": "어시스턴트"}
    return "\n".join(
        f"{labels[message.role]}: {message.content.strip()}"
        for message in conversation_context.recent_messages
        if message.content.strip()
    )


def evidence_question(
    question: str,
    conversation_context: ConversationContext | None,
    contextual_question: str,
) -> str:
    if conversation_context is None:
        return contextual_question
    matched_referents = _matching_referent_values(question, conversation_context.reference_context)
    if not matched_referents:
        return question
    return " ".join([*matched_referents, question])


def _matching_referent_values(question: str, reference_context: dict[str, object]) -> list[str]:
    referents = reference_context.get("referents")
    if not isinstance(referents, dict):
        return []
    values = []
    for marker, value in referents.items():
        if str(marker) in question and value is not None:
            values.extend(_reference_values(value))
    return list(dict.fromkeys(value for value in values if value))


def _reference_context_lines(reference_context: dict[str, object], excluded_values: set[str] | None = None) -> list[str]:
    excluded_values = excluded_values or set()
    lines: list[str] = []
    for value in reference_context.values():
        if value is None:
            continue
        lines.extend(
            line
            for line in _format_reference_value(value)
            if line and line not in excluded_values
        )
    return lines


def _format_reference_value(value: object) -> list[str]:
    if isinstance(value, dict):
        lines = []
        for child_value in value.values():
            if child_value is None:
                continue
            lines.extend(_reference_values(child_value))
        return lines
    if isinstance(value, list):
        return [item for child_value in value if child_value is not None for item in _reference_values(child_value)]
    return [_reference_scalar(value)]


def _reference_values(value: object) -> list[str]:
    if isinstance(value, dict):
        values: list[str] = []
        for child_value in value.values():
            if child_value is not None:
                values.extend(_reference_values(child_value))
        return values
    if isinstance(value, list):
        return [item for child_value in value if child_value is not None for item in _reference_values(child_value)]
    return [_reference_scalar(value)]


def _reference_scalar(value: object) -> str:
    if isinstance(value, (dict, list)):
        return str(value)
    return str(value).strip()
