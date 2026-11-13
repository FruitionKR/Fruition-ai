import json
from pathlib import Path
from app.modules.query.infrastructure.product_help import build_query_product_help, ROUTING_PROMPT
port=build_query_product_help(provider='openai',model='gpt-6-luna')
cases=[
 ('fruition_mcp','Fruition MCP 연결은 어떻게 해?',[], 'product_help'),
 ('mcp_tutorial','Fruition MCP에서 튜토리얼은 어떻게 봐?',[], 'product_help'),
 ('ko_wiki_colloquial','위키편입 어케하는거임?',[], 'product_help'),
 ('ko_wiki_refresh','위키 최신화는 어디서 해?',[], 'product_help'),
 ('en_add_wiki','Where is Add to wiki?',[], 'product_help'),
 ('wiki_implementation','내 앱에 위키 편입 기능을 구현하려면?',[], 'query'),
 ('wiki_action','이 문서를 위키에 편입해줘',[], 'query'),
 ('generic_mcp','MCP 프로토콜이 뭐야?',[], 'query'),
 ('ko_help','Fruition 사용법 알려줘',[], 'product_help'),
 ('en_help','How do I enable web search by default in Fruition?',[], 'product_help'),
 ('ko_ambiguous','이 앱은 어떻게 작동해?',[], 'ambiguous'),
 ('en_ambiguous','How does this app work?',[], 'ambiguous'),
 ('own_app','우리 앱에 Fruition 같은 기능을 구현하려면?',[], 'query'),
 ('own_app_context','이 앱은 어떻게 작동해?',[{'role':'user','content':'내가 만든 예약 앱은 매일 아침 예약 목록을 보여 줘.'}], 'query'),
 ('followup','그 설정은 어디 있어?',[{'role':'assistant','content':'Fruition 웹 검색 기본값은 AI 설정에서 바꿀 수 있습니다.'}], 'product_help'),
 ('new_request','이 문서를 요약해 줘',[{'role':'assistant','content':'Fruition 웹 검색 기본값은 AI 설정에서 바꿀 수 있습니다.'}], 'query'),
]
rows=[]
for name,question,history,expected in cases:
 actual=port._client.complete_json(ROUTING_PROMPT,json.dumps({'message':question,'recent_messages':history},ensure_ascii=False))
 rows.append(dict(id=name,question=question,history=history,expected=expected,actual=actual,passed=actual.get('action')==expected))
 print(name,actual.get('action'), 'PASS' if rows[-1]['passed'] else 'FAIL',flush=True)
Path('/tmp/product-help-routing.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
assert all(r['passed'] for r in rows)
