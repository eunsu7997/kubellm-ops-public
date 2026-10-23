"""Gateway Prometheus 지표. /metrics에서 노출한다."""
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

REQUEST_COUNT = Counter(
    "gateway_requests_total",
    "Gateway가 받은 전체 /v1/chat/completions 요청 수",
)

SUCCESS_COUNT = Counter(
    "gateway_requests_success_total",
    "Gateway가 성공(2xx)으로 처리한 요청 수",
)

ERROR_COUNT = Counter(
    "gateway_requests_error_total",
    "Gateway가 오류로 처리한 요청 수",
    ["status"],
)

BACKEND_FAILURE_COUNT = Counter(
    "gateway_backend_failures_total",
    "백엔드(vLLM/mock) 호출 자체가 실패한 횟수 (연결 실패/타임아웃/5xx)",
    ["reason"],
)

# stream=true 전달 도중 클라이언트 쪽 사유(연결 종료, 작업 취소, 제너레이터 조기 종료)로
# 멈춘 횟수. 백엔드 장애가 아니므로 gateway_backend_failures_total과 따로 센다.
CLIENT_DISCONNECT_COUNT = Counter(
    "gateway_client_disconnects_total",
    "stream=true 전달 도중 클라이언트 연결 종료/작업 취소로 중단된 요청 수",
)

LATENCY_BUCKETS =(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 20, 30)

# P50/P95/P99 계산이 가능하도록 histogram으로 latency를 기록한다.
REQUEST_LATENCY = Histogram(
    "gateway_request_latency_seconds",
    "Gateway가 /v1/chat/completions 요청을 처리하는 데 걸린 시간(초)",
    buckets=LATENCY_BUCKETS,
)

# 결과(outcome)와 stream 여부로 나눈 처리 시간. 성공 요청만의 지연 SLO를 PromQL로 계산할 수 있다.
# outcome: success | client_error | backend_error | stream_error | client_disconnect | rejected
REQUEST_DURATION = Histogram(
    "gateway_request_duration_seconds",
    "Chat completion duration split by outcome and stream mode",
    ["outcome", "stream"],
    buckets=LATENCY_BUCKETS,
)

INFLIGHT = Gauge(
    "gateway_inflight_requests", "Chat completions currently being processed or streamed by this Gateway",
)
ADMISSION_REJECTED = Counter(
    "gateway_admission_rejected_total", "Requests rejected with 429 because GATEWAY_MAX_INFLIGHT was reached",
)

# stream=true 요청에서 요청 수신부터 백엔드의 첫 청크가 도착할 때까지 걸린 시간(TTFT 근사치).
TIME_TO_FIRST_CHUNK = Histogram(
    "gateway_time_to_first_chunk_seconds",
    "stream=true 요청에서 Gateway가 백엔드의 첫 응답 청크를 받기까지 걸린 시간(초)",
    buckets=LATENCY_BUCKETS,
)

READINESS_ENABLED = Gauge(
    "gateway_backend_readiness_enabled", "1 when backend/inference readiness is enabled, otherwise 0",
)
BACKEND_READY = Gauge(
    "gateway_backend_ready", "Last checked backend state: -1 unknown, 0 failed, 1 ready; cached observation",
)
READINESS_CHECKS = Counter(
    "gateway_backend_readiness_checks_total", "Actual backend readiness checks, excluding cache hits", ["result"],
)
READINESS_CACHE_HITS = Counter(
    "gateway_backend_readiness_cache_hits_total", "Readiness calls served from cached observation",
)
READINESS_DURATION = Histogram(
    "gateway_backend_readiness_check_seconds", "Actual backend readiness check duration", buckets=LATENCY_BUCKETS,
)
READINESS_FAILURES = Counter(
    "gateway_backend_readiness_failures_total",
    "Bounded readiness failure categories without exception details", ["reason"],
)


def generate_metrics() -> bytes:
    return generate_latest()


__all__ = [
    "REQUEST_COUNT",
    "SUCCESS_COUNT",
    "ERROR_COUNT",
    "BACKEND_FAILURE_COUNT",
    "CLIENT_DISCONNECT_COUNT",
    "REQUEST_LATENCY",
    "TIME_TO_FIRST_CHUNK",
    "REQUEST_DURATION",
    "INFLIGHT",
    "ADMISSION_REJECTED",
    "READINESS_ENABLED",
    "BACKEND_READY",
    "READINESS_CHECKS",
    "READINESS_CACHE_HITS",
    "READINESS_DURATION",
    "READINESS_FAILURES",
    "generate_metrics",
    "CONTENT_TYPE_LATEST",
]
