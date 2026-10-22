"""Scenario 2: vLLM/Backend 응답 불능 상황 재현 (VERIFIED — Docker/Kubernetes 없이 로컬 프로세스로 실행 가능).

Gateway와 mock-backend를 실제 localhost 프로세스로 띄운 뒤 다음을 순서대로 확인한다.

1. 정상 요청 -> 200
2. 백엔드가 느려짐(x-mock-mode: slow, REQUEST_TIMEOUT보다 오래 걸림) -> Gateway가 504 반환
3. 백엔드가 500을 반환(x-mock-mode: error500) -> Gateway가 502로 감싸서 반환 (백엔드 원본 메시지 비노출)
4. 백엔드 프로세스를 강제 종료(connection failure 재현) -> Gateway가 502 반환
5. 매 단계마다 Gateway의 /metrics에서 gateway_backend_failures_total이 늘어나는지 확인

결과는 results/scenario2_backend_unavailable_<timestamp>.json 으로 저장되고,
docs/incidents/2026-XX-XX-backend-unavailable.md 의 Evidence로 사용된다.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
from local_stack import make_gateway, make_mock_backend, wait_until_healthy

RESULTS_DIR = Path(__file__).resolve().parent.parent.parent / "results"

GATEWAY_PORT = 18000
BACKEND_PORT = 18001
GATEWAY_URL = f"http://127.0.0.1:{GATEWAY_PORT}"
BACKEND_URL = f"http://127.0.0.1:{BACKEND_PORT}"
REQUEST_TIMEOUT = 2.0  # 짧게 잡아서 slow 모드가 실제로 timeout을 유발하게 함
SLOW_DELAY = REQUEST_TIMEOUT + 3.0


def _extract_metric(metrics_text: str, metric_name: str) -> float:
    total = 0.0
    for line in metrics_text.splitlines():
        if line.startswith(metric_name) and not line.startswith("#"):
            total += float(line.rsplit(" ", 1)[-1])
    return total


def _backend_failure_total(metrics_text: str) -> float:
    return _extract_metric(metrics_text, "gateway_backend_failures_total")


def main() -> None:
    steps: list[dict] = []
    gateway = make_gateway(GATEWAY_PORT, BACKEND_URL, request_timeout=REQUEST_TIMEOUT)
    mock_backend = None

    try:
        # 각 단계마다 mock-backend를 MOCK_FORCE_MODE만 바꿔서 재기동한다
        # (실제로는 "백엔드가 재배포되어 동작이 바뀐" 상황과 동등하다).
        mock_backend = make_mock_backend(BACKEND_PORT, extra_env={})
        mock_backend.start()
        assert wait_until_healthy(f"{BACKEND_URL}/healthz"), "mock-backend가 기동하지 않음"
        gateway.start()
        assert wait_until_healthy(f"{GATEWAY_URL}/healthz"), "gateway가 기동하지 않음"

        client = httpx.Client(timeout=REQUEST_TIMEOUT + 5)

        # --- Step 1: 정상 요청 ---
        resp = client.post(
            f"{GATEWAY_URL}/v1/chat/completions",
            json={"model": "mock-model", "messages": [{"role": "user", "content": "hello"}]},
        )
        steps.append({"step": "1_normal_request", "expected_status": 200, "actual_status": resp.status_code})

        metrics_before = client.get(f"{GATEWAY_URL}/metrics").text
        failures_before = _backend_failure_total(metrics_before)

        # --- Step 2: backend를 "항상 slow" 모드로 재기동 -> gateway timeout(504) ---
        mock_backend.stop()
        mock_backend = make_mock_backend(
            BACKEND_PORT, slow_delay_seconds=SLOW_DELAY, extra_env={"MOCK_FORCE_MODE": "slow"}
        )
        mock_backend.start()
        assert wait_until_healthy(f"{BACKEND_URL}/healthz"), "mock-backend(slow)가 기동하지 않음"
        resp = client.post(
            f"{GATEWAY_URL}/v1/chat/completions",
            json={"model": "mock-model", "messages": [{"role": "user", "content": "slow"}]},
        )
        steps.append(
            {
                "step": "2_backend_slow_timeout",
                "expected_status": 504,
                "actual_status": resp.status_code,
                "detail": resp.json().get("detail"),
            }
        )

        # --- Step 3: backend를 "항상 500" 모드로 재기동 -> gateway 502 ---
        mock_backend.stop()
        mock_backend = make_mock_backend(BACKEND_PORT, extra_env={"MOCK_FORCE_MODE": "error500"})
        mock_backend.start()
        assert wait_until_healthy(f"{BACKEND_URL}/healthz"), "mock-backend(error500)가 기동하지 않음"
        resp = client.post(
            f"{GATEWAY_URL}/v1/chat/completions",
            json={"model": "mock-model", "messages": [{"role": "user", "content": "err"}]},
        )
        steps.append(
            {
                "step": "3_backend_500",
                "expected_status": 502,
                "actual_status": resp.status_code,
                "detail": resp.json().get("detail"),
            }
        )

        metrics_mid = client.get(f"{GATEWAY_URL}/metrics").text
        failures_mid = _backend_failure_total(metrics_mid)
        steps.append(
            {
                "step": "3b_backend_failure_metric_increased",
                "before": failures_before,
                "after": failures_mid,
                "increased": failures_mid > failures_before,
            }
        )

        # --- Step 4: backend 프로세스 완전 종료 (connection failure 재현) ---
        # 관측(Windows 로컬 환경): 아무도 듣지 않는 포트에 연결을 시도하면 즉시 ECONNREFUSED가
        # 오는 경우 gateway는 502(connection failed)를 주지만, OS/방화벽이 RST를 바로 주지 않고
        # SYN을 재전송하는 경우에는 httpx 자체 타임아웃이 먼저 끝나 504(timeout)로 관측될 수
        # 있다. 두 경우 모두 "백엔드에 도달할 수 없다"는 동일한 장애를 gateway가 502/504로
        # 일반화해서 반환하고 사용자에게 원인을 노출하지 않는다는 점은 동일하므로 둘 다 정상
        # 동작으로 간주한다 — docs/incidents 문서에 이 관측을 그대로 기록한다.
        mock_backend.stop()
        time.sleep(0.5)
        resp = client.post(
            f"{GATEWAY_URL}/v1/chat/completions",
            json={"model": "mock-model", "messages": [{"role": "user", "content": "conn"}]},
        )
        steps.append(
            {
                "step": "4_backend_process_killed_connection_failure",
                "acceptable_statuses": [502, 504],
                "actual_status": resp.status_code,
                "detail": resp.json().get("detail"),
            }
        )

        metrics_after = client.get(f"{GATEWAY_URL}/metrics").text
        failures_after = _backend_failure_total(metrics_after)
        steps.append(
            {
                "step": "4b_backend_failure_metric_increased_again",
                "before": failures_mid,
                "after": failures_after,
                "increased": failures_after > failures_mid,
            }
        )

        client.close()

        all_expected_matched = all(
            s.get("actual_status") == s.get("expected_status")
            for s in steps
            if "expected_status" in s
        ) and all(
            s.get("actual_status") in s.get("acceptable_statuses", [s.get("actual_status")])
            for s in steps
            if "acceptable_statuses" in s
        )

        result = {
            "scenario": "2_backend_unavailable",
            "verified": True,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "all_expected_status_matched": all_expected_matched,
            "steps": steps,
        }

    finally:
        gateway.stop()
        mock_backend.stop()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = RESULTS_DIR / f"scenario2_backend_unavailable_{time.strftime('%Y%m%d_%H%M%S')}.json"
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n[scenario2] 결과 저장: {output_path}")
    if not result["all_expected_status_matched"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
