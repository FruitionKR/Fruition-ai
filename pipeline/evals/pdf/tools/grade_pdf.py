"""PDF Converter 결과를 정답(gold/<id>.json)과 코드로 대조한다. 모델 호출은 없다.

사용: python grade_pdf.py ../results/<날짜>
결과: <결과폴더>/grades.jsonl(사례·회차별), summary.json(유형별·전체)

정답 형식(gold/<id>.json):
  sentences: 원문 순서대로 꼭 남아야 할 문장·제목 목록
  tables:    [{"rows": [[셀, ...], ...]}] 표마다 남아야 할 행(머리행 포함)
판정:
- 내용 누락: 정규화한 결과에 문장이 없으면 1건. 정규화는 NFKC, Markdown·HTML 기호·공백·대시·물결·글머리 기호 제거다(줄바꿈·띄어쓰기·대시·글머리 글자 차이는 오류가 아니다).
- 순서 오류: 찾은 문장끼리 원문 순서가 뒤바뀐 이웃 쌍마다 1건.
- 표 오류: 결과의 어떤 표 행(Markdown `|` 행 또는 HTML <tr>)에도 정답 행의 셀 내용이 순서대로 들어 있지 않으면 행마다 1건.
  셀을 합치거나 나눈 것(예: 값과 증감률을 한 셀에)은 오류가 아니고, 값이 빠지거나 바뀌거나 순서가 뒤바뀌면 오류다.
- 복원율(본 지표): (필수 문장 수 - 내용 누락) / 필수 문장 수. 표 행 복원율은 (정답 표 행 수 - 표 오류) / 정답 표 행 수.
- 변환 성공(참고): 실행이 끝나고 누락·순서·표 오류가 모두 0건.
"""
import html
import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[1]


def norm(text: str) -> str:
    text = html.unescape(unicodedata.normalize("NFKC", text))
    # HTML 태그만 지운다. 원문의 "< 종사자 수 동향 >" 같은 꺾쇠 제목은 남긴다.
    text = re.sub(r"</?[A-Za-z][^>]*>", "", text)
    # 빼기·대시(-−–—), 물결(~∼), 글머리·가운뎃점(・•·)은 글꼴·변환기마다 다른 글자로 나오므로 같은 것으로 본다(지운다).
    return re.sub(r"[\s*_`#>|\\\-−–—~∼・•·]+", "", text).lower()


def table_rows(markdown: str) -> list[list[str]]:
    rows = [[norm(c) for c in line.strip().strip("|").split("|")]
            for line in markdown.splitlines() if line.strip().startswith("|")]
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", markdown, flags=re.S | re.I):
        rows.append([norm(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, flags=re.S | re.I)])
    return rows


def row_found(gold_row: list[str], rows: list[list[str]]) -> bool:
    """정답 행의 셀들이 결과의 한 행 안에 순서대로 들어 있으면 찾은 것이다. 셀을 합치거나 나눈 것은 오류가 아니다."""
    # "-" 같은 빈 값 표시는 정규화하면 비므로 비교하지 않는다.
    cells = [n for n in (norm(c) for c in gold_row) if n]
    for row in rows:
        joined, position = "".join(row), 0
        for cell in cells:
            position = joined.find(cell, position)
            if position < 0:
                break
            position += len(cell)
        else:
            return True
    return False


def grade(markdown: str, gold: dict) -> dict:
    text = norm(markdown)
    positions = [text.find(norm(s)) for s in gold["sentences"]]
    missing = [s for s, p in zip(gold["sentences"], positions) if p < 0]
    found = [p for p in positions if p >= 0]
    order_errors = sum(1 for a, b in zip(found, found[1:]) if a > b)
    rows = table_rows(markdown)
    table_misses = [r for t in gold.get("tables", []) for r in t["rows"] if not row_found(r, rows)]
    return {"missing": len(missing), "order_errors": order_errors, "table_errors": len(table_misses),
            "sentences": len(gold["sentences"]), "table_rows": sum(len(t["rows"]) for t in gold.get("tables", [])),
            "missing_examples": missing[:3], "table_examples": table_misses[:2]}


def main() -> None:
    out = Path(sys.argv[1])
    grades = []
    for row in map(json.loads, (out / "rows.jsonl").open()):
        gold = json.loads((EVAL_DIR / "gold" / f"{row['id']}.json").read_text())
        g = {"id": row["id"], "type": row["type"], "run": row["run"], "status": row["status"], "seconds": row.get("seconds")}
        if row["status"] == "completed":
            g.update(grade((out / row["markdown"]).read_text(encoding="utf-8"), gold))
            g["ok"] = g["missing"] == g["order_errors"] == g["table_errors"] == 0
        else:
            g["ok"] = False
        grades.append(g)
    (out / "grades.jsonl").write_text("".join(json.dumps(g, ensure_ascii=False) + "\n" for g in grades))

    groups = defaultdict(list)
    for g in grades:
        groups[g["type"]].append(g)
        groups["전체"].append(g)
    summary = {}
    for name, items in groups.items():
        done = [g for g in items if g["status"] == "completed"]
        summary[name] = {
            "runs": len(items), "completed": len(done), "ok": sum(g["ok"] for g in items),
            "missing": sum(g["missing"] for g in done), "sentences": sum(g["sentences"] for g in done),
            "order_errors": sum(g["order_errors"] for g in done),
            "table_errors": sum(g["table_errors"] for g in done), "table_rows": sum(g["table_rows"] for g in done),
            "seconds_avg": round(sum(g["seconds"] for g in done) / len(done), 1) if done else None,
        }
        # 복원율: 필수 문장 중 결과에 남은 비율(본 지표). 표 행 복원율도 같은 방식이다.
        s = summary[name]
        s["restored_rate"] = round(1 - s["missing"] / s["sentences"], 4) if s["sentences"] else None
        s["table_restored_rate"] = round(1 - s["table_errors"] / s["table_rows"], 4) if s["table_rows"] else None
        print(name, summary[name])
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n")


if __name__ == "__main__":
    main()
