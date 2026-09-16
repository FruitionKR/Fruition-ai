import json


MAX_UNTRUSTED_PAYLOAD_BYTES = 256 * 1024
MAX_UNTRUSTED_INPUT_DEPTH = 12
MAX_UNTRUSTED_CONTAINER_ITEMS = 1000
MAX_UNTRUSTED_TEXT_LENGTH = 200_000
BIDI_CONTROL_CHARACTERS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)


def validate_untrusted_payload(value: object) -> None:
    _validate_untrusted_value(value)
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > MAX_UNTRUSTED_PAYLOAD_BYTES:
        raise ValueError("Untrusted payload is too large.")


def _validate_untrusted_value(value: object, depth: int = 0) -> None:
    if depth > MAX_UNTRUSTED_INPUT_DEPTH:
        raise ValueError("Untrusted payload nesting is too deep.")
    if isinstance(value, str):
        if len(value) > MAX_UNTRUSTED_TEXT_LENGTH:
            raise ValueError("Untrusted payload text is too long.")
        if any(_is_unsafe_control(character) for character in value):
            raise ValueError("Untrusted payload contains an unsafe control character.")
        return
    if isinstance(value, dict):
        if len(value) > MAX_UNTRUSTED_CONTAINER_ITEMS:
            raise ValueError("Untrusted payload contains too many object fields.")
        for key, item in value.items():
            _validate_untrusted_value(key, depth + 1)
            _validate_untrusted_value(item, depth + 1)
        return
    if isinstance(value, list):
        if len(value) > MAX_UNTRUSTED_CONTAINER_ITEMS:
            raise ValueError("Untrusted payload contains too many list items.")
        for item in value:
            _validate_untrusted_value(item, depth + 1)


def _is_unsafe_control(character: str) -> bool:
    code_point = ord(character)
    return (
        character in BIDI_CONTROL_CHARACTERS
        or (code_point < 0x20 and character not in "\t\n\r")
        or 0x7F <= code_point <= 0x9F
    )
