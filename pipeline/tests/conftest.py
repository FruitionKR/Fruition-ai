import os

# 콜백 토큰은 운영 기본값을 두지 않아 미설정이면 기동이 막힌다. 테스트에서만 고정값을 준다.
os.environ.setdefault("INTERNAL_CALLBACK_TOKEN", "test-internal-callback")

# 모델 허용 목록은 운영 카탈로그 3개가 기본이다. 테스트는 여러 모델 id를 쓰므로 검사를 끄고,
# 허용 목록 자체는 test_llm_env에서 환경변수를 바꿔 검증한다.
os.environ.setdefault("AI_ALLOWED_LLM_MODELS", "*")
