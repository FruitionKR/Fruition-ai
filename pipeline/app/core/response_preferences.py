from __future__ import annotations

from typing import Literal


OutputLanguage = Literal["ko", "en", "document"]
ResponseLength = Literal["concise", "balanced", "detailed"]


def with_response_preferences(
    system_prompt: str,
    output_language: OutputLanguage | None,
    response_length: ResponseLength | None,
    *,
    document_creation: bool = False,
) -> str:
    rules: list[str] = []
    if output_language == "ko":
        rules.append("Write the response in Korean.")
    elif output_language == "en":
        rules.append("Write the response in English.")
    elif output_language == "document":
        rules.append(
            "Use the dominant language of the evidence document; "
            "if it cannot be determined, use the question language."
        )
    if response_length == "concise":
        rules.append("Be concise and include only the essential explanation.")
    elif response_length == "balanced":
        rules.append("Use a balanced level of detail.")
    elif response_length == "detailed":
        rules.append("Give a detailed explanation without omitting necessary context.")
    if not rules:
        return system_prompt
    if response_length is not None:
        rules.append("Do not omit required facts, citations, or warnings.")
    if document_creation:
        rules.insert(
            0,
            "For a new document, an explicit language in the user instruction "
            "overrides the language preference below.",
        )
    rules.append("Keep quotations, code, and proper nouns in their original language.")
    return f"{system_prompt.rstrip()}\n\n# Response Preferences\n" + "\n".join(
        f"- {rule}" for rule in rules
    )
