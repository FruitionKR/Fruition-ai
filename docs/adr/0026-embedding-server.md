# 0026. Serve Short-Text BGE-M3 Embeddings from a Dedicated Server

## Status
Proposed

## Context
BGE-M3(약 2.5GB)를 쓰는 query·agent·maintenance 워커는 pod마다 모델을 따로 올리고 2.5Gi씩 요청한다. KEDA 최대 복제 수로 늘면 모델이 최대 10벌 올라가고, AI 워커 메모리 요청 합계가 약 33Gi로 AI 노드(Spot `m5.xlarge` 최대 2대, 쓸 수 있는 메모리 약 28GiB)를 넘는다. 개념·클러스터 판정에 후보 좁히기를 적용하려면 ingest 워커에도 임베딩이 필요한데, ingest에 모델을 더 올리면 합계가 43Gi를 넘는다.

CPU 4스레드 측정에서 질문 1개 임베딩은 14ms, 개념 후보 100개는 0.4초였다. 최대 길이(8,192토큰) 페이지 2개는 4.6초가 걸려, 같은 프로세스에서 섞이면 실시간 질문이 수 초씩 밀린다.

## Decision
- `fruition-pipeline` 이미지에 짧은 텍스트 전용 임베딩 서버(`app.modules.wiki_embedding.interfaces.http.embedding_server`)를 별도 진입점으로 둔다. `POST /embeddings`, `GET /health`(모델 적재 완료 시 정상), `X-Internal-Token` 인증.
- 서버는 요청당 64개, 텍스트당 4,000자까지만 받고 encode를 차례로 처리한다.
- 워커는 `EMBEDDING_SERVICE_URL`이 있으면 `RemoteEmbeddingModel`로 서버를 호출한다. 응답의 모델 이름이 저장 벡터의 모델 이름과 다르면 실패시킨다. 서버 오류(5xx)와 연결 실패는 한 번 재시도하고, 타임아웃은 서버 부하를 키우지 않도록 재시도하지 않는다. 요청당 64개를 넘으면 나눠 보낸다.
- 원격 임베딩을 쓰는 Query는 저장 벡터가 없는 문서 중 4,000자 이하(섹션 등)만 질문 시점에 서버로 임베딩한다. 더 긴 문서는 임베딩 점수를 0으로 두고 hybrid의 BM25 점수에만 맡긴다. 벡터가 없는 문서만 따로 BM25로 점수를 매기면 그 안에서 최댓값으로 정규화돼 코사인 점수와 눈금이 맞지 않는다.
- 긴 페이지 임베딩(인제스트 후 page·unit 임베딩)은 maintenance 워커가 계속 프로세스에 모델을 올려 처리한다.
- 서버는 복제본 1개로 시작한다(데브옵스 합의).

## Alternatives
- **Hugging Face Text Embeddings Inference(TEI):** 검증된 서버지만 새 이미지와 운영 대상이 하나 늘어난다. 모델이 이미 들어 있는 `fruition-pipeline` 이미지에 엔드포인트를 두기로 데브옵스와 합의했다.
- **ingest 워커에도 모델 적재:** AI 노드 메모리를 더 넘긴다.
- **개념 판정을 maintenance 워커로 이동:** 인제스트 단계 구조를 크게 바꿔야 한다.
- **원격에서도 긴 문서를 즉석 임베딩:** 서버 하나에서 실시간 질문을 수 초씩 막는다.
- **벡터가 없는 문서를 BM25로 대체:** 대상 문서 안에서만 정규화돼 관련성과 상관없이 1.0이 나올 수 있다.

## Consequences
- 로컬 실측에서 원격과 로컬 벡터가 같았고(코사인 1.0), 원격 호출로 늘어난 지연은 질문 1개 약 2ms, 32개 묶음 약 6ms였다.
- query·agent의 모델 메모리 패치를 제거하면 최대 부하 메모리 요청 합계가 약 20Gi 안팎으로 줄어든다(추정).
- 서버가 재시작되는 동안(모델 적재 약 수십 초) 실시간 질문의 임베딩이 실패한다. 복제본 1개의 단일 장애 지점이다.
- 저장 벡터가 없는 4,000자 초과 문서는 원격 모드에서 임베딩 점수 없이 BM25만으로 비교된다. 운영 전환 전에 즉석 임베딩 빈도를 확인한다.
- `EMBEDDING_SERVICE_URL`이 없으면 기존 동작과 같다.
