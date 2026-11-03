"""grade_pdf 판정 확인(모델 호출 없음). 사용: python test_grade_pdf.py"""
from grade_pdf import grade

GOLD = {"sentences": ["# 제1장 총칙", "근로시간은 1주 40시간을 초과할 수 없다.", "휴게시간은 근로시간 도중에 준다."],
        "tables": [{"rows": [["구분", "2025", "2026"], ["취업자", "2,880", "2,912"]]}]}

ok = """## 제1장 총칙
근로시간은 1주 40시간을
초과할 수 없다.
**휴게시간은** 근로시간 도중에 준다.
| 구분 | 2025 | 2026 |
|---|---|---|
| 취업자 | 2,880 | 2,912 |
"""
g = grade(ok, GOLD)
assert (g["missing"], g["order_errors"], g["table_errors"]) == (0, 0, 0), g

swapped = "휴게시간은 근로시간 도중에 준다.\n근로시간은 1주 40시간을 초과할 수 없다.\n<table><tr><th>구분</th><th>2025</th><th>2026</th></tr><tr><td>취업자</td><td>2,912</td><td>2,880</td></tr></table>"
g = grade(swapped, GOLD)
assert (g["missing"], g["order_errors"], g["table_errors"]) == (1, 1, 1), g
# 빼기 기호(U+2212)와 물결(U+223C)은 ASCII와 같게 본다.
g = grade("| 구분 | 1∼9월 |\n|---|---|\n| 증감 | −0.2 |", {"sentences": [], "tables": [{"rows": [["구분", "1~9월"], ["증감", "-0.2"]]}]})
assert g["table_errors"] == 0, g
# 꺾쇠로 감싼 원문 제목은 HTML 태그로 지우지 않는다.
g = grade("<p>< 종사자 수 동향(300인 미만) ></p>", {"sentences": ["< 종사자 수 동향(300인 미만) >", "< 빈 일자리 수 동향(300인 이상) >"], "tables": []})
assert g["missing"] == 1, g
# 값과 증감률을 한 셀에 합친 것은 표 오류가 아니다.
g = grade("| 연도 | 종사자 |\n|---|---|\n| 2021 | 18,745<br>(1.8) |", {"sentences": [], "tables": [{"rows": [["2021", "18,745", "(1.8)"]]}]})
assert g["table_errors"] == 0, g
print("ok")
