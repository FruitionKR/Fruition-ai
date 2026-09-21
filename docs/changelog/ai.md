# AI 변경 기록

## 2026-09-21 (converter CPU 전용 torch)

- converter 이미지가 PyPI linux torch 2.14.0 wheel의 의존성으로 `cuda-toolkit 13`, `nvidia-cudnn-cu13`, `nvidia-nccl-cu13`, `nvidia-cusparselt-cu13`, `nvidia-nvshmem-cu13`, `triton` 등 수 GiB의 GPU 패키지를 함께 설치해, 운영 EKS CPU 노드(20GiB)에서 이미지 압축 해제 중 디스크가 고갈되던 문제를 수정했습니다. 기존 converter는 계속 운영 중이며 이 변경은 이미지 빌드·의존성만 바꿉니다.
- `converter/requirements.txt`가 공식 CPU index(`https://download.pytorch.org/whl/cpu`)의 `torch==2.14.0+cpu`·`torchvision==0.29.0+cpu`를 고정합니다. pipeline 이미지가 이미 쓰던 CPU index 방식과 같고, `docling-ibm-models`(torch>=2.2.2,<3)·`pix2tex`(torch>=1.7.1)의 요구 범위 안이며 PyPI에서 해석되던 버전과 동일해 OCR·문서 복원 동작은 그대로입니다. x86_64·aarch64 wheel이 모두 있어 로컬 arm64 빌드도 됩니다.
- 새 `converter/check_cpu_only.py`가 이미지 빌드 중 설치 환경을 검사해 `+cpu`가 아닌 torch/torchvision, `nvidia-*`·`cuda-*`·`triton` 패키지, CUDA 가용성이 있으면 빌드를 실패시키고 `pip check`도 실행합니다. CI는 같은 스크립트로 linux/amd64 dry-run 해석 결과(`pip --report`)를 검사합니다.
- 첫 native amd64 게시 빌드(run 35585856945)에서 pip는 `torch-2.14.0+cpu`·`torchvision-0.29.0+cpu`만 설치하고 nvidia 패키지가 없음을 확인했으나, 새 런타임 검사가 `cv2` import의 `libGL.so.1` 부재를 잡아 빌드를 중단했습니다. `rapidocr`(docling 표준 의존성)이 GUI 빌드 `opencv-python`을 요구하므로 apt에 `libgl1`을 추가했습니다. 기존 이미지도 같은 opencv를 담고 있었고 검사 없이 통과되던 상태입니다.
- 검증: PR CI에서 linux/amd64 dry-run 해석 검사·converter 테스트 13개(신규 5개) 통과. 실제 CPU 런타임 검사는 게시 workflow의 native 빌드에서 강제됩니다. DB schema·API 변경은 없습니다.

## 2026-09-21

- converter에 `/convert-source-batch`를 추가했습니다. 승인된 S3 HTTPS 호스트(`CONVERTER_SOURCE_HOSTS`)에서 1MiB Range 요청과 16MiB 캐시로 PDF를 읽고 pypdf로 페이지 묶음(`PDF_PAGES_PER_BATCH` 기본 10, 1~50)만 추출해 기존 복원 엔진에 전달합니다. 원본 전체를 디스크·메모리에 내려받지 않으며 단일 PDF object 읽기는 64MiB로 제한합니다. `PDF_BATCH_CONCURRENCY`(기본 1)로 동시 배치 수를 제한합니다.
- wiki 생성의 packet future를 설정된 worker 수만큼만 대기/실행하도록 바꿨습니다. 전체 packet future를 한꺼번에 만들지 않습니다.
- 결과 없이 중단된 agent 턴(`agent_runs.status=executing`)의 Kafka 재전달로 worker가 `Agent run is already executing`을 던지며 종료·재시작을 반복하던 문제를 수정했습니다. 실행 잠금을 보유한 상태의 `executing`은 고아 실행이므로 `failed`(`agent_turn_interrupted`)로 commit한 뒤 durable 실패 이벤트를 내고 offset을 진행합니다. 편집 부작용 중복을 막기 위해 재실행하지 않습니다.
- CI가 `converter/test_app.py`를 실행하도록 추가했습니다. 3GiB sparse 원본의 Range 페이지 추출·체크포인트 테스트 8개, 실제 PostgreSQL에서 interrupted 실패가 commit되는 테스트 1개를 포함해 pipeline 1,279개·하위 170개를 통과했습니다(격리 DSN·실모델 조건부 12개 skip). DB schema 변경은 없습니다.

## 2026-09-20

- ingest 최종 처리와 결과 저장이 같은 workspace의 PostgreSQL 잠금을 서로 다른 연결로 중복 획득해 자기 자신을 기다리던 오류를 수정했습니다.
- 같은 DB·workspace·run의 동기 중첩 호출은 현재 스레드가 가진 잠금을 재사용합니다. 다른 작업자나 run의 상호 배제와 최외곽 종료 시 잠금 해제는 유지합니다. DB schema 변경은 없습니다.
- 실제 PostgreSQL에서 기존 코드의 timeout을 재현했고, 중첩·동시 작업자·다른 run·예외 해제를 포함한 잠금 테스트 10개를 통과했습니다. 전체 AI 테스트는 1,265개·하위 테스트 170개 통과, 기존 조건부 테스트 24개 skip입니다. 새 PostgreSQL 잠금 테스트 4개는 모두 실행했습니다.

## 2026-09-17

- AWS Kafka mTLS에 맞춰 worker의 모든 producer·consumer에 공통 SSL 연결 설정을 적용했습니다. 인증서·호스트를 검증하며 설정 오류 시 평문으로 전환하지 않습니다.
- 로컬 PLAINTEXT 기본값은 유지합니다. TLS 설정·생성 지점 검증 테스트 5개를 통과했습니다.
- 플랫폼 KafkaUser/인증서 설정과 새 이미지를 함께 배포해야 하며, 인증서 갱신 후 클라이언트 순차 재시작이 필요합니다.
