import json

from app.core.jev_batches import BATCH_TOKEN_MARGIN, request_tokens, token_batches


def _request(rule: str):
    def build(batch: list[str]) -> dict:
        return {
            "state": json.dumps({"rule": rule, "candidates": batch}, ensure_ascii=False),
            "questions": {item: {"type": "choice", "instructions": item, "criteria": {"yes": "y", "no": "n"}} for item in batch},
        }

    return build


def test_shared_header_is_counted_once_per_batch_not_per_item():
    build = _request("규칙 " * 400)
    items = [f"후보{i}" for i in range(10)]
    empty = request_tokens(build([]))
    per_item = request_tokens(build([items[0]])) - empty + BATCH_TOKEN_MARGIN
    # 머리말 한 번 + 항목 10개가 들어가는 한도. 머리말을 항목마다 세면 여러 묶음으로 쪼개진다.
    max_tokens = empty + BATCH_TOKEN_MARGIN + per_item * 10 + 10

    batches = token_batches(items, build, max_tokens=max_tokens, max_items=100)

    assert batches == [items]
