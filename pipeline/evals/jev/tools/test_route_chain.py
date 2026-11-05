"""run_route_chain의 순수 함수 확인. 사용: PYTHONPATH=<dev>/pipeline python test_route_chain.py"""
from run_route_chain import goal_format_conflict

assert goal_format_conflict("checklist", ["numbered_list"]) == "list_goal_with_numbered_list"
assert goal_format_conflict("bullet_list", ["numbered_list"]) == "list_goal_with_numbered_list"
assert goal_format_conflict("convert_format", []) == "convert_format_without_format"
assert goal_format_conflict("checklist", []) is None
assert goal_format_conflict("convert_format", ["numbered_list"]) is None
assert goal_format_conflict("shorten", []) is None
print("ok")
