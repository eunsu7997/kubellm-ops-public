"""Gateway 환경변수 설정.

pydantic-settings 같은 별도 라이브러리 없이 표준 라이브러리 os.environ만으로 읽는다
(불필요한 dependency를 늘리지 않기 위해서다).
"""
import math
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # OpenAI 호환 백엔드 주소. 로컬 검증에서는 mock-backend를 가리킨다.
    vllm_base_url: str = os.environ.get("VLLM_BASE_URL", "http://localhost:8001")
    # 실제로 호출할 모델 이름 (vLLM/mock 양쪽에 전달됨)
    model_name: str = os.environ.get("MODEL_NAME", "mock-model")
    # 백엔드(vLLM/mock) 호출 타임아웃(초). 너무 크면 장애 시 사용자가 오래 기다리고,
    # 너무 작으면 정상 응답도 타임아웃 처리될 수 있다.
    request_timeout: float = float(os.environ.get("REQUEST_TIMEOUT", "30"))
    # Opt-in: local preserves the historical readiness contract.
    readiness_mode: str = os.environ.get("READINESS_MODE", "local")
    readiness_timeout: float = float(os.environ.get("READINESS_TIMEOUT", "2"))
    readiness_cache_seconds: float = float(os.environ.get("READINESS_CACHE_SECONDS", "5"))
    # Opt-in admission: 0 disables the limit. When set, requests beyond this many in-flight
    # chat completions get 429 with Retry-After instead of queueing inside vLLM.
    max_inflight: int = int(os.environ.get("GATEWAY_MAX_INFLIGHT", "0"))
    retry_after_seconds: int = int(os.environ.get("GATEWAY_RETRY_AFTER_SECONDS", "1"))

    def __post_init__(self):
        if self.readiness_mode not in {"local", "backend", "inference"}:
            raise ValueError("READINESS_MODE must be local, backend or inference")
        if not math.isfinite(self.readiness_timeout) or not 0 < self.readiness_timeout <= 10:
            raise ValueError("READINESS_TIMEOUT must be in (0, 10]")
        if not math.isfinite(self.readiness_cache_seconds) or not 0 < self.readiness_cache_seconds <= 60:
            raise ValueError("READINESS_CACHE_SECONDS must be in (0, 60]")
        if self.max_inflight < 0:
            raise ValueError("GATEWAY_MAX_INFLIGHT must be >= 0 (0 disables the limit)")
        if not 1 <= self.retry_after_seconds <= 60:
            raise ValueError("GATEWAY_RETRY_AFTER_SECONDS must be in [1, 60]")


settings = Settings()
