# AI 임베딩 서버 API

[서비스 문서](../README.md) · [API 목차](README.md)

짧은 텍스트 전용 BGE-M3 임베딩 서버입니다. pipeline API와 별도 프로세스(`uvicorn app.modules.wiki_embedding.interfaces.http.embedding_server:app`)로 실행하며, ai-svc 워커가 `EMBEDDING_SERVICE_URL`로 호출합니다. 결정 배경은 [ADR-0026](../adr/0026-embedding-server.md)을 따릅니다.

| Method + Path | 인증 | 입력 | 출력·목적 |
|---|---|---|---|
| `POST /embeddings` | `X-Internal-Token` | `texts`: 문자열 1~64개, 각 4,000자 이하 | `200`: `model`(예: `BAAI/bge-m3`), `vectors`(입력 순서대로 정규화된 벡터) |
| `GET /health` | 없음 | - | `200`: 모델 적재 완료(`status`, `model`). 적재 중 `503` |

- 인증 토큰은 pipeline API와 같은 `INTERNAL_CALLBACK_TOKEN`입니다. 서버에 설정되지 않았으면 `503`, 토큰이 없거나 다르면 `401`입니다.
- 텍스트가 4,000자를 넘으면 `413`, 개수가 0개이거나 64개를 넘으면 `422`입니다. 모델 적재 전 요청은 `503`입니다.
- 요청은 한 번에 하나씩 처리합니다. 긴 페이지 임베딩은 받지 않으며 maintenance 워커가 프로세스에 모델을 올려 처리합니다.

## 연동

- 인바운드 호출자: ai-svc Query·Agent(`RemoteEmbeddingModel`, `EMBEDDING_SERVICE_URL`이 설정된 경우). 서버 오류(5xx)와 연결 실패는 한 번 재시도하고, 응답 `model`이 저장 벡터의 모델 이름과 다르면 실패시킵니다.
- 아웃바운드 호출: 없음.
- 미연동 표시: 배포 매니페스트(Fruition-flatform)와 `EMBEDDING_SERVICE_URL` 설정 전에는 호출자가 없습니다.
