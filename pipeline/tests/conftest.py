import os

# 콜백 토큰은 운영 기본값을 두지 않아 미설정이면 기동이 막힌다. 테스트에서만 고정값을 준다.
os.environ.setdefault("INTERNAL_CALLBACK_TOKEN", "test-internal-callback")
